"""内置 qqmusic-api 二进制进程管理（跨平台单例）

负责拉起/停止预编译二进制（qqmusic-api-win-x64.exe / qqmusic-api-linux-x64），
对外提供幂等的 start/stop/status。单个进程仅监听 127.0.0.1 端口（默认 45602）。
公共进程管理逻辑见 core/providers/_bridge_base.py，本模块差异点：
- 二进制为 PyInstaller onefile 打包的 FastAPI/uvicorn 应用，监听地址经环境变量
  QQMUSIC_SERVER_HOST / QQMUSIC_SERVER_PORT 控制（pydantic-settings，Env 优先级
  高于 exe 同目录 config.toml）
- 就绪探测走 / 根端点（服务自身端点，返回 {"code":0,...}；本服务无 /health）
- onefile 首次启动需自解压，就绪等待 timeout 默认 60s
- fpk 部署时运行时目录为 APP_DATA_DIR/qqmusic
"""

import logging
import os
import shutil
from pathlib import Path

import requests

from .._bridge_base import LOCAL_HOST, ApiBridge, make_singleton_getter

logger = logging.getLogger(__name__)

# 平台 -> 二进制文件名
_BINARIES = {
    "win32": "qqmusic-api-win-x64.exe",
    "linux": "qqmusic-api-linux-x64",
}


class QqApiBridge(ApiBridge):
    BINARIES = _BINARIES
    LABEL = "QQ音乐API"
    MISSING_BINARY_MSG = (
        "未找到QQ音乐API二进制: {path}\n"
        "请将 qqmusic-api 对应平台版本放到 api/ 目录"
    )
    LOG_NAME = "qqmusic-api"
    # onefile 首次启动需自解压
    DEFAULT_TIMEOUT = 60.0

    def _prepare_runtime(self) -> Path:
        # 运行时目录（cwd）：qqmusic-api 启动即在 cwd 下创建 web/data/
        # （device.json / credentials.sqlite3 / logs）。fpk 部署时 bin_dir
        # 位于 APPDEST（安装目录，只读或应用专用用户无写权限），写入失败
        # 会导致进程秒退（表现为"QQ音乐API进程异常退出"）。APP_DATA_DIR
        # 由 fnos 生命周期脚本注入为可写持久目录；未设置（源码运行/普通
        # 打包）保持 bin_dir 原行为。监听地址已由环境变量覆盖，config.toml
        # 缺失不影响启动，尽力复制一份以保留限流等配置。
        app_data = os.environ.get("APP_DATA_DIR")
        if not app_data:
            return self.bin_dir
        candidate = Path(app_data).expanduser() / "qqmusic"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("QQ音乐API运行时目录创建失败，回退 bin_dir: %s", candidate)
            return self.bin_dir
        cfg = self.bin_dir / "config.toml"
        dst = candidate / "config.toml"
        if cfg.exists() and not dst.exists():
            try:
                shutil.copy2(cfg, dst)
            except OSError:
                pass  # 复制失败仅丢失自定义限流配置，不阻断启动
        return candidate

    def _listen_env(self, port: int) -> dict[str, str]:
        # 服务默认监听 127.0.0.1（config.toml），环境变量显式覆盖以保证
        # 任意默认配置下都不暴露局域网（Env 优先级高于 config.toml）
        return {"QQMUSIC_SERVER_HOST": LOCAL_HOST, "QQMUSIC_SERVER_PORT": str(port)}

    def _is_ready(self, resp: requests.Response) -> bool:
        # 根端点返回 200 且响应体 code==0 才视为就绪（非 JSON 抛 ValueError，
        # 由基类视为未就绪继续轮询）
        return resp.status_code == 200 and (resp.json() or {}).get("code") == 0


get_bridge = make_singleton_getter(QqApiBridge)
