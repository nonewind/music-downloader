"""用链接添加（POST /api/link）测试。

覆盖：
- 三个平台的链接类型识别：parse_netease_link / parse_qq_link / parse_kugou_link
- 按域名认平台、短链逐跳跟随（只访问本平台域名，认得出就停）
- 路由分流：单曲入队、专辑入队、歌单走关注、纯数字 ID 按所选平台当歌单

链接样例按公开资料和现有代码整理，未连真实平台验证；
网络与平台接口一律打桩（本地测试环境起不了平台 API 二进制）。
"""

import pytest
import requests

import routes.api as api_module
from core.providers.kugou.parse_links import parse_kugou_link
from core.providers.netease.parse_links import parse_netease_link
from core.providers.qq.parse_links import parse_qq_link, parse_qq_playlist_id
from models import Playlist, db


# ----------------------------------------------------------------------
# 链接类型识别
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url, expected", [
    ("https://music.163.com/song?id=186016", ("song", "186016")),
    ("https://music.163.com/#/song?id=186016", ("song", "186016")),
    ("https://y.music.163.com/m/song?id=186016&userid=99", ("song", "186016")),
    ("https://music.163.com/song?userid=99&id=186016", ("song", "186016")),
    ("https://music.163.com/#/album?id=18905", ("album", "18905")),
    ("https://y.music.163.com/m/album?id=18905&uct2=x", ("album", "18905")),
    ("https://music.163.com/playlist?id=3778678", ("playlist", "3778678")),
    ("https://y.music.163.com/m/song/186016/", ("song", "186016")),
    ("https://music.163.com/#/artist?id=6452", None),
    ("http://163cn.tv/CmHfO44", None),
])
def test_parse_netease_link(url, expected):
    assert parse_netease_link(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV", ("song", "0039MnYb0qxYhV")),
    ("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV?songtype=0", ("song", "0039MnYb0qxYhV")),
    ("https://y.qq.com/n/yqq/song/0039MnYb0qxYhV.html", ("song", "0039MnYb0qxYhV")),
    ("https://i.y.qq.com/v8/playsong.html?songid=&songmid=0039MnYb0qxYhV&type=0",
     ("song", "0039MnYb0qxYhV")),
    ("https://y.qq.com/n/ryqq/albumDetail/002jLGWe16Tf1H", ("album", "002jLGWe16Tf1H")),
    ("https://y.qq.com/n/yqq/album/002jLGWe16Tf1H.html", ("album", "002jLGWe16Tf1H")),
    ("https://i.y.qq.com/n2/m/share/details/album.html?albummid=002jLGWe16Tf1H",
     ("album", "002jLGWe16Tf1H")),
    ("https://y.qq.com/n/ryqq/playlist/7707261125", ("playlist", "7707261125")),
    ("https://i.y.qq.com/n2/m/share/details/taoge.html?hosteuin=x&id=7707261125",
     ("playlist", "7707261125")),
    # 只有数字 songid、songmid 为空：认不出
    ("https://i.y.qq.com/v8/playsong.html?songid=102065756&songmid=&type=0", None),
    ("https://c6.y.qq.com/base/fcgi-bin/u?__=fY3GHnkxtiHJ", None),
])
def test_parse_qq_link(url, expected):
    assert parse_qq_link(url) == expected


def test_qq_playlist_id_accepts_taoge_share_page():
    url = "https://i.y.qq.com/n2/m/share/details/taoge.html?platform=11&id=7707261125&hosteuin=x"
    assert parse_qq_playlist_id(url) == 7707261125


@pytest.mark.parametrize("url, expected", [
    ("https://m.kugou.com/share/song.html?hash=ABC&album_audio_id=32042828", ("song", "32042828")),
    ("https://www.kugou.com/share/x.html?mixsongid=32042828", ("song", "32042828")),
    ("https://www.kugou.com/album/966846.html", ("album", "966846")),
    ("https://www.kugou.com/album/info/966846/", ("album", "966846")),
    ("https://www.kugou.com/yy/album/single/966846.html", ("album", "966846")),
    ("https://www.kugou.com/songlist/x/?global_collection_id=collection_3_123_45_0",
     ("playlist", "collection_3_123_45_0")),
    ("https://www.kugou.com/yy/special/single/546903.html", ("playlist", "546903")),
    # mixsong 编码页、hash 页没有 album_audio_id：认不出
    ("https://www.kugou.com/mixsong/6yblw2a5.html", None),
    ("https://www.kugou.com/song/#hash=ABC&album_id=966846", None),
])
def test_parse_kugou_link(url, expected):
    assert parse_kugou_link(url) == expected


@pytest.mark.parametrize("url, expected", [
    ("http://163cn.tv/CmHfO44", "netease"),
    ("https://y.music.163.com/m/song?id=1", "netease"),
    ("https://c6.y.qq.com/base/fcgi-bin/u?__=x", "qq"),
    ("https://t1.kugou.com/abc", "kugou"),
    ("https://evil163.com/song?id=1", None),
    ("https://notkugou.com/a", None),
    ("https://example.com/song?id=1", None),
])
def test_link_platform(url, expected):
    assert api_module._link_platform(url) == expected


# ----------------------------------------------------------------------
# 短链跟随
# ----------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code, location=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


def _fake_session(monkeypatch, hops):
    """hops：{请求的 URL: _Resp}；记录请求过的 URL"""
    asked = []

    class _Session:
        def __init__(self):
            self.trust_env = True
            self.headers = {}

        def get(self, url, allow_redirects=True, timeout=None):
            assert allow_redirects is False
            asked.append(url)
            return hops[url]

    monkeypatch.setattr(api_module.requests, "Session", _Session)
    return asked


def test_follow_link_stops_at_first_recognized_hop(monkeypatch):
    asked = _fake_session(monkeypatch, {
        "http://163cn.tv/CmHfO44": _Resp(302, "https://y.music.163.com/m/song?id=186016&userid=1"),
    })
    found = api_module._follow_link("http://163cn.tv/CmHfO44", "netease")
    assert found == ("song", "186016", "https://y.music.163.com/m/song?id=186016&userid=1")
    assert asked == ["http://163cn.tv/CmHfO44"]


def test_follow_link_multi_hop_and_relative_location(monkeypatch):
    asked = _fake_session(monkeypatch, {
        "https://c6.y.qq.com/base/fcgi-bin/u?__=x": _Resp(302, "https://i.y.qq.com/r?x=1"),
        "https://i.y.qq.com/r?x=1": _Resp(302, "/v8/playsong.html?songmid=0039MnYb0qxYhV"),
    })
    found = api_module._follow_link("https://c6.y.qq.com/base/fcgi-bin/u?__=x", "qq")
    assert found == ("song", "0039MnYb0qxYhV", "https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV")
    assert len(asked) == 2


def test_follow_link_refuses_other_domain(monkeypatch):
    asked = _fake_session(monkeypatch, {
        "https://t1.kugou.com/abc": _Resp(302, "https://evil.example/song?album_audio_id=1"),
    })
    assert api_module._follow_link("https://t1.kugou.com/abc", "kugou") is None
    assert asked == ["https://t1.kugou.com/abc"]


def test_follow_link_no_redirect(monkeypatch):
    _fake_session(monkeypatch, {"https://www.kugou.com/mixsong/6yblw2a5.html": _Resp(200)})
    assert api_module._follow_link("https://www.kugou.com/mixsong/6yblw2a5.html", "kugou") is None


def test_follow_link_hop_limit(monkeypatch):
    hops = {f"https://t1.kugou.com/{i}": _Resp(302, f"https://t1.kugou.com/{i + 1}") for i in range(10)}
    asked = _fake_session(monkeypatch, hops)
    assert api_module._follow_link("https://t1.kugou.com/0", "kugou") is None
    assert len(asked) == api_module._LINK_MAX_HOPS


# ----------------------------------------------------------------------
# 路由分流
# ----------------------------------------------------------------------
class _StubTM:
    def __init__(self, album_total=3, song_ok=True):
        self.album_total = album_total
        self.song_ok = song_ok
        self.albums = []
        self.songs = []

    def download_album(self, album_id, album_name="", platform="netease"):
        self.albums.append((album_id, platform))
        return {"enqueued": self.album_total, "excluded": 0, "skipped": 0, "total": self.album_total}

    def download_single_song(self, song_id, name, artists, fee=0, platform="netease", force=False):
        self.songs.append((song_id, name, artists, fee, platform))
        return self.song_ok


class _StubClient:
    def __init__(self, title="晴天", artist="周杰伦", playlist=None):
        self.title = title
        self.artist = artist
        self.playlist = playlist
        self.detail_asked = []
        self.playlist_asked = []

    def get_song_detail(self, song_ids):
        self.detail_asked.append(song_ids)
        return [{"title": self.title, "artist": self.artist}]

    def get_playlist_detail(self, playlist_id, limit=200):
        self.playlist_asked.append(playlist_id)
        return self.playlist


@pytest.fixture
def client(webapp_app):
    from routes.api import api_bp

    (app,) = webapp_app
    app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


@pytest.fixture
def stubs(monkeypatch):
    tm = _StubTM()
    cli = _StubClient(playlist={"name": "热歌榜", "track_count": 300})
    platforms = []

    def _get_client(platform="netease"):
        platforms.append(platform)
        return cli

    monkeypatch.setattr(api_module, "_get_task_manager", lambda: tm)
    monkeypatch.setattr(api_module, "_get_client", _get_client)
    return tm, cli, platforms


def _post(client, source, platform="netease"):
    r = client.post("/api/link", json={"source": source, "platform": platform, "type": "user", "limit": 50})
    return r.get_json()


def test_song_share_text_enqueues_song(client, stubs):
    tm, cli, platforms = stubs
    body = _post(client, "分享周杰伦的单曲《晴天》: https://y.music.163.com/m/song?id=186016&userid=1 (来自@网易云音乐)",
                 platform="qq")
    assert body["code"] == 0
    assert "晴天" in body["msg"]
    # 链接的平台优先于下拉框；fee 未知按 VIP 传
    assert tm.songs == [("186016", "晴天", "周杰伦", 1, "netease")]
    assert platforms == ["netease"]


def test_song_not_found(client, stubs):
    tm, cli, _ = stubs
    cli.title = ""
    body = _post(client, "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
    assert body["code"] == 1
    assert tm.songs == []


def test_song_already_downloaded(client, stubs):
    tm, _, _ = stubs
    tm.song_ok = False
    body = _post(client, "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
    assert body["code"] == 1
    assert "已下载" in body["msg"]


def test_album_link_enqueues_album(client, stubs):
    tm, _, _ = stubs
    body = _post(client, "https://y.qq.com/n/ryqq/albumDetail/002jLGWe16Tf1H")
    assert body["code"] == 0
    assert tm.albums == [("002jLGWe16Tf1H", "qq")]


def test_empty_album_is_an_error(client, stubs):
    tm, _, _ = stubs
    tm.album_total = 0
    body = _post(client, "https://www.kugou.com/album/966846.html")
    assert body["code"] == 1
    assert tm.albums == [("966846", "kugou")]


def test_short_link_followed_then_downloaded(client, stubs, monkeypatch):
    tm, _, _ = stubs
    _fake_session(monkeypatch, {
        "https://c6.y.qq.com/base/fcgi-bin/u?__=fY3GHnkxtiHJ":
            _Resp(302, "https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV"),
    })
    body = _post(client, "https://c6.y.qq.com/base/fcgi-bin/u?__=fY3GHnkxtiHJ")
    assert body["code"] == 0
    assert tm.songs[0][0] == "0039MnYb0qxYhV"
    assert tm.songs[0][4] == "qq"


def test_short_link_network_error(client, stubs, monkeypatch):
    class _Session:
        def __init__(self):
            self.headers = {}

        def get(self, url, allow_redirects=True, timeout=None):
            raise requests.ConnectionError("连接超时")

    monkeypatch.setattr(api_module.requests, "Session", _Session)
    body = _post(client, "http://163cn.tv/CmHfO44")
    assert body["code"] == 1
    assert "短链打开失败" in body["msg"]


def test_unknown_domain(client, stubs):
    body = _post(client, "https://example.com/song?id=1")
    assert body["code"] == 1
    assert "认不出" in body["msg"]


def test_playlist_link_follows_playlist(client, stubs):
    _, cli, _ = stubs
    body = _post(client, "https://i.y.qq.com/n2/m/share/details/taoge.html?id=7707261125", platform="netease")
    assert body["code"] == 0
    assert cli.playlist_asked == [7707261125]
    assert db.session.get(Playlist, 7707261125).platform == "qq"


def test_bare_id_uses_selected_platform(client, stubs):
    _, cli, platforms = stubs
    body = _post(client, "3778678", platform="netease")
    assert body["code"] == 0
    assert cli.playlist_asked == [3778678]
    assert platforms == ["netease"]


def test_kugou_playlist_short_link(client, stubs, monkeypatch):
    _, cli, _ = stubs
    # gcid 缓存会落盘到 api/ 目录，测试里只记下来
    registered = []
    monkeypatch.setattr(api_module, "register_gcid", lambda pid, gcid: registered.append((pid, gcid)))
    _fake_session(monkeypatch, {
        "https://t1.kugou.com/7mBqx0bFZV2":
            _Resp(302, "https://m.kugou.com/songlist/gcid_3zs?global_collection_id=collection_3_1_546903_0"),
    })
    body = _post(client, "https://t1.kugou.com/7mBqx0bFZV2", platform="netease")
    assert body["code"] == 0
    # gcid form1：seg3 >= 10**4 即真实 specialid
    assert cli.playlist_asked == [546903]
    assert registered == [(546903, "collection_3_1_546903_0")]
    assert db.session.get(Playlist, 546903).platform == "kugou"
