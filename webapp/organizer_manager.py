"""整理任务调度器（ORG-01）

职责：
1. 音乐扫描（/api/organize/scan）后台任务化：单 worker 线程串行处理
   organizer_jobs 队列，进度经 job 行实时更新，前端轮询读取
2. 维护 LocalAudioFile 本地文件索引：增量复用未变化行（size+mtime 命中）、
   变化重读、消失标 missing（只在 walk 完整成功后标记）
3. 重复分组沿用 core.organizer.group_duplicates，SHA1 经 content_hash
   惰性缓存：命中不读盘，新算结果回写行
4. 关联回填（ORG-02）：扫描内用 DownloadTask / Song 的 file_path 命中
   给索引行回填可信身份 (platform, song_id)（只读任务/Song 表）

线程安全说明（与 task_manager 同款约束）：
- Flask 多线程下 SQLAlchemy session 需在子线程内创建/销毁
- worker 线程内所有 DB / Setting 访问必须在 app.app_context() 内完成，
  禁止裸调 Setting.get（PR #7）
"""

import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from core.organizer import FileEntry, group_duplicates, scan_directory
from models import (
    ORGANIZER_JOB_ACTIVE_STATUSES,
    DownloadTask,
    LocalAudioFile,
    OrganizerJob,
    Setting,
    Song,
    db,
)

logger = logging.getLogger(__name__)

# 项目根目录，用于解析相对路径的 output_dir；frozen: PyInstaller 打包后
# 用 exe 同级目录作为根目录（与 task_manager._ROOT 保持同一规则）
if getattr(sys, "frozen", False):
    _ROOT = Path(sys.executable).resolve().parent
else:
    _ROOT = Path(__file__).resolve().parent.parent


def _norm_lookup_path(raw: str) -> str | None:
    """路径归一化为关联查找键：与 routes/api._same_file_path 同规则
    （Path.resolve 比较，归一 ..、分隔符与软链接）。
    resolve 失败（OSError 等）返回 None，调用方跳过该行——宁可不关联、
    不误关联；与 api._same_file_path 失败时回退字符串比较不同，此处直接
    跳过是刻意的保守选择（关联错平台/错歌曲的代价高于漏关联）。
    """
    try:
        return str(Path(raw).resolve())
    except (OSError, ValueError):
        return None

# 进度落库节奏：每 N 个文件或每 1 秒 commit 一次（取先到者），避免逐条 commit
_PROGRESS_COMMIT_EVERY = 25


class OrganizerBusyError(Exception):
    """已有整理任务在执行（pending/running），拒绝重复提交"""


class OrganizerManager:
    """整理任务管理器：submit 入队 + 单 worker 串行执行 + 索引维护"""

    def __init__(self, app):
        self.app = app
        self._queue: queue.Queue = queue.Queue()   # 存放 OrganizerJob.id
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 worker 线程（幂等）；先把上次中断的任务标 interrupted"""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._recover_interrupted()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="organizer-worker")
        self._worker.start()
        logger.info("OrganizerManager 已启动")

    def stop(self) -> None:
        """置停止位 + 哨兵唤醒，等待在途任务自然结束（不强杀，超时放弃等待）"""
        if not self._started:
            return
        self._started = False
        self._stop.set()
        self._queue.put(None)
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10)
        self._worker = None
        logger.info("OrganizerManager 已停止")

    def _recover_interrupted(self) -> None:
        """启动恢复：上次进程退出时残留在途的 pending/running 一律标
        interrupted（含 finished_at）。刻意不自动重试：扫描由用户手动触发，
        重启后自动跑盘上扫描超出用户预期（先例参照 task_manager._recover_tasks）
        """
        with self.app.app_context():
            jobs = OrganizerJob.query.filter(
                OrganizerJob.status.in_(ORGANIZER_JOB_ACTIVE_STATUSES)
            ).all()
            for j in jobs:
                j.status = "interrupted"
                j.finished_at = datetime.now()
                if not j.error_msg:
                    j.error_msg = "服务重启时任务被中断"
            if jobs:
                db.session.commit()
            count = len(jobs)
        if count:
            logger.info("启动恢复：%d 个未完成整理任务标记为 interrupted", count)

    # ------------------------------------------------------------------
    # 提交与执行
    # ------------------------------------------------------------------
    def submit_scan(self) -> OrganizerJob:
        """提交扫描任务：入库 pending 并入队，立即返回 job 行

        Raises:
            OrganizerBusyError: 已有在途整理任务（单 worker 串行，拒绝并发）
            ValueError: 下载目录不存在
        """
        with self.app.app_context():
            active = OrganizerJob.query.filter(
                OrganizerJob.status.in_(ORGANIZER_JOB_ACTIVE_STATUSES)
            ).first()
            if active is not None:
                raise OrganizerBusyError("已有整理任务正在执行")
            if not self._resolve_output_dir().is_dir():
                raise ValueError("下载目录不存在")
            job = OrganizerJob(job_type="scan", status="pending")
            db.session.add(job)
            db.session.commit()
            db.session.refresh(job)     # 确保 id 等字段已加载
            db.session.expunge(job)     # 脱离 session：context 出栈后仍可读已加载字段
            self._queue.put(job.id)
            logger.info("整理扫描任务已提交: job_id=%s", job.id)
            return job

    def run_job(self, job_id: int) -> None:
        """同步执行单个任务：原子认领 → 执行 → 落终态。worker 循环与测试都调它

        统一收尾：成功 → success + progress=100；异常 → failed +
        error_msg（截断 2000）。两者都写 finished_at。
        """
        if not self._claim(job_id):
            logger.info("整理任务 %s 认领失败（非 pending），跳过", job_id)
            return
        try:
            with self.app.app_context():
                job = db.session.get(OrganizerJob, job_id)
                if job is None:
                    return
                if job.job_type != "scan":
                    raise ValueError("任务类型暂不支持")
                self._execute_scan(job)
                job.status = "success"
                job.progress = 100
                job.finished_at = datetime.now()
                db.session.commit()
                logger.info("整理扫描完成 job_id=%s: %s", job_id, job.result)
        except Exception as e:
            logger.exception("整理任务 %s 执行失败: %s", job_id, e)
            self._finish_failed(job_id, str(e))

    def _claim(self, job_id: int) -> bool:
        """条件 UPDATE pending→running + started_at，rowcount 原子认领
        （先例参照 task_manager._process_task 的条件 UPDATE 写法）"""
        with self.app.app_context():
            claimed = db.session.query(OrganizerJob).filter(
                OrganizerJob.id == job_id,
                OrganizerJob.status == "pending",
            ).update({"status": "running", "started_at": datetime.now()},
                     synchronize_session=False)
            db.session.commit()
            return bool(claimed)

    def _finish_failed(self, job_id: int, error: str) -> None:
        with self.app.app_context():
            job = db.session.get(OrganizerJob, job_id)
            if job is None:
                return
            job.status = "failed"
            job.error_msg = (error or "")[:2000]
            job.finished_at = datetime.now()
            db.session.commit()

    # ------------------------------------------------------------------
    # worker 循环
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        logger.info("整理工作线程已启动")
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if job_id is None:
                break                    # stop() 哨兵：立即退出
            try:
                self.run_job(job_id)
            except Exception as e:
                # run_job 内部已兜底落终态，此处防御未知异常带崩线程
                logger.exception("整理任务 %s 处理异常: %s", job_id, e)
        logger.info("整理工作线程已退出")

    # ------------------------------------------------------------------
    # 扫描核心
    # ------------------------------------------------------------------
    def _backfill_associations(self) -> None:
        """扫描内关联回填（ORG-02）：下载历史的 file_path 命中 → 给
        LocalAudioFile 行回填可信身份 (platform, song_id)

        - 任务映射优先于 Song 映射（任务带下载时刻快照，更接近真实下载行为）；
        - 同一路径多条任务取最新一条（updated_at → created_at → pk 降序，
          与 routes/api.py 既有"最新快照"判定同规则）；
        - 两者都不命中时保守保留原值：历史关联不因一次未命中清除（文件可能
          处于改名/移动的中间态，一次扫描缺席不代表历史无效），本函数只做
          可信覆盖、绝不擦除；文件名解析 song_id 只在增量同步步骤作初值；
        - 本步骤只写 LocalAudioFile，DownloadTask / Song 零写入。

        调用方须已在 app context 内；提交随 _execute_scan 既有批量 commit。
        """
        task_map: dict[str, tuple[str, str]] = {}
        # 写路径仅在任务 done 时落 file_path（task_manager.py:1785/1806），
        # 此处过滤是防御存量库/异常行：非 done 行的路径不作为该文件的
        # 下载历史事实
        tasks = DownloadTask.query.filter(
            DownloadTask.file_path != "",
            DownloadTask.status == "done",
        ).order_by(DownloadTask.updated_at.desc(),
                   DownloadTask.created_at.desc(),
                   DownloadTask.pk.desc()).all()
        for t in tasks:
            key = _norm_lookup_path(t.file_path)
            if key is not None:
                task_map.setdefault(key, (t.platform, t.song_id))

        song_map: dict[str, tuple[str, str]] = {}
        songs = Song.query.filter(
            Song.file_path != "",
            Song.status == "success",
        ).order_by(Song.platform.asc(), Song.id.asc()).all()
        for s in songs:
            key = _norm_lookup_path(s.file_path)
            if key is not None:
                song_map.setdefault(key, (s.platform, s.id))

        rows = LocalAudioFile.query.filter_by(status="active").all()
        for row in rows:
            key = _norm_lookup_path(row.path)
            if key is None:
                continue
            hit = task_map.get(key) or song_map.get(key)
            if hit:
                row.platform, row.song_id = hit

    def _resolve_output_dir(self) -> Path:
        """解析下载输出目录。规则与 routes/api._resolve_output_dir 保持一致
        （Setting 相对路径基于项目根；frozen 包基于 exe 同级目录）。刻意不复用
        api 模块（避免反向依赖 web 层），规则变更须两处同步。
        调用方须已在 app context 内（读 Setting）。
        """
        output_dir = Setting.get("output_dir", "downloads")
        p = Path(output_dir)
        if not p.is_absolute():
            p = _ROOT / output_dir
        return p.resolve()

    def _execute_scan(self, job) -> None:
        """核心扫描：增量同步 LocalAudioFile 索引 + 重复分组，写 result 摘要

        调用方（run_job）须已持有 app context 且已认领 job；本函数只把任务
        推进到可成功提交的状态，终态由 run_job 统一落。

        关键约束：
        - 只有 walk 完整成功后才允许标 missing；任何中途异常直接抛出交给
          run_job 落 failed，绝不触碰 missing 标记（误标会让索引与磁盘
          真实状态相反，用户会看到"文件丢失"假象）；
        - progress 只反映真实 processed/total，不虚构。
        """
        out_dir = self._resolve_output_dir()
        if not out_dir.is_dir():
            raise ValueError("下载目录不存在")
        started = time.perf_counter()

        # 1. 全量 walk（单个文件元数据读取失败由 core 层容错，不中断）
        entries = scan_directory(out_dir)
        total = len(entries)
        job.total = total
        db.session.commit()

        # 2. 增量同步索引：size+mtime 均未变 → 复用；否则新建/更新并清哈希
        by_path = {r.path: r for r in LocalAudioFile.query.all()}
        seen: set[str] = set()
        n_new = n_updated = n_reused = 0
        last_commit = time.perf_counter()
        for idx, e in enumerate(entries, 1):
            p = str(e.path)
            seen.add(p)
            row = by_path.get(p)
            if row is not None and row.size == e.size and row.mtime == e.mtime:
                # 复用：磁盘文件未变化，只刷新扫描批次号与状态。
                # 约束：walk 只会见真实存在于扫描树内的文件（.trash 与点目录
                # 在 core 层剪枝，trashed/cleaned 行不会被误复活），故 missing
                # 行在文件重现时必须回归 active——否则索引与磁盘相反，污染
                # state / files / duplicates 三个读端点
                row.status = "active"
                row.last_scan_job_id = job.id
                n_reused += 1
            else:
                if row is None:
                    row = LocalAudioFile(path=p)
                    db.session.add(row)
                    n_new += 1
                else:
                    n_updated += 1
                row.size = e.size
                row.mtime = e.mtime
                row.status = "active"
                row.ext = e.ext
                row.lossless = e.lossless
                row.duration_ms = e.duration_ms
                row.bitrate_kbps = e.bitrate_kbps
                row.sample_rate = e.sample_rate
                row.channels = e.channels
                row.bits_per_sample = e.bits_per_sample
                row.title = e.title
                row.artist = e.artist
                row.album = e.album
                row.song_id = e.song_id
                row.content_hash = None        # 文件变化，惰性哈希作废
                row.last_scan_job_id = job.id
            job.processed = idx
            job.progress = idx * 100 // total if total else 100
            now = time.perf_counter()
            if idx % _PROGRESS_COMMIT_EVERY == 0 or (now - last_commit) >= 1.0:
                db.session.commit()
                last_commit = now
        job.processed = total
        job.progress = total * 100 // total if total else 100
        db.session.commit()

        # 3. 标 missing：walk 完整成功走到这里，本扫描未见到且仍记 active 的行
        #    （seen 为空时 in_ 空列表语义不可靠，单独分支处理）
        if seen:
            missing_rows = LocalAudioFile.query.filter(
                LocalAudioFile.status == "active",
                ~LocalAudioFile.path.in_(seen),
            ).all()
        else:
            missing_rows = LocalAudioFile.query.filter_by(status="active").all()
        for row in missing_rows:
            row.status = "missing"
        n_missing = len(missing_rows)
        db.session.commit()

        # 3.5 关联回填（ORG-02）：任务/Song 的 file_path 命中 → 可信身份
        #     覆盖（只写索引行，不擦除历史关联）。无独立 commit，随下方
        #     步骤 4 的批量提交落库。
        self._backfill_associations()

        # 4. 分组：active 行重组为 FileEntry，SHA1 经 content_hash 惰性复用，
        #    新算出的哈希回写行（供下次分组免读盘）
        active_rows = LocalAudioFile.query.filter_by(status="active").all()
        hash_cache = {r.path: r.content_hash for r in active_rows
                      if r.content_hash}
        file_entries = [
            FileEntry(
                path=Path(r.path), size=r.size, mtime=r.mtime, ext=r.ext,
                song_id=r.song_id, platform=r.platform, title=r.title,
                artist=r.artist,
                album=r.album, duration_ms=r.duration_ms,
                bitrate_kbps=r.bitrate_kbps, sample_rate=r.sample_rate,
                channels=r.channels, bits_per_sample=r.bits_per_sample,
                lossless=r.lossless,
            )
            for r in active_rows
        ]
        groups = group_duplicates(file_entries, hash_cache=hash_cache)
        rows_by_path = {r.path: r for r in active_rows}
        for p, digest in hash_cache.items():
            row = rows_by_path.get(p)
            if row is not None and row.content_hash != digest:
                row.content_hash = digest
        db.session.commit()

        groups_summary = {"same_id": 0, "same_id_candidate": 0, "identical": 0}
        dup_files: set[str] = set()
        for g in groups:
            groups_summary[g.group_type] = groups_summary.get(g.group_type, 0) + 1
            dup_files.update(str(i.path) for i in g.items)

        summary = {
            "scanned": len(entries),
            "new": n_new,
            "updated": n_updated,
            "reused": n_reused,
            "missing": n_missing,
            "duration_s": round(time.perf_counter() - started, 3),
            "groups": groups_summary,
            "duplicate_files": len(dup_files),
        }
        job.result = json.dumps(summary, ensure_ascii=False)
        db.session.commit()
