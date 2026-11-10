"""dir_layout 存量迁移（UI-03e 收尾批 ③）：GET /api/settings/migrate-layout 端点测试。

回归背景：migrate_layout.count_pending 曾把模型类 Setting 当 getter 传入
_resolve_output（_resolve_output(Setting) → Setting("output_dir", "downloads")
实例化失败 TypeError），导致端点恒 code:1。修复后应返回 code:0 且
data.needs 为整数；存量旧结构文件（/歌手/歌名）应被计入 needs。
"""

import pytest

from models import Song, Setting, db


@pytest.fixture
def admin_client(tmp_path):
    """带 /api 蓝图的测试 client：内存库（v0.9.0.0 起无登录，直接请求）。

    output_dir 指向 tmp 空目录（绝对路径），避免测试触碰仓库根真实 downloads/。
    """
    from flask import Flask
    from routes.api import api_bp

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        db.create_all()
        # 空目录 + 绝对路径：_resolve_output 不再拼接项目根
        Setting.set("output_dir", str(tmp_path))

    client = app.test_client()
    yield client


def _add_song(file_path: str) -> None:
    """插入一条 success 的 Song 记录（旧结构 /歌手/ 下的文件）。"""
    song = Song(
        id="10086",
        platform="netease",
        name="测试歌曲",
        artists="测试歌手",
        album="测试专辑",
        duration_ms=0,
        quality="exhigh",
        file_path=file_path,
        file_size=1,
        playlist_id=None,
        source_name="",
        status="success",
        account_id=None,
    )
    db.session.add(song)
    db.session.commit()


def test_migrate_layout_get_ok_on_empty_output(admin_client):
    """GET /api/settings/migrate-layout：空输出目录 → code:0 且 needs 为整数。

    修复前：count_pending 内 _resolve_output(Setting) 把模型类当 getter，
    TypeError 被 except 捕获 → 恒 {"code": 1, "msg": ...}。
    """
    resp = admin_client.get("/api/settings/migrate-layout")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 0, body
    assert isinstance(body["data"]["needs"], int)


def test_migrate_layout_get_counts_old_structure(admin_client, tmp_path):
    """GET /api/settings/migrate-layout：旧结构文件（/歌手/歌名.flac）→ needs 计 1。"""
    old_dir = tmp_path / "测试歌手"
    old_dir.mkdir()
    old_file = old_dir / "测试歌手 - 测试歌曲.flac"
    old_file.write_bytes(b"fake flac")

    with admin_client.application.app_context():
        _add_song(str(old_file))

    resp = admin_client.get("/api/settings/migrate-layout")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 0, body
    assert body["data"]["needs"] == 1


def test_migrate_layout_ignores_year_suffixed_album_dir(admin_client, tmp_path):
    """新结构目录「歌手/专辑 (年份)/歌名-ID.flac」不算旧结构 → needs 为 0。

    回归背景：count_pending 把目标目录算成不带年份的「歌手/专辑」，而新
    下载放在「专辑 (年份)」目录，导致每首新下载的歌都被误报为未迁移。
    """
    new_dir = tmp_path / "测试歌手" / "测试专辑 (2024)"
    new_dir.mkdir(parents=True)
    new_file = new_dir / "测试歌曲-10086.flac"
    new_file.write_bytes(b"fake flac")

    with admin_client.application.app_context():
        _add_song(str(new_file))

    resp = admin_client.get("/api/settings/migrate-layout")
    body = resp.get_json()
    assert body["code"] == 0, body
    assert body["data"]["needs"] == 0

    # POST 迁移也不能把它搬回无年份目录
    resp = admin_client.post("/api/settings/migrate-layout")
    body = resp.get_json()
    assert body["code"] == 0, body
    assert new_file.exists()
    assert not (tmp_path / "测试歌手" / "测试专辑").exists()
