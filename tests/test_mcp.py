"""MCP 接口测试（webapp/routes/mcp.py）

- 协议：initialize / 通知 / ping / tools/list / tools/call / 未知方法、未知工具
- 工具转给现有 /api 接口执行，接口失败时 isError=True
- MCP 端口（请求带 MCP_PORT_FLAG 标记）不论什么部署方式都必须带访问令牌
- 令牌可放在 Authorization: Bearer 头
"""

import json

import pytest

from models import DownloadTask, Playlist, Setting, db

TOKEN = "s3cret-token-123"
LAN = {"REMOTE_ADDR": "192.168.1.50"}


@pytest.fixture
def make_app(webapp_app, monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)

    def _make(gateway=False):
        from access import init_access_control
        from models import DEFAULT_SETTINGS
        from routes.api import api_bp
        from routes.mcp import mcp_bp
        from task_manager import TaskManager

        (app,) = webapp_app
        for k, v in DEFAULT_SETTINGS.items():
            if not db.session.get(Setting, k):
                db.session.add(Setting(key=k, value=v))
        db.session.commit()
        app.config["TASK_MANAGER"] = TaskManager(app)  # 构造不启动线程/调度器
        init_access_control(app, gateway_mode=gateway)
        app.register_blueprint(api_bp, url_prefix="/api")
        app.register_blueprint(mcp_bp, url_prefix="/mcp")
        return app

    return _make


def _rpc(client, method, params=None, msg_id=1, **kwargs):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg, **kwargs)


def _call(client, name, arguments=None, **kwargs):
    resp = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    return result, json.loads(result["content"][0]["text"])


# ----------------------------------------------------------------------
# 协议
# ----------------------------------------------------------------------
def test_initialize_echoes_supported_version(make_app):
    c = make_app().test_client()
    resp = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["id"] == 1
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"] == {"tools": {}}
    assert body["result"]["serverInfo"]["name"] == "deen-music-downloader"


def test_initialize_unknown_version_gets_latest(make_app):
    from routes.mcp import PROTOCOL_VERSIONS
    c = make_app().test_client()
    body = _rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).get_json()
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]


def test_notification_accepted(make_app):
    c = make_app().test_client()
    resp = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp.status_code == 202
    assert resp.data == b""


def test_ping(make_app):
    c = make_app().test_client()
    assert _rpc(c, "ping").get_json()["result"] == {}


def test_get_not_allowed(make_app):
    c = make_app().test_client()
    assert c.get("/mcp").status_code == 405


def test_bad_json(make_app):
    c = make_app().test_client()
    resp = c.post("/mcp", data="{not json", content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == -32700


def test_unknown_method(make_app):
    c = make_app().test_client()
    assert _rpc(c, "resources/list").get_json()["error"]["code"] == -32601


def test_unknown_tool(make_app):
    c = make_app().test_client()
    body = _rpc(c, "tools/call", {"name": "delete_everything", "arguments": {}}).get_json()
    assert body["error"]["code"] == -32602


def test_tools_list(make_app):
    from routes.mcp import TOOLS
    c = make_app().test_client()
    tools = _rpc(c, "tools/list").get_json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) == len(TOOLS)
    # 只开放查看和下载类操作
    assert not any("delete" in n or "setting" in n for n in names)
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"


def test_every_tool_maps_to_existing_api(make_app):
    """每个工具翻译出的路径都能匹配到 /api 里的接口"""
    from routes.mcp import TOOLS, _tool_request
    app = make_app()
    sample = {"keyword": "k", "song_id": "1", "album_id": "1", "source": "x",
              "task_id": 1, "action": "pause", "account_id": 1, "playlist_id": 1, "dir": "a/b"}
    adapter = app.url_map.bind("localhost")
    for t in TOOLS:
        method, path, _, _ = _tool_request(t["name"], dict(sample))
        endpoint, _ = adapter.match(path, method=method)
        assert endpoint.startswith("api."), t["name"]


# ----------------------------------------------------------------------
# 工具调用
# ----------------------------------------------------------------------
def test_list_playlists(make_app):
    c = make_app().test_client()
    db.session.add(Playlist(id=123, platform="netease", name="我的歌单", type="user"))
    db.session.commit()
    result, data = _call(c, "list_playlists")
    assert result["isError"] is False
    assert [p["name"] for p in data["data"]] == ["我的歌单"]


def test_missing_required_arg(make_app):
    c = make_app().test_client()
    result = _rpc(c, "tools/call", {"name": "search_songs", "arguments": {}}).get_json()["result"]
    assert result["isError"] is True
    assert "keyword" in result["content"][0]["text"]


def test_api_failure_is_tool_error(make_app):
    c = make_app().test_client()
    result, data = _call(c, "control_task", {"task_id": 999, "action": "prioritize"})
    assert result["isError"] is True
    assert data["msg"] == "任务不存在"


def test_bad_int_arg(make_app):
    c = make_app().test_client()
    resp = _rpc(c, "tools/call", {"name": "test_account", "arguments": {"account_id": "abc"}})
    result = resp.get_json()["result"]
    assert result["isError"] is True
    assert "account_id" in result["content"][0]["text"]


def test_control_task_prioritize(make_app):
    app = make_app()
    c = app.test_client()
    task = DownloadTask(song_id="1", song_name="歌", artists="人", platform="netease", status="pending")
    db.session.add(task)
    db.session.commit()
    result, _ = _call(c, "control_task", {"task_id": task.pk, "action": "prioritize"})
    assert result["isError"] is False
    assert task.pk in app.config["TASK_MANAGER"]._next_pks


def test_download_song_forwards_args(make_app, monkeypatch):
    app = make_app()
    c = app.test_client()
    seen = {}

    def fake_download(song_id, name, artists, fee, platform="netease", force=False):
        seen.update(song_id=song_id, name=name, artists=artists, fee=fee, platform=platform, force=force)
        return True

    monkeypatch.setattr(app.config["TASK_MANAGER"], "download_single_song", fake_download)
    result, data = _call(c, "download_song", {"song_id": "003rJSwm3TechU", "name": "晴天",
                                              "artists": "周杰伦", "platform": "qq"})
    assert result["isError"] is False
    assert seen == {"song_id": "003rJSwm3TechU", "name": "晴天", "artists": "周杰伦",
                    "fee": 0, "platform": "qq", "force": False}


def test_list_history_passes_query(make_app):
    c = make_app().test_client()
    db.session.add(DownloadTask(song_id="1", song_name="成功的歌", artists="a", status="done"))
    db.session.add(DownloadTask(song_id="2", song_name="失败的歌", artists="a", status="failed",
                                error_msg="无版权"))
    db.session.commit()
    result, data = _call(c, "list_history", {"status": "failed"})
    assert result["isError"] is False
    assert [r["name"] for r in data["data"]] == ["失败的歌"]


# ----------------------------------------------------------------------
# 访问控制
# ----------------------------------------------------------------------
@pytest.mark.parametrize("gateway", [False, True])
def test_mcp_port_requires_token(make_app, gateway):
    from access import MCP_PORT_FLAG
    c = make_app(gateway=gateway).test_client()
    port = {MCP_PORT_FLAG: True, **LAN}
    # 没设令牌：MCP 端口一律拒绝
    assert _rpc(c, "ping", environ_base=port).status_code == 401
    Setting.set("access_token", TOKEN)
    assert _rpc(c, "ping", environ_base=port).status_code == 401
    assert _rpc(c, "ping", environ_base=port,
                headers={"Authorization": "Bearer wrong-token-1"}).status_code == 401
    ok = _rpc(c, "ping", environ_base=port, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200
    assert _rpc(c, "ping", environ_base=port, headers={"X-Access-Token": TOKEN}).status_code == 200


def test_main_port_bearer_token(make_app):
    c = make_app().test_client()
    Setting.set("access_token", TOKEN)
    assert _rpc(c, "ping", environ_base=LAN).status_code == 401
    assert _rpc(c, "ping", environ_base=LAN,
                headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_mcp_port_setting_is_trusted_only(make_app):
    c = make_app().test_client()
    resp = c.put("/api/settings", json={"mcp_port": "*:45604"}, environ_base=LAN)
    assert resp.status_code == 403
    resp = c.put("/api/settings", json={"mcp_port": "*:45604"})
    assert resp.status_code == 200
    assert "MCP 端口修改需重启服务生效" in resp.get_json()["msg"]
    assert Setting.get("mcp_port") == "*:45604"
