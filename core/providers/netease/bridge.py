"""内置 NeteaseCloudMusicApi-enhanced 二进制进程管理（跨平台单例）

负责拉起/停止官方预编译二进制（ncm-api-win-x64.exe / ncm-api-linux-x64），
对外提供幂等的 start/stop/status。单个进程仅监听 127.0.0.1 随机空闲端口。
公共进程管理逻辑见 core/providers/_bridge_base.py。

此模块为基础设施层，不经过 Provider 抽象。
"""

import logging
import os
from pathlib import Path

from .._bridge_base import LOCAL_HOST, ApiBridge, make_singleton_getter

logger = logging.getLogger(__name__)

# 平台 -> 二进制文件名
_BINARIES = {
    "win32": "ncm-api-win-x64.exe",
    "linux": "ncm-api-linux-x64",
}


class NodeBridge(ApiBridge):
    BINARIES = _BINARIES
    LABEL = "API "
    MISSING_BINARY_MSG = (
        "未找到 API 二进制: {path}\n"
        "请从官方 Release 下载对应平台版本放到 source/api/ 目录"
    )
    LOG_NAME = "ncm-api"
    # 本次 start 使用的持久 TMPDIR（None=沿用系统 TMPDIR）
    _ncm_tmp: Path | None = None

    def _prepare_runtime(self) -> Path:
        # 匿名令牌持久化（fpk 部署）：ncm-api 把 anonymous_token 存在
        # os.tmpdir()，require 时读入内存且所有上游请求自动携带 MUSIC_A。
        # fpk 的 TRIM_PKGTMP 可能被系统清理，或首启（网络未就绪）刷新
        # 失败导致令牌为空——扫码 check 将恒返回 801。改用 APP_DATA_DIR
        # 下持久 tmp 目录；仅覆盖本子进程环境，主进程自身解压用的
        # TMPDIR（生命周期脚本注入）不受影响。
        self._ncm_tmp = None
        app_data = os.environ.get("APP_DATA_DIR")
        if app_data:
            ncm_tmp = Path(app_data).expanduser() / "tmp"
            try:
                ncm_tmp.mkdir(parents=True, exist_ok=True)
                self._ncm_tmp = ncm_tmp
            except OSError:
                logger.warning("ncm-api 持久 tmp 目录创建失败，沿用系统 TMPDIR: %s", ncm_tmp)
        return self.bin_dir

    def _listen_env(self, port: int) -> dict[str, str]:
        # 显式 HOST=127.0.0.1——server.js 中 HOST 缺省为空字符串，
        # 等效监听所有网卡；内置 API 无鉴权，暴露局域网有安全风险
        env = {"PORT": str(port), "HOST": LOCAL_HOST}
        if self._ncm_tmp:
            env["TMPDIR"] = str(self._ncm_tmp)
        return env

    # _is_ready：沿用基类默认——探测根路由（纯本地静态，最快、不碰上游），
    # 收到任意 HTTP 响应即视为就绪（index.html 将来被移除返回 404 同样证明
    # 服务活着）


get_bridge = make_singleton_getter(NodeBridge)
