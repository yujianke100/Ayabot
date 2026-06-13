"""
BiliRobot WebUI — 送礼统计 & 精美导出

Features:
- 登录认证（config.yaml 可配置账号密码）
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

# 全局配置，由 init_app() 设置
AUTH_USER = "admin"
AUTH_PASS = "admin"
_SESSION_TIMEOUT = 3600
_HTTP_HOST = "0.0.0.0"
_HTTP_PORT = 8000
_DB_PATH = "data/bot.db"

# LLM 配置（可变引用，webui 可保存更新）
_LLM_CONFIG_DICT: dict[str, Any] = {}
_CONFIG_YAML_PATH: str = "config.yaml"


def get_llm_config() -> dict[str, Any]:
    """公开访问 LLM 配置（bot 通过此函数获取运行时配置）."""
    return _LLM_CONFIG_DICT


def init_app(config: Any = None, config_path: str = "config.yaml") -> None:
    """从 AppConfig 初始化 WebUI 配置.
    
    Args:
        config: AppConfig 对象
        config_path: 配置文件的实际路径（用于解析相对路径）
    """
    global AUTH_USER, AUTH_PASS, _SESSION_TIMEOUT, _HTTP_HOST, _HTTP_PORT, _DB_PATH, _LLM_CONFIG_DICT, _CONFIG_YAML_PATH
    if config is None:
        _fallback_read_config()
        return
    AUTH_USER = config.web_ui.username
    AUTH_PASS = config.web_ui.password
    _SESSION_TIMEOUT = config.web_ui.session_timeout
    _HTTP_HOST = config.web_ui.host
    _HTTP_PORT = config.web_ui.port
    _DB_PATH = config.storage.sqlite_path
    if not os.path.isabs(_DB_PATH):
        _DB_PATH = str(Path(config_path).parent / _DB_PATH)
    _LLM_CONFIG_DICT.update({
        "enabled": config.llm.enabled,
        "provider": config.llm.provider,
        "api_key": config.llm.api_key,
        "base_url": config.llm.base_url,
        "model": config.llm.model,
        "wake_word": config.llm.wake_word,
        "temperature": config.llm.temperature,
        "top_p": config.llm.top_p,
        "max_tokens": config.llm.max_tokens,
        "system_prompt": config.llm.system_prompt,
        "context": {
            "enabled": config.llm.context.enabled,
            "mode": config.llm.context.mode,
            "content": config.llm.context.content,
            "max_messages": config.llm.context.max_messages,
        },
    })
    _CONFIG_YAML_PATH = config_path
    logger.info("webui configured: host=%s port=%s db=%s", _HTTP_HOST, _HTTP_PORT, os.path.abspath(_DB_PATH))


def _fallback_read_config() -> None:
    global _DB_PATH
    _cfg_path = Path("config.yaml")
    if _cfg_path.exists():
        _raw = yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
        _DB_PATH = str(_raw.get("storage", {}).get("sqlite_path", "data/bot.db"))
        if not os.path.isabs(_DB_PATH):
            _DB_PATH = str(_cfg_path.parent / _DB_PATH)
    logger.info("webui using db (fallback): %s", os.path.abspath(_DB_PATH))


app = FastAPI(title="BiliRobot Manager")

# ══════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════

_SESSIONS: dict[str, float] = {}  # token -> expiry (unix ts)
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
        _, e_end = _parse_date(end)

        where_clause = "ts >= ? AND ts < ?"
        params: list[Any] = [s_start, e_end]

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
        last_guard: dict[int, int] = {}  # uid -> 从 SEND_GIFT 记住的 guard_level
        for r in rows:
            item = dict(r)
            raw = json.loads(item.pop("raw_json", "{}")) if isinstance(item.get("raw_json"), str) else {}

            # COMBO_SEND 是连击总结包，其数量是前面SEND_GIFT的总和
            # 过滤掉避免重复计数，SEND_GIFT已完整记录了每次送礼
            if item.get("event_type", "") == "COMBO_SEND":
                # 但它的 guard_level 可以拿来给后续事件兜底（实际上相同UID的后继事件可能有完整数据）
                # 从 sender_uinfo 提取头像兜底
                if not raw.get("face"):
                    raw["face"] = raw.get("sender_uinfo", {}).get("base", {}).get("face", "")
                continue

            # 头像
            face = raw.get("face") or raw.get("data", {}).get("face") or ""
            item["avatar"] = face

            # 大航海等级
            guard = _safe_int(raw.get("guard_level") or raw.get("data", {}).get("guard_level", 0))
            if guard:
                last_guard[item["uid"]] = guard
            item["guard_level"] = guard

            gift_id = _safe_int(raw.get("giftId") or raw.get("gift_id") or 0)
            gift_name = raw.get("giftName") or raw.get("gift_name") or item.get("gift_name", "")
            item["gift_name"] = gift_name

            # 图标：按 id 查，查不到按名字查；COMBO_SEND 额外查 gift_info
            icon = ""
            if gift_id and gift_id in _GIFT_ICON_CACHE:
                icon = _GIFT_ICON_CACHE[gift_id]
            elif gift_name and gift_name in _GIFT_NAME_CACHE:
                icon = _GIFT_NAME_CACHE[gift_name]
            if not icon:
                icon = raw.get("gift_info", {}).get("img_basic", "")
            item["gift_icon"] = icon

            item["price"] = _safe_int(raw.get("price") or raw.get("total_coin") or 0)

            results.append(item)

        # 合并：相同礼物、相同 guard_level、2分钟内 → 合并数量
        merged: list[dict[str, Any]] = []
        for item in results:
            if merged:
                last = merged[-1]
                same_gift = last["gift_name"] == item["gift_name"]
                same_guard = last["guard_level"] == item["guard_level"]
                time_diff = abs(item["ts"] - last["ts"]) < 120
                if same_gift and same_guard and time_diff:
                    last["gift_num"] += item["gift_num"]
                    last["ts"] = max(last["ts"], item["ts"])
                    continue
            merged.append(item)
        return merged
    except Exception as exc:
        logger.exception("user_gifts api failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/user_dates")
async def api_user_dates(uid: int):
    """获取某用户有送礼记录的所有日期（去重后的 YYYY-MM-DD 列表）."""
    try:
        conn = _get_db()
        rows = conn.execute(
            """
            SELECT DISTINCT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d
            FROM gift_events
            WHERE uid = ?
            ORDER BY d DESC
            LIMIT 60
            """,
            (uid,),
        ).fetchall()
        return [r["d"] for r in rows if r["d"]]
    except Exception as exc:
        logger.exception("user_dates api failed")
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
#  LLM Config API
# ══════════════════════════════════════════════════════════════════


@app.get("/api/llm_config")
async def api_get_llm_config():
    """返回 LLM 配置（不含 api_key，仅用于展示状态）."""
    ctx = _LLM_CONFIG_DICT.get("context", {})
    return {
        "enabled": _LLM_CONFIG_DICT.get("enabled", False),
        "provider": _LLM_CONFIG_DICT.get("provider", "openai"),
        "has_api_key": bool(_LLM_CONFIG_DICT.get("api_key")),
        "base_url": _LLM_CONFIG_DICT.get("base_url", ""),
        "model": _LLM_CONFIG_DICT.get("model", ""),
        "wake_word": _LLM_CONFIG_DICT.get("wake_word", "ayabot"),
        "temperature": _LLM_CONFIG_DICT.get("temperature", 0.7),
        "top_p": _LLM_CONFIG_DICT.get("top_p", 0.9),
        "max_tokens": _LLM_CONFIG_DICT.get("max_tokens", 150),
        "system_prompt": _LLM_CONFIG_DICT.get("system_prompt", ""),
        "context": {
            "enabled": ctx.get("enabled", True),
            "mode": ctx.get("mode", "isolated"),
            "content": ctx.get("content", "llm_only"),
            "max_messages": ctx.get("max_messages", 10),
        },
    }


@app.post("/api/llm_config")
async def api_save_llm_config(request: Request):
    """保存 LLM 配置到 config.yaml 并更新内存."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    # 更新内存中的配置
    for key in ("enabled", "provider", "api_key", "base_url", "model", "wake_word", "temperature", "top_p", "max_tokens", "system_prompt"):
        if key in body:
            _LLM_CONFIG_DICT[key] = body[key]
    if "context" in body and isinstance(body["context"], dict):
        _LLM_CONFIG_DICT.setdefault("context", {})
        for ck in ("enabled", "mode", "content", "max_messages"):
            if ck in body["context"]:
                _LLM_CONFIG_DICT["context"][ck] = body["context"][ck]

    # 回写到 config.yaml
    try:
        cfg_path = Path(_CONFIG_YAML_PATH)
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

        llm_section = raw.setdefault("llm", {})
        llm_section["enabled"] = _LLM_CONFIG_DICT.get("enabled", False)
        llm_section["provider"] = _LLM_CONFIG_DICT.get("provider", "openai")
        llm_section["api_key"] = _LLM_CONFIG_DICT.get("api_key", "")
        llm_section["base_url"] = _LLM_CONFIG_DICT.get("base_url", "")
        llm_section["model"] = _LLM_CONFIG_DICT.get("model", "")
        llm_section["wake_word"] = _LLM_CONFIG_DICT.get("wake_word", "ayabot")
        llm_section["temperature"] = _LLM_CONFIG_DICT.get("temperature", 0.7)
        llm_section["top_p"] = _LLM_CONFIG_DICT.get("top_p", 0.9)
        llm_section["max_tokens"] = _LLM_CONFIG_DICT.get("max_tokens", 150)
        llm_section["system_prompt"] = _LLM_CONFIG_DICT.get("system_prompt", "")
        llm_section["context"] = {
            "enabled": _LLM_CONFIG_DICT.get("context", {}).get("enabled", True),
            "mode": _LLM_CONFIG_DICT.get("context", {}).get("mode", "isolated"),
            "content": _LLM_CONFIG_DICT.get("context", {}).get("content", "llm_only"),
            "max_messages": _LLM_CONFIG_DICT.get("context", {}).get("max_messages", 10),
        }

        # 手动写 YAML 保留锚点和格式
        cfg_path.write_text(yaml.dump(raw, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        logger.info("llm config saved to %s", cfg_path)
    except Exception as exc:
        logger.warning("failed to save config.yaml: %s", exc)
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

    return {"ok": True}


@app.post("/api/llm_test")
async def api_llm_test(request: Request):
    """测试 LLM 连接."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    from app.llm_client import LLMClient

    client = LLMClient(
        provider=_LLM_CONFIG_DICT.get("provider", "openai"),
        api_key=_LLM_CONFIG_DICT.get("api_key", ""),
        base_url=_LLM_CONFIG_DICT.get("base_url", ""),
        model=_LLM_CONFIG_DICT.get("model", ""),
        system_prompt=_LLM_CONFIG_DICT.get("system_prompt", ""),
    )
    text = body.get("text", "")
    reply = await client.chat(user_text=text, uname="测试")
    return {"reply": reply or "(无回复)"}


import subprocess
import threading


@app.post("/api/restart")
async def api_restart():
    """重启 B站 Bot 服务 (在后台线程执行, 避免杀死自身进程)."""
    def _do_restart():
        import time
        time.sleep(0.5)
        try:
            subprocess.run(
                ["systemctl", "restart", "bili-live-bot.service"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"ok": True, "message": "服务正在重启..."}


# ══════════════════════════════════════════════════════════════
#  B站 扫码登录 API
# ══════════════════════════════════════════════════════════════

import io as _io
import base64 as _base64
from bilibili_api import login_v2

# 存储活跃的 QR 登录会话 (session_id → QRLoginObj + QR image)
_BILI_LOGIN_SESSIONS: dict[str, dict] = {}


@app.post("/api/bili_login/start")
async def api_bili_login_start():
    """生成 B站 QR 登录二维码并返回 session_id."""
    import qrcode
    import uuid
    import time as _time

    session_id = uuid.uuid4().hex[:12]

    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()

    # 获取 QR URL（使用属性访问，bilibili_api 内部用 name mangling）
    qr_url = qr._QrCodeLogin__qr_link  # noqa: SLF001

    # 用 Python qrcode 生成 base64 图片
    qr_img = qrcode.make(qr_url)
    buf = _io.BytesIO()
    qr_img.save(buf, format="PNG")
    img_b64 = _base64.b64encode(buf.getvalue()).decode("ascii")

    _BILI_LOGIN_SESSIONS[session_id] = {
        "qr": qr,
        "created_at": _time.time(),
        "state": "waiting",
    }

    return {
        "ok": True,
        "session_id": session_id,
        "qr_image": f"data:image/png;base64,{img_b64}",
        "qr_url": qr_url,
    }


@app.get("/api/bili_login/status")
async def api_bili_login_status(session_id: str):
    """查询 B站 QR 登录状态."""
    sess = _BILI_LOGIN_SESSIONS.get(session_id)
    if not sess:
        return {"state": "expired"}

    qr = sess["qr"]

    if qr.has_done():
        sess["state"] = "done"
        return {"state": "done"}

    try:
        state = await qr.check_state()
        state_str = state.value if hasattr(state, "value") else str(state)
    except Exception as exc:
        logger.warning("bili login check_state error: %s", exc)
        return {"state": "error", "message": str(exc)}

    if state == login_v2.QrCodeLoginEvents.TIMEOUT:
        sess["state"] = "timeout"
        _BILI_LOGIN_SESSIONS.pop(session_id, None)
        return {"state": "timeout"}

    if state == login_v2.QrCodeLoginEvents.SCAN:
        sess["state"] = "scanned"
        return {"state": "scanned"}

    if state == login_v2.QrCodeLoginEvents.CONF:
        sess["state"] = "done"
        return {"state": "done"}

    return {"state": "waiting"}


@app.post("/api/bili_login/save")
async def api_bili_login_save(request: Request):
    """保存 B站 二维码登录凭据并重启服务."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    session_id = body.get("session_id", "")
    sess = _BILI_LOGIN_SESSIONS.get(session_id)
    if not sess:
        return {"ok": False, "error": "session expired"}

    qr = sess["qr"]
    if not qr.has_done():
        return {"ok": False, "error": "login not completed"}

    try:
        credential = qr.get_credential()
    except Exception as exc:
        return {"ok": False, "error": f"get credential failed: {exc}"}

    # 保存凭据
    cookies = credential.get_cookies()
    store_path = Path(_CONFIG_YAML_PATH).parent / "data" / "credential.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps({
            "SESSDATA": cookies.get("SESSDATA", ""),
            "bili_jct": cookies.get("bili_jct", ""),
            "buvid3": cookies.get("buvid3", ""),
            "DedeUserID": cookies.get("DedeUserID", ""),
            "ac_time_value": cookies.get("ac_time_value", ""),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _BILI_LOGIN_SESSIONS.pop(session_id, None)

    # 重启服务
    import threading as _threading
    def _restart():
        import time as _t
        _t.sleep(1)
        try:
            import subprocess as _sp
            _sp.run(["systemctl", "restart", "bili-live-bot.service"],
                    capture_output=True, text=True, timeout=30)
        except Exception:
            pass
    _threading.Thread(target=_restart, daemon=True).start()

    return {"ok": True, "message": "凭据已保存，服务正在重启..."}


# ══════════════════════════════════════════════════════════════
#  通用机器人配置 API
# ══════════════════════════════════════════════════════════════


@app.get("/api/general_config")
async def api_get_general_config():
    """返回机器人全局配置."""
    from app.config import load_config, config_to_dict
    try:
        cfg = load_config(_CONFIG_YAML_PATH)
        return config_to_dict(cfg)
    except Exception as exc:
        logger.warning("general config load failed: %s", exc)
        return {"error": str(exc)}


@app.post("/api/general_config")
async def api_save_general_config(request: Request):
    """保存机器人全局配置到 config.yaml."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    from app.config import update_config_from_dict
    ok = update_config_from_dict(body, _CONFIG_YAML_PATH)
    return {"ok": ok}


# ══════════════════════════════════════════════════════════════
#  HTML
# ══════════════════════════════════════════════════════════════════

INDEX_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ayabot 直播间机器人</title>
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

/* ── 多列布局（水平可滚动，居中）── */
.capture-grid {
    display: flex;
    gap: 16px;
    align-items: flex-start;
}
.capture-col {
    min-width: 0;
    flex-shrink: 0;
}
.capture-inner {
    display: inline-block;
    margin: 0 auto;
    text-align: left;
}
#capture {
    text-align: center;
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
        <h1 class="text-xl font-bold text-center text-blue-600 mb-6">{{ botName }} 管理后台</h1>
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
    <h1 class="text-xl font-bold text-blue-600">🎯 {{ botName }} 管理后台</h1>
    <div class="flex items-center gap-4 text-sm">
        <button @click="tab='ranking'" :class="tab==='ranking'?'text-blue-600 font-bold border-b-2 border-blue-600':''">送礼排行</button>
        <button @click="tab='export'"  :class="tab==='export' ?'text-blue-600 font-bold border-b-2 border-blue-600':''">精美导出</button>
        <button @click="tab='llm'"    :class="tab==='llm'    ?'text-blue-600 font-bold border-b-2 border-blue-600':''">AI 回复</button>
        <button @click="tab='config'" :class="tab==='config'?'text-blue-600 font-bold border-b-2 border-blue-600':''">机器人配置</button>
        <button @click="tab='bili_login'" :class="tab==='bili_login'?'text-blue-600 font-bold border-b-2 border-blue-600':''">B站登录</button>
        <button @click="tab='manage'" :class="tab==='manage'?'text-blue-600 font-bold border-b-2 border-blue-600':''">数据管理</button>
        <button @click="tab='help'"  :class="tab==='help' ?'text-blue-600 font-bold border-b-2 border-blue-600':''">帮助</button>
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
        <div class="overflow-y-auto max-h-[520px]" v-if="ranking.length">
            <table class="w-full text-sm">
                <thead><tr class="bg-gray-50 sticky top-0"><th class="p-2 text-left">#</th><th class="p-2 text-left">用户</th><th class="p-2 text-right">价值</th><th class="p-2 text-right">利润</th></tr></thead>
                <tbody>
                    <tr v-for="(u,i) in ranking" :key="u.uid"
                        class="border-t hover:bg-blue-50 cursor-pointer transition"
                        @click="gotoExport(u.uid, u.uname)">
                        <td class="p-2">{{ i+1 }}</td>
                        <td class="p-2">{{ u.uname }}</td>
                        <td class="p-2 text-right">{{ Number(u.total_val).toFixed(1) }}</td>
                        <td class="p-2 text-right" :class="Number(u.total_profit)>=0?'text-red-500':'text-green-500'">{{ Number(u.total_profit).toFixed(1) }}</td>
                    </tr>
                </tbody>
            </table>
        </div>
        <div v-else-if="!errRanking" class="text-gray-400 text-center py-8">暂无数据</div>
    </div>
    <div class="bg-white p-4 rounded-xl shadow-sm flex flex-col min-h-[300px]">
        <canvas id="chartRank" class="flex-1 min-h-0"></canvas>
    </div>
</div>

<!-- ══════ 精美导出 ══════ -->
<div v-if="tab==='export'" class="flex flex-col items-center w-full">
    <div class="bg-white p-4 rounded-xl shadow-sm w-full max-w-3xl mb-4 space-y-3">
        <div class="flex flex-wrap gap-2 items-end">
            <label class="text-xs text-gray-500 flex-[2]">UID<input type="number" v-model.number="eUid" class="border p-2 rounded w-full text-sm mt-1" @input="onUidInput"></label>
            <label class="text-xs text-gray-500 flex-[2]">
                日期
                <div class="relative">
                    <input type="text" readonly :value="eDate" placeholder="点击选择日期"
                           class="border p-2 rounded w-full text-sm mt-1 cursor-pointer bg-white"
                           @click="showCalendar = !showCalendar">
                    <div v-if="showCalendar" @click.stop class="absolute top-full left-0 mt-1 bg-white border rounded-xl shadow-lg z-50 p-3 w-[300px]">
                        <div class="flex justify-between items-center mb-2">
                            <button @click="calMonth--" class="px-2 py-1 hover:bg-gray-100 rounded text-sm">&lt;</button>
                            <span class="text-sm font-bold">{{ calYear }}年{{ calMonth+1 }}月</span>
                            <button @click="calMonth++" class="px-2 py-1 hover:bg-gray-100 rounded text-sm">&gt;</button>
                        </div>
                        <div class="grid grid-cols-7 gap-1 text-center text-xs mb-1">
                            <div class="text-gray-400 font-medium">日</div>
                            <div class="text-gray-400 font-medium">一</div>
                            <div class="text-gray-400 font-medium">二</div>
                            <div class="text-gray-400 font-medium">三</div>
                            <div class="text-gray-400 font-medium">四</div>
                            <div class="text-gray-400 font-medium">五</div>
                            <div class="text-gray-400 font-medium">六</div>
                        </div>
                        <div class="grid grid-cols-7 gap-1">
                            <template v-for="(day,i) in calDays" :key="i">
                                <div v-if="!day" class="h-8"></div>
                                <button v-else
                                        :disabled="!day.hasData"
                                        @click="pickDate(day.ymd)"
                                        class="h-8 rounded text-xs transition"
                                        :class="day.hasData
                                            ? (day.ymd === eDate ? 'bg-blue-600 text-white' : 'bg-blue-100 text-blue-700 hover:bg-blue-200 cursor-pointer')
                                            : 'text-gray-300 cursor-not-allowed'">
                                    {{ day.d }}
                                </button>
                            </template>
                        </div>
                        <div class="text-[10px] text-gray-400 mt-2 text-center">蓝色 = 有数据，灰色 = 无数据</div>
                    </div>
                </div>
            </label>
            <select v-model="eType" class="border p-2 rounded text-sm h-[38px]">
                <option value="all">全部</option>
                <option value="gift">仅一般礼物</option>
                <option value="blindbox">仅盲盒</option>
            </select>
            <button @click="loadExport" class="bg-green-500 hover:bg-green-600 text-white px-5 py-2 rounded text-sm h-[38px]">生成</button>
        </div>
        <div class="flex flex-wrap gap-4 items-end">
            <label class="text-xs text-gray-500">每列行数
                <input type="number" v-model.number="ePerCol" min="1" max="50" class="border p-2 rounded text-sm mt-1 w-20">
            </label>
            <label class="text-xs text-gray-500 flex-1 min-w-[200px]">
                单列宽度 <span class="text-sm font-mono ml-1">{{ eColWidth }}px</span>
                <input type="range" v-model.number="eColWidth" min="200" max="600" step="10" class="w-full mt-1">
            </label>
        </div>
    </div>
    <div v-if="errExport" class="text-red-500 text-sm mb-2">{{ errExport }}</div>

    <!-- 精美数据展示区（水平可滚动，居中） -->
    <div id="capture" v-if="exportList.length" class="w-full overflow-x-auto">
        <div class="capture-inner">
        <div class="text-center text-gray-400 text-xs mb-3">
            <span class="font-semibold">{{ eName }}</span> ·
            {{ eDate }} · 礼物投喂明细
            <span v-if="eType==='gift'">（一般礼物）</span>
            <span v-else-if="eType==='blindbox'">（盲盒）</span>
        </div>
        <div class="capture-grid" :style="{ minWidth: exportCols.length * (eColWidth + 16) + 'px' }">
            <div v-for="(col,cidx) in exportCols" :key="cidx" class="capture-col" :style="{ minWidth: eColWidth + 'px', maxWidth: eColWidth + 'px' }">
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
</div>
    <div v-else-if="!errExport" class="text-gray-400 mt-20">输入 UID 和日期后点击"生成"</div>
</div>

<!-- ══════ 机器人配置 ══════ -->
<div v-if="tab==='config'" class="max-w-2xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
        <h2 class="text-lg font-bold">⚙️ 机器人配置</h2>

        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">机器人名称
                <input type="text" v-model="botName" class="border p-2 rounded w-full text-sm mt-1" placeholder="ayabot">
            </label>
            <label class="text-xs text-gray-500">Web 端口
                <input type="number" v-model.number="cfgPort" min="1024" max="65535" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">监听地址
                <input type="text" v-model="cfgHost" class="border p-2 rounded w-full text-sm mt-1" placeholder="0.0.0.0">
            </label>
        </div>

        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">直播间 ID
                <input type="number" v-model.number="cfgRoomId" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">主播 UID
                <input type="number" v-model.number="cfgAnchorUid" class="border p-2 rounded w-full text-sm mt-1">
            </label>
        </div>

        <hr>
        <h3 class="text-sm font-bold">⏱️ 冷却时间（秒）</h3>
        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">欢迎同用户间隔
                <input type="number" v-model.number="cfgWelcomeCd" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">感谢同用户间隔
                <input type="number" v-model.number="cfgThanksCd" class="border p-2 rounded w-full text-sm mt-1">
            </label>
        </div>

        <hr>
        <h3 class="text-sm font-bold">🚦 限流</h3>
        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">弹幕发送间隔(秒)
                <input type="number" v-model="cfgSendInterval" step="0.1" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">重试次数
                <input type="number" v-model.number="cfgRetry" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">队列上限
                <input type="number" v-model.number="cfgMaxQueue" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">回复延迟(秒)
                <input type="number" v-model="cfgReplyDelay" step="0.1" class="border p-2 rounded w-full text-sm mt-1">
            </label>
        </div>

        <hr>
        <h3 class="text-sm font-bold">🎛️ 功能开关</h3>
        <div class="grid grid-cols-2 gap-2 text-sm">
            <label class="flex items-center gap-2"><input type="checkbox" v-model="cfgWelcomeOn" class="w-4 h-4"> 欢迎</label>
            <label class="flex items-center gap-2"><input type="checkbox" v-model="cfgThanksOn" class="w-4 h-4"> 感谢</label>
            <label class="flex items-center gap-2"><input type="checkbox" v-model="cfgBlindboxOn" class="w-4 h-4"> 盲盒统计</label>
            <label class="flex items-center gap-2"><input type="checkbox" v-model="cfgGuardOn" class="w-4 h-4"> 大航海感谢</label>
            <label class="flex items-center gap-2"><input type="checkbox" v-model="cfgConnectedMsg" class="w-4 h-4"> 连接消息</label>
        </div>

        <hr>
        <h3 class="text-sm font-bold">📝 回复模板</h3>
        <div class="space-y-3">
            <label class="text-xs text-gray-500 block">欢迎模板
                <input type="text" v-model="cfgWelcomeTmpl" placeholder="欢迎{uname}来到直播间" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">感谢模板
                <input type="text" v-model="cfgThanksTmpl" placeholder="感谢{uname}的{gift_name}x{gift_num}!" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">大航海 - 舰长
                <input type="text" v-model="cfgGuardCaptain" placeholder="感谢{uname}上舰！" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">大航海 - 提督
                <input type="text" v-model="cfgGuardCommander" placeholder="感谢{uname}支持！" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">大航海 - 总督
                <input type="text" v-model="cfgGuardGovernor" placeholder="感谢{uname}支持！" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">大航海 - 默认
                <input type="text" v-model="cfgGuardDefault" placeholder="感谢{uname}开通大航海！" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500 block">连接消息
                <input type="text" v-model="cfgConnMsg" placeholder="来了喵~" class="border p-2 rounded w-full text-sm mt-1">
            </label>
        </div>

        <hr>
        <h3 class="text-sm font-bold">⏰ 定时消息</h3>
        <div class="space-y-3">
            <label class="flex items-center gap-2 text-sm">
                <input type="checkbox" v-model="cfgPeriodicOn" class="w-4 h-4">
                启用定时消息（仅在开播时发送）
            </label>
            <div class="grid grid-cols-2 gap-4">
                <label class="text-xs text-gray-500">间隔（秒）
                    <input type="number" v-model.number="cfgPeriodicInterval" min="30" max="86400" class="border p-2 rounded w-full text-sm mt-1">
                    <span class="text-xs text-gray-400">默认 600 秒（10 分钟）</span>
                </label>
            </div>
            <label class="text-xs text-gray-500 block">消息内容
                <input type="text" v-model="cfgPeriodicTmpl" placeholder="欢迎关注直播间~点个关注不迷路！" class="border p-2 rounded w-full text-sm mt-1">
                <span class="text-xs text-gray-400">留空则不发送定时消息</span>
            </label>
        </div>

        <div class="flex items-center gap-4 mt-4">
            <button @click="saveGeneralConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
            <span v-if="cfgSaveMsg" class="text-sm" :class="cfgSaveOk ? 'text-green-600' : 'text-red-500'">{{ cfgSaveMsg }}</span>
            <button @click="loadGeneralConfig" class="text-gray-500 hover:text-gray-700 underline text-sm">刷新</button>
        </div>
        <div class="flex items-center gap-4 mt-2 border-t pt-4">
            <button @click="restartService" class="bg-red-500 hover:bg-red-600 text-white px-6 py-2 rounded text-sm">🔄 重启服务</button>
            <span v-if="restartMsg" class="text-sm" :class="restartOk ? 'text-green-600' : 'text-red-500'">{{ restartMsg }}</span>
        </div>
    </div>
</div>

<!-- ══════ AI 回复设置 ══════ -->
<div v-if="tab==='llm'" class="max-w-2xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
        <h2 class="text-lg font-bold">🤖 AI 回复设置</h2>
        <p class="text-xs text-gray-400">用户发送 <code class="bg-gray-100 px-1 rounded">#{{ llmWakeWord || 'ayabot' }} &lt;聊天内容&gt;</code> 时调用 LLM API 自动回复。</p>

        <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" v-model="llmEnabled" class="w-4 h-4">
            启用 AI 回复
        </label>

        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">接口格式
                <select v-model="llmProvider" class="border p-2 rounded w-full text-sm mt-1">
                    <option value="openai">OpenAI 格式</option>
                    <option value="anthropic">Anthropic 格式</option>
                </select>
            </label>
            <label class="text-xs text-gray-500">模型
                <input type="text" v-model="llmModel" placeholder="gpt-4o-mini" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">唤醒词
                <div class="flex items-center mt-1">
                    <span class="bg-gray-200 px-2 py-[7px] rounded-l text-sm font-mono text-gray-500">#</span>
                    <input type="text" v-model="llmWakeWord" placeholder="ayabot" class="border p-2 rounded-r w-full text-sm flex-1">
                </div>
                <span class="text-xs text-gray-400">用户发送 <code>#{{ llmWakeWord || 'ayabot' }} &lt;聊天内容&gt;</code> 触发</span>
            </label>
            <label class="text-xs text-gray-500">温度 (temperature)
                <input type="number" v-model="llmTemp" step="0.1" min="0" max="2" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">Top P
                <input type="number" v-model="llmTopP" step="0.05" min="0" max="1" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">最大 Token
                <input type="number" v-model.number="llmMaxTokens" min="1" max="2000" class="border p-2 rounded w-full text-sm mt-1">
            </label>
        </div>

        <label class="text-xs text-gray-500">API Key
            <input type="password" v-model="llmApiKey" placeholder="sk-..." class="border p-2 rounded w-full text-sm mt-1">
        </label>

        <label class="text-xs text-gray-500">Base URL
            <input type="url" v-model="llmBaseUrl" placeholder="https://api.openai.com/v1" class="border p-2 rounded w-full text-sm mt-1">
        </label>

        <label class="text-xs text-gray-500">人设（System Prompt）
            <textarea v-model="llmPrompt" rows="3" class="border p-2 rounded w-full text-sm mt-1" placeholder="你是ayabot，一个可爱温柔的虚拟主播助手。"></textarea>
        </label>

        <hr class="my-2">
        <h3 class="text-sm font-bold">🧠 对话上下文</h3>

        <label class="flex items-center gap-2 text-sm">
            <input type="checkbox" v-model="ctxEnabled" class="w-4 h-4">
            开启上下文记忆
        </label>

        <div v-if="ctxEnabled" class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">隔离方式
                <select v-model="ctxMode" class="border p-2 rounded w-full text-sm mt-1">
                    <option value="isolated">按用户隔离</option>
                    <option value="merged">所有用户合并</option>
                </select>
            </label>
            <label class="text-xs text-gray-500">记录内容
                <select v-model="ctxContent" class="border p-2 rounded w-full text-sm mt-1">
                    <option value="llm_only">仅 #{{ llmWakeWord }} 对话</option>
                    <option value="all">所有弹幕消息</option>
                </select>
            </label>
        </div>

        <label v-if="ctxEnabled" class="text-xs text-gray-500">保留条数
            <input type="number" v-model.number="ctxMaxMsg" min="1" max="50" class="border p-2 rounded w-full text-sm mt-1">
        </label>

        <div class="flex items-center gap-4">
            <button @click="saveLlmConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
            <span v-if="llmSaveMsg" class="text-sm" :class="llmSaveOk ? 'text-green-600' : 'text-red-500'">{{ llmSaveMsg }}</span>
        </div>

        <hr class="my-2">
        <h3 class="text-sm font-bold">测试</h3>
        <div class="flex gap-2">
            <input type="text" v-model="llmTestText" placeholder="输入测试消息" class="border p-2 rounded flex-1 text-sm"
                   @keyup.enter="testLlm">
            <button @click="testLlm" class="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded text-sm">测试</button>
        </div>
        <div v-if="llmTestResp" class="text-sm bg-gray-50 p-3 rounded">{{ llmTestResp }}</div>
    </div>
</div>

<!-- ══════ B站 扫码登录 ══════ -->
<div v-if="tab==='bili_login'" class="max-w-lg mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm text-center space-y-4">
        <h2 class="text-lg font-bold">🅱️ B 站扫码登录</h2>
        <p class="text-sm text-gray-500">首次启动或 Cookie 过期时，使用 Bilibili App 扫码登录</p>

        <div v-if="biliLoginState === 'idle'">
            <button @click="startBiliLogin" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">📱 生成二维码</button>
        </div>

        <div v-if="biliLoginState === 'loading'" class="py-4">
            <p class="text-gray-400">正在生成二维码...</p>
        </div>

        <div v-if="biliLoginState === 'waiting' || biliLoginState === 'scanned'" class="space-y-3">
            <div class="flex justify-center">
                <img :src="biliQrImage" class="border-2 border-gray-200 rounded-lg" style="width:200px;height:200px">
            </div>
            <div class="flex items-center justify-center gap-2">
                <span v-if="biliLoginState==='waiting'" class="inline-block w-3 h-3 rounded-full bg-green-400 animate-pulse"></span>
                <span v-if="biliLoginState==='scanned'" class="inline-block w-3 h-3 rounded-full bg-yellow-400 animate-pulse"></span>
                <span class="text-sm">{{ biliLoginState === 'waiting' ? '等待扫码...' : '已扫码，请在手机上确认' }}</span>
            </div>
        </div>

        <div v-if="biliLoginState === 'done'" class="space-y-3 py-4">
            <div class="text-4xl text-green-500">✅</div>
            <p class="text-green-600 font-bold">扫码成功！</p>
            <button @click="saveBiliLogin" class="bg-green-500 hover:bg-green-600 text-white px-6 py-2 rounded text-sm">
                保存凭据并重启服务
            </button>
        </div>

        <div v-if="biliLoginState === 'saving'" class="py-4">
            <p class="text-gray-400">正在保存凭据并重启服务...</p>
        </div>

        <div v-if="biliLoginState === 'timeout'" class="space-y-3">
            <p class="text-red-500">⏰ 二维码已过期</p>
            <button @click="startBiliLogin" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">重新生成</button>
        </div>

        <div v-if="biliLoginState === 'error'" class="space-y-3">
            <p class="text-red-500">❌ {{ biliLoginError }}</p>
            <button @click="startBiliLogin" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">重试</button>
        </div>
    </div>
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


<!-- ══════ 帮助页面 ══════ -->
<div v-if="tab==='help'" class="max-w-3xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-6 text-sm leading-relaxed">
        <h2 class="text-lg font-bold">📖 使用指南</h2>

        <div>
            <h3 class="font-bold text-blue-600 mb-1">🎯 弹幕命令</h3>
            <table class="w-full text-xs border-collapse">
                <thead><tr class="bg-gray-100"><th class="border p-1 text-left">命令</th><th class="border p-1 text-left">说明</th></tr></thead>
                <tbody>
                    <tr><td class="border p-1"><code>#签到</code></td><td class="border p-1">每日签到（按直播场次计算）</td></tr>
                    <tr><td class="border p-1"><code>#抽签</code></td><td class="border p-1">今日运势抽签</td></tr>
                    <tr><td class="border p-1"><code>#今日盲盒</code></td><td class="border p-1">今日盲盒统计</td></tr>
                    <tr><td class="border p-1"><code>#本月盲盒</code></td><td class="border p-1">本月盲盒统计</td></tr>
                    <tr><td class="border p-1"><code>#{{ llmWakeWord || 'ayabot' }} &lt;聊天内容&gt;</code></td><td class="border p-1">AI 智能回复（需在 AI 回复页开启）</td></tr>
                    <tr><td class="border p-1"><code>#帮助</code></td><td class="border p-1">显示所有命令</td></tr>
                </tbody>
            </table>
        </div>

        <div>
            <h3 class="font-bold text-blue-600 mb-1">🤖 功能介绍</h3>
            <ul class="list-disc pl-4 space-y-1 text-xs">
                <li><b>欢迎</b> — 新观众进入直播间时自动发送欢迎消息</li>
                <li><b>感谢</b> — 观众送礼物/盲盒时自动感谢</li>
                <li><b>大航海感谢</b> — 舰长/提督/总督购买时自动感谢</li>
                <li><b>关键词回复</b> — 设定关键词自动回复（如「群」回复群号）</li>
                <li><b>AI 回复</b> — 唤醒词触发 LLM 智能对话，支持自定义人设和上下文记忆</li>
                <li><b>连接消息</b> — 机器人成功连接直播间时发送消息</li>
            </ul>
        </div>

        <div>
            <h3 class="font-bold text-blue-600 mb-1">⚙️ 配置提示</h3>
            <ul class="list-disc pl-4 space-y-1 text-xs">
                <li>机器人配置修改后需要点击「重启服务」按钮才能生效</li>
                <li>AI 回复配置保存后立即生效，无需重启</li>
                <li>直播间 ID 和主播 UID 修改后需要重启才能生效</li>
                <li>如果收不到弹幕命令回复，可以尝试调大「回复延迟」或「弹幕发送间隔」</li>
            </ul>
        </div>

        <div>
            <h3 class="font-bold text-blue-600 mb-1">🎨 界面功能</h3>
            <ul class="list-disc pl-4 space-y-1 text-xs">
                <li><b>送礼排行</b> — 按日期范围查看送礼排行（支持礼物/盲盒/全部）</li>
                <li><b>精美导出</b> — 按用户导出精美礼物卡片，支持多列布局</li>
                <li><b>数据管理</b> — 删除指定日期之前的旧数据</li>
            </ul>
        </div>
    </div>
</div>
</div><!-- /loggedIn -->

</div>

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
        const eColWidth = ref(340);
        const exportList = ref([]);
        const exportDates = ref([]);
        const errExport = ref('');

        // 日历状态
        const showCalendar = ref(false);
        const calYear = ref(new Date().getFullYear());
        const calMonth = ref(new Date().getMonth());
        const exportDatesSet = ref(new Set());

        // 日历天数计算
        const calDays = Vue.computed(() => {
            const year = calYear.value;
            const month = calMonth.value;
            const firstDay = new Date(year, month, 1).getDay();
            const daysInMonth = new Date(year, month + 1, 0).getDate();
            const cells = [];
            for (let i = 0; i < firstDay; i++) cells.push(null);
            for (let d = 1; d <= daysInMonth; d++) {
                const ymd = `${year}-${String(month+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
                cells.push({ d, ymd, hasData: exportDatesSet.value.has(ymd) });
            }
            return cells;
        });

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

        // LLM Config
        const llmEnabled = ref(false);
        const llmProvider = ref('openai');
        const llmApiKey = ref('');
        const llmBaseUrl = ref('');
        const llmModel = ref('');
        const llmWakeWord = ref('ayabot');
        const llmTemp = ref(0.7);
        const llmTopP = ref(0.9);
        const llmMaxTokens = ref(150);
        const llmPrompt = ref('');
        const llmSaveMsg = ref('');
        const llmSaveOk = ref(false);
        const llmTestText = ref('');
        const llmTestResp = ref('');

        // LLM Context Config
        const ctxEnabled = ref(true);
        const ctxMode = ref('isolated');
        const ctxContent = ref('llm_only');
        const ctxMaxMsg = ref(10);

        // General Config
        const botName = ref('ayabot');
        const cfgHost = ref('0.0.0.0');
        const cfgPort = ref(8000);
        const cfgRoomId = ref(0);
        const cfgAnchorUid = ref(0);
        const cfgWelcomeCd = ref(600);
        const cfgThanksCd = ref(10);
        const cfgSendInterval = ref(1.2);
        const cfgRetry = ref(2);
        const cfgMaxQueue = ref(50);
        const cfgReplyDelay = ref(1.0);
        const cfgWelcomeOn = ref(true);
        const cfgThanksOn = ref(true);
        const cfgBlindboxOn = ref(true);
        const cfgGuardOn = ref(true);
        const cfgConnectedMsg = ref(false);
        const cfgWelcomeTmpl = ref('');
        const cfgThanksTmpl = ref('');
        const cfgGuardCaptain = ref('');
        const cfgGuardCommander = ref('');
        const cfgGuardGovernor = ref('');
        const cfgGuardDefault = ref('');
        const cfgConnMsg = ref('');
        const cfgPeriodicOn = ref(true);
        const cfgPeriodicInterval = ref(600);
        const cfgPeriodicTmpl = ref('');
        const cfgSaveMsg = ref('');
        const cfgSaveOk = ref(false);
        const restartMsg = ref('');
        const restartOk = ref(false);

        // B站 登录
        let biliPollTimer = null;
        const biliLoginState = ref('idle');
        const biliQrImage = ref('');
        const biliLoginError = ref('');
        const biliSessionId = ref('');

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
                await loadLlmConfig();
                await loadGeneralConfig();
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
        async function loadUserDates() {
            if (!eUid.value) return;
            try {
                const res = await fetch(`/api/user_dates?uid=${eUid.value}`);
                if (!res.ok) return;
                const arr = await res.json();
                exportDates.value = arr;
                exportDatesSet.value = new Set(arr);
                // 如果有数据日期，默认选第一个
                if (arr.length && !exportList.value.length) {
                    // 不自动切换，让用户自己选
                }
            } catch(e) { /* ignore */ }
        }
        function onUidInput() {
            showCalendar.value = false;
            exportList.value = [];
            errExport.value = '';
            loadUserDates();
        }
        function pickDate(ymd) {
            eDate.value = ymd;
            showCalendar.value = false;
            loadExport();
        }
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
            } catch(e) { errExport.value = '加载失败: ' + e.message; }
        }
        function gotoExport(uid, uname) {
            eUid.value = uid;
            eName.value = uname || '';
            tab.value = 'export';
            showCalendar.value = false;
            exportDates.value = [];
            exportList.value = [];
            errExport.value = '';
            loadUserDates();
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

        // ── LLM Config ──
        async function loadLlmConfig() {
            try {
                const res = await fetch('/api/llm_config', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                llmEnabled.value = data.enabled;
                llmProvider.value = data.provider;
                llmBaseUrl.value = data.base_url;
                llmModel.value = data.model;
                llmWakeWord.value = data.wake_word || 'ayabot';
                llmTemp.value = data.temperature ?? 0.7;
                llmTopP.value = data.top_p ?? 0.9;
                llmMaxTokens.value = data.max_tokens ?? 150;
                llmPrompt.value = data.system_prompt;
                if (data.has_api_key) {
                    llmApiKey.value = '********';
                }
                if (data.context) {
                    ctxEnabled.value = data.context.enabled;
                    ctxMode.value = data.context.mode;
                    ctxContent.value = data.context.content;
                    ctxMaxMsg.value = data.context.max_messages;
                }
            } catch(e) { /* ignore */ }
        }
        async function saveLlmConfig() {
            llmSaveMsg.value = '';
            const body = {
                enabled: llmEnabled.value,
                provider: llmProvider.value,
                base_url: llmBaseUrl.value,
                model: llmModel.value,
                wake_word: llmWakeWord.value,
                temperature: llmTemp.value,
                top_p: llmTopP.value,
                max_tokens: llmMaxTokens.value,
                system_prompt: llmPrompt.value,
                context: {
                    enabled: ctxEnabled.value,
                    mode: ctxMode.value,
                    content: ctxContent.value,
                    max_messages: ctxMaxMsg.value,
                },
            };
            if (llmApiKey.value && llmApiKey.value !== '********') {
                body.api_key = llmApiKey.value;
            }
            try {
                const res = await fetch('/api/llm_config', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                if (res.status === 401) {
                    loggedIn.value = false;
                    llmSaveMsg.value = '会话已过期，请重新登录';
                    llmSaveOk.value = false;
                    return;
                }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                if (data.ok) {
                    llmSaveMsg.value = '已保存';
                    llmSaveOk.value = true;
                    await loadLlmConfig();
                } else {
                    llmSaveMsg.value = data.error || '保存失败';
                    llmSaveOk.value = false;
                }
            } catch(e) {
                llmSaveMsg.value = '保存失败: ' + e.message;
                llmSaveOk.value = false;
            }
        }
        async function testLlm() {
            llmTestResp.value = '';
            if (!llmTestText.value) return;
            try {
                const res = await fetch('/api/llm_test', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({text: llmTestText.value}),
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) { llmTestResp.value = '测试失败'; return; }
                const data = await res.json();
                llmTestResp.value = data.reply || '(无回复)';
            } catch(e) {
                llmTestResp.value = '测试失败: ' + e.message;
            }
        }

        // 自动加载配置
        loadLlmConfig();
        loadGeneralConfig();

        // ── 动态标题 ──
        watch(botName, (name) => {
            document.title = (name || 'Ayabot') + ' 直播间机器人';
        }, { immediate: true });

        // ── General Config ──
        async function loadGeneralConfig() {
            try {
                const res = await fetch('/api/general_config', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                if (data.error) return;
                botName.value = data.bot_name || 'ayabot';
                cfgHost.value = data.web_ui?.host || '0.0.0.0';
                cfgPort.value = data.web_ui?.port || 8000;
                cfgRoomId.value = data.room_display_id || 0;
                cfgAnchorUid.value = data.anchor_uid || 0;
                cfgWelcomeCd.value = data.cooldown?.welcome_user_seconds ?? 600;
                cfgThanksCd.value = data.cooldown?.thanks_user_seconds ?? 10;
                cfgSendInterval.value = data.rate_limit?.send_interval_seconds ?? 1.2;
                cfgRetry.value = data.rate_limit?.retry_count ?? 2;
                cfgMaxQueue.value = data.rate_limit?.max_queue_size ?? 50;
                cfgReplyDelay.value = data.rate_limit?.reply_delay_seconds ?? 1.0;
                cfgWelcomeOn.value = data.features?.welcome_enabled ?? true;
                cfgThanksOn.value = data.features?.thanks_enabled ?? true;
                cfgBlindboxOn.value = data.features?.blindbox_enabled ?? true;
                cfgGuardOn.value = data.features?.guard_thanks_enabled ?? true;
                cfgConnectedMsg.value = data.features?.connected_message_enabled ?? false;
                cfgWelcomeTmpl.value = data.features?.welcome_template || '';
                cfgThanksTmpl.value = data.features?.thanks_template || '';
                cfgGuardCaptain.value = data.features?.guard_thanks_template_captain || '';
                cfgGuardCommander.value = data.features?.guard_thanks_template_commander || '';
                cfgGuardGovernor.value = data.features?.guard_thanks_template_governor || '';
                cfgGuardDefault.value = data.features?.guard_thanks_template_default || '';
                cfgConnMsg.value = data.features?.connected_message || '';
                cfgPeriodicOn.value = data.features?.periodic_message_enabled ?? true;
                cfgPeriodicInterval.value = data.features?.periodic_message_interval_seconds ?? 600;
                cfgPeriodicTmpl.value = data.features?.periodic_message_template || '';
            } catch(e) { /* ignore */ }
        }
        async function saveGeneralConfig() {
            cfgSaveMsg.value = '';
            try {
                const res = await fetch('/api/general_config', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({
                        bot_name: botName.value,
                        web_ui: {
                            host: cfgHost.value,
                            port: cfgPort.value,
                        },
                        cooldown: {
                            welcome_user_seconds: cfgWelcomeCd.value,
                            thanks_user_seconds: cfgThanksCd.value,
                        },
                        rate_limit: {
                            send_interval_seconds: cfgSendInterval.value,
                            retry_count: cfgRetry.value,
                            max_queue_size: cfgMaxQueue.value,
                            reply_delay_seconds: cfgReplyDelay.value,
                        },
                        features: {
                            welcome_enabled: cfgWelcomeOn.value,
                            welcome_template: cfgWelcomeTmpl.value,
                            thanks_enabled: cfgThanksOn.value,
                            thanks_template: cfgThanksTmpl.value,
                            blindbox_enabled: cfgBlindboxOn.value,
                            guard_thanks_enabled: cfgGuardOn.value,
                            guard_thanks_template_captain: cfgGuardCaptain.value,
                            guard_thanks_template_commander: cfgGuardCommander.value,
                            guard_thanks_template_governor: cfgGuardGovernor.value,
                            guard_thanks_template_default: cfgGuardDefault.value,
                            connected_message: cfgConnMsg.value,
                            connected_message_enabled: cfgConnectedMsg.value,
                            periodic_message_enabled: cfgPeriodicOn.value,
                            periodic_message_interval_seconds: cfgPeriodicInterval.value,
                            periodic_message_template: cfgPeriodicTmpl.value,
                        },
                    }),
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                if (data.ok) {
                    cfgSaveMsg.value = '已保存（重启后生效）';
                    cfgSaveOk.value = true;
                } else {
                    cfgSaveMsg.value = '保存失败';
                    cfgSaveOk.value = false;
                }
            } catch(e) {
                cfgSaveMsg.value = '保存失败: ' + e.message;
                cfgSaveOk.value = false;
            }
        }

        // ── B站 扫码登录 ──
        async function startBiliLogin() {
            biliLoginState.value = 'loading';
            biliLoginError.value = '';
            if (biliPollTimer) { clearInterval(biliPollTimer); biliPollTimer = null; }
            try {
                const res = await fetch('/api/bili_login/start', {
                    method: 'POST', credentials: 'include',
                });
                if (!res.ok) throw new Error('请求失败');
                const data = await res.json();
                if (!data.ok) throw new Error(data.error || '生成二维码失败');
                biliQrImage.value = data.qr_image;
                biliSessionId.value = data.session_id;
                biliLoginState.value = 'waiting';
                // 开始轮询
                biliPollTimer = setInterval(pollBiliLogin, 2000);
            } catch(e) {
                biliLoginError.value = e.message;
                biliLoginState.value = 'error';
            }
        }
        async function pollBiliLogin() {
            if (!biliSessionId.value) return;
            try {
                const res = await fetch('/api/bili_login/status?session_id=' + biliSessionId.value, {
                    credentials: 'include',
                });
                if (!res.ok) return;
                const data = await res.json();
                if (data.state === 'done') {
                    biliLoginState.value = 'done';
                    if (biliPollTimer) { clearInterval(biliPollTimer); biliPollTimer = null; }
                } else if (data.state === 'scanned') {
                    biliLoginState.value = 'scanned';
                } else if (data.state === 'timeout') {
                    biliLoginState.value = 'timeout';
                    if (biliPollTimer) { clearInterval(biliPollTimer); biliPollTimer = null; }
                } else if (data.state === 'error') {
                    biliLoginError.value = data.message || '登录失败';
                    biliLoginState.value = 'error';
                    if (biliPollTimer) { clearInterval(biliPollTimer); biliPollTimer = null; }
                }
            } catch(e) { /* ignore */ }
        }
        async function saveBiliLogin() {
            biliLoginState.value = 'saving';
            try {
                const res = await fetch('/api/bili_login/save', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({session_id: biliSessionId.value}),
                });
                if (!res.ok) throw new Error('保存失败');
                const data = await res.json();
                if (data.ok) {
                    // 服务正在重启，自动登出
                    setTimeout(() => { loggedIn.value = false; }, 2000);
                } else {
                    biliLoginError.value = data.error || '保存失败';
                    biliLoginState.value = 'error';
                }
            } catch(e) {
                biliLoginError.value = '保存失败: ' + e.message;
                biliLoginState.value = 'error';
            }
        }

        // ── Restart ──
        async function restartService() {
            restartMsg.value = '';
            restartOk.value = false;
            try {
                const res = await fetch('/api/restart', {
                    method: 'POST',
                    credentials: 'include',
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                const data = await res.json();
                if (data.ok) {
                    restartMsg.value = '服务正在重启...';
                    restartOk.value = true;
                    // 重启后自动登出（web 也重启了）
                    setTimeout(() => { loggedIn.value = false; }, 2000);
                } else {
                    restartMsg.value = data.error || '重启失败';
                    restartOk.value = false;
                }
            } catch(e) {
                restartMsg.value = '重启失败: ' + e.message;
                restartOk.value = false;
            }
        }

        return {loggedIn, loginUser, loginPass, loginErr, doLogin, doLogout,
                tab, rStart, rEnd, rType, ranking, errRanking, loadRanking,
                eUid, eName, eDate, eType, ePerCol, eColWidth, exportList, exportDates, exportCols, errExport,
                loadExport, gotoExport, loadUserDates, onUidInput, pickDate,
                showCalendar, calYear, calMonth, calDays, exportDatesSet,
                proxyImg, fmtTime, cardBgClass, guardLabel, guardBadgeClass,
                delDate, delResult, confirmDelete,
                llmEnabled, llmProvider, llmApiKey, llmBaseUrl, llmModel, llmPrompt,
                llmWakeWord, llmTemp, llmTopP, llmMaxTokens,
                llmSaveMsg, llmSaveOk, llmTestText, llmTestResp,
                ctxEnabled, ctxMode, ctxContent, ctxMaxMsg,
                saveLlmConfig, testLlm,
                cfgRoomId, cfgAnchorUid, cfgWelcomeCd, cfgThanksCd,
                cfgSendInterval, cfgRetry, cfgMaxQueue, cfgReplyDelay,
                cfgWelcomeOn, cfgThanksOn, cfgBlindboxOn, cfgGuardOn, cfgConnectedMsg,
                cfgWelcomeTmpl, cfgThanksTmpl, cfgGuardCaptain, cfgGuardCommander, cfgGuardGovernor, cfgGuardDefault, cfgConnMsg,
                cfgPeriodicOn, cfgPeriodicInterval, cfgPeriodicTmpl,
                botName, cfgHost, cfgPort,
                cfgSaveMsg, cfgSaveOk, loadGeneralConfig, saveGeneralConfig,
                restartMsg, restartOk, restartService,
                biliLoginState, biliQrImage, biliLoginError,
                startBiliLogin, saveBiliLogin};
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
