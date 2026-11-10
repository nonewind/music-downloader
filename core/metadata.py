"""音频元数据写入模块

支持：
- MP3 (ID3v2)：标题、艺术家、专辑、年份、音轨号、碟号、专辑歌手、封面、原文/翻译歌词
- FLAC (Vorbis Comment)：同上
- OGG (Vorbis / Opus，同为 Vorbis Comment)：字段集与 FLAC 对齐，
  封面按规范写 METADATA_BLOCK_PICTURE（base64 的 FLAC Picture 块）
"""

import base64
import logging
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT
from mutagen.mp3 import MP3
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

logger = logging.getLogger(__name__)


def _download_cover(url: str, timeout: int = 10) -> bytes | None:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        logger.warning("下载封面失败 %s: %s", url, e)
        return None


def _guess_cover_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return "image/jpeg"


def write_mp3_tags(
    file_path: Path,
    title: str,
    artist: str,
    album: str,
    year: str = "",
    cover_url: str = "",
    lyric: str | None = "",
    tlyric: str | None = "",
    track_no: int = 0,
    disc_no: int = 0,
    albumartist: str = "",
    write_lyric: bool = True,
    write_meta: bool = True,
) -> bool:
    """写入 MP3 ID3 标签（ID3v2.4：年份用 TDRC，音轨/碟号用 TRCK/TPOS）

    歌词按 write_lyric 开关处理：`False` ⇒ 完全不碰已有歌词；`True` ⇒ 仅在
    本次确有歌词时重建 `USLT` 帧（原文仅非空写入，翻译传空则删除）。

    `USLT` 不进无条件 delall 清单：否则「本次无词」会把已有歌词一起抹掉。

    元数据按 write_meta 开关处理：`False` ⇒ 标题/歌手/专辑/年份/音轨/碟号/
    专辑歌手/封面完全不碰（封面亦不下载）；`True` ⇒ 按既有语义覆盖。
    """
    try:
        audio = MP3(file_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        # write_meta=False ⇒ 非歌词字段完全不碰（含封面，不下载不覆盖）。
        # delall 清帧与重建必须在同一门控内：只清不建会把已有元数据抹掉
        # （与 D1「只删 USLT 不重建」同型坑）。
        if write_meta:
            for key in ("TIT2", "TPE1", "TALB", "TDRC", "TYER", "APIC",
                        "TRCK", "TPOS", "TPE2"):
                tags.delall(key)

            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))
            if year:
                tags.add(TDRC(encoding=3, text=year))
            if track_no > 0:
                tags.add(TRCK(encoding=3, text=str(track_no)))
            if disc_no > 0:
                tags.add(TPOS(encoding=3, text=str(disc_no)))
            if albumartist:
                tags.add(TPE2(encoding=3, text=albumartist))

            cover = _download_cover(cover_url)
            if cover:
                mime = _guess_cover_mime(cover)
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover",
                              data=cover))

        # 歌词：write_lyric=False ⇒ 整体跳过，已有歌词原样保留。
        # 其余情况与 write_flac_tags / write_ogg_tags 逐行对齐（这是修复点：
        # 原先无条件 delall("USLT")，本次无词时会把已有歌词一起抹掉）：
        #   原文：仅非空时写入；空值不动旧值
        #   翻译：写入覆盖；传空则摘掉翻译帧（原文帧不受影响）
        if write_lyric:
            if lyric:
                tags.delall("USLT")
                tags.add(USLT(encoding=3, lang="chi", desc="Lyrics", text=lyric))
            if tlyric:
                tags.add(USLT(encoding=3, lang="chi",
                              desc="Lyrics-Translation", text=tlyric))
            elif any(f.desc == "Lyrics-Translation" for f in tags.getall("USLT")):
                kept = [f for f in tags.getall("USLT")
                        if f.desc != "Lyrics-Translation"]
                tags.delall("USLT")
                for f in kept:
                    tags.add(f)

        audio.save()
        return True
    except Exception as e:
        logger.error("写入 MP3 标签失败 %s: %s", file_path.name, e)
        return False


def write_flac_tags(
    file_path: Path,
    title: str,
    artist: str,
    album: str,
    year: str = "",
    cover_url: str = "",
    lyric: str | None = "",
    tlyric: str | None = "",
    track_no: int = 0,
    disc_no: int = 0,
    albumartist: str = "",
    write_lyric: bool = True,
    write_meta: bool = True,
) -> bool:
    """写入 FLAC Vorbis Comment 标签

    歌词语义与 write_mp3_tags / write_ogg_tags 逐行对齐：write_lyric=False 时
    完全不碰已有歌词（含 translation）；为 True 时 lyrics 仅非空写入，
    translation 传空则删除。

    元数据按 write_meta 开关处理：False 时非歌词字段完全不碰（含封面，
    不覆盖、不 pop 清理）；True 时按既有语义覆盖。
    """
    try:
        audio = FLAC(file_path)
        # write_meta=False ⇒ 非歌词字段完全不碰（不覆盖、不 pop 清理）
        if write_meta:
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if year:
                audio["date"] = year
            else:
                audio.pop("date", None)
            if track_no > 0:
                audio["tracknumber"] = str(track_no)
            else:
                audio.pop("tracknumber", None)
            if disc_no > 0:
                audio["discnumber"] = str(disc_no)
            else:
                audio.pop("discnumber", None)
            if albumartist:
                audio["albumartist"] = albumartist
            else:
                audio.pop("albumartist", None)
        if write_lyric:
            if lyric:
                audio["lyrics"] = lyric
            if tlyric:
                audio["translation"] = tlyric
            elif "translation" in audio:
                audio.pop("translation", None)
        # write_lyric=False：歌词字段完全不碰（保留文件里已有的）

        # write_meta=False ⇒ 封面完全不碰（不下载、不清旧、不覆盖）
        if write_meta:
            audio.clear_pictures()
            cover = _download_cover(cover_url)
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = _guess_cover_mime(cover)
                pic.desc = "Cover"
                pic.data = cover
                audio.add_picture(pic)

        audio.save()
        return True
    except Exception as e:
        logger.error("写入 FLAC 标签失败 %s: %s", file_path.name, e)
        return False


def write_ogg_tags(
    file_path: Path,
    title: str,
    artist: str,
    album: str,
    year: str = "",
    cover_url: str = "",
    lyric: str | None = "",
    tlyric: str | None = "",
    track_no: int = 0,
    disc_no: int = 0,
    albumartist: str = "",
    write_lyric: bool = True,
    write_meta: bool = True,
) -> bool:
    """写入 OGG（Vorbis / Opus）Vorbis Comment 标签

    字段集与 write_flac_tags 逐行对齐（音轨号/碟号 str() 化）。容器类型经
    mutagen.File 嗅探：QQ 的 OGG 640k 理论为 Vorbis，Opus 做兜底。

    封面：mutagen 的 OggVorbis/OggOpus 均无 FLAC 那套 add_picture/
    clear_pictures，按 Vorbis Comment 规范写 METADATA_BLOCK_PICTURE =
    base64(FLAC Picture 块)——与 foobar2000 / 各播放器读法一致。

    元数据按 write_meta 开关处理：`False` ⇒ 非歌词字段完全不碰——
    `pop("metadata_block_picture")` 的清封面必须与重建同门，否则清了不建。
    """
    try:
        audio = MutagenFile(str(file_path))
        if not isinstance(audio, (OggVorbis, OggOpus)):
            logger.error("写入 OGG 标签失败 %s: 不支持的容器类型 %s",
                         file_path.name, type(audio).__name__)
            return False
        # write_meta=False ⇒ 非歌词字段完全不碰（不覆盖、不 pop 清理）
        if write_meta:
            audio["title"] = title
            audio["artist"] = artist
            audio["album"] = album
            if year:
                audio["date"] = year
            else:
                audio.pop("date", None)
            if track_no > 0:
                audio["tracknumber"] = str(track_no)
            else:
                audio.pop("tracknumber", None)
            if disc_no > 0:
                audio["discnumber"] = str(disc_no)
            else:
                audio.pop("discnumber", None)
            if albumartist:
                audio["albumartist"] = albumartist
            else:
                audio.pop("albumartist", None)
        if write_lyric:
            if lyric:
                audio["lyrics"] = lyric
            if tlyric:
                audio["translation"] = tlyric
            elif "translation" in audio:
                audio.pop("translation", None)
        # write_lyric=False：歌词字段完全不碰（保留文件里已有的）

        # 清封面（等价于 FLAC 的 clear_pictures）。
        # write_meta=False ⇒ 封面完全不碰：pop 必须与重建同门，否则清了不建
        if write_meta:
            audio.pop("metadata_block_picture", None)
            cover = _download_cover(cover_url)
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = _guess_cover_mime(cover)
                pic.desc = "Cover"
                pic.data = cover
                audio["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]

        audio.save()
        return True
    except Exception as e:
        logger.error("写入 OGG 标签失败 %s: %s", file_path.name, e)
        return False


def write_tags(file_path: Path, meta: dict,
               write_lyric: bool = True, write_meta: bool = True) -> bool:
    """根据扩展名自动选择写入器

    入口归一：meta 字段可能为 None（上游「键存在值为 null」，dict.get 默认值
    兜不住），而 mutagen 的 `audio["title"] = None` / `TIT2(text=None)` 会直接
    抛异常导致整首歌标签写不进去。此处单点收敛为 ""，各写入器无需再防御。

    `write_lyric` 与 `write_meta` 各管各的字段块：歌词（含翻译）只受
    write_lyric 约束，其余字段（标题/歌手/专辑/年份/音轨/碟号/专辑歌手/
    封面）只受 write_meta 约束。两开关默认 True，既有调用方零影响。
    """
    ext = file_path.suffix.lower()
    common = dict(
        title=str(meta.get("title") or ""),
        artist=str(meta.get("artist") or ""),
        album=str(meta.get("album") or ""),
        year=str(meta.get("year") or ""),
        cover_url=str(meta.get("cover_url") or ""),
        lyric=str(meta.get("lyric") or ""),
        tlyric=str(meta.get("tlyric") or ""),
        track_no=_as_int(meta.get("track_no")),
        disc_no=_as_int(meta.get("disc_no")),
        albumartist=str(meta.get("albumartist") or ""),
    )
    common["write_lyric"] = write_lyric
    common["write_meta"] = write_meta

    if ext == ".mp3":
        return write_mp3_tags(file_path, **common)
    if ext == ".flac":
        return write_flac_tags(file_path, **common)
    if ext == ".ogg":
        return write_ogg_tags(file_path, **common)
    logger.warning("不支持的格式，跳过元数据写入: %s", ext)
    return False


def update_tags(file_path: Path, changes: dict[str, str]) -> None:
    """只改给定的几项标签，别的标签和封面都不碰（音乐库改专辑信息用）

    write_tags 会按整套元数据重写（没给封面地址就把封面清掉、没给年份就删
    年份），不能拿来改单项。这里用 mutagen 的 easy 键名（album /
    albumartist / date …），FLAC/OGG/MP3/M4A 写法一致。

    Args:
        changes: {easy 键名: 新值}；新值为空串表示删掉这一项

    读写失败直接抛出，由调用方记成这一首失败。
    """
    audio = MutagenFile(str(file_path), easy=True)
    if audio is None:
        raise ValueError("不是能识别的音频文件")
    if audio.tags is None:
        audio.add_tags()
    for key, value in changes.items():
        if value:
            audio[key] = value
        elif key in audio:
            del audio[key]
    audio.save()


def _as_int(value) -> int:
    """宽容整型转换（None/脏数据 → 0，0=不写入该字段）"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
