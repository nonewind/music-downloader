"""内置 API 二进制进程管理的公共基类（netease / qq / kugou 三个 bridge 共用）

负责拉起/停止预编译二进制，对外提供幂等的 start/stop/status。子进程仅监听
127.0.0.1（内置 API 均无鉴权，暴露局域网有安全风险）。

各平台只需在子类中声明真正的差异：
- BINARIES：平台 -> 二进制文件名
- LABEL：错误信息/日志中的人类可读前缀（如 "QQ音乐API"）
- MISSING_BINARY_MSG：二进制缺失时的完整提示（含 {path} 占位）
- LOG_NAME：子进程日志文件名（logs/<LOG_NAME>.log）
- DEFAULT_TIMEOUT：就绪等待默认超时
- _prepare_runtime()：每次 start 前准备运行时目录，返回子进程 cwd
- _listen_env(port)：子进程监听地址等环境变量
- _is_ready(resp)：就绪判定

此模块为基础设施层，不经过 Provider 抽象。
"""

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from . import _proc

logger = logging.getLogger(__name__)

# 子进程唯一允许的监听地址
LOCAL_HOST = "127.0.0.1"


def default_bin_dir() -> Path:
    """内置二进制目录：frozen 时为 exe 同级 api/，源码运行为项目根 api/"""
    if getattr(sys, "frozen", False):
        # frozen: PyInstaller 打包后用 exe 同级目录作为根目录
        root = Path(sys.executable).resolve().parent
    else:
        # _bridge_base.py(0) → providers(1) → core(2) → source(3)
        root = Path(__file__).resolve().parents[2]
    return root / "api"


class ApiBridge:
    # ---- 子类覆盖的平台差异 ----
    BINARIES: dict[str, str] = {}
    LABEL: str = "API "
    MISSING_BINARY_MSG: str = "未找到 API 二进制: {path}"
    LOG_NAME: str = "api"
    DEFAULT_TIMEOUT: float = 60.0

    def __init__(self, bin_dir: Path, auto_start: bool = True,
                 timeout: float | None = None, port: int = 0):
        self.bin_dir = Path(bin_dir).resolve()
        self.bin_path = self.bin_dir / self.BINARIES.get(sys.platform, "")
        self.auto_start = auto_start
        self.timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        self._preferred_port = port
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.base_url: str | None = None
        self._lock = threading.Lock()
        # 子进程日志（open_api_log 打开的文件句柄及路径，重启/停止时关闭）
        self._log_fh = None
        self._log_path: str | None = None

    # ---------------- 平台钩子 ----------------
    def _prepare_runtime(self) -> Path:
        """start 时（锁内、spawn 前）调用一次，返回子进程 cwd。默认 bin_dir。"""
        return self.bin_dir

    def _listen_env(self, port: int) -> dict[str, str]:
        """子进程监听地址相关环境变量（必须绑定 LOCAL_HOST）"""
        raise NotImplementedError

    def _is_ready(self, resp: requests.Response) -> bool:
        """就绪判定；默认收到任意 HTTP 响应即就绪"""
        return True

    # ---------------- 公共实现 ----------------
    def status(self) -> dict:
        # 不加 self._lock——首次 start() 持锁最长 timeout 秒，共用锁会让设置页
        # 3s 轮询 /api/<平台>/status 全部挂起。仅读原子引用，瞬时不一致可接受。
        # 局部快照 p = self.proc，避免与 stop() 并发时两次读 self.proc
        # 中间被置 None 而抛 AttributeError。
        p = self.proc
        return {
            "running": self._is_alive(),
            "port": self.port,
            "preferred_port": self._preferred_port,
            "base_url": self.base_url,
            "pid": p.pid if p else None,
            "exe": str(self.bin_path),
            "platform": sys.platform,
            "bin_exists": self.bin_path.exists(),
            "auto_start": self.auto_start,
        }

    def start(self) -> str:
        """启动（幂等），返回 base_url；失败抛 RuntimeError（中文原因）"""
        with self._lock:
            if self._is_alive():
                return self.base_url
            self.proc = None      # 清理上次退出/失败的残留引用
            self.port = None
            self.base_url = None
            if not self.bin_path.exists():
                raise RuntimeError(self.MISSING_BINARY_MSG.format(path=self.bin_path))
            self._ensure_executable()
            runtime_dir = self._prepare_runtime()
            # 端口 TOCTOU 缓解（#21）：_find_free_port 探测后即释放 socket，
            # spawn 前该端口可能被并发抢占（设置页连点/重启竞态）。探活失败
            # 时换随机端口重试一次（第二次失败按原异常抛出，不做无限重试）
            last_exc: Exception | None = None
            for attempt in range(2):
                if attempt:
                    logger.warning("%s服务探活失败，换端口重试一次: %s",
                                   self.LABEL, self.bin_path.name)
                self._close_log()
                self.port = self._find_free_port(self._preferred_port if attempt == 0 else 0)
                env = {**os.environ, **self._listen_env(self.port)}
                for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                    env.pop(k, None)
                # 子进程日志：写文件而非 DEVNULL——进程秒退/上游异常时保留现场
                # （目录不可写时回退 DEVNULL 不阻断启动）
                log_fh, log_path = _proc.open_api_log(self.LOG_NAME)
                self._log_fh = log_fh
                self._log_path = log_path
                # spawn_protected 启用"父进程死亡即杀"（Win 作业对象 / Linux PDEATHSIG），
                # 下载器无论正常还是被强制退出，其启动的 API 进程都会被系统关闭
                # Popen 在 exec 被系统拒绝时抛 OSError（PermissionError / Exec
                # format error / 缺 glibc 加载器 / WinError 193 等），统一转为
                # RuntimeError 以兑现 start() 的异常契约——main() 与 Web 启停
                # 路由均只捕获 RuntimeError，OSError 逃逸会导致整个服务启动失败
                try:
                    self.proc = _proc.spawn_protected(
                        [str(self.bin_path)], cwd=str(runtime_dir), env=env,
                        stdout=log_fh if log_fh else subprocess.DEVNULL,
                        stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                    )
                except OSError as e:
                    self._close_log()
                    self.port = None
                    raise RuntimeError(
                        f"{self.LABEL}进程启动失败（{e}），请检查二进制文件完整性与执行权限"
                    ) from e
                self.base_url = f"http://{LOCAL_HOST}:{self.port}"
                try:
                    self._wait_ready(self.timeout)
                    return self.base_url
                except Exception as e:
                    self._kill_proc()
                    self.proc = None
                    self.port = None
                    self.base_url = None
                    last_exc = e
            assert last_exc is not None
            raise last_exc

    def stop(self) -> None:
        """停止（幂等）"""
        with self._lock:
            self._kill_proc()
            self.proc = None
            self.port = None
            self.base_url = None
            self._close_log()

    def _ensure_executable(self) -> None:
        if sys.platform != "linux":
            return
        # NAS 套件安装目录常为 root 所有、运行用户无写权限，chmod 会抛
        # PermissionError(EPERM)；执行位由打包/安装时设置，这里只做兜底：
        # 失败则忽略，改用 X_OK 检查，仍不可执行才报错
        try:
            self.bin_path.chmod(0o755)
        except PermissionError:
            if not os.access(self.bin_path, os.X_OK):
                raise RuntimeError(
                    f"无法为 {self.bin_path} 设置执行权限且文件不可执行\n"
                    "请以 root 执行: chmod +x <该文件>"
                )
            logger.debug("%s 已可执行，跳过 chmod", self.bin_path.name)

    def _close_log(self) -> None:
        """关闭当前子进程日志句柄（重启/停止时复用，幂等）"""
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _kill_proc(self) -> None:
        """terminate → 等待 → kill（stop 与 start 失败路径复用）"""
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def _is_alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _find_free_port(self, preferred: int = 0) -> int:
        """查找空闲端口；preferred 非 0 时优先尝试绑定指定端口，失败则回退随机端口"""
        if preferred > 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((LOCAL_HOST, preferred))
                    return preferred
                except OSError:
                    pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((LOCAL_HOST, 0))
            return s.getsockname()[1]

    def _wait_ready(self, timeout: float) -> None:
        # 轮询根路由，由 _is_ready 判定是否就绪；连接拒绝/超时/非 JSON 响应
        # 视为未就绪继续轮询。requests.get 对 4xx/5xx 不抛异常，仅连接层错误
        # 抛 RequestException。清空代理 env 只作用于子进程，此处需显式
        # proxies 强制直连 127.0.0.1。
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                tail = (f"（exit code={self.proc.returncode}，"
                        f"输出见 {self._log_path}）") if self._log_path else ""
                raise RuntimeError(f"{self.LABEL}进程异常退出{tail}，请检查二进制完整性")
            try:
                resp = requests.get(
                    f"{self.base_url}/", timeout=2,
                    proxies={"http": None, "https": None},
                )
                if self._is_ready(resp):
                    return
            except (requests.exceptions.RequestException, ValueError):
                pass  # 未就绪，继续轮询
            time.sleep(0.5)
        raise RuntimeError(f"{self.LABEL}服务启动超时")


def make_singleton_getter(cls: type[ApiBridge]):
    """生成模块级 get_bridge(auto_start=..., port=...) 单例访问器（双检锁）"""
    state: dict[str, ApiBridge | None] = {"bridge": None}
    lock = threading.Lock()

    def get_bridge(auto_start: bool | None = None, port: int | None = None) -> ApiBridge:
        """获取全局唯一 bridge（不访问数据库，无需 app context）

        auto_start / port 由 main() 在 app context 内读出后显式传入；main() 总是
        首个调用者并传值，后续调用返回既有单例，参数被忽略。
        """
        if state["bridge"] is None:
            with lock:
                if state["bridge"] is None:
                    state["bridge"] = cls(
                        bin_dir=default_bin_dir(),
                        auto_start=(True if auto_start is None else auto_start),
                        port=(0 if port is None else port),
                    )
        return state["bridge"]

    return get_bridge
