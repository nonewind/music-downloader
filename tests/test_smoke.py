"""最小冒烟测试：核心模块可导入、核心类型与 Downloader 可构造。

core.metadata 依赖 mutagen（生产运行时依赖），故在测试函数内导入，
避免收集阶段因环境缺依赖而整体中断。
"""

from pathlib import Path

import core.downloader


def test_import_core_modules():
    """core.downloader 与 core.metadata 导入成功，DownloadOutcome 可构造。"""
    import core.metadata

    assert hasattr(core.downloader, "Downloader")
    assert hasattr(core.metadata, "write_tags")

    outcome = core.downloader.DownloadOutcome(path="/tmp/x.mp3", produced=False)
    assert outcome.path == "/tmp/x.mp3"
    assert outcome.produced is False


def test_downloader_instantiates(tmp_path: Path):
    """Downloader 以最小参数（output_dir）即可构造，其余参数均有默认值。"""
    dl = core.downloader.Downloader(output_dir=tmp_path)
    assert dl.output_dir == tmp_path
    assert dl.overwrite is False
