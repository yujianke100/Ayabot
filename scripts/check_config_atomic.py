"""校验配置文件写入的原子性，以及保存时不会丢字段。

背景：config.yaml 是「一坏就全废」的文件。直接 write_text 被打断会留下截断的
YAML，之后 load_config 直接抛异常 —— 用户看到「房间打不开」且无从恢复。
这里验证：
  1. atomic_write_text 正常写入
  2. 写临时文件失败时，原文件保持不变、不留临时文件
  3. update_config_from_dict 只改传入的段，其它字段（含未知字段）原样保留
  4. 保存后 load_config 仍然能解析，且值正确
  5. 截断的 YAML 确实会让 load_config 失败（证明这个风险是真的）
  6. WebUI 的房间配置保存走的是原子写

不动真实 rooms/：全部在临时目录里。

用法: ./.venv/Scripts/python.exe scripts/check_config_atomic.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    import yaml

    from app.config import (atomic_write_text, load_config, update_config_from_dict,
                            write_default_config)

    tmp = Path(tempfile.mkdtemp(prefix="ayabot_atomic_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra=None) -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra is not None else ""))
        if not cond:
            fails.append(label)

    try:
        # ── 1. 正常写入 ──
        print("[1] atomic_write_text 正常写入")
        f = tmp / "a.yaml"
        atomic_write_text(f, "x: 1\n")
        ok("内容正确", f.read_text(encoding="utf-8") == "x: 1\n")
        atomic_write_text(f, "x: 2\n")
        ok("覆盖写入正确", f.read_text(encoding="utf-8") == "x: 2\n")
        ok("没有残留临时文件", sorted(p.name for p in tmp.iterdir()) == ["a.yaml"],
           sorted(p.name for p in tmp.iterdir()))

        # ── 2. 写入失败时原文件必须完好 ──
        print("[2] 写临时文件失败时原文件不变")
        f2 = tmp / "b.yaml"
        f2.write_text("keep: me\n", encoding="utf-8")
        real_write_text = Path.write_text

        def _boom(self, *a, **kw):
            # 只让「临时文件」写入失败，模拟磁盘满 / 被杀软拦截
            if ".tmp" in self.name:
                raise OSError(28, "No space left on device (simulated)")
            return real_write_text(self, *a, **kw)

        Path.write_text = _boom  # type: ignore[assignment]
        raised = False
        try:
            atomic_write_text(f2, "new: content\n")
        except OSError:
            raised = True
        finally:
            Path.write_text = real_write_text  # type: ignore[assignment]
        ok("失败时抛错（不静默吞掉）", raised)
        ok("原文件内容完好", f2.read_text(encoding="utf-8") == "keep: me\n",
           f2.read_text(encoding="utf-8"))
        ok("没有残留临时文件", not any(".tmp" in p.name for p in tmp.iterdir()),
           sorted(p.name for p in tmp.iterdir()))

        # ── 3. 截断的 YAML 确实会让 load_config 失败 ──
        print("[3] 截断的 YAML 会让房间加载失败（风险是真的）")
        room = tmp / "rooms" / "111222"
        (room / "data").mkdir(parents=True)
        good = room / "config.yaml"
        write_default_config(good)
        good.write_text(
            good.read_text(encoding="utf-8") +
            "\nfeatures:\n  thanks_enabled: true\n  thanks_template: \"谢谢 {uname}\"\n",
            encoding="utf-8")
        load_config(str(good))  # 能正常加载
        ok("完整配置能加载", True)
        # 模拟「写到一半断了」：在字符串中间截断。
        # 关键点：截断不一定报错！YAML 只要前半段语法完整就会解析成功，
        # 尾巴上的 features / custom_fortunes 整段消失，房间照常启动、配置静默丢失。
        full = good.read_text(encoding="utf-8")
        silent_loss = False
        errored = False
        for cut in (0.4, 0.55, 0.7, 0.85):
            good.write_text(full[: int(len(full) * cut)], encoding="utf-8")
            try:
                cfg_t = load_config(str(good))
            except Exception:
                errored = True
                continue
            # 能加载但 keywords/签文没了 → 静默丢配置（比抛错更难排查）
            if not cfg_t.features.keyword_reply.enabled or not cfg_t.custom_fortunes:
                silent_loss = True
        ok("截断的配置至少有一种会抛异常（说明写入必须原子）", errored)
        ok("截断也可能「加载成功但静默丢配置」（所以不能只靠 load 报错发现）", silent_loss)

        # ── 4. update_config_from_dict 不丢字段 ──
        print("[4] update_config_from_dict 保留未提到的字段")
        room2 = tmp / "rooms" / "333444"
        (room2 / "data").mkdir(parents=True)
        cfg2 = room2 / "config.yaml"
        write_default_config(cfg2)
        raw = yaml.safe_load(cfg2.read_text(encoding="utf-8"))
        raw["features"] = {"thanks_enabled": True, "thanks_template": "谢谢 {uname}"}
        raw["custom_fortunes"] = ["上上签", "中签"]
        raw["my_unknown_section"] = {"keep": "me"}     # 未来版本 / 手写的字段
        atomic_write_text(cfg2, yaml.safe_dump(raw, allow_unicode=True, sort_keys=False))

        saved = update_config_from_dict(
            {"features": {"thanks_enabled": False}, "room_name": "测试房间"}, str(cfg2))
        ok("保存成功", saved is True)
        after = yaml.safe_load(cfg2.read_text(encoding="utf-8"))
        ok("改到的字段生效", after["features"]["thanks_enabled"] is False)
        ok("同段其它字段保留", after["features"]["thanks_template"] == "谢谢 {uname}",
           after["features"])
        ok("顶层自定义字段保留", after.get("custom_fortunes") == ["上上签", "中签"],
           after.get("custom_fortunes"))
        ok("未知段落保留", after.get("my_unknown_section") == {"keep": "me"},
           after.get("my_unknown_section"))
        ok("room_name 写入", after.get("room_name") == "测试房间")
        cfg_obj = load_config(str(cfg2))
        ok("保存后仍能 load_config", cfg_obj is not None)
        ok("加载后取值正确", cfg_obj.features.thanks_enabled is False)

        # ── 5. 保存后没有临时文件残留，且有 .bak 可回滚 ──
        print("[5] 目录干净 + 备份")
        leftovers = [p.name for p in room2.iterdir() if ".tmp" in p.name]
        ok("没有 .tmp 残留", not leftovers, leftovers)
        bak = room2 / "config.yaml.bak"
        ok("保存时留了 config.yaml.bak", bak.exists())
        if bak.exists():
            bak_raw = yaml.safe_load(bak.read_text(encoding="utf-8"))
            ok(".bak 是保存前的内容（thanks_enabled 还是 true）",
               bak_raw["features"]["thanks_enabled"] is True,
               bak_raw.get("features", {}).get("thanks_enabled"))
            ok(".bak 能正常解析并保留自定义字段",
               bak_raw.get("my_unknown_section") == {"keep": "me"})

        # ── 6. WebUI 的房间配置保存必须走原子写 ──
        print("[6] WebUI 保存路径")
        src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")
        seg = src[src.index("def _save_room_raw_config"):]
        seg = seg[:seg.index("\n@app.")]
        ok("_save_room_raw_config 使用 atomic_write_text",
           "atomic_write_text" in seg, seg.strip().splitlines()[:3])
        ok("_save_room_raw_config 不再直接 write_text",
           "cfg_path.write_text" not in seg)
        cfg_src = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        seg2 = cfg_src[cfg_src.index("def update_config_from_dict"):]
        seg2 = seg2[:seg2.index("\ndef ")]
        ok("update_config_from_dict 使用 atomic_write_text",
           "atomic_write_text(" in seg2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nCONFIG ATOMIC OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
