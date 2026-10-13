"""ORG-04 前端工作台升级配套后端补齐 测试。

覆盖：
1. /api/songs file_state 注入（下载页 done 行文件状态徽章）：
   - 索引四态命中：active / trashed / cleaned / missing
   - 未入索引 → "unknown"
   - 无文件路径 / 非 done 行（downloading/failed）→ None
   - /api/tasks 保持现状：仅返回在途任务、不带 file_state 键
     （注：任务简报写的注入点 /api/tasks 实际只返回 pending/downloading/
      paused（task_manager.get_active_tasks 过滤），done 行只在 /api/songs
      的 _history_row 单点构造中出现——故 file_state 注入在 /api/songs，
      task_manager 零改动）
2. 旧库兼容：raw SQL 临时 SQLite 文件建最小旧 schema（playlists/songs/
   download_tasks/settings/users，列取 models.py 现状关键列；songs.id 与
   download_tasks.song_id 为 INTEGER 以触发重建式迁移）→ init_db →
   organizer_jobs / local_audio_files / organizer_clean_items 三表存在，
   旧行完好（songs 复合主键字符串化、快照补列默认值、用户/设置保留）。
"""

import sqlite3

import pytest
from flask import Flask
from sqlalchemy import inspect, text

from models import (
    DownloadTask,
    LocalAudioFile,
    OrganizerCleanItem,
    OrganizerJob,
    Setting,
    User,
    db,
    init_db,
)


# ======================================================================
# 公共：登录态 client（/api/songs、/api/tasks 仅要求登录）
# ======================================================================
@pytest.fixture
def api_client(webapp_app):
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"
    # /api/tasks 读取 config["TASK_MANAGER"]（构造不启动线程/调度器）
    from task_manager import TaskManager
    app.config["TASK_MANAGER"] = TaskManager(app)
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="root", is_admin=True, enabled=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["uid"] = user.id
    return client


def _add_task(status: str, file_path: str = "", song_id: str = "1001") -> DownloadTask:
    t = DownloadTask(
        platform="netease", song_id=song_id, song_name="晴天",
        artists="周杰伦", status=status, file_path=file_path,
    )
    db.session.add(t)
    db.session.commit()
    return t


def _add_local(path: str, status: str = "active") -> None:
    db.session.add(LocalAudioFile(path=path, size=100, mtime=1.0, status=status))
    db.session.commit()


# ======================================================================
# 1. /api/songs file_state：四态 + unknown + null
# ======================================================================
def test_api_songs_file_state_active_trashed_cleaned_missing(api_client):
    _add_local("/music/a.mp3", "active")
    _add_local("/music/b.mp3", "trashed")
    _add_local("/music/c.flac", "cleaned")
    _add_local("/music/d.mp3", "missing")
    t_active = _add_task("done", "/music/a.mp3", song_id="1")
    t_trashed = _add_task("done", "/music/b.mp3", song_id="2")
    t_cleaned = _add_task("done", "/music/c.flac", song_id="3")
    t_missing = _add_task("done", "/music/d.mp3", song_id="4")

    data = api_client.get("/api/songs?per_page=50").get_json()["data"]
    by_pk = {row["pk"]: row for row in data}

    assert by_pk[t_active.pk]["file_state"] == "active"
    assert by_pk[t_trashed.pk]["file_state"] == "trashed"
    assert by_pk[t_cleaned.pk]["file_state"] == "cleaned"
    assert by_pk[t_missing.pk]["file_state"] == "missing"


def test_api_songs_file_state_unknown_and_null(api_client):
    _add_local("/music/a.mp3", "active")
    t_unknown = _add_task("done", "/music/e.mp3", song_id="5")     # 未入索引
    t_nopath = _add_task("done", "", song_id="6")                  # done 但无文件
    t_downloading = _add_task("downloading", "/music/a.mp3", song_id="7")
    t_failed = _add_task("failed", "/music/a.mp3", song_id="8")

    data = api_client.get("/api/songs?per_page=50").get_json()["data"]
    by_pk = {row["pk"]: row for row in data}

    assert by_pk[t_unknown.pk]["file_state"] == "unknown"
    assert by_pk[t_nopath.pk]["file_state"] is None
    assert by_pk[t_downloading.pk]["file_state"] is None
    assert by_pk[t_failed.pk]["file_state"] is None
    # done 行的其余字段不受影响（display_status 仍为 success）
    assert by_pk[t_unknown.pk]["status"] == "success"


def test_api_tasks_active_only_without_file_state(api_client):
    """注入点说明守卫：/api/tasks 仅返回在途任务（done 不出现），
    任务行保持 to_dict 现状（无 file_state 键），task_manager 零改动。"""
    _add_local("/music/a.mp3", "active")
    _add_task("downloading", "/music/a.mp3", song_id="9")
    _add_task("done", "/music/a.mp3", song_id="10")

    data = api_client.get("/api/tasks").get_json()["data"]

    statuses = {t["status"] for t in data}
    assert statuses == {"downloading"}
    assert all("file_state" not in t for t in data)


# ======================================================================
# 2. 旧库兼容：最小旧 schema → init_db → organizer 三新表 + 旧行完好
# ======================================================================
# 列取 models.py 现状关键列；刻意缺失的列（platform/account_id/快照三列/
# must_change_password）由 init_db 的 ALTER/重建迁移补齐。songs.source_name
# 必须在旧 schema 中：_migrate_song_ids_to_text 的复制 SELECT 引用了该列，
# init_db 没有它的补列迁移（更老的真实旧库若有缺失需人工补列）。
# 注意表名：Setting 模型未显式声明 __tablename__，Flask-SQLAlchemy 按类名
# 单数化为 `setting`（非 settings）；其余表名与 models.py 的 __tablename__ 一致。
LEGACY_SCHEMA = """
CREATE TABLE playlists (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    type VARCHAR(20),
    enabled BOOLEAN,
    limit_count INTEGER,
    last_synced_at DATETIME,
    track_count INTEGER,
    created_at DATETIME
);
CREATE TABLE songs (
    id INTEGER NOT NULL PRIMARY KEY,
    name VARCHAR(300) NOT NULL,
    artists VARCHAR(300),
    album VARCHAR(300),
    duration_ms INTEGER,
    quality VARCHAR(20),
    file_path VARCHAR(500),
    file_size INTEGER,
    playlist_id INTEGER,
    downloaded_at DATETIME,
    status VARCHAR(20),
    error_msg VARCHAR(500),
    source_name VARCHAR(200)
);
CREATE TABLE download_tasks (
    pk INTEGER NOT NULL PRIMARY KEY,
    song_id INTEGER NOT NULL,
    song_name VARCHAR(300),
    artists VARCHAR(300),
    playlist_id INTEGER,
    playlist_name VARCHAR(200),
    status VARCHAR(20),
    progress INTEGER,
    error_msg VARCHAR(500),
    fee INTEGER,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE setting (
    key VARCHAR(100) NOT NULL PRIMARY KEY,
    value TEXT
);
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_admin BOOLEAN,
    enabled BOOLEAN,
    created_at DATETIME,
    last_login_at DATETIME
);
"""


def test_init_db_migrates_legacy_db_and_creates_organizer_tables(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO playlists (id, name, type, enabled, limit_count, track_count) "
        "VALUES (1, '我的歌单', 'official', 1, 50, 10)"
    )
    conn.execute(
        "INSERT INTO songs (id, name, artists, album, duration_ms, quality, "
        "file_path, file_size, status, source_name) "
        "VALUES (123, '旧歌', '周杰伦', '叶惠美', 200000, 'lossless', "
        "'/old/晴天.mp3', 1024, 'success', '我的歌单')"
    )
    conn.execute(
        "INSERT INTO download_tasks (song_id, song_name, artists, status, "
        "progress, fee) VALUES (123, '旧歌', '周杰伦', 'done', 100, 1)"
    )
    conn.execute("INSERT INTO setting (key, value) VALUES ('output_dir', '/old/downloads')")
    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, enabled) "
        "VALUES ('admin', 'legacy-hash', 1, 1)"
    )
    conn.commit()
    conn.close()

    app = Flask(__name__)
    init_db(app, str(db_path))

    with app.app_context():
        inspector = inspect(db.engine)
        # ORG-01/03 三新表已建
        for table in ("organizer_jobs", "local_audio_files", "organizer_clean_items"):
            assert inspector.has_table(table), table

        # 旧行完好：songs 主键迁移为 (id, platform) 复合、id 字符串化
        song = db.session.execute(
            text("SELECT id, platform, name, artists, status, file_path FROM songs")
        ).fetchone()
        assert song[0] == "123"
        assert song[1] == "netease"
        assert song[2] == "旧歌"
        assert song[3] == "周杰伦"
        assert song[4] == "success"
        assert song[5] == "/old/晴天.mp3"

        # download_tasks：song_id 字符串化，快照三列补齐默认值
        task = db.session.execute(
            text("SELECT song_id, status, quality, file_path, file_size "
                 "FROM download_tasks")
        ).fetchone()
        assert task[0] == "123"
        assert task[1] == "done"
        assert task[2] == ""
        assert task[3] == ""
        assert task[4] == 0

        # users 旧行保留 + must_change_password 补列默认 0（存量账号不强制改密）
        user = db.session.execute(
            text("SELECT username, must_change_password FROM users")
        ).fetchone()
        assert user[0] == "admin"
        assert user[1] == 0

        # 用户自定义设置不被默认值覆盖
        assert Setting.get("output_dir", "") == "/old/downloads"

        # 新表经 ORM 可用（空表就绪）
        assert LocalAudioFile.query.count() == 0
        assert OrganizerJob.query.count() == 0
        assert OrganizerCleanItem.query.count() == 0
