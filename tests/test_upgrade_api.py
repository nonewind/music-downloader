"""UPG-02/03 音质升级候选清单与批量入队。

候选规则（find_upgrade_candidates）：
- Song.status=success、文件仍在磁盘
- Song.quality 在 QUALITY_ORDER 内且低于该平台当前音质设置；与设置档位
  映射到同一平台请求值（QQ hires/lossless 同为 flac）的不算可升级
- 无在途任务（pending/downloading/paused）
- 冷却期内（7 天）升级任务被 skipped 的不列入，过期后重新列入
- 当前设置档位在 QUALITY_ORDER 外（如 dolby）→ 无法比较，无候选
"""

from datetime import datetime, timedelta

import pytest

import task_manager
from core.providers import get_provider
from models import DownloadTask, LocalAudioFile, Setting, Song, User, db


def _set(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.session.commit()


def _song(tmp_path, sid, quality, platform="netease", exists=True, status="success",
          name=None, downloaded_at=None):
    path = tmp_path / f"{platform}-{sid}.mp3"
    if exists:
        path.write_bytes(b"x" * 10)
    s = Song(id=sid, platform=platform, name=name or f"歌{sid}", artists="歌手",
             album="专辑", duration_ms=1000, quality=quality, file_path=str(path),
             file_size=10, status=status,
             downloaded_at=downloaded_at or datetime.now())
    db.session.add(s)
    db.session.commit()
    return s


def _task(sid, status, kind="normal", platform="netease", updated_at=None):
    t = DownloadTask(platform=platform, song_id=sid, song_name="x", status=status, kind=kind)
    db.session.add(t)
    db.session.commit()
    if updated_at is not None:
        # onupdate 会覆盖 ORM 赋值，直接改库
        db.session.execute(
            DownloadTask.__table__.update().where(DownloadTask.pk == t.pk)
            .values(updated_at=updated_at))
        db.session.commit()
    return t


def _ids(cands):
    return sorted(s.id for s in cands)


# ----------------------------------------------------------------------
# find_upgrade_candidates
# ----------------------------------------------------------------------
def test_candidates_only_lower_than_setting(webapp_app, tmp_path):
    _set("level_netease", "lossless")
    _song(tmp_path, "1", "standard")
    _song(tmp_path, "2", "exhigh")
    _song(tmp_path, "3", "lossless")       # 同级
    _song(tmp_path, "4", "hires")          # 更高
    _song(tmp_path, "5", "dolby")          # 链外，无法比较
    _song(tmp_path, "6", "")               # 存量无档位
    _song(tmp_path, "7", "standard", exists=False)
    _song(tmp_path, "8", "standard", status="failed")
    _song(tmp_path, "9", "standard", platform="qq")

    target, cands = task_manager.find_upgrade_candidates("netease")
    assert target == "lossless"
    assert _ids(cands) == ["1", "2"]


def test_candidates_falls_back_to_global_level(webapp_app, tmp_path):
    _set("level", "exhigh")
    _song(tmp_path, "1", "standard")
    target, cands = task_manager.find_upgrade_candidates("netease")
    assert target == "exhigh"
    assert _ids(cands) == ["1"]


def test_candidates_empty_when_setting_out_of_chain(webapp_app, tmp_path):
    _set("level_netease", "dolby")
    _song(tmp_path, "1", "standard")
    target, cands = task_manager.find_upgrade_candidates("netease")
    assert target == "dolby"
    assert cands == []


def test_candidates_qq_same_request_tier_excluded(webapp_app, tmp_path):
    """QQ hires 与 lossless 同为 flac：已有 lossless 不算可升级到 hires"""
    _set("level_qq", "hires")
    _song(tmp_path, "a", "lossless", platform="qq")
    _song(tmp_path, "b", "exhigh", platform="qq")
    _song(tmp_path, "c", "higher", platform="qq")
    _, cands = task_manager.find_upgrade_candidates("qq")
    assert _ids(cands) == ["b", "c"]


def test_candidates_exclude_active_tasks(webapp_app, tmp_path):
    _set("level_netease", "lossless")
    for sid, st in (("1", "pending"), ("2", "downloading"), ("3", "paused"), ("4", "done")):
        _song(tmp_path, sid, "standard")
        _task(sid, st)
    _, cands = task_manager.find_upgrade_candidates("netease")
    assert _ids(cands) == ["4"]


def test_candidates_cooldown_after_skipped_upgrade(webapp_app, tmp_path):
    _set("level_netease", "lossless")
    now = datetime.now()
    _song(tmp_path, "recent", "standard")
    _task("recent", "skipped", kind="upgrade", updated_at=now - timedelta(days=2))
    _song(tmp_path, "old", "standard")
    _task("old", "skipped", kind="upgrade", updated_at=now - timedelta(days=8))
    _song(tmp_path, "normal", "standard")
    _task("normal", "skipped", kind="normal", updated_at=now - timedelta(days=1))

    _, cands = task_manager.find_upgrade_candidates("netease", now=now)
    assert _ids(cands) == ["normal", "old"]


def test_candidates_sorted_newest_first_and_filter_by_ids(webapp_app, tmp_path):
    _set("level_netease", "lossless")
    now = datetime.now()
    _song(tmp_path, "1", "standard", downloaded_at=now - timedelta(days=3))
    _song(tmp_path, "2", "standard", downloaded_at=now - timedelta(days=1))
    _song(tmp_path, "3", "standard", downloaded_at=now - timedelta(days=2))
    _, cands = task_manager.find_upgrade_candidates("netease")
    assert [s.id for s in cands] == ["2", "3", "1"]
    _, sub = task_manager.find_upgrade_candidates("netease", song_ids=["1", "3", "nope"])
    assert _ids(sub) == ["1", "3"]


def test_should_skip_upgrade_same_request_tier(webapp_app, tmp_path):
    s = _song(tmp_path, "q", "lossless", platform="qq")
    qk = get_provider("qq").quality_key
    skip, why = task_manager.should_skip_upgrade(s, "hires", quality_key=qk)
    assert skip is True
    assert "无更高音质" in why
    # 不传 quality_key 时按档位序（hires 高于 lossless）放行，保持纯函数默认语义
    assert task_manager.should_skip_upgrade(s, "hires")[0] is False


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@pytest.fixture
def client(webapp_app):
    from routes.api import api_bp
    from task_manager import TaskManager

    (app,) = webapp_app
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TASK_MANAGER"] = TaskManager(app)  # 构造不启动线程/调度器
    app.register_blueprint(api_bp, url_prefix="/api")
    user = User(username="root", is_admin=True, enabled=True)
    user.set_password("admin123")
    db.session.add(user)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = user.id
    return c


def test_api_candidates_with_spec_and_recent(client, tmp_path):
    _set("level_netease", "lossless")
    s = _song(tmp_path, "1", "standard")
    _song(tmp_path, "2", "exhigh")
    db.session.add(LocalAudioFile(path=s.file_path, size=10, mtime=1.0, status="active",
                                  ext="mp3", bitrate_kbps=128, sample_rate=44100,
                                  bits_per_sample=0, lossless=False))
    db.session.commit()
    _song(tmp_path, "9", "lossless")
    _task("9", "done", kind="upgrade")
    _task("8", "skipped", kind="upgrade")
    _task("7", "failed", kind="upgrade")

    body = client.get("/api/upgrade/candidates?platform=netease").get_json()
    assert body["code"] == 0
    data = body["data"]
    assert data["target_level"] == "lossless"
    assert data["total"] == 2
    assert data["cooldown_days"] == 7
    assert data["batch_limit"] == 100
    by_id = {it["song_id"]: it for it in data["items"]}
    assert by_id["1"]["spec"]["bitrate_kbps"] == 128
    assert by_id["1"]["quality"] == "standard"
    assert by_id["2"]["spec"] is None
    assert data["recent"] == {"done": 1, "skipped": 1, "failed": 1, "active": 0}


def test_api_candidates_pagination(client, tmp_path):
    _set("level_netease", "lossless")
    for i in range(5):
        _song(tmp_path, str(i), "standard")
    data = client.get("/api/upgrade/candidates?platform=netease&offset=2&limit=2").get_json()["data"]
    assert data["total"] == 5
    assert len(data["items"]) == 2


def test_api_enqueue_creates_upgrade_tasks(client, tmp_path):
    _set("level_netease", "lossless")
    _song(tmp_path, "1", "standard", name="晴天")
    _song(tmp_path, "2", "lossless")       # 已是目标档：不入队

    body = client.post("/api/upgrade/enqueue",
                       json={"platform": "netease", "song_ids": ["1", "2", "404"]}).get_json()
    assert body["code"] == 0
    assert body["data"] == {"queued": 1, "skipped": 2, "target_level": "lossless"}

    t = DownloadTask.query.filter_by(song_id="1").one()
    assert t.kind == "upgrade"
    assert t.status == "pending"
    assert t.fee == 1
    assert t.song_name == "晴天"
    assert t.playlist_name == "音质升级"

    # 再次提交：在途任务拦截，不重复入队
    again = client.post("/api/upgrade/enqueue",
                        json={"platform": "netease", "song_ids": ["1"]}).get_json()
    assert again["data"]["queued"] == 0
    assert DownloadTask.query.filter_by(song_id="1").count() == 1


def test_api_enqueue_validates_input(client):
    assert client.post("/api/upgrade/enqueue", json={"song_ids": []}).get_json()["code"] == 1
    assert client.post("/api/upgrade/enqueue", json={"song_ids": "1"}).get_json()["code"] == 1
    too_many = [str(i) for i in range(101)]
    body = client.post("/api/upgrade/enqueue", json={"song_ids": too_many}).get_json()
    assert body["code"] == 1
    assert "100" in body["msg"]


def test_api_songs_and_tasks_expose_kind(client, tmp_path):
    _task("1", "done", kind="upgrade")
    _task("2", "pending", kind="upgrade")
    rows = client.get("/api/songs").get_json()["data"]
    assert {r["kind"] for r in rows} == {"upgrade"}
    tasks = client.get("/api/tasks").get_json()["data"]
    assert tasks[0]["kind"] == "upgrade"
