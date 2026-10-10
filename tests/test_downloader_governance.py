"""下载治理守护测试：路径截断保护、已存在文件判定、DownloadOutcome 兼容性。

HTTP 全部通过 monkeypatch 打在 requests.get 上（core.downloader 以
`requests.get(...)` 方式调用），不引入任何新依赖。
"""

import logging
from pathlib import Path

import pytest

from core import downloader


class FakeResponse:
    """最小 requests.Response 替身：仅实现 download() 用到的接口"""

    def __init__(self, chunks, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def raise_for_status(self):
        assert self.status_code < 400, f"意外的 HTTP 状态: {self.status_code}"

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size):
        return iter(self._chunks)


def install_http(monkeypatch, responder):
    """把 requests.get 替换为 responder(index, headers) -> FakeResponse。

    返回 calls 列表，每项为 {"url": ..., "headers": ...}，按调用顺序记录。
    """
    calls = []

    def fake_get(url, headers=None, stream=True, timeout=None, **kwargs):
        calls.append({"url": url, "headers": dict(headers or {})})
        return responder(len(calls) - 1, dict(headers or {}))

    monkeypatch.setattr(downloader.requests, "get", fake_get)
    return calls


def make_downloader(tmp_path: Path) -> downloader.Downloader:
    return downloader.Downloader(output_dir=tmp_path, max_retries=2)


# ---------------------------------------------------------------------------
# 修复1：_fit_path 截断保护尾部后缀
# ---------------------------------------------------------------------------

def test_fit_path_protected_suffix_keeps_tail(tmp_path: Path):
    """超长路径触发截断时，受保护后缀（如歌曲唯一标识 "-12345"）必须保留在尾部。"""
    stem = "很" * 300 + "-12345"
    path = tmp_path / (stem + ".mp3")
    assert len(str(path)) > 240  # 前置：确实触发截断

    result = downloader._fit_path(path, protected_suffix="-12345")

    assert result != path
    assert result.name.endswith("-12345.mp3")
    assert len(str(result)) <= 240


def test_fit_path_without_protected_suffix_keeps_old_behavior(tmp_path: Path):
    """守护旧行为：无 protected_suffix 时截掉主名尾部，保留前 available 字符。"""
    stem = "a" * 300
    path = tmp_path / (stem + ".mp3")

    result = downloader._fit_path(path)

    parent_len = len(str(tmp_path)) + 1
    available = 240 - parent_len - len(".mp3")
    assert available > 10  # 前置：本环境走的是截断分支而非放弃分支
    assert result.parent == tmp_path
    assert result.stem == "a" * available
    assert len(str(result)) <= 240


# ---------------------------------------------------------------------------
# 修复2 + 修复3：入口跳过分支判定 与 overwrote 字段
# ---------------------------------------------------------------------------

def test_download_skips_existing_when_size_matches(tmp_path: Path, monkeypatch):
    """目标已存在且大小与 expected_size 相符 → 跳过，不发 HTTP，不视为覆盖。"""
    data = b"M" * 4096
    target = tmp_path / "song.mp3"
    target.write_bytes(data)

    def responder(i, h):
        raise AssertionError("命中跳过分支后不应发起 HTTP 请求")

    install_http(monkeypatch, responder)
    dl = make_downloader(tmp_path)

    outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                          expected_size=len(data))

    assert outcome is not None
    assert outcome.path == target
    assert outcome.produced is False
    assert outcome.overwrote is False
    assert target.read_bytes() == data


def test_download_redownloads_on_size_mismatch_and_reports_overwrote(
        tmp_path: Path, monkeypatch):
    """已存在但与 expected_size 差 >1024 → 重下成功后 produced/overwrote 均为 True。"""
    old = b"OLD" * 100  # 300 字节
    new = b"NEW" * 1024  # 3072 字节，差 2772 > 1024
    target = tmp_path / "song.mp3"
    target.write_bytes(old)

    install_http(monkeypatch, lambda i, h: FakeResponse(
        [new], headers={"Content-Length": str(len(new))}))
    dl = make_downloader(tmp_path)

    outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                          expected_size=len(new))

    assert outcome is not None
    assert outcome.produced is True
    assert outcome.overwrote is True
    assert target.read_bytes() == new
    assert not target.with_suffix(".mp3.part").exists()  # 落盘后 .part 已被 replace


def test_download_redownloads_suspicious_small_file_without_expected_size(
        tmp_path: Path, monkeypatch, caplog):
    """守护修复2：expected_size=None 时，≤1024 字节的既有文件视为可疑并重下。"""
    target = tmp_path / "song.mp3"
    target.write_bytes(b"x" * 500)  # 500 字节空壳/残留
    new = b"FRESH" * 512

    install_http(monkeypatch, lambda i, h: FakeResponse(
        [new], headers={"Content-Length": str(len(new))}))
    dl = make_downloader(tmp_path)

    with caplog.at_level(logging.WARNING, logger="core.downloader"):
        outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                              expected_size=None)

    assert outcome is not None
    assert outcome.produced is True
    assert outcome.overwrote is True
    assert target.read_bytes() == new
    # 重下原因文案须能与"大小不符"分支区分开
    assert "可疑" in caplog.text
    assert "重新下载" in caplog.text
    assert "大小不符" not in caplog.text


def test_download_fresh_target_reports_not_overwrote(tmp_path: Path, monkeypatch):
    """目标不存在 → 正常下载，produced=True 且 overwrote=False。"""
    data = b"D" * 2048
    install_http(monkeypatch, lambda i, h: FakeResponse(
        [data], headers={"Content-Length": str(len(data))}))
    dl = make_downloader(tmp_path)

    outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                          expected_size=len(data))

    assert outcome is not None
    assert outcome.path == tmp_path / "song.mp3"
    assert outcome.produced is True
    assert outcome.overwrote is False
    assert outcome.path.read_bytes() == data


# ---------------------------------------------------------------------------
# 守护：断点续传两条写盘路径
# ---------------------------------------------------------------------------

def test_resume_with_206_appends_to_part(tmp_path: Path, monkeypatch):
    """守护：有 .part 时带 Range 请求，服务器返 206 → 追加写（ab），内容为拼接。"""
    head = b"PART"
    (tmp_path / "song.mp3.part").write_bytes(head)
    rest = b"REST" * 512

    def responder(i, h):
        return FakeResponse([rest], status_code=206,
                            headers={"Content-Length": str(len(rest))})

    calls = install_http(monkeypatch, responder)
    dl = make_downloader(tmp_path)

    outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                          expected_size=len(head) + len(rest))

    assert outcome is not None
    assert outcome.produced is True
    assert calls[0]["headers"].get("Range") == f"bytes={len(head)}-"
    assert (tmp_path / "song.mp3").read_bytes() == head + rest


def test_resume_fallback_to_200_overwrites_part(tmp_path: Path, monkeypatch):
    """守护：服务器不支持 Range 返 200 → 覆盖写（wb），不得把 200 响应追加到旧 .part 后。"""
    (tmp_path / "song.mp3.part").write_bytes(b"STALE")
    full = b"FULL" * 512

    def responder(i, h):
        return FakeResponse([full], headers={"Content-Length": str(len(full))})

    install_http(monkeypatch, responder)
    dl = make_downloader(tmp_path)

    outcome = dl.download("http://example.com/song.mp3", None, "song.mp3",
                          expected_size=len(full))

    assert outcome is not None
    assert outcome.produced is True
    assert (tmp_path / "song.mp3").read_bytes() == full  # 不含 "STALE" 前缀


# ---------------------------------------------------------------------------
# 守护：DownloadOutcome 向后兼容
# ---------------------------------------------------------------------------

def test_download_outcome_two_arg_construction_backward_compatible():
    """两参构造（path, produced）仍可用，overwrote 默认 False；三参可显式置 True。"""
    o1 = downloader.DownloadOutcome(Path("/tmp/a.mp3"), True)
    assert o1.path == Path("/tmp/a.mp3")
    assert o1.produced is True
    assert o1.overwrote is False

    o2 = downloader.DownloadOutcome(path=Path("/tmp/b.mp3"), produced=False)
    assert o2.produced is False
    assert o2.overwrote is False

    o3 = downloader.DownloadOutcome(Path("/tmp/c.mp3"), True, True)
    assert o3.overwrote is True
