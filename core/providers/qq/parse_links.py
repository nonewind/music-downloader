"""QQ音乐链接解析模块

- parse_qq_playlist_id：歌单 ID（纯数字 / 歌单链接）
- parse_qq_link：认出链接是单曲、专辑还是歌单
（平台分流在 api.py 层完成）
"""

import re

# 手机分享歌单页：taoge.html?id= / taoge/index.html?id=
_TAOGE_RE = re.compile(r"taoge[^?\s]*\?(?:[^#\s]*&)?id=(\d+)")

# 单曲：新版详情页 / 旧版 .html 页 / 手机播放页 songmid= 参数
_SONG_RES = (
    re.compile(r"/songDetail/([0-9A-Za-z]+)"),
    re.compile(r"/song/([0-9A-Za-z]+)\.html"),
    re.compile(r"[?&]songmid=([0-9A-Za-z]+)", re.I),
)
# 专辑：新版详情页 / 旧版 .html 页 / 手机分享页 albummid= 参数
_ALBUM_RES = (
    re.compile(r"/albumDetail/([0-9A-Za-z]+)"),
    re.compile(r"/album/([0-9A-Za-z]+)\.html"),
    re.compile(r"[?&]albummid=([0-9A-Za-z]+)", re.I),
)
# 歌单：歌单页 / disstid= 参数 / 手机分享页
_PLAYLIST_RES = (
    re.compile(r"/playlist/(\d+)"),
    re.compile(r"[?&]disstid=(\d+)"),
    _TAOGE_RE,
)


def parse_qq_playlist_id(text: str) -> int | None:
    """从用户输入解析 QQ 歌单 ID

    支持以下格式：
        - 纯数字：7707261125
        - 歌单页链接：https://y.qq.com/n/ryqq/playlist/7707261125
        - 含 disstid= 参数的 URL
        - 手机分享页：https://i.y.qq.com/n2/m/share/details/taoge.html?id=7707261125
        - 分享文案末尾数字

    Returns:
        歌单 ID（disstid，纯数字），解析失败返回 None
    """
    text = text.strip()
    if not text:
        return None
    # 纯数字
    if text.isdigit():
        return int(text)
    # URL 中的 disstid= 参数
    m = re.search(r"[?&]disstid=(\d+)", text)
    if m:
        return int(m.group(1))
    # 歌单页路径 /playlist/xxx
    m = re.search(r"/playlist/(\d+)", text)
    if m:
        return int(m.group(1))
    # 手机分享页 taoge.html?id=xxx
    m = _TAOGE_RE.search(text)
    if m:
        return int(m.group(1))
    # 路径中的末尾数字（分享文案格式）
    m = re.search(r"/(\d{5,})(?:/|\s|$)", text)
    if m:
        return int(m.group(1))
    return None


def parse_qq_link(url: str) -> tuple[str, str] | None:
    """认出 QQ 音乐链接的类型和 ID

    支持以下格式：
        - 单曲：https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV、
          旧版 /n/yqq/song/<mid>.html、手机播放页 ?songmid=<mid>
        - 专辑：https://y.qq.com/n/ryqq/albumDetail/002jLGWe16Tf1H、
          旧版 /n/yqq/album/<mid>.html、?albummid=<mid>
        - 歌单：/playlist/<id>、?disstid=<id>、taoge.html?id=<id>

    短链（c6.y.qq.com/base/fcgi-bin/u?__=…）本身认不出，由 api.py
    跟随跳转后再调本函数。只有数字 songid、没有 songmid 的链接认不出。

    Returns:
        ("song" | "album" | "playlist", ID 字符串：单曲 songmid / 专辑 albummid /
        歌单 disstid)；认不出返回 None
    """
    # 第一步：单曲
    for pattern in _SONG_RES:
        m = pattern.search(url)
        if m:
            return "song", m.group(1)
    # 第二步：专辑
    for pattern in _ALBUM_RES:
        m = pattern.search(url)
        if m:
            return "album", m.group(1)
    # 第三步：歌单
    for pattern in _PLAYLIST_RES:
        m = pattern.search(url)
        if m:
            return "playlist", m.group(1)
    return None
