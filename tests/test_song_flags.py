"""搜索行状态标记（v0.9.0.0）测试。

覆盖：
- /api/discover/search：items 附 failed/fail_msg 标记（active 任务优先于 failed）
- /api/discover/song-flags：仅查本地库的批量标记刷新（已下载/进行中/失败）
- /api/discover/playlist-detail：榜单/歌单详情的歌曲列表带同样的标记

搜索接口的桩手法与 test_quality_names.py 一致：monkeypatch _get_client，
避免触桥（本地测试环境起不了平台 API 二进制）。
"""

import pytest

import routes.api as api_module
from models import DownloadTask, Song, db


class _StubClient:
    def __init__(self, sids):
        self._sids = sids

    def search_songs(self, keyword, limit=50, offset=0):
        items = [
            {"id": sid, "name": "测试歌曲", "artists": "测试歌手", "album": "测试专辑", "fee": 0}
            for sid in self._sids
        ]
        return {"items": items, "total": len(items)}


@pytest.fixture
def client(webapp_app):
    from routes.api import api_bp

    (app,) = webapp_app
    app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


def _search(client, sids, monkeypatch):
    monkeypatch.setattr(api_module, "_get_client",
                        lambda platform="netease": _StubClient(sids))
    return client.post("/api/discover/search", json={
        "keyword": "测试", "type": "song", "limit": 5, "offset": 0,
        "platform": "qq",
    })


def _add_song(sid, status, error_msg="", platform="qq"):
    db.session.merge(Song(
        id=sid, platform=platform, name="测试歌曲", artists="测试歌手",
        album="专辑", duration_ms=0, quality="exhigh",
        file_path="", file_size=0, status=status, error_msg=error_msg))
    db.session.commit()


def _add_task(sid, status):
    t = DownloadTask(platform="qq", song_id=sid, song_name="测试歌曲",
                     artists="测试歌手", status=status)
    db.session.add(t)
    db.session.commit()


def test_search_marks_failed_song(client, monkeypatch):
    _add_song("1001", "failed", "取流失败[xxx]")
    _add_song("1002", "success")

    resp = _search(client, ["1001", "1002"], monkeypatch)
    assert resp.status_code == 200
    items = {it["id"]: it for it in resp.get_json()["data"]["items"]}
    assert items["1001"]["failed"] is True
    assert items["1001"]["fail_msg"] == "取流失败[xxx]"
    assert items["1001"]["downloaded"] is False
    assert items["1002"]["failed"] is False
    assert items["1002"]["downloaded"] is True


def test_search_active_task_beats_failed_mark(client, monkeypatch):
    _add_song("1001", "failed", "旧失败")
    _add_task("1001", "pending")

    resp = _search(client, ["1001"], monkeypatch)
    items = {it["id"]: it for it in resp.get_json()["data"]["items"]}
    assert items["1001"]["failed"] is False
    assert items["1001"]["downloaded"] is True   # active 任务按已下载/下载中呈现


def test_song_flags_refresh(client):
    _add_song("1001", "success", "")
    _add_song("1002", "failed", "试听片段")
    _add_song("1003", "failed", "无音源")
    _add_task("1003", "downloading")
    _add_song("2001", "failed", "其它平台", platform="netease")   # platform 维度隔离

    resp = client.post("/api/discover/song-flags", json={
        "platform": "qq", "ids": ["1001", "1002", "1003", "2001"],
    })
    data = resp.get_json()["data"]
    assert data["downloaded_ids"] == ["1001"]
    assert data["active_ids"] == ["1003"]
    assert data["failed"] == {"1002": "试听片段"}
    assert data["downloaded_qualities"] == {"1001": "exhigh"}


def test_song_flags_empty_ids(client):
    resp = client.post("/api/discover/song-flags", json={"platform": "qq", "ids": []})
    assert resp.get_json()["code"] == 0
    assert resp.get_json()["data"]["failed"] == {}


# ======================================================================
# /api/discover/playlist-detail
# ======================================================================
class _DetailClient:
    def __init__(self, detail):
        self._detail = detail
        self.asked = None

    def get_playlist_detail(self, playlist_id, limit=200):
        self.asked = (playlist_id, limit)
        return self._detail


def _detail(client, monkeypatch, detail, pid="62"):
    stub = _DetailClient(detail)
    monkeypatch.setattr(api_module, "_get_client", lambda platform="netease": stub)
    return client.get(f"/api/discover/playlist-detail?platform=qq&id={pid}"), stub


def test_playlist_detail_marks_songs(client, monkeypatch):
    _add_song("a1", "success")
    _add_song("a2", "failed", error_msg="版权限制")
    tracks = [{"id": sid, "name": "歌", "artists": "人", "fee": 0} for sid in ("a1", "a2", "a3")]
    r, stub = _detail(client, monkeypatch, {"name": "热歌榜", "track_count": 300, "tracks": tracks})

    body = r.get_json()
    assert body["code"] == 0
    assert stub.asked == (62, 200)
    d = body["data"]
    assert d["name"] == "热歌榜"
    assert d["track_count"] == 300
    by_id = {i["id"]: i for i in d["items"]}
    assert by_id["a1"]["downloaded"] is True
    assert by_id["a2"]["failed"] is True and by_id["a2"]["fail_msg"] == "版权限制"
    assert by_id["a3"]["downloaded"] is False and by_id["a3"]["failed"] is False
    assert d["downloaded_qualities"] == {"a1": "exhigh"}


def test_playlist_detail_empty_and_bad_id(client, monkeypatch):
    r, _ = _detail(client, monkeypatch, {})
    assert r.get_json()["code"] == 1

    r, _ = _detail(client, monkeypatch, {}, pid="abc")
    assert r.status_code == 400
