"""模板 :disabled 绑定回归测试（v0.9.0.0「搜索后点下载无反应」）

Alpine 3 的 x-bind 对"含点号的表达式"结果为 undefined 时会改写成 ""，
而 disabled="" 等于禁用。发现页搜索结果行没有 busy/downloading 字段，
`:disabled="s.busy || s.downloading"` 因此让全部下载按钮恒为禁用。

规则：表达式含点号的 :disabled 必须能保证得到布尔值——用 !!(...) 包裹，
或者是比较/取反/Set.has 这类必然返回布尔的写法，或者是返回布尔的方法调用。
"""

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "webapp" / "templates"

# :disabled="..." 与 ui.btn attrs 里的 ':disabled': '...'
_PATTERNS = [
    re.compile(r':disabled="([^"]*)"'),
    re.compile(r"':disabled':\s*'([^']*)'"),
]

# 已确认必返回布尔的写法（方法内部 return true/false 或 !!）
_KNOWN_BOOL_CALLS = ("taskLocked(", "isSvcCustom(")


def _is_safe(expr: str) -> bool:
    e = expr.strip()
    if "." not in e:
        return True                      # 无点号：Alpine 不做 "" 改写
    if e.startswith("!!(") or e.startswith("!"):
        return True
    if re.search(r"===|!==|==|!=|<=|>=|<|>", e) and "||" not in e and "&&" not in e:
        return True                      # 单一比较表达式
    if re.fullmatch(r"[\w.]+\.has\(.*\)", e) and "||" not in e and "&&" not in e:
        return True                      # Set.has 必返回布尔
    if e.startswith(_KNOWN_BOOL_CALLS) and "||" not in e:
        return True
    return False


def test_disabled_bindings_are_boolean():
    bad = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        text = f.read_text(encoding="utf-8")
        for pat in _PATTERNS:
            for m in pat.finditer(text):
                if not _is_safe(m.group(1)):
                    line = text.count("\n", 0, m.start()) + 1
                    bad.append(f"{f.name}:{line}: {m.group(1)}")
    assert not bad, "以下 :disabled 可能得到 undefined→\"\"→禁用，请用 !!(...) 包裹：\n" + "\n".join(bad)


# ----------------------------------------------------------------------
# 页面样式统一（v0.13.2.1 / v0.13.2.2「下载页控件和文字比其他页面大」）
#
# 下载页的两个标签曾自己写了一套分段按钮（写死颜色/阴影/间距）；公共列表行默认
# 标题 17px，比音乐库的歌曲列表（15px）大一号，找歌、设置、下载三页都跟着大。
# 规则：标签用公共选项片 chip；文字字号只用 app.css 里的 --ui-fs-* 变量（图标除外）；
# 下载页局部样式不写死颜色和阴影。
# ----------------------------------------------------------------------
CSS = TEMPLATES.parent / "static" / "css" / "app.css"

# 图标的选择器（<i> 图标、封面占位、关闭按钮的 ×）：图标大小不算文字字号
_ICON_SELECTOR = re.compile(r"(\bi\b(\s*[,{]|\s*$))|-ph\b|close|disc-clear")


def _history_html() -> str:
    return (TEMPLATES / "history.html").read_text(encoding="utf-8")


def test_history_tabs_use_shared_chip():
    text = _history_html()
    # 第 1 步：标签栏是公共的 chips，不再有自定义分段按钮
    assert 'class="chips hist-tabs"' in text
    assert "hist-seg" not in text
    # 第 2 步：两个标签按钮都是 chip
    assert text.count('class="chip" role="tab"') == 2


def test_history_style_has_no_hardcoded_colors():
    text = _history_html()
    style = text[text.index("<style>"):text.index("</style>")]
    # 颜色和阴影只用 app.css 里的变量
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", style)
    assert "rgba(" not in style and "rgb(" not in style


def test_list_row_matches_library_song_size():
    css = CSS.read_text(encoding="utf-8")
    # 公共列表行：标题和正文一样大，第二行和右侧是次要字号
    assert re.search(r"\.list-title \{[^}]*font-size: var\(--ui-fs-base\)", css)
    assert re.search(r"\.list-sub \{[^}]*font-size: var\(--ui-fs-s\)", css)
    assert re.search(r"\.list-side \{[^}]*font-size: var\(--ui-fs-s\)", css)


def test_text_font_sizes_use_tokens():
    bad = []
    files = [CSS] + sorted(TEMPLATES.glob("*.html"))
    for f in files:
        selector = ""
        for n, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            # 第 1 步：记下这一行所在规则的选择器
            if "{" in line:
                selector = line.split("{")[0]
            # 第 2 步：写死 px 的字号，除非是图标
            m = re.search(r"font-size:\s*\d+px", line)
            if m is None:
                continue
            if line.lstrip().startswith("--"):
                continue                     # 变量定义本身
            if _ICON_SELECTOR.search(selector):
                continue
            bad.append(f"{f.name}:{n}: {line.strip()}")
    assert not bad, "文字字号请用 --ui-fs-* 变量：\n" + "\n".join(bad)


def test_list_row_buttons_are_small():
    bad = []
    for f in sorted(TEMPLATES.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'class="list-side', text):
            # 第 1 步：取到这一行结束（下一个列表行、模板循环或宏结束）为止
            rest = text[m.end():]
            end = len(rest)
            for stop in ('class="list-row', "</template>", "endmacro"):
                i = rest.find(stop)
                if i != -1 and i < end:
                    end = i
            side = rest[:end]
            # 第 2 步：右侧的按钮都用小号，和下载页、设置页一样高
            for b in re.findall(r'class="(ui-btn [^"]*)"', side):
                if "ui-btn-sm" not in b:
                    line = text.count("\n", 0, m.start()) + 1
                    bad.append(f"{f.name}:{line}: {b}")
    assert not bad, "列表行右侧按钮请加 ui-btn-sm：\n" + "\n".join(bad)
