"""历史记录删除语义测试（方案 A：按行删除）。

v0.7.7.1 起每条任务行带当次实际档位快照（quality/file_path/file_size），
"一行一档"。旧版 DELETE ?delete_file=1 删的是 Song.file_path 指向的最新
文件并级联删除该曲全部历史行——用户选中旧 MP3 行却删掉了 FLAC、丢掉全部
历史。新语义：只删选中行指向的文件与该行记录；Song 按 (song_id, platform)
剩余任务行联动（无剩余行则删 Song；被删文件正是 Song 指向的文件时改指向
剩余最新 done 行的非空快照）。delete_file=False 行为完全不变。
"""

import pytest

import routes.api as api_module
from models import DownloadTask, Setting, Song, db


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _set_output_dir(path):
    """把下载目录白名单指向 tmp_path，使 _delete_song_file 放行其中的文件。"""
    Setting.set("output_dir", str(path))


def _add_song(file_path="/music/a.flac", file_size=12345, quality="lossless",
              sid="10086", platform="netease"):
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


def _add_task(file_path="", quality="", file_size=0,
              song_id="10086", platform="netease", status="done"):
    task = DownloadTask(
        platform=platform,
        song_id=song_id,
        song_name="测试歌曲",
        artists="测试歌手",
        status=status,
        progress=100,
        quality=quality,
        file_path=file_path,
        file_size=file_size,
    )
    db.session.add(task)
    db.session.commit()
    return task


def _make_file(path, size=100):
    path.write_bytes(b"x" * size)
    return path


# ----------------------------------------------------------------------
# 1. 删旧行（快照 != Song.file_path）：只删该行文件，Song 与其余行不动
# ----------------------------------------------------------------------
def test_delete_old_row_file_only_removes_that_file(webapp_app, tmp_path):
    """行A快照=mp3、行B快照=flac、Song 指向 flac；删行A(delete_file=True)
    → mp3 被删、flac 完好、行B 保留、Song 保留且仍指 flac。"""
    (app,) = webapp_app
    _set_output_dir(tmp_path)
    mp3 = _make_file(tmp_path / "old.mp3", size=111)
    flac = _make_file(tmp_path / "new.flac", size=222)

    _add_song(file_path=str(flac), file_size=222, quality="lossless")
    row_a = _add_task(file_path=str(mp3), quality="standard", file_size=111)
    _add_task(file_path=str(flac), quality="lossless", file_size=222)

    ok, msg = api_module._delete_song_record(row_a.pk, delete_file=True)

    assert ok, msg
    assert not mp3.exists()
    assert flac.exists()
    # 行B 保留
    assert DownloadTask.query.count() == 1
    remaining = DownloadTask.query.first()
    assert remaining.file_path == str(flac)
    # Song 保留且仍指向 flac
    song = Song.query.filter_by(id="10086").first()
    assert song is not None
    assert song.file_path == str(flac)
    assert song.quality == "lossless"
    assert song.file_size == 222


# ----------------------------------------------------------------------
# 2. 删最新行（快照 == Song.file_path）：Song 改指向剩余最新 done 行快照
# ----------------------------------------------------------------------
def test_delete_current_row_song_repoints_to_latest_remaining(webapp_app, tmp_path):
    """删行B（flac，Song 当前指向）→ flac 被删、行A 保留、Song 保留且
    file_path/quality/file_size 更新为行A 快照。"""
    (app,) = webapp_app
    _set_output_dir(tmp_path)
    mp3 = _make_file(tmp_path / "old.mp3", size=111)
    flac = _make_file(tmp_path / "new.flac", size=222)

    _add_song(file_path=str(flac), file_size=222, quality="lossless")
    _add_task(file_path=str(mp3), quality="standard", file_size=111)
    row_b = _add_task(file_path=str(flac), quality="lossless", file_size=222)

    ok, msg = api_module._delete_song_record(row_b.pk, delete_file=True)

    assert ok, msg
    assert not flac.exists()
    assert mp3.exists()
    # 行A 保留
    assert DownloadTask.query.count() == 1
    assert DownloadTask.query.first().file_path == str(mp3)
    # Song 保留，改指行A 快照
    song = Song.query.filter_by(id="10086").first()
    assert song is not None
    assert song.file_path == str(mp3)
    assert song.quality == "standard"
    assert song.file_size == 111


# ----------------------------------------------------------------------
# 3. 删唯一一行：文件、任务行、Song 行全删
# ----------------------------------------------------------------------
def test_delete_only_row_removes_song_too(webapp_app, tmp_path):
    """唯一一行 + delete_file=True → 文件删、任务行删、Song 行删
    （重新下载不再被"已下载"去重拦截）。"""
    (app,) = webapp_app
    _set_output_dir(tmp_path)
    flac = _make_file(tmp_path / "only.flac", size=222)

    _add_song(file_path=str(flac), file_size=222)
    row = _add_task(file_path=str(flac), quality="lossless", file_size=222)

    ok, msg = api_module._delete_song_record(row.pk, delete_file=True)

    assert ok, msg
    assert not flac.exists()
    assert DownloadTask.query.count() == 0
    assert Song.query.filter_by(id="10086").first() is None


# ----------------------------------------------------------------------
# 4. 存量行（快照为空）：回退删 Song.file_path（旧版行为）
# ----------------------------------------------------------------------
def test_legacy_row_without_snapshot_falls_back_to_song_path(webapp_app, tmp_path):
    """task.file_path=""（存量行）+ delete_file=True → 删 song.file_path。"""
    (app,) = webapp_app
    _set_output_dir(tmp_path)
    flac = _make_file(tmp_path / "legacy.flac", size=222)

    _add_song(file_path=str(flac), file_size=222)
    row = _add_task(file_path="", quality="", file_size=0)

    ok, msg = api_module._delete_song_record(row.pk, delete_file=True)

    assert ok, msg
    assert not flac.exists()
    assert DownloadTask.query.count() == 0
    assert Song.query.filter_by(id="10086").first() is None


# ----------------------------------------------------------------------
# 5. delete_file=False 守护：文件不动、只删选中行（现状不变）
# ----------------------------------------------------------------------
def test_delete_without_file_keeps_files_and_other_rows(webapp_app, tmp_path):
    """delete_file=False → 任何文件都不删，只删选中行，Song 因仍有其他
    done 行引用而保留（守护现状）。"""
    (app,) = webapp_app
    _set_output_dir(tmp_path)
    mp3 = _make_file(tmp_path / "old.mp3", size=111)
    flac = _make_file(tmp_path / "new.flac", size=222)

    _add_song(file_path=str(flac), file_size=222, quality="lossless")
    row_a = _add_task(file_path=str(mp3), quality="standard", file_size=111)
    _add_task(file_path=str(flac), quality="lossless", file_size=222)

    ok, msg = api_module._delete_song_record(row_a.pk, delete_file=False)

    assert ok, msg
    assert mp3.exists()
    assert flac.exists()
    assert DownloadTask.query.count() == 1
    assert DownloadTask.query.first().file_path == str(flac)
    assert Song.query.filter_by(id="10086").first() is not None


# ----------------------------------------------------------------------
# 6. 快照路径在下载目录白名单外：文件拒删、任务行照删
# ----------------------------------------------------------------------
def test_snapshot_outside_output_dir_file_kept_row_deleted(webapp_app, tmp_path):
    """快照指向白名单外 + delete_file=True → 文件保留（拒删），任务行照删。"""
    (app,) = webapp_app
    # 白名单指向 out 子目录，快照文件放在其外
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _set_output_dir(out_dir)
    outside = _make_file(tmp_path / "outside.mp3", size=111)

    _add_song(file_path=str(outside), file_size=111, quality="standard")
    row = _add_task(file_path=str(outside), quality="standard", file_size=111)

    ok, msg = api_module._delete_song_record(row.pk, delete_file=True)

    assert ok, msg
    assert outside.exists()
    assert DownloadTask.query.count() == 0
