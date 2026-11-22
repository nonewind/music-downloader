"""酷狗音乐链接解析模块

包含三类解析（平台分流在 api.py 层完成）：
- parse_kugou_playlist_id：榜单 rankid / PC 歌单链接 / 纯数字（原有能力不变）
- parse_kugou_share：App 分享短链 / zlist 长链 → gcid → 整型歌单 id
- parse_kugou_link：认出链接是单曲、专辑还是歌单
  （2026-09-30 探针实测：t1.kugou.com 短链 30x Location 直接携带
  global_collection_id 参数；PC 网页版 special/single 页内嵌精确 gcid；
  collect 类分享无 specialid，唯一稳定标识是 gcid 字符串）

酷狗歌单曲目接口不可用 specialid 直查（接口文档 §6.3）：
- rankid 走 /rank/audio（get_playlist_detail 榜单分流）
- specialid/合成 id 经 _gcid_cache（api.py 预写 + 浏览缓存 + PC 页面兜底）
"""

import re
import urllib.parse


def parse_kugou_playlist_id(text: str) -> int | None:
    """从用户输入解析酷狗榜单 ID

    支持以下格式：
        - 纯数字：8888（TOP500 的 rankid）
        - 含 rankid 参数/路径的 URL
        - 分享链接中提取的末尾数字 ID

    Returns:
        榜单 ID（rankid，纯数字），解析失败返回 None
    """
    text = text.strip()
    if not text:
        return None
    # 纯数字
    if text.isdigit():
        return int(text)
    # URL 中的 rankid= 参数或 /rankid/ 路径
    m = re.search(r"rankid[=/](\d+)", text)
    if m:
        return int(m.group(1))
    # 路径中的末尾数字（分享链接格式，3 位以上避免误匹配年份等；
    # 兼容 /8888.html、/8888/、?x=1 等后缀形态）
    m = re.search(r"/(\d{3,})(?:[/?\.\s]|$)", text)
    if m:
        return int(m.group(1))
    return None


# ----------------------------------------------------------------------
# 分享链接解析（parse_kugou_share）
# ----------------------------------------------------------------------
# gcid 形态：collection_{type}_{seg2}_{seg3}_0
_GCID_RE = re.compile(r"^collection_(\d+)_(\d+)_(\d+)_(\d+)$")
_GCID_PARAM = "global_collection_id"
_JS_MAX_SAFE_INT = 2 ** 53 - 1     # 9007199254740991：Playlist.to_dict 的 id 以
# JSON number 直传浏览器（Song.id 已改 str，Playlist 未改），超界即前端精度丢失
_SHARE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _gcid_to_playlist_id(gcid: str) -> int | None:
    """gcid → 整型歌单 id（三条互斥判据，顺序求值，首条命中即返回）

    1. seg3 >= 10**4 → id = seg3 直用（实测 form1 的 seg3 == 真实 specialid）；
    2. seg3 <  10**4 → id = seg2 * 10**6 + seg3（form2/3 为 collect 分享，
       无 specialid；合成 id >= 10**13，与网易云（<=10 位）/QQ disstid（<=10 位）
       /酷狗真实 specialid（<=9 位）取值空间不重叠，撞车由 api.py 占用检查兜底）；
    3. 对最终 id 判 > 2**53 - 1 → 拒绝（越界防护在合成结果上，而非 seg3）。

    滑动段假设（固化）：判据 1 隐含「seg3 >= 10**4 即真实 specialid」的量级
    假设。实测（2026-09-30，11 个 specialid 的 PC 页）form3 的 seg3 全为小
    slid（1/6/8/26/35/225/510/598）、form1 的 seg3==specialid（5 位+）、
    form2 的 seg3=1，当前成立；若未来 slid 突破 10**4，会被误判为 specialid，
    届时需按 seg1（form 类型）细分。
    """
    m = _GCID_RE.match(gcid)
    if not m:
        return None
    seg2 = int(m.group(2))
    seg3 = int(m.group(3))
    if seg3 >= 10 ** 4:
        pid = seg3
    else:
        pid = seg2 * 10 ** 6 + seg3
    if pid > _JS_MAX_SAFE_INT:
        return None
    return pid


def _is_kugou_host(host: str) -> bool:
    """酷狗域判定（先验 SSRF 约束）：host == "kugou.com" 或以 ".kugou.com" 结尾。
    字面 endswith("kugou.com") 会放行 notkugou.com，必须带点比对。"""
    return host == "kugou.com" or host.endswith(".kugou.com")


def _expand_kugou_url(url: str, timeout: float = 8.0) -> str | None:
    """展开酷狗短链：allow_redirects=False 手动循环，逐跳先验校验 host（<=5 跳）。

    任一跳 URL（含 Location）带 global_collection_id= 参数即提前返回该 URL；
    非酷狗域 / 非 30x / 超限 / 任意异常 → None。"""
    import requests

    try:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": _SHARE_UA})
        cur = url
        for _ in range(5):
            parts = urllib.parse.urlparse(cur)
            host = (parts.hostname or "").lower()
            if not _is_kugou_host(host):
                return None
            if _GCID_PARAM in urllib.parse.parse_qs(parts.query):
                return cur
            r = s.get(cur, allow_redirects=False, timeout=timeout)
            if not (300 <= r.status_code < 400):
                return None
            loc = r.headers.get("Location")
            if not loc:
                return None
            # 相对路径 Location 先 urljoin 再进下一跳判定
            cur = urllib.parse.urljoin(cur, loc)
        return None
    except Exception:
        return None


def _first_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s\"'<>]+", text)
    return m.group(0) if m else None


def parse_kugou_share(text: str, expand=None) -> dict | None:
    """识别酷狗分享链接（App 分享短链 / zlist 长链），提取 gcid 并合成整型 id

    支持输入：
        - zlist 长链 / 展开后的链接（本身含 global_collection_id= 参数）：零网络
        - App 分享短链（t1.kugou.com/xxx）：展开一次取 Location 中的参数
          （仅对 *.kugou.com 域发起请求，先验 SSRF 约束）

    Args:
        text: 用户输入原文
        expand: 可注入的 URL 展开器（测试用），默认 _expand_kugou_url

    Returns:
        {"id": int, "gcid": str}；非 http 输入 / 展开失败 / 无 gcid /
        无法合成 id → None（调用方回退 parse_kugou_playlist_id）
    """
    text = (text or "").strip()
    if "http" not in text:
        return None
    url = _first_url(text)
    if not url:
        return None
    gcid = ""
    # ① 文本首个 URL 本身带参数（zlist 长链 / 用户粘贴的展开后链接）：零网络
    if _GCID_PARAM in url:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        vals = qs.get(_GCID_PARAM) or []
        gcid = str(vals[0]).strip() if vals else ""
    # ② 短链：展开一次取参数
    if not gcid:
        expander = expand or _expand_kugou_url
        expanded = expander(url)
        if not expanded:
            return None
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(expanded).query)
        vals = qs.get(_GCID_PARAM) or []
        if not vals:
            return None
        gcid = str(vals[0]).strip()
    if not gcid:
        return None
    pid = _gcid_to_playlist_id(gcid)
    if pid is None:
        return None
    return {"id": pid, "gcid": gcid}


# ----------------------------------------------------------------------
# 认链接类型（parse_kugou_link）
# ----------------------------------------------------------------------
# 单曲：链接里直接带 album_audio_id（/krm/audio 与取流用的歌曲 ID）
_SONG_RES = (
    re.compile(r"[?&]album_audio_id=(\d+)"),
    re.compile(r"[?&]mixsongid=(\d+)"),
)
# 专辑：www.kugou.com/album/123.html、/album/info/123、/yy/album/single/123.html
_ALBUM_RE = re.compile(r"/album/(?:info/|single/)?(\d+)")
# 歌单：gcid 参数 / PC 歌单页 / 榜单 rankid
_PLAYLIST_GCID_RE = re.compile(r"[?&]global_collection_id=([0-9a-z_]+)")
_PLAYLIST_RES = (
    re.compile(r"/special/single/(\d+)"),
    re.compile(r"rankid[=/](\d+)"),
)


def parse_kugou_link(url: str) -> tuple[str, str] | None:
    """认出酷狗链接的类型和 ID

    支持以下格式：
        - 单曲：带 album_audio_id= 或 mixsongid= 参数的链接
        - 专辑：/album/<id>.html、/album/info/<id>、/yy/album/single/<id>.html
        - 歌单：带 global_collection_id= 参数、/special/single/<id>.html、rankid

    短链（t1.kugou.com）本身认不出，由 api.py 跟随跳转后再调本函数。
    mixsong/<编码>.html 和 song/#hash= 形态里没有 album_audio_id，认不出。

    Returns:
        ("song" | "album" | "playlist", ID 字符串：单曲 album_audio_id /
        专辑 album_id / 歌单 gcid、specialid 或 rankid)；认不出返回 None
    """
    # 第一步：单曲
    for pattern in _SONG_RES:
        m = pattern.search(url)
        if m:
            return "song", m.group(1)
    # 第二步：专辑
    m = _ALBUM_RE.search(url)
    if m:
        return "album", m.group(1)
    # 第三步：歌单
    m = _PLAYLIST_GCID_RE.search(url)
    if m:
        return "playlist", m.group(1)
    for pattern in _PLAYLIST_RES:
        m = pattern.search(url)
        if m:
            return "playlist", m.group(1)
    return None
