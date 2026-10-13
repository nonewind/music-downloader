"""无启用账号时的失败原因：应提示未配置账号，而不是「月额度已满」。

any_monthly_available 对「无账号」与「全部额度满」同返回 False，
_process_task 须先区分无账号场景，否则误导用户去调额度配置。
"""

import task_manager
from models import Account, DownloadTask, db


def _pending_task(platform="netease"):
    t = DownloadTask(platform=platform, song_id="5001", song_name="测试歌曲",
                     artists="测试歌手", playlist_name="搜索单曲", status="pending")
    db.session.add(t)
    db.session.commit()
    return t.pk


def _run(app, pk):
    tm = task_manager.TaskManager(app)  # 构造不启动线程/调度器
    db.session.commit()  # 外层不留开启事务，内层独立会话的写入才可见
    tm._process_task(pk)
    db.session.expire_all()
    return DownloadTask.query.filter_by(pk=pk).first()


def test_no_account_reports_unconfigured(webapp_app):
    (app,) = webapp_app
    row = _run(app, _pending_task("qq"))
    assert row.status == "failed"
    assert "未配置" in row.error_msg
    assert "QQ音乐" in row.error_msg
    assert "月额度" not in row.error_msg


def test_disabled_accounts_count_as_unconfigured(webapp_app):
    (app,) = webapp_app
    db.session.add(Account(platform="netease", name="停用号", enabled=False))
    db.session.commit()
    row = _run(app, _pending_task("netease"))
    assert "未配置" in row.error_msg


def test_quota_exhausted_message_unchanged(webapp_app, monkeypatch):
    """有启用账号但全部月额度满：保持原文案"""
    (app,) = webapp_app
    db.session.add(Account(platform="netease", name="满额号", enabled=True, quota_limit=1))
    db.session.commit()
    monkeypatch.setattr(task_manager.AccountSelector, "is_quota_exceeded", lambda self, aid: True)
    row = _run(app, _pending_task("netease"))
    assert row.status == "failed"
    assert "月额度已满" in row.error_msg
