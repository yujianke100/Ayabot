"""配置里有、代码不读：`pk_report_template` 改了完全没效果。

`config.example.yaml` 和 WebUI 的「机器人配置」都能看到 PK 汇报模板，
`config_to_dict` 也会把它原样输出（所以「导出配置」里能看到自己改的值），
但 `_handle_pk_start` 里是**写死的三行**：

    line1 = f"PK开始！对手{opp_name}"
    line2 = f"对手 {_fmt(fans)}粉，{guard_count}舰"
    line3 = f"{online}观众，贡献{total_score}"

用户把模板改成「PK来了！对手{opponent}」，PK 开始时看到的还是那三行。
这和 `rate_limit.reply_delay_seconds` 是同一类 bug（第 43 轮修过）：
**配置项读进了 AppConfig、也输出给前端，但没有任何代码消费它。**

修法：先按模板渲染；放得进一条弹幕（20 字，UTF-16 口径）就发用户那一条，
超长才退回三行拆发（默认模板 5 个占位符必然超长，行为与以前一致）。

    ./.venv/Scripts/python.exe scripts/check_pk_report_template.py
"""
from __future__ import annotations

import ast
import asyncio
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    import yaml
    from app.bot import LiveRobot
    from app.config import load_config

    tmp = Path(tempfile.mkdtemp(prefix="pktpl_"))
    try:
        raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        raw.setdefault("features", {})["pk_report_enabled"] = True
        raw["features"]["pk_end_report_enabled"] = False

        class _FakeAuth:
            status = "ok"

            def write_status(self, *a, **k) -> None:
                pass

        def make_robot(features_patch: dict) -> tuple[LiveRobot, list[str]]:
            cfg_raw = dict(raw)
            f = dict(raw["features"])
            f.update(features_patch)
            cfg_raw["features"] = f
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(yaml.safe_dump(cfg_raw, allow_unicode=True, sort_keys=False),
                                encoding="utf-8")
            cfg = load_config(str(cfg_path))
            robot = LiveRobot(config=cfg, credential=object(), auth=_FakeAuth())
            sent: list[str] = []

            async def cap(text: str, reply_uid=None, **kw) -> None:
                sent.append(text)

            robot._enqueue_message = cap     # type: ignore[assignment]
            robot._enqueue_reply = cap       # type: ignore[assignment]
            return robot, sent

        # 只用 uname（不带 uid）→ 不会去请求 B站 接口，测试不联网
        PK_EVENT = {"type": "PK_BATTLE_PRE", "data": {"cmd": "PK_BATTLE_PRE",
                                                     "data": {"uname": "小明"}}}

        print("[1] 短模板（一条放得下）→ 就发用户那一条")
        robot, sent = make_robot({"pk_report_template": "PK来了！对手{opponent}"})
        asyncio.run(robot._handle_pk_start(PK_EVENT))
        ok("只发一条", len(sent) == 1, sent)
        ok("内容是用户模板渲染的结果", sent and sent[0] == "PK来了！对手小明", sent)

        print("[2] 占位符都能用")
        robot, sent = make_robot({"pk_report_template": "{opponent}开打{score}分"})
        asyncio.run(robot._handle_pk_start(PK_EVENT))
        ok("渲染出对手名和分数", sent and sent[0] == "小明开打0分", sent)

        print("[3] 默认长模板 → 退回三行拆发（和以前一致）")
        robot, sent = make_robot({})      # 用 example 里的默认值
        asyncio.run(robot._handle_pk_start(PK_EVENT))
        ok("发了三条", len(sent) == 3, sent)
        ok("第一条是「PK开始！对手…」", sent and sent[0].startswith("PK开始！对手"), sent)
        ok("每条都在 20 字以内", all(len(x) <= 20 for x in sent), [len(x) for x in sent])

        print("[4] 超长但仍算「用户模板」时不静默丢信息")
        # 必须真的超过 20 个 UTF-16 单元，否则这条断言什么都测不到
        long_tpl = "{opponent}和我们打PK啦，大家快来帮忙顶一下人气谢谢啦"
        robot, sent = make_robot({"pk_report_template": long_tpl.replace("{opponent}", "小明")})
        asyncio.run(robot._handle_pk_start(PK_EVENT))
        ok("退回三行（否则整条会被 B站 拒收）", len(sent) == 3, sent)

        print("[5] 关掉 PK 汇报就一条都不发")
        robot, sent = make_robot({"pk_report_enabled": False})
        asyncio.run(robot._handle_pk_start(PK_EVENT))
        ok("没有消息", not sent, sent)

        print("[6] 全量审计：每个配置字段都要被代码消费")
        # 抓 AppConfig 各 dataclass 的字段，看有没有任何非 config.py 的代码引用它。
        # 允许清单里是**确实不再使用**的历史字段（保留是为了不破坏老配置文件）。
        ALLOW_ONLY_DECLARED = {"uid_configs"}
        cfg_tree = ast.parse((ROOT / "app" / "config.py").read_text(encoding="utf-8"))
        declared: list[str] = []
        for node in cfg_tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Config"):
                for item in node.body:
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        declared.append(item.target.id)
        others = [p for p in (ROOT / "app").rglob("*.py") if p.name != "config.py"]
        others += list((ROOT / "scripts").glob("*.py"))
        blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in others)
        dead = [n for n in declared
                if n not in ALLOW_ONLY_DECLARED
                and not re.search(rf"\.{re.escape(n)}\b", blob)]
        ok(f"{len(declared)} 个配置字段都被消费了", not dead, dead)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails}")
        return 1
    print("PK REPORT TEMPLATE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
