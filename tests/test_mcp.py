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
    assert "MCP 端口未开启：需要先设置访问令牌" in resp.get_json()["msg"]
    assert Setting.get("mcp_port") == "*:45604"


# ----------------------------------------------------------------------
# 使用说明
# ----------------------------------------------------------------------
def test_initialize_sends_usage_guide(make_app):
    from routes.mcp import TOOLS, USAGE_GUIDE
    c = make_app().test_client()
    body = _rpc(c, "initialize", {"protocolVersion": "2025-06-18"}).get_json()
    assert body["result"]["instructions"] == USAGE_GUIDE
    # 说明里每个工具都讲到了
    for t in TOOLS:
        assert t["name"] in USAGE_GUIDE, t["name"]


def test_settings_page_gets_usage_guide(make_app, monkeypatch):
    """设置页「复制 SKILL」用的说明和 MCP 发给 AI 的是同一份"""
    import routes.views as views
    from routes.mcp import USAGE_GUIDE
    seen = {}
    monkeypatch.setattr(views, "render_template", lambda name, **kw: seen.update(kw) or "")
    app = make_app()
    app.register_blueprint(views.views_bp)
    assert app.test_client().get("/settings").status_code == 200
    assert seen["mcp_guide"] == USAGE_GUIDE


# ----------------------------------------------------------------------
# MCP 端口默认只让内网连
# ----------------------------------------------------------------------
PUBLIC = {"REMOTE_ADDR": "114.114.114.114"}


@pytest.mark.parametrize("addr,lan", [
    ("192.168.1.50", True), ("10.0.0.2", True), ("172.16.3.4", True), ("127.0.0.1", True),
    ("::1", True), ("fe80::1", True), ("fd00::5", True), ("::ffff:192.168.1.9", True),
    ("114.114.114.114", False), ("8.8.8.8", False), ("2001:4860::8888", False), ("", False),
])
def test_is_lan_addr(addr, lan):
    from access import is_lan_addr
    assert is_lan_addr(addr) is lan


@pytest.mark.parametrize("gateway", [False, True])
def test_mcp_port_rejects_public_by_default(make_app, gateway):
    from access import MCP_PORT_FLAG
    c = make_app(gateway=gateway).test_client()
    Setting.set("access_token", TOKEN)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    # 外网来的，带对令牌也拒绝
    resp = _rpc(c, "ping", environ_base={MCP_PORT_FLAG: True, **PUBLIC}, headers=auth)
    assert resp.status_code == 403
    assert resp.get_json()["msg"] == "MCP 端口只允许内网连接"
    # 内网来的照常
    assert _rpc(c, "ping", environ_base={MCP_PORT_FLAG: True, **LAN}, headers=auth).status_code == 200
    # 用户允许外网后放行，但仍要令牌
    Setting.set("mcp_allow_public", "true")
    assert _rpc(c, "ping", environ_base={MCP_PORT_FLAG: True, **PUBLIC}, headers=auth).status_code == 200
    assert _rpc(c, "ping", environ_base={MCP_PORT_FLAG: True, **PUBLIC}).status_code == 401


def test_main_port_not_affected_by_lan_rule(make_app):
    """主端口不看来源地址（规则只管 MCP 端口）"""
    c = make_app(gateway=True).test_client()
    assert _rpc(c, "ping", environ_base=PUBLIC).status_code == 200


def test_mcp_allow_public_only_trusted_can_enable(make_app):
    c = make_app().test_client()
    # 局域网未带令牌：打开被拒
    resp = c.put("/api/settings", json={"mcp_allow_public": True}, environ_base=LAN)
    assert resp.status_code == 403
    # 本机可以打开
    assert c.put("/api/settings", json={"mcp_allow_public": True}).status_code == 200
    assert Setting.get("mcp_allow_public") == "true"
    # 关闭不算敏感变更，局域网也能关
    assert c.put("/api/settings", json={"mcp_allow_public": False}, environ_base=LAN).status_code == 200
    assert Setting.get("mcp_allow_public") == "false"


def test_settings_returns_lan_ip(make_app, monkeypatch):
    import routes.api as api
    monkeypatch.setattr(api, "lan_ipv4", lambda: "192.168.1.20")
    c = make_app().test_client()
    data = c.get("/api/settings").get_json()["data"]
    assert data["lan_ip"] == "192.168.1.20"
    assert data["mcp_allow_public"] == "false"


# ----------------------------------------------------------------------
# MCP 端口保存后立即生效
# ----------------------------------------------------------------------
@pytest.fixture
def close_mcp_port():
    yield
    import routes.mcp as mcp
    if mcp._port_server is not None:
        mcp._port_server.shutdown()
        mcp._port_server.server_close()
        mcp._port_server = None


def _ping_port(port, token):
    import urllib.request
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return resp.status


def test_mcp_port_applies_on_save(make_app, close_mcp_port):
    import routes.mcp as mcp
    c = make_app().test_client()

    # 步骤 1：先填端口、没令牌：不开
    resp = c.put("/api/settings", json={"mcp_port": "127.0.0.1:0"})
    assert "MCP 端口未开启：需要先设置访问令牌" in resp.get_json()["msg"]
    assert mcp._port_server is None

    # 步骤 2：设令牌：立刻开，能连上
    resp = c.put("/api/settings/access-token", json={"token": TOKEN})
    assert resp.get_json()["msg"] == "访问令牌已设置；MCP 端口已开启"
    port = mcp._port_server.server_port
    assert _ping_port(port, TOKEN) == 200

    # 步骤 3：换端口：旧的关掉，新的开
    old = mcp._port_server
    resp = c.put("/api/settings", json={"mcp_port": "127.0.0.1:0"}, headers={"X-Access-Token": TOKEN})
    assert "MCP 端口" not in resp.get_json()["msg"]          # 值没变，不动端口
    Setting.set("mcp_port", "")
    resp = c.put("/api/settings", json={"mcp_port": "localhost:0"}, headers={"X-Access-Token": TOKEN})
    assert "MCP 端口已开启" in resp.get_json()["msg"]
    assert mcp._port_server is not old

    # 步骤 4：清空端口：关掉
    resp = c.put("/api/settings", json={"mcp_port": ""}, headers={"X-Access-Token": TOKEN})
    assert "MCP 端口已关闭" in resp.get_json()["msg"]
    assert mcp._port_server is None


def test_clearing_token_closes_mcp_port(make_app, close_mcp_port):
    import routes.mcp as mcp
    c = make_app().test_client()
    Setting.set("mcp_port", "127.0.0.1:0")
    c.put("/api/settings/access-token", json={"token": TOKEN})
    assert mcp._port_server is not None
    resp = c.put("/api/settings/access-token", json={"token": ""}, headers={"X-Access-Token": TOKEN})
    assert resp.get_json()["msg"] == "访问令牌已关闭；MCP 端口未开启：需要先设置访问令牌"
    assert mcp._port_server is None
