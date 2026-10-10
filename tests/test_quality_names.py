"""TASK-04b 搜索角标接口补音质：/api/discover/search 响应新增 downloaded_qualities。

背景：前端重复下载确认框需要展示"已有文件音质"，此前角标接口只回
downloaded 布尔值。新增 downloaded_qualities（{"<song_id>": "<quality>"}），
仅含 success 记录；原字段一律不动（向后兼容）。
"""

import pytest

import routes.api as api_module
from models import Song, db


class _StubClient:
    """search_songs 桩：按给定 id 列表返回固定搜索结果。"""

    def __init__(self, sids):
        self._sids = sids

    def search_songs(self, keyword, limit=50, offset=0):
        items = [
            {"id": sid, "name": "测试歌曲", "artists": "测试歌手", "album": "测试专辑", "fee": 0}
            for sid in self._sids
        ]
        return {"items": items, "total": len(items)}


@pytest.fixture
def api_client(webapp_app):
    """注册 api 蓝图并注入登录态的 test client（discover 接口有全局登录校验）。"""
    from models import User
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="root", is_admin=True, enabled=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = user.id
    return client


def _add_song(sid, quality, status="success", platform="netease"):
    song = Song(
        id=sid,
        platform=platform,
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        duration_ms=0,
        quality=quality,
        file_path="/music/a.flac",
        file_size=1,
        playlist_id=None,
        source_name="",
        status=status,
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()
    return song


def _search(client, sids, monkeypatch, keyword="测试"):
    monkeypatch.setattr(api_module, "_get_client", lambda platform="netease": _StubClient(sids))
    resp = client.post("/api/discover/search", json={"keyword": keyword})
    assert resp.status_code == 200
    return resp.get_json()


def test_search_returns_downloaded_qualities(api_client, monkeypatch):
    """两条 success 记录 → downloaded_qualities 含 id→quality 映射，downloaded 同步为 True。"""
    _add_song("1001", "lossless")
    _add_song("1002", "standard")

    body = _search(api_client, ["1001", "1002"], monkeypatch)

    assert body["code"] == 0
    data = body["data"]
    assert data["downloaded_qualities"] == {"1001": "lossless", "1002": "standard"}
    flags = {t["id"]: t["downloaded"] for t in data["items"]}
    assert flags == {"1001": True, "1002": True}


def test_search_qualities_exclude_failed_and_empty(api_client, monkeypatch):
    """failed 记录与 quality 为空的 success 记录不进映射；failed 歌曲 downloaded=False。"""
    _add_song("2001", "hires", status="failed")
    _add_song("2002", "")

    body = _search(api_client, ["2001", "2002"], monkeypatch)

    assert body["data"]["downloaded_qualities"] == {}
    flags = {t["id"]: t["downloaded"] for t in body["data"]["items"]}
    assert flags == {"2001": False, "2002": True}


def test_search_response_backward_compatible(api_client, monkeypatch):
    """原字段（items/total/page/pages/type/downloaded）不缺不变形，仅新增字段。"""
    _add_song("3001", "exhigh")

    body = _search(api_client, ["3001"], monkeypatch)

    data = body["data"]
    assert set(data.keys()) >= {"items", "total", "page", "pages", "type", "downloaded_qualities"}
    assert data["type"] == "song"
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["items"][0]["name"] == "测试歌曲"
    assert data["items"][0]["downloaded"] is True


def test_search_platform_scoped_qualities(api_client, monkeypatch):
    """复合主键 (id, platform)：同 id 双平台仅当前平台的 success 记录入映射。"""
    _add_song("4001", "lossless", platform="netease")
    _add_song("4001", "standard", platform="qq")

    body = _search(api_client, ["4001"], monkeypatch)

    assert body["data"]["downloaded_qualities"] == {"4001": "lossless"}
