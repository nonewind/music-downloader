"""UPG-01 音质升级任务语义：DownloadTask.kind + should_skip_upgrade。

治理决策表（升级任务 kind=upgrade）：
- 无已有 success 记录 / 已有文件已丢失 → 放行（等同普通补下）
- 已有档位或本次档位不在 QUALITY_ORDER（无法确认是提升）→ 跳过
- 本次档位同级或更低 → 跳过（避免白耗额度重下同档文件）
- 本次档位严格更高 → 放行

接入点（_download_with_account 取流成功后）：
- 升级未提升且为接力模式 → 换下一个账号再取流（取流不计额度），全部账号
  都拿不到更高档位才 skipped
- 轮询模式不换号，直接 skipped
- 普通任务（kind=normal）沿用降级保护，同级放行，行为不变
"""

import pytest

import task_manager
from models import Account, DownloadTask, Song, db


def _add_song(file_path, quality, sid="10086", platform="netease"):
    song = Song(
        id=sid, platform=platform, name="测试歌曲", artists="测试歌手",
        album="测试专辑", duration_ms=0, quality=quality, file_path=file_path,
        file_size=1, playlist_id=None, source_name="", status="success",
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()
    return song


def _existing_file(tmp_path, name="song.mp3"):
    p = tmp_path / name
    p.write_bytes(b"fake audio")
    return p


# ----------------------------------------------------------------------
# should_skip_upgrade 纯函数
# ----------------------------------------------------------------------
def test_upgrade_allowed_when_strictly_higher(webapp_app, tmp_path):
    song = _add_song(str(_existing_file(tmp_path)), quality="exhigh")
    assert task_manager.should_skip_upgrade(song, "lossless") == (False, "")


def test_upgrade_skipped_when_same_level(webapp_app, tmp_path):
    song = _add_song(str(_existing_file(tmp_path)), quality="exhigh")
    skip, why = task_manager.should_skip_upgrade(song, "exhigh")
    assert skip is True
    assert "exhigh" in why


def test_upgrade_skipped_when_lower(webapp_app, tmp_path):
    song = _add_song(str(_existing_file(tmp_path)), quality="lossless")
    skip, why = task_manager.should_skip_upgrade(song, "standard")
    assert skip is True
    assert "lossless" in why and "standard" in why


@pytest.mark.parametrize("old, new", [("", "lossless"), ("exhigh", "dolby"), ("sky", "hires")])
def test_upgrade_skipped_when_not_comparable(webapp_app, tmp_path, old, new):
    """链外或空档位无法确认是提升：保守跳过，不消耗额度"""
    song = _add_song(str(_existing_file(tmp_path)), quality=old)
    skip, _ = task_manager.should_skip_upgrade(song, new)
    assert skip is True


def test_upgrade_allowed_when_no_record_or_file_missing(webapp_app, tmp_path):
    assert task_manager.should_skip_upgrade(None, "standard") == (False, "")
    song = _add_song(str(tmp_path / "gone.mp3"), quality="lossless")
    assert task_manager.should_skip_upgrade(song, "standard") == (False, "")


def test_download_task_kind_defaults_to_normal(webapp_app):
    t = DownloadTask(platform="netease", song_id="1", song_name="x", status="pending")
    db.session.add(t)
    db.session.commit()
    assert t.kind == "normal"


# ----------------------------------------------------------------------
# 接入点：_download_with_account
# ----------------------------------------------------------------------
class _PassedGuard(Exception):
    """守卫放行后会进入取元数据阶段，用它截断后续真实下载"""


class _FakeClient:
    def __init__(self, level):
        self.level = level
        self.calls = 0

    def get_song_url_with_fallback(self, sid, level):
        self.calls += 1
        return {"url": "http://example.invalid/a.mp3", "ext": "mp3"}, self.level


@pytest.fixture
def harness(webapp_app, tmp_path, monkeypatch):
    """两个网易云账号 + 一首已下载 exhigh 歌曲 + 一个待处理任务"""
    (app,) = webapp_app
    a1 = Account(platform="netease", name="A1", sort_order=1, enabled=True)
    a2 = Account(platform="netease", name="A2", sort_order=2, enabled=True)
    db.session.add_all([a1, a2])
    db.session.commit()
    _add_song(str(_existing_file(tmp_path)), quality="exhigh", sid="777")

    tm = task_manager.TaskManager(app)  # 构造不启动线程/调度器
    clients = {}
    monkeypatch.setattr(tm, "_get_client_for_account", lambda acc: clients[acc.id])

    def _stop(*_a, **_kw):
        raise _PassedGuard()
    monkeypatch.setattr(task_manager, "_fetch_meta_with_retry", _stop)

    def make_task(kind):
        t = DownloadTask(platform="netease", song_id="777", song_name="测试歌曲",
                         artists="测试歌手", playlist_name="音质升级",
                         status="downloading", kind=kind)
        db.session.add(t)
        db.session.commit()
        return t.pk

    def run(pk, account, switch_on_fail):
        db.session.commit()  # 外层不留开启事务，内层独立会话的写入才可见
        tm._download_with_account(pk, account, "777", "测试歌曲", "测试歌手", None,
                                  "音质升级", "lossless", True, True,
                                  switch_on_fail=switch_on_fail)

    def task_row(pk):
        db.session.expire_all()
        return DownloadTask.query.filter_by(pk=pk).first()

    return {"a1": a1, "a2": a2, "clients": clients, "make_task": make_task,
            "run": run, "task_row": task_row}


def test_upgrade_task_skipped_when_all_accounts_same_level(harness):
    h = harness
    h["clients"][h["a1"].id] = _FakeClient("exhigh")
    h["clients"][h["a2"].id] = _FakeClient("exhigh")
    pk = h["make_task"]("upgrade")

    h["run"](pk, h["a1"], switch_on_fail=True)

    row = h["task_row"](pk)
    assert row.status == "skipped"
    assert "无更高音质" in row.error_msg
    assert "2 个账号" in row.error_msg
    # 两个账号都尝试过取流
    assert h["clients"][h["a1"].id].calls == 1
    assert h["clients"][h["a2"].id].calls == 1
    song = Song.query.filter_by(id="777", platform="netease").first()
    assert song.quality == "exhigh"


def test_upgrade_task_switches_account_to_reach_higher_level(harness):
    h = harness
    h["clients"][h["a1"].id] = _FakeClient("exhigh")
    h["clients"][h["a2"].id] = _FakeClient("lossless")
    pk = h["make_task"]("upgrade")

    with pytest.raises(_PassedGuard):
        h["run"](pk, h["a1"], switch_on_fail=True)
    assert h["clients"][h["a2"].id].calls == 1


def test_upgrade_task_round_robin_does_not_switch(harness):
    h = harness
    h["clients"][h["a1"].id] = _FakeClient("exhigh")
    h["clients"][h["a2"].id] = _FakeClient("lossless")
    pk = h["make_task"]("upgrade")

    h["run"](pk, h["a1"], switch_on_fail=False)

    assert h["task_row"](pk).status == "skipped"
    assert h["clients"][h["a2"].id].calls == 0


def test_normal_task_same_level_still_allowed(harness):
    """普通 force 重下：同级放行，行为与 UPG-01 前一致"""
    h = harness
    h["clients"][h["a1"].id] = _FakeClient("exhigh")
    pk = h["make_task"]("normal")

    with pytest.raises(_PassedGuard):
        h["run"](pk, h["a1"], switch_on_fail=True)
