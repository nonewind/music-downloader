"""_find_delivered_song / _song_file_exists 守门逻辑测试。

覆盖下载治理规则："success 记录且文件仍在磁盘才跳过；文件丢失
允许重新下载"，以及 _mark_failed 不覆盖 success 记录的守护行为。
"""

import task_manager
from models import DownloadTask, Song, db


def _add_song(file_path, sid="10086", platform="netease", status="success"):
    """向内存库插入一条 Song 记录并提交。"""
    song = Song(
        id=sid,
        platform=platform,
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        duration_ms=0,
        quality="standard",
        file_path=file_path,
        file_size=1,
        playlist_id=None,
        source_name="",
        status=status,
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()
    return song


# ----------------------------------------------------------------------
# _find_delivered_song：查询与文件存在性守门
# ----------------------------------------------------------------------
def test_success_with_file_on_disk_returns_record(webapp_app, tmp_path):
    """1. success 记录 + 文件真实存在（绝对路径）→ 返回该记录。"""
    (app,) = webapp_app
    assert app is not None
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake audio")
    _add_song(str(audio))

    found = task_manager._find_delivered_song("10086", "netease")
    assert found is not None
    assert found.id == "10086"
    assert found.platform == "netease"
    assert found.file_path == str(audio)


def test_success_with_missing_file_returns_none(webapp_app, tmp_path):
    """2. success 记录但文件已丢失 → None（允许入队重下）。"""
    (app,) = webapp_app
    missing = tmp_path / "deleted.mp3"  # 不创建该文件
    _add_song(str(missing))

    assert task_manager._find_delivered_song("10086", "netease") is None


def test_success_with_empty_file_path_returns_none(webapp_app):
    """3. success 记录但 file_path 为空串 → None（无路径视为文件不存在）。"""
    (app,) = webapp_app
    _add_song("")

    assert task_manager._find_delivered_song("10086", "netease") is None


def test_no_record_returns_none(webapp_app):
    """4. 无任何记录 → None。"""
    (app,) = webapp_app
    assert task_manager._find_delivered_song("99999", "netease") is None


def test_relative_path_missing_under_root_returns_none(webapp_app, tmp_path, monkeypatch):
    """5a. 相对路径记录且 _ROOT 下文件不存在 → None。"""
    (app,) = webapp_app
    monkeypatch.setattr(task_manager, "_ROOT", tmp_path)
    _add_song("downloads/x.mp3")  # tmp_path 下不创建 downloads/x.mp3

    assert task_manager._find_delivered_song("10086", "netease") is None


def test_relative_path_present_under_root_returns_record(webapp_app, tmp_path, monkeypatch):
    """5b. 相对路径正例：monkeypatch _ROOT=tmp_path 并在 tmp 下构造
    downloads/x.mp3，相对路径以 _ROOT 为基准解析后文件存在 → 返回记录。"""
    (app,) = webapp_app
    monkeypatch.setattr(task_manager, "_ROOT", tmp_path)
    rel_file = tmp_path / "downloads" / "x.mp3"
    rel_file.parent.mkdir()
    rel_file.write_bytes(b"fake audio")
    _add_song("downloads/x.mp3")

    found = task_manager._find_delivered_song("10086", "netease")
    assert found is not None
    assert found.file_path == "downloads/x.mp3"


def test_platform_isolation(webapp_app, tmp_path):
    """补充：同 id 跨平台是合法状态（复合主键），查询必须带平台维度。"""
    (app,) = webapp_app
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake audio")
    _add_song(str(audio), platform="qq")

    # netease 无记录 → None；qq 命中 → 返回记录
    assert task_manager._find_delivered_song("10086", "netease") is None
    assert task_manager._find_delivered_song("10086", "qq") is not None


# ----------------------------------------------------------------------
# _mark_failed 守护：success 记录不被失败路径覆盖
# ----------------------------------------------------------------------
def test_mark_failed_preserves_success_record(webapp_app):
    """6. 预置 success Song 后触发 _mark_failed：Song 记录仍为 success
    未被覆盖，任务行正常终态化为 failed。"""
    (app,) = webapp_app
    _add_song("", sid="20001")
    tm = task_manager.TaskManager(app)  # 构造不启动线程/调度器
    task = DownloadTask(
        platform="netease",
        song_id="20001",
        song_name="测试歌曲",
        artists="测试歌手",
        playlist_id=None,
        playlist_name="搜索单曲",
        status="downloading",
        progress=10,
    )
    db.session.add(task)
    db.session.commit()
    pk = task.pk
    # 收尾外层会话事务：_mark_failed 内部自开 app_context，在
    # Flask-SQLAlchemy 3.x 下是独立会话，外层不留开启的事务才能
    # 保证内层更新对外可见（下方断言不用 Query.get，避免命中
    # 身份映射里未过期的旧对象而绕过数据库）
    db.session.commit()

    tm._mark_failed(pk, "20001", "测试歌曲", "测试歌手", None, "", "模拟下载失败",
                    platform="netease")

    db.session.expire_all()  # 丢弃外层会话缓存，从库中重读
    row = Song.query.filter_by(id="20001", platform="netease").first()
    assert row is not None
    assert row.status == "success"
    assert row.error_msg != "模拟下载失败"
    t = DownloadTask.query.filter_by(pk=pk).first()
    assert t is not None
    assert t.status == "failed"
