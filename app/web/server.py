"""
BiliRobot WebUI — 送礼统计 & 精美导出

Features:
- 登录认证 (wenwen / 31415926)
- 送礼排行 (礼物/盲盒/全部)
- 精美导出：多列布局、时间显示、类型筛选
- 手动清理旧数据
- 礼物图标来自 bilibili-api 官方
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml
from fastapi import FastAPI, Request, Response as FastResponse
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

from bilibili_api import live

logger = logging.getLogger("webui")

# ══════════════════════════════════════════════════════════════════
#  Config
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

# ══════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════

AUTH_USER = "wenwen"
AUTH_PASS = "31415926"
_SESSIONS: dict[str, float] = {}  # token -> expiry (unix ts)
_SESSION_TIMEOUT = 3600  # 1 hour
_RATE_LIMIT: dict[str, list[float]] = {}  # ip -> [timestamps]


def _check_auth(request: Request) -> bool:
    token = request.cookies.get("session")
    if token and token in _SESSIONS:
        if _SESSIONS[token] > time.time():
            return True
        del _SESSIONS[token]
    return False


def _rate_limited(ip: str) -> bool:
    now = time.time()
    if ip not in _RATE_LIMIT:
        _RATE_LIMIT[ip] = []
    # Clean old entries (>60s)
    _RATE_LIMIT[ip] = [t for t in _RATE_LIMIT[ip] if now - t < 60]
    if len(_RATE_LIMIT[ip]) >= 60:  # max 60 req/min per IP
        return True
    _RATE_LIMIT[ip].append(now)
    return False


# ══════════════════════════════════════════════════════════════════
#  Gift icon cache
# ══════════════════════════════════════════════════════════════════

_GIFT_ICON_CACHE: dict[int, str] = {}
_GIFT_NAME_CACHE: dict[str, str] = {}
_GIFT_CACHE_BUILT = False


async def _build_gift_cache() -> None:
    global _GIFT_ICON_CACHE, _GIFT_NAME_CACHE, _GIFT_CACHE_BUILT
    if _GIFT_CACHE_BUILT:
        return
    try:
        data = await live.get_gift_config()
    except Exception as exc:
        logger.warning("get_gift_config failed: %s", exc)
        _GIFT_CACHE_BUILT = True
        return

    cache_id: dict[int, str] = {}
    cache_name: dict[str, str] = {}
    for g in data.get("list", []):
        gid = _safe_int(g.get("id"))
        name = g.get("name", "")
        icon = g.get("img_basic") or ""
        if not icon:
            continue
        if gid > 0:
            cache_id[gid] = icon
        if name:
            cache_name[name] = icon
    _GIFT_ICON_CACHE = cache_id
    _GIFT_NAME_CACHE = cache_name
    _GIFT_CACHE_BUILT = True
    logger.info("gift cache built: %d by id, %d by name", len(cache_id), len(cache_name))


async def _ensure_gift_cache() -> None:
    if not _GIFT_CACHE_BUILT:
        await _build_gift_cache()


# ══════════════════════════════════════════════════════════════════
#  DB
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


def _safe_int(val: Any) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


# ══════════════════════════════════════════════════════════════════
#  Startup
# ══════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def _startup():
    try:
        await _build_gift_cache()
    except Exception:
        logger.exception("gift cache init failed")


# ══════════════════════════════════════════════════════════════════
#  Auth middleware
# ══════════════════════════════════════════════════════════════════

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    # Allow login page and auth endpoint without session
    path = request.url.path
    if path in ("/", "/api/login", "/favicon.ico"):
        return await call_next(request)

    # API paths need auth
    if not _check_auth(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return FastResponse(
            content='<script>window.location.href="/"</script>',
            status_code=302,
        )

    return await call_next(request)


# ══════════════════════════════════════════════════════════════════
#  Auth API
# ══════════════════════════════════════════════════════════════════

@app.post("/api/login")
async def api_login(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    username = body.get("username", "")
    password = body.get("password", "")
    if username != AUTH_USER or password != AUTH_PASS:
        return JSONResponse({"error": "wrong credentials"}, status_code=403)

    token = secrets.token_hex(32)
    _SESSIONS[token] = time.time() + _SESSION_TIMEOUT

    resp = JSONResponse({"ok": True, "token": token})
    resp.set_cookie(
        key="session", value=token,
        max_age=_SESSION_TIMEOUT,
        httponly=True, samesite="lax",
    )
    return resp


# ══════════════════════════════════════════════════════════════════
#  Data API
# ══════════════════════════════════════════════════════════════════

@app.get("/api/ranking")
async def api_ranking(start: str, end: str, gift_type: str = "all"):
    """
    gift_type: all / gift / blindbox
    """
    try:
        s_start, _ = _parse_date(start)
        e_start, _ = _parse_date(end)

        where_clause = "ts >= ? AND ts < ?"
        params: list[Any] = [s_start, e_start]

        if gift_type == "blindbox":
            where_clause += " AND is_blind_box = 1"
        elif gift_type == "gift":
            where_clause += " AND is_blind_box = 0"

        conn = _get_db()
        rows = conn.execute(
            f"""
            SELECT uid, uname,
                   CAST(SUM(actual_value) AS REAL) AS total_val,
                   CAST(SUM(profit_value)  AS REAL) AS total_profit,
                   CAST(SUM(CASE WHEN is_blind_box=1 THEN gift_num ELSE 0 END) AS INTEGER) AS blindbox_count,
                   CAST(SUM(CASE WHEN is_blind_box=0 THEN gift_num ELSE 0 END) AS INTEGER) AS gift_count
            FROM gift_events
            WHERE {where_clause}
            GROUP BY uid
            ORDER BY total_val DESC
            LIMIT 20
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.exception("ranking api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/user_gifts")
async def api_user_gifts(uid: int, date: str, gift_type: str = "all"):
    try:
        await _ensure_gift_cache()
        day_start, day_end = _parse_date(date)

        where_clause = "uid = ? AND ts >= ? AND ts < ?"
        params: list[Any] = [uid, day_start, day_end]

        if gift_type == "blindbox":
            where_clause += " AND is_blind_box = 1"
        elif gift_type == "gift":
            where_clause += " AND is_blind_box = 0"

        conn = _get_db()
        rows = conn.execute(
            f"SELECT * FROM gift_events WHERE {where_clause} ORDER BY ts ASC",
            params,
        ).fetchall()

        results = []
        for r in rows:
            item = dict(r)
            raw = json.loads(item.pop("raw_json", "{}")) if isinstance(item.get("raw_json"), str) else {}

            item["avatar"] = raw.get("face") or raw.get("data", {}).get("face") or ""
            item["guard_level"] = _safe_int(
                raw.get("guard_level") or raw.get("data", {}).get("guard_level", 0)
            )

            gift_id = _safe_int(raw.get("giftId") or raw.get("gift_id") or 0)
            gift_name = raw.get("giftName") or raw.get("gift_name") or item.get("gift_name", "")
            item["gift_name"] = gift_name

            # 图标：按 id 查，查不到按名字查
            icon = ""
            if gift_id and gift_id in _GIFT_ICON_CACHE:
                icon = _GIFT_ICON_CACHE[gift_id]
            elif gift_name and gift_name in _GIFT_NAME_CACHE:
                icon = _GIFT_NAME_CACHE[gift_name]
            item["gift_icon"] = icon

            item["price"] = _safe_int(raw.get("price") or raw.get("total_coin") or 0)

            results.append(item)
        return results
    except Exception as exc:
        logger.exception("user_gifts api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/delete_old")
async def api_delete_old(request: Request):
    """删除指定日期之前的数据."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    date_str = body.get("date", "")
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        before_ts = int(dt.timestamp())
    except ValueError:
        return JSONResponse({"error": "bad date format, use YYYY-MM-DD"}, status_code=400)

    try:
        conn = _get_db()
        deleted = conn.execute("DELETE FROM gift_events WHERE ts < ?", (before_ts,)).rowcount
        conn.execute("DELETE FROM monthly_blindbox_stats WHERE month < ?", (dt.strftime("%Y-%m"),))
        conn.execute("DELETE FROM monthly_gift_stats WHERE month < ?", (dt.strftime("%Y-%m"),))
        conn.commit()
        return {"deleted_events": deleted}
    except Exception as exc:
        logger.exception("delete failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ══════════════════════════════════════════════════════════════════
#  Image proxy
# ══════════════════════════════════════════════════════════════════

@app.get("/api/proxy_image")
async def api_proxy_image(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
#  HTML
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
/* ── 礼物卡片（毛玻璃 + 主题色渐变，html2canvas 兼容）── */
.bili-card {
    border-radius: 12px;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 12px;
    color: #fff;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    box-shadow: 0 3px 10px rgba(0,0,0,0.15);
    margin-bottom: 8px;
    position: relative;
    min-height: 56px;
    border: 1px solid rgba(255,255,255,0.08);
}
/* 毛玻璃：顶部高光 */
.bili-card::after {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 48%;
    border-radius: 12px 12px 0 0;
    background: linear-gradient(180deg, rgba(255,255,255,0.10) 0%, transparent 100%);
    pointer-events: none;
}
.bili-card > * { position: relative; z-index: 1; }

/* ── 身份底色（半透明主题色 + 毛玻璃质感）── */
.bg-default { background: linear-gradient(135deg, #374151 0%, #1f2937 100%); }
.bg-captain { background: linear-gradient(135deg, #1e3a5f 0%, #1a2d4a 100%); }
.bg-commander { background: linear-gradient(135deg, #3b1f6e 0%, #2d1550 100%); }
.bg-governor { background: linear-gradient(135deg, #5c3a0e 0%, #3d2608 100%); }

.avatar-wrap {
    position: relative;
    width: 52px;
    height: 52px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
}
.avatar-wrap img.face {
    width: 44px; height: 44px;
    border-radius: 50%;
    border: 2px solid rgba(255,255,255,0.85);
    object-fit: cover;
    display: block;
    max-width: none !important;
    position: relative;
    z-index: 1;
}
.avatar-wrap img.frame-img {
    position: absolute;
    width: 60px; height: 60px;
    top: -4px; left: -4px;
    pointer-events: none;
    z-index: 2;
    max-width: none !important;
}

/* ── 多列布局（html2canvas 兼容）── */
.capture-grid {
    display: flex;
    gap: 16px;
    align-items: flex-start;
}
.capture-col {
    flex: 1;
    min-width: 0;
}

.gift-icon {
    width: 38px; height: 38px;
    flex-shrink: 0;
    object-fit: contain;
    border-radius: 6px;
}
.gift-value {
    background: rgba(255,255,255,0.2);
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 11px;
    font-weight: 600;
    white-space: nowrap;
    display: inline-block;
    line-height: 1.5;
    vertical-align: middle;
}
.gift-time {
    font-size: 10px;
    color: rgba(255,255,255,0.65);
    white-space: nowrap;
    display: inline-block;
    vertical-align: middle;
}
</style>
</head>
<body class="bg-gray-100 p-4">
<div id="app" class="max-w-6xl mx-auto">

<!-- ══════ 登录页 ══════ -->
<div v-if="!loggedIn" class="flex items-center justify-center min-h-[80vh]">
    <div class="bg-white p-8 rounded-xl shadow-md w-80">
        <h1 class="text-xl font-bold text-center text-blue-600 mb-6">BiliRobot 管理后台</h1>
        <div class="space-y-4">
            <input v-model="loginUser" placeholder="账号" class="border p-2 rounded w-full text-sm" @keyup.enter="doLogin">
            <input v-model="loginPass" type="password" placeholder="密码" class="border p-2 rounded w-full text-sm" @keyup.enter="doLogin">
            <div v-if="loginErr" class="text-red-500 text-sm">{{ loginErr }}</div>
            <button @click="doLogin" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded w-full">登录</button>
        </div>
    </div>
</div>

<!-- ══════ 主界面 ══════ -->
<div v-if="loggedIn">
<header class="mb-6 flex justify-between items-center bg-white p-4 rounded-xl shadow-sm">
    <h1 class="text-xl font-bold text-blue-600">🎯 BiliRobot 管理后台</h1>
    <div class="flex items-center gap-4 text-sm">
        <button @click="tab='ranking'" :class="tab==='ranking'?'text-blue-600 font-bold border-b-2 border-blue-600':''">送礼排行</button>
        <button @click="tab='export'"  :class="tab==='export' ?'text-blue-600 font-bold border-b-2 border-blue-600':''">精美导出</button>
        <button @click="tab='manage'" :class="tab==='manage'?'text-blue-600 font-bold border-b-2 border-blue-600':''">数据管理</button>
        <button @click="doLogout" class="text-gray-400 hover:text-red-500 ml-2">退出</button>
    </div>
</header>

<!-- ══════ 送礼排行 ══════ -->
<div v-if="tab==='ranking'" class="grid grid-cols-1 lg:grid-cols-2 gap-6">
    <div class="bg-white p-4 rounded-xl shadow-sm">
        <div class="flex flex-wrap gap-2 mb-3">
            <input type="date" v-model="rStart" class="border p-2 rounded text-sm flex-1 min-w-0">
            <input type="date" v-model="rEnd"   class="border p-2 rounded text-sm flex-1 min-w-0">
            <select v-model="rType" class="border p-2 rounded text-sm">
                <option value="all">全部</option>
                <option value="gift">一般礼物</option>
                <option value="blindbox">盲盒</option>
            </select>
            <button @click="loadRanking" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">查询</button>
        </div>
        <div v-if="errRanking" class="text-red-500 text-sm mb-2">{{ errRanking }}</div>
        <table class="w-full text-sm" v-if="ranking.length">
            <thead><tr class="bg-gray-50"><th class="p-2 text-left">#</th><th class="p-2 text-left">用户</th><th class="p-2 text-right">价值</th><th class="p-2 text-right">利润</th></tr></thead>
            <tbody>
                <tr v-for="(u,i) in ranking" :key="u.uid"
                    class="border-t hover:bg-blue-50 cursor-pointer transition"
                    @click="gotoExport(u.uid, u.uname)">
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
    <div class="bg-white p-4 rounded-xl shadow-sm w-full max-w-2xl mb-4 flex flex-wrap gap-2 items-end">
        <label class="text-xs text-gray-500 flex-[2]">UID<input type="number" v-model.number="eUid" class="border p-2 rounded w-full text-sm mt-1"></label>
        <label class="text-xs text-gray-500 flex-[2]">日期<input type="date" v-model="eDate" class="border p-2 rounded w-full text-sm mt-1"></label>
        <label class="text-xs text-gray-500 w-20">每列行数
            <input type="number" v-model.number="ePerCol" min="1" max="50" class="border p-2 rounded w-full text-sm mt-1">
        </label>
        <select v-model="eType" class="border p-2 rounded text-sm h-[38px]">
            <option value="all">全部</option>
            <option value="gift">仅一般礼物</option>
            <option value="blindbox">仅盲盒</option>
        </select>
        <button @click="loadExport" class="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded text-sm h-[38px]">生成</button>
        <button @click="downloadImage" v-if="exportList.length" class="bg-purple-500 hover:bg-purple-600 text-white px-4 py-2 rounded text-sm h-[38px]">📥 导出 PNG</button>
    </div>
    <div v-if="errExport" class="text-red-500 text-sm mb-2">{{ errExport }}</div>

    <!-- 精美截图区 — 手动分列布局（html2canvas 兼容） -->
    <div id="capture" v-if="exportList.length" class="w-full max-w-2xl">
        <div class="text-center text-gray-400 text-xs mb-3">
            <span class="font-semibold">{{ eName }}</span> ·
            {{ eDate }} · 礼物投喂明细
            <span v-if="eType==='gift'">（一般礼物）</span>
            <span v-else-if="eType==='blindbox'">（盲盒）</span>
        </div>
        <div class="capture-grid">
            <div v-for="(col,cidx) in exportCols" :key="cidx" class="capture-col">
                <div v-for="(item,idx2) in col" :key="item.id"
                     class="bili-card"
                     :class="cardBgClass(item.guard_level)">
                    <div class="avatar-wrap">
                        <!-- 舰长头像框 — 仅舰长有 -->
                        <img v-if="item.guard_level === 3"
                             :src="proxyImg('https://i0.hdslb.com/bfs/live/80f732943cc3367029df65e267960d56736a82ee.png')"
                             class="frame-img"
                             @error="$event.target.style.display='none'">
                        <img :src="proxyImg(item.avatar)" class="face"
                             @error="$event.target.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 44 44%22><rect width=%2244%22 height=%2244%22 fill=%22%23ccc%22 rx=%2222%22/></svg>'">
                    </div>
                    <div class="flex-1 min-w-0">
                        <div class="font-bold text-sm truncate">
                            {{ item.uname }}
                            <span v-if="item.guard_level" class="inline-block text-[9px] font-bold px-[5px] py-[1px] rounded-full ml-1 align-middle"
                                  :class="guardBadgeClass(item.guard_level)">{{ guardLabel(item.guard_level) }}</span>
                        </div>
                        <div class="flex items-center gap-1.5 mt-0.5 flex-wrap">
                            <span class="text-xs text-white/80">投喂了 {{ item.gift_name }} × {{ item.gift_num }}</span>
                            <span class="gift-value" v-if="item.price">¥{{ (item.price / 1000).toFixed(1) }}</span>
                            <span class="gift-time" v-if="item.ts">{{ fmtTime(item.ts) }}</span>
                        </div>
                    </div>
                    <img v-if="item.gift_icon" :src="proxyImg(item.gift_icon)" class="gift-icon"
                         @error="$event.target.style.display='none'">
                </div>
            </div>
        </div>
    </div>
    <div v-else-if="!errExport" class="text-gray-400 mt-20">输入 UID 和日期后点击"生成"</div>
</div>

<!-- ══════ 数据管理 ══════ -->
<div v-if="tab==='manage'" class="max-w-lg mx-auto bg-white p-6 rounded-xl shadow-sm">
    <h2 class="text-lg font-bold mb-4 text-red-600">⚠️ 数据管理</h2>
    <p class="text-sm text-gray-500 mb-4">注意：删除操作不可恢复。</p>
    <div class="flex gap-2 items-end">
        <label class="text-xs text-gray-500 flex-1">删除此日期之前的数据<input type="date" v-model="delDate" class="border p-2 rounded w-full text-sm mt-1"></label>
        <button @click="confirmDelete" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded text-sm h-[38px]">删除</button>
    </div>
    <div v-if="delResult" class="mt-4 text-sm">{{ delResult }}</div>
</div>

</div><!-- /loggedIn -->

</div>

<script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
<script>
const {createApp, ref, computed, nextTick} = Vue;
createApp({
    setup() {
        const loggedIn = ref(document.cookie.includes('session='));
        const loginUser = ref('');
        const loginPass = ref('');
        const loginErr = ref('');
        const tab = ref('ranking');

        // Ranking
        const rStart = ref(new Date().toISOString().slice(0,10));
        const rEnd   = ref(new Date().toISOString().slice(0,10));
        const rType  = ref('all');
        const ranking = ref([]);
        const errRanking = ref('');

        // Export
        const eUid = ref(0);
        const eName = ref('');
        const eDate = ref(new Date().toISOString().slice(0,10));
        const eType = ref('all');
        const ePerCol = ref(6);
        const exportList = ref([]);
        const errExport = ref('');

        // 手动分列计算
        const exportCols = Vue.computed(() => {
            const items = exportList.value;
            const perCol = Math.max(1, ePerCol.value);
            if (!items.length) return [];
            const cols = [];
            for (let i = 0; i < items.length; i += perCol) {
                cols.push(items.slice(i, i + perCol));
            }
            return cols;
        });

        // Manage
        const delDate = ref('');
        const delResult = ref('');

        let chartInst = null;

        // ── Auth ──
        async function doLogin() {
            loginErr.value = '';
            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({username: loginUser.value, password: loginPass.value})
                });
                if (!res.ok) { loginErr.value = '账号或密码错误'; return; }
                loggedIn.value = true;
            } catch(e) { loginErr.value = '登录失败: ' + e.message; }
        }
        function doLogout() {
            document.cookie = 'session=;max-age=0';
            loggedIn.value = false;
        }

        // ── Helpers ──
        function proxyImg(url) {
            if (!url) return '';
            if (url.startsWith('data:') || url.startsWith('blob:')) return url;
            if (url.startsWith('/api/')) return url;
            return '/api/proxy_image?url=' + encodeURIComponent(url);
        }
        function fmtTime(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            return d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
        }
        function cardBgClass(guardLevel) {
            if (guardLevel === 3) return 'bg-captain';
            if (guardLevel === 2) return 'bg-commander';
            if (guardLevel === 1) return 'bg-governor';
            return 'bg-default';
        }
        function guardLabel(guardLevel) {
            if (guardLevel === 3) return '舰长';
            if (guardLevel === 2) return '提督';
            if (guardLevel === 1) return '总督';
            return '';
        }
        function guardBadgeClass(guardLevel) {
            if (guardLevel === 3) return 'bg-blue-500';
            if (guardLevel === 2) return 'bg-purple-600';
            if (guardLevel === 1) return 'bg-amber-500';
            return '';
        }

        // ── Ranking ──
        async function loadRanking() {
            errRanking.value = '';
            ranking.value = [];
            try {
                const res = await fetch(`/api/ranking?start=${rStart.value}&end=${rEnd.value}&gift_type=${rType.value}`);
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

        // ── Export ──
        async function loadExport() {
            errExport.value = '';
            exportList.value = [];
            if (!eUid.value || !eDate.value) { errExport.value = '请填写 UID 和日期'; return; }
            try {
                const res = await fetch(`/api/user_gifts?uid=${eUid.value}&date=${eDate.value}&gift_type=${eType.value}`);
                if (!res.ok) { const txt = await res.text(); throw new Error(txt.slice(0,80)); }
                exportList.value = await res.json();
                if (exportList.value.length) {
                    eName.value = exportList.value[0].uname || '';
                } else {
                    errExport.value = '该用户当天无送礼记录';
                }
                await nextTick();
                await new Promise(r => setTimeout(r, 800));
            } catch(e) { errExport.value = '加载失败: ' + e.message; }
        }
        function gotoExport(uid, uname) {
            eUid.value = uid;
            eName.value = uname || '';
            tab.value = 'export';
            loadExport();
        }

        // ── Download PNG ──
        function downloadImage() {
            const el = document.getElementById('capture');
            if (!el) return;
            errExport.value = '正在生成图片...';
            html2canvas(el, {
                scale: 2,
                useCORS: true,
                allowTaint: true,
                backgroundColor: '#ffffff',
                logging: false,
                width: el.scrollWidth,
                height: el.scrollHeight,
                windowWidth: el.scrollWidth,
                windowHeight: el.scrollHeight,
            }).then(canvas => {
                const a = document.createElement('a');
                a.download = `gift_${eUid.value}_${eDate.value}.png`;
                a.href = canvas.toDataURL('image/png');
                a.click();
                errExport.value = '';
            }).catch(e => { errExport.value = '导出失败: ' + e.message; });
        }

        // ── Delete ──
        async function confirmDelete() {
            if (!delDate.value) { delResult.value = '请选择日期'; return; }
            if (!confirm(`确定删除 ${delDate.value} 之前的所有数据？此操作不可恢复！`)) return;
            try {
                const res = await fetch('/api/delete_old', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({date: delDate.value})
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                delResult.value = `已删除 ${data.deleted_events} 条事件记录`;
            } catch(e) { delResult.value = '删除失败: ' + e.message; }
        }

        return {loggedIn, loginUser, loginPass, loginErr, doLogin, doLogout,
                tab, rStart, rEnd, rType, ranking, errRanking, loadRanking,
                eUid, eName, eDate, eType, ePerCol, exportList, exportCols, errExport, loadExport, gotoExport, downloadImage, proxyImg, fmtTime, cardBgClass, guardLabel, guardBadgeClass,
                delDate, delResult, confirmDelete};
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
