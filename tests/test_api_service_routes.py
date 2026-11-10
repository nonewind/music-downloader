"""内置 API 服务控制 / 扫码登录 / 账号测试 路由的行为基线

三平台的这几组路由结构相同，路由层按平台表驱动注册；本文件锁定对外
契约（URL、响应码、文案、字段），防止重构时某个平台漏注册或语义漂移。
外部依赖（bridge 进程、平台 API）全部打桩。
"""

from datetime import datetime

import pytest

import routes.api as api_module
from models import Account, db

PLATFORM_ROUTES = [
    # (路由前缀, bridge 模块属性名, 平台标识, 服务中文名)
    ("ncm", "bridge", "netease", "网易云API服务"),
    ("qq", "qq_bridge", "qq", "QQ音乐API服务"),
    ("kugou", "kugou_bridge", "kugou", "酷狗音乐API服务"),
]


class _StubBridge:
    def status(self):
        return {"running": True, "port": 1}


@pytest.fixture
def client(webapp_app):
    (app,) = webapp_app
    from routes.api import api_bp
    if "api" not in app.blueprints:
        app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


def _patch_bridge(monkeypatch, attr, br):
    mod = getattr(api_module, attr)
    monkeypatch.setattr(mod, "get_bridge", lambda *a, **k: br)


@pytest.mark.parametrize("prefix,attr,_platform,svc", PLATFORM_ROUTES)
def test_bridge_status(client, monkeypatch, prefix, attr, _platform, svc):
    _patch_bridge(monkeypatch, attr, _StubBridge())

    r = client.get(f"/api/{prefix}/status")
    assert r.status_code == 200
    assert r.get_json() == {"code": 0, "data": {"running": True, "port": 1}}


@pytest.mark.parametrize("prefix,attr,_platform,_svc", PLATFORM_ROUTES)
def test_bridge_start_stop_routes_removed(client, prefix, attr, _platform, _svc):
    # 服务由程序自动启动，手动启停接口已删除
    assert client.post(f"/api/{prefix}/start").status_code == 404
    assert client.post(f"/api/{prefix}/stop").status_code == 404


def test_save_api_port_only_hints_restart(client, webapp_app):
    (app,) = webapp_app

    class _TM:
        def refresh_schedule(self):
            pass
    app.config["TASK_MANAGER"] = _TM()

    body = client.put("/api/settings", json={"qq_api_port": "45612"}).get_json()
    assert body["code"] == 0
    assert "API服务端口修改需重启程序生效" in body["msg"]


class _QrClient:
    def __init__(self):
        self.calls = []

    def create_qr_login(self, *args):
        self.calls.append(("create", args))
        return {"ok": True, "key": "K", "identifier": "ID", "login_type": "wx",
                "qr_img": "IMG"}

    def check_qr_login(self, *args):
        self.calls.append(("check", args))
        return {"ok": True, "status": 4, "cookie": "c=1"}


def _patch_client(monkeypatch, stub):
    seen = []

    def fake(cookie="", platform="netease"):
        seen.append(platform)
        return stub
    monkeypatch.setattr(api_module, "_create_client", fake)
    return seen


@pytest.mark.parametrize("prefix,platform", [("ncm", "netease"), ("kugou", "kugou")])
def test_key_qr_create_and_check(client, monkeypatch, prefix, platform):
    stub = _QrClient()
    seen = _patch_client(monkeypatch, stub)

    r = client.post(f"/api/{prefix}/qr/create", json={})
    assert r.get_json() == {"code": 0, "data": {"key": "K", "qr_img": "IMG"}}

    r = client.post(f"/api/{prefix}/qr/check", json={})
    assert r.get_json() == {"code": 1, "msg": "缺少二维码 key"}

    r = client.post(f"/api/{prefix}/qr/check", json={"key": " K "})
    assert r.get_json() == {"code": 0, "data": {"status": 4, "cookie": "c=1"}}
    assert stub.calls == [("create", ()), ("check", ("K",))]
    assert set(seen) == {platform}


def test_qq_qr_create_and_check(client, monkeypatch):
    stub = _QrClient()
    seen = _patch_client(monkeypatch, stub)

    r = client.post("/api/qq/qr/create", json={"login_type": "bad"})
    assert r.get_json() == {"code": 1, "msg": "login_type 仅支持 qq / wx"}

    r = client.post("/api/qq/qr/create", json={"login_type": "WX"})
    assert r.get_json() == {"code": 0, "data": {"identifier": "ID", "login_type": "wx",
                                                "qr_img": "IMG"}}

    r = client.post("/api/qq/qr/check", json={"login_type": "wx"})
    assert r.get_json() == {"code": 1, "msg": "缺少二维码 identifier"}

    r = client.post("/api/qq/qr/check", json={"identifier": "ID", "login_type": "wx"})
    assert r.get_json() == {"code": 0, "data": {"status": 4, "cookie": "c=1"}}
    assert stub.calls == [("create", ("wx",)), ("check", ("wx", "ID"))]
    assert set(seen) == {"qq"}


def test_qr_upstream_failure_and_exception(client, monkeypatch):
    class _Bad:
        def create_qr_login(self, *a):
            return {"ok": False, "msg": "上游挂了"}

        def check_qr_login(self, *a):
            raise RuntimeError("连接失败")

    _patch_client(monkeypatch, _Bad())
    assert client.post("/api/ncm/qr/create").get_json() == {"code": 1, "msg": "上游挂了"}
    assert client.post("/api/kugou/qr/check", json={"key": "k"}).get_json() == {
        "code": 1, "msg": "状态查询失败: 连接失败"}


class _UserClient:
    """三平台账号信息接口的最小桩"""

    def get_account_info(self):
        return {"code": 200, "account": {"vipType": 11},
                "profile": {"nickname": "网易昵称"}}

    def get_vip_info(self):
        return {"vip_package": "redplus", "expire_time": 1_900_000_000_000}

    def get_user_info(self):
        return {"ok": True, "nickname": "昵称", "vip_type": 3,
                "vip_expire_ts": 1_900_000_000, "msg": ""}

    def test_download_capability(self):
        return {"msg": "高音质可用"}


@pytest.mark.parametrize("platform,expect_msg,expect_vip", [
    ("netease", "登录成功: 网易昵称 (SVIP)", 12),
    ("qq", "登录成功: 昵称 (绿钻VIP Lv.3)", 3),
    ("kugou", "登录成功: 昵称 (VIP会员)；下载实测: 高音质可用", 3),
])
def test_account_login_test(client, monkeypatch, platform, expect_msg, expect_vip):
    _patch_client(monkeypatch, _UserClient())
    acc = Account(platform=platform, name="a", cookie="x", sort_order=1, enabled=True)
    db.session.add(acc)
    db.session.commit()

    body = client.post(f"/api/accounts/{acc.id}/test").get_json()
    assert body["code"] == 0
    assert body["msg"] == expect_msg
    assert body["data"]["vip_type"] == expect_vip
    db.session.expire_all()
    acc = db.session.get(Account, acc.id)
    assert acc.vip_expire_at == datetime.fromtimestamp(1_900_000_000)
    assert acc.last_check_at is not None


@pytest.mark.parametrize("platform,label", [("qq", "QQ音乐"), ("kugou", "酷狗音乐"),
                                            ("netease", "网易云")])
def test_account_login_test_invalid_cookie(client, monkeypatch, platform, label):
    class _Invalid:
        def get_account_info(self):
            return {"code": 301, "msg": "未登录"}

        def get_user_info(self):
            return {"ok": False, "msg": "未登录"}

    _patch_client(monkeypatch, _Invalid())
    acc = Account(platform=platform, name="a", cookie="x", sort_order=1, enabled=True)
    db.session.add(acc)
    db.session.commit()
    body = client.post(f"/api/accounts/{acc.id}/test").get_json()
    assert body == {"code": 1, "msg": "Cookie 无效: 未登录"}

    class _Boom:
        def get_account_info(self):
            raise RuntimeError("down")

        get_user_info = get_account_info

    _patch_client(monkeypatch, _Boom())
    body = client.post(f"/api/accounts/{acc.id}/test").get_json()
    assert body == {"code": 1, "msg": f"连接{label}API服务失败: down"}


def test_accounts_list_monthly_counts(client):
    from models import Song
    a1 = Account(platform="qq", name="a1", cookie="x", sort_order=1, enabled=True)
    a2 = Account(platform="qq", name="a2", cookie="x", sort_order=2, enabled=True)
    db.session.add_all([a1, a2])
    db.session.commit()
    db.session.add_all([
        Song(id="1", platform="qq", name="s", status="success", account_id=a1.id),
        Song(id="2", platform="qq", name="s", status="success", account_id=a1.id),
        Song(id="3", platform="qq", name="s", status="failed", account_id=a2.id),
    ])
    db.session.commit()
    rows = client.get("/api/accounts?platform=qq").get_json()["data"]
    assert [(r["name"], r["monthly_downloaded"]) for r in rows] == [("a1", 2), ("a2", 0)]
    stats = client.get("/api/accounts/stats").get_json()["data"]
    assert [(r["name"], r["monthly_downloaded"]) for r in stats] == [("a1", 2), ("a2", 0)]


def test_save_settings_normalizes_json_booleans(client, webapp_app):
    from models import Setting
    (app,) = webapp_app

    class _TM:
        def refresh_schedule(self):
            pass
    app.config["TASK_MANAGER"] = _TM()

    body = client.put("/api/settings", json={"write_lyric": False,
                                             "prefer_non_vip": True,
                                             "write_metadata": "false"}).get_json()
    assert body["code"] == 0
    assert Setting.get("write_lyric") == "false"
    assert Setting.get("prefer_non_vip") == "true"
    assert Setting.get("write_metadata") == "false"
