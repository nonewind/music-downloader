"""音频内容指纹（下载完成去重用，v0.9.0.0）

同一首歌被不同专辑收录时，平台给的往往是同一份音频，但本程序写入的
标签（专辑名/年份/封面/曲目号）不同，整文件 MD5 必然不同。这里只对
"声音部分"取指纹，标签差异不影响结果：

- FLAC：直接取 STREAMINFO 块自带的 MD5（编码器对解码后 PCM 计算，
  只读文件头 42 字节，极快）；该字段为全 0（编码器未填）时，退回对
  元数据块之后的音频帧区做 MD5。文件头前若有不规范的 ID3v2 前缀先跳过。
- MP3：跳过开头 ID3v2、去掉结尾 ID3v1 / APEv2 标签后，对中间音频帧区
  做 MD5。
- 其他内容：整文件 MD5（只能识别字节完全相同的文件）。

格式按文件内容判断而不是扩展名（库内存在扩展名 .flac 实为 MP3 的文件）。
指纹带格式前缀（flac:/flacraw:/mp3:/raw:），不同算法的结果互不相等。
"""

from __future__ import annotations

import hashlib
import os

_CHUNK = 262144


def _syncsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _skip_id3v2(f, size: int) -> int:
    """返回跳过开头（可能连续多个）ID3v2 标签后的偏移"""
    pos = 0
    while pos + 10 <= size:
        f.seek(pos)
        head = f.read(10)
        if head[:3] != b"ID3":
            break
        footer = 10 if head[5] & 0x10 else 0
        pos += 10 + _syncsafe(head[6:10]) + footer
    return min(pos, size)


def _strip_tail_tags(f, start: int, end: int) -> int:
    """去掉结尾 ID3v1（128 字节 TAG）与 APEv2 标签，返回音频区结尾偏移"""
    for _ in range(3):  # ID3v1 / APEv2 至多各一，顺序不定
        changed = False
        if end - start >= 128:
            f.seek(end - 128)
            if f.read(3) == b"TAG":
                end -= 128
                changed = True
        if end - start >= 32:
            f.seek(end - 32)
            footer = f.read(32)
            if footer[:8] == b"APETAGEX":
                tag_size = int.from_bytes(footer[12:16], "little")
                flags = int.from_bytes(footer[20:24], "little")
                has_header = bool(flags & 0x80000000)
                new_end = end - tag_size - (32 if has_header else 0)
                if start <= new_end < end:
                    end = new_end
                    changed = True
        if not changed:
            break
    return end


def _md5_range(f, start: int, end: int) -> str:
    h = hashlib.md5()
    f.seek(start)
    remain = end - start
    while remain > 0:
        chunk = f.read(min(_CHUNK, remain))
        if not chunk:
            break
        h.update(chunk)
        remain -= len(chunk)
    return h.hexdigest()


def _flac_fingerprint(f, start: int, size: int) -> str | None:
    pos = start + 4  # 跳过 "fLaC"
    streaminfo_md5 = None
    while pos + 4 <= size:
        f.seek(pos)
        hdr = f.read(4)
        if len(hdr) < 4:
            return None
        is_last = bool(hdr[0] & 0x80)
        btype = hdr[0] & 0x7F
        length = int.from_bytes(hdr[1:4], "big")
        if btype == 0 and length >= 34 and streaminfo_md5 is None:
            data = f.read(34)
            if len(data) < 34:
                return None
            streaminfo_md5 = data[18:34]
        pos += 4 + length
        if is_last:
            break
    if streaminfo_md5 is None:
        return None
    if any(streaminfo_md5):
        return "flac:" + streaminfo_md5.hex()
    # 编码器未填 STREAMINFO MD5：对音频帧区取 MD5
    end = _strip_tail_tags(f, pos, size)
    if end <= pos:
        return None
    return "flacraw:" + _md5_range(f, pos, end)


def audio_fingerprint(path) -> str | None:
    """返回文件"声音部分"的指纹；空文件 / 读取失败 / 结构损坏返回 None"""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return None
        with open(path, "rb") as f:
            start = _skip_id3v2(f, size)
            f.seek(start)
            magic = f.read(4)
            if magic == b"fLaC":
                return _flac_fingerprint(f, start, size)
            is_mp3 = start > 0 or (len(magic) >= 2 and magic[0] == 0xFF and (magic[1] & 0xE0) == 0xE0)
            if is_mp3:
                end = _strip_tail_tags(f, start, size)
                if end <= start:
                    return None
                return "mp3:" + _md5_range(f, start, end)
            return "raw:" + _md5_range(f, 0, size)
    except OSError:
        return None
