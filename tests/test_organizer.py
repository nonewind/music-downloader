"""音乐整理后端 MVP 测试：扫描 / 分组 / 规格比较 / 回收站清理 / Song 联动。

覆盖：
1.  extract_song_id 文件名尾部 ID 提取
2.  scan_directory 扫描（扩展名过滤、.trash 与隐藏目录剪枝、参数透传、读失败容错）
3.  group_duplicates 分组（same_id 优先、identical 字节级、互斥与排序）
4.  spec_rank 规格等级（hires > lossless > 320k > 128k）
5.  POST /api/organize/clean 白名单（目录外路径拒删）
6.  trash 模式（移入 .trash + Song repair 联动，quality 不动）
7.  direct 模式（organize_direct_delete=true 直接 unlink）
8.  trash 同名冲突（追加 _YYYYmmddHHMMSS 时间戳后缀）
9.  POST /api/organize/scan 响应结构与目录不存在分支、403 权限风格
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.organizer as organizer
from core.organizer import (
    DuplicateGroup,
    FileEntry,
    extract_song_id,
    file_sha1,
    group_duplicates,
    scan_directory,
    spec_rank,
)


# ======================================================================
# mutagen 桩：core.organizer.MutagenFile 调用点 monkeypatch
# ======================================================================
class FakeInfo:
    """模拟 mutagen .info 音频参数（bitrate 单位与 mutagen 一致：bps）"""

    def __init__(self, length=0.0, bitrate=0, sample_rate=0, channels=0,
                 bits_per_sample=0):
        self.length = length
        self.bitrate = bitrate
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits_per_sample = bits_per_sample


@pytest.fixture
def fake_audio_factory(monkeypatch):
    """安装 FakeAudio 到 core.organizer.MutagenFile，控制参数/标签/异常"""

    def _install(info=None, tags=None, exc=None):
        def _fake(_path):
            if exc is not None:
                raise exc
            return SimpleNamespace(
                info=info if info is not None else FakeInfo(),
                tags=tags if tags is not None else {},
            )

        monkeypatch.setattr(organizer, "MutagenFile", _fake)
        return _fake

    return _install


def _entry(path, size=None, lossless=False, sample_rate=0, bits_per_sample=0,
           bitrate_kbps=0, song_id=None):
    """按真实文件构造 FileEntry（song_id 缺省时从文件名提取）"""
    p = Path(path)
    st = p.stat()
    return FileEntry(
        path=p,
        size=st.st_size if size is None else size,
        mtime=st.st_mtime,
        ext=p.suffix.lower(),
        song_id=extract_song_id(p.stem) if song_id is None else song_id,
        lossless=lossless,
        sample_rate=sample_rate,
        bits_per_sample=bits_per_sample,
        bitrate_kbps=bitrate_kbps,
    )


# ======================================================================
# 1. extract_song_id
# ======================================================================
def test_extract_song_id_basic():
    assert extract_song_id("晴天-12345") == "12345"


def test_extract_song_id_takes_last_segment():
    assert extract_song_id("A-B-abc12") == "abc12"


def test_extract_song_id_no_dash():
    assert extract_song_id("歌名") is None


def test_extract_song_id_chinese_tail_rejected():
    # 中文在 str.isalnum() 里也为 True，必须按 ASCII 字母数字判断
    assert extract_song_id("歌名-纯中文") is None


def test_extract_song_id_too_long_rejected():
    assert extract_song_id("歌名-" + "a" * 33) is None


def test_extract_song_id_mixed_alnum():
    assert extract_song_id("歌名-12a") == "12a"


# ======================================================================
# 2. scan_directory
# ======================================================================
def test_scan_directory_filters_and_parses(tmp_path, fake_audio_factory):
    fake_audio_factory(
        info=FakeInfo(length=200.5, bitrate=320000, sample_rate=44100,
                      channels=2, bits_per_sample=16),
        tags={"title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
    )
    (tmp_path / "晴天-12345.mp3").write_bytes(b"x" * 100)
    (tmp_path / "七里香-abc12.flac").write_bytes(b"y" * 200)
    (tmp_path / "notes.txt").write_text("skip me")
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "旧歌.mp3").write_bytes(b"z")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "hide.mp3").write_bytes(b"h")
    sub = tmp_path / "周杰伦" / "叶惠美"
    sub.mkdir(parents=True)
    (sub / "以父之名-999.flac").write_bytes(b"w" * 50)

    entries = scan_directory(tmp_path)

    assert len(entries) == 3
    by_name = {e.path.name: e for e in entries}
    assert set(by_name) == {"晴天-12345.mp3", "七里香-abc12.flac", "以父之名-999.flac"}

    e = by_name["晴天-12345.mp3"]
    assert e.song_id == "12345"
    assert e.title == "晴天"
    assert e.artist == "周杰伦"
    assert e.album == "叶惠美"
    assert e.duration_ms == 200500          # .info.length 秒 → 毫秒
    assert e.bitrate_kbps == 320            # .info.bitrate bps → kbps
    assert e.sample_rate == 44100
    assert e.channels == 2
    assert e.bits_per_sample == 16
    assert e.lossless is False
    assert e.ext == ".mp3"
    assert e.size == 100

    f = by_name["七里香-abc12.flac"]
    assert f.lossless is True               # flac=True；mp3/ogg/m4a/opus=False
    assert f.song_id == "abc12"

    s = by_name["以父之名-999.flac"]
    assert s.song_id == "999"               # 子目录文件也被扫到


def test_scan_directory_missing_tags_tolerated(tmp_path, fake_audio_factory):
    """无 tag（tags=None）：tag 置空、音频参数仍透传"""
    fake_audio_factory(info=FakeInfo(length=10, bitrate=128000, sample_rate=44100,
                                     channels=2, bits_per_sample=0), tags=None)
    (tmp_path / "raw-7.m4a").write_bytes(b"m")

    entries = scan_directory(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.title == "" and e.artist == "" and e.album == ""
    assert e.duration_ms == 10000
    assert e.bitrate_kbps == 128


def test_scan_directory_read_failure_tolerated(tmp_path, fake_audio_factory):
    """读取异常：参数置 0、tag 置空、仍入列表"""
    fake_audio_factory(exc=RuntimeError("boom"))
    (tmp_path / "歌-1.mp3").write_bytes(b"x")

    entries = scan_directory(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.duration_ms == 0
    assert e.bitrate_kbps == 0
    assert e.sample_rate == 0
    assert e.channels == 0
    assert e.bits_per_sample == 0
    assert e.title == "" and e.artist == "" and e.album == ""
    assert e.song_id == "1"


# ======================================================================
# 3. group_duplicates
# ======================================================================
def test_group_duplicates_same_id_prefers_higher_spec(tmp_path):
    mp3 = tmp_path / "晴天-12345.mp3"
    mp3.write_bytes(b"a" * 100)
    flac = tmp_path / "晴天-12345.flac"
    flac.write_bytes(b"b" * 1000)

    groups = group_duplicates([_entry(mp3, bitrate_kbps=128),
                               _entry(flac, lossless=True, sample_rate=44100,
                                      bits_per_sample=16)])

    assert len(groups) == 1
    g = groups[0]
    assert g.group_type == "same_id"
    assert g.recommended == str(flac)       # 组内规格最高（无损）为推荐保留
    assert {i.path for i in g.items} == {mp3, flac}


def test_group_duplicates_identical_by_real_content(tmp_path):
    a = tmp_path / "a.mp3"
    a.write_bytes(b"SAMEDATA" * 10)
    b = tmp_path / "b.mp3"
    b.write_bytes(b"SAMEDATA" * 10)
    c = tmp_path / "c.mp3"
    c.write_bytes(b"OTHERXXX" * 10)

    groups = group_duplicates([_entry(a), _entry(b), _entry(c)])

    assert len(groups) == 1
    g = groups[0]
    assert g.group_type == "identical"
    assert {i.path for i in g.items} == {a, b}
    assert g.recommended in (str(a), str(b))


def test_group_duplicates_empty_when_all_unique(tmp_path):
    a = tmp_path / "a-1.mp3"
    a.write_bytes(b"1")
    b = tmp_path / "b-2.mp3"
    b.write_bytes(b"2")
    assert group_duplicates([_entry(a), _entry(b)]) == []


def test_same_id_files_do_not_reenter_identical_group(tmp_path):
    """same_id 组内文件即使字节级相同，也不再进 identical 组"""
    a = tmp_path / "晴天-12345.mp3"
    a.write_bytes(b"SAMEDATA" * 5)
    b = tmp_path / "爱-12345.mp3"
    b.write_bytes(b"SAMEDATA" * 5)

    groups = group_duplicates([_entry(a), _entry(b)])

    assert len(groups) == 1
    assert groups[0].group_type == "same_id"


def test_group_duplicates_sorted_by_count_then_max_size(tmp_path):
    """排序：组按 items 数降序、再按组内最大 size 降序"""
    # 组1：same_id，2 个文件（小）
    a1 = tmp_path / "晴天-111.mp3"
    a1.write_bytes(b"x" * 10)
    a2 = tmp_path / "晴天-111.flac"
    a2.write_bytes(b"x" * 10)
    # 组2：identical，3 个文件（大）
    b1 = tmp_path / "b1.mp3"
    b1.write_bytes(b"Y" * 5000)
    b2 = tmp_path / "b2.mp3"
    b2.write_bytes(b"Y" * 5000)
    b3 = tmp_path / "b3.mp3"
    b3.write_bytes(b"Y" * 5000)
    # 组3：identical，2 个文件（中）
    c1 = tmp_path / "c1.mp3"
    c1.write_bytes(b"Z" * 100)
    c2 = tmp_path / "c2.mp3"
    c2.write_bytes(b"Z" * 100)

    groups = group_duplicates([_entry(a1), _entry(a2), _entry(b1), _entry(b2),
                               _entry(b3), _entry(c1), _entry(c2)])

    assert [len(g.items) for g in groups] == [3, 2, 2]
    assert groups[0].group_type == "identical"
    # 同为 2 个文件：组内最大 size 大的在前（组3=100B > 组1=10B）
    assert {i.path for i in groups[1].items} == {c1, c2}
    assert {i.path for i in groups[2].items} == {a1, a2}


# ======================================================================
# 4. spec_rank
# ======================================================================
def test_spec_rank_ordering():
    hires = FileEntry(path=Path("hires.flac"), lossless=True,
                      sample_rate=96000, bits_per_sample=24)
    lossless = FileEntry(path=Path("cd.flac"), lossless=True,
                         sample_rate=44100, bits_per_sample=16)
    mp3_320 = FileEntry(path=Path("320.mp3"), lossless=False, bitrate_kbps=320)
    mp3_128 = FileEntry(path=Path("128.mp3"), lossless=False, bitrate_kbps=128)

    ranked = sorted([mp3_128, mp3_320, lossless, hires], key=spec_rank)
    assert [r.path for r in ranked] == [
        Path("128.mp3"), Path("320.mp3"), Path("cd.flac"), Path("hires.flac")]
    assert max([mp3_128, mp3_320, lossless, hires], key=spec_rank) is hires


def test_file_sha1_streaming(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    import hashlib
    assert file_sha1(p) == hashlib.sha1(b"hello world").hexdigest()


# ======================================================================
# 端点测试基建：测试专用 app 上注册 api 蓝图 + 管理员登录态
# ======================================================================
@pytest.fixture
def admin_client(webapp_app):
    """注册 api 蓝图并注入管理员登录态的 test client"""
    from models import User, db
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"    # 裸 app 无 secret_key，session 打不开
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="root", is_admin=True, enabled=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = user.id
    return client


@pytest.fixture
def member_client(webapp_app):
    """注册 api 蓝图并注入普通用户（非管理员）登录态的 test client"""
    from models import User, db
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="member", is_admin=False, enabled=True)
    user.set_password("member1")
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = user.id
    return client


def _setup_out_dir(tmp_path):
    """把下载目录指向 tmp_path/dl 并创建，返回目录 Path"""
    from models import Setting
    out_dir = tmp_path / "dl"
    out_dir.mkdir()
    Setting.set("output_dir", str(out_dir))
    return out_dir


# ======================================================================
# 5. clean 白名单：外部路径拒
# ======================================================================
def test_clean_rejects_path_outside_output_dir(admin_client, tmp_path):
    out_dir = _setup_out_dir(tmp_path)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")

    resp = admin_client.post("/api/organize/clean",
                             json={"delete_paths": [str(outside)]})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["deleted"] == []
    assert len(body["data"]["failed"]) == 1
    assert body["data"]["failed"][0]["path"] == str(outside)
    assert body["data"]["failed"][0]["reason"]
    assert outside.exists()                 # 文件仍在


def test_clean_requires_admin(member_client, tmp_path):
    _setup_out_dir(tmp_path)
    f = tmp_path / "dl" / "a.mp3"
    f.write_bytes(b"x")

    resp = member_client.post("/api/organize/clean",
                              json={"delete_paths": [str(f)]})
    assert resp.status_code == 403
    assert f.exists()


# ======================================================================
# 6. trash 模式：移入 .trash + Song repair 联动（quality 不动）
# ======================================================================
def test_clean_trash_mode_moves_file_and_repairs_song(admin_client, tmp_path):
    from models import Song, db
    out_dir = _setup_out_dir(tmp_path)
    keep = out_dir / "晴天-12345.flac"
    keep.write_bytes(b"K" * 222)
    dup = out_dir / "周杰伦" / "晴天-12345.mp3"
    dup.parent.mkdir()
    dup.write_bytes(b"D" * 111)

    song = Song(id="12345", platform="netease", name="晴天", artists="周杰伦",
                album="叶惠美", duration_ms=0, quality="lossless",
                file_path=str(dup), file_size=111, playlist_id=None,
                source_name="", status="success", account_id=None)
    db.session.add(song)
    db.session.commit()

    resp = admin_client.post("/api/organize/clean", json={
        "delete_paths": [str(dup)],
        "repair": {str(dup): str(keep)},
    })
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["failed"] == []
    assert [Path(p).name for p in body["data"]["deleted"]] == ["晴天-12345.mp3"]

    # 原位置消失，文件出现在 .trash
    assert not dup.exists()
    trash_file = out_dir / ".trash" / "晴天-12345.mp3"
    assert trash_file.exists()

    # Song 联动：file_path 重指保留文件、file_size 更新、quality 不动
    fresh = Song.query.filter_by(id="12345", platform="netease").first()
    assert Path(fresh.file_path).resolve() == keep.resolve()
    assert fresh.file_size == 222
    assert fresh.quality == "lossless"

    assert len(body["data"]["repaired"]) == 1
    rep = body["data"]["repaired"][0]
    assert rep["song"] == "netease/12345"
    assert Path(rep["from"]).resolve() == dup.resolve()
    assert Path(rep["to"]).resolve() == keep.resolve()


def test_clean_repair_skipped_when_kept_file_missing(admin_client, tmp_path):
    """保留文件不存在 → 不做 repair，Song 不动"""
    from models import Song, db
    out_dir = _setup_out_dir(tmp_path)
    dup = out_dir / "a.mp3"
    dup.write_bytes(b"D")

    song = Song(id="1", platform="netease", name="a", quality="standard",
                file_path=str(dup), file_size=1, status="success")
    db.session.add(song)
    db.session.commit()

    resp = admin_client.post("/api/organize/clean", json={
        "delete_paths": [str(dup)],
        "repair": {str(dup): str(out_dir / "gone.flac")},
    })
    body = resp.get_json()

    assert body["code"] == 0
    assert body["data"]["repaired"] == []
    assert not dup.exists()
    fresh = Song.query.filter_by(id="1").first()
    assert Path(fresh.file_path) == dup     # Song 未被改指


def test_clean_keeps_song_pointing_at_surviving_file(admin_client, tmp_path):
    """Song 指向的文件未被删（指向保留文件）→ Song 不动"""
    from models import Song, db
    out_dir = _setup_out_dir(tmp_path)
    keep = out_dir / "keep.flac"
    keep.write_bytes(b"K" * 10)
    dup = out_dir / "dup.mp3"
    dup.write_bytes(b"D" * 5)

    song = Song(id="2", platform="netease", name="k", quality="lossless",
                file_path=str(keep), file_size=10, status="success")
    db.session.add(song)
    db.session.commit()

    resp = admin_client.post("/api/organize/clean", json={
        "delete_paths": [str(dup)],
        "repair": {str(dup): str(keep)},
    })
    body = resp.get_json()

    assert body["code"] == 0
    assert body["data"]["repaired"] == []
    fresh = Song.query.filter_by(id="2").first()
    assert Path(fresh.file_path).resolve() == keep.resolve()
    assert fresh.file_size == 10


# ======================================================================
# 7. direct 模式：直接 unlink
# ======================================================================
def test_clean_direct_delete_mode_unlinks(admin_client, tmp_path):
    from models import Setting
    out_dir = _setup_out_dir(tmp_path)
    Setting.set("organize_direct_delete", "true")
    dup = out_dir / "a.mp3"
    dup.write_bytes(b"D")

    resp = admin_client.post("/api/organize/clean",
                             json={"delete_paths": [str(dup)]})
    body = resp.get_json()

    assert body["code"] == 0
    assert not dup.exists()
    assert not (out_dir / ".trash").exists()    # 不产生回收站目录


def test_clean_trash_inside_whitelist_rejected(admin_client, tmp_path):
    """回收站里的文件视为已删除，不可再次清理"""
    out_dir = _setup_out_dir(tmp_path)
    trash_file = out_dir / ".trash" / "already.mp3"
    trash_file.parent.mkdir()
    trash_file.write_bytes(b"T")

    resp = admin_client.post("/api/organize/clean",
                             json={"delete_paths": [str(trash_file)]})
    body = resp.get_json()

    assert body["code"] == 0
    assert body["data"]["deleted"] == []
    assert len(body["data"]["failed"]) == 1
    assert trash_file.exists()


# ======================================================================
# 8. trash 同名冲突：时间戳后缀
# ======================================================================
def test_clean_trash_name_conflict_gets_timestamp_suffix(admin_client, tmp_path):
    out_dir = _setup_out_dir(tmp_path)
    trash = out_dir / ".trash"
    trash.mkdir()
    (trash / "a.mp3").write_bytes(b"old")
    dup = out_dir / "a.mp3"
    dup.write_bytes(b"new")

    resp = admin_client.post("/api/organize/clean",
                             json={"delete_paths": [str(dup)]})
    body = resp.get_json()

    assert body["code"] == 0
    assert not dup.exists()
    # 原回收站同名文件不被覆盖
    assert (trash / "a.mp3").read_bytes() == b"old"
    moved = [p for p in trash.iterdir() if p.name != "a.mp3"]
    assert len(moved) == 1
    assert re.fullmatch(r"a_\d{14}\.mp3", moved[0].name)


# ======================================================================
# 9. scan 端点：响应结构 / 目录不存在 / 权限
# ======================================================================
def test_scan_endpoint_returns_groups_and_items(admin_client, tmp_path):
    out_dir = _setup_out_dir(tmp_path)
    (out_dir / "晴天-12345.mp3").write_bytes(b"a" * 100)
    (out_dir / "晴天-12345.flac").write_bytes(b"b" * 1000)

    resp = admin_client.post("/api/organize/scan")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["scanned"] == 2
    assert body["data"]["duration_s"] >= 0
    groups = body["data"]["groups"]
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "same_id"
    assert g["recommended"].endswith(".flac")
    item = g["items"][0]
    assert item["rel"] == item["filename"] == Path(item["path"]).name
    assert item["song_id"] == "12345"
    assert item["ext"] in (".mp3", ".flac")
    for key in ("path", "filename", "rel", "ext", "size", "mtime", "song_id",
                "title", "artist", "album", "duration_ms", "bitrate_kbps",
                "sample_rate", "channels", "bits_per_sample", "lossless"):
        assert key in item


def test_scan_endpoint_missing_dir(admin_client, tmp_path):
    from models import Setting
    Setting.set("output_dir", str(tmp_path / "nope"))

    resp = admin_client.post("/api/organize/scan")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 1
    assert body["msg"] == "下载目录不存在"


def test_scan_endpoint_requires_admin(member_client, tmp_path):
    _setup_out_dir(tmp_path)
    resp = member_client.post("/api/organize/scan")
    assert resp.status_code == 403


# 防止未使用导入告警（DuplicateGroup/FileEntry 是导出契约的一部分）
_ = (DuplicateGroup, FileEntry, file_sha1)
