"""访问控制回归测试（v0.9.0.0 移除登录后的暴露面收敛，见 webapp/access.py）

- 新装默认 web_port 仅监听本机；Docker 经 WEB_PORT_DEFAULT 改默认；存量值不动
- 可选访问令牌：设置后页面与 /api/* 需令牌（?token= 一次写 cookie / 请求头）
- 导出账号、改自定义 API 地址等敏感操作仅限本机 / 飞牛网关 / 有效令牌
"""

import pytest
from flask import Flask

from models import Account, Setting, db

LAN = {"REMOTE_ADDR": "192.168.1.50"}
TOKEN = "s3cret-token-123"


@pytest.fixture
def make_client(webapp_app, monkeypatch):
    """按需构造 client：gateway=True 模拟飞牛网关模式"""
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)

    def _make(gateway=False):
        from access import init_access_control
        from routes.api import api_bp
        from routes.views import views_bp
        from task_manager import TaskManager
        from models import DEFAULT_SETTINGS

        (app,) = webapp_app
        # 与 init_db 一致预写默认配置（设置页全量提交的比较基线）
        for k, v in DEFAULT_SETTINGS.items():
            if not db.session.get(Setting, k):
                db.session.add(Setting(key=k, value=v))
        db.session.commit()
        app.config["SECRET_KEY"] = "test-secret"
        app.config["TASK_MANAGER"] = TaskManager(app)  # 构造不启动线程/调度器
        init_access_control(app, gateway_mode=gateway)
        app.register_blueprint(views_bp)
        app.register_blueprint(api_bp, url_prefix="/api")
        # 测试 app 无模板目录：用占位路由代替真实页面
        # （访问控制只看路径，与路由实现无关）
        app.add_url_rule("/page", "test_page", lambda: "page")
        return app

    def _client(gateway=False):
        return _make(gateway).test_client()

    _client.app_factory = _make
    return _client


@pytest.fixture
def make_client_pair(make_client):
    """同一 app 的两个独立 client（cookie 罐互不共享）"""
    def _pair(gateway=False):
        app = make_client.app_factory(gateway)
        return app.test_client(), app.test_client()
    return _pair


def _add_account():
    db.session.add(Account(platform="netease", name="主号", cookie="MUSIC_U=topsecret"))
    db.session.commit()


# ----------------------------------------------------------------------
# 默认监听地址
# ----------------------------------------------------------------------
def _fresh_db_web_port(tmp_path, name="a.db"):
    from models import init_db
    app = Flask(__name__)
    init_db(app, str(tmp_path / name))
    with app.app_context():
        return Setting.get("web_port", "")


def test_new_install_defaults_to_loopback(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_PORT_DEFAULT", raising=False)
    assert _fresh_db_web_port(tmp_path) == "127.0.0.1:45600"


def test_docker_default_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PORT_DEFAULT", "0.0.0.0:45600")
    assert _fresh_db_web_port(tmp_path) == "0.0.0.0:45600"


def test_existing_web_port_untouched(tmp_path, monkeypatch):
    """存量库里的 *:45600 在新版本 init_db 后保持不变"""
    import sqlite3
    from models import init_db
    monkeypatch.delenv("WEB_PORT_DEFAULT", raising=False)
    path = tmp_path / "old.db"
    app = Flask(__name__)
    init_db(app, str(path))
    with app.app_context():
        Setting.set("web_port", "*:45600")
        db.session.remove()
    app2 = Flask(__name__)
    init_db(app2, str(path))
    with app2.app_context():
        assert Setting.get("web_port") == "*:45600"
        db.session.remove()
    con = sqlite3.connect(path)
    assert con.execute("select value from setting where key='web_port'").fetchone()[0] == "*:45600"
    con.close()


@pytest.mark.parametrize("raw,expected", [
    ("*:45600", ("0.0.0.0", 45600)),
    (":45601", ("0.0.0.0", 45601)),
    ("45602", ("0.0.0.0", 45602)),
    ("127.0.0.1:45600", ("127.0.0.1", 45600)),
    ("[::1]:45600", ("::1", 45600)),
    ("garbage", ("127.0.0.1", 45600)),
])
def test_parse_web_bind(raw, expected):
    from access import parse_web_bind
    assert parse_web_bind(raw) == expected


def test_is_public_bind():
    from access import is_public_bind
    assert is_public_bind("0.0.0.0")
    assert is_public_bind("192.168.1.2")
    assert not is_public_bind("127.0.0.1")
    assert not is_public_bind("::1")
    assert not is_public_bind("localhost")


# ----------------------------------------------------------------------
# 未设置令牌：普通接口照常开放，敏感接口仅本机
# ----------------------------------------------------------------------
def test_no_token_regular_api_open_from_lan(make_client):
    c = make_client()
    assert c.get("/api/accounts", environ_base=LAN).status_code == 200
    assert c.get("/page", environ_base=LAN).status_code == 200
    assert c.get("/healthz", environ_base=LAN).status_code == 200


def test_export_allowed_from_loopback(make_client):
    _add_account()
    c = make_client()
    r = c.get("/api/accounts/export")
    assert r.status_code == 200
    assert "topsecret" in r.get_data(as_text=True)


def test_export_denied_from_lan(make_client):
    _add_account()
    c = make_client()
    r = c.get("/api/accounts/export", environ_base=LAN)
    assert r.status_code == 403
    assert "topsecret" not in r.get_data(as_text=True)


def test_export_denied_on_dns_rebinding_host(make_client):
    """回环来源但 Host 是外部域名（DNS rebinding）也不放行"""
    _add_account()
    c = make_client()
    r = c.get("/api/accounts/export", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_export_allowed_in_gateway_mode(make_client):
    _add_account()
    c = make_client(gateway=True)
    assert c.get("/api/accounts/export", environ_base=LAN).status_code == 200


def test_lan_cannot_change_custom_api_url(make_client):
    c = make_client()
    r = c.put("/api/settings", environ_base=LAN,
              json={"use_custom_api_url": "true", "custom_api_url": "http://evil.example.com"})
    assert r.status_code == 403
    assert Setting.get("custom_api_url") == ""
    assert Setting.get("use_custom_api_url") == "false"


@pytest.mark.parametrize("key,value", [
    ("qq_api_base_url", "http://evil.example.com"),
    ("output_dir", "/"),
    ("web_port", "*:45600"),
    ("use_custom_api_url", "true"),
    # JSON 布尔须与字符串同等判定：曾以 "True" 参与比较而绕过可信校验
    ("use_custom_qq_api_url", True),
])
def test_lan_cannot_change_sensitive_settings(make_client, key, value):
    c = make_client()
    before = Setting.get(key)
    r = c.put("/api/settings", environ_base=LAN, json={key: value})
    assert r.status_code == 403
    assert Setting.get(key) == before


def test_lan_can_save_unchanged_full_form(make_client):
    """设置页全量提交：敏感项未改动（URL 清空亦不算）时局域网照常可保存"""
    c = make_client()
    payload = {
        "web_port": Setting.get("web_port"),
        "output_dir": Setting.get("output_dir"),
        "use_custom_qq_api_url": "false",
        "qq_api_base_url": "",          # 前端未勾选时提交空串
        "max_retries": "5",
    }
    r = c.put("/api/settings", environ_base=LAN, json=payload)
    assert r.status_code == 200, r.get_json()
    assert Setting.get("max_retries") == "5"


def test_loopback_can_change_custom_api_url(make_client):
    c = make_client()
    r = c.put("/api/settings",
              json={"use_custom_api_url": "true", "custom_api_url": "http://10.0.0.2:3000"})
    assert r.status_code == 200
    assert Setting.get("custom_api_url") == "http://10.0.0.2:3000"


def test_settings_never_echo_token(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client()
    body = c.get("/api/settings", headers={"X-Access-Token": TOKEN}).get_json()
    assert body["data"]["access_token_enabled"] is True
    assert TOKEN not in str(body)


# ----------------------------------------------------------------------
# 设置令牌后
# ----------------------------------------------------------------------
def test_token_required_for_api_and_pages(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client()
    r = c.get("/api/accounts", environ_base=LAN)
    assert r.status_code == 401
    assert r.get_json()["code"] == 1
    r = c.get("/page", environ_base=LAN)
    assert r.status_code == 401
    assert "访问令牌" in r.get_data(as_text=True)
    # 本机也需要令牌（同机反向代理的请求同样来自回环）
    assert c.get("/api/accounts").status_code == 401


def test_static_and_healthz_exempt(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client()
    assert c.get("/healthz", environ_base=LAN).status_code == 200
    # 测试 app 没有真实静态文件：只要不是 401 即说明未被令牌拦截
    assert c.get("/static/js/app.js", environ_base=LAN).status_code != 401


def test_header_token(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client()
    assert c.get("/api/accounts", environ_base=LAN,
                 headers={"X-Access-Token": TOKEN}).status_code == 200
    assert c.get("/api/accounts", environ_base=LAN,
                 headers={"X-Access-Token": "wrong"}).status_code == 401


def test_query_token_sets_cookie_and_redirects(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client()
    r = c.get(f"/page?token={TOKEN}&x=1", environ_base=LAN)
    assert r.status_code == 302
    assert TOKEN not in r.headers["Location"]
    assert r.headers["Location"].endswith("/page?x=1")
    cookie = c.get_cookie("deen_access")
    assert cookie is not None and cookie.value != TOKEN   # 存摘要不存明文
    # 之后靠 cookie 访问
    assert c.get("/api/accounts", environ_base=LAN).status_code == 200
    assert c.get("/page", environ_base=LAN).status_code == 200


def test_token_holder_can_export_from_lan(make_client):
    _add_account()
    Setting.set("access_token", TOKEN)
    c = make_client()
    r = c.get("/api/accounts/export", environ_base=LAN, headers={"X-Access-Token": TOKEN})
    assert r.status_code == 200
    assert "topsecret" in r.get_data(as_text=True)


def test_env_token_overrides_setting(make_client, monkeypatch):
    Setting.set("access_token", "db-token-xxxx")
    monkeypatch.setenv("ACCESS_TOKEN", TOKEN)
    c = make_client()
    assert c.get("/api/accounts", headers={"X-Access-Token": "db-token-xxxx"}).status_code == 401
    assert c.get("/api/accounts", headers={"X-Access-Token": TOKEN}).status_code == 200
    # 环境变量提供的令牌不可经接口修改
    r = c.put("/api/settings/access-token", headers={"X-Access-Token": TOKEN},
              json={"token": "another-token-1"})
    assert r.status_code == 400


def test_gateway_mode_skips_token(make_client):
    Setting.set("access_token", TOKEN)
    c = make_client(gateway=True)
    assert c.get("/api/accounts", environ_base=LAN).status_code == 200


# ----------------------------------------------------------------------
# 令牌设置接口
# ----------------------------------------------------------------------
def test_lan_cannot_set_token(make_client):
    c = make_client()
    r = c.put("/api/settings/access-token", environ_base=LAN, json={"token": "attacker-token"})
    assert r.status_code == 403
    assert Setting.get("access_token", "") == ""


def test_loopback_sets_and_clears_token(make_client_pair):
    c, other = make_client_pair()
    assert c.put("/api/settings/access-token", json={"token": "short"}).status_code == 400
    r = c.put("/api/settings/access-token", json={"token": TOKEN})
    assert r.status_code == 200
    assert Setting.get("access_token") == TOKEN
    # 响应已给调用方写入 cookie：不会把自己锁在门外
    assert c.get("/api/accounts").status_code == 200
    assert other.get("/api/accounts", environ_base=LAN).status_code == 401
    r = c.put("/api/settings/access-token", json={"token": ""})
    assert r.status_code == 200
    assert other.get("/api/accounts", environ_base=LAN).status_code == 200
