"""ORG-01 音乐扫描后台任务化 + 本地文件索引持久化 测试。

覆盖（对应任务简报 a-k）：
a.  submit_scan 入队即返回 pending job（不启动线程）
b.  run_job 同步执行成功：终态 / 进度 / result 摘要正确
c.  二次扫描不重复建行（行集合不变，摘要计 reused）
d.  未变化文件（size+mtime 相同）复用索引：manager 路径零元数据读取
e.  文件内容/size 变化后重提取：新 title 生效、content_hash 清空
f.  外部删除文件后重扫 → missing，其余行保持 active
g.  扫描异常 → job failed，既有 active 行不被标 missing、行数据不变
h.  重启识别：插入 running/pending job → _recover_interrupted → interrupted
i.  存在 pending/running job 时重复提交 → OrganizerBusyError
j.  真线程端到端（文件 SQLite + 自建 app）+ stop() 干净退出
k.  API 层：scan / jobs / state / files / duplicates + 权限 + manager 未配置
"""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.organizer as organizer
from core.organizer import (
    AUDIO_EXTS,
    LOSSLESS_EXTS,
    FileEntry,
    extract_song_id,
)

from models import LocalAudioFile, OrganizerJob, Setting, User, db
from organizer_manager import OrganizerBusyError, OrganizerManager


# ======================================================================
# mutagen 桩（手法与 test_organizer.py 一致）：core.organizer.MutagenFile
# 调用点 monkeypatch，控制参数/标签/异常
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


def _setup_out_dir(tmp_path):
    """把下载目录指向 tmp_path/dl 并创建，返回目录 Path"""
    out_dir = tmp_path / "dl"
    out_dir.mkdir()
    Setting.set("output_dir", str(out_dir))
    return out_dir


def _make_audio(out_dir, name, size=100):
    p = out_dir / name
    p.write_bytes(b"x" * size)
    return p


def _ids():
    return sorted(r.id for r in LocalAudioFile.query.all())


def _run(manager, job_id):
    """同步执行并使外层测试 session 的缓存实体失效。

    webapp_app 在整个测试期间持有一个长驻 app context（独立 session），而
    run_job 的嵌套 app context 用另一个 session 落库；不 expire_all 的话，
    identity map 会把先读过的实体旧属性原样返回，断言打不到真实落库值。
    """
    manager.run_job(job_id)
    db.session.expire_all()


# ======================================================================
# a. submit_scan：不入队线程也立即返回 pending job
# ======================================================================
def test_submit_scan_creates_pending_job_without_worker(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    mgr = OrganizerManager(app)

    job = mgr.submit_scan()

    assert job.id is not None
    assert job.status == "pending"
    assert job.job_type == "scan"
    row = db.session.get(OrganizerJob, job.id)
    assert row is not None and row.status == "pending"


def test_submit_scan_missing_dir_raises_value_error(webapp_app, tmp_path):
    (app,) = webapp_app
    Setting.set("output_dir", str(tmp_path / "nope"))
    mgr = OrganizerManager(app)

    with pytest.raises(ValueError, match="下载目录不存在"):
        mgr.submit_scan()


# ======================================================================
# b. run_job 同步执行成功
# ======================================================================
def test_run_job_scan_success(webapp_app, tmp_path, fake_audio_factory):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3", size=100)
    _make_audio(out_dir, "七里香-678.flac", size=500)
    fake_audio_factory(
        info=FakeInfo(length=200.0, bitrate=320000, sample_rate=44100,
                      channels=2, bits_per_sample=16),
        tags={"title": "晴天", "artist": "周杰伦", "album": "叶惠美"},
    )
    mgr = OrganizerManager(app)
    job = mgr.submit_scan()

    _run(mgr, job.id)

    row = db.session.get(OrganizerJob, job.id)
    assert row.status == "success"
    assert row.progress == 100
    assert row.processed == row.total == 2
    assert row.started_at is not None
    assert row.finished_at is not None

    summary = json.loads(row.result)
    assert summary["scanned"] == 2
    assert summary["new"] == 2
    assert summary["updated"] == 0
    assert summary["reused"] == 0
    assert summary["missing"] == 0
    assert summary["groups"] == {"same_id": 0, "identical": 0}
    assert summary["duplicate_files"] == 0
    assert summary["duration_s"] >= 0

    rows = LocalAudioFile.query.all()
    assert len(rows) == 2
    by_ext = {r.ext: r for r in rows}
    assert by_ext[".mp3"].song_id == "12345"
    assert by_ext[".mp3"].title == "晴天"
    assert by_ext[".mp3"].duration_ms == 200000
    assert by_ext[".mp3"].bitrate_kbps == 320
    assert by_ext[".mp3"].lossless is False
    assert by_ext[".flac"].lossless is True
    assert by_ext[".flac"].song_id == "678"
    assert all(r.status == "active" for r in rows)
    assert all(r.path.startswith(str(out_dir)) for r in rows)


# ======================================================================
# c. 二次扫描不重复建行
# ======================================================================
def test_second_scan_does_not_duplicate_rows(webapp_app, tmp_path,
                                             fake_audio_factory):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    _make_audio(out_dir, "七里香-678.flac")
    fake_audio_factory(tags={"title": "晴天"})
    mgr = OrganizerManager(app)
    job1 = mgr.submit_scan()
    _run(mgr, job1.id)
    ids_first = _ids()

    job2 = mgr.submit_scan()
    _run(mgr, job2.id)

    assert _ids() == ids_first                     # 行数与 id 集合不变
    summary = json.loads(db.session.get(OrganizerJob, job2.id).result)
    assert summary["new"] == 0
    assert summary["updated"] == 0
    assert summary["reused"] == 2


# ======================================================================
# d. 未变化文件复用索引：manager 路径零元数据读取
# ======================================================================
def test_rescan_unchanged_files_reuse_index(webapp_app, tmp_path,
                                            fake_audio_factory, monkeypatch):
    """scan_directory 自身每次全量读元数据（既有契约）；本测试验证 manager
    的增量复用判定只依赖 size+mtime：以 stat-only entries 替换 scan_directory
    后重扫，全程 0 次 MutagenFile 调用、行全部按 reused 处理。"""
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    _make_audio(out_dir, "七里香-678.flac")
    fake_audio_factory(tags={"title": "晴天"})
    mgr = OrganizerManager(app)
    job1 = mgr.submit_scan()
    _run(mgr, job1.id)
    ids_first = _ids()

    calls: list[str] = []

    def _counting_fake(_p):
        calls.append(str(_p))
        return SimpleNamespace(info=FakeInfo(), tags={})

    monkeypatch.setattr(organizer, "MutagenFile", _counting_fake)

    def _stat_entries(root):
        """不经 mutagen 的 stat-only 扫描（仅覆盖本测试的平铺目录场景）"""
        entries = []
        for p in sorted(Path(root).iterdir()):
            ext = p.suffix.lower()
            if ext not in AUDIO_EXTS:
                continue
            st = p.stat()
            entries.append(FileEntry(
                path=p, size=st.st_size, mtime=st.st_mtime, ext=ext,
                song_id=extract_song_id(p.stem), lossless=ext in LOSSLESS_EXTS,
            ))
        return entries

    import organizer_manager as om
    monkeypatch.setattr(om, "scan_directory", _stat_entries)

    job2 = mgr.submit_scan()
    _run(mgr, job2.id)

    assert calls == []                             # 复用路径零元数据读取
    assert _ids() == ids_first
    summary = json.loads(db.session.get(OrganizerJob, job2.id).result)
    assert summary["reused"] == 2
    assert summary["new"] == 0 and summary["updated"] == 0


# ======================================================================
# e. 文件变化后重提取 + content_hash 清空
# ======================================================================
def test_changed_file_reextracts_metadata_and_clears_hash(webapp_app, tmp_path,
                                                          fake_audio_factory):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "a.mp3", size=80)
    b = _make_audio(out_dir, "b.mp3", size=80)
    a.write_bytes(b"SAMEDATA" * 10)
    b.write_bytes(b"SAMEDATA" * 10)
    fake_audio_factory(tags={"title": "旧标题"})
    mgr = OrganizerManager(app)
    job1 = mgr.submit_scan()
    _run(mgr, job1.id)

    # 首扫：a/b 同 size 且字节级相同 → identical 组 → content_hash 已回写
    ra = LocalAudioFile.query.filter_by(path=str(a)).first()
    rb = LocalAudioFile.query.filter_by(path=str(b)).first()
    assert ra.content_hash is not None
    assert rb.content_hash == ra.content_hash

    # a 内容与 size 变化 + 新 title
    a.write_bytes(b"NEW-DATA" * 20)
    fake_audio_factory(tags={"title": "新标题"})
    job2 = mgr.submit_scan()
    _run(mgr, job2.id)

    ra = LocalAudioFile.query.filter_by(path=str(a)).first()
    rb = LocalAudioFile.query.filter_by(path=str(b)).first()
    assert ra.title == "新标题"                     # 重新提取元数据
    assert ra.content_hash is None                  # 变化行哈希清空
    assert ra.size == 160
    assert ra.status == "active"
    assert rb.title == "旧标题"                     # 未变化行不受影响
    assert rb.content_hash is not None              # 未变化行哈希保留
    summary = json.loads(db.session.get(OrganizerJob, job2.id).result)
    assert summary["updated"] == 1 and summary["reused"] == 1


# ======================================================================
# f. 外部删除文件 → missing
# ======================================================================
def test_deleted_file_marked_missing_others_active(webapp_app, tmp_path,
                                                   fake_audio_factory):
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3")
    b = _make_audio(out_dir, "七里香-678.flac")
    fake_audio_factory(tags={"title": "晴天"})
    mgr = OrganizerManager(app)
    job1 = mgr.submit_scan()
    _run(mgr, job1.id)

    a.unlink()
    job2 = mgr.submit_scan()
    _run(mgr, job2.id)

    ra = LocalAudioFile.query.filter_by(path=str(a)).first()
    rb = LocalAudioFile.query.filter_by(path=str(b)).first()
    assert ra.status == "missing"
    assert ra.title == "晴天"                       # 其余字段保留
    assert ra.song_id == "12345"
    assert rb.status == "active"
    summary = json.loads(db.session.get(OrganizerJob, job2.id).result)
    assert summary["missing"] == 1


def test_missing_file_restored_returns_active(webapp_app, tmp_path,
                                              fake_audio_factory):
    """删→missing→原样移回（size+mtime 不变，复用命中）→状态必须回归 active。

    回归背景：复用分支曾只刷 last_scan_job_id 不写 status，文件移走又原样
    移回后行永久停留 missing，索引与磁盘相反。"""
    (app,) = webapp_app
    out_dir = _setup_out_dir(tmp_path)
    a = _make_audio(out_dir, "晴天-12345.mp3", size=100)
    fake_audio_factory(tags={"title": "晴天"})
    mgr = OrganizerManager(app)
    _run(mgr, mgr.submit_scan().id)
    rid = LocalAudioFile.query.filter_by(path=str(a)).first().id
    assert LocalAudioFile.query.filter_by(path=str(a)).first().status == "active"

    # 外部删除 → missing
    payload = a.read_bytes()
    mtime = a.stat().st_mtime
    a.unlink()
    _run(mgr, mgr.submit_scan().id)
    assert LocalAudioFile.query.filter_by(path=str(a)).first().status == "missing"

    # 原字节恢复 + os.utime 还原 mtime：size+mtime 均不变 → 复用命中
    a.write_bytes(payload)
    os.utime(a, (mtime, mtime))
    _run(mgr, mgr.submit_scan().id)

    row = LocalAudioFile.query.filter_by(path=str(a)).first()
    assert row.status == "active"
    assert row.id == rid                          # 复用既有行，不新建


def test_scan_directory_walk_error_raises(tmp_path, monkeypatch):
    """os.walk onerror 接线必须为抛错而非吞掉：不可读目录让扫描失败，
    从而"只有 walk 完整成功才返回"，索引层才允许标 missing。"""
    import core.organizer as organizer_mod

    calls: dict = {}

    def _fake_walk(root, onerror=None):
        calls["onerror"] = onerror
        if onerror is not None:
            onerror(OSError(13, "Permission denied"))
        yield ()                                  # onerror 抛出时不可达

    monkeypatch.setattr(organizer_mod.os, "walk", _fake_walk)

    with pytest.raises(OSError):
        list(organizer_mod.scan_directory(tmp_path))
    assert calls["onerror"] is not None           # onerror 已接线传入


# ======================================================================
# g. 扫描异常 → failed，索引不动
# ======================================================================
def test_scan_failure_keeps_active_rows(webapp_app, tmp_path,
                                        fake_audio_factory, monkeypatch):
    (app,) = webapp_app
    import organizer_manager as om
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "晴天"})
    mgr = OrganizerManager(app)
    job1 = mgr.submit_scan()
    _run(mgr, job1.id)
    before = [(r.path, r.status, r.title, r.size) for r in LocalAudioFile.query.all()]

    def _boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(om, "scan_directory", _boom)
    job2 = mgr.submit_scan()
    _run(mgr, job2.id)

    row = db.session.get(OrganizerJob, job2.id)
    assert row.status == "failed"
    assert "boom" in row.error_msg
    assert row.finished_at is not None
    # 既有行不被标 missing、数据不变
    after = [(r.path, r.status, r.title, r.size) for r in LocalAudioFile.query.all()]
    assert after == before
    assert all(r.status == "active" for r in LocalAudioFile.query.all())


# ======================================================================
# h. 重启恢复：pending/running → interrupted
# ======================================================================
def test_recover_interrupted_marks_active_jobs(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    j_running = OrganizerJob(job_type="scan", status="running", progress=40)
    j_pending = OrganizerJob(job_type="scan", status="pending")
    db.session.add(j_running)
    db.session.add(j_pending)
    db.session.commit()

    mgr = OrganizerManager(app)
    mgr._recover_interrupted()

    assert db.session.get(OrganizerJob, j_running.id).status == "interrupted"
    assert db.session.get(OrganizerJob, j_running.id).finished_at is not None
    assert db.session.get(OrganizerJob, j_pending.id).status == "interrupted"


# ======================================================================
# i. 重复提交 → OrganizerBusyError
# ======================================================================
def test_duplicate_submit_raises_busy(webapp_app, tmp_path):
    (app,) = webapp_app
    _setup_out_dir(tmp_path)
    mgr = OrganizerManager(app)
    mgr.submit_scan()

    with pytest.raises(OrganizerBusyError):
        mgr.submit_scan()


# ======================================================================
# j. 真线程端到端 + stop 干净退出
# ======================================================================
def test_real_thread_end_to_end(tmp_path):
    """文件 SQLite 自建 app（内存库无法跨线程共享），start→submit→轮询终态"""
    from flask import Flask

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / 'org.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        out = tmp_path / "dl"
        out.mkdir()
        (out / "晴天-12345.mp3").write_bytes(b"a" * 100)
        (out / "七里香-678.flac").write_bytes(b"b" * 500)
        Setting.set("output_dir", str(out))

    mgr = OrganizerManager(app)
    mgr.start()
    try:
        with app.app_context():
            jid = mgr.submit_scan().id
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            with app.app_context():
                status = db.session.get(OrganizerJob, jid).status
            if status in ("success", "partial", "failed", "interrupted"):
                break
            time.sleep(0.05)
        assert status == "success"
        with app.app_context():
            assert LocalAudioFile.query.count() == 2
    finally:
        worker = mgr._worker
        mgr.stop()
    assert worker is not None and not worker.is_alive()   # 干净退出


# ======================================================================
# k. API 层
# ======================================================================
@pytest.fixture
def org_admin_client(webapp_app, tmp_path):
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
def org_member_client(webapp_app):
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


def test_api_scan_returns_job_id(org_admin_client, tmp_path,
                                 fake_audio_factory):
    client = org_admin_client
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "晴天"})

    resp = client.post("/api/organize/scan")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["code"] == 0
    assert body["data"]["status"] == "pending"
    jid = body["data"]["job_id"]
    row = db.session.get(OrganizerJob, jid)
    assert row is not None and row.status == "pending"

    # state：有活跃任务
    state = client.get("/api/organize/state").get_json()["data"]
    assert state["active_job"]["id"] == jid
    assert state["active_job"]["status"] == "pending"


def test_api_scan_missing_dir(org_admin_client, tmp_path):
    client = org_admin_client
    Setting.set("output_dir", str(tmp_path / "nope"))

    body = client.post("/api/organize/scan").get_json()

    assert body["code"] == 1
    assert body["msg"] == "下载目录不存在"


def test_api_scan_requires_admin(org_member_client, tmp_path):
    _setup_out_dir(tmp_path)
    resp = org_member_client.post("/api/organize/scan")
    assert resp.status_code == 403


def test_api_scan_without_manager(org_admin_client):
    client = org_admin_client
    client.application.config.pop("ORGANIZER_MANAGER", None)

    body = client.post("/api/organize/scan").get_json()

    assert body == {"code": 1, "msg": "整理服务未初始化"}


def test_api_job_detail(org_admin_client, tmp_path, fake_audio_factory):
    client = org_admin_client
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "晴天"})

    jid = client.post("/api/organize/scan").get_json()["data"]["job_id"]
    _run(client.application.config["ORGANIZER_MANAGER"], jid)

    resp = client.get(f"/api/organize/jobs/{jid}")
    assert resp.status_code == 200
    job = resp.get_json()["data"]["job"]
    assert job["id"] == jid
    assert job["job_type"] == "scan"
    assert job["status"] == "success"
    assert job["progress"] == 100
    assert job["processed"] == job["total"] == 1
    assert job["error_msg"] is None
    assert job["result"]["scanned"] == 1
    for key in ("created_at", "started_at", "finished_at"):
        assert job[key] is not None

    # 任务不存在
    resp = client.get("/api/organize/jobs/999999")
    assert resp.status_code == 200
    assert resp.get_json() == {"code": 1, "msg": "任务不存在"}


def test_api_state(org_admin_client, tmp_path, fake_audio_factory):
    client = org_admin_client
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3")
    fake_audio_factory(tags={"title": "晴天"})

    jid = client.post("/api/organize/scan").get_json()["data"]["job_id"]
    _run(client.application.config["ORGANIZER_MANAGER"], jid)

    state = client.get("/api/organize/state").get_json()["data"]
    assert state["active_job"] is None              # 已到终态
    assert state["last_scan"]["id"] == jid
    assert state["last_scan"]["status"] == "success"
    assert state["index"] == {"total": 1, "active": 1, "missing": 0}


def test_api_files(org_admin_client, tmp_path, fake_audio_factory):
    client = org_admin_client
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3", size=100)
    _make_audio(out_dir, "七里香-678.flac", size=500)
    fake_audio_factory(tags={"title": "晴天"})

    jid = client.post("/api/organize/scan").get_json()["data"]["job_id"]
    _run(client.application.config["ORGANIZER_MANAGER"], jid)

    resp = client.get("/api/organize/files")
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["total"] == 2
    item = data["items"][0]
    for key in ("id", "path", "rel", "filename", "ext", "size", "mtime",
                "status", "lossless", "duration_ms", "bitrate_kbps",
                "sample_rate", "channels", "bits_per_sample", "title",
                "artist", "album", "song_id", "platform"):
        assert key in item
    assert item["rel"] == item["filename"]          # 顶层文件 rel 即文件名
    assert item["platform"] is None                 # ORG-01 恒为 NULL
    assert item["status"] == "active"

    # 分页 + 状态过滤
    page = client.get("/api/organize/files?limit=1&offset=1").get_json()["data"]
    assert page["total"] == 2
    assert len(page["items"]) == 1
    missing = client.get("/api/organize/files?status=missing").get_json()["data"]
    assert missing["total"] == 0


def test_api_files_requires_admin(org_member_client, tmp_path):
    _setup_out_dir(tmp_path)
    resp = org_member_client.get("/api/organize/files")
    assert resp.status_code == 403


def test_api_duplicates(org_admin_client, tmp_path, fake_audio_factory):
    client = org_admin_client
    out_dir = _setup_out_dir(tmp_path)
    _make_audio(out_dir, "晴天-12345.mp3", size=100)
    flac = out_dir / "晴天-12345.flac"
    flac.write_bytes(b"b" * 1000)
    fake_audio_factory(tags={"title": "晴天"})

    jid = client.post("/api/organize/scan").get_json()["data"]["job_id"]
    _run(client.application.config["ORGANIZER_MANAGER"], jid)

    resp = client.get("/api/organize/duplicates")
    assert resp.status_code == 200
    groups = resp.get_json()["data"]["groups"]
    assert len(groups) == 1
    g = groups[0]
    assert g["group_type"] == "same_id"
    assert g["recommended"].endswith(".flac")
    assert len(g["items"]) == 2
    for item in g["items"]:
        assert "id" in item and "path" in item and "song_id" in item
        assert item["song_id"] == "12345"
