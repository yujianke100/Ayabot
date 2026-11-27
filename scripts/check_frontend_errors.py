"""前端「假成功」：请求失败了，界面却说成功了。

`fetch()` 对 4xx/5xx **不抛异常** —— 它会正常 resolve，只是 `res.ok === false`。
所以这种写法是错的：

    try {
        await fetch(`/api/rooms/${id}/restart`, {method: 'POST'});
        msg.value = '✅ 已触发重启';          // 后端返回 500 时照样执行
    } catch(e) { /* ignore */ }

用户看到「已触发重启」，机器人根本没起来 —— 后台这边我们已经花了好几轮
把所有「200 + {"ok": false}」改成真 5xx，但如果前端不看状态码，等于白改。

这里扫前端脚本里的每个函数：只要它设置了「成功类文案」（✅ / 已保存 / 已触发 …），
就必须在该函数里真的检查过响应（`res.ok` / `res.status` / `data.ok` / 抛异常）。
读取类函数（load…）不产生成功文案，自然不在检查范围内。

    ./.venv/Scripts/python.exe scripts/check_frontend_errors.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "app" / "web" / "server.py"

#: 只要函数里出现这些前缀的赋值，就说明它在向用户宣告成功
SUCCESS_MARKERS = ("✅", "'已保存", '"已保存', "已触发", "已保存", "保存成功", "已复制",
                   "'已删除", "已删除", "已重启", "已启动", "已停止")

#: 函数体里必须出现其中至少一个，才算真的检查了响应
RESPONSE_CHECKS = ("res.ok", "r.ok", ".status ===", ".status===", ".status !==",
                   "data.ok", "d.ok", "result.ok", "throw new Error", "throw ")


def extract_frontend(src: str) -> str:
    start = src.index('INDEX_HTML = r"""')
    start = src.index('"""', start) + 3
    return src[start: src.index('"""', start)]


def iter_functions(html: str):
    """产出 (函数名, 行号, 函数体)。只认 `        function x(` / `        async function x(`。"""
    for m in re.finditer(r"\n        (async )?function ([A-Za-z_$][\w$]*)\(", html):
        name = m.group(2)
        body_start = m.end()
        nxt = re.search(r"\n        (?:async )?function ", html[body_start:])
        body = html[body_start: body_start + (nxt.start() if nxt else 4000)]
        line = html[: m.start()].count("\n") + 1
        yield name, line, body


def main() -> int:
    html = extract_frontend(SERVER.read_text(encoding="utf-8"))
    fails: list[str] = []

    checked = 0
    for name, line, body in iter_functions(html):
        if not any(mk in body for mk in SUCCESS_MARKERS):
            continue
        # 纯本地操作（套用模板到表单、复制到剪贴板）没有请求，谈不上"假成功"
        if "fetch(" not in body:
            continue
        checked += 1
        if not any(ck in body for ck in RESPONSE_CHECKS):
            fails.append(f"{name}()（server.py 前端第 ~{line} 行）宣告成功但没检查响应状态")
        # 反例：把 fetch 的结果直接丢掉（`await fetch(...)` 不接返回值）
        if re.search(r"await fetch\([^)]*\)\s*;", body) and \
                not any(ck in body for ck in RESPONSE_CHECKS):
            fails.append(f"{name}() 把 fetch 结果直接丢弃，失败时仍会宣告成功")

    print(f"检查了 {checked} 个「会宣告成功」的前端函数")
    for f in fails:
        print(f"  FAIL {f}")
    print()
    if fails:
        print(f"FRONTEND ERRORS FAILED（{len(fails)} 处假成功）")
        return 1
    print("FRONTEND ERRORS OK：没有「失败了还说成功」的地方")
    return 0


if __name__ == "__main__":
    sys.exit(main())
