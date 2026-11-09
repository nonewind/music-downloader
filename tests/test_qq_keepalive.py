"""QQ cookie 自动保活（v0.9.0.0）测试。

覆盖：
- QqClient.set_cookie 的网页 Cookie psrf_* 刷新字段映射（refresh_credential
  login_type=2 分支依赖 access_token/refresh_token/expired_at）
- merge_refreshed_cookie：musickey 换发写回 qqmusic_key/qm_keyst、psrf_*
  刷新字段更新、微信字段共存、无变化/缺 musickey 返回 None、其余字段保留
- TaskManager._qq_cookie_keepalive：成功续期写回账号 cookie；接口无返回
  （cookie 失效/缺刷新字段）不动账号
"""

import pytest

from core.providers.qq.client import QqClient, merge_refreshed_cookie
from models import Account, db
import task_manager


WEB_COOKIE = (
    "eas_sid=abc; ptcz=keepme; uin=10001; "
    "qm_keyst=Q_H_L_oldkey; qqmusic_key=Q_H_L_oldkey; "
    "psrf_musickey_createtime=1794192972; "
    "psrf_qqopenid=OPENID123; psrf_qqrefresh_token=RT_OLD; "
    "psrf_qqaccess_token=AT_OLD; psrf_access_token_expiresAt=1799376972; "
    "psrf_qqunionid=UNION_ABC; euin=oivkoiC5oevq"
)


# ----------------------------------------------------------------------
# set_cookie：psrf_* 字段映射
# ----------------------------------------------------------------------
def test_set_cookie_maps_qq_web_refresh_fields():
    c = QqClient(cookie=WEB_COOKIE)

    def cookie_val(name):
        for k, v in c.session.cookies.items():
            if k == name:
                return v
        return None

    assert cookie_val("musicid") == "10001"
    assert cookie_val("musickey") == "Q_H_L_oldkey"
    # QQ 网页 Cookie 刷新字段 → 服务端标准名（凭证续期必需）
    assert cookie_val("refresh_token") == "RT_OLD"
    assert cookie_val("access_token") == "AT_OLD"
    assert cookie_val("openid") == "OPENID123"
    assert cookie_val("unionid") == "UNION_ABC"
    assert cookie_val("expired_at") == "1799376972"


def test_set_cookie_standard_names_still_work():
    c = QqClient(cookie="uin=123; musickey=K1; refresh_token=R1")
    got = {k: v for k, v in c.session.cookies.items()}
    assert got["musicid"] == "123"
    assert got["musickey"] == "K1"
    assert got["refresh_token"] == "R1"


# ----------------------------------------------------------------------
# merge_refreshed_cookie
# ----------------------------------------------------------------------
NEW_CRED = {
    "musicid": 10001,
    "musickey": "Q_H_L_newkey",
    "refresh_token": "RT_NEW",
    "access_token": "AT_NEW",
    "expired_at": 1799981772,
    "musickeyCreateTime": 1794804972,
}


def test_merge_updates_all_refresh_fields():
    merged = merge_refreshed_cookie(WEB_COOKIE, NEW_CRED)
    assert merged is not None
    pairs = dict(p.split("=", 1) for p in merged.split("; "))
    assert pairs["qqmusic_key"] == "Q_H_L_newkey"
    assert pairs["qm_keyst"] == "Q_H_L_newkey"
    assert pairs["psrf_qqrefresh_token"] == "RT_NEW"
    assert pairs["psrf_qqaccess_token"] == "AT_NEW"
    assert pairs["psrf_access_token_expiresAt"] == "1799981772"
    assert pairs["psrf_musickey_createtime"] == "1794804972"
    # 其余登录态相关字段保留
    assert pairs["ptcz"] == "keepme"
    assert pairs["uin"] == "10001"
    assert pairs["psrf_qqunionid"] == "UNION_ABC"
    assert pairs["euin"] == "oivkoiC5oevq"


def test_merge_wechat_field_coexists():
    cookie = WEB_COOKIE + "; wxrefresh_token=WX_OLD"
    merged = merge_refreshed_cookie(cookie, NEW_CRED)
    pairs = dict(p.split("=", 1) for p in merged.split("; "))
    assert pairs["wxrefresh_token"] == "RT_NEW"
    assert pairs["psrf_qqrefresh_token"] == "RT_NEW"


def test_merge_returns_none_when_unchanged():
    same = dict(NEW_CRED)
    same["musickey"] = "Q_H_L_oldkey"
    same["refresh_token"] = "RT_OLD"
    same["access_token"] = "AT_OLD"
    same["expired_at"] = 1799376972
    same["musickeyCreateTime"] = 1794192972
    assert merge_refreshed_cookie(WEB_COOKIE, same) is None


def test_merge_returns_none_without_musickey():
    assert merge_refreshed_cookie(WEB_COOKIE, {"refresh_token": "x"}) is None
    assert merge_refreshed_cookie(WEB_COOKIE, {}) is None
    assert merge_refreshed_cookie(WEB_COOKIE, None) is None


# ----------------------------------------------------------------------
# refresh_credential 与定时任务
# ----------------------------------------------------------------------
class _FakeProvider:
    def __init__(self, cred):
        self.cred = cred
        self.cookie_seen = ""

    def set_cookie(self, cookie):
        self.cookie_seen = cookie

    def refresh_credential(self):
        return self.cred


def test_refresh_credential_calls_bridge_path(monkeypatch):
    c = QqClient(cookie=WEB_COOKIE)
    seen = {}

    def fake_request(path, **kwargs):
        seen["path"] = path
        return {"musickey": "K"}

    monkeypatch.setattr(c, "_request", fake_request)
    assert c.refresh_credential() == {"musickey": "K"}
    assert seen["path"] == "/login/refresh_credential"


def test_keepalive_persists_refreshed_cookie(webapp_app, monkeypatch):
    (app,) = webapp_app
    acc = Account(platform="qq", name="QQ1", enabled=True, cookie=WEB_COOKIE)
    db.session.add(acc)
    db.session.commit()

    tm = task_manager.TaskManager(app)
    monkeypatch.setattr(task_manager, "get_provider",
                        lambda platform: _FakeProvider(dict(NEW_CRED)))

    tm._qq_cookie_keepalive()

    db.session.expire_all()
    acc = Account.query.get(acc.id)
    assert "Q_H_L_newkey" in acc.cookie
    assert "psrf_qqrefresh_token=RT_NEW" in acc.cookie
    assert "ptcz=keepme" in acc.cookie
    assert acc.last_check_at is not None


def test_keepalive_keeps_cookie_when_refresh_fails(webapp_app, monkeypatch):
    """接口无返回（失效/缺刷新字段）：不动账号 cookie，仅记检查时间不写"""
    (app,) = webapp_app
    acc = Account(platform="qq", name="QQ1", enabled=True, cookie=WEB_COOKIE)
    db.session.add(acc)
    db.session.commit()

    tm = task_manager.TaskManager(app)
    monkeypatch.setattr(task_manager, "get_provider",
                        lambda platform: _FakeProvider({}))

    tm._qq_cookie_keepalive()

    db.session.expire_all()
    acc = Account.query.get(acc.id)
    assert acc.cookie == WEB_COOKIE
