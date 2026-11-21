"""校验「机器人配置模板」把签文 / 关键词规则 / 冷却限流一起带走（issue #3）。

背景：模板保存时原来是 `config = roomCfg.features || {}`，而 custom_fortunes、
keyword_reply、cooldown、rate_limit 都是 features 的**同级**字段，于是被静默丢弃。
表现就是：从房间 A 存模板 → 套到房间 B，定时消息能过去，但 `#抽签` 还是默认签文、
关键词自动回复全空。

本脚本做三层校验：
  1. 提取模板里内置的 window.AyabotBotTpl 编解码器，用 node 跑真实用例
     （打包 / 解包 / 套用到编辑器 / 生成保存请求体 / 老模板兼容 / 空段落不覆盖）；
  2. 检查三处调用点都走编解码器，旧的 `config = roomCfg.features` 之类写法已不存在；
  3. 把 node 生成的请求体交给真实的 update_config_from_dict + load_config 落盘再读回，
     确认后端能真正吃下这些字段。

用法: ./.venv/Scripts/python.exe scripts/check_bot_template.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MARKER = "机器人配置模板的打包 / 套用"

# 前端编解码器的用例。node 跑完把结果以 JSON 打到 stdout。
NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');

// 用假的 window/document 跑一遍，既验证语法也拿到 window.AyabotBotTpl
const sandbox = { window: {}, document: { write() {} }, Vue: {}, Chart: {}, console };
sandbox.globalThis = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const T = sandbox.window.AyabotBotTpl;
if (!T) { console.log(JSON.stringify({ fatal: 'window.AyabotBotTpl 未定义' })); process.exit(0); }

const roomA = {
  features: {
    welcome_enabled: true,
    welcome_template: '欢迎 {uname}',
    periodic_messages_list: [{interval: 300, template: '来点弹幕'}],
    welcome_templates_for_uids: { '12345': '大佬来了' },
    keyword_reply: undefined,
  },
  keyword_reply: {
    enabled: true,
    cooldown: 45,
    rules: [{ keywords: ['抽签', '求签'], reply: '好运！', match_mode: 'contains', allowed_uids: null }],
  },
  custom_fortunes: {
    daiji: ['今天欧气爆棚', '主播都惊了'],
    zhongji: '单条旧格式',
    xiaoji: ['', '   '],   // 全空 -> 应被剔除
    daxiong: [],           // 空数组 -> 应被剔除
  },
  cooldown: { welcome_user_seconds: 10, thanks_user_seconds: 20 },
  rate_limit: { max_per_minute: 8 },
};

const packed = T.pack(roomA);

// 极老配置：keyword_reply 塞在 features 里
const packedOld = T.pack({
  features: { keyword_reply: { enabled: true, rules: [{ keywords: ['old'], reply: 'r' }] }, welcome_enabled: false },
});

// 老模板（无标记）：整个 config 就是 features
const legacyTpl = { welcome_enabled: true, periodic_messages_list: [{ interval: 60, template: 'x' }] };

// 套用到空房间
const editor = { features: {}, keyword_reply: { enabled: false, cooldown: 30, rules: [] },
                 custom_fortunes: { daiji: '', zhongji: '' }, cooldown: {}, rate_limit: {} };
T.applyToEditor(editor, packed);

// 空段落不覆盖：模板里没有签文/规则时，目标房间原有配置应保住
const keep = { features: {}, keyword_reply: { enabled: false, cooldown: 30, rules: [{ keywords: ['mine'], reply: 'm' }] },
               custom_fortunes: { daiji: ['我自己的签文'] } };
T.applyToEditor(keep, T.pack({ features: { welcome_enabled: true }, custom_fortunes: { daiji: '' } }));

// 老模板套用时只动 features
const legacyEditor = { features: {}, keyword_reply: { enabled: true, cooldown: 30, rules: [{ keywords: ['keep'], reply: 'k' }] },
                       custom_fortunes: { daiji: ['保留'] } };
T.applyToEditor(legacyEditor, legacyTpl);

console.log(JSON.stringify({
  packed: packed,
  packedOld: packedOld,
  legacy: T.unpack(legacyTpl),
  editor: editor,
  keep: keep,
  legacyEditor: legacyEditor,
  body: T.toSaveBody(packed),
  bodyLegacy: T.toSaveBody(legacyTpl),
  summary: T.summary(packed),
  summaryLegacy: T.summary(legacyTpl),
}));
"""


def fail(fails: list[str], msg: str) -> None:
    fails.append(msg)


def extract_codec(src: str) -> str:
    """从 INDEX_HTML 里抠出 window.AyabotBotTpl 的脚本块。"""
    idx = src.find(MARKER)
    if idx < 0:
        raise SystemExit("FAILED: 模板里找不到机器人配置模板编解码器")
    start = src.rfind("<script>", 0, idx)
    end = src.find("</script>", idx)
    if start < 0 or end < 0:
        raise SystemExit("FAILED: 编解码器脚本块不完整")
    return src[start + len("<script>"):end]


def main() -> int:
    fails: list[str] = []
    src = (ROOT / "app" / "web" / "server.py").read_text(encoding="utf-8")

    codec = extract_codec(src)
    print(f"编解码器脚本: {len(codec):,} 字符")

    # ── 1. node 跑编解码器用例 ──
    tmp = Path(tempfile.mkdtemp(prefix="ayabot_tpl_"))
    try:
        js_file = tmp / "codec.js"
        js_file.write_text(codec, encoding="utf-8")
        harness = tmp / "run.js"
        harness.write_text(NODE_HARNESS, encoding="utf-8")
        proc = subprocess.run(["node", str(harness), str(js_file)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
            return 1
        r = json.loads(proc.stdout.strip().splitlines()[-1])
        if r.get("fatal"):
            print("FAILED:", r["fatal"])
            return 1

        packed = r["packed"]
        print("\n[1] 打包（房间 A -> 模板）")
        for key in ("features", "keyword_reply", "custom_fortunes", "cooldown", "rate_limit"):
            ok = key in packed
            print(f"  {'OK ' if ok else 'MISS'} 含 {key}")
            if not ok:
                fail(fails, f"打包结果缺少 {key}")
        ok = packed.get("__ayabot_bot_tpl") == 2
        print(f"  {'OK ' if ok else 'MISS'} 版本标记 __ayabot_bot_tpl == 2")
        if not ok:
            fail(fails, "缺少版本标记")
        ok = "keyword_reply" not in packed.get("features", {})
        print(f"  {'OK ' if ok else 'MISS'} features 里不再夹带 keyword_reply")
        if not ok:
            fail(fails, "features 里仍夹带 keyword_reply（会和顶层重复）")

        cf = packed.get("custom_fortunes") or {}
        ok = cf.get("daiji") == ["今天欧气爆棚", "主播都惊了"] and cf.get("zhongji") == "单条旧格式"
        print(f"  {'OK ' if ok else 'MISS'} 签文原样保留（含旧版单字符串）-> {list(cf)}")
        if not ok:
            fail(fails, f"签文内容不对: {cf}")
        ok = "xiaoji" not in cf and "daxiong" not in cf
        print(f"  {'OK ' if ok else 'MISS'} 空签文被剔除")
        if not ok:
            fail(fails, "空签文没被剔除")

        kr = packed.get("keyword_reply") or {}
        ok = len(kr.get("rules") or []) == 1 and kr["rules"][0]["keywords"] == ["抽签", "求签"]
        print(f"  {'OK ' if ok else 'MISS'} 关键词规则保留 + 冷却 {kr.get('cooldown')}")
        if not ok:
            fail(fails, f"关键词规则丢了: {kr}")
        ok = "keywordsStr" not in kr["rules"][0]
        print(f"  {'OK ' if ok else 'MISS'} 不含界面专用关键词串字段")
        if not ok:
            fail(fails, "keywordsStr 混进模板")

        old = r["packedOld"]
        ok = (old.get("keyword_reply") or {}).get("rules") and "keyword_reply" not in old.get("features", {})
        print(f"  {'OK ' if ok else 'MISS'} 极老配置的 features.keyword_reply 已提升到顶层")
        if not ok:
            fail(fails, "极老配置迁移失败")

        print("\n[2] 老模板兼容")
        lg = r["legacy"]
        ok = lg.get("legacy") is True and lg.get("features", {}).get("welcome_enabled") is True
        print(f"  {'OK ' if ok else 'MISS'} 无标记视为老模板，整个 config 当 features")
        if not ok:
            fail(fails, f"老模板识别失败: {lg}")

        print("\n[3] 套用到编辑器")
        ed = r["editor"]
        ok = ed["custom_fortunes"].get("daiji") == ["今天欧气爆棚", "主播都惊了"]
        print(f"  {'OK ' if ok else 'MISS'} 签文写进编辑器")
        if not ok:
            fail(fails, f"套用后签文为空: {ed.get('custom_fortunes')}")
        ok = len(ed["keyword_reply"]["rules"]) == 1 and ed["keyword_reply"]["rules"][0].get("keywordsStr") == "抽签, 求签"
        print(f"  {'OK ' if ok else 'MISS'} 关键词规则写进编辑器（带界面用的 keywordsStr）")
        if not ok:
            fail(fails, f"套用后关键词规则不对: {ed.get('keyword_reply')}")
        entries = ed["features"].get("welcome_templates_for_uids_entries") or []
        ok = len(entries) == 1 and entries[0]["uid"] == 12345
        print(f"  {'OK ' if ok else 'MISS'} UID 欢迎模板转成 entries")
        if not ok:
            fail(fails, f"UID 欢迎模板未转换: {entries}")
        ok = ed["cooldown"].get("welcome_user_seconds") == 10 and ed["rate_limit"].get("max_per_minute") == 8
        print(f"  {'OK ' if ok else 'MISS'} 冷却 / 限流写进编辑器")
        if not ok:
            fail(fails, "冷却或限流未套用")

        print("\n[4] 空段落不覆盖目标房间")
        keep = r["keep"]
        ok = (keep["custom_fortunes"].get("daiji") == ["我自己的签文"]
              and len(keep["keyword_reply"]["rules"]) == 1)
        print(f"  {'OK ' if ok else 'MISS'} 模板无签文/规则时不清空目标房间")
        if not ok:
            fail(fails, "空模板把目标房间的签文或规则清空了")

        lg_ed = r["legacyEditor"]
        ok = (lg_ed["features"].get("welcome_enabled") is True
              and len(lg_ed["keyword_reply"]["rules"]) == 1
              and lg_ed["custom_fortunes"].get("daiji") == ["保留"])
        print(f"  {'OK ' if ok else 'MISS'} 老模板只动 features，不碰签文和关键词")
        if not ok:
            fail(fails, "老模板套用时误改了签文或关键词")

        print("\n[5] 保存请求体")
        body = r["body"]
        ok = all(k in body for k in ("features", "keyword_reply", "custom_fortunes", "cooldown", "rate_limit"))
        print(f"  {'OK ' if ok else 'MISS'} 请求体含全部 5 段 -> {sorted(body)}")
        if not ok:
            fail(fails, f"请求体字段不全: {sorted(body)}")
        ok = "__ayabot_bot_tpl" not in body
        print(f"  {'OK ' if ok else 'MISS'} 请求体不含内部标记")
        if not ok:
            fail(fails, "内部标记写进了房间配置")
        ok = "__ayabot_bot_tpl" not in r["bodyLegacy"]
        print(f"  {'OK ' if ok else 'MISS'} 老模板请求体仍是纯 features")
        if not ok:
            fail(fails, "老模板请求体格式变了")

        sm = r["summary"]
        ok = sm["features"] > 0 and sm["fortunes"] == 3 and sm["keywords"] == 1 and sm["legacy"] is False
        print(f"  {'OK ' if ok else 'MISS'} 摘要 {sm['features']} 项功能 / 签文 {sm['fortunes']} 条 / 关键词 {sm['keywords']} 条")
        if not ok:
            fail(fails, f"摘要数字不对: {sm}")

        # ── 2. 调用点检查 ──
        print("\n[6] 调用点")
        checks = [
            ("保存模板走 pack", "config = window.AyabotBotTpl.pack(roomCfg)"),
            ("套用模板走 applyToEditor", "window.AyabotBotTpl.applyToEditor(roomConfig.value, tpl.config)"),
            ("新建房间走 toSaveBody", "Object.assign(body, window.AyabotBotTpl.toSaveBody(tpl.config))"),
            ("模板摘要走 summary", "botTplSummaryText(t.config)"),
        ]
        for label, needle in checks:
            ok = needle in src
            print(f"  {'OK ' if ok else 'MISS'} {label}")
            if not ok:
                fail(fails, f"调用点未接入: {label}")
        for label, needle in [("旧的 config = roomCfg.features", "config = roomCfg.features || {}"),
                              ("旧的 body.features = tpl.config", "body.features = tpl.config;")]:
            ok = needle not in src
            print(f"  {'OK ' if ok else 'MISS'} 已移除 {label}")
            if not ok:
                fail(fails, f"旧写法仍在: {label}")

        # ── 3. 后端真的吃得下这个请求体 ──
        print("\n[7] 后端落盘回读")
        from app.config import load_config, update_config_from_dict

        room_cfg = tmp / "config.yaml"
        base = ROOT / "config.example.yaml"
        shutil.copyfile(base, room_cfg)
        before = load_config(str(room_cfg))
        ok = update_config_from_dict(body, str(room_cfg))
        print(f"  {'OK ' if ok else 'MISS'} update_config_from_dict 写入成功")
        if not ok:
            fail(fails, "配置写入失败")
        after = load_config(str(room_cfg))
        got_f = after.custom_fortunes or {}
        ok = got_f.get("daiji") == ["今天欧气爆棚", "主播都惊了"] and got_f.get("zhongji") == "单条旧格式"
        print(f"  {'OK ' if ok else 'MISS'} 重新加载后 custom_fortunes = {got_f}")
        if not ok:
            fail(fails, "落盘后签文丢失")
        rules = after.features.keyword_reply.rules or []
        ok = len(rules) == 1 and rules[0].keywords == ["抽签", "求签"] and rules[0].reply == "好运！"
        print(f"  {'OK ' if ok else 'MISS'} 重新加载后关键词规则 {len(rules)} 条")
        if not ok:
            fail(fails, f"落盘后关键词规则丢失: {rules}")
        ok = after.cooldown.welcome_user_seconds == 10 and before.cooldown.welcome_user_seconds != 10
        print(f"  {'OK ' if ok else 'MISS'} 冷却/限流也写进去了")
        if not ok:
            fail(fails, "冷却/限流未落盘")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("\nFAILED:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nBOT TEMPLATE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
