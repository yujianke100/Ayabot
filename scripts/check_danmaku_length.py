"""弹幕长度：B站 按 UTF-16 计，我们按码点计 —— 带 emoji 的昵称会让回复被静默丢弃。

真实场景：观众昵称里有 emoji（B站 上很常见），渲染出的感谢文案
`感谢 🎉🎊🎈🎁🎂 送出的辣条 x1` 只有 **17 个 Python 字符**，
但它是 **22 个 UTF-16 code unit** —— B站 前端是 JS，`String.length` 数的是后者。
于是「看起来没超长」的回复被拒（code 1003212），
而 `_is_fatal_send_error` 判定为不可重试 → **直接丢掉，用户看到机器人不回复**。

本脚本断言：
  [1] `_cap_text` 同时满足码点和 UTF-16 两个口径
  [2] 纯中文/英文文案行为完全不变（不能因为修 emoji 而把正常文案截短）
  [3] 超长被拒时要降级重发，而不是直接丢弃
  [4] 限流/封禁类错误仍然直接放弃（不能因为新增降级而变成无限重试）

    ./.venv/Scripts/python.exe scripts/check_danmaku_length.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.bot import LiveRobot, _cap_text, _utf16_len

    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    print("[1] 两种口径都要满足")
    cases = [
        ("纯中文", "感谢小明送出的辣条x1"),
        ("纯英文", "Thanks for your gift!"),
        ("少量 emoji", "感谢 🎉🎊🎈🎁🎂 送出的辣条 x1"),
        ("全是 emoji", "🎉" * 40),
        ("生僻字", "𠀀𠀁𠀂𠀃𠀄" * 8),
        ("中英混排", "感谢 alice 送出的 Super Chat x1"),
    ]
    for label, text in cases:
        capped = _cap_text(text, 20)
        ok(f"{label:10s} 码点 <= 20", len(capped) <= 20, len(capped))
        ok(f"{label:10s} UTF-16 <= 20", _utf16_len(capped) <= 20, _utf16_len(capped))

    print("[2] 短文案一律不动（不能因为修 emoji 而误截）")
    for text in ("感谢小明送出的辣条x1", "签到成功，本月第3天", "谢谢你的礼物"):
        ok(f"{text[:10]:12s} 原样返回", _cap_text(text, 20) == text, _cap_text(text, 20))

    print("[3] 长文案截断后要有省略号，且不超出上限")
    long_text = "感谢" + "很长的名字" * 10 + " 送出的礼物"
    capped = _cap_text(long_text, 20)
    ok("码点 <= 20", len(capped) <= 20, len(capped))
    ok("UTF-16 <= 20", _utf16_len(capped) <= 20, _utf16_len(capped))
    ok("结尾是省略号", capped.endswith("…"), capped)
    ok("是原文的前缀（没有乱码）", long_text.startswith(capped[:-1]), capped)

    print("[4] max_len 边界值")
    ok("max_len=1 不崩", isinstance(_cap_text("abc", 1), str), repr(_cap_text("abc", 1)))
    ok("max_len=0 返回空", _cap_text("abc", 0) == "", repr(_cap_text("abc", 0)))
    ok("空文本返回空", _cap_text("", 20) == "", repr(_cap_text("", 20)))

    print("[5] 长度类错误的判定")
    for err, expect in (
        ("code=1003212, message=超出限制长度", True),
        ("弹幕长度超过限制", True),
        ("文字太长", True),
        ("发送频率过快，请稍后再试", False),
        ("账号被封禁", False),
        ("-101 账号未登录", False),
    ):
        got = LiveRobot._is_length_error(err)
        ok(f"{err[:22]:24s} -> {expect}", got is expect, got)

    print("[6] 结构：必须降级重发，不能直接丢")
    src = (ROOT / "app/bot.py").read_text(encoding="utf-8")
    i_fatal = src.index("if self._is_fatal_send_error(err_str):")
    seg = src[i_fatal:i_fatal + 1600]
    ok("有 length_retry_left", "length_retry_left" in seg)
    ok("降级时砍一半", "max(4, len(before) // 2)" in seg)
    ok("降级后 continue 重发", "continue" in seg)
    ok("次数用尽才报永久失败", "永久失败（不重试）" in seg)
    ok("限流/封禁仍然不降级（只对长度类降级）", "self._is_length_error(err_str) and length_retry_left > 0" in seg)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails[:6]}")
        return 1
    print("DANMAKU LENGTH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
