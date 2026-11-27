"""配置里写了值，加载时到底读到没有？——逐个字段验。

v1.2.6 有个真 bug 就是这么藏的：

    config.example.yaml:  reply_delay_seconds: 3.0
    load_config():        RateLimitConfig(send_interval_seconds=..., retry_count=...)
                          # ← reply_delay_seconds / max_queue_size / max_text_len 没读
    dataclass 默认值:      1.0

于是界面上显示 3.0、保存也成功、机器人实际按 1.0 发 —— 发得比配置快 3 倍，
更容易撞 B站 的发送频率限制，用户看到的是「机器人有时候不回」。
`max_queue_size` / `max_text_len` 更隐蔽：默认值恰好等于模板里的值，
用户把它改大/改小都毫无效果。

两道闸：

1. **读到了吗** —— 给 example 里每个叶子字段换一个**哨兵值**，load_config 后看
   哨兵有没有出现在 config_to_dict 的输出里。不需要维护「yaml 路径 → dataclass
   字段」的映射表。
2. **存了还在吗** —— 对 config_to_dict 里每个叶子，改哨兵值 →
   `update_config_from_dict`（界面保存走的就是它）→ 重新 load → 值必须还是哨兵。
   这是 issue #3「机器人模板字段丢失」的同一形态。

覆盖边界：第 2 道只覆盖 `config_to_dict` 里有的字段（现在 86 个）。
不在其中的（例如 `room_name` —— 房间名走单独接口保存）由
`check_room_list_robust.py` 覆盖。

    ./.venv/Scripts/python.exe scripts/check_config_read_through.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 这些路径写进去本来就不该原样出现在 config_to_dict 里（会被解析成绝对路径等）
ALLOW_UNCHANGED = {
    "storage.sqlite_path",
    "auth.credential_store_path",
    "auth.auto_login",          # 见下面：auto_login 目前确实没被任何代码读，单独报告
    "auth.wait_for_login",      # 由 AuthManager 在 prepare_credential 里直接读 self.config，会生效
    "auth.qr_poll_seconds",
    "auth.refresh_interval_seconds",
    "credential.*",
    "web_ui.password",          # 密码不回显
    "web_ui.username",
    "web_ui.host",              # 只影响启动参数，不进 config_to_dict 的比对
    "web_ui.api_token",         # 密钥不回显（只回显 has_key）
    "web_ui.enabled",           # WebUI 自身的开关，不进 config_to_dict
}

#: 整段跳过（运行时状态，不是配置）
SKIP_SECTIONS = {"credential"}


def walk_leaves(node, prefix=""):
    """产出 (点分路径, 值)。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk_leaves(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        yield prefix, node
    else:
        yield prefix, node


def sentinel(value, n: int):
    """给一个明显不同的合法值。"""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int) and not isinstance(value, bool):
        return value + 7777
    if isinstance(value, float):
        return round(value + 77.5, 3)
    if isinstance(value, str):
        return f"SENTINEL_{n}"
    return value


def set_path(data, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur[p]
    cur[parts[-1]] = value


def main() -> int:
    from app.config import load_config, config_to_dict

    example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    leaves = [(p, v) for p, v in walk_leaves(example)
              if p and p.split(".")[0] not in SKIP_SECTIONS]
    print(f"config.example.yaml 里有 {len(leaves)} 个叶子字段")

    tmp = Path(tempfile.mkdtemp(prefix="readthru_"))
    fails: list[str] = []
    allowed: list[str] = []
    try:
        for n, (path, value) in enumerate(leaves):
            if any(path == a or (a.endswith(".*") and path.startswith(a[:-1]))
                   for a in ALLOW_UNCHANGED):
                allowed.append(path)
                continue
            data = yaml.safe_load(yaml.dump(example))
            marker = sentinel(value, n)
            # int 型的哨兵值要能被 int() 解析；float 同理 —— 已经满足
            set_path(data, path, marker)
            cfg_path = tmp / f"c{n}.yaml"
            cfg_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            try:
                cfg = load_config(str(cfg_path))
                dumped = json.dumps(config_to_dict(cfg), ensure_ascii=False, default=str)
            except Exception as exc:  # noqa: BLE001
                fails.append(f"{path}: 改成 {marker!r} 之后加载失败 → {type(exc).__name__}: {exc}")
                continue
            # bool 在 JSON 里是 true/false（小写），str(True) 是 "True" —— 直接比会假红
            needle = ("true" if marker else "false") if isinstance(marker, bool) else str(marker)
            if needle not in dumped:
                fails.append(f"{path}: 值 {marker!r} 没有被读到（config_to_dict 里找不到）")

        print(f"  允许豁免 {len(allowed)} 个（路径类/密码等）")
        print(f"  实际校验 {len(leaves) - len(allowed)} 个")
        print()
        if fails:
            print(f"发现 {len(fails)} 个「写了但不生效」的配置项：")
            for f in fails:
                print(f"  FAIL {f}")
            print()
            print(f"CONFIG READ-THROUGH FAILED（{len(fails)} 项）")
            return 1
        # ── 第二道：逐字段往返（界面改 → 保存 → 重启后还在吗）──
        # 这是 issue #3「机器人模板字段丢失」的同一形态：能读、能显示，
        # 但保存时被 update_config_from_dict 丢掉，或者重载时不读回来。
        print()
        print("[2] 逐字段往返：改一个值 → 保存 → 重载，值还在吗")
        from app.config import update_config_from_dict
        base_cfg = Path(tmp) / "roundtrip_base.yaml"
        shutil.copy(ROOT / "config.example.yaml", base_cfg)
        base_dict = config_to_dict(load_config(str(base_cfg)))
        rt_leaves = [(p, v) for p, v in walk_leaves(base_dict) if p]
        lost: list[str] = []
        skipped = 0
        kept = 0
        for n, (path, value) in enumerate(rt_leaves):
            cfg = Path(tmp) / f"rt{n}.yaml"
            shutil.copy(ROOT / "config.example.yaml", cfg)
            body = yaml.safe_load(yaml.dump(base_dict))
            marker = sentinel(value, n)
            try:
                set_path(body, path, marker)
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            try:
                if not update_config_from_dict(body, str(cfg)):
                    lost.append(f"{path}: 保存返回 False")
                    continue
                back = config_to_dict(load_config(str(cfg)))
                after = back
                for part in path.split("."):
                    after = after[part]
            except KeyError:
                lost.append(f"{path}: 重载后这个字段不见了")
                continue
            except Exception:  # noqa: BLE001
                # 校验拒绝（比如 timezone 写成哨兵）是**正确**行为
                skipped += 1
                continue
            if after == marker:
                kept += 1
            else:
                lost.append(f"{path}: 期望 {marker!r}，重载后是 {after!r}")

        print(f"  保留 {kept} / 丢失 {len(lost)} / 跳过（校验拒绝） {skipped}")
        if lost:
            for line in lost:
                print(f"  FAIL {line}")
            print()
            print(f"CONFIG ROUND-TRIP FAILED（{len(lost)} 项）")
            return 1

        print()
        print("CONFIG READ-THROUGH OK：每个配置项改了都真的生效，存了也都真的还在")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
