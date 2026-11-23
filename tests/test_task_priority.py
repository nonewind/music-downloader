"""优先下载：等待中的任务排到下一首。

- 只对 pending 可用，其它状态返回明确错误
- 不打断正在下载的那首，下完就轮到优先的
- 多次点击时后点的排在最前
- 删除任务时从优先列表里去掉（pk 会被复用）
- 只在内存里，重启后按加入顺序
"""

import pytest

import task_manager
from models import DownloadTask, db


def _task(name, status="pending"):
    t = DownloadTask(platform="netease", song_id=name, song_name=name,
                     artists="歌手", status=status)
    db.session.add(t)
    db.session.commit()
    return t.pk


@pytest.fixture
def tm(webapp_app):
    (app,) = webapp_app
    return task_manager.TaskManager(app)  # 构造不启动线程/调度器


def _run_worker(tm, on_task=None):
    """同步跑 worker 循环，返回处理顺序。

    用替身代替 _process_task：记下 pk、调用 on_task，队列和优先列表都空了就停。
    """
    order = []

    def fake_process(pk):
        order.append(pk)
        if on_task is not None:
            on_task(pk)
        if tm._task_queue.empty() and not tm._next_pks:
            tm._stop_event.set()

    tm._process_task = fake_process
    tm._worker_loop()
    return order


def test_prioritized_task_goes_next(tm):
    a, b, c = _task("A"), _task("B"), _task("C")
    for pk in (a, b, c):
        tm._task_queue.put(pk)

    ok, msg = tm.prioritize_task(c)

    assert (ok, msg) == (True, "已排到下一首")
    order = _run_worker(tm)
    # 原来的队列条目还在，轮到时由 _process_task 的状态守门跳过
    assert order == [c, a, b, c]


def test_does_not_interrupt_current_task(tm):
    a, b, c = _task("A"), _task("B"), _task("C")
    for pk in (a, b, c):
        tm._task_queue.put(pk)

    def on_task(pk):
        # 正在下 A 时点优先下载 C
        if pk == a:
            tm.prioritize_task(c)

    order = _run_worker(tm, on_task)

    assert order[:3] == [a, c, b]


def test_later_click_goes_first(tm):
    a, b, c = _task("A"), _task("B"), _task("C")
    for pk in (a, b, c):
        tm._task_queue.put(pk)

    tm.prioritize_task(b)
    tm.prioritize_task(c)
    # 再点一次 B：挪回最前，不重复
    tm.prioritize_task(b)

    assert tm._next_pks == [b, c]
    order = _run_worker(tm)
    assert order[:3] == [b, c, a]


@pytest.mark.parametrize("status", ["downloading", "paused", "done", "failed", "skipped"])
def test_only_pending_can_be_prioritized(tm, status):
    pk = _task("A", status=status)

    ok, msg = tm.prioritize_task(pk)

    assert ok is False
    assert msg == f"任务当前状态（{status}）不可优先下载"
    assert tm._next_pks == []


def test_missing_task(tm):
    ok, msg = tm.prioritize_task(9999)

    assert (ok, msg) == (False, "任务不存在")


def test_delete_removes_from_priority(tm):
    pk = _task("A")
    tm.prioritize_task(pk)

    ok, _ = tm.delete_task(pk)

    assert ok is True
    assert tm._next_pks == []


def test_done_duplicate_entry_is_skipped(tm):
    # 优先下载完的歌，原队列条目轮到时状态已是 done，真实 _process_task 直接跳过
    pk = _task("A", status="done")
    db.session.commit()

    tm._process_task(pk)

    db.session.expire_all()
    assert db.session.get(DownloadTask, pk).status == "done"


def test_active_tasks_show_prioritized(tm):
    a, b = _task("A"), _task("B")
    tm.prioritize_task(b)

    rows = {r["pk"]: r for r in tm.get_active_tasks()}

    assert rows[a]["prioritized"] is False
    assert rows[b]["prioritized"] is True


def test_priority_not_kept_after_restart(webapp_app):
    (app,) = webapp_app
    a, b = _task("A"), _task("B")
    old = task_manager.TaskManager(app)
    old.prioritize_task(b)

    # 重启：新的 TaskManager 按加入顺序重建队列
    new = task_manager.TaskManager(app)
    new._recover_tasks()

    assert new._next_pks == []
    assert [new._task_queue.get_nowait(), new._task_queue.get_nowait()] == [a, b]


@pytest.fixture
def client(webapp_app):
    from routes.api import api_bp

    (app,) = webapp_app
    app.config["TASK_MANAGER"] = task_manager.TaskManager(app)
    app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


def test_api_prioritize(client):
    pending = _task("A")
    paused = _task("B", status="paused")

    ok = client.post(f"/api/tasks/{pending}/prioritize").get_json()
    bad = client.post(f"/api/tasks/{paused}/prioritize").get_json()
    missing = client.post("/api/tasks/9999/prioritize").get_json()

    assert ok == {"code": 0, "msg": "已排到下一首"}
    assert bad == {"code": 1, "msg": "任务当前状态（paused）不可优先下载"}
    assert missing == {"code": 1, "msg": "任务不存在"}
