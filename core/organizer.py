"""音乐整理核心逻辑（纯逻辑，无 Flask 依赖）

扫描下载目录中的音频文件，按「同平台歌曲 ID」与「字节级内容」两种维度
找出重复文件，并为每组推荐规格最高的保留文件。删除动作由 webapp/routes/api.py
的 /api/organize/* 端点执行，本模块不做任何写操作（file_sha1 只读）。

设计要点：
- scan_directory 用 os.walk + 目录名原地剪枝：跳过 .trash 回收站与任何
  以 . 开头的隐藏目录；
- 单个文件元数据读取失败不中断扫描：参数置 0、tag 置空、logger.warning，
  文件仍入列表（整理决策可继续用 size/文件名）；
- group_duplicates 中 same_id 优先：已进 same_id 组的文件不再参与
  identical（字节级）分组，避免同一文件出现在两个组里；
- spec_rank 是规格等级排序键（越高越好）：无损按采样率/位深，有损按码率，
  同级取大文件、再取更新的 mtime。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from mutagen import File as MutagenFile

logger = logging.getLogger(__name__)

# 参与扫描的音频扩展名（小写）
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".opus"}

# 无损扩展名（其余一律按有损处理）
LOSSLESS_EXTS = {".flac"}

# 文件名尾部歌曲 ID：最后一个 '-' 后 1~32 位 ASCII 字母数字
# （不能用 str.isalnum()：中文也满足 isalnum，"歌名-纯中文" 会被误判）
_SONG_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")

# 常见 tag 键名兼容：Vorbis 注释（flac/ogg）、ID3 帧（mp3）、iTunes（m4a）
_TAG_KEYS = {
    "title": ("title", "TIT2", "\xa9nam"),
    "artist": ("artist", "TPE1", "\xa9ART"),
    "album": ("album", "TALB", "\xa9alb"),
}


@dataclass
class FileEntry:
    """扫描到的单个音频文件（含 tag 与音频参数，读取失败处为 0/空串）"""
    path: Path
    size: int = 0
    mtime: float = 0.0
    ext: str = ""
    song_id: str | None = None      # 从文件名尾部 -ID 提取（无则为 None）
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    bitrate_kbps: int = 0
    sample_rate: int = 0
    channels: int = 0
    bits_per_sample: int = 0
    lossless: bool = False          # flac=True；mp3/ogg/m4a/opus=False


@dataclass
class DuplicateGroup:
    """一组重复文件：same_id（同平台歌曲 ID）或 identical（字节级相同）"""
    group_type: str = "identical"   # "same_id" | "identical"
    items: list = field(default_factory=list)
    recommended: str = ""           # 推荐保留的 path（组内规格最高）


def extract_song_id(stem: str) -> str | None:
    """从文件名主干提取歌曲 ID：最后一个 '-' 后的部分为 1~32 位纯 ASCII
    字母数字时返回该段，否则 None（无 '-' / 中文 / 超长均视为无 ID）
    """
    if not stem or "-" not in stem:
        return None
    tail = stem.rsplit("-", 1)[-1]
    if _SONG_ID_RE.match(tail):
        return tail
    return None


def _read_tag(tags, keys: tuple) -> str:
    """从 mutagen tags 对象按候选键名读文本 tag（无 tag 容错）

    Vorbis/ID3 dict 直接给 str/list；ID3 帧对象经 .text 取值。
    任何读取异常都吞掉返回空串。
    """
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if value is not None and not isinstance(value, str):
            text = getattr(value, "text", None)
            if isinstance(text, (list, tuple)):
                value = text[0] if text else None
            elif isinstance(text, str):
                value = text
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_entry(path: Path, ext: str) -> FileEntry:
    """构造单个 FileEntry：stat + mutagen 读 tag/音频参数，失败容错"""
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError as e:
        logger.warning("读取文件信息失败 %s: %s", path, e)
        size, mtime = 0, 0.0

    title = artist = album = ""
    duration_ms = bitrate_kbps = sample_rate = channels = bits_per_sample = 0
    try:
        audio = MutagenFile(str(path))
        if audio is not None:
            tags = getattr(audio, "tags", None)
            if tags is not None:
                title = _read_tag(tags, _TAG_KEYS["title"])
                artist = _read_tag(tags, _TAG_KEYS["artist"])
                album = _read_tag(tags, _TAG_KEYS["album"])
            info = getattr(audio, "info", None)
            if info is not None:
                length = float(getattr(info, "length", 0) or 0)
                duration_ms = int(round(length * 1000))
                bitrate = int(getattr(info, "bitrate", 0) or 0)
                bitrate_kbps = bitrate // 1000      # mutagen 单位为 bps
                sample_rate = int(getattr(info, "sample_rate", 0) or 0)
                channels = int(getattr(info, "channels", 0) or 0)
                bits_per_sample = int(getattr(info, "bits_per_sample", 0) or 0)
    except Exception as e:
        # 读取异常：参数置 0、tag 置空，文件仍入列表
        logger.warning("读取音频元数据失败 %s: %s", path, e)
        title = artist = album = ""
        duration_ms = bitrate_kbps = sample_rate = channels = bits_per_sample = 0

    return FileEntry(
        path=path,
        size=size,
        mtime=mtime,
        ext=ext,
        song_id=extract_song_id(path.stem),
        title=title,
        artist=artist,
        album=album,
        duration_ms=duration_ms,
        bitrate_kbps=bitrate_kbps,
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
        lossless=ext in LOSSLESS_EXTS,
    )


def scan_directory(root: Path) -> list[FileEntry]:
    """递归扫描目录下的音频文件

    - 只收 AUDIO_EXTS 扩展名；
    - 剪枝跳过 .trash 与任何以 . 开头的隐藏目录（不进入）；
    - 目录不存在/不可读时返回空列表（由端点层负责给出用户可见错误）。
    """
    root = Path(root)
    entries: list[FileEntry] = []
    if not root.is_dir():
        logger.warning("scan_directory: 目录不存在或不可读 %s", root)
        return entries
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝：.trash 与隐藏目录整体跳过
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            ext = path.suffix.lower()
            if ext not in AUDIO_EXTS:
                continue
            entries.append(_build_entry(path, ext))
    return entries


def spec_rank(entry: FileEntry) -> tuple:
    """规格等级排序键（高优先）：无损按采样率/位深，有损按码率，
    同级取大文件、再取更新的 mtime。供 max()/sorted() 使用。
    """
    return (
        1 if entry.lossless else 0,
        entry.sample_rate,
        entry.bits_per_sample,
        entry.bitrate_kbps,
        entry.size,
        entry.mtime,
    )


def file_sha1(path: Path) -> str:
    """流式计算文件 SHA1（不整读进内存）"""
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def group_duplicates(entries: list[FileEntry]) -> list[DuplicateGroup]:
    """把扫描结果分成重复组

    - same_id：song_id 非空的文件按 song_id 分组，组内 ≥2 个文件成组；
    - identical：排除已在 same_id 组内的文件后，按字节 size 预分组，
      同 size 且 ≥2 个的再算 SHA1，哈希一致的成组；
    - 返回的组按 items 数降序、再按组内最大 size 降序。
    """
    groups: list[DuplicateGroup] = []

    # ---- same_id 组 ----
    by_id: dict[str, list[FileEntry]] = {}
    for e in entries:
        if e.song_id:
            by_id.setdefault(e.song_id, []).append(e)
    in_same_id: set[Path] = set()
    for items in by_id.values():
        if len(items) < 2:
            continue
        groups.append(DuplicateGroup(
            group_type="same_id",
            items=items,
            recommended=str(max(items, key=spec_rank).path),
        ))
        in_same_id.update(i.path for i in items)

    # ---- identical 组（排除已进 same_id 组的文件）----
    by_size: dict[int, list[FileEntry]] = {}
    for e in entries:
        if e.path in in_same_id:
            continue
        by_size.setdefault(e.size, []).append(e)
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        by_hash: dict[str, list[FileEntry]] = {}
        for e in candidates:
            try:
                digest = file_sha1(e.path)
            except OSError as err:
                logger.warning("计算 SHA1 失败，跳过 %s: %s", e.path, err)
                continue
            by_hash.setdefault(digest, []).append(e)
        for items in by_hash.values():
            if len(items) < 2:
                continue
            groups.append(DuplicateGroup(
                group_type="identical",
                items=items,
                recommended=str(max(items, key=spec_rank).path),
            ))

    groups.sort(key=lambda g: (-len(g.items), -max(i.size for i in g.items)))
    return groups
