"""校验弹幕发送的「长度上限」与「不该重试的错误」。

背景（两个真实 bug）：
  1. 发送前截断写死 30 字。B站 2024-03 下线了 UL 成就的「30 字」特权，
     普通账号**只能发 20 字**，超长被拒（code 1003212）。于是默认的
     「感谢 {uname} 的 {gift_name} x{num}」在用户名稍长时直接发不出去。
  2. 超长/频率类错误被当成可重试错误：内层重试 3 次 + 外层重新入队 3 次
     = 每条消息打 9 次接口，只会把限流窗口拖得更长。

另外一并锁住：`rate_limit.max_queue_size` / `reply_delay_seconds` /
`max_text_len` 必须真的从 config.yaml 读进来（以前前两项一直用 dataclass 默认值，
WebUI 里改完界面显示 3.0 秒、机器人实际按 1.0 秒发）。

用法: ./.venv/Scripts/python.exe scripts/check_send_limit.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/send_limit_test")


def build_config(**rate_limit) -> str:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(
        (ROOT / "rooms" / "1946287911" / "config.yaml").read_text(encoding="utf-8"))
    base["storage"] = {"sqlite_path": "bot.db"}
    base["auth"] = {"auto_login": True, "credential_store_path": "cred.json",
                    "qr_poll_seconds": 1.0, "refresh_interval_seconds": 3600}
    base["rate_limit"] = dict(base.get("rate_limit") or {}, **rate_limit)
    path = SCRATCH / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


class _SendStub:
    """替身 live_room：记录调用次数，按预设抛错。"""

    def __init__(self, errors: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.errors = list(errors or [])

    async def send_danmaku(self, danmaku=None, room_id=None, reply_mid=None):  # noqa: ANN001
        self.calls.append(getattr(danmaku, "text", ""))
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        return {"code": 0}

    async def get_room_play_info(self):  # noqa: ANN201
        return {"room_id": 1, "live_status": 0}


async def drive(robot, text: str, *, wait: float = 0.6) -> None:
    """把一条消息塞进队列并等它被处理掉。"""
    task = asyncio.create_task(robot._message_worker())
    try:
        await robot._enqueue_message(text=text, reply_uid=1)
        await asyncio.sleep(wait)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def main() -> int:
    from app.bot import LiveRobot
    from app.config import load_config

    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)

    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    # ── 1. 配置里这三项必须真的被读到 ──
    print("[1] rate_limit 三个字段必须从 config.yaml 读进来")
    cfg = load_config(build_config(max_text_len=25, max_queue_size=7, reply_delay_seconds=3.5))
    print(f"  max_text_len={cfg.rate_limit.max_text_len} "
          f"max_queue_size={cfg.rate_limit.max_queue_size} "
          f"reply_delay_seconds={cfg.rate_limit.reply_delay_seconds}")
    ok("max_text_len 生效", cfg.rate_limit.max_text_len == 25, cfg.rate_limit.max_text_len)
    ok("max_queue_size 生效（以前被忽略）", cfg.rate_limit.max_queue_size == 7,
       cfg.rate_limit.max_queue_size)
    ok("reply_delay_seconds 生效（以前被忽略）",
       abs(cfg.rate_limit.reply_delay_seconds - 3.5) < 1e-6,
       cfg.rate_limit.reply_delay_seconds)

    # 缺省时应回落到 20（B站普通账号上限）
    cfg_default = load_config(build_config())
    ok("缺省 max_text_len = 20", cfg_default.rate_limit.max_text_len == 20,
       cfg_default.rate_limit.max_text_len)

    # ── 2. 截断按 max_text_len，且带省略号 ──
    print("[2] 发送前按上限截断")
    cfg2 = load_config(build_config(max_text_len=20, send_interval_seconds=0.1,
                                    retry_count=0, reply_delay_seconds=0))
    robot = LiveRobot(config=cfg2)
    stub = _SendStub()
    robot.live_room = stub  # type: ignore[assignment]
    robot._apply_keyword_filter = lambda t: t  # type: ignore[assignment]

    long_text = "感谢 一个名字特别特别长的观众 的 小心心 x10"   # 明显 > 20 字
    print(f"  原文 {len(long_text)} 字: {long_text}")
    await drive(robot, long_text)
    print(f"  实发: {stub.calls}")
    ok("只发了一次", len(stub.calls) == 1, stub.calls)
    if stub.calls:
        sent = stub.calls[0]
        ok(f"发送内容不超过 20 字（实际 {len(sent)}）", len(sent) <= 20, len(sent))
        ok("被截断时带省略号", sent.endswith("…"), sent)

    # 短消息不该被改动
    stub.calls.clear()
    await drive(robot, "无盲盒记录")
    ok("短消息原样发送", stub.calls == ["无盲盒记录"], stub.calls)

    # max_text_len=30 时应允许 25 字通过
    cfg3 = load_config(build_config(max_text_len=30, send_interval_seconds=0.1,
                                    retry_count=0, reply_delay_seconds=0))
    robot3 = LiveRobot(config=cfg3)
    stub3 = _SendStub()
    robot3.live_room = stub3  # type: ignore[assignment]
    robot3._apply_keyword_filter = lambda t: t  # type: ignore[assignment]
    twenty_five = "本月盲盒共20个，花费10430，收益-1783"
    print(f"  25 字左右: {len(twenty_five)} 字")
    await drive(robot3, twenty_five)
    ok("上限设 30 时不需要截断", stub3.calls == [twenty_five], stub3.calls)

    # ── 3. 致命错误识别 ──
    print("[3] 哪些发送错误不值得重试")
    cases = [
        ("超出限制长度", True),
        ("code: 1003212", True),
        ("弹幕长度超过限制", True),
        ("超过长度限制", True),
        ("文字太长", True),
        ("发送频率过快", True),
        ("code 10031", True),
        ("账号被封禁", True),
        ("Connection reset by peer", False),
        ("timeout", False),
        ("-101 账号未登录", False),   # 登录失效要走重新登录引导，不是这里
        ("", False),
    ]
    bad = []
    for err, want in cases:
        got = LiveRobot._is_fatal_send_error(err)
        mark = "OK  " if got == want else "FAIL"
        if got != want:
            bad.append(err)
        print(f"  {mark} {err!r:<32} 致命={got} 期望={want}")
    ok("致命错误判定全部正确", not bad, bad)

    # ── 4. 超长错误：只打一次接口，不重新入队 ──
    print("[4] 超长错误只发送一次")
    cfg4 = load_config(build_config(max_text_len=20, send_interval_seconds=0.1,
                                    retry_count=2, reply_delay_seconds=0))
    robot4 = LiveRobot(config=cfg4)
    stub4 = _SendStub(errors=["超出限制长度"] * 20)
    robot4.live_room = stub4  # type: ignore[assignment]
    robot4._apply_keyword_filter = lambda t: t  # type: ignore[assignment]
    await drive(robot4, "这条消息本来就被截断了但B站还是拒绝", wait=1.2)
    print(f"  接口调用次数: {len(stub4.calls)}")
    ok("只调用 1 次（不再 3 次重试 × 3 次入队）", len(stub4.calls) == 1, len(stub4.calls))
    ok("队列里没有残留", robot4._msg_queue.qsize() == 0, robot4._msg_queue.qsize())

    # ── 5. 网络类错误仍然重试 ──
    print("[5] 网络抖动仍然重试")
    cfg5 = load_config(build_config(max_text_len=20, send_interval_seconds=0.1,
                                    retry_count=2, reply_delay_seconds=0))
    robot5 = LiveRobot(config=cfg5)
    stub5 = _SendStub(errors=["Connection reset by peer", "Connection reset by peer"])
    robot5.live_room = stub5  # type: ignore[assignment]
    robot5._apply_keyword_filter = lambda t: t  # type: ignore[assignment]
    # 内层退避是 1.0*(attempt+1) 秒，两次重试至少 3 秒，等够
    await drive(robot5, "测试重试", wait=5.0)
    print(f"  接口调用次数: {len(stub5.calls)}")
    ok("网络错误重试后成功（调用 3 次）", len(stub5.calls) == 3, len(stub5.calls))

    for r in (robot, robot3, robot4, robot5):
        try:
            r.store.close()
        except Exception:  # noqa: BLE001
            pass

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        shutil.rmtree(SCRATCH, ignore_errors=True)
        return 1
    print("\nSEND LIMIT OK")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
