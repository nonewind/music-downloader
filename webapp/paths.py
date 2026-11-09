"""程序根目录与下载目录解析（单一实现点）

此前同一套规则在 app / task_manager / organizer_manager / migrate_layout /
routes.api 各写一份，注释里反复要求"规则变更须多处同步"。本模块不依赖
models 以外的 web 层代码，core 之外的任何模块都可安全导入。
"""

import sys
from pathlib import Path

# 程序根目录：源码运行为项目根（webapp 的上级）；PyInstaller 打包后为 exe 同级目录
if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent.parent


def output_dir_path(raw: str | None, root: Path | None = None) -> Path:
    """把 output_dir 设置值解析为路径（相对路径基于程序根目录，不做 resolve）

    Args:
        raw: Setting("output_dir") 的原始值（None 按缺省 "downloads" 处理）
        root: 相对路径的基准目录，缺省 APP_ROOT（测试可注入）
    """
    p = Path("downloads" if raw is None else raw)
    if not p.is_absolute():
        p = (root or APP_ROOT) / p
    return p


def current_output_dir(resolve: bool = True) -> Path:
    """读取当前下载目录设置并解析（调用方须在 app context 内）"""
    from models import Setting
    p = output_dir_path(Setting.get("output_dir", "downloads"))
    return p.resolve() if resolve else p
