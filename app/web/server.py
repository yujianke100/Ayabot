"""Web UI for BiliRobot — 送礼统计 & 精美导出

礼物图标来源：bilibili-api-python 官方 live.get_gift_config() API
大航海头像框：舰长使用 B站 真实图片，提督/总督使用 CSS 金色/紫色光环
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

from bilibili_api import live

logger = logging.getLogger("webui")

# ══════════════════════════════════════════════════════════════════
#  Config — DB path from config.yaml
# ══════════════════════════════════════════════════════════════════

_CONFIG_PATH = Path("config.yaml")
if _CONFIG_PATH.exists():
    _raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    _storage_cfg = _raw.get("storage", {})
    _DB_PATH = str(_storage_cfg.get("sqlite_path", "data/bot.db"))
    if not os.path.isabs(_DB_PATH):
        _DB_PATH = str(_CONFIG_PATH.parent / _DB_PATH)
else:
    _DB_PATH = "data/bot.db"
logger.info("webui using db: %s", os.path.abspath(_DB_PATH))

app = FastAPI(title="BiliRobot Manager")

# ── 礼物图标缓存：gift_id → img_basic URL ────────────────────────
_GIFT_ICON_CACHE: dict[int, str] = {}
_GIFT_CACHE_BUILT = False

# 常见礼物回退表 — 即使 get_gift_config 失败，这些也保证有图标
_FALLBACK_ICONS: dict[int, str] = {
    1: "https://s1.hdslb.com/bfs/live/d57afb7c5596359970eb430655c6aef501a268ab.png",       # 辣条
    30607: "https://s1.hdslb.com/bfs/live/d4a0827cbb6b00e48f4d3e6c0f4fdd6e24c93a8f.png",   # 小心心
    31164: "https://s1.hdslb.com/bfs/live/e051dfd4557678f8edcac4993ed00a0935cbd9cc.png",    # 粉丝团灯牌
    30606: "https://s1.hdslb.com/bfs/live/03f77460f808e33bf3564b1092e261a00b37b4e2.png",   # 泡泡机
    33972: "https://s1.hdslb.com/bfs/live/a97726f370a5aa6d5e6100b042bee848efc560f6.png",   # 舰长一号
    33908: "https://s1.hdslb.com/bfs/live/af5b620387a20a8b65b9bd6fc47cf9058a8bbd85.png",   # 提督一号
    33909: "https://s1.hdslb.com/bfs/live/52e00ca134a8a41f08b203eb5886875507e4b44e.png",   # 总督一号
    30688: "https://s1.hdslb.com/bfs/live/3816eb1d809c7020a5ef6b4deb10ee9a470acdac.png",   # 冲浪
    30047: "https://s1.hdslb.com/bfs/live/b33c94c51b669bd88f811ecf5f4e34a1db22a648.png",   # 友谊的小船
    30608: "https://s1.hdslb.com/bfs/live/a7ef8654bdfc1ed7f55e890c3b1abf5d620607c9.png",   # 奶粉钱
    30869: "https://s1.hdslb.com/bfs/live/b304a1ae04d10c25db87cfd8ec2a83bce1749322.png",   # 心动卡
    30675: "https://s1.hdslb.com/bfs/live/8ba04b53487581cda0c25440ca5d3b300c2e5ee2.png",   # 下饭
    30607: "https://s1.hdslb.com/bfs/live/d4a0827cbb6b00e48f4d3e6c0f4fdd6e24c93a8f.png",   # 小心心
    31036: "https://s1.hdslb.com/bfs/live/5126973892625f3a43a8290be6b625b5e54261a5.png",   # 小花花
    33987: "https://s1.hdslb.com/bfs/live/7164c955ec0ed7537491d189b821cc68f1bea20d.png",   # 人气票
}


async def _build_gift_cache() -> None:
    """从 bilibili-api 官方 get_gift_config() 获取全量礼物图标."""
    global _GIFT_ICON_CACHE, _GIFT_CACHE_BUILT
    if _GIFT_CACHE_BUILT:
        return
    try:
        data = await live.get_gift_config()
    except Exception as exc:
        logger.warning("get_gift_config failed: %s — using fallback icons", exc)
        _GIFT_ICON_CACHE = dict(_FALLBACK_ICONS)
        _GIFT_CACHE_BUILT = True
        return

    cache: dict[int, str] = {}
    for g in data.get("list", []):
        gid = _safe_int(g.get("id"))
        if gid <= 0:
            continue
        icon = g.get("img_basic") or ""
        if icon:
            cache[gid] = icon

    # Merge with fallback to fill any gaps, fallback takes lower priority
    merged = dict(_FALLBACK_ICONS)
    merged.update(cache)
    _GIFT_ICON_CACHE = merged
    _GIFT_CACHE_BUILT = True
    logger.info("gift icon cache built: %d entries (fallback: %d)", len(cache), len(_FALLBACK_ICONS))


def _get_gift_icon(gift_id: int) -> str:
    return _GIFT_ICON_CACHE.get(gift_id, "")


async def _ensure_gift_cache() -> None:
    """确保缓存已加载（懒初始化，给 startup 事件未触发的兜底）."""
    if not _GIFT_CACHE_BUILT:
        await _build_gift_cache()


# ══════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_date(d: str) -> tuple[int, int]:
    dt = datetime.datetime.strptime(d, "%Y-%m-%d")
    start = int(dt.timestamp())
    end = int((dt + datetime.timedelta(days=1)).timestamp())
    return start, end


def _safe_json(val: str) -> dict[str, Any]:
    if not val:
        return {}
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return {}


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


# ══════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def _startup():
    try:
        await _build_gift_cache()
    except Exception:
        logger.exception("gift cache init failed")


@app.get("/api/ranking")
async def api_ranking(start: str, end: str):
    try:
        s_start, _ = _parse_date(start)
        e_start, _ = _parse_date(end)
        conn = _get_db()
        rows = conn.execute(
            """
            SELECT uid, uname,
                   CAST(SUM(actual_value) AS REAL) AS total_val,
                   CAST(SUM(profit_value)  AS REAL) AS total_profit
            FROM gift_events
            WHERE ts >= ? AND ts < ?
            GROUP BY uid
            ORDER BY total_val DESC
            LIMIT 20
            """,
            (s_start, e_start),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("ranking api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/user_gifts")
async def api_user_gifts(uid: int, date: str):
    try:
        # 确保礼物图标缓存已加载
        await _ensure_gift_cache()

        day_start, day_end = _parse_date(date)
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM gift_events WHERE uid = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
            (uid, day_start, day_end),
        ).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            raw = _safe_json(item.pop("raw_json", "{}"))

            # 头像 — 从 B站 原始 payload 的 face 字段提取
            avatar = (
                raw.get("face")
                or raw.get("data", {}).get("face")
                or ""
            )
            item["avatar"] = avatar

            # 大航海等级 (3=舰长, 2=提督, 1=总督)
            # 注意：普通送礼事件的 raw 里可能没有 guard_level，有则用
            item["guard_level"] = _safe_int(
                raw.get("guard_level")
                or raw.get("data", {}).get("guard_level", 0)
            )

            # 礼物图标
            gift_id = _safe_int(raw.get("giftId") or raw.get("gift_id") or 0)
            icon = _get_gift_icon(gift_id) if gift_id else ""
            item["gift_icon"] = icon

            # 礼物名称
            item["gift_name"] = (
                raw.get("giftName")
                or raw.get("gift_name")
                or item.get("gift_name", "")
            )

            # 单礼物价格（金瓜子）
            item["price"] = _safe_int(
                raw.get("price") or raw.get("total_coin") or 0
            )

            results.append(item)
        return results
    except Exception as exc:
        logger.exception("user_gifts api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/proxy_image")
async def api_proxy_image(url: str):
    """Fetch external image and return with CORS headers for html2canvas."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                body = await resp.read()
                ct = resp.content_type or "image/png"
                return Response(
                    content=body,
                    media_type=ct,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=86400",
                    },
                )
    except Exception as exc:
        logger.warning("proxy_image failed for %s: %s", url, exc)
        return Response(status_code=302, headers={"Location": url})


# ══════════════════════════════════════════════════════════════════
#  Static HTML
# ══════════════════════════════════════════════════════════════════

INDEX_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BiliRobot 管理后台</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
/* ── 仿 B 站礼物卡片 ── */
.bili-card {
    border-radius: 12px;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    gap: 14px;
    color: #fff;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    box-shadow: 0 4px 14px rgba(0,0,0,0.15);
    margin-bottom: 10px;
    position: relative;
    overflow: hidden;
}
.bili-card > * { position: relative; z-index: 1; }

/* 渐变底色 */
.bg-v1  { background: linear-gradient(135deg, #ff9a9e 0%, #fecfef 100%); }
.bg-v2  { background: linear-gradient(135deg, #a1c4fd 0%, #c2e9fb 100%); }
.bg-v3  { background: linear-gradient(135deg, #f6d365 0%, #fda085 100%); }
.bg-v4  { background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%); }
.bg-v5  { background: linear-gradient(135deg, #d4fc79 0%, #96e6a1 100%); }
.bg-v6  { background: linear-gradient(135deg, #84fab0 0%, #8fd3f4 100%); }
.bg-v7  { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
.bg-v8  { background: linear-gradient(135deg, #e0c3fc 0%, #8ec5fc 100%); }
.bg-v9  { background: linear-gradient(135deg, #fccb90 0%, #d57eeb 100%); }
.bg-v10 { background: linear-gradient(135deg, #e2b0ff 0%, #ffb6c1 100%); }

/* ── 头像 ── */
.avatar-wrap {
    position: relative;
    width: 56px; height: 56px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
}
.avatar-wrap img.face {
    width: 44px; height: 44px;
    border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.9);
    object-fit: cover;
    position: relative;
    z-index: 1;
}
/* 大航海光环 — CSS Ring */
.avatar-wrap .ring {
    position: absolute;
    inset: -3px;
    border-radius: 50%;
    border: 3px solid transparent;
    pointer-events: none;
    z-index: 2;
}
/* 舰长 — 蓝色+用户真实图片 */
.avatar-wrap.guard-captain .ring {
    border-color: #3b82f6;
    box-shadow: 0 0 10px rgba(59,130,246,0.5), inset 0 0 6px rgba(59,130,246,0.3);
}
.avatar-wrap.guard-captain .frame-img {
    position: absolute;
    width: 66px; height: 66px;
    top: -5px; left: -5px;
    pointer-events: none;
    z-index: 3;
    max-width: none !important;
}
/* 提督 — 紫色 */
.avatar-wrap.guard-commander .ring {
    border-color: #a78bfa;
    box-shadow: 0 0 10px rgba(167,139,250,0.5), inset 0 0 6px rgba(167,139,250,0.3);
}
/* 总督 — 金色 */
.avatar-wrap.guard-governor .ring {
    border-color: #fbbf24;
    box-shadow: 0 0 10px rgba(251,191,36,0.5), inset 0 0 6px rgba(251,191,36,0.3);
}

.gift-icon {
    width: 40px; height: 40px;
    flex-shrink: 0;
    object-fit: contain;
    border-radius: 6px;
}
.gift-value {
    background: rgba(255,255,255,0.25);
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
}
.guard-badge {
    font-size: 9px;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 8px;
    vertical-align: middle;
    display: inline-block;
}
.guard-badge.gb-3 { background: #3b82f6; color: #fff; }
.guard-badge.gb-2 { background: #7c3aed; color: #fff; }
.guard-badge.gb-1 { background: #f59e0b; color: #fff; }
</style>
</head>
<body class="bg-gray-100 p-4">
<div id="app" class="max-w-6xl mx-auto">

<header class="mb-6 flex justify-between items-center bg-white p-4 rounded-xl shadow-sm">
    <h1 class="text-xl font-bold text-blue-600">🎯 BiliRobot 管理后台</h1>
    <div class="space-x-3 text-sm">
        <button @click="tab='ranking'" :class="tab==='ranking'?'text-blue-600 font-bold border-b-2 border-blue-600':''">送礼排行</button>
        <button @click="tab='export'"  :class="tab==='export' ?'text-blue-600 font-bold border-b-2 border-blue-600':''">精美导出</button>
    </div>
</header>

<!-- ══════ 送礼排行 ══════ -->
<div v-if="tab==='ranking'" class="grid grid-cols-1 lg:grid-cols-2 gap-6">
    <div class="bg-white p-4 rounded-xl shadow-sm">
        <div class="flex flex-wrap gap-2 mb-4">
            <input type="date" v-model="rStart" class="border p-2 rounded text-sm flex-1 min-w-0">
            <input type="date" v-model="rEnd"   class="border p-2 rounded text-sm flex-1 min-w-0">
            <button @click="loadRanking" class="bg-blue-500 hover:bg-blue-600 text-white px-5 py-2 rounded text-sm">查询</button>
        </div>
        <div v-if="errRanking" class="text-red-500 text-sm mb-2">{{ errRanking }}</div>
        <table class="w-full text-sm" v-if="ranking.length">
            <thead><tr class="bg-gray-50"><th class="p-2 text-left">#</th><th class="p-2 text-left">用户</th><th class="p-2 text-right">送礼价值</th><th class="p-2 text-right">利润</th></tr></thead>
            <tbody>
                <tr v-for="(u,i) in ranking" :key="u.uid"
                    class="border-t hover:bg-blue-50 cursor-pointer transition"
                    @click="gotoExport(u.uid)">
                    <td class="p-2">{{ i+1 }}</td>
                    <td class="p-2">{{ u.uname }}</td>
                    <td class="p-2 text-right">{{ Number(u.total_val).toFixed(2) }}</td>
                    <td class="p-2 text-right" :class="Number(u.total_profit)>=0?'text-red-500':'text-green-500'">{{ Number(u.total_profit).toFixed(2) }}</td>
                </tr>
            </tbody>
        </table>
        <div v-else-if="!errRanking" class="text-gray-400 text-center py-8">暂无数据</div>
    </div>
    <div class="bg-white p-4 rounded-xl shadow-sm flex flex-col min-h-[300px]">
        <canvas id="chartRank" class="flex-1 min-h-0"></canvas>
    </div>
</div>

<!-- ══════ 精美导出 ══════ -->
<div v-if="tab==='export'" class="flex flex-col items-center">
    <div class="bg-white p-4 rounded-xl shadow-sm w-full max-w-lg mb-4 flex flex-wrap gap-2 items-end">
        <label class="text-xs text-gray-500 flex-1">用户UID<input type="number" v-model.number="eUid" class="border p-2 rounded w-full text-sm mt-1"></label>
        <label class="text-xs text-gray-500 flex-1">日期<input type="date" v-model="eDate" class="border p-2 rounded w-full text-sm mt-1"></label>
        <button @click="loadExport" class="bg-green-500 hover:bg-green-600 text-white px-5 py-2 rounded text-sm h-[38px]">生成</button>
        <button @click="downloadImage" v-if="exportList.length" class="bg-purple-500 hover:bg-purple-600 text-white px-5 py-2 rounded text-sm h-[38px]">📥 导出 PNG</button>
    </div>
    <div v-if="errExport" class="text-red-500 text-sm mb-2">{{ errExport }}</div>

    <!-- 精美截图区 -->
    <div id="capture" v-if="exportList.length" class="w-full max-w-md">
        <div class="text-center text-gray-400 text-xs mb-2">{{ eDate }} · 礼物投喂明细</div>
        <div v-for="(item,idx) in exportList" :key="item.id"
             class="bili-card"
             :class="'bg-v' + ((idx % 10) + 1)">
            <div class="avatar-wrap"
                 :class="item.guard_level === 3 ? 'guard-captain' : item.guard_level === 2 ? 'guard-commander' : item.guard_level === 1 ? 'guard-governor' : ''">
                <!-- 舰长头像框 (用户提供的B站真实图片) -->
                <img v-if="item.guard_level === 3"
                     :src="proxyImg('https://i0.hdslb.com/bfs/live/80f732943cc3367029df65e267960d56736a82ee.png')"
                     class="frame-img"
                     @error="$event.target.style.display='none'">
                <!-- CSS 光环 (所有等级都有) -->
                <div class="ring"></div>
                <!-- 用户头像 -->
                <img :src="proxyImg(item.avatar)" class="face"
                     @error="$event.target.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 44 44%22><rect width=%2244%22 height=%2244%22 fill=%22%23ccc%22 rx=%2222%22/></svg>'">
            </div>
            <div class="flex-1 min-w-0">
                <div class="font-bold text-sm truncate">
                    {{ item.uname }}
                    <span v-if="item.guard_level === 3" class="guard-badge gb-3">舰长</span>
                    <span v-else-if="item.guard_level === 2" class="guard-badge gb-2">提督</span>
                    <span v-else-if="item.guard_level === 1" class="guard-badge gb-1">总督</span>
                </div>
                <div class="flex items-center gap-2 mt-0.5">
                    <span class="text-xs text-white/80">投喂了 {{ item.gift_name }} × {{ item.gift_num }}</span>
                    <span class="gift-value" v-if="item.price">¥{{ (item.price / 1000).toFixed(1) }}</span>
                </div>
            </div>
            <!-- 礼物图标 -->
            <img v-if="item.gift_icon"
                 :src="proxyImg(item.gift_icon)" class="gift-icon"
                 @error="onIconError($event)">
        </div>
    </div>
    <div v-else-if="!errExport" class="text-gray-400 mt-20">输入 UID 和日期后点击"生成"</div>
</div>

</div>

<script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
<script>
const {createApp, ref, nextTick} = Vue;
createApp({
    setup() {
        const tab = ref('ranking');
        const rStart = ref(new Date().toISOString().slice(0,10));
        const rEnd   = ref(new Date().toISOString().slice(0,10));
        const ranking = ref([]);
        const errRanking = ref('');
        const eUid = ref(0);
        const eDate = ref(new Date().toISOString().slice(0,10));
        const exportList = ref([]);
        const errExport = ref('');
        let chartInst = null;

        function proxyImg(url) {
            if (!url) return '';
            if (url.startsWith('data:') || url.startsWith('blob:')) return url;
            if (url.startsWith('/api/')) return url;
            return '/api/proxy_image?url=' + encodeURIComponent(url);
        }

        function onIconError(e) {
            const el = e.target;
            el.style.display = 'none';
            const fb = document.createElement('span');
            fb.textContent = '🎁';
            fb.style.cssText = 'font-size:24px;flex-shrink:0;width:40px;text-align:center;';
            el.parentNode.insertBefore(fb, el.nextSibling);
        }

        async function loadRanking() {
            errRanking.value = '';
            ranking.value = [];
            try {
                const res = await fetch(`/api/ranking?start=${rStart.value}&end=${rEnd.value}`);
                if (!res.ok) { const txt = await res.text(); throw new Error(txt.slice(0,80)); }
                ranking.value = await res.json();
            } catch(e) { errRanking.value = '加载失败: ' + e.message; }
            await nextTick();
            updateChart();
        }
        function updateChart() {
            const canvas = document.getElementById('chartRank');
            if (!canvas) return;
            if (chartInst) chartInst.destroy();
            if (!ranking.value.length) return;
            chartInst = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: ranking.value.map(u=>u.uname),
                    datasets: [{
                        label: '送礼价值',
                        data: ranking.value.map(u=>Number(u.total_val)),
                        backgroundColor: 'rgba(59,130,246,0.6)',
                        borderRadius: 4
                    }]
                },
                options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }
            });
        }
        async function loadExport() {
            errExport.value = '';
            exportList.value = [];
            if (!eUid.value || !eDate.value) { errExport.value = '请填写 UID 和日期'; return; }
            try {
                const res = await fetch(`/api/user_gifts?uid=${eUid.value}&date=${eDate.value}`);
                if (!res.ok) { const txt = await res.text(); throw new Error(txt.slice(0,80)); }
                exportList.value = await res.json();
                if (!exportList.value.length) errExport.value = '该用户当天无送礼记录';
                await nextTick();
                await new Promise(r => setTimeout(r, 1000));
            } catch(e) { errExport.value = '加载失败: ' + e.message; }
        }
        function gotoExport(uid) { eUid.value = uid; tab.value = 'export'; loadExport(); }
        function downloadImage() {
            const el = document.getElementById('capture');
            if (!el) return;
            errExport.value = '正在生成图片...';
            html2canvas(el, {
                scale: 2, useCORS: true, allowTaint: true,
                backgroundColor: '#ffffff', logging: false
            }).then(canvas => {
                const a = document.createElement('a');
                a.download = `gift_${eUid.value}_${eDate.value}.png`;
                a.href = canvas.toDataURL('image/png');
                a.click();
                errExport.value = '';
            }).catch(e => { errExport.value = '导出失败: ' + e.message; });
        }
        return {tab, rStart, rEnd, ranking, errRanking, eUid, eDate, exportList, errExport,
                loadRanking, loadExport, gotoExport, downloadImage, proxyImg, onIconError};
    }
}).mount('#app');
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
