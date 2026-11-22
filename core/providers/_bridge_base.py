"""内置 API 二进制进程管理的公共基类（netease / qq / kugou 三个 bridge 共用）

负责拉起/停止预编译二进制，对外提供幂等的 start/stop/status。子进程仅监听
127.0.0.1（内置 API 均无鉴权，暴露局域网有安全风险）。

各平台只需在子类中声明真正的差异：
- BINARIES：平台 -> 二进制文件名
- LABEL：错误信息/日志中的人类可读前缀（如 "QQ音乐API"）
- NAME：平台中文名（如 "QQ音乐"），用于"接口程序出问题"的提示
- MISSING_BINARY_MSG：二进制缺失时的完整提示（含 {path} 占位）
- LOG_NAME：子进程日志文件名（logs/<LOG_NAME>.log）
- DEFAULT_TIMEOUT：就绪等待默认超时
- _prepare_runtime()：每次 start 前准备运行时目录，返回子进程 cwd
- _listen_env(port)：子进程监听地址等环境变量
- _is_ready(resp)：就绪判定
- VERSION_PATH / VERSION_KEYS：版本接口路径与版本号在返回 JSON 中的位置

接口程序出问题时统一抛 ApiProgramError（见下），并记到 bridge.last_problem，
设置页的服务状态据此显示"最近一次异常"。

换新版接口程序：APP_DATA_DIR/api/ 下放同名文件即优先使用（Docker / fnOS
的安装目录不便改写），否则用内置 api/ 目录里的文件。

此模块为基础设施层，不经过 Provider 抽象。
"""

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from . import _proc

logger = logging.getLogger(__name__)

# 子进程唯一允许的监听地址
LOCAL_HOST = "127.0.0.1"


class ApiProgramError(RuntimeError):
    """接口程序本身出了问题——不是账号问题，换账号重试没有意义

    kind：
    - "unreachable"：接口程序没在运行、连不上或没有响应
    - "bad_response"：接口程序有回应，但报错或返回的数据和预期对不上
      （平台改了接口需要换新版接口程序，或它自己连不上平台）

    继承 RuntimeError：既有只捕获 RuntimeError 的调用点（启动线程、路由）行为不变。
    """

    def __init__(self, name: str, kind: str, detail: str, log_name: str):
        self.kind = kind
        if kind == "unreachable":
            text = f"{name}接口程序连不上：{detail}。不是账号问题，日志见 {log_name}.log"
        elif kind == "bad_response":
            text = (f"{name}接口程序返回异常：{detail}。可能是平台改了接口（需换新版接口程序）"
                    f"或网络连不上平台，日志见 {log_name}.log")
        else:
            raise ValueError(f"未知的接口程序异常类型: {kind}")
        super().__init__(text)


def default_bin_dir() -> Path:
    """内置二进制目录：frozen 时为 exe 同级 api/，源码运行为项目根 api/"""
    if getattr(sys, "frozen", False):
        # frozen: PyInstaller 打包后用 exe 同级目录作为根目录
        root = Path(sys.executable).resolve().parent
    else:
        # _bridge_base.py(0) → providers(1) → core(2) → source(3)
        root = Path(__file__).resolve().parents[2]
    return root / "api"


def override_bin_dir() -> Path | None:
    """换新版用的目录：APP_DATA_DIR/api/（未设置 APP_DATA_DIR 时没有）"""
    app_data = os.environ.get("APP_DATA_DIR")
    if not app_data:
        return None
    return Path(app_data).expanduser() / "api"


class ApiBridge:
    # ---- 子类覆盖的平台差异 ----
    BINARIES: dict[str, str] = {}
    LABEL: str = "API "
    NAME: str = ""
    # 版本接口：路径 + 版本号在返回 JSON 里逐层的键；None=接口程序没有版本接口
    VERSION_PATH: str | None = None
    VERSION_KEYS: tuple[str, ...] = ()
    MISSING_BINARY_MSG: str = "未找到 API 二进制: {path}"
    LOG_NAME: str = "api"
    DEFAULT_TIMEOUT: float = 60.0

    def __init__(self, bin_dir: Path,
                 timeout: float | None = None, port: int = 0):
        self.bin_dir = Path(bin_dir).resolve()
        bin_name = self.BINARIES.get(sys.platform, "")
        # 第 1 步：数据目录 api/ 下有同名文件就用它（换新版的地方）
        override_dir = override_bin_dir()
        if override_dir is not None and bin_name and (override_dir / bin_name).exists():
            self.bin_path = (override_dir / bin_name).resolve()
        else:
            # 第 2 步：否则用内置 api/ 目录
            self.bin_path = self.bin_dir / bin_name
        self.timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        self._preferred_port = port
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.base_url: str | None = None
        self._lock = threading.Lock()
        # 子进程日志（open_api_log 打开的文件句柄及路径，重启/停止时关闭）
        self._log_fh = None
        self._log_path: str | None = None
        # 接口程序自报的版本号（启动就绪后读一次；读不到为 None）
        self.version: str | None = None
        # 最近一次接口程序异常 {"kind", "text", "at"}；重新启动成功时清空
        self.last_problem: dict | None = None

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

    def _read_version(self) -> str | None:
        """读接口程序自报的版本号；只用于显示，读不到记 WARNING 返回 None"""
        if self.VERSION_PATH is None:
            return None
        # 第 1 步：请求版本接口（显式直连 127.0.0.1，同 _wait_ready）
        try:
            resp = requests.get(
                f"{self.base_url}{self.VERSION_PATH}", timeout=3,
                proxies={"http": None, "https": None},
            )
            node = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("%s接口程序版本号读取失败: %s", self.NAME, e)
            return None
        # 第 2 步：按 VERSION_KEYS 逐层取出版本号
        for key in self.VERSION_KEYS:
            if not isinstance(node, dict) or key not in node:
                logger.warning("%s接口程序版本接口格式不对: %s", self.NAME, resp.text[:200])
                return None
            node = node[key]
        return str(node)

    # ---------------- 接口程序异常 ----------------
    def problem(self, kind: str, detail: str) -> ApiProgramError:
        """生成接口程序异常，并记为最近一次异常（设置页显示）；由调用方 raise"""
        err = ApiProgramError(self.NAME, kind, detail, self.LOG_NAME)
        self.last_problem = {
            "kind": kind,
            "text": str(err),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return err

    # ---------------- 公共实现 ----------------
    def status(self) -> dict:
        # 不加 self._lock——首次 start() 持锁最长 timeout 秒，共用锁会让设置页
        # 3s 轮询 /api/<平台>/status 全部挂起。仅读原子引用，瞬时不一致可接受。
        # 局部快照 p = self.proc，避免与 stop() 并发时两次读 self.proc
        # 中间被置 None 而抛 AttributeError。
        p = self.proc
        bin_exists = self.bin_path.exists()
        # 文件更新时间：换新版后据此确认用的是新文件
        bin_updated_at = None
        if bin_exists:
            mtime = self.bin_path.stat().st_mtime
            bin_updated_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        return {
            "running": self._is_alive(),
            "port": self.port,
            "preferred_port": self._preferred_port,
            "base_url": self.base_url,
            "pid": p.pid if p else None,
            "exe": str(self.bin_path),
            "platform": sys.platform,
            "bin_exists": bin_exists,
            "bin_updated_at": bin_updated_at,
            "version": self.version,
            "last_problem": self.last_problem,
        }

    def start(self) -> str:
        """启动（幂等），返回 base_url；失败抛 ApiProgramError（unreachable，中文原因）"""
        with self._lock:
            if self._is_alive():
                return self.base_url
            self.proc = None      # 清理上次退出/失败的残留引用
            self.port = None
            self.base_url = None
            self.version = None
            if not self.bin_path.exists():
                raise self.problem("unreachable", self.MISSING_BINARY_MSG.format(path=self.bin_path))
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
                    raise self.problem(
                        "unreachable",
                        f"进程启动失败（{e}），请检查二进制文件完整性与执行权限",
                    ) from e
                self.base_url = f"http://{LOCAL_HOST}:{self.port}"
                try:
                    self._wait_ready(self.timeout)
                except Exception as e:
                    self._kill_proc()
                    self.proc = None
                    self.port = None
                    self.base_url = None
                    last_exc = e
                    continue
                # 就绪：清掉旧异常，读版本号并记日志（换新版后据此确认生效）
                self.last_problem = None
                self.version = self._read_version()
                logger.info("%s接口程序已启动：版本 %s，文件 %s",
                            self.NAME, self.version or "未知", self.bin_path)
                return self.base_url
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
                raise self.problem(
                    "unreachable",
                    f"无法为 {self.bin_path} 设置执行权限且文件不可执行，"
                    "请以 root 执行: chmod +x <该文件>",
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
                raise self.problem("unreachable", f"进程启动后退出{tail}，请检查二进制完整性")
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
        raise self.problem("unreachable", f"启动 {timeout:.0f} 秒内没有就绪")


def make_singleton_getter(cls: type[ApiBridge]):
    """生成模块级 get_bridge(port=...) 单例访问器（双检锁）"""
    state: dict[str, ApiBridge | None] = {"bridge": None}
    lock = threading.Lock()

    def get_bridge(port: int | None = None) -> ApiBridge:
        """获取全局唯一 bridge（不访问数据库，无需 app context）

        port 由 main() 在 app context 内读出后显式传入；main() 总是
        首个调用者并传值，后续调用返回既有单例，参数被忽略。
        """
        if state["bridge"] is None:
            with lock:
                if state["bridge"] is None:
                    state["bridge"] = cls(
                        bin_dir=default_bin_dir(),
                        port=(0 if port is None else port),
                    )
        return state["bridge"]

    return get_bridge
