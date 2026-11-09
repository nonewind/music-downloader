"""声音内容去重回退（v0.9.0.0）测试。

场景：同一首歌被不同专辑/歌单收录为不同 song_id，(id, platform) 去重键
拦不住，下载完成后按"声音部分"指纹识别（指纹本身见 test_audio_fingerprint）。

覆盖：
- _find_md5_duplicate 纯函数层：
  同内容命中、标签/封面不同（大小不同）仍命中、声音不同不命中、
  非音频扩展名不参与、自身路径排除、缓存回写
- _download_with_account 接入层（test_upgrade_task.py harness 同款手法，
  downloader/取元数据打桩）：
  命中回退（新文件删除 + 空目录清理 + 任务 skipped + Song 指向库内文件）
  未命中正常完成（done）、开关关闭不去重、同 song_id 自身文件不放行误判
"""

from types import SimpleNamespace

import pytest

import task_manager
from models import Account, DownloadTask, FileMd5Cache, Setting, Song, db
from test_audio_fingerprint import make_flac, tag_flac


# ----------------------------------------------------------------------
# 纯函数层：_compute_file_md5 / _find_md5_duplicate
# ----------------------------------------------------------------------
def test_find_duplicate_hits_same_content(webapp_app, tmp_path):
    out = tmp_path / "dl"
    (out / "歌手A").mkdir(parents=True)
    old = out / "歌手A" / "old.mp3"
    old.write_bytes(b"same-bytes-123")
    new = out / "new.mp3"
    new.write_bytes(b"same-bytes-123")

    with app_context(webapp_app):
        hit = task_manager._find_md5_duplicate(new, out)
    assert hit == old


def test_find_duplicate_hits_same_audio_different_tags(webapp_app, tmp_path):
    """《千里之外》场景：专辑名/封面不同、文件大小不同，声音相同 → 命中"""
    out = tmp_path / "dl"
    (out / "周杰伦" / "依然范特西").mkdir(parents=True)
    (out / "周杰伦" / "闪亮2006").mkdir(parents=True)
    old = make_flac(out / "周杰伦" / "依然范特西" / "千里之外.flac")
    new = make_flac(out / "周杰伦" / "闪亮2006" / "千里之外-000nLX.flac")
    tag_flac(old, "依然范特西", b"\xff\xd8" + b"A" * 5000)
    tag_flac(new, "闪亮2006 Hit FM年度百首单曲票选", b"\xff\xd8" + b"B" * 800)
    assert old.stat().st_size != new.stat().st_size

    with app_context(webapp_app):
        assert task_manager._find_md5_duplicate(new, out) == old


def test_find_duplicate_different_audio_not_hit(webapp_app, tmp_path):
    out = tmp_path / "dl"
    out.mkdir()
    make_flac(out / "a.flac", md5=b"\x01" * 16)
    new = make_flac(out / "b.flac", md5=b"\x02" * 16)
    with app_context(webapp_app):
        assert task_manager._find_md5_duplicate(new, out) is None
        # 两个音频文件都已进缓存，供后续下载比对
        paths = {r.path for r in FileMd5Cache.query.all()}
        assert {str(out / "a.flac"), str(new)} <= paths


def test_find_duplicate_ignores_non_audio_files(webapp_app, tmp_path):
    """同字节的 .lrc/.jpg/.part 不参与比对"""
    out = tmp_path / "dl"
    out.mkdir()
    for name in ("x.lrc", "cover.jpg", "y.mp3.part"):
        (out / name).write_bytes(b"same-bytes")
    new = out / "new.mp3"
    new.write_bytes(b"same-bytes")
    with app_context(webapp_app):
        assert task_manager._find_md5_duplicate(new, out) is None


def test_find_duplicate_excludes_self_and_caches(webapp_app, tmp_path):
    """唯一同内容文件是自身时不命中；缓存按 (size, mtime) 失效"""
    out = tmp_path / "dl"
    out.mkdir()
    f = out / "self.mp3"
    f.write_bytes(b"only-me")

    with app_context(webapp_app):
        assert task_manager._find_md5_duplicate(f, out) is None
        row = FileMd5Cache.query.filter_by(path=str(f)).first()
        assert row is not None and row.md5

        # 文件变化后旧行失效重算，新值正确
        f.write_bytes(b"changed-content")
        row.size, row.mtime = 1, 1.0     # 伪造过期缓存
        db.session.commit()
        assert task_manager._find_md5_duplicate(f, out) is None
        db.session.expire_all()
        row = FileMd5Cache.query.filter_by(path=str(f)).first()
        import hashlib
        assert row.md5 == "raw:" + hashlib.md5(b"changed-content").hexdigest()


def app_context(webapp_app):
    (app,) = webapp_app
    return app.app_context()


# ----------------------------------------------------------------------
# 接入层：_download_with_account（downloader/取元数据打桩）
# ----------------------------------------------------------------------
class _FakeClient:
    def __init__(self, level="lossless"):
        self.level = level

    def get_song_url_with_fallback(self, sid, level):
        return {"url": "http://example.invalid/a.mp3", "ext": "mp3"}, self.level


class _StubDownloader:
    """download() 直接把指定内容写到目标路径（模拟真实写盘）"""

    def __init__(self, out_root, content):
        self.output_dir = out_root
        self.content = content
        self.calls = 0

    def download(self, url, sub_dir, filename, expected_size=None,
                 progress_callback=None, abort_check=None, protected_suffix=""):
        self.calls += 1
        target = self.output_dir / sub_dir / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content)
        return SimpleNamespace(path=target, produced=True)


@pytest.fixture
def dedup_harness(webapp_app, tmp_path, monkeypatch):
    """网易云账号 + 打桩 downloader/取元数据，直呼 _download_with_account"""
    (app,) = webapp_app
    acc = Account(platform="netease", name="A1", sort_order=1, enabled=True)
    db.session.add(acc)
    db.session.commit()

    out = tmp_path / "dl"
    out.mkdir()
    Setting.set("output_dir", str(out))

    tm = task_manager.TaskManager(app)
    stub = {}

    def _set_content(content):
        stub["downloader"] = _StubDownloader(out, content)
        return stub["downloader"]

    monkeypatch.setattr(tm, "_get_client_for_account", lambda a: _FakeClient())
    monkeypatch.setattr(tm, "_get_downloader",
                        lambda: (stub["downloader"], False, "artist_album"))
    monkeypatch.setattr(task_manager, "write_tags", lambda *a, **k: None)
    monkeypatch.setattr(task_manager, "_fetch_meta_with_retry", lambda *a, **k: (
        {"album": "专辑B", "duration_ms": 0, "cover_url": "", "year": "", "title": "测试歌曲"},
        "", "",
    ))

    def make_task(sid="777"):
        t = DownloadTask(platform="netease", song_id=sid, song_name="测试歌曲",
                         artists="测试歌手", playlist_name="搜索单曲",
                         status="downloading")
        db.session.add(t)
        db.session.commit()
        return t.pk

    def run(pk, sid="777", md5_dedup=True):
        db.session.commit()  # 外层不留开启事务，内层独立会话写入才可见
        tm._download_with_account(pk, acc, sid, "测试歌曲", "测试歌手", None,
                                  "搜索单曲", "lossless", True, True,
                                  switch_on_fail=False, md5_dedup=md5_dedup)

    def task_row(pk):
        db.session.expire_all()
        return DownloadTask.query.filter_by(pk=pk).first()

    return {"app": app, "out": out, "set_content": _set_content,
            "make_task": make_task, "run": run, "task_row": task_row}


def test_download_rolls_back_on_duplicate(dedup_harness):
    """库内已有同内容文件（另一 song_id）→ 回退：删新文件/空目录、skipped、Song 指向旧文件"""
    h = dedup_harness
    # 库内既有文件：另一首歌 888，放在不同的目录树下
    existing_dir = h["out"] / "歌手X" / "专辑X"
    existing_dir.mkdir(parents=True)
    existing = existing_dir / "测试歌曲-888.mp3"
    existing.write_bytes(b"duplicate-content")
    db.session.add(Song(
        id="888", platform="netease", name="测试歌曲", artists="歌手X",
        album="专辑X", duration_ms=0, quality="lossless",
        file_path=str(existing), file_size=len(b"duplicate-content"),
        status="success"))
    db.session.commit()

    dl = h["set_content"](b"duplicate-content")
    pk = h["make_task"]("777")
    h["run"](pk, "777")

    row = h["task_row"](pk)
    assert row.status == "skipped"
    assert "声音内容与库内重复" in row.error_msg and "888" not in row.error_msg
    # 本次产出文件与新建空目录均已清理
    assert not (h["out"] / "测试歌手" / "专辑B").exists()
    # Song 777 指向库内已有文件（防下次同步重复下载）
    song = Song.query.filter_by(id="777", platform="netease").first()
    assert song is not None and song.status == "success"
    assert song.file_path == str(existing)


def test_download_completes_when_content_unique(dedup_harness):
    h = dedup_harness
    h["set_content"](b"unique-bytes")
    pk = h["make_task"]("777")
    h["run"](pk, "777")

    assert h["task_row"](pk).status == "done"
    song = Song.query.filter_by(id="777", platform="netease").first()
    assert song.file_path.endswith("测试歌曲-777.mp3")
    assert song.file_path == str(h["out"] / "测试歌手" / "专辑B" / "测试歌曲-777.mp3")


def test_download_skips_dedup_when_disabled(dedup_harness):
    """开关关闭：同内容也不回退，照常完成"""
    h = dedup_harness
    existing_dir = h["out"] / "歌手X"
    existing_dir.mkdir(parents=True)
    (existing_dir / "测试歌曲-888.mp3").write_bytes(b"duplicate-content")

    h["set_content"](b"duplicate-content")
    pk = h["make_task"]("777")
    h["run"](pk, "777", md5_dedup=False)

    assert h["task_row"](pk).status == "done"


def test_dedup_self_path_not_duplicate(dedup_harness):
    """force 重下同一 song_id：同字节写到同一路径，不得把自己判成重复"""
    h = dedup_harness
    target = h["out"] / "测试歌手" / "专辑B" / "测试歌曲-777.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"same-as-before")
    db.session.add(Song(
        id="777", platform="netease", name="测试歌曲", artists="测试歌手",
        album="专辑B", duration_ms=0, quality="lossless",
        file_path=str(target), file_size=len(b"same-as-before"),
        status="success"))
    db.session.commit()

    h["set_content"](b"same-as-before")
    pk = h["make_task"]("777")
    h["run"](pk, "777")

    assert h["task_row"](pk).status == "done"
    # 文件仍在（未被回退删除）
    assert target.exists()


def test_download_rolls_back_flac_with_different_tags(dedup_harness, tmp_path):
    """接入层：新下载的 FLAC 与库内文件只差专辑名/封面 → 仍回退"""
    h = dedup_harness
    existing_dir = h["out"] / "歌手X" / "原版专辑"
    existing_dir.mkdir(parents=True)
    existing = make_flac(existing_dir / "测试歌曲.flac")
    tag_flac(existing, "原版专辑", b"A" * 4000)

    src = make_flac(tmp_path / "src.flac")
    tag_flac(src, "合辑", b"B" * 300)
    h["set_content"](src.read_bytes())
    pk = h["make_task"]("777")
    h["run"](pk, "777")

    row = h["task_row"](pk)
    assert row.status == "skipped"
    assert existing.exists()
    song = Song.query.filter_by(id="777", platform="netease").first()
    assert song.file_path == str(existing)
