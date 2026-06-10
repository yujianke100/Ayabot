from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

logger = logging.getLogger("webui")

# ── 加载 config.yaml 获得正确的 DB 路径 ──────────────────────────
_CONFIG_PATH = Path("config.yaml")
if _CONFIG_PATH.exists():
    _raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    _storage_cfg = _raw.get("storage", {})
    _DB_PATH = str(_storage_cfg.get("sqlite_path", "data/bot.db"))
    # 若为相对路径则相对于项目根目录
    if not os.path.isabs(_DB_PATH):
        _DB_PATH = str(_CONFIG_PATH.parent / _DB_PATH)
else:
    _DB_PATH = "data/bot.db"
logger.info("webui using db: %s", os.path.abspath(_DB_PATH))

app = FastAPI(title="BiliRobot Manager")


# ── helpers ──────────────────────────────────────────────────────
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_date(d: str) -> tuple[int, int]:
    """return (start_ts, end_ts_exclusive)"""
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


# ── static HTML ─────────────────────────────────────────────────
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
.avatar-wrap { position: relative; width: 52px; height: 52px; flex-shrink: 0; }
.avatar-wrap img.face {
    width: 44px; height: 44px; border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.9);
    position: absolute; top: 4px; left: 4px; object-fit: cover;
}
.avatar-wrap img.frame {
    position: absolute; width: 52px; height: 52px;
    top: 0; left: 0; pointer-events: none;
}
.gift-icon { width: 40px; height: 40px; flex-shrink: 0; object-fit: contain; }
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

<!-- 送礼排行 -->
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
                    <td class="p-2 text-right">{{ u.total_val.toFixed(2) }}</td>
                    <td class="p-2 text-right" :class="u.total_profit>=0?'text-red-500':'text-green-500'">{{ u.total_profit.toFixed(2) }}</td>
                </tr>
            </tbody>
        </table>
        <div v-else-if="!errRanking" class="text-gray-400 text-center py-8">暂无数据</div>
    </div>
    <div class="bg-white p-4 rounded-xl shadow-sm flex flex-col">
        <canvas id="chartRank" class="flex-1 min-h-0"></canvas>
    </div>
</div>

<!-- 精美导出 -->
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
            <div class="avatar-wrap">
                <img :src="item.avatar || 'https://static.hdslb.com/images/akari.jpg'" class="face" @error="$event.target.src='https://static.hdslb.com/images/akari.jpg'">
                <img v-if="item.guard_level === 3" src="https://i0.hdslb.com/bfs/live/782b2cf6d82a89fad7ea12f635dac6b4b19f53b0.png" class="frame" title="舰长">
                <img v-else-if="item.guard_level === 2" src="https://i0.hdslb.com/bfs/live/882b2cf6d82a89fad7ea12f635dac6b4b19f53b0.png" class="frame" title="提督">
                <img v-else-if="item.guard_level === 1" src="https://i0.hdslb.com/bfs/live/962b2cf6d82a89fad7ea12f635dac6b4b19f53b0.png" class="frame" title="总督">
            </div>
            <div class="flex-1 min-w-0">
                <div class="font-bold text-sm truncate">{{ item.uname }}</div>
                <div class="text-xs text-white/80 mt-0.5">投喂了 {{ item.gift_name }} × {{ item.gift_num }}</div>
            </div>
            <img :src="item.gift_icon || 'https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/img/gift/1.png'" class="gift-icon" @error="$event.target.src='https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/img/gift/1.png'">
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

        async function loadRanking() {
            errRanking.value = '';
            ranking.value = [];
            try {
                const res = await fetch(`/api/ranking?start=${rStart.value}&end=${rEnd.value}`);
                if (!res.ok) {
                    const txt = await res.text();
                    throw new Error(txt.slice(0,80));
                }
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
                        data: ranking.value.map(u=>u.total_val),
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
            } catch(e) { errExport.value = '加载失败: ' + e.message; }
        }
        function gotoExport(uid) { eUid.value = uid; tab.value = 'export'; loadExport(); }
        function downloadImage() {
            const el = document.getElementById('capture');
            if (!el) return;
            html2canvas(el, { scale: 2, useCORS: true, allowTaint: false, backgroundColor: '#ffffff' })
                .then(canvas => {
                    const a = document.createElement('a');
                    a.download = `gift_${eUid.value}_${eDate.value}.png`;
                    a.href = canvas.toDataURL('image/png');
                    a.click();
                })
                .catch(e => { errExport.value = '导出失败: ' + e.message; });
        }
        return {tab, rStart, rEnd, ranking, errRanking, eUid, eDate, exportList, errExport,
                loadRanking, loadExport, gotoExport, downloadImage};
    }
}).mount('#app');
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/api/ranking")
async def api_ranking(start: str, end: str):
    try:
        s_ts, e_ts = _parse_date(start), _parse_date(end)
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
            (s_ts[0], e_ts[0]),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("ranking api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/user_gifts")
async def api_user_gifts(uid: int, date: str):
    try:
        s, e = _parse_date(date)
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM gift_events WHERE uid = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
            (uid, s[0], e[0]),
        ).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            raw = _safe_json(item.pop("raw_json", "{}"))
            item["avatar"] = raw.get("face") or None
            item["guard_level"] = _safe_int(raw.get("guard_level"))
            gift_id = _safe_int(raw.get("gift_id") or raw.get("giftId") or 1)
            item["gift_icon"] = f"https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/img/gift/{gift_id}.png"
            results.append(item)
        return results
    except Exception as exc:
        logger.exception("user_gifts api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
