"""下载历史音质快照测试。

根因：DownloadTask 无音质字段，下载成功只 merge 单条 Song（覆盖式），
/api/songs 用 DownloadTask LEFT JOIN Song，N 条历史行全显示同一条 Song 的
最新 quality。修复：任务行记录下载时刻快照（quality/file_path/file_size），
读取优先用快照、空值回退 Song（存量兼容，不回填不编造）。
"""

import sqlite3

import pytest
from flask import Flask
from sqlalchemy import inspect, text

import routes.api as api_module
from models import DownloadTask, Song, db, init_db


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _add_song(quality="lossless", sid="10086", platform="netease",
              file_path="/music/a.flac", file_size=12345):
    """向内存库插入一条 Song 记录并提交。"""
    song = Song(
        id=sid,
        platform=platform,
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        duration_ms=0,
        quality=quality,
        file_path=file_path,
        file_size=file_size,
        playlist_id=None,
        source_name="",
        status="success",
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()
    return song


def _add_task(song_id="10086", platform="netease", quality=None,
              file_path=None, file_size=None, status="done"):
    """向内存库插入一条 DownloadTask 记录并提交。

    quality/file_path/file_size 传 None 表示走列默认值（模拟存量行/新列默认）。
    """
    task = DownloadTask(
        platform=platform,
        song_id=song_id,
        song_name="测试歌曲",
        artists="测试歌手",
        status=status,
        progress=100,
    )
    if quality is not None:
        task.quality = quality
    if file_path is not None:
        task.file_path = file_path
    if file_size is not None:
        task.file_size = file_size
    db.session.add(task)
    db.session.commit()
    return task


# ----------------------------------------------------------------------
# 1. DownloadTask 新列默认值 + to_dict 输出
# ----------------------------------------------------------------------
def test_downloadtask_snapshot_defaults_and_to_dict(webapp_app):
    """新列默认值为空串/0，且 to_dict 输出三字段。"""
    (app,) = webapp_app
    task = _add_task()
    db.session.expire_all()  # 丢弃会话缓存，从库中重读默认值
    t = DownloadTask.query.get(task.pk)

    assert t.quality == ""
    assert t.file_path == ""
    assert t.file_size == 0

    d = t.to_dict()
    assert d["quality"] == ""
    assert d["file_path"] == ""
    assert d["file_size"] == 0


# ----------------------------------------------------------------------
# 2-4. _history_row：快照优先 / 空值回退 Song / song=None
# ----------------------------------------------------------------------
def test_history_row_snapshot_wins(webapp_app):
    """task.quality="standard"、song.quality="lossless" → 返回 standard（快照优先）。"""
    (app,) = webapp_app
    _add_song(quality="lossless")
    task = _add_task(quality="standard", file_path="/music/old.mp3", file_size=99)

    row = api_module._history_row(task, Song.query.filter_by(id="10086").first())
    assert row["quality"] == "standard"
    assert row["file_path"] == "/music/old.mp3"
    assert row["file_size"] == 99


def test_history_row_empty_snapshot_falls_back_to_song(webapp_app):
    """task.quality=""（存量行）、song.quality="lossless" → 回退 lossless（兼容现状）。"""
    (app,) = webapp_app
    _add_song(quality="lossless", file_path="/music/a.flac", file_size=12345)
    task = _add_task()  # 全部走默认值（存量行）

    row = api_module._history_row(task, Song.query.filter_by(id="10086").first())
    assert row["quality"] == "lossless"
    assert row["file_path"] == "/music/a.flac"
    assert row["file_size"] == 12345


def test_history_row_empty_snapshot_and_no_song(webapp_app):
    """task.quality="" 且 song=None → 空串（skipped/failed 行无 Song 记录）。"""
    (app,) = webapp_app
    task = _add_task(status="skipped")

    row = api_module._history_row(task, None)
    assert row["quality"] == ""
    assert row["file_path"] == ""
    assert row["file_size"] == 0


# ----------------------------------------------------------------------
# 5. 写入点：成功入库段对任务行的快照赋值能正确落库读回
# ----------------------------------------------------------------------
def test_success_snapshot_assignment_persists(webapp_app, tmp_path):
    """按 task_manager 成功入库段同款字段赋值，断言 task.quality 从空
    变为传入的 actual_level 且 file_path/file_size 落库读回。"""
    (app,) = webapp_app
    audio = tmp_path / "done.flac"
    audio.write_bytes(b"x" * 2048)
    task = _add_task(status="downloading")
    task.progress = 10
    db.session.commit()
    pk = task.pk

    # ── 与 webapp/task_manager.py 成功入库段同款赋值 ──
    actual_level = "lossless"
    path = audio
    t = DownloadTask.query.get(pk)
    t.status = "done"
    t.progress = 100
    t.quality = actual_level
    t.file_path = str(path)
    t.file_size = path.stat().st_size if path.exists() else 0
    db.session.commit()
    # ──────────────────────────────────────────────

    db.session.expire_all()
    t2 = DownloadTask.query.get(pk)
    assert t2.quality == "lossless"
    assert t2.file_path == str(audio)
    assert t2.file_size == 2048
    assert t2.to_dict()["quality"] == "lossless"


def test_success_snapshot_missing_file_sizes_zero(webapp_app, tmp_path):
    """写入点 file_size 同款写法：文件不存在时落 0（path.exists() 分支）。"""
    (app,) = webapp_app
    missing = tmp_path / "gone.flac"  # 不创建
    task = _add_task(status="downloading")
    pk = task.pk

    path = missing
    t = DownloadTask.query.get(pk)
    t.quality = "standard"
    t.file_path = str(path)
    t.file_size = path.stat().st_size if path.exists() else 0
    db.session.commit()

    db.session.expire_all()
    t2 = DownloadTask.query.get(pk)
    assert t2.file_size == 0


# ----------------------------------------------------------------------
# 6. 迁移：旧结构 download_tasks 补三列且默认值生效
# ----------------------------------------------------------------------
LEGACY_DDL = """
CREATE TABLE download_tasks (
    pk INTEGER NOT NULL PRIMARY KEY,
    platform VARCHAR(20) DEFAULT 'netease' NOT NULL,
    song_id INTEGER NOT NULL,
    song_name VARCHAR(300),
    artists VARCHAR(300),
    playlist_id INTEGER,
    playlist_name VARCHAR(200),
    status VARCHAR(20),
    progress INTEGER,
    error_msg VARCHAR(500),
    account_id INTEGER,
    fee INTEGER,
    created_at DATETIME,
    updated_at DATETIME
)
"""


def test_init_db_adds_snapshot_columns(tmp_path):
    """临时 SQLite 文件建不含新列的旧结构 download_tasks（song_id 为
    INTEGER，同时触发重建式迁移）→ init_db → inspector 验证三列已补齐
    且默认值生效（存量行补为 ''/''/0，行数据不丢）。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(LEGACY_DDL)
    conn.execute(
        "INSERT INTO download_tasks (song_id, song_name, status, progress) "
        "VALUES (123, '旧歌', 'done', 100)"
    )
    conn.commit()
    conn.close()

    app = Flask(__name__)
    init_db(app, str(db_path))

    with app.app_context():
        inspector = inspect(db.engine)
        cols = {c["name"]: c for c in inspector.get_columns("download_tasks")}
        assert "quality" in cols
        assert "file_path" in cols
        assert "file_size" in cols

        # 默认值生效 + 重建迁移后行数据仍在（song_id 已字符串化）
        row = db.session.execute(
            text("SELECT song_id, quality, file_path, file_size FROM download_tasks")
        ).fetchone()
        assert row[0] == "123"
        assert row[1] == ""
        assert row[2] == ""
        assert row[3] == 0


# ----------------------------------------------------------------------
# 7. 守护：存量兼容——升级前行为不变
# ----------------------------------------------------------------------
def test_legacy_row_output_matches_pre_upgrade_behavior(webapp_app):
    """已有 Song(quality=lossless) + 快照为空的 DownloadTask 场景，
    _history_row 输出与升级前行为一致（沿用 Song 的 quality/file_path/
    file_size），其余字段名不变。"""
    (app,) = webapp_app
    _add_song(quality="lossless", file_path="/music/a.flac", file_size=12345)
    task = _add_task()  # 存量行：快照列为默认空值

    song = Song.query.filter_by(id="10086").first()
    row = api_module._history_row(task, song)

    # 升级前该行从 Song 借历史属性，升级后快照为空必须原样回退
    assert row["quality"] == "lossless"
    assert row["file_path"] == "/music/a.flac"
    assert row["file_size"] == 12345
    # 响应字段名与升级前完全一致（前端零改动）
    expected_keys = {
        "id", "pk", "platform", "platform_name", "name", "artists", "album",
        "duration_ms", "quality", "file_path", "file_size", "playlist_id",
        "playlist_name", "downloaded_at", "status", "error_msg", "account_id",
    }
    assert expected_keys <= set(row.keys())
