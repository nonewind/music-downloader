"""音乐库（歌手 → 专辑 → 歌曲）接口测试。

覆盖：
- /api/library/tree：按文件夹分组（不按标签）、年份后缀拆分、专辑排序、
  未归位计数、旧下载目录残留行不算进来
- /api/library/album：按曲序排序、年份取值顺序、参数检查
- core.organizer.read_easy_tags：真 FLAC 文件现读标签，坏文件不报错

FLAC 用最小文件头现做（只有 STREAMINFO），再用 mutagen 写标签，不依赖样本文件。
"""

from pathlib import Path

import pytest

from core.organizer import read_easy_tags, track_no_of, year_of
from models import LocalAudioFile, Setting, db


def _make_flac(path: Path, **tags) -> Path:
    """写一个只有 STREAMINFO 的最小 FLAC（44.1kHz/2ch/16bit），再写标签"""
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    packed = (44100 << 44) | ((2 - 1) << 41) | ((16 - 1) << 36)
    info = ((4096).to_bytes(2, "big") * 2 + (0).to_bytes(3, "big") * 2
            + packed.to_bytes(8, "big") + b"\0" * 16)
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info)
    if tags:
        f = FLAC(str(path))
        for k, v in tags.items():
            f[k] = v
        f.save()
    return path


@pytest.fixture
def out_dir(tmp_path):
    d = (tmp_path / "music").resolve()
    d.mkdir()
    Setting.set("output_dir", str(d))
    return d


@pytest.fixture
def client(webapp_app, out_dir):
    from routes.api import api_bp

    (app,) = webapp_app
    app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


def _index(path: Path, size=100, lossless=True, artist="", album="", status="active"):
    db.session.add(LocalAudioFile(
        path=str(path), size=size, mtime=1.0, status=status,
        ext=path.suffix, lossless=lossless, artist=artist, album=album))
    db.session.commit()


# ======================================================================
# /api/library/tree
# ======================================================================
def test_tree_groups_by_folder_not_tag(client, out_dir):
    # 标签写的是合唱，文件在「周杰伦」文件夹里 → 只算周杰伦一位歌手
    _index(out_dir / "周杰伦" / "十一月的萧邦 (2005)" / "a-1.flac", artist="周杰伦/林迈可")
    _index(out_dir / "周杰伦" / "十一月的萧邦 (2005)" / "b-2.flac", artist="周杰伦")
    _index(out_dir / "周杰伦" / "叶惠美 (2003)" / "c-3.mp3", size=50, lossless=False)
    _index(out_dir / "周杰伦" / "无年份专辑" / "d-4.flac")
    _index(out_dir / "孙燕姿" / "逆光 (2007)" / "e-5.flac")
    # 不在三层结构里 → 未归位
    _index(out_dir / "零散文件夹" / "f.flac")
    _index(out_dir / "g.flac")
    # 状态不是 active 的不算
    _index(out_dir / "周杰伦" / "叶惠美 (2003)" / "h-6.flac", status="trashed")
    # 下载目录以外的旧行不算，也不报错
    _index(Path("/old/music/某歌手/某专辑/i.flac"))

    r = client.get("/api/library/tree")
    body = r.get_json()
    assert body["code"] == 0
    d = body["data"]
    assert d["unplaced"] == 2

    artists = d["artists"]
    assert [a["name"] for a in artists] == ["周杰伦", "孙燕姿"]   # 按歌曲数从多到少
    jay = artists[0]
    assert jay["count"] == 4
    # 有年份的从新到旧在前，没年份的在后
    assert [a["name"] for a in jay["albums"]] == ["十一月的萧邦", "叶惠美", "无年份专辑"]
    chopin = jay["albums"][0]
    assert chopin == {
        "dir": "周杰伦/十一月的萧邦 (2005)", "name": "十一月的萧邦", "year": "2005",
        "count": 2, "size": 200, "lossless": True,
    }
    assert jay["albums"][1]["lossless"] is False
    assert jay["albums"][2]["year"] == ""


# ======================================================================
# /api/library/album
# ======================================================================
def test_album_sorted_by_track_and_year_from_folder(client, out_dir):
    album = out_dir / "孙燕姿" / "逆光 (2007)"
    p3 = _make_flac(album / "三-3.flac", tracknumber="3/10", date="2006-01-01")
    p1 = _make_flac(album / "一-1.flac", tracknumber="1")
    p0 = _make_flac(album / "没曲序-9.flac")
    for p in (p3, p1, p0):
        _index(p)
    # 子文件夹里的不算这张专辑
    _index(album / "CD2" / "x.flac")

    r = client.get("/api/library/album", query_string={"dir": "孙燕姿/逆光 (2007)"})
    body = r.get_json()
    assert body["code"] == 0
    d = body["data"]
    assert d["artist"] == "孙燕姿"
    assert d["name"] == "逆光"
    assert d["year"] == "2007"          # 文件夹名上的年份优先于标签里的 2006
    assert [i["filename"] for i in d["items"]] == ["一-1.flac", "三-3.flac", "没曲序-9.flac"]
    assert [i["track"] for i in d["items"]] == [1, 3, 0]


def test_album_year_falls_back_to_tag(client, out_dir):
    p = _make_flac(out_dir / "孙燕姿" / "无年份" / "a-1.flac", date="2011")
    _index(p)

    d = client.get("/api/library/album", query_string={"dir": "孙燕姿/无年份"}).get_json()["data"]
    assert d["year"] == "2011"


@pytest.mark.parametrize("bad", ["", "孙燕姿", "a/b/c", "../x", "孙燕姿/.trash", "/abs"])
def test_album_rejects_bad_dir(client, bad):
    r = client.get("/api/library/album", query_string={"dir": bad})
    assert r.status_code == 400


def test_album_empty_folder(client, out_dir):
    r = client.get("/api/library/album", query_string={"dir": "没人/没专辑"})
    assert r.status_code == 200
    assert r.get_json()["code"] == 1


# ======================================================================
# read_easy_tags
# ======================================================================
def test_read_easy_tags_real_flac(tmp_path):
    p = _make_flac(tmp_path / "a.flac", tracknumber="07/12", date="2015-11-20",
                   album="专辑", albumartist="歌手甲")
    tags = read_easy_tags(p)
    assert tags == {"album": "专辑", "albumartist": "歌手甲",
                    "date": "2015-11-20", "tracknumber": "07/12"}
    assert track_no_of(tags) == 7
    assert year_of(tags) == "2015"


def test_read_easy_tags_no_tags_and_broken_file(tmp_path):
    assert read_easy_tags(_make_flac(tmp_path / "a.flac")) == {}
    # 扩展名是 .flac 但内容是空的（飞牛曲库里真有这种文件）
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"")
    assert read_easy_tags(broken) == {}
    assert track_no_of({}) == 0 and year_of({}) == ""


# ======================================================================
# /api/library/album/edit（改专辑信息）
# ======================================================================
from mutagen.flac import FLAC, Picture

from models import DownloadTask, OrganizerJob, Song


def _album_with_records(out_dir: Path) -> tuple[Path, Path]:
    """周杰伦/叶惠美 (2003)/ 下两首，第一首带封面、下载记录和索引身份"""
    album = out_dir / "周杰伦" / "叶惠美 (2003)"
    a = _make_flac(album / "晴天-101.flac", title="晴天", album="叶惠美",
                   albumartist="周杰伦", date="2003-07-31", tracknumber="3")
    b = _make_flac(album / "懦夫-102.flac", title="懦夫", album="叶惠美", date="2003")
    f = FLAC(str(a))
    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.data = b"cover-bytes"
    f.add_picture(pic)
    f.save()
    _index(a)
    _index(b)
    row = LocalAudioFile.query.filter_by(path=str(a)).first()
    row.platform = "qq"
    row.song_id = "101"
    db.session.add(Song(id="101", platform="qq", name="晴天", file_path=str(a)))
    db.session.add(DownloadTask(platform="qq", song_id="101", status="done", file_path=str(a)))
    db.session.commit()
    return a, b


def _edit(client, **body):
    return client.post("/api/library/album/edit", json=body)


def test_edit_preview_changes_nothing(client, out_dir):
    a, b = _album_with_records(out_dir)
    before = a.read_bytes()

    r = _edit(client, dir="周杰伦/叶惠美 (2003)", changes={"album": "叶惠美 (重制)", "year": "2004"},
              move_folder=True, preview=True)
    d = r.get_json()["data"]

    assert d["new_dir"] == "周杰伦/叶惠美 (重制) (2004)"
    plans = {p["from"]: p for p in d["plans"]}
    pa = plans["周杰伦/叶惠美 (2003)/晴天-101.flac"]
    assert pa["to"] == "周杰伦/叶惠美 (重制) (2004)/晴天-101.flac" and pa["moved"] is True
    assert {"key": "year", "old": "2003-07-31", "new": "2004"} in pa["tags"]
    assert {"key": "album", "old": "叶惠美", "new": "叶惠美 (重制)"} in pa["tags"]
    # 预览不动文件
    assert a.read_bytes() == before
    assert not (out_dir / "周杰伦" / "叶惠美 (重制) (2004)").exists()


def test_edit_execute_renames_and_syncs_records(client, out_dir):
    a, b = _album_with_records(out_dir)

    r = _edit(client, dir="周杰伦/叶惠美 (2003)", changes={"album": "叶惠美2", "year": "2004"},
              move_folder=True, preview=False)
    body = r.get_json()
    assert body["code"] == 0, body
    assert [x["status"] for x in body["data"]["results"]] == ["done", "done"]

    new_a = out_dir / "周杰伦" / "叶惠美2 (2004)" / "晴天-101.flac"
    assert new_a.exists() and not a.exists()
    assert not (out_dir / "周杰伦" / "叶惠美 (2003)").exists()     # 挪空的旧文件夹删掉了
    assert (out_dir / "周杰伦").exists()                           # 歌手文件夹里还有东西就不动

    # 标签：改了的改了，没改的（歌名、曲序、专辑歌手、封面）都还在
    f = FLAC(str(new_a))
    assert f["album"] == ["叶惠美2"] and f["date"] == ["2004"]
    assert f["title"] == ["晴天"] and f["tracknumber"] == ["3"] and f["albumartist"] == ["周杰伦"]
    assert len(f.pictures) == 1 and f.pictures[0].data == b"cover-bytes"

    # 三处记录跟着改，索引身份保住
    row = LocalAudioFile.query.filter_by(path=str(new_a)).first()
    assert row is not None and row.status == "active"
    assert row.platform == "qq" and row.song_id == "101"
    assert row.album == "叶惠美2"
    assert LocalAudioFile.query.filter_by(path=str(a)).first() is None
    assert db.session.get(Song, ("101", "qq")).file_path == str(new_a)
    assert DownloadTask.query.first().file_path == str(new_a)


def test_edit_albumartist_moves_to_other_artist_and_merges(client, out_dir):
    # 「伍佰」下的爱你伍佰年 并到「伍佰 & China Blue」下已有的同名专辑
    src = _make_flac(out_dir / "伍佰" / "爱你伍佰年" / "突然的自我-502.flac", album="爱你伍佰年")
    other = _make_flac(out_dir / "伍佰 & China Blue" / "爱你伍佰年" / "挪威的森林-501.flac")
    _index(src)
    _index(other)

    r = _edit(client, dir="伍佰/爱你伍佰年", changes={"albumartist": "伍佰 & China Blue"},
              move_folder=True, preview=False)
    assert r.get_json()["data"]["new_dir"] == "伍佰 & China Blue/爱你伍佰年"

    moved = out_dir / "伍佰 & China Blue" / "爱你伍佰年" / "突然的自我-502.flac"
    assert moved.exists() and other.exists()
    assert not (out_dir / "伍佰").exists()          # 歌手文件夹也空了，一起删掉
    assert FLAC(str(moved))["albumartist"] == ["伍佰 & China Blue"]


def test_edit_skips_when_target_exists(client, out_dir):
    src = _make_flac(out_dir / "甲" / "专辑" / "歌-1.flac")
    taken = _make_flac(out_dir / "甲" / "新专辑" / "歌-1.flac")
    _index(src)

    r = _edit(client, dir="甲/专辑", changes={"album": "新专辑"}, move_folder=True, preview=False)
    result = r.get_json()["data"]["results"][0]

    assert result["status"] == "skipped" and "同名" in result["msg"]
    assert src.exists() and taken.exists()          # 两个文件都在，没覆盖
    assert FLAC(str(src)).get("album") is None      # 跳过的文件标签也不动


def test_edit_tags_only_keeps_folder(client, out_dir):
    a, b = _album_with_records(out_dir)

    r = _edit(client, dir="周杰伦/叶惠美 (2003)", changes={"album": "叶惠美X"},
              move_folder=False, preview=False)
    assert r.get_json()["data"]["new_dir"] == "周杰伦/叶惠美 (2003)"
    assert a.exists() and FLAC(str(a))["album"] == ["叶惠美X"]
    assert db.session.get(Song, ("101", "qq")).file_size == a.stat().st_size


def test_edit_year_suffix_kept_or_dropped(client, out_dir):
    # 原来不带年份的文件夹：改了年份也不加后缀
    p = _make_flac(out_dir / "甲" / "无年份" / "歌-1.flac")
    _index(p)
    d = _edit(client, dir="甲/无年份", changes={"year": "2020"}, move_folder=True,
              preview=True).get_json()["data"]
    assert d["new_dir"] == "甲/无年份"
    # 原来带年份、年份删掉：后缀也去掉
    q = _make_flac(out_dir / "甲" / "有年份 (2001)" / "歌-2.flac", date="2001")
    _index(q)
    d = _edit(client, dir="甲/有年份 (2001)", changes={"year": ""}, move_folder=True,
              preview=True).get_json()["data"]
    assert d["new_dir"] == "甲/有年份"


@pytest.mark.parametrize("changes,msg", [
    ({}, "没有要改"),
    ({"album": ""}, "专辑名"),
    ({"albumartist": " "}, "专辑歌手"),
    ({"year": "20"}, "四位"),
])
def test_edit_rejects_bad_input(client, out_dir, changes, msg):
    r = _edit(client, dir="甲/专辑", changes=changes, move_folder=True, preview=True)
    assert r.status_code == 400 and msg in r.get_json()["msg"]


def test_edit_refused_while_organizer_busy(client, out_dir):
    p = _make_flac(out_dir / "甲" / "专辑" / "歌-1.flac")
    _index(p)
    db.session.add(OrganizerJob(job_type="scan", status="running"))
    db.session.commit()

    r = _edit(client, dir="甲/专辑", changes={"album": "新"}, move_folder=True, preview=False)
    assert r.get_json()["code"] == 1 and "扫描" in r.get_json()["msg"]
    assert p.exists()


def test_update_tags_mp3_keeps_cover(tmp_path):
    """MP3：只改给定几项，封面（APIC）和别的帧不动；空值删掉该项"""
    from mutagen.id3 import APIC, ID3, TDRC, TIT2
    from mutagen.mp3 import MP3

    from core.metadata import update_tags

    # 几帧 128kbps/44.1kHz 的空 MPEG 帧，mutagen 能认成 MP3
    p = tmp_path / "a.mp3"
    p.write_bytes((b"\xff\xfb\x90\x64" + b"\0" * 413) * 5)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="晴天"))
    tags.add(TDRC(encoding=3, text="2003"))
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"cover"))
    tags.save(str(p))

    update_tags(p, {"album": "叶惠美", "albumartist": "周杰伦", "date": ""})

    audio = MP3(str(p))
    assert str(audio.tags["TALB"]) == "叶惠美"
    assert str(audio.tags["TPE2"]) == "周杰伦"
    assert "TDRC" not in audio.tags
    assert str(audio.tags["TIT2"]) == "晴天"
    assert audio.tags.getall("APIC")[0].data == b"cover"


# ======================================================================
# /api/library/songs/move（移到别的专辑）
# ======================================================================
def _move(client, **body):
    return client.post("/api/library/songs/move", json=body)


def test_move_preview_and_execute_with_retag(client, out_dir):
    a, b = _album_with_records(out_dir)                       # 周杰伦/叶惠美 (2003)
    target = out_dir / "周杰伦" / "十一月的萧邦 (2005)"
    t1 = _make_flac(target / "夜曲-1.flac", album="十一月的萧邦", albumartist="周杰伦", date="2005-11-01")
    _index(t1)

    d = _move(client, paths=[str(a)], target_dir="周杰伦/十一月的萧邦 (2005)",
              retag=True, preview=True).get_json()["data"]
    pl = d["plans"][0]
    assert pl["to"] == "周杰伦/十一月的萧邦 (2005)/晴天-101.flac" and pl["moved"]
    assert {"key": "album", "old": "叶惠美", "new": "十一月的萧邦"} in pl["tags"]
    assert {"key": "year", "old": "2003-07-31", "new": "2005-11-01"} in pl["tags"]
    assert a.exists()                                          # 预览不动

    body = _move(client, paths=[str(a)], target_dir="周杰伦/十一月的萧邦 (2005)",
                 retag=True, preview=False).get_json()
    assert body["code"] == 0 and body["data"]["results"][0]["status"] == "done"
    moved = target / "晴天-101.flac"
    assert moved.exists() and not a.exists()
    assert FLAC(str(moved))["album"] == ["十一月的萧邦"]
    assert len(FLAC(str(moved)).pictures) == 1
    assert db.session.get(Song, ("101", "qq")).file_path == str(moved)
    assert DownloadTask.query.first().file_path == str(moved)
    assert b.exists()                                          # 没选的不动，旧专辑还有歌就留着


def test_move_without_retag_keeps_tags(client, out_dir):
    src = _make_flac(out_dir / "甲" / "A" / "歌-1.flac", album="A")
    _make_flac(out_dir / "甲" / "B" / "别的-2.flac", album="B")
    _index(src)

    _move(client, paths=[str(src)], target_dir="甲/B", retag=False, preview=False)
    moved = out_dir / "甲" / "B" / "歌-1.flac"
    assert FLAC(str(moved))["album"] == ["A"]
    assert not (out_dir / "甲" / "A").exists()


def test_move_unplaced_root_file_never_touches_output_dir(client, out_dir):
    """根目录下的未归位文件移走后，下载目录本身不能被当成空文件夹删掉"""
    src = _make_flac(out_dir / "根目录的歌.flac")
    _make_flac(out_dir / "甲" / "B" / "别的-2.flac")
    _index(src)

    body = _move(client, paths=[str(src)], target_dir="甲/B", retag=False, preview=False).get_json()
    assert body["data"]["results"][0]["status"] == "done"
    assert out_dir.is_dir()
    assert (out_dir / "甲" / "B" / "根目录的歌.flac").exists()


def test_move_skips_same_album_and_existing_name(client, out_dir):
    a = _make_flac(out_dir / "甲" / "A" / "歌-1.flac")
    b = _make_flac(out_dir / "甲" / "B" / "歌-1.flac")

    d = _move(client, paths=[str(a), str(b)], target_dir="甲/B", retag=False,
              preview=True).get_json()["data"]
    skips = [p["skip"] for p in d["plans"]]
    assert skips == ["新位置已有同名文件", "已经在这张专辑里"]


@pytest.mark.parametrize("body,status", [
    ({"paths": [], "target_dir": "甲/B"}, 400),
    ({"paths": ["/etc/passwd"], "target_dir": "甲/B"}, 400),
    ({"paths": ["x"], "target_dir": "../B"}, 400),
])
def test_move_rejects_bad_input(client, out_dir, body, status):
    assert _move(client, preview=True, **body).status_code == status


def test_move_target_must_exist(client, out_dir):
    a = _make_flac(out_dir / "甲" / "A" / "歌-1.flac")
    r = _move(client, paths=[str(a)], target_dir="甲/不存在", preview=True)
    assert r.status_code == 400


# ======================================================================
# 空文件夹
# ======================================================================
def test_empty_dirs_find_and_remove(client, out_dir):
    (out_dir / "空歌手" / "空专辑1").mkdir(parents=True)
    (out_dir / "空歌手" / "空专辑2").mkdir(parents=True)
    (out_dir / "有歌手" / "空专辑").mkdir(parents=True)
    _make_flac(out_dir / "有歌手" / "有专辑" / "歌-1.flac")
    (out_dir / ".trash" / "空的").mkdir(parents=True)          # 回收站不管
    (out_dir / "只有隐藏" / ".x").mkdir(parents=True)           # 带隐藏子文件夹的不删

    d = client.get("/api/library/empty-dirs").get_json()["data"]
    assert d["dirs"] == ["有歌手/空专辑", "空歌手"]               # 整棵空的只列最上层

    body = client.post("/api/library/empty-dirs/remove").get_json()
    assert body["data"]["removed"] == 2
    assert not (out_dir / "空歌手").exists()
    assert not (out_dir / "有歌手" / "空专辑").exists()
    assert (out_dir / "有歌手" / "有专辑" / "歌-1.flac").exists()
    assert (out_dir / ".trash" / "空的").exists()
    assert (out_dir / "只有隐藏" / ".x").exists()
    assert out_dir.is_dir()


# ======================================================================
# 未归位 + 文件有问题
# ======================================================================
def test_unplaced_lists_with_problems(client, out_dir):
    good = _make_flac(out_dir / "零散" / "好的.flac")
    empty = out_dir / "零散" / "空的.flac"
    empty.write_bytes(b"")
    fake = out_dir / "根目录假的.flac"
    fake.write_bytes(b"\xff\xfb\x90\x64" + b"\0" * 100)        # MP3 内容
    placed = _make_flac(out_dir / "甲" / "A" / "歌-1.flac")
    for p in (good, empty, fake, placed):
        _index(p)

    items = client.get("/api/library/unplaced").get_json()["data"]["items"]
    by_name = {i["filename"]: i["problem"] for i in items}
    assert set(by_name) == {"好的.flac", "空的.flac", "根目录假的.flac"}
    assert by_name["好的.flac"] == ""
    assert by_name["空的.flac"] == "空文件"
    assert "MP3" in by_name["根目录假的.flac"]
