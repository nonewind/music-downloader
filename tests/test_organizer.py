"""音乐整理后端 MVP 测试：扫描 / 分组 / 规格比较。

覆盖：
1.  extract_song_id 文件名尾部 ID 提取
2.  scan_directory 扫描（扩展名过滤、.trash 与隐藏目录剪枝、参数透传、读失败容错）
3.  group_duplicates 分组（same_id 优先、identical 字节级、互斥与排序）
4.  spec_rank 规格等级（hires > lossless > 320k > 128k）
5.  POST /api/organize/scan 后台任务化契约（返回 job_id）、目录不存在分支、
    403 权限风格；分组结果改经 GET /api/organize/duplicates 读取

（clean 端点/清理任务用例 ORG-03 起迁移至 tests/test_organizer_clean.py：
后台任务契约 + 逐文件计划 + 白名单 + 回收站命名冲突）
"""

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
           bitrate_kbps=0, song_id=None, platform=None):
    """按真实文件构造 FileEntry（song_id 缺省时从文件名提取）"""
    p = Path(path)
    st = p.stat()
    return FileEntry(
        path=p,
        size=st.st_size if size is None else size,
        mtime=st.st_mtime,
        ext=p.suffix.lower(),
        song_id=extract_song_id(p.stem) if song_id is None else song_id,
        platform=platform,
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

    # ORG-02：same_id 确定组须 (platform, song_id) 双可信
    groups = group_duplicates([_entry(mp3, bitrate_kbps=128, platform="netease"),
                               _entry(flac, lossless=True, sample_rate=44100,
                                      bits_per_sample=16, platform="netease")])

    assert len(groups) == 1
    g = groups[0]
    assert g.group_type == "same_id"
    assert g.platform == "netease"
    assert g.song_id == "12345"
    assert g.recommended == str(flac)       # 组内规格最高（无损）为推荐保留
    assert "无损" in g.reason
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


def test_same_song_id_different_platforms_not_merged(tmp_path):
    """ORG-02 核心缺陷修复：netease 与 qq 的同 song_id 是两首歌，
    不得合并进同一个确定组（也不得并入候选组）"""
    a = tmp_path / "晴天-18886.mp3"
    a.write_bytes(b"a" * 100)
    b = tmp_path / "晴天-18886.flac"
    b.write_bytes(b"b" * 1000)

    groups = group_duplicates([
        _entry(a, platform="netease"),
        _entry(b, platform="qq"),
    ])

    assert groups == []                     # 各自单独不成组


def test_same_id_files_do_not_reenter_identical_group(tmp_path):
    """ID 组（含无平台的 same_id_candidate）内文件即使字节级相同，
    也不再进 identical 组"""
    a = tmp_path / "晴天-12345.mp3"
    a.write_bytes(b"SAMEDATA" * 5)
    b = tmp_path / "爱-12345.mp3"
    b.write_bytes(b"SAMEDATA" * 5)

    groups = group_duplicates([_entry(a), _entry(b)])

    assert len(groups) == 1
    # ORG-02：无平台可信身份 → 候选组（不再按裸 song_id 出 same_id 确定组）
    assert groups[0].group_type == "same_id_candidate"
    assert groups[0].platform is None


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
# 5. scan 端点（ORG-01 后台任务化）：job_id 契约 / 目录不存在 / 权限
# ======================================================================
def test_scan_endpoint_returns_job_id_and_duplicates(admin_client, tmp_path):
    """POST scan 只入队返回 job_id；分组结果经 GET duplicates 读取"""
    from models import OrganizerJob
    from organizer_manager import OrganizerManager

    out_dir = _setup_out_dir(tmp_path)
    (out_dir / "晴天-12345.mp3").write_bytes(b"a" * 100)
    (out_dir / "晴天-12345.flac").write_bytes(b"b" * 1000)

    mgr = OrganizerManager(admin_client.application)
    admin_client.application.config["ORGANIZER_MANAGER"] = mgr

    resp = admin_client.post("/api/organize/scan")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["status"] == "pending"
    jid = body["data"]["job_id"]
    assert OrganizerJob.query.get(jid) is not None

    mgr.run_job(jid)                        # 测试内同步执行，不依赖线程

    resp = admin_client.get("/api/organize/duplicates")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    groups = body["data"]["groups"]
    assert len(groups) == 1
    g = groups[0]
    # ORG-02：无下载历史、仅文件名解析的裸 song_id → 候选组（非确定组）
    assert g["group_type"] == "same_id_candidate"
    assert g["platform"] is None
    assert g["song_id"] == "12345"
    assert "reason" in g
    assert g["recommended"].endswith(".flac")
    item = g["items"][0]
    assert item["rel"] == item["filename"] == Path(item["path"]).name
    assert item["song_id"] == "12345"
    assert item["downloads"]["count"] == 0
    assert item["downloads"]["latest"] is None
    assert item["ext"] in (".mp3", ".flac")
    assert isinstance(item["id"], int)
    for key in ("id", "path", "filename", "rel", "ext", "size", "mtime",
                "song_id", "title", "artist", "album", "duration_ms",
                "bitrate_kbps", "sample_rate", "channels", "bits_per_sample",
                "lossless", "status", "platform", "downloads"):
        assert key in item


def test_scan_endpoint_missing_dir(admin_client, tmp_path):
    from models import Setting
    from organizer_manager import OrganizerManager

    admin_client.application.config["ORGANIZER_MANAGER"] = OrganizerManager(
        admin_client.application)
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
