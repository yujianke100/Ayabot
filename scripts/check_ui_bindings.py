"""模板里用到、但 setup 没返回的变量 —— 一类「页面静默坏掉」的 bug。

真实案例：导出页的日期输入框是 `:value="eDateLabel"`，而 `eDateLabel` 这个
computed **没有出现在 setup 的 return 里**。Vue 不会报错，它只是把 undefined
绑上去 —— 结果用户永远看到一个空的「点击选择日期」框，选了哪天也看不见。
这类问题不会让任何后端自检变红，界面上也没有任何报错，只能靠人眼发现。

这里把模板里的**简单绑定**全部抽出来（`:x="name"`、`v-model="name"`、
`v-if="name"`、`{{ name }}`、`@click="fn"`、`:x="fn(...)"`、
`:x="obj.prop"`），逐个对照 setup 返回的键名。多标识符的表达式跳过
（噪音太大），但漏掉的那些通常也长这样。

    ./.venv/Scripts/python.exe scripts/check_ui_bindings.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "app" / "web" / "server.py"

#: 模板里合法但不来自 setup 的标识符
JS_GLOBALS = {
    "Math", "JSON", "Date", "Number", "String", "Boolean", "Array", "Object",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "Set", "Map", "Promise", "RegExp", "Error",
    "console", "window", "document", "location", "navigator", "undefined", "null",
    "true", "false", "Infinity", "NaN",
}
JS_KEYWORDS = {
    "if", "else", "return", "typeof", "in", "of", "new", "void", "delete",
    "await", "async", "function", "try", "catch", "finally", "throw", "class",
    "instanceof", "this", "let", "const", "var", "do", "while", "for", "switch",
    "case", "break", "continue", "default", "yield", "with",
}


def extract_html(src: str) -> str:
    start = src.index('INDEX_HTML = r"""')
    start = src.index('"""', start) + 3
    end = src.index('"""', start)
    return src[start:end]


def extract_returned_keys(src: str) -> set[str]:
    """抓 setup 里那个大 return 对象的键名。

    定位方式：从 `loggedIn, loginUser, loginPass` 那一行往前找最近的
    `return {`，再一直读到与之配对的 `};`。
    """
    anchor = src.index("loggedIn, loginUser, loginPass")
    start = src.rindex("return {", 0, anchor)
    i = src.index("{", start)
    depth = 0
    end = i
    for j in range(i, len(src)):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    body = src[i : end + 1]
    # 先把 `// ...` 注释整行删掉：return 对象里带注释（说明性文字）时，
    # 注释里的标点会把「下一个键」粘成一个以 // 开头的 token，从而被漏掉。
    body = re.sub(r"//[^\n]*", "", body)
    keys: set[str] = set()
    # 去掉嵌套对象/函数体，只取顶层键
    depth = 0
    token = ""
    top_tokens: list[str] = []
    for ch in body:
        if ch in "{([":
            depth += 1
            continue
        if ch in "})]":
            depth -= 1
            continue
        if depth == 1:
            if ch in ",":
                top_tokens.append(token)
                token = ""
            else:
                token += ch
    top_tokens.append(token)
    for t in top_tokens:
        t = t.strip()
        m = re.match(r"^([A-Za-z_$][\w$]*)\s*(?::|$)", t)
        if m:
            keys.add(m.group(1))
    return keys


def identifiers_in(expr: str) -> str | None:
    """从绑定表达式里取「模板依赖的那个名字」，取不到就返回 None。"""
    e = expr.strip()
    if not e:
        return None
    # 简单标识符：name
    m = re.fullmatch(r"([A-Za-z_$][\w$]*)", e)
    if m:
        return m.group(1)
    # 调用/属性访问：fn(...) / obj.prop / arr[i]
    m = re.match(r"^([A-Za-z_$][\w$]*)\s*[.([]", e)
    if m:
        return m.group(1)
    return None


BIND_ATTRS = re.compile(
    r'(?:^|\s)(?:v-if|v-else-if|v-show|v-model(?:\.\w+)*|v-html|v-text|v-for|'
    r':[A-Za-z0-9_.\-]+|@[A-Za-z0-9_.\-]+|v-bind:[A-Za-z0-9_.\-]+)="([^"]*)"',
    re.S,
)


def main() -> int:
    src = SERVER.read_text(encoding="utf-8")
    html = extract_html(src)
    keys = extract_returned_keys(src)
    if not keys:
        print("FAIL 没能解析出 setup 的 return 键名（脚本要跟着改）")
        return 1

    # v-for 引入的局部变量：v-for="(item, i) in list" / v-for="x in list"
    for m in re.finditer(r'v-for="\(?([^)"]*)\)?\s+in\s+', html):
        for name in re.split(r"[,\s]+", m.group(1)):
            name = name.strip()
            if name:
                keys.add(name)

    used: dict[str, list[str]] = {}

    def collect(expr: str, where: str) -> None:
        # 去掉字符串字面量，避免把文案当成变量
        cleaned = re.sub(r"'[^']*'|\"[^\"]*\"", "''", expr)
        name = identifiers_in(cleaned)
        if not name:
            return
        if name in JS_GLOBALS or name in JS_KEYWORDS or name.startswith("$"):
            return
        used.setdefault(name, []).append(where)

    for m in BIND_ATTRS.finditer(html):
        collect(m.group(1), m.group(0)[:40])

    for m in re.finditer(r"\{\{(.*?)\}\}", html, re.S):
        collect(m.group(1), "{{ " + m.group(1).strip()[:30] + " }}")

    missing = {k: v for k, v in used.items() if k not in keys}
    # 过滤掉模板里的小写单词误判（属性值里常见的一两个字母）
    missing = {k: v for k, v in missing.items() if len(k) > 2}

    print(f"setup 返回 {len(keys)} 个名字；模板里用到 {len(used)} 个")
    for name, wheres in sorted(missing.items()):
        print(f"  FAIL 模板用了 {name!r}，但 setup 没返回（出现 {len(wheres)} 次，"
              f"例如 {wheres[0]}）")
    print()
    if missing:
        print(f"UI BINDINGS FAILED（{len(missing)} 个变量永远取不到值）")
        return 1
    print("UI BINDINGS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
