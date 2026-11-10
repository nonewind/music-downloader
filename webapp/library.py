"""音乐库的改动：改专辑信息（标签 + 文件夹改名）、移动歌曲、清理空文件夹

预览和执行走同一份计划：plan_* 先算出每个文件要怎么变（FilePlan），
预览只把计划交给页面看，执行按同一份计划逐个文件做——保证看到的就是会做的。

执行规则（硬盘没有备份，宁可跳过也不覆盖）：
- 目标位置已有同名文件：跳过，不覆盖；
- 挪动只改位置，文件名（「歌名-歌曲ID.ext」）不变，extract_song_id 靠它取 ID；
- 每个文件做完立即提交数据库：索引行、Song.file_path、DownloadTask.file_path
  三处一起改，中途断了也不会出现大面积「文件挪了、记录没改」；
- 有整理任务（扫描/清理）在跑时整体拒绝。

调用方须在 Flask app context 内。
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.downloader import sanitize_filename
from core.metadata import update_tags
from core.organizer import build_entry, read_easy_tags
from models import (
    ORGANIZER_JOB_ACTIVE_STATUSES,
    DownloadTask,
    LocalAudioFile,
    OrganizerJob,
    Song,
    db,
)
from organizer_manager import _fill_index_row, _norm_lookup_path

logger = logging.getLogger(__name__)

# 专辑文件夹名结尾的「 (四位年份)」
ALBUM_YEAR_SUFFIX_RE = re.compile(r" \((\d{4})\)$")

# 页面字段 → easy 标签键名
_FIELD_TAG_KEYS = {"album": "album", "albumartist": "albumartist", "year": "date"}


class LibraryBusyError(Exception):
    """有整理任务在跑，拒绝改动"""


@dataclass
class FilePlan:
    """一个文件要怎么变"""
    src: Path                                       # 现在的位置
    dst: Path                                       # 改完的位置（不挪就等于 src）
    tags: dict[str, str] = field(default_factory=dict)   # 要写的标签 {easy 键: 新值}
    tag_diff: list[dict] = field(default_factory=list)   # 预览用 [{key, old, new}]
    skip: str = ""                                  # 非空 = 跳过的原因


def split_album_dir(album_dir: str) -> tuple[str, str]:
    """专辑文件夹名 → (显示名, 年份)：去掉结尾的「 (四位年份)」"""
    m = ALBUM_YEAR_SUFFIX_RE.search(album_dir)
    if m is None:
        return album_dir, ""
    return album_dir[:m.start()], m.group(1)


def album_rows(album_path: Path) -> list[LocalAudioFile]:
    """一个专辑文件夹里（不含子文件夹）的 active 索引行，按文件名排"""
    prefix = str(album_path) + os.sep
    rows = LocalAudioFile.query.filter(
        LocalAudioFile.status == "active",
        LocalAudioFile.path.startswith(prefix, autoescape=True),
    ).all()
    rows = [r for r in rows if Path(r.path).parent == album_path]
    rows.sort(key=lambda r: Path(r.path).name)
    return rows


def ensure_not_busy() -> None:
    """有扫描/清理任务在跑时拒绝（判断方法与 register_downloaded_file 一致）"""
    busy = OrganizerJob.query.filter(
        OrganizerJob.status.in_(ORGANIZER_JOB_ACTIVE_STATUSES)).first()
    if busy is not None:
        raise LibraryBusyError("正在扫描或清理，请等它做完再改")


# ======================================================================
# 计划：改专辑信息
# ======================================================================
def _new_album_folder(out_dir: Path, artist_dir: str, album_dir: str,
                      changes: dict[str, str]) -> Path:
    """改专辑信息后专辑文件夹应该在哪

    - 改了专辑歌手：换到新歌手的文件夹（取第一个歌手，和下载时同一规则）；
    - 改了专辑名：文件夹名跟着改；
    - 原来带「(年份)」的保持带，年份改了后缀跟着改，年份删了后缀也去掉；
      原来不带的保持不带。
    文件夹名用 sanitize_filename 清洗，和下载时一致。
    """
    new_artist_dir = artist_dir
    if "albumartist" in changes:
        primary = re.split(r"[/、]", changes["albumartist"])[0].strip()
        new_artist_dir = sanitize_filename(primary)

    name, year = split_album_dir(album_dir)
    new_name = changes.get("album", name)
    new_album_dir = sanitize_filename(new_name)
    if year:
        new_year = changes.get("year", year)
        if new_year:
            new_album_dir = f"{new_album_dir} ({new_year})"
    return out_dir / new_artist_dir / new_album_dir


def plan_album_edit(out_dir: Path, rel_dir: str, changes: dict[str, str],
                    move_folder: bool) -> list[FilePlan]:
    """改专辑信息的计划

    Args:
        rel_dir: 「歌手文件夹/专辑文件夹」（调用方已检查过格式）
        changes: 页面上改了的字段 {album / albumartist / year: 新值}（已检查过）
        move_folder: 文件夹是否跟着改名
    """
    artist_dir, album_dir = rel_dir.split("/")
    album_path = out_dir / artist_dir / album_dir
    if move_folder:
        target_dir = _new_album_folder(out_dir, artist_dir, album_dir, changes)
    else:
        target_dir = album_path

    plans = []
    for row in album_rows(album_path):
        src = Path(row.path)
        plan = FilePlan(src=src, dst=target_dir / src.name)

        # 步骤 1：和文件里现有的标签比，只写不一样的
        current = read_easy_tags(src)
        for field_name, new_value in changes.items():
            key = _FIELD_TAG_KEYS[field_name]
            old_value = current.get(key, "")
            if key == "date":
                # 年份只比前四位：「2015-11-20」改成「2015」不算改动
                same = old_value[:4] == new_value
            else:
                same = old_value == new_value
            if not same:
                plan.tags[key] = new_value
                plan.tag_diff.append({"key": field_name, "old": old_value, "new": new_value})

        # 步骤 2：目标位置已有同名文件 → 跳过，不覆盖
        if plan.dst != src and plan.dst.exists():
            plan.skip = "新位置已有同名文件"
        plans.append(plan)
    return plans


# ======================================================================
# 计划：把选中的歌移到别的专辑
# ======================================================================
def _album_tag_values(out_dir: Path, target_rel: str) -> dict[str, str]:
    """目标专辑的专辑名、专辑歌手、年份（移动时可选改成和它一样）

    先取目标专辑里第一首有标签的歌，取不到的项用文件夹名补：
    专辑名 = 文件夹显示名，专辑歌手 = 歌手文件夹名，年份 = 文件夹后缀。
    """
    artist_dir, album_dir = target_rel.split("/")
    name, folder_year = split_album_dir(album_dir)
    values = {"album": "", "albumartist": "", "date": ""}
    for row in album_rows(out_dir / artist_dir / album_dir):
        tags = read_easy_tags(Path(row.path))
        for key in values:
            if not values[key] and tags.get(key):
                values[key] = tags[key]
    if not values["album"]:
        values["album"] = name
    if not values["albumartist"]:
        values["albumartist"] = artist_dir
    if not values["date"]:
        values["date"] = folder_year
    return values


def plan_move_songs(out_dir: Path, srcs: list[Path], target_rel: str,
                    retag: bool) -> list[FilePlan]:
    """移动歌曲的计划：挪进目标专辑文件夹，文件名不变

    Args:
        srcs: 要移动的文件（调用方已检查过在下载目录内、在索引里）
        target_rel: 目标专辑「歌手文件夹/专辑文件夹」（已检查过格式）
        retag: 是否同时把专辑名、专辑歌手、年份改成和目标专辑一样
    """
    target_dir = out_dir.joinpath(*target_rel.split("/"))
    target_values = {}
    if retag:
        target_values = _album_tag_values(out_dir, target_rel)

    plans = []
    for src in srcs:
        plan = FilePlan(src=src, dst=target_dir / src.name)
        if plan.dst == src:
            plan.skip = "已经在这张专辑里"
            plans.append(plan)
            continue
        if plan.dst.exists():
            plan.skip = "新位置已有同名文件"
            plans.append(plan)
            continue
        # 只写目标专辑有值、且和这首不一样的项（目标没有年份就不动这首的年份）
        current = read_easy_tags(src)
        for key, new_value in target_values.items():
            if not new_value:
                continue
            old_value = current.get(key, "")
            if old_value != new_value:
                plan.tags[key] = new_value
                field_name = "year" if key == "date" else key
                plan.tag_diff.append({"key": field_name, "old": old_value, "new": new_value})
        plans.append(plan)
    return plans


def album_folder_for(out_dir: Path, artist: str, album: str) -> str:
    """按歌手名、专辑名定专辑文件夹，返回「歌手文件夹/专辑文件夹」

    名字用 sanitize_filename 清洗（和下载时一致）；歌手文件夹下已有同名专辑
    （不管带不带「(年份)」）就用它，合到一起；没有就新建「专辑」文件夹。
    """
    artist_dir = sanitize_filename(artist)
    album_dir = sanitize_filename(album)
    artist_path = out_dir / artist_dir
    if artist_path.is_dir():
        for child in sorted(artist_path.iterdir()):
            if child.is_dir() and split_album_dir(child.name)[0] == album_dir:
                return f"{artist_dir}/{child.name}"
    return f"{artist_dir}/{album_dir}"


# ======================================================================
# 未归位文件：按标签给出归位建议
# ======================================================================
def suggest_placement(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    """按标签给未归位文件分组：同一个（歌手, 专辑）一组

    歌手取专辑歌手标签，没有就取歌手标签；有「/、&,;」隔开的取第一个。
    用空格隔开的不拆（「波普电 Popest」本身就是一个名字），留给页面上人工改。

    Returns:
        (groups, skipped)
        groups: [{artist, album, paths}]，按歌手、专辑排序
        skipped: [{path, reason}]，没有专辑或歌手标签的
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    skipped = []
    for path in paths:
        tags = read_easy_tags(path)
        album = tags.get("album", "").strip()
        artist_raw = tags.get("albumartist", "").strip()
        if not artist_raw:
            artist_raw = tags.get("artist", "").strip()
        artist = re.split(r"[/、&,;，；]", artist_raw)[0].strip()

        if not album:
            skipped.append({"path": str(path), "reason": "没有专辑标签"})
            continue
        if not artist:
            skipped.append({"path": str(path), "reason": "没有歌手标签"})
            continue
        grouped.setdefault((artist, album), []).append(str(path))

    groups = []
    for (artist, album), group_paths in sorted(grouped.items()):
        groups.append({"artist": artist, "album": album, "paths": group_paths})
    return groups, skipped


# ======================================================================
# 空文件夹
# ======================================================================
def find_empty_dirs(out_dir: Path) -> list[Path]:
    """下载目录里「整棵都没有文件」的文件夹，只列最上层的那个

    例如「歌手/专辑」空了、「歌手」下也没别的东西，只列「歌手」。
    以 . 开头的文件夹（.trash 回收站等）跳过。只看磁盘，不看索引。
    """
    has_file: dict[Path, bool] = {}
    # 从最深处往上走，子文件夹的结果先算出来
    for dirpath, dirnames, filenames in os.walk(out_dir, topdown=False):
        folder = Path(dirpath)
        if any(part.startswith(".") for part in folder.relative_to(out_dir).parts):
            continue
        if filenames:
            has_file[folder] = True
            continue
        # 隐藏子文件夹（.trash 等）按「有东西」算，带着它的文件夹不会被删
        child_has_file = False
        for d in dirnames:
            if d.startswith(".") or has_file.get(folder / d, True):
                child_has_file = True
        has_file[folder] = child_has_file

    empty = []
    for folder, flag in has_file.items():
        if flag or folder == out_dir:
            continue
        # 上一层也是空的就只列上一层
        if folder.parent != out_dir and not has_file.get(folder.parent, True):
            continue
        empty.append(folder)
    empty.sort()
    return empty


def remove_empty_dirs(out_dir: Path) -> int:
    """删掉 find_empty_dirs 找到的文件夹（连同里面的空子文件夹）

    逐层 rmdir：rmdir 只能删空文件夹，万一中途有文件放进来也不会被删。
    Returns:
        删掉的最上层文件夹个数
    """
    removed = 0
    for top in find_empty_dirs(out_dir):
        try:
            for dirpath, _dirnames, _filenames in os.walk(top, topdown=False):
                Path(dirpath).rmdir()
        except OSError as e:
            # 刚好有文件放进来、或没有权限：这个文件夹不删，接着处理下一个
            logger.warning("删除空文件夹失败 %s: %s", top, e)
            continue
        logger.info("删除空文件夹: %s", top)
        removed += 1
    return removed


# ======================================================================
# 文件有问题（扩展名和内容对不上 / 空文件）
# ======================================================================
def _real_format(head: bytes) -> str:
    """按文件开头几个字节认格式；认不出返回空串"""
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    return ""


def file_problem(path: Path) -> str:
    """文件有没有问题：空文件、扩展名和内容对不上。没问题返回空串"""
    with open(path, "rb") as f:
        head = f.read(12)
    if not head:
        return "空文件"
    real = _real_format(head)
    ext = path.suffix.lower().lstrip(".")
    # .opus 也是 Ogg 容器
    if ext == "opus":
        ext = "ogg"
    if real and real != ext:
        return f"扩展名是 .{path.suffix.lower().lstrip('.')}，内容其实是 {real.upper()}"
    if not real:
        return "认不出是什么格式"
    return ""


# ======================================================================
# 执行
# ======================================================================
def _song_lookup() -> tuple[dict[str, list[Song]], dict[str, list[DownloadTask]]]:
    """Song / DownloadTask 按归一化文件路径分组（挪文件前建好，旧路径还能 resolve）"""
    songs: dict[str, list[Song]] = {}
    for s in Song.query.filter(Song.file_path != "").all():
        key = _norm_lookup_path(s.file_path)
        if key is not None:
            songs.setdefault(key, []).append(s)
    tasks: dict[str, list[DownloadTask]] = {}
    for t in DownloadTask.query.filter(DownloadTask.file_path != "").all():
        key = _norm_lookup_path(t.file_path)
        if key is not None:
            tasks.setdefault(key, []).append(t)
    return songs, tasks


def _sync_records(src: Path, dst: Path, songs: dict, tasks: dict) -> None:
    """文件改完后同步三处记录（不 commit，由调用方逐文件 commit）

    - 索引行：路径改成新位置，按磁盘重读大小、时间、标签；
      _fill_index_row 会把 song_id 换成文件名解析值、不写 platform，
      这里保住原来的身份（和 register_downloaded_file 同一做法）；
    - Song.file_path / DownloadTask.file_path：指向旧位置的改成新位置。
    """
    row = LocalAudioFile.query.filter_by(path=str(src)).first()
    if dst != src:
        # 新位置可能有一条旧记录（以前在这儿、后来进了回收站或丢了），路径唯一，先删掉
        stale = LocalAudioFile.query.filter_by(path=str(dst)).first()
        if stale is not None:
            db.session.delete(stale)
            db.session.flush()
    if row is None:
        row = LocalAudioFile(path=str(dst))
        db.session.add(row)
    platform = row.platform
    song_id = row.song_id
    row.path = str(dst)
    entry = build_entry(dst, dst.suffix.lower())
    _fill_index_row(row, entry)
    if platform:
        row.platform = platform
    if song_id:
        row.song_id = song_id

    if dst == src:
        # 只改了标签：下载记录里的文件大小跟着更新
        for s in songs.get(str(src), []):
            s.file_size = entry.size
        return
    for s in songs.get(str(src), []):
        s.file_path = str(dst)
        s.file_size = entry.size
    for t in tasks.get(str(src), []):
        t.file_path = str(dst)


def _remove_if_empty(out_dir: Path, folder: Path) -> None:
    """文件夹空了就删掉（只删空的，里面有任何东西都不动；下载目录本身和它外面的绝不碰）"""
    if folder == out_dir or not folder.is_relative_to(out_dir):
        return
    if folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        logger.info("删除空文件夹: %s", folder)


def execute_plans(out_dir: Path, plans: list[FilePlan]) -> list[dict]:
    """按计划逐个文件执行，返回每个文件的结果

    Returns:
        [{from, to, status: done/skipped/failed, msg}]（路径为相对下载目录）
    """
    songs, tasks = _song_lookup()
    results = []
    old_dirs = set()
    for plan in plans:
        result = {
            "from": plan.src.relative_to(out_dir).as_posix(),
            "to": plan.dst.relative_to(out_dir).as_posix(),
            "status": "done",
            "msg": "",
        }
        results.append(result)

        # 步骤 1：计划里就要跳过的
        if plan.skip:
            result["status"] = "skipped"
            result["msg"] = plan.skip
            continue
        if not plan.tags and plan.dst == plan.src:
            result["status"] = "skipped"
            result["msg"] = "没有变化"
            continue
        # 执行前重新确认：文件还在、目标还空着
        if not plan.src.is_file():
            result["status"] = "failed"
            result["msg"] = "文件不在了，可以重新扫描"
            continue
        if plan.dst != plan.src and plan.dst.exists():
            result["status"] = "skipped"
            result["msg"] = "新位置已有同名文件"
            continue

        # 步骤 2：改标签、挪文件
        now_at = plan.src
        try:
            if plan.tags:
                update_tags(plan.src, plan.tags)
            if plan.dst != plan.src:
                plan.dst.parent.mkdir(parents=True, exist_ok=True)
                plan.src.rename(plan.dst)
                now_at = plan.dst
                old_dirs.add(plan.src.parent)
        except Exception as e:
            logger.warning("音乐库改动失败 %s: %s", plan.src, e)
            result["status"] = "failed"
            result["msg"] = str(e)[:200]

        # 步骤 3：记录同步到文件现在的位置（失败时标签可能已改，也要同步），逐文件提交
        _sync_records(plan.src, now_at, songs, tasks)
        db.session.commit()
        logger.info("音乐库改动: %s -> %s 标签 %s", plan.src, now_at, plan.tags)

    # 步骤 4：挪空了的专辑文件夹、歌手文件夹删掉
    for folder in old_dirs:
        _remove_if_empty(out_dir, folder)
        _remove_if_empty(out_dir, folder.parent)
    return results
