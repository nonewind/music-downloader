"""内置 kugou-api 二进制进程管理（跨平台单例）

负责拉起/停止预编译二进制（kugou_api_win.exe / kugou_api_linux），
对外提供幂等的 start/stop/status。单个进程仅监听 127.0.0.1 端口（默认 45603）。
公共进程管理逻辑见 core/providers/_bridge_base.py，本模块差异点：
- 二进制为 KuGouMusicApi（Node/Express）的 pkg 单文件打包产物，
  监听地址经 PORT/HOST 环境变量控制（与 ncm-api 相同）
- 就绪探测走 GET /（public/index.html 静态页，不碰上游），收到任意 HTTP
  响应即就绪（基类默认）
- 注意：二进制文件名为用户实际放置的下划线风格
  （kugou_api_win.exe / kugou_api_linux），与 ncm-api-* 连字符风格不同，
  改名需同步修改 _BINARIES

2026-09-04 实测（PORT=45699 HOST=127.0.0.1）：
- server.js 正确读取 PORT/HOST，启动日志 "server running @ http://127.0.0.1:45699"
- GET / 返回 HTTP 200；/register/dev、/search、/song/url/new 等主链路全部可用
- /song/url/auth/merge 的 P0 崩溃缺陷在该打包版中已修复（bogus hash 触发
  "本次请求需要验证" 后服务仍存活），client 白名单仍禁调该接口作为二道防线
"""

from .._bridge_base import LOCAL_HOST, ApiBridge, make_singleton_getter

# 平台 -> 二进制文件名（与 api/ 目录实际放置的文件名一致）
_BINARIES = {
    "win32": "kugou_api_win.exe",
    "linux": "kugou_api_linux",
}


class KuGouApiBridge(ApiBridge):
    BINARIES = _BINARIES
    LABEL = "酷狗音乐API"
    MISSING_BINARY_MSG = (
        "未找到酷狗音乐API二进制: {path}\n"
        "请将 kugou-api 对应平台版本放到 api/ 目录"
    )
    LOG_NAME = "kugou-api"

    def _listen_env(self, port: int) -> dict[str, str]:
        # 显式 HOST=127.0.0.1——server.js 中 HOST 缺省为空字符串，
        # 等效监听所有网卡；内置 API 无鉴权，暴露局域网有安全风险
        return {"PORT": str(port), "HOST": LOCAL_HOST}


get_bridge = make_singleton_getter(KuGouApiBridge)
