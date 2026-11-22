"""网易云链接解析模块

- parse_playlist_id：歌单 ID（纯数字 / 歌单链接）
- parse_netease_link：认出链接是单曲、专辑还是歌单（平台分流在 api.py 层完成）
"""

import re


def parse_playlist_id(text: str) -> int | None:
    """从用户输入解析歌单 ID

    支持以下格式：
        - 纯数字：3778678
        - 网易云分享链接：https://music.163.com/playlist?id=3778678
        - 短链接：https://y.music.163.com/m/playlist?id=3778678
        - 分享文案：「...」https://y.music.163.com/.../3778678/...

    Returns:
        歌单 ID，解析失败返回 None
    """
    text = text.strip()
    if not text:
        return None
    # 纯数字
    if text.isdigit():
        return int(text)
    # URL 中的 id= 参数
    m = re.search(r"[?&]id=(\d+)", text)
    if m:
        return int(m.group(1))
    # 路径中的数字（短链接格式 /playlist/xxx 或末尾数字）
    m = re.search(r"/(\d{5,})(?:/|\s|$)", text)
    if m:
        return int(m.group(1))
    return None


# 单曲 / 专辑 / 歌单链接：?id= 参数形态（含 /#/、/m/ 前缀和 y.music.163.com）
# 与路径形态 /song/123
_LINK_PARAM_RE = re.compile(r"(song|album|playlist)\?(?:[^#\s]*&)?id=(\d+)")
_LINK_PATH_RE = re.compile(r"/(song|album|playlist)/(\d+)")


def parse_netease_link(url: str) -> tuple[str, str] | None:
    """认出网易云链接的类型和 ID

    支持以下格式：
        - https://music.163.com/song?id=186016（album / playlist 同理）
        - https://music.163.com/#/song?id=186016
        - https://y.music.163.com/m/song?id=186016&userid=1（分享链接）
        - 路径形态 /song/186016

    短链（163cn.tv）本身认不出，由 api.py 跟随跳转后再调本函数。

    Returns:
        ("song" | "album" | "playlist", ID 字符串)；认不出返回 None
    """
    # 第一步：?id= 参数形态
    m = _LINK_PARAM_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    # 第二步：路径形态
    m = _LINK_PATH_RE.search(url)
    if m:
        return m.group(1), m.group(2)
    return None
