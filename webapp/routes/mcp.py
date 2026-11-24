"""MCP 接口：让 AI（如 dsh）通过工具管理下载器

按 MCP 的 Streamable HTTP 方式实现，无状态，只回 JSON：
- POST /mcp   收一条 JSON-RPC 消息：initialize / ping / tools/list / tools/call / 通知
- GET  /mcp   405（不提供服务端推送流，规范允许）

每个工具都转给 /api 里现有的接口函数执行，参数校验和返回内容与网页完全一致。
只开放查看和下载类操作；删除、移动文件、改专辑信息、改设置和账号不开放。

访问控制与 /api 相同（见 access.py）；单独开的 MCP 端口（设置项 mcp_port）
不论什么部署方式都必须带访问令牌。
"""

import json
import logging

from flask import Blueprint, Response, current_app, jsonify, request

from models import PLATFORMS
from version import get_version

mcp_bp = Blueprint("mcp", __name__)
logger = logging.getLogger(__name__)

# 支持的协议版本（新的在前）；客户端要的版本不在表里时回最新版
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")

# JSON-RPC 错误码
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

_PLATFORM_PROP = {
    "type": "string",
    "enum": PLATFORMS,
    "description": "平台：netease 网易云 / qq QQ音乐 / kugou 酷狗音乐，默认 netease",
}

# ----------------------------------------------------------------------
# 工具清单（tools/list 原样返回）
# ----------------------------------------------------------------------
TOOLS = [
    {
        "name": "search_songs",
        "description": "按歌名、歌手或专辑名搜索。type=song 搜单曲，type=album 搜专辑。"
                       "结果里 downloaded 表示已经下过；单曲的 id 用于 download_song，专辑的 id 用于 download_album。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "type": {"type": "string", "enum": ["song", "album"], "description": "默认 song"},
                "platform": _PLATFORM_PROP,
                "limit": {"type": "integer", "description": "每页条数，1-100，默认 20"},
                "offset": {"type": "integer", "description": "跳过前几条，用于翻页，默认 0"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "download_song",
        "description": "下载一首歌，加入下载队列。song_id 来自 search_songs 的结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_id": {"type": "string", "description": "歌曲 ID"},
                "name": {"type": "string", "description": "歌名"},
                "artists": {"type": "string", "description": "歌手"},
                "fee": {"type": "integer", "description": "search_songs 结果里的 fee，原样传回"},
                "platform": _PLATFORM_PROP,
                "force": {"type": "boolean", "description": "已经下过也重新下载，默认 false"},
            },
            "required": ["song_id"],
        },
    },
    {
        "name": "download_album",
        "description": "下载整张专辑，专辑里的歌全部加入下载队列。album_id 来自 search_songs（type=album）的结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "album_id": {"type": "string", "description": "专辑 ID"},
                "album_name": {"type": "string", "description": "专辑名"},
                "platform": _PLATFORM_PROP,
            },
            "required": ["album_id"],
        },
    },
    {
        "name": "add_link",
        "description": "用链接添加：单曲、专辑链接直接下载；歌单链接加入关注并同步。"
                       "可以直接传手机上复制的分享文案或分享短链，平台按链接自动识别。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "链接、分享文案或歌单 ID"},
                "platform": {**_PLATFORM_PROP,
                             "description": "只有 source 是纯数字歌单 ID 时才用到，默认 netease"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "list_downloading",
        "description": "看正在下载和等待中的歌、整批进度、失败记录数。每首歌的 pk 用于 control_task。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "control_task",
        "description": "操作一首在队列里的歌：prioritize 优先下载（排到其它等待中的歌前面），"
                       "pause 暂停，resume 继续。task_id 是 list_downloading 结果里的 pk。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "任务 pk"},
                "action": {"type": "string", "enum": ["prioritize", "pause", "resume"]},
            },
            "required": ["task_id", "action"],
        },
    },
    {
        "name": "retry_failed",
        "description": "重试下载失败的歌。不传 song_ids 就重试全部失败的歌；"
                       "song_ids 是 list_history 结果里的 id。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "song_ids": {"type": "array", "items": {"type": "string"}, "description": "歌曲 ID 列表"},
                "platform": {**_PLATFORM_PROP, "description": "只重试这个平台的歌；不传就是全部平台"},
            },
        },
    },
    {
        "name": "list_history",
        "description": "看下载记录（新的在前，分页）。失败的记录里 error_msg 是失败原因。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success", "failed", "skipped"],
                           "description": "只看成功 / 失败 / 跳过；不传看全部"},
                "keyword": {"type": "string", "description": "按歌名、歌手筛选"},
                "page": {"type": "integer", "description": "第几页，默认 1"},
                "per_page": {"type": "integer", "description": "每页条数，默认 20"},
            },
        },
    },
    {
        "name": "list_accounts",
        "description": "看各平台账号：昵称、会员类型和到期时间、本月下载数、额度、是否启用、上次登录校验时间。"
                       "账号现在能不能用要用 test_account 实测。",
        "inputSchema": {
            "type": "object",
            "properties": {"platform": {**_PLATFORM_PROP, "description": "只看这个平台；不传看全部"}},
        },
    },
    {
        "name": "test_account",
        "description": "实测一个账号能否登录，成功时顺便刷新会员信息。account_id 来自 list_accounts。",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "integer", "description": "账号 id"}},
            "required": ["account_id"],
        },
    },
    {
        "name": "list_playlists",
        "description": "看已关注的歌单：名称、平台、是否自动同步、上次同步时间。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sync_playlist",
        "description": "立即同步一个已关注的歌单，把新歌加入下载队列。playlist_id 来自 list_playlists 的 id。",
        "inputSchema": {
            "type": "object",
            "properties": {"playlist_id": {"type": "integer", "description": "歌单 id"}},
            "required": ["playlist_id"],
        },
    },
    {
        "name": "library_artists",
        "description": "浏览音乐库：所有歌手和他们的专辑（每张专辑的首数、大小、年份、是否全无损）。"
                       "专辑的 dir 用于 library_album。需要先在网页上扫描过音乐库。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "library_album",
        "description": "看音乐库里一张专辑的歌，按曲序排。dir 是 library_artists 结果里专辑的 dir，原样传回。",
        "inputSchema": {
            "type": "object",
            "properties": {"dir": {"type": "string", "description": "歌手文件夹/专辑文件夹"}},
            "required": ["dir"],
        },
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}

# ----------------------------------------------------------------------
# 使用说明：initialize 时作为 instructions 发给 AI；设置页「复制 SKILL」也用这一份
# ----------------------------------------------------------------------
USAGE_GUIDE = """\
这是一台家用音乐下载器，能从网易云、QQ音乐、酷狗音乐下载歌曲，下好的歌自动整理进音乐库。

常见做法：
1. 下载一首歌：先 search_songs 搜，挑出用户要的那首（核对歌名和歌手），再 download_song：结果里的 id 填 song_id，name、artists、fee 原样传，平台和搜索时一致。downloaded 为 true 表示已经下过，除非用户要求重下，否则告诉用户已有即可。
2. 下载整张专辑：search_songs 时传 type=album，找到专辑后 download_album。
3. 用户给的是链接或分享文案：直接 add_link，不用先搜。
4. 看下载进度：list_downloading。要让某首先下、暂停或继续，用 control_task，task_id 填那首歌的 pk。
5. 下载失败：list_history 传 status=failed，error_msg 是失败原因，先把原因告诉用户；能重试的用 retry_failed。
6. 账号：list_accounts 看会员和额度；它不代表账号现在还能用，要确认就用 test_account 实测。账号失效需要用户自己在网页的设置页重新登录。
7. 歌单：list_playlists 看已关注的歌单，sync_playlist 立即同步。
8. 音乐库：library_artists 看有哪些歌手和专辑，library_album 看一张专辑里的歌。

平台：用户说 QQ 音乐用 qq，说酷狗用 kugou，没说就用 netease。

做不到的事：删歌、移动文件、改专辑信息、改设置、加或改账号。用户要做这些时，请用户到网页上操作。

工具返回 isError 时，把 msg 里的原因照实告诉用户，不要自己编原因。
"""


class ToolArgError(Exception):
    """工具参数不对（回给 AI 看，让它改了再调）"""


def _int_arg(args: dict, key: str) -> int:
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ToolArgError(f"{key} 必须是整数")
    try:
        return int(value)
    except ValueError:
        raise ToolArgError(f"{key} 必须是整数") from None


def _tool_request(name: str, args: dict) -> tuple[str, str, dict | None, dict | None]:
    """工具名 + 参数 → 要调用的现有接口：(方法, 路径, JSON 请求体, 查询参数)"""
    if name == "search_songs":
        body = {
            "keyword": args["keyword"],
            "type": args.get("type", "song"),
            "platform": args.get("platform", "netease"),
            "limit": args.get("limit", 20),
            "offset": args.get("offset", 0),
        }
        return "POST", "/api/discover/search", body, None
    elif name == "download_song":
        return "POST", "/api/discover/download-song", args, None
    elif name == "download_album":
        return "POST", "/api/discover/album-download", args, None
    elif name == "add_link":
        return "POST", "/api/link", args, None
    elif name == "list_downloading":
        return "GET", "/api/tasks", None, None
    elif name == "control_task":
        task_id = _int_arg(args, "task_id")
        action = args["action"]
        if action not in ("prioritize", "pause", "resume"):
            raise ToolArgError("action 只能是 prioritize / pause / resume")
        return "POST", f"/api/tasks/{task_id}/{action}", {}, None
    elif name == "retry_failed":
        return "POST", "/api/retry", args, None
    elif name == "list_history":
        return "GET", "/api/songs", None, args
    elif name == "list_accounts":
        return "GET", "/api/accounts", None, args
    elif name == "test_account":
        account_id = _int_arg(args, "account_id")
        return "POST", f"/api/accounts/{account_id}/test", {}, None
    elif name == "list_playlists":
        return "GET", "/api/playlists", None, None
    elif name == "sync_playlist":
        playlist_id = _int_arg(args, "playlist_id")
        return "POST", f"/api/sync/{playlist_id}", {}, None
    elif name == "library_artists":
        return "GET", "/api/library/tree", None, None
    elif name == "library_album":
        return "GET", "/api/library/album", None, {"dir": args["dir"]}
    raise KeyError(name)


def _call_api(method: str, path: str, body: dict | None, query: dict | None) -> tuple[int, dict]:
    """在本进程里直接调用现有 /api 接口函数，返回 (HTTP 状态码, JSON)"""
    # 步骤 1：按路径找到接口函数和路径参数（如 /api/tasks/3/pause 的 pk=3）
    adapter = current_app.url_map.bind("localhost")
    endpoint, view_args = adapter.match(path, method=method)
    view = current_app.view_functions[endpoint]
    # 步骤 2：造一个同样的请求上下文执行接口函数
    with current_app.test_request_context(path, method=method, json=body, query_string=query):
        rv = view(**view_args)
        resp = current_app.make_response(rv)
    return resp.status_code, resp.get_json()


def _call_tool(name: str, args: dict) -> dict:
    """执行一个工具，返回 MCP 的 tools/call 结果"""
    # 步骤 1：必填参数检查（工具名已在上层校验过）
    required = _TOOLS_BY_NAME[name]["inputSchema"].get("required", [])
    missing = [k for k in required if args.get(k) in (None, "")]
    if missing:
        return _tool_text("缺少参数: " + ", ".join(missing), is_error=True)

    # 步骤 2：翻译成现有接口并调用
    try:
        method, path, body, query = _tool_request(name, args)
    except ToolArgError as e:
        return _tool_text(str(e), is_error=True)
    status, data = _call_api(method, path, body, query)

    # 步骤 3：接口的 code 非 0 或 HTTP 出错都算工具失败，原样把返回给 AI 看
    failed = status >= 400 or data["code"] != 0
    logger.info("MCP 工具 %s -> %s %s (%s)", name, method, path, "失败" if failed else "成功")
    return _tool_text(json.dumps(data, ensure_ascii=False), is_error=failed)


def _tool_text(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _rpc_result(msg_id, result: dict):
    return jsonify({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id, code: int, message: str, http_status: int = 200):
    return jsonify({"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": code, "message": message}}), http_status


# ----------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------
@mcp_bp.route("", methods=["GET", "DELETE"])
def mcp_no_stream():
    """不提供服务端推送流，也没有会话可删"""
    return Response(status=405, headers={"Allow": "POST"})


@mcp_bp.route("", methods=["POST"])
def mcp_post():
    # 步骤 1：解析 JSON-RPC 消息（外部输入，只在这里检查一次）
    msg = request.get_json(force=True, silent=True)
    if msg is None:
        return _rpc_error(None, _PARSE_ERROR, "不是合法的 JSON", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _rpc_error(None, _INVALID_REQUEST, "不是合法的 JSON-RPC 请求", 400)
    method = msg["method"]
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(msg.get("id"), _INVALID_PARAMS, "params 必须是对象", 400)

    # 步骤 2：没有 id 的是通知（如 notifications/initialized），收下即可
    if "id" not in msg:
        return Response(status=202)
    msg_id = msg["id"]

    # 步骤 3：按方法处理
    if method == "initialize":
        wanted = params.get("protocolVersion")
        if wanted in PROTOCOL_VERSIONS:
            version = wanted
        else:
            version = PROTOCOL_VERSIONS[0]
        return _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "deen-music-downloader", "version": get_version()},
            "instructions": USAGE_GUIDE,
        })
    elif method == "ping":
        return _rpc_result(msg_id, {})
    elif method == "tools/list":
        return _rpc_result(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        if name not in _TOOLS_BY_NAME:
            return _rpc_error(msg_id, _INVALID_PARAMS, f"没有这个工具: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _rpc_error(msg_id, _INVALID_PARAMS, "arguments 必须是对象")
        return _rpc_result(msg_id, _call_tool(name, args))
    else:
        return _rpc_error(msg_id, _METHOD_NOT_FOUND, f"不支持的方法: {method}")
