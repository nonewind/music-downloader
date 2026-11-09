"""访问控制（v0.9.0.0 移除登录体系后的最小防护）

无登录后，任何能连上 Web 端口的人都能调用全部 /api/*。本模块提供三层防护，
按部署方式各取所需：

1. 监听地址：新装默认 ``127.0.0.1:45600``（见 models.DEFAULT_SETTINGS），
   只有本机可访问；Docker 镜像通过 ``WEB_PORT_DEFAULT`` 环境变量把新装默认值
   改为 ``0.0.0.0:45600``（容器内必须监听全部网卡）。存量库里已保存的
   ``web_port`` 不做任何改动。
2. 可选访问令牌：设置了令牌（环境变量 ``ACCESS_TOKEN`` 优先，否则数据库
   ``access_token`` 设置项）后，除静态资源与 ``/healthz`` 外的所有请求都必须
   携带令牌：首次访问 ``/?token=<令牌>`` 写入 HttpOnly cookie，之后自动携带；
   脚本也可用 ``X-Access-Token`` 请求头。未设置令牌时不拦截（向后兼容）。
3. 敏感操作仅限可信来源：导出账号（含全部平台 Cookie）、修改自定义 API 地址
   （全部账号 Cookie 会发往该地址）、修改监听地址/下载目录/访问令牌，
   要求请求来自 本机回环 / 飞牛网关 / 持有有效令牌。

飞牛 fnOS 网关模式（FNNAS_GATEWAY_SOCKET）只监听 Unix Socket，所有请求都已
经过 NAS 网关自身的认证，视为可信，令牌校验也一并跳过。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from urllib.parse import urlencode

from flask import Flask, Response, current_app, jsonify, redirect, request

TOKEN_SETTING_KEY = "access_token"
TOKEN_ENV = "ACCESS_TOKEN"
TOKEN_COOKIE = "deen_access"
TOKEN_HEADER = "X-Access-Token"
TOKEN_QUERY = "token"

# 无需令牌即可访问的路径（相对 SCRIPT_NAME）
_EXEMPT_PREFIXES = ("/static/",)
_EXEMPT_PATHS = {"/healthz", "/favicon.ico"}

_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


# ----------------------------------------------------------------------
# 令牌
# ----------------------------------------------------------------------
def get_access_token() -> str:
    """当前生效的访问令牌；空串表示未启用（需在 app context 内调用）"""
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env
    from models import Setting
    try:
        return (Setting.get(TOKEN_SETTING_KEY, "") or "").strip()
    except Exception:            # 表未建等极端情况：按未启用处理，不阻断请求
        return ""


def token_from_env() -> bool:
    return bool((os.environ.get(TOKEN_ENV) or "").strip())


def _cookie_value(token: str) -> str:
    """cookie 存令牌的摘要而非明文（cookie 泄露不等于令牌泄露）"""
    return hashlib.sha256(("deen-access:" + token).encode("utf-8")).hexdigest()


def _request_has_valid_token(token: str) -> bool:
    if not token:
        return False
    presented = request.headers.get(TOKEN_HEADER) or request.args.get(TOKEN_QUERY) or ""
    if presented and hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
        return True
    cookie = request.cookies.get(TOKEN_COOKIE) or ""
    return bool(cookie) and hmac.compare_digest(cookie, _cookie_value(token))


# ----------------------------------------------------------------------
# 来源判定
# ----------------------------------------------------------------------
def is_gateway_mode() -> bool:
    return bool(current_app.config.get("GATEWAY_MODE"))


def _is_loopback_addr(addr: str | None) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback


def _host_is_loopback_name() -> bool:
    """Host 头必须是回环名：防 DNS rebinding（恶意网页把自有域名解析到
    127.0.0.1 后，以本机浏览器为跳板读取导出接口）"""
    host = (request.host or "").strip().lower()
    if host.startswith("["):
        name = host.split("]", 1)[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in _LOOPBACK_HOSTNAMES


def is_loopback_request() -> bool:
    return _is_loopback_addr(request.remote_addr) and _host_is_loopback_name()


def is_trusted_request() -> bool:
    """敏感操作的放行条件：飞牛网关 / 有效令牌 / 本机回环"""
    if is_gateway_mode():
        return True
    if _request_has_valid_token(get_access_token()):
        return True
    return is_loopback_request()


def forbidden_untrusted(action: str):
    """敏感操作被拒时的统一响应（JSON，403）"""
    return jsonify({
        "code": 1,
        "msg": (f"{action}仅允许在本机（127.0.0.1）访问时操作，或先设置访问令牌"
                f"（环境变量 {TOKEN_ENV} 或设置项 {TOKEN_SETTING_KEY}）后携带令牌访问"),
    }), 403


# ----------------------------------------------------------------------
# 全局拦截
# ----------------------------------------------------------------------
_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要访问令牌</title>
<style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px}}
input,button{{font-size:16px;padding:8px;width:100%;box-sizing:border-box;margin-top:8px}}</style>
</head><body>
<h3>Deen 音乐下载器</h3>
<p>本服务已启用访问令牌，请输入令牌继续。</p>
<form method="get" action="{action}">
<input type="password" name="token" placeholder="访问令牌" autofocus required>
<button type="submit">进入</button>
</form>
</body></html>
"""


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _check_access():
    if is_gateway_mode():
        return None
    path = request.path or "/"
    if _is_exempt(path):
        return None
    token = get_access_token()
    if not token:
        return None
    if not _request_has_valid_token(token):
        if path.startswith("/api/"):
            return jsonify({"code": 1, "msg": "未授权：需要访问令牌"}), 401
        action = request.script_root + path
        return Response(_LOGIN_PAGE.format(action=action), status=401,
                        mimetype="text/html")
    # 页面 GET 带 ?token=：写 cookie 后重定向去掉 URL 中的令牌
    # （避免令牌留在地址栏/浏览器历史/Referer 里）
    if request.method == "GET" and TOKEN_QUERY in request.args and not path.startswith("/api/"):
        rest = [(k, v) for k, v in request.args.items(multi=True) if k != TOKEN_QUERY]
        target = request.script_root + path + (("?" + urlencode(rest)) if rest else "")
        resp = redirect(target, code=302)
        set_token_cookie(resp, token)
        return resp
    return None


def set_token_cookie(resp, token: str) -> None:
    resp.set_cookie(
        TOKEN_COOKIE, _cookie_value(token),
        max_age=400 * 24 * 3600, httponly=True, samesite="Lax",
        secure=request.is_secure, path=(request.script_root or "") + "/",
    )


def init_access_control(app: Flask, gateway_mode: bool = False) -> None:
    """注册全局访问控制（app.py 与测试共用）"""
    app.config["GATEWAY_MODE"] = bool(gateway_mode)
    app.before_request(_check_access)


def is_public_bind(host: str) -> bool:
    """监听地址是否对外（非回环）"""
    if host in ("localhost",):
        return False
    return not _is_loopback_addr(host)


def parse_web_bind(raw: str) -> tuple:
    """解析监听地址字符串 → (host, port)

    支持 "host:port"、":port"、"port"、"*:port"；"*"/空 host 与纯端口号表示
    全部网卡（与历史行为一致，存量配置语义不变）。
    """
    raw = str(raw or "").strip()
    if ":" not in raw:
        try:
            return ("0.0.0.0", int(raw))
        except (TypeError, ValueError):
            return ("127.0.0.1", 45600)
    host, _, port_str = raw.rpartition(":")
    if host in ("", "*"):
        host = "0.0.0.0"
    elif host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return (host, int(port_str))
    except (TypeError, ValueError):
        return (host, 45600)
