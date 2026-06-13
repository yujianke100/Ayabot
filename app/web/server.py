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
import uuid
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml
from fastapi import FastAPI, Request, Response as FastResponse
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

from bilibili_api import live

from app.config import DEFAULT_ROOMS_DIR

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
    global AUTH_USER, AUTH_PASS, _SESSION_TIMEOUT, _HTTP_HOST, _HTTP_PORT, _DB_PATH, _LLM_CONFIG_DICT, _CONFIG_YAML_PATH, _ROOMS_BASE_DIR
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
    # 房间管理的基础目录 = 项目根目录（rooms/ 所在位置）
    # 当 config 在 rooms/<id>/config.yaml 时，往上两级到项目根
    cfg_parent = Path(_CONFIG_YAML_PATH).parent if _CONFIG_YAML_PATH else Path.cwd()
    if cfg_parent.parent.name == DEFAULT_ROOMS_DIR:
        _ROOMS_BASE_DIR = str(cfg_parent.parent.parent.resolve())
    else:
        _ROOMS_BASE_DIR = str(cfg_parent.resolve())
    logger.info("webui configured: host=%s port=%s db=%s", _HTTP_HOST, _HTTP_PORT, os.path.abspath(_DB_PATH))

    # 同步 admin 凭据到 auth/users.json（确保 config.yaml 修改的密码也能登录）
    users = _load_users()
    if AUTH_USER not in users:
        users[AUTH_USER] = {"password": AUTH_PASS, "role": "admin", "rooms": []}
    elif users[AUTH_USER].get("password") != AUTH_PASS:
        users[AUTH_USER]["password"] = AUTH_PASS
    _save_users(users)


def _fallback_read_config() -> None:
    global _DB_PATH, AUTH_USER, AUTH_PASS
    _cfg_path = Path("config.yaml")
    if _cfg_path.exists():
        _raw = yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
        _DB_PATH = str(_raw.get("storage", {}).get("sqlite_path", "data/bot.db"))
        if not os.path.isabs(_DB_PATH):
            _DB_PATH = str(_cfg_path.parent / _DB_PATH)
        web_ui = _raw.get("web_ui", {}) or {}
        if web_ui.get("username"):
            AUTH_USER = web_ui["username"]
        if web_ui.get("password"):
            AUTH_PASS = web_ui["password"]
    logger.info("webui using db (fallback): %s", os.path.abspath(_DB_PATH))

    # 同步 admin 凭据到 auth/users.json
    users = _load_users()
    if AUTH_USER not in users:
        users[AUTH_USER] = {"password": AUTH_PASS, "role": "admin", "rooms": []}
    elif users[AUTH_USER].get("password") != AUTH_PASS:
        users[AUTH_USER]["password"] = AUTH_PASS
    _save_users(users)


app = FastAPI(title="BiliRobot Manager")

# ══════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════

_SESSIONS: dict[str, tuple[float, str, str, list]] = {}  # token -> (expiry, username, role, allowed_rooms)
_RATE_LIMIT: dict[str, list[float]] = {}  # ip -> [timestamps]
_AUTH_CONFIG_PATH: str = "auth/users.json"


def _load_users() -> dict:
    """加载用户配置. 返回 {username: {password, role, rooms}}"""
    p = Path(_ROOMS_BASE_DIR).resolve() / _AUTH_CONFIG_PATH
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_users(users: dict) -> None:
    p = Path(_ROOMS_BASE_DIR).resolve() / _AUTH_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _user_role(token: str) -> tuple[str, str, list]:
    """返回 (username, role, allowed_rooms) 或 (\"\", \"\", [])"""
    sess = _SESSIONS.get(token)
    if sess and sess[0] > time.time():
        return (sess[1], sess[2], sess[3])
    return ("", "", [])


def _check_auth(request: Request) -> bool:
    token = request.cookies.get("session")
    if token and token in _SESSIONS:
        sess = _SESSIONS[token]
        if sess[0] > time.time():
            return True
        else:
            _SESSIONS.pop(token, None)
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

    # 检查多用户配置
    users = _load_users()
    user_info = users.get(username)
    if user_info and user_info.get("password") == password:
        role = user_info.get("role", "admin")
        allowed_rooms = user_info.get("rooms", [])
    elif username == AUTH_USER and password == AUTH_PASS:
        # 兼容旧配置（config.yaml 中的账号密码）
        role = "admin"
        allowed_rooms = []
    else:
        return JSONResponse({"error": "wrong credentials"}, status_code=403)

    token = secrets.token_hex(32)
    _SESSIONS[token] = (time.time() + _SESSION_TIMEOUT, username, role, allowed_rooms)

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
#  用户管理 API
# ══════════════════════════════════════════════════════════════


@app.get("/api/users")
async def api_list_users():
    """列出所有用户."""
    return {"users": _load_users()}


@app.post("/api/users/update")
async def api_update_user(request: Request):
    """添加/修改用户（管理员或主播）."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    username = body.get("username", "").strip()
    if not username:
        return JSONResponse({"error": "username required"}, status_code=400)

    users = _load_users()
    password = body.get("password", "")
    role = body.get("role", "streamer")
    rooms = body.get("rooms", [])

    if password:
        users[username] = {"password": password, "role": role, "rooms": rooms}
    elif username in users:
        # 不修改密码，只更新角色和房间
        users[username]["role"] = role
        users[username]["rooms"] = rooms
    else:
        return JSONResponse({"error": "user not found and no password provided"}, status_code=400)

    _save_users(users)
    return {"ok": True}


@app.delete("/api/users/{username}")
async def api_delete_user(username: str):
    """删除用户."""
    users = _load_users()
    users.pop(username, None)
    _save_users(users)
    return {"ok": True}


@app.post("/api/users/admin_password")
async def api_set_admin_password(request: Request):
    """设置管理员密码（同步到 auth 配置和当前环境变量）."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    new_pass = body.get("password", "").strip()
    if len(new_pass) < 4:
        return JSONResponse({"error": "password too short"}, status_code=400)

    global AUTH_PASS
    users = _load_users()
    users["admin"] = {"password": new_pass, "role": "admin", "rooms": []}
    _save_users(users)
    AUTH_PASS = new_pass  # 立即生效
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
#  多房间管理 API
# ══════════════════════════════════════════════════════════════

_ROOMS_BASE_DIR: str = "."  # 由 init_app 设置


async def _resolve_room_id(anchor_uid: int) -> int:
    """通过 B站 API 从主播 UID 查询直播间号."""
    url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={anchor_uid}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url) as resp:
                data = await resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("room_id"):
            return int(data["data"]["room_id"])
        raise ValueError(f"B站 API 返回异常: {data}")
    except Exception as exc:
        raise ValueError(f"无法解析直播间号 (UID={anchor_uid}): {exc}") from exc


def _room_service_name(room_id: str) -> str:
    return f"bili-live-bot@{room_id}.service"


def _room_status(room_id: str) -> str:
    """查询 systemd 服务状态: running / stopped / not_found."""
    import subprocess as _sp
    try:
        r = _sp.run(
            ["systemctl", "is-active", _room_service_name(room_id)],
            capture_output=True, text=True, timeout=5,
        )
        out = r.stdout.strip()
        return "running" if out == "active" else out
    except Exception:
        return "unknown"


def _list_rooms_from_disk() -> list[dict[str, Any]]:
    """扫描 rooms/ 目录列出所有房间."""
    from app.config import DEFAULT_ROOMS_DIR
    base = Path(_ROOMS_BASE_DIR).resolve() / DEFAULT_ROOMS_DIR
    if not base.exists():
        return []
    rooms: list[dict[str, Any]] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        room_id = d.name
        cfg_path = d / "config.yaml"
        if not cfg_path.exists():
            continue
        # 读取关键信息
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
        rooms.append({
            "room_id": room_id,
            "anchor_uid": cfg.get("anchor_uid", 0),
            "room_display_id": cfg.get("room_display_id", 0),
            "bot_name": cfg.get("web_ui", {}).get("bot_name", ""),
            "status": _room_status(room_id),
            "port": cfg.get("web_ui", {}).get("port", 8000),
            "account_uid": cfg.get("account_uid", ""),
            "account_nick": "",
            "room_name": cfg.get("room_name", ""),
        })
    return rooms


@app.get("/api/rooms")
async def api_list_rooms(request: Request):
    """列出所有房间及状态."""
    rooms = _list_rooms_from_disk()
    # 补齐 account_nick
    accounts_map = {a["uid"]: a.get("nickname", "") for a in _list_accounts()}
    for r in rooms:
        if r.get("account_uid"):
            r["account_nick"] = accounts_map.get(str(r["account_uid"]), "")
    # 根据用户角色过滤
    token = request.cookies.get("session", "")
    _, role, allowed = _user_role(token)
    if role == "streamer" and allowed:
        rooms = [r for r in rooms if r["room_id"] in allowed]
    return {"rooms": rooms}


@app.post("/api/rooms")
async def api_create_room(request: Request):
    """新建房间: 传入 anchor_uid, 可选 room_display_id / bot_name / port."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    anchor_uid = body.get("anchor_uid")
    if not anchor_uid:
        return JSONResponse({"error": "anchor_uid is required"}, status_code=400)

    # 自动解析直播间号
    room_display_id = body.get("room_display_id")
    if not room_display_id:
        try:
            room_display_id = await _resolve_room_id(int(anchor_uid))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    room_id = str(room_display_id)
    from app.config import ensure_room_dirs, get_room_path
    room_dir = ensure_room_dirs(room_id, base_dir=_ROOMS_BASE_DIR)
    cfg_path = room_dir / "config.yaml"

    if cfg_path.exists():
        return JSONResponse({"error": f"房间 {room_id} 已存在"}, status_code=409)

    # 从根 config.yaml 复制模板
    import shutil
    root_cfg = Path(_ROOMS_BASE_DIR).resolve() / "config.yaml"
    alt_cfg = Path(_ROOMS_BASE_DIR).resolve() / "config.example.yaml"
    src = root_cfg if root_cfg.exists() else (alt_cfg if alt_cfg.exists() else None)
    if not src:
        return JSONResponse({"error": "根目录 config.yaml 不存在"}, status_code=500)

    shutil.copy2(str(src), str(cfg_path))

    # 写入房间配置
    port = body.get("port", 8000)
    bot_name = body.get("bot_name", f"文文{room_id[:4]}")
    from app.config import update_config_from_dict
    update_config_from_dict({
        "room_display_id": int(room_display_id),
        "anchor_uid": int(anchor_uid),
        "bot_name": bot_name,
        "web_ui": {"port": int(port)},
    }, str(cfg_path))

    logger.info("room created: id=%s uid=%s port=%s", room_id, anchor_uid, port)
    return {"ok": True, "room_id": room_id, "room_display_id": room_display_id}


@app.post("/api/rooms/{room_id}/start")
async def api_start_room(room_id: str):
    """启动房间 systemd 服务."""
    import subprocess as _sp
    svc = _room_service_name(room_id)
    r = _sp.run(["systemctl", "start", svc], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return JSONResponse({"error": f"启动失败: {r.stderr.strip()}"}, status_code=500)
    logger.info("room started: %s", room_id)
    return {"ok": True, "status": "running"}


@app.post("/api/rooms/{room_id}/stop")
async def api_stop_room(room_id: str):
    """停止房间 systemd 服务."""
    import subprocess as _sp
    svc = _room_service_name(room_id)
    _sp.run(["systemctl", "stop", svc], capture_output=True, text=True, timeout=15)
    logger.info("room stopped: %s", room_id)
    return {"ok": True, "status": "stopped"}


@app.post("/api/rooms/{room_id}/restart")
async def api_restart_room(room_id: str):
    """重启房间 systemd 服务."""
    import subprocess as _sp
    svc = _room_service_name(room_id)
    _sp.run(["systemctl", "restart", svc], capture_output=True, text=True, timeout=15)
    logger.info("room restarted: %s", room_id)
    return {"ok": True, "status": "running"}


@app.delete("/api/rooms/{room_id}")
async def api_delete_room(room_id: str):
    """删除房间（先停止，再删目录）."""
    import shutil
    import subprocess as _sp

    # 先停止
    svc = _room_service_name(room_id)
    _sp.run(["systemctl", "stop", svc], capture_output=True, text=True, timeout=15)
    _sp.run(["systemctl", "disable", svc], capture_output=True, text=True, timeout=15)

    # 删除目录
    from app.config import get_room_path
    room_dir = get_room_path(room_id, base_dir=_ROOMS_BASE_DIR)
    if room_dir.exists():
        shutil.rmtree(str(room_dir))
    logger.info("room deleted: %s", room_id)
    return {"ok": True}


@app.get("/api/rooms/{room_id}/config")
async def api_get_room_config(room_id: str):
    """获取房间配置."""
    from app.config import get_room_path
    cfg_path = get_room_path(room_id, base_dir=_ROOMS_BASE_DIR) / "config.yaml"
    if not cfg_path.exists():
        return JSONResponse({"error": "room not found"}, status_code=404)
    from app.config import load_config, config_to_dict
    try:
        cfg = load_config(str(cfg_path))
        return config_to_dict(cfg)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/rooms/{room_id}/config")
async def api_save_room_config(room_id: str, request: Request):
    """保存房间配置."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    from app.config import get_room_path
    cfg_path = get_room_path(room_id, base_dir=_ROOMS_BASE_DIR) / "config.yaml"
    if not cfg_path.exists():
        return JSONResponse({"error": "room not found"}, status_code=404)
    from app.config import update_config_from_dict
    ok = update_config_from_dict(body, str(cfg_path))
    return {"ok": ok}


# ══════════════════════════════════════════════════════════════
#  B站账号管理 API
# ══════════════════════════════════════════════════════════════

_ACCOUNTS_DIR: str = "accounts"


def _get_account_dir(uid: str) -> Path:
    return Path(_ROOMS_BASE_DIR).resolve() / _ACCOUNTS_DIR / str(uid)


def _list_accounts() -> list[dict[str, Any]]:
    """扫描 accounts/ 目录列出所有已登录 B站 账号."""
    base = Path(_ROOMS_BASE_DIR).resolve() / _ACCOUNTS_DIR
    if not base.exists():
        return []
    accounts: list[dict[str, Any]] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        uid = d.name
        cred_path = d / "credential.json"
        meta_path = d / "meta.yaml"
        meta = {}
        if meta_path.exists():
            try:
                meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
            except Exception:
                meta = {}
        has_cred = cred_path.exists()
        nick = meta.get("nickname", "") or meta.get("uname", "") or ""
        accounts.append({
            "uid": uid,
            "nickname": nick,
            "has_credential": has_cred,
            "linked_rooms": _get_account_rooms(uid),
        })
    return accounts


def _get_account_rooms(uid: str) -> list[dict[str, Any]]:
    """返回指定 B站 账号关联了哪些房间."""
    linked: list[dict[str, Any]] = []
    rooms_dir = Path(_ROOMS_BASE_DIR).resolve() / DEFAULT_ROOMS_DIR
    if not rooms_dir.exists():
        return linked
    for d in sorted(rooms_dir.iterdir()):
        if not d.is_dir():
            continue
        cfg_path = d / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        account_uid = cfg.get("account_uid", "")
        if str(account_uid) == str(uid):
            linked.append({
                "room_id": d.name,
                "bot_name": cfg.get("web_ui", {}).get("bot_name", ""),
            })
    return linked


@app.post("/api/bili_accounts")
async def api_bili_login_account(request: Request):
    """生成 B站 二维码用于登录账号（保存到 accounts/<uid>/credential.json）."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    account_uid = body.get("uid", "")
    # 不强制 UID — 如果没传，登录后自动从 credential 提取

    # 生成二维码
    try:
        from bilibili_api import login_v2  # noqa: PLC0415
    except Exception:
        return JSONResponse({"error": "bilibili_api not available"}, status_code=500)

    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()
    qr_pic = qr.get_qrcode_picture()
    session_id = str(uuid.uuid4())

    # 直接使用 bilibili_api 生成的二维码图片
    import base64  # noqa: PLC0415

    img_b64 = base64.b64encode(qr_pic.content).decode("ascii")

    _BILI_LOGIN_SESSIONS[session_id] = {
        "qr": qr,
        "created_at": time.time(),
        "state": "waiting",
        "target_uid": account_uid or "",
    }

    return {
        "ok": True,
        "session_id": session_id,
        "qr_image": f"data:image/png;base64,{img_b64}",
    }


@app.get("/api/bili_accounts")
async def api_list_accounts():
    """列出所有已登录的 B站 账号."""
    return {"accounts": _list_accounts()}


@app.post("/api/bili_accounts/save")
async def api_save_account_credential(request: Request):
    """保存扫码登录后的凭据到 accounts/<uid>/credential.json."""
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

    target_uid = sess.get("target_uid", "")
    if not target_uid:
        # 如果没传 UID，登录后从 credential 获取
        target_uid = ""

    try:
        credential = qr.get_credential()
    except Exception as exc:
        return {"ok": False, "error": f"get credential failed: {exc}"}

    cookies = credential.get_cookies()
    dedeuserid = cookies.get("DedeUserID", "")

    # 如果没传 UID，使用登录后的 DedeUserID
    if not target_uid and dedeuserid:
        target_uid = dedeuserid

    if not target_uid:
        return {"ok": False, "error": "no target uid"}

    # 保存到 accounts/<uid>/
    acc_dir = _get_account_dir(target_uid)
    acc_dir.mkdir(parents=True, exist_ok=True)

    cred_data = {
        "SESSDATA": cookies.get("SESSDATA", ""),
        "bili_jct": cookies.get("bili_jct", ""),
        "buvid3": cookies.get("buvid3", ""),
        "DedeUserID": dedeuserid,
        "ac_time_value": cookies.get("ac_time_value", ""),
    }
    (acc_dir / "credential.json").write_text(
        json.dumps(cred_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 查询用户昵称
    nickname = ""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess_aio:
            async with sess_aio.get(
                f"https://api.bilibili.com/x/space/wbi/acc/info?mid={dedeuserid}"
            ) as resp:
                info = await resp.json()
                if info.get("code") == 0:
                    nickname = info.get("data", {}).get("name", "")
    except Exception:
        pass

    if nickname:
        (acc_dir / "meta.yaml").write_text(
            yaml.dump({"nickname": nickname, "uname": nickname}, allow_unicode=True),
            encoding="utf-8",
        )

    _BILI_LOGIN_SESSIONS.pop(session_id, None)
    logger.info("bili account saved: uid=%s nickname=%s", dedeuserid, nickname)

    return {
        "ok": True,
        "uid": dedeuserid,
        "nickname": nickname,
    }


@app.get("/api/bili_accounts/status")
async def api_account_login_status(session_id: str):
    """查询账号二维码登录状态."""
    sess = _BILI_LOGIN_SESSIONS.get(session_id)
    if not sess:
        return {"state": "expired"}

    qr = sess["qr"]

    # 二维码硬超时：B站二维码有效期通常 180 秒，60 秒后已经不可靠
    import time as _time2
    elapsed = _time2.time() - sess.get("created_at", 0)
    QR_MAX_AGE = 60  # 60 秒后强制过期

    if elapsed >= QR_MAX_AGE:
        # 不删除 session，给前端留足够时间看到 "timeout"
        return {"state": "timeout"}

    try:
        if qr.has_done():
            return {"state": "done"}

        state = await qr.check_state()

        if state == login_v2.QrCodeLoginEvents.TIMEOUT:
            return {"state": "timeout"}
        # 注意：bilibili_api 的 SCAN = 86101 = 还没被扫（二维码有效等待扫码）
        # CONF = 86090 = 已扫码等待确认
        if state == login_v2.QrCodeLoginEvents.SCAN:
            return {"state": "waiting"}
        if state == login_v2.QrCodeLoginEvents.CONF:
            return {"state": "scanned"}

        return {"state": "waiting"}
    except Exception as exc:
        logger.warning("account login status error: %s", exc)
        return {"state": "error", "message": str(exc)}


@app.delete("/api/bili_accounts/{uid}")
async def api_delete_account(uid: str):
    """删除 B站 账号（解除所有房间的关联）."""
    import shutil  # noqa: PLC0415

    # 先解除所有房间的关联
    rooms_dir = Path(_ROOMS_BASE_DIR).resolve() / DEFAULT_ROOMS_DIR
    if rooms_dir.exists():
        for d in rooms_dir.iterdir():
            if not d.is_dir():
                continue
            cfg_path = d / "config.yaml"
            if not cfg_path.exists():
                continue
            try:
                cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if str(cfg.get("account_uid", "")) == str(uid):
                cfg.pop("account_uid", None)
                (d / "config.yaml").write_text(
                    yaml.dump(cfg, default_flow_style=False, allow_unicode=True),
                    encoding="utf-8",
                )
                logger.info("removed account %s from room %s", uid, d.name)

    # 删除账号目录
    acc_dir = _get_account_dir(uid)
    if acc_dir.exists():
        shutil.rmtree(str(acc_dir))

    logger.info("bili account deleted: %s", uid)
    return {"ok": True}


@app.post("/api/bili_accounts/{uid}/nickname")
async def api_update_account_nickname(uid: str, request: Request):
    """修改 B站 账号的显示名称."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    nickname = body.get("nickname", "").strip()
    if not nickname:
        return JSONResponse({"error": "nickname required"}, status_code=400)
    meta_path = _get_account_dir(uid) / "meta.yaml"
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except Exception:
        meta = {}
    meta["nickname"] = nickname
    meta_path.write_text(yaml.dump(meta, allow_unicode=True), encoding="utf-8")
    return {"ok": True, "nickname": nickname}


@app.post("/api/bili_accounts/{uid}/verify")
async def api_verify_account_credential(uid: str):
    """验证 B站 凭证是否仍然有效."""
    cred_path = _get_account_dir(uid) / "credential.json"
    if not cred_path.exists():
        return {"valid": False, "error": "no credential"}
    try:
        cred_data = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        return {"valid": False, "error": "corrupted credential"}
    try:
        from bilibili_api import Credential  # noqa: PLC0415
        credential = Credential(
            sessdata=cred_data.get("SESSDATA", ""),
            bili_jct=cred_data.get("bili_jct", ""),
            buvid3=cred_data.get("buvid3", ""),
            dedeuserid=cred_data.get("DedeUserID", ""),
            ac_time_value=cred_data.get("ac_time_value", ""),
        )
        ok = await credential.check_valid()
        return {"valid": ok}
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


# ══════════════════════════════════════════════════════════════
#  Per-Room 数据 API
# ══════════════════════════════════════════════════════════════

def _room_db_path(room_id: str) -> Path:
    """返回房间的 SQLite 数据库路径."""
    from app.config import get_room_path
    return get_room_path(room_id, base_dir=_ROOMS_BASE_DIR) / "data" / "bot.db"


@app.get("/api/rooms/{room_id}/ranking")
async def api_room_ranking(room_id: str, rStart: str = "", rEnd: str = "", rType: str = "all"):
    """获取指定房间的送礼排行."""
    db_path = _room_db_path(room_id)
    if not db_path.exists():
        return {"ranking": []}

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        where_clauses = ["1=1"]
        params: list[Any] = []

        if rStart:
            where_clauses.append("date(ts, 'unixepoch') >= date(?)")
            params.append(rStart)
        if rEnd:
            where_clauses.append("date(ts, 'unixepoch') <= date(?)")
            params.append(rEnd)

        if rType == "gift":
            where_clauses.append("is_blind_box = 0")
        elif rType == "blindbox":
            where_clauses.append("is_blind_box = 1")

        where_sql = " AND ".join(where_clauses)
        if where_clauses:
            where_sql += " AND event_type = 'SEND_GIFT'"
        else:
            where_sql = "event_type = 'SEND_GIFT'"

        cur.execute(f"""
            SELECT uid, uname,
                   ROUND(SUM(CASE WHEN is_blind_box=1 THEN CAST(actual_value AS REAL) / 10.0 ELSE CAST(json_extract(raw_json, '$.total_coin') AS REAL) / 1000.0 END), 2) as total,
                   ROUND(SUM(CAST(profit_value AS REAL) / 10.0), 2) as total_profit
            FROM gift_events WHERE {where_sql}
            GROUP BY uid ORDER BY total DESC LIMIT 50
        """, params)
        rows = cur.fetchall()
        conn.close()

        ranking = [
            {"uid": r[0], "uname": r[1], "count": 0, "total": r[2] or 0, "total_profit": r[3] or 0}
            for r in rows
        ]
        return {"ranking": ranking}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/rooms/{room_id}/user_gifts")
async def api_room_user_gifts(room_id: str, uid: int = 0, date: str = "", gift_type: str = "all"):
    """获取指定房间某用户某天的送礼详情."""
    db_path = _room_db_path(room_id)
    if not db_path.exists() or not uid or not date:
        return []

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        where_extra = ""
        if gift_type == "gift":
            where_extra = " AND is_blind_box = 0"
        elif gift_type == "blindbox":
            where_extra = " AND is_blind_box = 1"

        cur.execute(f"""
            SELECT uid, uname, gift_name, gift_num, actual_value, ts, is_blind_box, id, raw_json
            FROM gift_events
            WHERE uid = ? AND date(ts, 'unixepoch') = date(?) AND event_type = 'SEND_GIFT'{where_extra}
            ORDER BY ts
        """, (uid, date))
        rows = cur.fetchall()
        conn.close()

        import json as _json
        results = []
        # 合并字典：key = (batch_combo_id, gift_name) → merged entry
        merged = {}
        for r in rows:
            raw = {}
            if r[8]:
                try:
                    raw = _json.loads(r[8])
                except Exception:
                    raw = {}
            # 从 raw_json 解析实际价格、舰长等级、头像、礼物图标、batch_combo_id
            guard_level = 0
            avatar = ""
            gift_icon = ""
            actual_value = r[4]  # DB actual_value (for blindbox = item value)
            is_blind = bool(r[6])

            if not is_blind and raw:
                # 一般礼物：total_coin 单位是 1000=1元
                actual_value = round(int(raw.get("total_coin", 0) or 0) / 1000.0, 2)
            else:
                # 盲盒：DB actual_value 单位是角，/10 转元
                actual_value = round(actual_value / 10.0, 2)

            if raw:
                sender_info = raw.get("sender_uinfo", {}) or {}
                base = sender_info.get("base", {}) or {}
                avatar = base.get("face", "") or ""
                guard_info = sender_info.get("guard", {}) or None
                if guard_info:
                    guard_level = guard_info.get("level", 0) or 0
                medal = sender_info.get("medal", {}) or None
                if medal and not guard_level:
                    guard_level = medal.get("guard_level", 0) or 0
                gift_info = raw.get("gift_info", {}) or {}
                gift_icon = gift_info.get("img_basic", "") or gift_info.get("webp", "") or ""

            # 合并 key：batch_combo_id（短时间相同礼物合并）
            batch_key = r[8] and _json.loads(r[8]).get("batch_combo_id", "") or ""
            merge_key = (batch_key, r[2])

            if merge_key in merged:
                merged[merge_key]["gift_num"] += r[3]
                merged[merge_key]["actual_value"] += actual_value
            else:
                merged[merge_key] = {
                    "uid": r[0], "uname": r[1], "gift_name": r[2],
                    "gift_num": r[3], "actual_value": actual_value,
                    "ts": r[5], "is_blind_box": is_blind,
                    "id": r[7],
                    "guard_level": guard_level,
                    "avatar": avatar,
                    "gift_icon": gift_icon,
                }

        results = list(merged.values())
        return results
    except Exception:
        return []


@app.get("/api/rooms/{room_id}/user_dates")
async def api_room_user_dates(room_id: str, uid: int = 0):
    """获取指定房间某用户有送礼记录的所有日期."""
    db_path = _room_db_path(room_id)
    if not db_path.exists() or not uid:
        return []

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT date(ts, 'unixepoch') FROM gift_events WHERE uid = ? ORDER BY date(ts, 'unixepoch')",
            (uid,),
        )
        dates = [r[0] for r in cur.fetchall()]
        conn.close()
        return dates
    except Exception:
        return []


@app.post("/api/rooms/{room_id}/delete_old")
async def api_room_delete_old(room_id: str, request: Request):
    """删除指定房间的旧数据."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    date_str = body.get("date", "")
    if not date_str:
        return JSONResponse({"error": "date required"}, status_code=400)

    db_path = _room_db_path(room_id)
    if not db_path.exists():
        return {"deleted_events": 0, "deleted_gifts": 0}

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("DELETE FROM gift_events WHERE date(ts, 'unixepoch') <= date(?)", (date_str,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return {"deleted_events": 0, "deleted_gifts": deleted}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
        <h1 class="text-xl font-bold text-center text-blue-600 mb-6">Ayabot 管理后台</h1>
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
    <h1 class="text-xl font-bold text-blue-600">🎯 Ayabot 管理后台</h1>
    <div class="flex items-center gap-4 text-sm">
        <button @click="tab='accounts'" :class="tab==='accounts'?'text-blue-600 font-bold border-b-2 border-blue-600':''">🤖 机器人B站账号</button>
        <button @click="tab='rooms'" :class="tab==='rooms'?'text-blue-600 font-bold border-b-2 border-blue-600':''">🏠 房间管理</button>
        <button @click="tab='global'" :class="tab==='global'?'text-blue-600 font-bold border-b-2 border-blue-600':''">⚙️ 全局配置</button>
        <button @click="tab='help'"  :class="tab==='help' ?'text-blue-600 font-bold border-b-2 border-blue-600':''">帮助</button>
        <button @click="doLogout" class="text-gray-400 hover:text-red-500 ml-2">退出</button>
    </div>
</header>

<!-- ══════ 房间管理 ══════ -->
<div v-if="tab==='rooms'" class="max-w-5xl mx-auto">

    <!-- ── 房间列表 ── -->
    <div v-if="!selectedRoom">
        <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
            <div class="flex items-center justify-between">
                <h2 class="text-lg font-bold">🏠 房间管理</h2>
                <button @click="toggleCreateRoom"
                        class="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded text-sm">
                    {{ showCreateRoom ? '取消' : '➕ 新建房间' }}
                </button>
            </div>

            <!-- 新建表单 -->
            <div v-if="showCreateRoom" class="border rounded-lg p-4 bg-gray-50 space-y-3">
                <h3 class="text-sm font-bold">新建直播间</h3>
                <div class="grid grid-cols-2 gap-3">
                    <label class="text-xs text-gray-500">主播 UID
                        <input type="number" v-model.number="newRoomUid" placeholder="B站 主播 UID"
                               class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">关联 B站 账号
                        <select v-model="newRoomAccount" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="">暂不关联</option>
                            <option v-for="a in accounts" :key="a.uid" :value="a.uid">{{ a.nickname || 'UID:'+a.uid }}</option>
                        </select>
                    </label>
                </div>
                <div class="flex gap-2">
                    <button @click="createRoom" :disabled="creatingRoom"
                            class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                        {{ creatingRoom ? '创建中...' : '创建' }}
                    </button>
                    <div v-if="createRoomMsg" class="text-sm" :class="createRoomOk ? 'text-green-600' : 'text-red-500'">
                        {{ createRoomMsg }}
                    </div>
                </div>
            </div>

            <!-- 房间卡片列表 -->
            <div v-if="!rooms || rooms.length === 0" class="text-sm text-gray-400 text-center py-8">
                暂无房间。点击「新建房间」添加。
            </div>
            <div v-for="r in rooms" :key="r.room_id"
                 class="border rounded-lg p-4 flex items-center justify-between cursor-pointer hover:shadow-md transition"
                 :class="r.status==='running' ? 'border-green-300 bg-green-50' : 'border-gray-200'"
                 @click="selectRoom(r)">
                <div class="flex-1">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full inline-block"
                              :class="r.status==='running' ? 'bg-green-500' : 'bg-gray-400'"></span>
                        <span class="font-bold text-sm">{{ r.room_name || ('#'+r.room_id) }}</span>
                        <span v-if="r.room_name" class="text-xs text-gray-400">#{{ r.room_id }}</span>
                        <span class="text-xs text-gray-400 ml-1">UID: {{ r.anchor_uid }}</span>
                    </div>
                    <div class="text-xs text-gray-500 mt-1 flex gap-3">
                        <span>状态: {{ r.status === 'running' ? '🟢 运行中' : '⏹️ 已停止' }}</span>
                        <span v-if="r.account_nick">账号: {{ r.account_nick }}</span>
                    </div>
                </div>
                <div class="flex gap-2 items-center" @click.stop>
                    <button v-if="r.status !== 'running'"
                            @click="startRoom(r.room_id)"
                            class="bg-green-500 hover:bg-green-600 text-white px-3 py-1 rounded text-xs">启动</button>
                    <button v-if="r.status === 'running'"
                            @click="stopRoom(r.room_id)"
                            class="bg-yellow-500 hover:bg-yellow-600 text-white px-3 py-1 rounded text-xs">停止</button>
                    <button @click="deleteRoom(r.room_id, r.room_id)"
                            class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ── 房间详情 ── -->
    <div v-else>
        <div class="mb-4">
            <button @click="selectedRoom = null; roomSubTab = 'ranking'"
                    class="text-blue-500 hover:text-blue-700 text-sm">&larr; 返回房间列表</button>
        </div>

        <!-- 房间信息头 -->
        <div class="bg-white p-4 rounded-xl shadow-sm mb-4 flex items-center justify-between">
            <div>
                <h2 class="text-lg font-bold">📺
                    <template v-if="editingRoomName">
                        <input type="text" v-model="roomNameEdit" class="border p-1 rounded text-sm w-40" @keyup.enter="saveRoomName" @keyup.escape="editingRoomName=false">
                        <button @click="saveRoomName" class="text-blue-500 text-xs ml-1">保存</button>
                        <button @click="editingRoomName=false" class="text-gray-400 text-xs ml-1">取消</button>
                    </template>
                    <template v-else>
                        {{ selectedRoom.room_name || ('房间 #'+selectedRoom.room_id) }}
                        <button @click="startEditRoomName" class="text-gray-400 hover:text-blue-500 text-xs ml-1">✏️</button>
                    </template>
                </h2>
                <div class="text-xs text-gray-500 mt-1">
                    主播 UID: {{ selectedRoom.anchor_uid }}
                    | 状态: {{ selectedRoom.status === 'running' ? '🟢 运行中' : '⏹️ 已停止' }}
                </div>
            </div>
            <div class="flex items-center gap-2">
                <span class="text-xs text-gray-500">B站账号:</span>
                <select v-model="selectedRoomAccount" class="border p-1 rounded text-sm">
                    <option value="">不关联</option>
                    <option v-for="a in accounts" :key="a.uid" :value="a.uid">{{ a.nickname || 'UID:'+a.uid }}</option>
                </select>
                <button @click="assignAccountAndRestart" class="bg-green-600 hover:bg-green-700 text-white px-2 py-0.5 rounded text-xs" :disabled="accountRestarting">{{ accountRestarting ? '重启中...' : '✅ 保存并重启' }}</button>
                <span v-if="accountAssignMsg" class="text-xs" :class="accountAssignOk ? 'text-green-600' : 'text-red-500'">{{ accountAssignMsg }}</span>
            </div>
        </div>

        <!-- 子导航 -->
        <div class="flex gap-1 mb-4 text-sm bg-white rounded-xl shadow-sm p-1">
            <button v-for="st in roomSubTabs" :key="st.key"
                    @click="selectRoomSubTab(st.key)"
                    class="px-4 py-2 rounded-lg transition"
                    :class="roomSubTab === st.key ? 'bg-blue-500 text-white' : 'text-gray-600 hover:bg-gray-100'">
                {{ st.label }}
            </button>
        </div>

        <!-- 送礼排行 -->
        <div v-if="roomSubTab==='ranking'" class="grid grid-cols-1 lg:grid-cols-2 gap-6">
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

        <!-- 精美导出 -->
        <div v-if="roomSubTab==='export'" class="flex flex-col items-center w-full">
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
                                    <span class="gift-value" v-if="item.actual_value">¥{{ Number(item.actual_value).toFixed(1) }}</span>
                                    <span class="text-[10px] text-white/60" v-if="item.actual_value && item.gift_num > 1">(¥{{ Number(item.actual_value / item.gift_num).toFixed(2) }}/个)</span>
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

        <!-- AI 回复 -->
        <div v-if="roomSubTab==='llm'" class="max-w-2xl mx-auto">
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

        <!-- 机器人配置 -->
        <div v-if="roomSubTab==='config'" class="max-w-2xl mx-auto">
            <div v-if="roomConfig" class="bg-white p-6 rounded-xl shadow-sm space-y-4">
                <h2 class="text-lg font-bold">⚙️ 机器人配置</h2>
                <p class="text-xs text-gray-500">配置保存后需重启对应房间服务才能生效。</p>

                <div class="grid grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">主播 UID
                        <input type="number" v-model.number="roomConfig.anchor_uid" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">⏱️ 冷却时间（秒）</h3>
                <div class="grid grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">欢迎同用户间隔
                        <input type="number" v-model.number="roomConfig.cooldown.welcome_user_seconds" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">感谢同用户间隔
                        <input type="number" v-model.number="roomConfig.cooldown.thanks_user_seconds" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">🚦 限流</h3>
                <div class="grid grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">弹幕发送间隔(秒)
                        <input type="number" v-model="roomConfig.rate_limit.send_interval_seconds" step="0.1" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">重试次数
                        <input type="number" v-model.number="roomConfig.rate_limit.retry_count" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">队列上限
                        <input type="number" v-model.number="roomConfig.rate_limit.max_queue_size" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">回复延迟(秒)
                        <input type="number" v-model="roomConfig.rate_limit.reply_delay_seconds" step="0.1" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">🎛️ 功能开关</h3>
                <div class="grid grid-cols-2 gap-2 text-sm">
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.welcome_enabled" class="w-4 h-4"> 欢迎</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.thanks_enabled" class="w-4 h-4"> 感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.blindbox_enabled" class="w-4 h-4"> 盲盒统计</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.guard_thanks_enabled" class="w-4 h-4"> 大航海感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.connected_message_enabled" class="w-4 h-4"> 连接消息</label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">📝 回复模板</h3>
                <div class="space-y-3">
                    <label class="text-xs text-gray-500 block">欢迎模板
                        <input type="text" v-model="roomConfig.features.welcome_template" placeholder="欢迎{uname}来到直播间" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">感谢模板
                        <input type="text" v-model="roomConfig.features.thanks_template" placeholder="感谢{uname}的{gift_name}x{gift_num}!" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">大航海 - 舰长
                        <input type="text" v-model="roomConfig.features.guard_thanks_template_captain" placeholder="感谢{uname}上舰！" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">大航海 - 提督
                        <input type="text" v-model="roomConfig.features.guard_thanks_template_commander" placeholder="感谢{uname}支持！" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">大航海 - 总督
                        <input type="text" v-model="roomConfig.features.guard_thanks_template_governor" placeholder="感谢{uname}支持！" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">大航海 - 默认
                        <input type="text" v-model="roomConfig.features.guard_thanks_template_default" placeholder="感谢{uname}开通大航海！" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">连接消息
                        <input type="text" v-model="roomConfig.features.connected_message" placeholder="来了喵~" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">⏰ 定时消息</h3>
                <div class="space-y-3">
                    <label class="flex items-center gap-2 text-sm">
                        <input type="checkbox" v-model="roomConfig.features.periodic_message_enabled" class="w-4 h-4">
                        启用定时消息（仅在开播时发送）
                    </label>
                    <div class="grid grid-cols-2 gap-4">
                        <label class="text-xs text-gray-500">间隔（秒）
                            <input type="number" v-model.number="roomConfig.features.periodic_message_interval_seconds" min="30" max="86400" class="border p-2 rounded w-full text-sm mt-1">
                            <span class="text-xs text-gray-400">默认 600 秒（10 分钟）</span>
                        </label>
                    </div>
                    <label class="text-xs text-gray-500 block">消息内容
                        <input type="text" v-model="roomConfig.features.periodic_message_template" placeholder="欢迎关注直播间~点个关注不迷路！" class="border p-2 rounded w-full text-sm mt-1">
                        <span class="text-xs text-gray-400">留空则不发送定时消息</span>
                    </label>
                </div>

                <div class="flex items-center gap-4 mt-4">
                    <button @click="saveRoomConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
                    <span v-if="roomSaveMsg" class="text-sm" :class="roomSaveOk ? 'text-green-600' : 'text-red-500'">{{ roomSaveMsg }}</span>
                    <button @click="editRoomConfig(selectedRoom?.room_id)" class="text-gray-500 hover:text-gray-700 underline text-sm">刷新</button>
                </div>
            </div>
        </div>

        <!-- 数据管理 -->
        <div v-if="roomSubTab==='manage'" class="max-w-lg mx-auto bg-white p-6 rounded-xl shadow-sm">
            <h2 class="text-lg font-bold mb-4 text-red-600">⚠️ 数据管理</h2>
            <p class="text-sm text-gray-500 mb-4">注意：删除操作不可恢复。</p>
            <div class="flex gap-2 items-end">
                <label class="text-xs text-gray-500 flex-1">删除此日期之前的数据<input type="date" v-model="delDate" class="border p-2 rounded w-full text-sm mt-1"></label>
                <button @click="confirmDelete" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded text-sm h-[38px]">删除</button>
            </div>
            <div v-if="delResult" class="mt-4 text-sm">{{ delResult }}</div>
        </div>
    </div>
</div>

<!-- ══════ B站账号管理 ══════ -->
<div v-if="tab==='accounts'" class="max-w-3xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
        <div class="flex items-center justify-between">
            <h2 class="text-lg font-bold">🤖 机器人B站账号</h2>
            <button @click="toggleNewAccount"
                    class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                {{ showNewAccount ? '取消' : '📱 扫码登录' }}
            </button>
        </div>

        <!-- 扫码登录 -->
        <div v-if="showNewAccount" class="border rounded-lg p-4 bg-gray-50 space-y-3">
            <h3 class="text-sm font-bold">扫码登录 B站 账号</h3>
            <p class="text-xs text-gray-500">用 B站 App 扫描二维码即可登录，系统自动识别账号。</p>
            <button @click="startAccountLogin" :disabled="accountLoggingIn"
                    class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                {{ accountLoggingIn ? '请稍候...' : '生成二维码' }}
            </button>

            <div v-if="accountQrImage" class="flex flex-col items-center space-y-2">
                <img :src="accountQrImage" class="border-2 border-gray-200 rounded-lg" style="width:180px;height:180px">
                <div class="flex items-center gap-2">
                    <span class="inline-block w-3 h-3 rounded-full bg-green-400 animate-pulse"></span>
                    <span class="text-sm">请用 B站 App 扫码</span>
                </div>
                <div v-if="accountQrState === 'scanned'" class="text-yellow-600 text-sm">
                    已扫码，请在手机上确认
                </div>
                <div v-if="accountQrState === 'done'" class="text-green-600 text-sm font-bold">
                    ✅ 登录成功，即将返回...
                </div>
                <div v-if="accountQrState === 'timeout' || accountQrState === 'expired'" class="text-red-500 text-sm">
                    ⏰ 二维码已过期
                </div>
                <div v-if="accountQrState === 'error'" class="text-red-500 text-sm">
                    {{ accountQrError }}
                </div>
                <div v-if="accountQrState === 'waiting' || accountQrState === 'scanned' || accountQrState === 'timeout' || accountQrState === 'expired'" class="flex gap-2 justify-center">
                    <button @click="refreshQrCode" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-1.5 rounded text-sm">🔄 刷新二维码</button>
                </div>
            </div>
        </div>

        <!-- 账号列表 -->
        <div v-if="!accounts || accounts.length === 0" class="text-sm text-gray-400 text-center py-8">
            暂无已登录的 B站 账号。
        </div>
        <div v-for="a in accounts" :key="a.uid"
             class="border rounded-lg p-4 flex items-center justify-between">
            <div>
                <div class="font-bold text-sm">
                    <template v-if="editingNickname === a.uid">
                        <input type="text" v-model="a.editNick" class="border p-1 rounded text-sm w-32" @keyup.enter="saveNickname(a)">
                        <button @click="saveNickname(a)" class="text-blue-500 text-xs ml-1">保存</button>
                        <button @click="editingNickname = null" class="text-gray-400 text-xs ml-1">取消</button>
                    </template>
                    <template v-else>
                        <span @click="startEditNickname(a)" class="cursor-pointer hover:text-blue-600">{{ a.nickname || '未命名' }}</span>
                        <button @click="startEditNickname(a)" class="text-gray-400 hover:text-blue-500 text-xs ml-1">✏️</button>
                    </template>
                </div>
                <div class="text-xs text-gray-500 mt-1">UID: {{ a.uid }}</div>
                <div v-if="a.linked_rooms && a.linked_rooms.length" class="text-xs text-gray-400 mt-1">
                    关联房间: <span v-for="(lr, li) in a.linked_rooms" :key="lr.room_id">{{ lr.room_id }}<span v-if="li < a.linked_rooms.length-1">, </span></span>
                </div>
                <div v-else class="text-xs text-gray-400 mt-1">未关联房间（可在房间详情设置中绑定）</div>
            </div>
            <div class="flex gap-2 items-center">
                <button @click="verifyAccount(a.uid)" :disabled="a.verifying" class="text-green-500 hover:text-green-700 text-xs underline">
                    {{ a.verifying ? '验证中...' : (a.credential_ok === true ? '✅ 有效' : (a.credential_ok === false ? '❌ 失效' : '验证')) }}
                </button>
                <button @click="deleteAccount(a.uid, a.nickname)"
                        class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
            </div>
        </div>
    </div>
</div>

<!-- ══════ 全局配置 ══════ -->
<div v-if="tab==='global'" class="max-w-2xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
        <h2 class="text-lg font-bold">⚙️ 全局配置</h2>
        <div class="grid grid-cols-2 gap-4">
            <label class="text-xs text-gray-500">Web 端口
                <input type="number" v-model.number="cfgPort" min="1024" max="65535" class="border p-2 rounded w-full text-sm mt-1">
            </label>
            <label class="text-xs text-gray-500">监听地址
                <input type="text" v-model="cfgHost" class="border p-2 rounded w-full text-sm mt-1" placeholder="0.0.0.0">
            </label>
        </div>
        <div class="flex items-center gap-4">
            <button @click="saveGlobalConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
            <span v-if="cfgSaveMsg" class="text-sm" :class="cfgSaveOk ? 'text-green-600' : 'text-red-500'">{{ cfgSaveMsg }}</span>
        </div>
    </div>

    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4 mt-4">
        <h2 class="text-lg font-bold">👥 账号管理</h2>
        <p class="text-xs text-gray-500">管理员可查看所有房间。主播只能看到自己被分配的直播间。</p>

        <!-- 管理员密码 -->
        <div class="border rounded-lg p-4 bg-gray-50 space-y-3">
            <h3 class="text-sm font-bold">管理员密码</h3>
            <div class="flex gap-2 items-end">
                <label class="text-xs text-gray-500 flex-1">新密码
                    <input type="password" v-model="adminPass" placeholder="留空不修改" class="border p-2 rounded w-full text-sm mt-1">
                </label>
                <button @click="saveAdminPass" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm h-[38px]">保存</button>
            </div>
            <div v-if="adminPassMsg" class="text-sm" :class="adminPassOk ? 'text-green-600' : 'text-red-500'">{{ adminPassMsg }}</div>
        </div>

        <!-- 主播账号列表 -->
        <div class="space-y-2">
            <div class="flex items-center justify-between">
                <h3 class="text-sm font-bold">主播账号</h3>
                <button @click="showStreamerForm = 'add'" class="text-blue-500 hover:text-blue-700 text-xs underline">➕ 添加主播</button>
            </div>

            <div v-if="showStreamerForm" class="border rounded-lg p-3 bg-gray-50 space-y-2">
                <h4 class="text-sm font-bold">{{ showStreamerForm === 'add' ? '添加主播' : '编辑主播' }}</h4>
                <div class="grid grid-cols-2 gap-2">
                    <label class="text-xs text-gray-500">用户名
                        <input type="text" v-model="editStreamerUser" :disabled="showStreamerForm==='edit'" class="border p-1 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">密码
                        <input type="password" v-model="editStreamerPass" :placeholder="showStreamerForm==='edit' ? '留空不修改' : ''" class="border p-1 rounded w-full text-sm mt-1">
                    </label>
                </div>
                <label class="text-xs text-gray-500">可查看的房间</label>
                <div class="relative">
                    <button @click="showRoomDropdown = !showRoomDropdown"
                            class="border rounded w-full text-sm mt-1 p-2 text-left bg-white flex items-center justify-between">
                        <span v-if="editStreamerRooms.length === 0" class="text-gray-400">选择房间...</span>
                        <span v-else class="text-gray-700">{{ editStreamerRooms.length }} 个房间已选</span>
                        <span class="text-gray-400">▼</span>
                    </button>
                    <div v-if="showRoomDropdown" class="absolute z-50 mt-1 bg-white border rounded shadow-lg w-full max-h-48 overflow-y-auto">
                        <div v-for="r in allRooms" :key="r.room_id"
                             class="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 cursor-pointer text-sm"
                             @click="toggleStreamerRoom(r.room_id)">
                            <input type="checkbox" :checked="editStreamerRooms.includes(r.room_id)" class="w-3.5 h-3.5">
                            <span>{{ r.room_name || ('#'+r.room_id) }}</span>
                        </div>
                        <div v-if="!allRooms.length" class="text-xs text-gray-400 px-3 py-2">暂无房间</div>
                    </div>
                </div>
                <div class="flex gap-2 mt-2">
                    <button @click="saveStreamer" :disabled="savingStreamer"
                            class="bg-green-500 hover:bg-green-600 text-white px-3 py-1 rounded text-sm">
                        {{ savingStreamer ? '保存中...' : (showStreamerForm === 'add' ? '添加' : '保存') }}
                    </button>
                    <button @click="showStreamerForm = null" class="bg-gray-300 hover:bg-gray-400 px-3 py-1 rounded text-sm">取消</button>
                </div>
                <div v-if="streamerMsg" class="text-sm" :class="streamerOk ? 'text-green-600' : 'text-red-500'">{{ streamerMsg }}</div>
            </div>

            <div v-if="!streamers || streamers.length === 0" class="text-sm text-gray-400 text-center py-4">
                暂无主播账号。
            </div>
            <div v-for="s in streamers" :key="s.username"
                 class="border rounded p-3 flex items-center justify-between">
                <div>
                    <div class="font-bold text-sm">{{ s.username }}</div>
                    <div class="text-xs text-gray-500">可查看: {{ s.rooms?.join(', ') || '无' }}</div>
                </div>
                <div class="flex gap-2">
                    <button @click="editStreamer(s)" class="text-blue-500 hover:text-blue-700 text-xs underline">编辑</button>
                    <button @click="deleteStreamer(s.username)" class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
                </div>
            </div>
        </div>
    </div>
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
                    <tr><td class="border p-1"><code>#{{ llmWakeWord || 'ayabot' }} &lt;聊天内容&gt;</code></td><td class="border p-1">AI 智能回复</td></tr>
                    <tr><td class="border p-1"><code>#帮助</code></td><td class="border p-1">显示所有命令</td></tr>
                </tbody>
            </table>
        </div>
        <div>
            <h3 class="font-bold text-blue-600 mb-1">🤖 功能</h3>
            <ul class="list-disc pl-4 space-y-1 text-xs">
                <li>欢迎 — 新观众进入时自动欢迎</li>
                <li>感谢 — 送礼物/盲盒时自动感谢</li>
                <li>大航海感谢 — 舰长/提督/总督自动感谢</li>
                <li>关键词回复 — 设定关键词自动回复</li>
                <li>AI 回复 — 唤醒词触发 LLM 智能对话</li>
                <li>多房间 — 一个 WebUI 管理多个主播</li>
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
        const tab = ref('rooms');

        // Room detail
        const selectedRoom = ref(null);
        const roomSubTab = ref('ranking');
        const roomSubTabs = [
            {key: 'ranking', label: '送礼排行'},
            {key: 'export', label: '精美导出'},
            {key: 'llm', label: 'AI回复'},
            {key: 'config', label: '机器人配置'},
            {key: 'manage', label: '数据管理'},
        ];
        const newRoomAccount = ref('');
        const selectedRoomAccount = ref('');
        const accountAssignMsg = ref('');
        const accountAssignOk = ref(false);
        const accountRestarting = ref(false);

        // Accounts management
        const accounts = ref([]);
        const showNewAccount = ref(false);
        const newAccountUid = ref(0);
        const accountLoggingIn = ref(false);
        const accountQrImage = ref('');
        const accountQrState = ref('idle');
        const accountQrError = ref('');
        const accountSessionId = ref('');
        let accountsPollTimer = null;

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

        // Room Management
        const rooms = ref([]);
        const showCreateRoom = ref(false);
        const newRoomUid = ref(0);
        const newRoomName = ref('');
        const newRoomPort = ref(8001);
        const newRoomDisplayId = ref(0);
        const creatingRoom = ref(false);
        const createRoomMsg = ref('');
        const createRoomOk = ref(false);
        const editingRoom = ref(null);
        const roomConfig = ref(null);
        const roomSaveMsg = ref('');
        const roomSaveOk = ref(false);
        const editingRoomName = ref(false);
        const roomNameEdit = ref('');

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
                await loadRooms();
                await loadAccounts();
                await loadUsers();
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

        // ── Room Detail ──
        function selectRoom(r) {
            selectedRoom.value = r;
            roomSubTab.value = 'ranking';
            selectedRoomAccount.value = r.account_uid || '';
        }
        function startEditRoomName() {
            roomNameEdit.value = selectedRoom.value?.room_name || '';
            editingRoomName.value = true;
        }
        async function saveRoomName() {
            const name = roomNameEdit.value?.trim() || '';
            const rid = selectedRoom.value?.room_id;
            if (!rid) return;
            editingRoomName.value = false;
            try {
                const res = await fetch(`/api/rooms/${rid}/config`, {
                    method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include',
                    body: JSON.stringify({room_name: name}),
                });
                const data = await res.json();
                if (data.ok) {
                    selectedRoom.value.room_name = name;
                    await loadRooms();
                }
            } catch(e) { /* ignore */ }
        }
        function selectRoomSubTab(key) {
            roomSubTab.value = key;
            if (key === 'config' && selectedRoom.value) {
                editRoomConfig(selectedRoom.value.room_id);
            }
        }
        function toggleCreateRoom() { showCreateRoom.value = !showCreateRoom.value; }
        function toggleNewAccount() { showNewAccount.value = !showNewAccount.value; }
        async function assignAccountToRoom() {
            const rid = selectedRoom.value?.room_id;
            if (!rid) return;
            accountAssignMsg.value = '';
            try {
                const res = await fetch(`/api/rooms/${rid}/config`, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({account_uid: selectedRoomAccount.value || ''}),
                });
                const data = await res.json();
                if (data.ok) {
                    accountAssignMsg.value = '✅ 已保存';
                    accountAssignOk.value = true;
                    selectedRoom.value.account_uid = selectedRoomAccount.value;
                    await loadRooms();
                    await loadAccounts();
                } else {
                    accountAssignMsg.value = '❌ 保存失败';
                    accountAssignOk.value = false;
                }
            } catch(e) {
                accountAssignMsg.value = '❌ 保存失败';
                accountAssignOk.value = false;
            }
        }
        async function assignAccountAndRestart() {
            const rid = selectedRoom.value?.room_id;
            if (!rid) return;
            accountAssignMsg.value = '';
            accountRestarting.value = true;
            try {
                // 先保存账号
                const res = await fetch(`/api/rooms/${rid}/config`, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({account_uid: selectedRoomAccount.value || ''}),
                });
                const data = await res.json();
                if (!data.ok) throw new Error('保存失败');
                // 再重启 bot
                const restartRes = await fetch(`/api/rooms/${rid}/restart`, {
                    method: 'POST', credentials: 'include',
                });
                if (!restartRes.ok) throw new Error('重启失败');
                accountAssignMsg.value = '✅ 已保存并重启';
                accountAssignOk.value = true;
                selectedRoom.value.account_uid = selectedRoomAccount.value;
                selectedRoom.value.status = 'running';
                await loadRooms();
                await loadAccounts();
            } catch(e) {
                accountAssignMsg.value = '❌ ' + e.message;
                accountAssignOk.value = false;
            } finally {
                accountRestarting.value = false;
            }
        }

        // ── Accounts Management ──
        async function loadAccounts() {
            try {
                const res = await fetch('/api/bili_accounts', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                accounts.value = data.accounts || [];
            } catch(e) { /* ignore */ }
        }
        async function startAccountLogin() {
            accountLoggingIn.value = true;
            accountQrState.value = 'loading';
            accountQrError.value = '';
            if (accountsPollTimer) { clearInterval(accountsPollTimer); accountsPollTimer = null; }
            accountQrImage.value = '';
            try {
                const res = await fetch('/api/bili_accounts', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({}),
                });
                if (!res.ok) throw new Error('请求失败');
                const data = await res.json();
                if (!data.ok) throw new Error(data.error || '生成二维码失败');
                accountQrImage.value = data.qr_image;
                accountSessionId.value = data.session_id;
                accountQrState.value = 'waiting';
                accountsPollTimer = setInterval(pollAccountLogin, 2000);
            } catch(e) {
                accountQrError.value = e.message;
                accountQrState.value = 'error';
            } finally {
                accountLoggingIn.value = false;
            }
        }
        function refreshQrCode() {
            // 停止旧轮询，重新生成二维码
            if (accountsPollTimer) { clearInterval(accountsPollTimer); accountsPollTimer = null; }
            accountSessionId.value = '';
            accountQrImage.value = '';
            accountQrState.value = 'idle';
            accountQrError.value = '';
            startAccountLogin();
        }
        async function pollAccountLogin() {
            if (!accountSessionId.value) return;
            try {
                const res = await fetch('/api/bili_accounts/status?session_id=' + accountSessionId.value, {
                    credentials: 'include',
                });
                if (!res.ok) return;
                const data = await res.json();
                if (data.state === 'done') {
                    accountQrState.value = 'done';
                    if (accountsPollTimer) { clearInterval(accountsPollTimer); accountsPollTimer = null; }
                    await saveAccountLogin();
                    // 登录成功，自动关闭二维码面板
                    setTimeout(() => {
                        showNewAccount.value = false;
                        accountQrState.value = 'idle';
                        accountQrImage.value = '';
                        accountSessionId.value = '';
                    }, 1500);
                } else if (data.state === 'scanned') {
                    accountQrState.value = 'scanned';
                } else if (data.state === 'timeout') {
                    accountQrState.value = 'timeout';
                    if (accountsPollTimer) { clearInterval(accountsPollTimer); accountsPollTimer = null; }
                } else if (data.state === 'error') {
                    accountQrError.value = data.message || '登录失败';
                    accountQrState.value = 'error';
                    if (accountsPollTimer) { clearInterval(accountsPollTimer); accountsPollTimer = null; }
                }
                // 不处理 'waiting' - 保持当前状态
            } catch(e) { /* ignore */ }
        }
        async function saveAccountLogin() {
            try {
                await fetch('/api/bili_accounts/save', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({session_id: accountSessionId.value}),
                });
            } catch(e) { /* ignore */ }
            await loadAccounts();
        }
        async function refreshAccount(uid) {
            // 目前没有单独的刷新 API，重新加载列表
            await loadAccounts();
        }
        async function deleteAccount(uid, nickname) {
            if (!confirm(`⚠️ 确定删除 B站账号「${nickname || uid}」（${uid}）？`)) return;
            try {
                const res = await fetch(`/api/bili_accounts/${uid}`, {
                    method: 'DELETE', credentials: 'include',
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                await loadAccounts();
            } catch(e) { alert('删除失败: ' + e.message); }
        }
        const editingNickname = ref(null);
        function startEditNickname(a) {
            a.editNick = a.nickname || '';
            editingNickname.value = a.uid;
        }
        async function saveNickname(a) {
            const nick = a.editNick?.trim();
            if (!nick) return;
            try {
                const res = await fetch(`/api/bili_accounts/${a.uid}/nickname`, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({nickname: nick}),
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                a.nickname = nick;
                editingNickname.value = null;
            } catch(e) { alert('修改失败: ' + e.message); }
        }
        async function verifyAccount(uid) {
            const acct = accounts.value.find(a => a.uid === uid);
            if (!acct) return;
            acct.verifying = true;
            try {
                const res = await fetch(`/api/bili_accounts/${uid}/verify`, {
                    method: 'POST', credentials: 'include',
                });
                const data = await res.json();
                acct.credential_ok = data.valid;
            } catch(e) { acct.credential_ok = false; }
            finally { acct.verifying = false; }
        }

        // ── Ranking ──
        async function loadRanking() {
            errRanking.value = '';
            ranking.value = [];
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            // 日期校验：确保 start <= end
            if (rStart.value && rEnd.value && rStart.value > rEnd.value) {
                errRanking.value = '起始日期不能晚于终止日期';
                return;
            }
            try {
                const res = await fetch(`/api/rooms/${roomId}/ranking?rStart=${rStart.value}&rEnd=${rEnd.value}&rType=${rType.value}`);
                if (!res.ok) { const txt = await res.text(); throw new Error(txt.slice(0,80)); }
                const data = await res.json();
                ranking.value = (data.ranking || []).map(u => ({
                    uid: u.uid, uname: u.uname, total_val: u.total,
                    total_profit: u.total_profit || 0
                }));
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
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}/user_dates?uid=${eUid.value}`);
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
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}/user_gifts?uid=${eUid.value}&date=${eDate.value}&gift_type=${eType.value}`);
                if (!res.ok) { const txt = await res.text(); throw new Error(txt.slice(0,80)); }
                const data = await res.json();
                exportList.value = (data || []).map((item, idx) => ({
                    ...item,
                    id: item.id || idx,
                    gift_num: item.gift_num || 0,
                    price: item.actual_value || 0,
                    ts: typeof item.ts === 'number' ? item.ts : 0,
                    avatar: item.avatar || '',
                    guard_level: item.guard_level || 0,
                    gift_icon: item.gift_icon || '',
                }));
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
            roomSubTab.value = 'export';
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
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}/delete_old`, {
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
        loadRooms();
        loadAccounts();

        // ── 动态标题 ──
        document.title = 'Ayabot 直播间机器人';

        // ── General Config ──
        async function loadGeneralConfig() {
            try {
                const res = await fetch('/api/general_config', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                if (data.error) return;
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

        // ── Room Management ──
        async function loadRooms() {
            try {
                const res = await fetch('/api/rooms', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                rooms.value = data.rooms || [];
                allRooms.value = data.rooms || [];
            } catch(e) { /* ignore */ }
        }
        async function createRoom() {
            if (!newRoomUid.value) { createRoomMsg.value = '请填写主播 UID'; createRoomOk.value = false; return; }
            creatingRoom.value = true;
            createRoomMsg.value = '';
            try {
                const body = {
                    anchor_uid: newRoomUid.value,
                };
                if (newRoomName.value) body.bot_name = newRoomName.value;
                if (newRoomPort.value && newRoomPort.value !== 8001) body.port = newRoomPort.value;
                if (newRoomDisplayId.value) body.room_display_id = newRoomDisplayId.value;
                const res = await fetch('/api/rooms', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                const data = await res.json();
                if (data.ok) {
                    createRoomMsg.value = `✅ 房间 ${data.room_id}（${data.room_display_id}）创建成功！`;
                    createRoomOk.value = true;
                    showCreateRoom.value = false;
                    // 关联 B站 账号
                    if (newRoomAccount.value) {
                        try {
                            await fetch(`/api/rooms/${data.room_id}/config`, {
                                method: 'POST',
                                headers: {'Content-Type':'application/json'},
                                credentials: 'include',
                                body: JSON.stringify({account_uid: newRoomAccount.value}),
                            });
                        } catch(e) { /* ignore */ }
                    }
                    newRoomUid.value = 0;
                    newRoomName.value = '';
                    newRoomPort.value = 8001;
                    newRoomDisplayId.value = 0;
                    newRoomAccount.value = '';
                    await loadRooms();
                } else {
                    createRoomMsg.value = '❌ ' + (data.error || '创建失败');
                    createRoomOk.value = false;
                }
            } catch(e) {
                createRoomMsg.value = '❌ 创建失败: ' + e.message;
                createRoomOk.value = false;
            } finally {
                creatingRoom.value = false;
            }
        }
        async function startRoom(roomId) {
            if (!confirm(`确定启动房间 ${roomId}？`)) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}/start`, {
                    method: 'POST', credentials: 'include',
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                await loadRooms();
            } catch(e) { alert('启动失败: ' + e.message); }
        }
        async function stopRoom(roomId) {
            if (!confirm(`确定停止房间 ${roomId}？`)) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}/stop`, {
                    method: 'POST', credentials: 'include',
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                await loadRooms();
            } catch(e) { alert('停止失败: ' + e.message); }
        }
        async function deleteRoom(roomId, name) {
            if (!confirm(`⚠️ 确定删除房间「${name || roomId}」（${roomId}）？\n这将停止服务并删除所有数据！`)) return;
            try {
                const res = await fetch(`/api/rooms/${roomId}`, {
                    method: 'DELETE', credentials: 'include',
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                editingRoom.value = null;
                roomConfig.value = null;
                await loadRooms();
            } catch(e) { alert('删除失败: ' + e.message); }
        }
        async function editRoomConfig(roomId) {
            editingRoom.value = roomId;
            roomConfig.value = null;
            roomSaveMsg.value = '';
            try {
                const res = await fetch(`/api/rooms/${roomId}/config`, {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                if (data.error) throw new Error(data.error);
                roomConfig.value = data;
            } catch(e) { alert('加载配置失败: ' + e.message); }
        }
        async function saveRoomConfig() {
            if (!roomConfig.value || !editingRoom.value) return;
            roomSaveMsg.value = '';
            try {
                const res = await fetch(`/api/rooms/${editingRoom.value}/config`, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify(roomConfig.value),
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                if (data.ok) {
                    roomSaveMsg.value = '✅ 已保存';
                    roomSaveOk.value = true;
                    await loadRooms();
                } else {
                    roomSaveMsg.value = '❌ 保存失败';
                    roomSaveOk.value = false;
                }
            } catch(e) {
                roomSaveMsg.value = '❌ ' + e.message;
                roomSaveOk.value = false;
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

        async function saveGlobalConfig() {
            cfgSaveMsg.value = '';
            try {
                const res = await fetch('/api/general_config', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({web_ui: {host: cfgHost.value, port: cfgPort.value}}),
                });
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                if (data.ok) {
                    cfgSaveMsg.value = '已保存';
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

        // ── User Management ──
        const streamers = ref([]);
        const allRooms = ref([]);
        const showStreamerForm = ref(null); // null | 'add' | 'edit'
        const editStreamerUser = ref('');
        const editStreamerPass = ref('');
        const editStreamerRooms = ref([]);
        const savingStreamer = ref(false);
        const streamerMsg = ref('');
        const streamerOk = ref(false);
        const showRoomDropdown = ref(false);
        const adminPass = ref('');
        const adminPassMsg = ref('');
        const adminPassOk = ref(false);
        async function loadUsers() {
            try {
                const res = await fetch('/api/users', {credentials: 'include'});
                if (!res.ok) return;
                const data = await res.json();
                const users = data.users || {};
                const list = [];
                for (const [uname, info] of Object.entries(users)) {
                    if (info.role === 'streamer') {
                        list.push({username: uname, ...info});
                    }
                }
                streamers.value = list;
            } catch(e) { /* ignore */ }
        }
        function toggleStreamerRoom(roomId) {
            const idx = editStreamerRooms.value.indexOf(roomId);
            if (idx >= 0) editStreamerRooms.value.splice(idx, 1);
            else editStreamerRooms.value.push(roomId);
        }
        function editStreamer(s) {
            editStreamerUser.value = s.username;
            editStreamerPass.value = '';
            editStreamerRooms.value = [...(s.rooms || [])];
            showStreamerForm.value = 'edit';
        }
        async function saveStreamer() {
            if (!editStreamerUser.value) { streamerMsg.value = '请填写用户名'; streamerOk.value = false; return; }
            if (showStreamerForm.value === 'add' && !editStreamerPass.value) { streamerMsg.value = '请填写密码'; streamerOk.value = false; return; }
            savingStreamer.value = true;
            streamerMsg.value = '';
            try {
                const body = {
                    username: editStreamerUser.value,
                    role: 'streamer',
                    rooms: editStreamerRooms.value,
                };
                if (showStreamerForm.value === 'add' || editStreamerPass.value) {
                    body.password = editStreamerPass.value;
                }
                const res = await fetch('/api/users/update', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include',
                    body: JSON.stringify(body),
                });
                const data = await res.json();
                if (data.ok) {
                    streamerMsg.value = '✅ 保存成功';
                    streamerOk.value = true;
                    showStreamerForm.value = null;
                    editStreamerUser.value = ''; editStreamerPass.value = ''; editStreamerRooms.value = [];
                    await loadUsers();
                } else { streamerMsg.value = '❌ 保存失败'; streamerOk.value = false; }
            } catch(e) { streamerMsg.value = '❌ ' + e.message; streamerOk.value = false; }
            finally { savingStreamer.value = false; }
        }
        async function deleteStreamer(username) {
            if (!confirm(`确定删除主播账号「${username}」？`)) return;
            try {
                const res = await fetch(`/api/users/${username}`, {method: 'DELETE', credentials: 'include'});
                if (!res.ok) throw new Error('删除失败');
                await loadUsers();
            } catch(e) { alert(e.message); }
        }
        async function saveAdminPass() {
            if (!adminPass.value || adminPass.value.length < 4) { adminPassMsg.value = '密码至少4位'; adminPassOk.value = false; return; }
            adminPassMsg.value = '';
            try {
                const res = await fetch('/api/users/admin_password', {
                    method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include',
                    body: JSON.stringify({password: adminPass.value}),
                });
                const data = await res.json();
                if (data.ok) {
                    adminPassMsg.value = '✅ 密码已更新';
                    adminPassOk.value = true;
                    adminPass.value = '';
                } else { adminPassMsg.value = '❌ 保存失败'; adminPassOk.value = false; }
            } catch(e) { adminPassMsg.value = '❌ ' + e.message; adminPassOk.value = false; }
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
                cfgHost, cfgPort,
                cfgSaveMsg, cfgSaveOk, loadGeneralConfig, saveGlobalConfig,
                restartMsg, restartOk, restartService,
                selectedRoom, roomSubTab, roomSubTabs, newRoomAccount, selectedRoomAccount,
                selectRoom, assignAccountToRoom, toggleCreateRoom, toggleNewAccount,
                selectRoomSubTab, accountAssignMsg, accountAssignOk, accountRestarting, assignAccountAndRestart,
                startEditRoomName, saveRoomName, editingRoomName, roomNameEdit,
                rooms, showCreateRoom, newRoomUid, newRoomName, newRoomPort, newRoomDisplayId,
                creatingRoom, createRoomMsg, createRoomOk, createRoom,
                startRoom, stopRoom, deleteRoom, editRoomConfig, saveRoomConfig, editingRoom, roomConfig,
                roomSaveMsg, roomSaveOk,
                accounts, showNewAccount, newAccountUid, accountLoggingIn, accountQrImage,
                accountQrState, accountQrError, startAccountLogin, refreshAccount, deleteAccount,
                loadAccounts, loadRooms, refreshQrCode,
                editingNickname, startEditNickname, saveNickname, verifyAccount,
                streamers, allRooms, showStreamerForm, editStreamerUser, editStreamerPass,
                editStreamerRooms, savingStreamer, streamerMsg, streamerOk, adminPass, adminPassMsg, adminPassOk,
                loadUsers, saveStreamer, editStreamer, deleteStreamer, toggleStreamerRoom, saveAdminPass, showRoomDropdown};
    }
}).mount('#app');
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
