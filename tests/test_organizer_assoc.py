"""ORG-02 重复识别优化 + 下载历史关联 测试。

覆盖：
1.  test_same_platform_mp3_flac_grouped_confirmed
    同 (platform, song_id) 的 mp3+flac → 确定身份组 same_id（组带 platform/
    song_id），flac 为 recommended，reason 含"无损"
2.  test_different_platform_same_id_not_merged
    netease/qq 同 song_id（均有可信关联）→ 不产生含两者的确定组（核心缺陷修复）
3.  test_identical_content_different_paths
    字节相同路径不同（真实 SHA1）→ identical 组（回归）
4.  test_three_downloads_two_files_associated
    3 条 done 任务两条路径 → 两行均回填身份；duplicates 中 downloads.count
    与 latest 摘要正确
5.  test_file_without_history_stays_candidate
    无任务无 Song 命中、仅文件名解析 song_id → same_id_candidate 组
    （platform 为 None，与确定组区分）
6.  test_history_exists_file_missing
    任务在、文件删 → 行 missing、不出现在 duplicates、任务快照逐字段不变
7.  test_missing_spec_params_recommendation
    全零技术参数 → 仍有 recommended，reason 落"文件更大/文件更新/规格相同"，
    不虚构数值
8.  test_groups_change_after_rescan
    扫→加重复→组出现；删其一→再扫→组消失
9.  test_download_snapshots_never_modified
    扫描+关联+分组全流程后 DownloadTask 快照逐字段相等（零写入）
10. test_association_priority_task_over_song
    任务身份与 Song 身份冲突 → 回填任务侧；无任务时回退 Song
11. test_recommend_reason_axis_priority
    recommend_reason 纯函数：按 spec_rank 轴序输出第一决定性依据

fixture 手法与 test_organizer_jobs.py 一致：fake mutagen、内存库、
tmp_path output_dir、管理员登录态 client。
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.organizer as organizer
from core.organizer import (
    FileEntry,
    group_duplicates,
    recommend_reason,
)

from models import (
    DownloadTask,
    LocalAudioFile,
    Song,
    Setting,
    User,
    db,
)
from organizer_manager import OrganizerManager


# ======================================================================
# mutagen 桩（手法与 test_organizer_jobs.py 一致）
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


@pytest.fixture
def manager(webapp_app):
    """未启动线程的 OrganizerManager（run_job 测试线程内同步执行）"""
    (app,) = webapp_app
    return OrganizerManager(app)


@pytest.fixture
def admin_client(webapp_app):
    """注册 /api 蓝图 + 管理员登录态 + 已配置 ORGANIZER_MANAGER 的 client"""
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"
    app.config["ORGANIZER_MANAGER"] = OrganizerManager(app)
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="root", is_admin=True, enabled=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = user.id
    return client


def _setup_out_dir(tmp_path):
    """把下载目录指向 tmp_path/dl 并创建，返回目录 Path"""
    out_dir = tmp_path / "dl"
    out_dir.mkdir()
    Setting.set("output_dir", str(out_dir))
    return out_dir


def _make_audio(out_dir, name, size=100, payload=None):
    p = out_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload if payload is not None else b"x" * size)
    return p


def _make_task(platform, song_id, file_path, song_name="测试歌曲",
               quality="standard", file_size=100, status="done",
               created_at=None, updated_at=None):
    """造一条 DownloadTask（快照三列按成功下载语义写入）"""
    t = DownloadTask(
        platform=platform, song_id=song_id, song_name=song_name,
        status=status, quality=quality, file_path=str(file_path),
        file_size=file_size,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
        updated_at=updated_at or datetime(2026, 1, 1, 12, 0, 0),
    )
    db.session.add(t)
    return t


def _run(manager, job_id):
    """同步执行并使外层测试 session 的缓存实体失效（先例见
    test_organizer_jobs._run：不 expire_all 会打到 identity map 旧值）"""
    manager.run_job(job_id)
    db.session.expire_all()


def _duplicates(client):
    return client.get("/api/organize/duplicates").get_json()["data"]["groups"]


def _row(path):
    return LocalAudioFile.query.filter_by(path=str(path)).first()


# ======================================================================
# 1. 同平台同 ID：确定身份组
# ======================================================================
def test_same_platform_mp3_flac_grouped_confirmed(admin_client, manager,
                                                  tmp_path,
                                                  fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    mp3 = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    flac = _make_audio(out_dir, "晴天-12345.flac", size=1000)
    fake_audio_factory(
        info=FakeInfo(length=200.0, bitrate=320000, sample_rate=44100,
                      channels=2, bits_per_sample=16),
        tags={"title": "晴天"},
    )
    _make_task("netease", "12345", mp3, quality="exhigh")
    _make_task("netease", "12345", flac, quality="lossless")
    db.session.commit()

    _run(manager, manager.submit_scan().id)

    # 索引行均回填可信身份
    assert _row(mp3).platform == "netease"
    assert _row(mp3).song_id == "12345"
    assert _row(flac).platform == "netease"
    assert _row(flac).song_id == "12345"

    groups = _duplicates(admin_client)
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "same_id"          # 确定身份组（非 candidate）
    assert g["platform"] == "netease"
    assert g["song_id"] == "12345"
    assert g["recommended"].endswith(".flac")    # 组内规格最高（无损）保留
    assert "无损" in g["reason"]
    assert len(g["items"]) == 2
    for item in g["items"]:
        assert item["platform"] == "netease"
        assert item["song_id"] == "12345"
        assert item["downloads"]["count"] == 1
        assert item["downloads"]["latest"]["song_name"] == "测试歌曲"


# ======================================================================
# 2. 不同平台同 ID：绝不合并（本任务核心缺陷修复）
# ======================================================================
def test_different_platform_same_id_not_merged(admin_client, manager,
                                               tmp_path, fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-18886.mp3", size=100)
    b = _make_audio(out_dir, "晴天-18886.flac", size=1000)
    fake_audio_factory(tags={"title": "晴天"})
    _make_task("netease", "18886", a)
    _make_task("qq", "18886", b)
    db.session.commit()

    _run(manager, manager.submit_scan().id)

    assert _row(a).platform == "netease" and _row(a).song_id == "18886"
    assert _row(b).platform == "qq" and _row(b).song_id == "18886"

    # 全栈：各自 (platform, song_id) 键不同、size 不同 → 不成任何组
    assert _duplicates(admin_client) == []

    # 核心层直接证据：同 song_id 不同 platform 不得合并成组
    ea = FileEntry(path=Path(a), size=100, ext=".mp3", song_id="18886",
                   platform="netease")
    eb = FileEntry(path=Path(b), size=1000, ext=".flac", song_id="18886",
                   platform="qq")
    assert group_duplicates([ea, eb]) == []


# ======================================================================
# 3. 字节级相同（真实 SHA1）→ identical 组（回归）
# ======================================================================
def test_identical_content_different_paths(admin_client, manager, tmp_path,
                                           fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    payload = b"SAMEDATA" * 10
    a = _make_audio(out_dir, "same_a.mp3", payload=payload)
    b = _make_audio(out_dir, "same_b.mp3", payload=payload)
    fake_audio_factory()
    import os
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))                   # b 更新 → 推荐保留 b

    _run(manager, manager.submit_scan().id)

    groups = _duplicates(admin_client)
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "identical"
    assert g["platform"] is None
    assert g["song_id"] is None
    assert {i["path"] for i in g["items"]} == {str(a), str(b)}
    assert g["recommended"] == str(b)           # mtime 轴唯一胜出
    assert g["reason"] == "文件更新"


# ======================================================================
# 4. 3 条任务 2 个文件：关联回填 + downloads 计数/最新摘要
# ======================================================================
def test_three_downloads_two_files_associated(admin_client, manager, tmp_path,
                                              fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    b = _make_audio(out_dir, "晴天-12345.flac", size=1000)
    fake_audio_factory(tags={"title": "晴天"})
    t1 = _make_task("netease", "12345", a, song_name="旧下载",
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1))
    t2 = _make_task("netease", "12345", a, song_name="最新下载",
                    quality="lossless", created_at=datetime(2026, 3, 1),
                    updated_at=datetime(2026, 3, 1))
    t3 = _make_task("netease", "12345", b, song_name="flac版",
                    quality="lossless", created_at=datetime(2026, 2, 1),
                    updated_at=datetime(2026, 2, 1))
    db.session.commit()

    _run(manager, manager.submit_scan().id)

    # 两行均回填任务侧身份
    assert (_row(a).platform, _row(a).song_id) == ("netease", "12345")
    assert (_row(b).platform, _row(b).song_id) == ("netease", "12345")

    groups = _duplicates(admin_client)
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "same_id"
    by_name = {Path(i["path"]).name: i for i in g["items"]}
    assert by_name[a.name]["downloads"]["count"] == 2     # A 命中 2 条
    assert by_name[b.name]["downloads"]["count"] == 1     # B 命中 1 条

    latest = by_name[a.name]["downloads"]["latest"]
    assert latest["task_id"] == t2.pk                     # 最新一条
    assert latest["song_name"] == "最新下载"
    assert latest["quality"] == "lossless"
    assert latest["created_at"] == "2026-03-01 00:00:00"  # 与 /api/tasks 同格式
    assert by_name[b.name]["downloads"]["latest"]["task_id"] == t3.pk


# ======================================================================
# 5. 无历史命中：仅文件名解析 song_id → 候选组
# ======================================================================
def test_file_without_history_stays_candidate(admin_client, manager, tmp_path,
                                              fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3", size=100)
    _make_audio(out_dir, "晴天-12345.flac", size=1000)
    fake_audio_factory(tags={"title": "晴天"})

    _run(manager, manager.submit_scan().id)

    groups = _duplicates(admin_client)
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "same_id_candidate"   # 候选组：与确定组区分
    assert g["platform"] is None                    # 平台未知
    assert g["song_id"] == "12345"
    for item in g["items"]:
        assert item["platform"] is None             # 行未被可信回填
        assert item["song_id"] == "12345"
        assert item["downloads"]["count"] == 0
        assert item["downloads"]["latest"] is None


# ======================================================================
# 6. 历史在、文件删：missing + 任务快照不变
# ======================================================================
def test_history_exists_file_missing(admin_client, manager, tmp_path,
                                     fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    fake_audio_factory(tags={"title": "晴天"})
    task = _make_task("netease", "12345", a, quality="exhigh", file_size=100)
    db.session.commit()
    _run(manager, manager.submit_scan().id)
    assert _row(a).status == "active"

    def _snapshots():
        return [(t.pk, t.quality, t.file_path, t.file_size, t.status)
                for t in DownloadTask.query.all()]

    before = _snapshots()

    a.unlink()
    _run(manager, manager.submit_scan().id)

    row = _row(a)
    assert row.status == "missing"
    # missing 行不出现在 duplicates（分组只取 active 行）
    assert _duplicates(admin_client) == []
    # DownloadTask 快照逐字段不变
    assert _snapshots() == before
    assert row.song_id == "12345"                   # 历史字段保留不清除


# ======================================================================
# 7. 全零技术参数：推荐理由不虚构
# ======================================================================
def test_missing_spec_params_recommendation():
    big = FileEntry(path=Path("/x/a-1.mp3"), size=2 * 1048576, mtime=100.0,
                    ext=".mp3", song_id="1")
    small = FileEntry(path=Path("/x/b-1.mp3"), size=1048576, mtime=50.0,
                      ext=".mp3", song_id="1")

    groups = group_duplicates([big, small])
    assert len(groups) == 1
    g = groups[0]
    assert g.recommended == str(big.path)          # 仍有推荐
    assert g.reason == "文件更大 2.0MB"            # size 轴，不虚构数值

    # 全轴并列 → 不挑不出差别
    twin = FileEntry(path=Path("/x/c-1.mp3"), size=1048576, mtime=50.0,
                     ext=".mp3", song_id="1")
    assert group_duplicates([small, twin])[0].reason == "规格相同，任选其一"

    # 仅 mtime 胜出
    newer = FileEntry(path=Path("/x/d-1.mp3"), size=1048576, mtime=90.0,
                      ext=".mp3", song_id="1")
    g3 = group_duplicates([small, newer])[0]
    assert g3.recommended == str(newer.path)
    assert g3.reason == "文件更新"


# ======================================================================
# 8. 组随扫描变化
# ======================================================================
def test_groups_change_after_rescan(admin_client, manager, tmp_path,
                                    fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    mp3 = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    fake_audio_factory(tags={"title": "晴天"})
    _make_task("netease", "12345", mp3)
    db.session.commit()

    _run(manager, manager.submit_scan().id)
    assert _duplicates(admin_client) == []          # 单文件不成组

    # 加一个同身份重复文件 → 确定组出现
    flac = _make_audio(out_dir, "晴天-12345.flac", size=1000)
    _make_task("netease", "12345", flac)
    db.session.commit()
    _run(manager, manager.submit_scan().id)
    groups = _duplicates(admin_client)
    assert len(groups) == 1
    assert groups[0]["group_type"] == "same_id"
    assert {Path(i["path"]).name for i in groups[0]["items"]} == {
        mp3.name, flac.name}

    # 删其一 → 再扫 → 组消失
    flac.unlink()
    _run(manager, manager.submit_scan().id)
    assert _duplicates(admin_client) == []


# ======================================================================
# 9. 全流程 DownloadTask 零写入
# ======================================================================
def test_download_snapshots_never_modified(admin_client, manager, tmp_path,
                                           fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    b = _make_audio(out_dir, "晴天-12345.flac", size=1000)
    c = _make_audio(out_dir, "same_x.mp3", payload=b"SAMEDATA" * 10)
    d = _make_audio(out_dir, "same_y.mp3", payload=b"SAMEDATA" * 10)
    fake_audio_factory(tags={"title": "晴天"})
    _make_task("netease", "12345", a, quality="exhigh", file_size=100)
    _make_task("netease", "12345", b, quality="lossless", file_size=1000)
    _make_task("netease", "99999", c, song_name="字节相同1", file_size=100)
    _make_task("qq", "8888", d, song_name="字节相同2", quality="standard",
               file_size=100)
    _make_task("netease", "7777", "", song_name="已删任务",
               status="failed", quality="", file_size=0)
    db.session.commit()

    def _snapshots():
        return [(t.pk, t.platform, t.song_id, t.status, t.quality,
                 t.file_path, t.file_size) for t in DownloadTask.query.all()]

    before = _snapshots()
    assert before                               # 快照样本非空，防空表假绿

    _run(manager, manager.submit_scan().id)
    groups = _duplicates(admin_client)
    assert len(groups) == 2                        # same_id + identical 均成组
    assert _snapshots() == before                  # 逐字段相等


# ======================================================================
# 10. 关联优先级：任务 > Song；无任务回退 Song
# ======================================================================
def test_association_priority_task_over_song(admin_client, manager, tmp_path,
                                             fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    both = _make_audio(out_dir, "both.mp3", size=100)
    only_song = _make_audio(out_dir, "only_song.mp3", size=100)
    fake_audio_factory(tags={"title": "t"})
    _make_task("netease", "T1", both, song_name="任务侧")
    db.session.add(Song(id="S1", platform="qq", name="Song侧",
                        file_path=str(both), status="success"))
    db.session.add(Song(id="S2", platform="kugou", name="仅Song",
                        file_path=str(only_song), status="success"))
    db.session.commit()

    _run(manager, manager.submit_scan().id)

    # 冲突：回填任务侧身份
    assert (_row(both).platform, _row(both).song_id) == ("netease", "T1")
    # 无任务：回退 Song 身份
    assert (_row(only_song).platform, _row(only_song).song_id) == ("kugou",
                                                                   "S2")


# ======================================================================
# 11. recommend_reason 轴序（纯函数）
# ======================================================================
def test_recommend_reason_axis_priority():
    flac_cd = FileEntry(path=Path("cd.flac"), lossless=True,
                        sample_rate=44100, bits_per_sample=16, size=50)
    mp3_320 = FileEntry(path=Path("320.mp3"), bitrate_kbps=320,
                        sample_rate=48000, size=100)
    flac_hires = FileEntry(path=Path("hires.flac"), lossless=True,
                           sample_rate=96000, bits_per_sample=16, size=10)
    flac_24bit = FileEntry(path=Path("24bit.flac"), lossless=True,
                           sample_rate=44100, bits_per_sample=24, size=10)

    # 第一轴（无损）唯一胜出 → 即使有损侧采样率更高也按第一轴输出
    assert recommend_reason(flac_cd, [flac_cd, mp3_320]) == "唯一无损版本"
    # 无损并列 → 采样率轴
    assert recommend_reason(flac_hires, [flac_hires, flac_cd]) == \
        "采样率更高 96kHz"
    # 采样率并列 → 位深轴
    assert recommend_reason(flac_24bit, [flac_24bit, flac_cd]) == \
        "位深更高 24bit"
    # 位深并列（均 0）→ 码率轴
    assert recommend_reason(mp3_320, [mp3_320,
                                      FileEntry(path=Path("l.mp3"),
                                                bitrate_kbps=128,
                                                sample_rate=48000,
                                                size=100)]) == "码率更高 320kbps"
    # 全部并列 / 单元素 → 兜底文案
    assert recommend_reason(mp3_320, [mp3_320]) == "规格相同，任选其一"
