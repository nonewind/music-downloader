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
_KNOWN_BOOL_CALLS = ("taskLocked(", "svcPortLocked(", "svcBusy(")


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
    if e == "migrate.busy":
        return True                      # settings.js 初始化为 false
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
