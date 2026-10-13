"""ORG-03 文件清理后台任务与数据一致性 测试。

覆盖（对应任务简报 1-13 + 旧同步用例迁移）：
1.  test_clean_trash_repairs_song_and_keeps_quality
    mp3+flac 同曲清 mp3、repair 指 flac → item success、盘上仍在、
    Song 重指 flac 路径与 size、quality 不变、索引行 status 正确
2.  test_clean_keeps_other_group_members_untouched
    同组保留多个只删低规格 → 其余索引行/盘上文件不动
3.  test_clean_without_keep_clears_song_pointer
    组内全部被清且无 repair → Song.file_path=""、file_size=0（cleared）
4.  test_clean_missing_file_fails_item_and_continues
    清理前文件已被外部删除 → item failed、其余继续成功、job=partial
5.  test_clean_trash_conflict_records_dest
    回收站模式：.trash 实际文件存在于 trash_dest、item.trash_dest 记录、
    行 status="trashed"；同名冲突走时间戳后缀
5b. test_move_to_trash_timestamp_and_sequence_suffix
    _move_to_trash 单元：时间戳分支 + 同秒冲突 _N 序号分支（冻结时钟）
6.  test_clean_direct_mode_unlinks_marks_cleaned
    直删模式（organize_direct_delete="true"）→ 文件消失、行 "cleaned"、
    trash_dest NULL、result 计 deleted
7.  test_clean_partial_counts_in_result
    单文件失败 + 部分成功 → job=partial、result 计数正确
8.  test_clean_rows_not_active_afterwards
    清理后索引状态正确（trashed 行不再 active）
9.  test_clean_repoints_song_in_three_file_group
    三文件组删除两个、repair 都指 keep → Song 重指 keep
10. test_clean_never_touches_download_snapshots
    全流程后 DownloadTask 快照逐字段不变（零写入）
11. test_recover_interrupted_clean_preserves_pending_items
    中断识别：running clean job + pending items → interrupted，items 不被
    改写；重新 submit_clean 生成新 job（旧计划不复活）
12. test_rescan_after_clean_trashed_rows_and_restore
    清理后再扫描：trashed 行既不复活也不被标 missing；文件移回原位再扫
    → 行回归 active
13. test_submit_clean_busy_while_scan_active / test_api_clean_* 
    扫描运行中提交清理 → OrganizerBusyError；登录非管理员 POST → 403；
    job 详情含 items；state 含 last_clean
迁移（自 tests/test_organizer.py 旧同步端点用例，改后台任务契约）：
    test_clean_rejects_path_outside_output_dir（白名单：出目录）
    test_clean_rejects_trash_component_path（白名单：.trash 组件）
    test_clean_repair_invalid_keep_clears_song（保留文件失效 → 清空指针）
    test_clean_song_pointing_at_surviving_file_untouched（Song 指保留文件不动）
    test_submit_clean_validates_delete_paths / test_submit_clean_truncates_to_1000
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.organizer as organizer

import routes.api as api_module
from models import (
    DownloadTask,
    LocalAudioFile,
    OrganizerCleanItem,
    OrganizerJob,
    Setting,
    Song,
    User,
    db,
)
from organizer_manager import OrganizerBusyError, OrganizerManager


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
    """未启动线程的 OrganizerManager（submit/run 全部测试线程内同步执行）"""
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


@pytest.fixture
def member_client(webapp_app):
    """普通用户（非管理员）登录态的 client"""
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


# ======================================================================
# 助手
# ======================================================================
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


def _make_song(song_id, platform, file_path, quality="standard", file_size=100):
    s = Song(id=song_id, platform=platform, name="测试歌曲",
             quality=quality, file_path=str(file_path),
             file_size=file_size, status="success")
    db.session.add(s)
    return s


def _run(mgr, job_id):
    """同步执行并使外层测试 session 的缓存实体失效（不 expire_all 会打到
    identity map 旧值，先例见 test_organizer_jobs._run）"""
    mgr.run_job(job_id)
    db.session.expire_all()


def _scan(mgr):
    """同步跑一次扫描，返回 job 行"""
    job = mgr.submit_scan()
    _run(mgr, job.id)
    return db.session.get(OrganizerJob, job.id)


def _clean(mgr, paths, repair=None):
    """提交并同步执行一次清理，返回落库后的 job 行"""
    job = mgr.submit_clean(list(paths), repair)
    _run(mgr, job.id)
    return db.session.get(OrganizerJob, job.id)


def _items(job_id):
    return OrganizerCleanItem.query.filter_by(job_id=job_id)\
        .order_by(OrganizerCleanItem.id).all()


def _result(job):
    return json.loads(job.result)


def _row(path):
    return LocalAudioFile.query.filter_by(path=str(path)).first()


def _snapshots():
    return [(t.pk, t.platform, t.song_id, t.status, t.quality,
             t.file_path, t.file_size) for t in DownloadTask.query.all()]


# ======================================================================
# submit_clean：入队契约 / 参数校验 / 互斥
# ======================================================================
def test_submit_clean_creates_pending_job_with_items(webapp_app, tmp_path):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3")
    b = _make_audio(out_dir, "晴天-12345.flac")
    mgr = OrganizerManager(app)

    job = mgr.submit_clean([str(a), str(b)], {str(a): str(b)})

    assert job.id is not None
    assert job.status == "pending"
    assert job.job_type == "clean"
    row = db.session.get(OrganizerJob, job.id)
    assert row is not None and row.status == "pending"
    items = _items(job.id)
    assert [i.path for i in items] == [str(a), str(b)]
    assert items[0].keep_path == str(b)             # repair 计划随 item 落库
    assert items[1].keep_path is None
    assert all(i.status == "pending" for i in items)
    assert all(i.trash_dest is None for i in items)


def test_submit_clean_validates_delete_paths(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    mgr = OrganizerManager(app)

    for bad in (None, [], "a.mp3", ["a.mp3", 123]):
        with pytest.raises(ValueError, match="缺少 delete_paths 列表"):
            mgr.submit_clean(bad)

    # repair 非 dict 一律按 {} 处理（不报错）
    out_dir = Path(Setting.get("output_dir"))
    f = _make_audio(out_dir, "a.mp3")
    job = mgr.submit_clean([str(f)], repair="oops")
    assert _items(job.id)[0].keep_path is None


def test_submit_clean_truncates_to_1000(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    mgr = OrganizerManager(app)
    paths = [f"/nowhere/{i}.mp3" for i in range(1002)]

    job = mgr.submit_clean(paths)

    assert len(_items(job.id)) == 1000              # 与旧同步端点一致截断


def test_submit_clean_busy_while_scan_active(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    mgr = OrganizerManager(app)
    scan_job = mgr.submit_scan()

    with pytest.raises(OrganizerBusyError):
        mgr.submit_clean(["/x/a.mp3"])              # 扫描在途 → 清理拒绝

    # 反向同样互斥：清理在途 → 扫描拒绝
    _run(mgr, scan_job.id)
    clean_job = mgr.submit_clean(["/x/a.mp3"])
    with pytest.raises(OrganizerBusyError):
        mgr.submit_scan()
    _run(mgr, clean_job.id)


# ======================================================================
# 1. trash 模式 + repair：Song 重指、quality 不动
# ======================================================================
def test_clean_trash_repairs_song_and_keeps_quality(manager, tmp_path,
                                                    fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    mp3 = _make_audio(out_dir, "晴天-12345.mp3", size=111)
    flac = _make_audio(out_dir, "晴天-12345.flac", size=222)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)
    _make_song("12345", "netease", mp3, quality="standard", file_size=111)
    db.session.commit()

    job = _clean(manager, [str(mp3)], {str(mp3): str(flac)})

    assert job.status == "success"
    assert job.progress == 100
    result = _result(job)
    assert result["total"] == 1
    assert result["trashed"] == 1 and result["deleted"] == 0
    assert result["failed"] == 0
    assert result["songs_repaired"] == 1 and result["songs_cleared"] == 0
    rep = result["repaired"][0]
    assert rep["song"] == "netease/12345"
    assert Path(rep["from"]).resolve() == mp3.resolve()
    assert Path(rep["to"]).resolve() == flac.resolve()

    # mp3 已入回收站，flac 盘上还在
    assert not mp3.exists()
    assert flac.exists()

    # Song 重指保留文件、size 更新；quality（交付档位）刻意不动
    fresh = Song.query.filter_by(id="12345", platform="netease").first()
    assert Path(fresh.file_path).resolve() == flac.resolve()
    assert fresh.file_size == 222
    assert fresh.quality == "standard"

    # 计划行与索引行状态
    item = _items(job.id)[0]
    assert item.status == "success"
    assert item.error_msg is None
    assert Path(item.trash_dest) == out_dir / ".trash" / "晴天-12345.mp3"
    assert item.finished_at is not None
    assert _row(mp3).status == "trashed"
    assert _row(flac).status == "active"


# ======================================================================
# 2. 同组保留多个、只删低规格：其余不动
# ======================================================================
def test_clean_keeps_other_group_members_untouched(manager, tmp_path,
                                                   fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    low = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    mid = _make_audio(out_dir, "晴天-12345_320.mp3", size=300)
    keep = _make_audio(out_dir, "晴天-12345.flac", size=900)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)

    job = _clean(manager, [str(low)], {str(low): str(keep)})

    assert job.status == "success"
    assert not low.exists()
    assert mid.exists() and keep.exists()           # 其余文件盘上不动
    assert _row(mid).status == "active"
    assert _row(keep).status == "active"
    assert _row(mid).size == 300 and _row(keep).size == 900
    fresh = Song.query.filter_by(id="12345").first()
    assert fresh is None or Path(fresh.file_path).resolve() not in (
        low.resolve(),)                             # 无 Song 指向被删文件则零改动


# ======================================================================
# 3. 组内全部被清、无 repair → 清空 Song 指针
# ======================================================================
def test_clean_without_keep_clears_song_pointer(manager, tmp_path,
                                                fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    mp3 = _make_audio(out_dir, "晴天-12345.mp3", size=111)
    flac = _make_audio(out_dir, "晴天-12345.flac", size=222)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)
    _make_song("12345", "netease", mp3, quality="lossless", file_size=111)
    db.session.commit()

    job = _clean(manager, [str(mp3), str(flac)])

    assert job.status == "success"
    result = _result(job)
    assert result["trashed"] == 2
    assert result["songs_cleared"] == 1 and result["songs_repaired"] == 0
    cleared = result["cleared"][0]
    assert cleared["song"] == "netease/12345"
    assert Path(cleared["path"]).resolve() == mp3.resolve()

    fresh = Song.query.filter_by(id="12345", platform="netease").first()
    assert fresh.file_path == ""                    # 表达文件不可用
    assert fresh.file_size == 0
    assert fresh.quality == "lossless"              # quality 依旧不动


# ======================================================================
# 4. 清理前文件已被外部删除 → failed 且继续其余
# ======================================================================
def test_clean_missing_file_fails_item_and_continues(manager, tmp_path,
                                                     fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    b = _make_audio(out_dir, "七里香-678.flac", size=200)
    gone = _make_audio(out_dir, "龙卷风-999.mp3", size=50)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)
    gone.unlink()                                   # 提交前被外部删除

    job = _clean(manager, [str(gone), str(a), str(b)])

    assert job.status == "partial"
    items = _items(job.id)
    assert items[0].status == "failed"
    assert "不存在" in (items[0].error_msg or "")   # 校验原因含不存在类信息
    assert items[0].trash_dest is None
    assert items[1].status == "success" and items[2].status == "success"
    assert not a.exists() and not b.exists()
    result = _result(job)
    assert result["failed"] == 1 and result["trashed"] == 2
    assert result["total"] == 3


# ======================================================================
# 5. trash_dest 可追溯 + 同名冲突时间戳后缀（端到端）
# ======================================================================
def test_clean_trash_conflict_records_dest(manager, tmp_path,
                                           fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    trash = out_dir / ".trash"
    trash.mkdir()
    (trash / "a.mp3").write_bytes(b"old")
    dup = _make_audio(out_dir, "a.mp3", payload=b"new")
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)

    job = _clean(manager, [str(dup)])

    assert job.status == "success"
    item = _items(job.id)[0]
    assert item.status == "success"
    assert item.trash_dest is not None
    assert re.fullmatch(r"a_\d{14}\.mp3", Path(item.trash_dest).name)
    # 实际落点存在且内容为新文件；原回收站同名文件不被覆盖
    assert Path(item.trash_dest).read_bytes() == b"new"
    assert (trash / "a.mp3").read_bytes() == b"old"
    assert not dup.exists()
    assert _row(dup).status == "trashed"


# ======================================================================
# 5b. _move_to_trash 单元：时间戳分支 + 同秒冲突 _N 序号分支
# ======================================================================
def test_move_to_trash_timestamp_and_sequence_suffix(webapp_app, tmp_path,
                                                     monkeypatch):
    """冻结时钟到同一秒：无冲突走原名、同名走 _YYYYmmddHHMMSS、
    同秒再冲突走 _N 序号（保证回收站既有文件不被覆盖）"""
    from routes.api import _move_to_trash

    out_dir = _setup_out_dir(tmp_path)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 12, 30, 0)

    monkeypatch.setattr(api_module, "datetime", _Frozen)

    s1 = _make_audio(out_dir, "s1/a.mp3", payload=b"1")
    s2 = _make_audio(out_dir, "s2/a.mp3", payload=b"2")
    s3 = _make_audio(out_dir, "s3/a.mp3", payload=b"3")

    d1 = _move_to_trash(s1, out_dir)
    assert d1 == out_dir / ".trash" / "a.mp3"       # 无冲突：原名
    d2 = _move_to_trash(s2, out_dir)
    assert d2.name == "a_20260101123000.mp3"        # 时间戳分支
    d3 = _move_to_trash(s3, out_dir)
    assert d3.name == "a_20260101123000_1.mp3"      # 同秒再冲突：序号分支
    for d, payload in ((d1, b"1"), (d2, b"2"), (d3, b"3")):
        assert d.read_bytes() == payload


# ======================================================================
# 6. 直删模式：unlink、行 cleaned、trash_dest NULL
# ======================================================================
def test_clean_direct_mode_unlinks_marks_cleaned(manager, tmp_path,
                                                 fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    Setting.set("organize_direct_delete", "true")
    dup = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)

    job = _clean(manager, [str(dup)])

    assert job.status == "success"
    assert not dup.exists()
    assert not (out_dir / ".trash").exists()        # 不产生回收站目录
    item = _items(job.id)[0]
    assert item.status == "success"
    assert item.trash_dest is None                  # 直删无回收站落点
    assert item.finished_at is not None
    assert _row(dup).status == "cleaned"
    result = _result(job)
    assert result["deleted"] == 1 and result["trashed"] == 0


# ======================================================================
# 7. 部分成功 → partial，result 计数正确
# ======================================================================
def test_clean_partial_counts_in_result(manager, tmp_path,
                                        fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "a-1.mp3", size=10)
    b = _make_audio(out_dir, "b-2.mp3", size=20)
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)

    job = _clean(manager, [str(a), "/nowhere/missing.mp3", str(b)])

    assert job.status == "partial"
    assert job.error_msg is None                    # 仅全失败才写 error_msg
    result = _result(job)
    assert result["total"] == 3
    assert result["trashed"] == 2
    assert result["deleted"] == 0
    assert result["failed"] == 1
    assert job.processed == 3 and job.total == 3
    assert job.progress == 100


# ======================================================================
# 8. 清理后索引状态正确
# ======================================================================
def test_clean_rows_not_active_afterwards(manager, tmp_path,
                                          fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "a-1.mp3", size=10)
    b = _make_audio(out_dir, "b-2.mp3", size=20)
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)
    assert LocalAudioFile.query.filter_by(status="active").count() == 2

    job = _clean(manager, [str(a), str(b)])

    assert job.status == "success"
    statuses = {r.status for r in LocalAudioFile.query.all()}
    assert statuses == {"trashed"}                  # 无残留 active 行
    assert LocalAudioFile.query.filter_by(status="active").count() == 0


# ======================================================================
# 9. 三文件组：删除两个、repair 都指 keep → Song 重指 keep
# ======================================================================
def test_clean_repoints_song_in_three_file_group(manager, tmp_path,
                                                 fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    low = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    mid = _make_audio(out_dir, "晴天-12345_320.mp3", size=300)
    keep = _make_audio(out_dir, "晴天-12345.flac", size=999)
    fake_audio_factory(tags={"title": "晴天"})
    _scan(manager)
    _make_song("12345", "netease", low, quality="standard", file_size=100)
    db.session.commit()

    job = _clean(manager, [str(low), str(mid)],
                 {str(low): str(keep), str(mid): str(keep)})

    assert job.status == "success"
    result = _result(job)
    assert result["trashed"] == 2 and result["failed"] == 0
    assert result["songs_repaired"] == 1
    fresh = Song.query.filter_by(id="12345", platform="netease").first()
    assert Path(fresh.file_path).resolve() == keep.resolve()
    assert fresh.file_size == 999
    assert fresh.quality == "standard"
    assert not low.exists() and not mid.exists()
    assert keep.exists()
    assert _row(low).status == "trashed" and _row(mid).status == "trashed"
    assert _row(keep).status == "active"


# ======================================================================
# 10. 全流程 DownloadTask 快照零写入
# ======================================================================
def test_clean_never_touches_download_snapshots(manager, tmp_path,
                                                fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    mp3 = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    flac = _make_audio(out_dir, "晴天-12345.flac", size=1000)
    other = _make_audio(out_dir, "其他-777.mp3", size=50)
    fake_audio_factory(tags={"title": "t"})
    t1 = DownloadTask(platform="netease", song_id="12345", song_name="低规格",
                      status="done", quality="standard",
                      file_path=str(mp3), file_size=100,
                      created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    t2 = DownloadTask(platform="netease", song_id="12345", song_name="无损",
                      status="done", quality="lossless",
                      file_path=str(flac), file_size=1000,
                      created_at=datetime(2026, 2, 1), updated_at=datetime(2026, 2, 1))
    t3 = DownloadTask(platform="qq", song_id="8888", song_name="无关任务",
                      status="done", quality="exhigh",
                      file_path=str(other), file_size=50,
                      created_at=datetime(2026, 3, 1), updated_at=datetime(2026, 3, 1))
    db.session.add_all([t1, t2, t3])
    _make_song("12345", "netease", mp3, quality="standard", file_size=100)
    db.session.commit()

    before = _snapshots()
    assert before                                   # 快照样本非空，防空表假绿

    _scan(manager)
    job = _clean(manager, [str(mp3)], {str(mp3): str(flac)})
    assert job.status == "success"

    # 扫描 + 清理 + repair 全流程后快照逐字段不变
    assert _snapshots() == before


# ======================================================================
# 11. 中断识别：pending 计划不被改写，旧计划不复活
# ======================================================================
def test_recover_interrupted_clean_preserves_pending_items(webapp_app,
                                                           tmp_path):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    f = _make_audio(out_dir, "a.mp3")
    mgr = OrganizerManager(app)

    stuck = OrganizerJob(job_type="clean", status="running", progress=40)
    db.session.add(stuck)
    db.session.flush()
    old_items = [
        OrganizerCleanItem(job_id=stuck.id, path=str(f), status="pending"),
        OrganizerCleanItem(job_id=stuck.id, path="/x/gone.mp3", status="pending"),
    ]
    db.session.add_all(old_items)
    db.session.commit()
    old_ids = [i.id for i in old_items]

    mgr._recover_interrupted()

    row = db.session.get(OrganizerJob, stuck.id)
    assert row.status == "interrupted"
    assert row.finished_at is not None
    items = [db.session.get(OrganizerCleanItem, i) for i in old_ids]
    assert all(i.status == "pending" for i in items)   # pending 计划不被改写
    assert all(i.trash_dest is None for i in items)
    assert f.exists()                                   # 未被执行

    # 重新提交生成新 job 新计划；旧 job 的计划不复活
    new_job = mgr.submit_clean([str(f)])
    assert new_job.id != stuck.id
    _run(mgr, new_job.id)
    assert db.session.get(OrganizerJob, new_job.id).status == "success"
    assert [db.session.get(OrganizerCleanItem, i).status for i in old_ids] \
        == ["pending", "pending"]
    assert len(_items(new_job.id)) == 1
    assert _items(new_job.id)[0].status == "success"    # 只执行新计划
    assert db.session.get(OrganizerJob, stuck.id).status == "interrupted"


# ======================================================================
# 12. 清理后再扫描：不复活、不标 missing；移回原位 → 回归 active
# ======================================================================
def test_rescan_after_clean_trashed_rows_and_restore(manager, tmp_path,
                                                     fake_audio_factory):
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    b = _make_audio(out_dir, "七里香-678.flac", size=200)
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)
    mtime = a.stat().st_mtime                       # 清理前留档，移回后还原
    trash_dest = out_dir / ".trash" / "晴天-12345.mp3"

    _clean(manager, [str(a)])
    assert _row(a).status == "trashed"

    # 再扫描：trashed 行既不复活也不被标 missing（.trash 在 walk 中剪枝）
    _scan(manager)
    assert _row(a).status == "trashed"
    assert _row(b).status == "active"

    # 把 .trash 里的文件手工移回原位（保字节与 mtime）再扫 → 回归 active
    payload = trash_dest.read_bytes()
    trash_dest.replace(a)
    os.utime(a, (mtime, mtime))
    assert a.read_bytes() == payload
    _scan(manager)

    row = _row(a)
    assert row.status == "active"                   # size+mtime 命中复用回归
    assert LocalAudioFile.query.filter_by(path=str(a)).count() == 1  # 复用既有行


# ======================================================================
# 迁移：白名单（出目录 / .trash 组件）——后台任务契约
# ======================================================================
def test_clean_rejects_path_outside_output_dir(manager, tmp_path):
    _setup_out_dir(tmp_path)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")

    job = _clean(manager, [str(outside)])

    item = _items(job.id)[0]
    assert item.status == "failed"
    assert "不在下载目录内" in (item.error_msg or "")
    assert item.trash_dest is None
    assert outside.exists()                         # 文件仍在
    assert job.status == "failed"                   # 全失败 → job failed
    assert "不在下载目录内" in (job.error_msg or "")


def test_clean_rejects_trash_component_path(manager, tmp_path):
    """回收站里的文件视为已删除，不可再次清理"""
    out_dir = _setup_out_dir(tmp_path)
    trash_file = out_dir / ".trash" / "already.mp3"
    trash_file.parent.mkdir()
    trash_file.write_bytes(b"T")

    job = _clean(manager, [str(trash_file)])

    item = _items(job.id)[0]
    assert item.status == "failed"
    assert ".trash" in (item.error_msg or "")
    assert trash_file.exists()
    assert job.status == "failed"


# ======================================================================
# 迁移：repair 保留文件失效 / Song 本就指向保留文件
# ======================================================================
def test_clean_repair_invalid_keep_clears_song(manager, tmp_path,
                                               fake_audio_factory):
    """repair 保留文件复检不通过（不存在）→ 按无可保留文件处理：
    Song 指针清空（不再留指向已删文件的悬空引用，ORG-03 数据一致性语义）"""
    out_dir = _setup_out_dir(tmp_path)
    dup = _make_audio(out_dir, "a.mp3", size=1)
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)
    _make_song("1", "netease", dup, quality="standard", file_size=1)
    db.session.commit()

    job = _clean(manager, [str(dup)], {str(dup): str(out_dir / "gone.flac")})

    assert job.status == "success"
    result = _result(job)
    assert result["songs_repaired"] == 0
    assert result["songs_cleared"] == 1
    fresh = Song.query.filter_by(id="1").first()
    assert fresh.file_path == ""
    assert fresh.file_size == 0


def test_clean_song_pointing_at_surviving_file_untouched(manager, tmp_path,
                                                         fake_audio_factory):
    """Song 指向的文件未被删（指向保留文件）→ 不在匹配集，Song 不动"""
    out_dir = _setup_out_dir(tmp_path)
    keep = _make_audio(out_dir, "keep.flac", size=10)
    dup = _make_audio(out_dir, "dup.mp3", size=5)
    fake_audio_factory(tags={"title": "t"})
    _scan(manager)
    _make_song("2", "netease", keep, quality="lossless", file_size=10)
    db.session.commit()

    job = _clean(manager, [str(dup)], {str(dup): str(keep)})

    assert job.status == "success"
    result = _result(job)
    assert result["songs_repaired"] == 0
    assert result["songs_cleared"] == 0
    fresh = Song.query.filter_by(id="2").first()
    assert Path(fresh.file_path).resolve() == keep.resolve()
    assert fresh.file_size == 10


# ======================================================================
# 13. API 层：端点契约 / 权限 / job items / state last_clean
# ======================================================================
def test_api_clean_returns_job_id(admin_client, tmp_path, fake_audio_factory):
    client = admin_client
    out_dir = _setup_out_dir(tmp_path)
    f = _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "t"})

    resp = client.post("/api/organize/clean",
                       json={"delete_paths": [str(f)]})
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["status"] == "pending"
    row = db.session.get(OrganizerJob, body["data"]["job_id"])
    assert row is not None and row.job_type == "clean"

    # 执行完（释放互斥）后再验证参数校验：缺少 delete_paths → code:1
    _run(client.application.config["ORGANIZER_MANAGER"], body["data"]["job_id"])
    for payload in ({}, {"delete_paths": []}, {"delete_paths": "x"},
                    {"delete_paths": ["a", 1]}):
        resp = client.post("/api/organize/clean", json=payload)
        assert resp.get_json()["code"] == 1
        assert "delete_paths" in resp.get_json()["msg"]


def test_api_clean_busy_and_missing_dir(admin_client, tmp_path,
                                        fake_audio_factory):
    client = admin_client
    out_dir = _setup_out_dir(tmp_path)
    f = _make_audio(out_dir, "a.mp3")
    fake_audio_factory(tags={"title": "t"})

    # 扫描在途 → 清理被拒
    scan_id = client.post("/api/organize/scan").get_json()["data"]["job_id"]
    body = client.post("/api/organize/clean",
                       json={"delete_paths": [str(f)]}).get_json()
    assert body["code"] == 1
    assert "正在执行" in body["msg"]
    _run(client.application.config["ORGANIZER_MANAGER"], scan_id)

    # 下载目录不存在 → 提交成功但执行失败（与 scan 端点前置校验不同）
    Setting.set("output_dir", str(tmp_path / "nope"))
    resp = client.post("/api/organize/clean", json={"delete_paths": [str(f)]})
    assert resp.get_json()["code"] == 0
    mgr = client.application.config["ORGANIZER_MANAGER"]
    jid = resp.get_json()["data"]["job_id"]
    _run(mgr, jid)
    job = db.session.get(OrganizerJob, jid)
    assert job.status == "failed"
    assert "下载目录不存在" in (job.error_msg or "")


def test_api_clean_requires_admin(member_client, tmp_path):
    out_dir = _setup_out_dir(tmp_path)
    f = _make_audio(out_dir, "a.mp3")

    resp = member_client.post("/api/organize/clean",
                              json={"delete_paths": [str(f)]})
    assert resp.status_code == 403
    assert f.exists()
    assert OrganizerJob.query.count() == 0


def test_api_clean_without_manager(admin_client, tmp_path):
    _setup_out_dir(tmp_path)
    client = admin_client
    client.application.config.pop("ORGANIZER_MANAGER", None)

    body = client.post("/api/organize/clean",
                       json={"delete_paths": ["/x/a.mp3"]}).get_json()

    assert body == {"code": 1, "msg": "整理服务未初始化"}


def test_api_clean_job_detail_contains_items(admin_client, tmp_path,
                                             fake_audio_factory):
    client = admin_client
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    b = _make_audio(out_dir, "晴天-12345.flac", size=200)
    fake_audio_factory(tags={"title": "t"})
    mgr = client.application.config["ORGANIZER_MANAGER"]
    _run(mgr, mgr.submit_scan().id)

    jid = client.post("/api/organize/clean", json={
        "delete_paths": [str(a), str(out_dir / "gone.mp3")],
        "repair": {str(a): str(b)},
    }).get_json()["data"]["job_id"]
    _run(mgr, jid)

    resp = client.get(f"/api/organize/jobs/{jid}")
    assert resp.status_code == 200
    job = resp.get_json()["data"]["job"]
    assert job["job_type"] == "clean"
    assert job["status"] == "partial"
    items = job["items"]
    assert len(items) == 2
    assert items[0]["path"] == str(a)
    assert items[0]["keep_path"] == str(b)
    assert items[0]["status"] == "success"
    assert items[0]["trash_dest"].endswith(".trash/晴天-12345.mp3")
    assert items[0]["finished_at"] is not None
    assert items[1]["status"] == "failed"
    assert "不存在" in items[1]["error_msg"]
    assert items[1]["trash_dest"] is None
    assert job["result"]["trashed"] == 1 and job["result"]["failed"] == 1

    # scan job 无 items 键（不内联计划行）
    scan = client.get("/api/organize/jobs/1").get_json()["data"]["job"]
    assert scan["job_type"] == "scan"
    assert "items" not in scan


def test_api_state_contains_last_clean(admin_client, tmp_path,
                                       fake_audio_factory):
    client = admin_client
    out_dir = _setup_out_dir(tmp_path)
    f = _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "t"})
    mgr = client.application.config["ORGANIZER_MANAGER"]

    # 无清理历史时键存在且为 null
    state = client.get("/api/organize/state").get_json()["data"]
    assert "last_clean" in state
    assert state["last_clean"] is None

    jid = client.post("/api/organize/clean",
                      json={"delete_paths": [str(f)]}).get_json()["data"]["job_id"]
    _run(mgr, jid)

    state = client.get("/api/organize/state").get_json()["data"]
    last_clean = state["last_clean"]
    assert last_clean is not None
    assert last_clean["id"] == jid
    assert last_clean["status"] == "success"
    assert last_clean["finished_at"] is not None
    assert last_clean["result"]["trashed"] == 1
    assert state["last_scan"] is None               # clean 不串位到 last_scan
