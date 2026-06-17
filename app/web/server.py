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
import hashlib
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
from app.process_manager import start_room, stop_room, restart_room, room_status, clean_room, set_rooms_base_dir, start_room_async, stop_room_async, restart_room_async, start_periodic_log_cleanup, cleanup_all_stale_pidfiles
from app.version import get_version_display

_APP_VERSION = get_version_display()

logger = logging.getLogger("webui")

# 全局配置，由 init_app() 设置
AUTH_USER = "ayabot"
AUTH_PASS = "123456"
_SESSION_TIMEOUT = 3600
_HTTP_HOST = "0.0.0.0"
_HTTP_PORT = int(os.environ.get("AYABOT_PORT", "19810"))
_DB_PATH = "data/bot.db"

# LLM 配置（可变引用，webui 可保存更新）
_LLM_CONFIG_DICT: dict[str, Any] = {}
_CONFIG_YAML_PATH: str = "config.yaml"
# 房间状态持久化文件（记录哪些房间在重启前是启动的）
_ROOM_STATES_PATH: str = "data/room_states.json"


def _save_room_states() -> None:
    """保存当前各房间的运行状态到文件。"""
    try:
        rooms = _list_rooms_from_disk()
        states = {}
        for r in rooms:
            rid = r["room_id"]
            states[rid] = room_status(rid) == "running"
        Path(_ROOMS_BASE_DIR).resolve().joinpath(_ROOM_STATES_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(_ROOMS_BASE_DIR).resolve().joinpath(_ROOM_STATES_PATH).write_text(
            json.dumps(states, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("save room states failed: %s", exc)


def _load_room_states() -> dict[str, bool]:
    """读取之前保存的房间状态。"""
    try:
        p = Path(_ROOMS_BASE_DIR).resolve() / _ROOM_STATES_PATH
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("load room states failed: %s", exc)
    return {}


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
    set_rooms_base_dir(_ROOMS_BASE_DIR)
    logger.info("webui configured: host=%s port=%s db=%s", _HTTP_HOST, _HTTP_PORT, os.path.abspath(_DB_PATH))

    # 初始化/同步 users.json（不覆盖已通过 WebUI 修改过密码的账号）
    _init_users()


def _fallback_read_config() -> None:
    global _DB_PATH, AUTH_USER, AUTH_PASS, _CONFIG_YAML_PATH, _LLM_CONFIG_DICT, _ROOMS_BASE_DIR
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
    _CONFIG_YAML_PATH = str(_cfg_path.resolve())
    _ROOMS_BASE_DIR = str(Path(_CONFIG_YAML_PATH).parent.resolve())
    set_rooms_base_dir(_ROOMS_BASE_DIR)
    # 加载 LLM 配置到内存
    llm_raw = _raw.get("llm", {}) if _cfg_path.exists() else {}
    _LLM_CONFIG_DICT.update({
        "enabled": llm_raw.get("enabled", False),
        "provider": llm_raw.get("provider", "openai"),
        "api_key": llm_raw.get("api_key", ""),
        "base_url": llm_raw.get("base_url", ""),
        "model": llm_raw.get("model", ""),
        "wake_word": llm_raw.get("wake_word", "ayabot"),
        "temperature": llm_raw.get("temperature", 0.7),
        "top_p": llm_raw.get("top_p", 0.9),
        "max_tokens": llm_raw.get("max_tokens", 150),
        "system_prompt": llm_raw.get("system_prompt", ""),
        "context": {
            "enabled": llm_raw.get("context", {}).get("enabled", True),
            "mode": llm_raw.get("context", {}).get("mode", "isolated"),
            "content": llm_raw.get("context", {}).get("content", "llm_only"),
            "max_messages": llm_raw.get("context", {}).get("max_messages", 10),
        },
    })
    logger.info("webui using db (fallback): %s", os.path.abspath(_DB_PATH))

    # 初始化/同步 users.json（不覆盖已修改过的密码）
    _init_users()


app = FastAPI(title="BiliRobot Manager")

# ══════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════

_SESSIONS: dict[str, tuple[float, str, str, list]] = {}  # token -> (expiry, username, role, allowed_rooms)
_RATE_LIMIT: dict[str, list[float]] = {}  # ip -> [timestamps]
_AUTH_CONFIG_PATH: str = "data/users.json"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _migrate_old_users(old_file: Path, users: dict) -> dict:
    """Migrate plain-text passwords and old format to new format."""
    try:
        old = json.loads(old_file.read_text(encoding="utf-8"))
    except Exception:
        return users
    for uname, info in old.items():
        if uname in users:
            continue
        pw = info.get("password", "")
        role = info.get("role", "user")
        allowed_rooms = info.get("rooms", info.get("allowed_rooms", []))
        if role == "streamer":
            role = "user"
        users[uname] = {
            "password_hash": _hash_password(pw) if pw else "",
            "role": role,
            "allowed_rooms": allowed_rooms,
        }
    old_file.rename(old_file.with_suffix(".json.bak"))
    return users


def _load_users() -> dict:
    """加载用户配置. 返回 {username: {password_hash, role, allowed_rooms}}"""
    p = Path(_ROOMS_BASE_DIR).resolve() / _AUTH_CONFIG_PATH
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Migrate old auth/users.json if present
    old_p = Path(_ROOMS_BASE_DIR).resolve() / "auth" / "users.json"
    if old_p.exists():
        return _migrate_old_users(old_p, {})
    return {}


def _save_users(users: dict) -> None:
    p = Path(_ROOMS_BASE_DIR).resolve() / _AUTH_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    # Strip plain-text passwords for safety, keep only password_hash
    clean = {}
    for uname, info in users.items():
        entry = {
            "password_hash": info.get("password_hash", _hash_password(info.get("password", ""))),
            "role": info.get("role", "user"),
            "allowed_rooms": info.get("allowed_rooms", info.get("rooms", [])),
        }
        if info.get("must_reset_password"):
            entry["must_reset_password"] = True
        clean[uname] = entry
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_users() -> None:
    """初始化 users.json：创建默认账号但不覆盖已通过 WebUI 修改过的密码。"""
    users = _load_users()
    default_pw_hash = _hash_password("123456")
    # AUTH_USER（config.yaml 管理员账号）不存在则创建
    if AUTH_USER not in users:
        users[AUTH_USER] = {"password_hash": _hash_password(AUTH_PASS), "role": "admin", "allowed_rooms": []}
    # ayabot 默认账号不存在则创建
    if "ayabot" not in users:
        users["ayabot"] = {"password_hash": default_pw_hash, "role": "admin", "allowed_rooms": [], "must_reset_password": True}
    else:
        # ayabot 存在且密码仍是默认 123456 → 补 must_reset_password 标记
        ayabot = users["ayabot"]
        if ayabot.get("password_hash") == default_pw_hash and "must_reset_password" not in ayabot:
            ayabot["must_reset_password"] = True
    _save_users(users)


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
        data = await asyncio.wait_for(live.get_gift_config(), timeout=8)
    except asyncio.TimeoutError:
        logger.warning("get_gift_config timed out (network issue)")
        _GIFT_CACHE_BUILT = True
        return
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
    # 后台加载礼物缓存，不阻塞启动
    asyncio.create_task(_build_gift_cache())
    # 启动定期日志清理（后台线程，默认保留 3 天）
    start_periodic_log_cleanup()
    # 自动启动所有已配置的房间（Docker 部署时自动拉起）
    asyncio.create_task(_auto_start_rooms())


async def _auto_start_rooms() -> None:
    """延迟扫描 rooms/ 目录，按上次状态自动启停房间 Bot。"""
    try:
        await asyncio.sleep(3)
        cleanup_all_stale_pidfiles()
        saved = _load_room_states()
        rooms = _list_rooms_from_disk()
        if not rooms:
            logger.info("auto-start: no rooms found on disk")
            return
        started = 0
        for room in rooms:
            room_id = room["room_id"]
            should_run = saved.get(room_id, False)
            status = room_status(room_id)
            if should_run and status != "running":
                logger.info("auto-start: starting room %s (was running before restart)", room_id)
                ok = await start_room_async(room_id)
                if ok:
                    started += 1
                else:
                    logger.warning("auto-start: room %s failed to start", room_id)
            elif not should_run and status == "running":
                logger.info("auto-start: stopping room %s (was stopped before restart)", room_id)
                await stop_room_async(room_id)
            else:
                logger.debug("auto-start: room %s status unchanged, skip", room_id)
        logger.info("auto-start: %d rooms auto-started", started)
        # 保存启动后的状态
        _save_room_states()
    except Exception as exc:
        logger.error("auto-start: error: %s", exc, exc_info=True)


# ══════════════════════════════════════════════════════════════════
#  Auth middleware
# ══════════════════════════════════════════════════════════════════

def _get_current_role(request: Request) -> tuple[str, str, list]:
    """返回 (username, role, allowed_rooms) 或 ("", "", [])"""
    token = request.cookies.get("session", "")
    return _user_role(token)


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

    # ── Role-based access control ──
    _, role, _ = _get_current_role(request)
    method = request.method

    # Admin can do everything
    if role == "admin":
        return await call_next(request)

    # Regular user restrictions
    if role == "user":
        # Blocked paths for regular users
        if path.startswith("/api/admin/"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if path == "/api/general_config" and method == "POST":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if path == "/api/llm_config" and method == "POST":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if path in ("/api/users", "/api/users/update", "/api/users/admin_password") or \
           path.startswith("/api/users/"):
            return JSONResponse({"error": "forbidden"}, status_code=403)

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

    # 检查多用户配置（使用密码哈希比对）
    users = _load_users()
    user_info = users.get(username)
    must_reset = False
    if user_info:
        stored_hash = user_info.get("password_hash", "")
        if stored_hash and stored_hash == _hash_password(password):
            role = user_info.get("role", "user")
            allowed_rooms = user_info.get("allowed_rooms", [])
            must_reset = user_info.get("must_reset_password", False)
        else:
            return JSONResponse({"error": "wrong credentials"}, status_code=403)
    elif username == AUTH_USER and password == AUTH_PASS:
        # 兼容旧配置（config.yaml 中的账号密码）
        role = "admin"
        allowed_rooms = []
    else:
        return JSONResponse({"error": "wrong credentials"}, status_code=403)

    token = secrets.token_hex(32)
    _SESSIONS[token] = (time.time() + _SESSION_TIMEOUT, username, role, allowed_rooms)

    resp = JSONResponse({"ok": True, "token": token, "role": role, "username": username, "must_reset_password": must_reset})
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
    """返回 LLM 配置（含完整 api_key，前端自行决定是否掩码显示）."""
    ctx = _LLM_CONFIG_DICT.get("context", {})
    return {
        "enabled": _LLM_CONFIG_DICT.get("enabled", False),
        "provider": _LLM_CONFIG_DICT.get("provider", "openai"),
        "has_api_key": bool(_LLM_CONFIG_DICT.get("api_key")),
        "api_key": _LLM_CONFIG_DICT.get("api_key", ""),
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


@app.post("/api/restart")
async def api_restart():
    """重启 B站 Bot 服务（委托 ProcessManager 重新启动当前房间）. """
    return {"ok": True, "message": "多房间模式下请在房间详情中操作"}


# ══════════════════════════════════════════════════════════════
#  B站 扫码登录 API
# ══════════════════════════════════════════════════════════════
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

    return {"ok": True, "message": "凭据已保存"}


# ══════════════════════════════════════════════════════════════
#  预设模板 API
# ══════════════════════════════════════════════════════════════

_TEMPLATES_PATH: str = "data/templates.json"


def _load_templates() -> dict:
    p = Path(_ROOMS_BASE_DIR).resolve() / _TEMPLATES_PATH
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"llm_templates": [], "bot_templates": []}


def _save_templates(data: dict) -> None:
    p = Path(_ROOMS_BASE_DIR).resolve() / _TEMPLATES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/templates")
async def api_get_templates():
    """获取所有预设模板."""
    return _load_templates()


@app.post("/api/templates")
async def api_save_template(request: Request):
    """保存一个预设模板."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    ttype = body.get("type", "")  # "llm" or "bot"
    name = body.get("name", "").strip()
    config = body.get("config", {})
    if not name or not config:
        return JSONResponse({"error": "name and config required"}, status_code=400)

    data = _load_templates()
    key = f"{ttype}_templates"
    if key not in data:
        data[key] = []

    # 同名覆盖
    existing = [t for t in data[key] if t.get("name") == name]
    if existing:
        existing[0]["config"] = config
    else:
        data[key].append({"name": name, "config": config})

    _save_templates(data)
    return {"ok": True}


@app.delete("/api/templates")
async def api_delete_template(request: Request):
    """删除一个预设模板."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    ttype = body.get("type", "")
    name = body.get("name", "").strip()
    data = _load_templates()
    key = f"{ttype}_templates"
    if key in data:
        data[key] = [t for t in data[key] if t.get("name") != name]
    _save_templates(data)
    return {"ok": True}


@app.post("/api/restart_all_bots")
async def api_restart_all_bots():
    """重启所有正在运行的 Bot 进程."""
    rooms = _list_rooms_from_disk()
    count = 0
    for r in rooms:
        if r["status"] == "running":
            await restart_room_async(r["room_id"])
            count += 1
    _save_room_states()
    logger.info("restarted %d bots", count)
    return {"ok": True, "count": count}


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
async def api_list_users(request: Request):
    """列出所有用户（不包含密码哈希）. 管理员可看全部，普通用户只能看自己."""
    _, role, _ = _get_current_role(request)
    users_raw = _load_users()
    if role == "user":
        # 普通用户只能看到自己的信息
        token = request.cookies.get("session", "")
        username, _, _ = _user_role(token)
        filtered = {}
        if username in users_raw:
            filtered[username] = users_raw[username]
        return {"users": filtered}
    # Strip password hashes from response
    safe = {}
    for uname, info in users_raw.items():
        safe[uname] = {
            "role": info.get("role", "user"),
            "allowed_rooms": info.get("allowed_rooms", []),
        }
    return {"users": safe}


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
    role = body.get("role", "user")
    rooms = body.get("rooms", body.get("allowed_rooms", []))

    if password:
        users[username] = {
            "password_hash": _hash_password(password),
            "role": role,
            "allowed_rooms": rooms,
        }
    elif username in users:
        # 不修改密码，只更新角色和房间
        users[username]["role"] = role
        users[username]["allowed_rooms"] = rooms
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
    users["admin"] = {"password_hash": _hash_password(new_pass), "role": "admin", "allowed_rooms": []}
    _save_users(users)
    AUTH_PASS = new_pass  # 立即生效
    return {"ok": True}


# ── New Admin User Management API ──


@app.get("/api/admin/users")
async def api_admin_list_users():
    """管理员列出所有用户（完整信息，不含密码哈希）."""
    users_raw = _load_users()
    safe = {}
    for uname, info in users_raw.items():
        safe[uname] = {
            "role": info.get("role", "user"),
            "allowed_rooms": info.get("allowed_rooms", []),
        }
    return {"users": safe}


@app.post("/api/admin/users")
async def api_admin_create_user(request: Request):
    """管理员创建用户."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    if len(password) < 4:
        return JSONResponse({"error": "password too short"}, status_code=400)

    users = _load_users()
    if username in users:
        return JSONResponse({"error": "user already exists"}, status_code=409)

    role = body.get("role", "user")
    allowed_rooms = body.get("allowed_rooms", [])
    users[username] = {
        "password_hash": _hash_password(password),
        "role": role if role in ("admin", "user") else "user",
        "allowed_rooms": allowed_rooms,
    }
    _save_users(users)
    return {"ok": True}


@app.put("/api/admin/users/{username}")
async def api_admin_update_user(username: str, request: Request):
    """管理员修改用户（角色、密码、授权房间）."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    users = _load_users()
    if username not in users:
        return JSONResponse({"error": "user not found"}, status_code=404)

    password = body.get("password", "").strip()
    if password:
        if len(password) < 4:
            return JSONResponse({"error": "password too short"}, status_code=400)
        users[username]["password_hash"] = _hash_password(password)

    if "role" in body:
        role = body["role"]
        if role in ("admin", "user"):
            users[username]["role"] = role
    if "allowed_rooms" in body:
        users[username]["allowed_rooms"] = body["allowed_rooms"]

    _save_users(users)
    return {"ok": True}


@app.delete("/api/admin/users/{username}")
async def api_admin_delete_user(username: str):
    """管理员删除用户（不能删除自己）. """
    if username == "admin":
        return JSONResponse({"error": "cannot delete admin"}, status_code=400)
    users = _load_users()
    if username not in users:
        return JSONResponse({"error": "user not found"}, status_code=404)
    users.pop(username, None)
    _save_users(users)
    return {"ok": True}


# ── User Self-Service API ──


@app.post("/api/user/password")
async def api_user_change_password(request: Request):
    """用户修改自己的密码并可修改用户名。需要旧密码验证."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    token = request.cookies.get("session", "")
    username, role, _ = _user_role(token)
    if not username:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    old_password = body.get("old_password", "")
    new_password = body.get("new_password", "").strip()
    new_username = body.get("new_username", "").strip()

    if not old_password or not new_password:
        return JSONResponse({"error": "old_password and new_password required"}, status_code=400)
    if len(new_password) < 4:
        return JSONResponse({"error": "password too short"}, status_code=400)

    users = _load_users()
    user_info = users.get(username)
    if not user_info:
        return JSONResponse({"error": "user not found"}, status_code=404)

    stored_hash = user_info.get("password_hash", "")
    if stored_hash and stored_hash != _hash_password(old_password):
        return JSONResponse({"error": "wrong password"}, status_code=403)

    # 允许修改用户名
    if new_username and new_username != username:
        if new_username in users:
            return JSONResponse({"error": "用户名已存在"}, status_code=400)
        # 迁移用户信息到新用户名
        users[new_username] = users.pop(username)
        users[new_username]["password_hash"] = _hash_password(new_password)
        users[new_username]["must_reset_password"] = False
        _save_users(users)
        # 更新当前session
        _SESSIONS[token] = (time.time() + _SESSION_TIMEOUT, new_username, role, body.get("allowed_rooms", []))
        return {"ok": True, "must_reset_password": False, "new_username": new_username}
    else:
        users[username]["password_hash"] = _hash_password(new_password)
        users[username]["must_reset_password"] = False
        _save_users(users)
        return {"ok": True, "must_reset_password": False}


# ══════════════════════════════════════════════════════════════
#  多房间管理 API
# ══════════════════════════════════════════════════════════════

_ROOMS_BASE_DIR: str = "."  # 由 init_app 设置


async def _resolve_room_id(anchor_uid: int) -> int:
    """通过 B站 API 从主播 UID 查询直播间号."""
    url = f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={anchor_uid}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://live.bilibili.com/",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.content_type != "application/json":
                    raise ValueError(f"B站 API 返回非 JSON (content_type={resp.content_type}), 可能被风控")
                data = await resp.json()
        if data.get("code") == 0:
            room_id = data.get("data", {}).get("room_id") or data.get("data", {}).get("roomid")
            if room_id:
                return int(room_id)
        raise ValueError(f"B站 API 返回异常: {data}")
    except Exception as exc:
        raise ValueError(f"无法解析直播间号 (UID={anchor_uid}): {exc}") from exc


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
            "status": room_status(room_id),
            "port": cfg.get("web_ui", {}).get("port", 8000),
            "account_uid": cfg.get("account_uid", ""),
            "account_nick": "",
            "room_name": cfg.get("room_name", ""),
        })
    return rooms


@app.get("/api/rooms")
async def api_list_rooms(request: Request):
    """列出所有房间及状态."""
    # 从 process_manager 查询状态
    rooms = _list_rooms_from_disk()
    # 补齐 account_nick
    accounts_map = {a["uid"]: a.get("nickname", "") for a in _list_accounts()}
    for r in rooms:
        if r.get("account_uid"):
            r["account_nick"] = accounts_map.get(str(r["account_uid"]), "")
    # 根据用户角色过滤
    token = request.cookies.get("session", "")
    _, role, allowed = _user_role(token)
    if role in ("user", "streamer") and allowed:
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

    # 检查直播间号
    room_display_id = body.get("room_display_id")
    if not room_display_id:
        return JSONResponse({"error": "请填写直播间号（room_display_id）"}, status_code=400)

    room_id = str(room_display_id)
    from app.config import ensure_room_dirs, get_room_path
    room_dir = ensure_room_dirs(room_id, base_dir=_ROOMS_BASE_DIR)
    cfg_path = room_dir / "config.yaml"

    if cfg_path.exists():
        return JSONResponse({"error": f"房间 {room_id} 已存在"}, status_code=409)

    # 从 config.example.yaml 复制模板（优先），降级到根 config.yaml
    import shutil
    root_cfg = Path(_ROOMS_BASE_DIR).resolve() / "config.yaml"
    example_cfg = Path(_ROOMS_BASE_DIR).resolve() / "config.example.yaml"
    src = example_cfg if example_cfg.exists() else (root_cfg if root_cfg.exists() else None)
    if not src:
        return JSONResponse({"error": "根目录 config.yaml 不存在"}, status_code=500)

    shutil.copy2(str(src), str(cfg_path))

    # 写入房间配置
    port = body.get("port", 8000)
    room_name = body.get("room_name", "")
    bot_name = f"ayabot{room_id[:4]}"
    from app.config import update_config_from_dict
    cfg_update = {
        "room_display_id": int(room_display_id),
        "anchor_uid": int(anchor_uid),
        "bot_name": bot_name,
        "web_ui": {"port": int(port)},
    }
    if room_name:
        cfg_update["room_name"] = room_name
    update_config_from_dict(cfg_update, str(cfg_path))

    logger.info("room created: id=%s uid=%s port=%s", room_id, anchor_uid, port)
    return {"ok": True, "room_id": room_id, "room_display_id": room_display_id}


@app.post("/api/rooms/{room_id}/start")
async def api_start_room(room_id: str):
    """启动房间 Bot（自动选择同进程/子进程模式）。"""
    ok = await start_room_async(room_id)
    if not ok:
        return JSONResponse({"error": "启动失败，请检查配置"}, status_code=500)
    _save_room_states()
    return {"ok": True, "status": "running"}


@app.post("/api/rooms/{room_id}/stop")
async def api_stop_room(room_id: str):
    """停止房间 Bot（同时清理同进程和子进程）。"""
    await stop_room_async(room_id)
    _save_room_states()
    return {"ok": True, "status": "stopped"}


@app.post("/api/rooms/{room_id}/restart")
async def api_restart_room(room_id: str):
    """重启房间 Bot（自动选择同进程/子进程模式）。"""
    ok = await restart_room_async(room_id)
    _save_room_states()
    return {"ok": ok, "status": "running" if ok else "stopped"}


@app.delete("/api/rooms/{room_id}")
async def api_delete_room(room_id: str):
    """删除房间（停止进程 + 删目录）."""
    import shutil
    from app.config import get_room_path

    clean_room(room_id)
    _save_room_states()

    # 删除目录
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
    cred_json = json.dumps(cred_data, ensure_ascii=False, indent=2)
    (acc_dir / "credential.json").write_text(cred_json, encoding="utf-8")
    logger.info("credential saved for account %s", target_uid)

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


@app.post("/api/rooms/{room_id}/log/clear")
async def api_room_log_clear(room_id: str):
    """清空房间 Bot 日志文件。"""
    from app.config import get_room_path
    log_path = get_room_path(room_id, base_dir=_ROOMS_BASE_DIR) / "bot.log"
    try:
        if log_path.exists():
            log_path.write_text("", encoding="utf-8")
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/rooms/{room_id}/log")
async def api_room_log(room_id: str, request: Request, lines: int = 200):
    """返回房间 Bot 日志的最新 N 行。"""
    from app.config import get_room_path
    log_path = get_room_path(room_id, base_dir=_ROOMS_BASE_DIR) / "bot.log"
    if not log_path.exists():
        return {"lines": []}
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": tail, "total": len(all_lines)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


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
            where_clauses.append("date(ts, 'unixepoch', 'localtime') >= date(?)")
            params.append(rStart)
        if rEnd:
            where_clauses.append("date(ts, 'unixepoch', 'localtime') <= date(?)")
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
            WHERE uid = ? AND date(ts, 'unixepoch', 'localtime') = date(?) AND event_type = 'SEND_GIFT'{where_extra}
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
            "SELECT DISTINCT date(ts, 'unixepoch', 'localtime') FROM gift_events WHERE uid = ? ORDER BY date(ts, 'unixepoch', 'localtime')",
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
#  Danmaku Log API
# ══════════════════════════════════════════════════════════════


@app.get("/api/danmaku_log")
async def api_get_danmaku_log(room_id: str = "", limit: int = 50, offset: int = 0):
    try:
        db_path = _room_db_path(room_id) if room_id else Path(_DB_PATH)
        if not db_path.exists():
            return {"rows": [], "total": 0}
        from app.storage import StatsStore
        store = StatsStore(str(db_path))
        rows = store.get_danmaku_log(limit=limit, offset=offset)
        total = store.get_danmaku_log_count()
        store.close()
        return {"rows": rows, "total": total}
    except Exception as exc:
        logger.exception("danmaku_log get failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/danmaku_log")
async def api_clear_danmaku_log(room_id: str = ""):
    try:
        db_path = _room_db_path(room_id) if room_id else Path(_DB_PATH)
        if not db_path.exists():
            return {"deleted": 0}
        from app.storage import StatsStore
        store = StatsStore(str(db_path))
        count = store.clear_danmaku_log()
        store.close()
        return {"deleted": count}
    except Exception as exc:
        logger.exception("danmaku_log clear failed")
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
<link rel="icon" href="/favicon.ico">
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://unpkg.com/modern-screenshot@4.7.0/dist/index.js"></script>
<style>
/* ── 礼物卡片（毛玻璃 + 主题色渐变）── */
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
<body class="bg-gray-100 p-2 sm:p-4">
<div id="app" class="max-w-6xl mx-auto">

<!-- ══════ 登录页 ══════ -->
<div v-if="!loggedIn" class="flex items-center justify-center min-h-[80vh] px-4">
    <div class="bg-white p-6 sm:p-8 rounded-xl shadow-md w-full max-w-sm">
        <h1 class="text-xl font-bold text-center text-blue-600 mb-6">Ayabot 管理后台</h1>
        <div class="space-y-4">
            <input v-model="loginUser" placeholder="账号" class="border p-2 rounded w-full text-sm" @keyup.enter="doLogin">
            <input v-model="loginPass" type="password" placeholder="密码" class="border p-2 rounded w-full text-sm" @keyup.enter="doLogin">
            <div v-if="loginErr" class="text-red-500 text-sm">{{ loginErr }}</div>
            <button @click="doLogin" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded w-full">登录</button>
            <div class="mt-3 text-xs text-center text-amber-700 bg-amber-50 rounded p-2 border border-amber-200 leading-relaxed">
                初始账号: <strong>ayabot</strong>&nbsp;&nbsp;密码: <strong>123456</strong><br>
                <span class="text-gray-500">（首次登录请修改密码）</span>
            </div>
        </div>
        <div class="mt-5 pt-4 border-t border-gray-100 text-center">
            <p class="text-xs text-gray-400">
                <a href="https://github.com/yujianke100/ayabot" target="_blank" class="text-blue-500 hover:underline">⭐ GitHub</a>
                <span class="mx-1">·</span>
                <span class="text-red-500 font-bold">完全免费</span> · 开源
                <span class="block mt-1">Ayabot __AYABOT_VERSION__</span>
            </p>
        </div>
    </div>
</div>

<!-- ══════ 主界面 ══════ -->
<div v-if="loggedIn">
<header class="mb-4 sm:mb-6 flex items-center bg-white p-3 sm:p-4 rounded-xl shadow-sm gap-2">
    <h1 class="text-base sm:text-xl font-bold text-blue-600 flex-shrink-0">🎯 Ayabot</h1>
    <div class="flex-1 min-w-0"></div>
    <div class="flex items-center gap-1 sm:gap-4 text-xs sm:text-sm overflow-x-auto no-scrollbar mr-1 sm:mr-2">
        <button @click="tab='rooms'" :class="tab==='rooms'?'text-blue-600 font-bold border-b-2 border-blue-600':''" class="whitespace-nowrap px-1 sm:px-0">🏠 房间管理</button>
        <button v-if="userRole === 'admin'" @click="tab='global'" :class="tab==='global'?'text-blue-600 font-bold border-b-2 border-blue-600':''" class="whitespace-nowrap px-1 sm:px-0">⚙️ 全局配置</button>
        <button v-if="userRole === 'admin'" @click="tab='users'" :class="tab==='users'?'text-blue-600 font-bold border-b-2 border-blue-600':''" class="whitespace-nowrap px-1 sm:px-0">👥 用户管理</button>
        <button @click="tab='help'"  :class="tab==='help' ?'text-blue-600 font-bold border-b-2 border-blue-600':''" class="whitespace-nowrap px-1 sm:px-0">帮助</button>
    </div>
    <div class="relative flex-shrink-0">
        <button @click="showUserMenu = !showUserMenu" class="text-gray-600 hover:text-gray-800 border rounded px-2 py-1 text-xs whitespace-nowrap">
            {{ loginUser }} ▾
        </button>
        <div v-if="showUserMenu" class="absolute right-0 top-full mt-1 bg-white border rounded-xl shadow-lg w-44 text-sm overflow-hidden z-[100]" @click.stop>
            <div class="px-4 py-2.5 border-b text-xs text-gray-400 font-medium truncate">{{ loginUser }}</div>
            <button @click="openChangePwd(); showUserMenu=false" class="block w-full text-left px-4 py-2.5 hover:bg-gray-50 border-b">🔑 修改密码</button>
            <button @click="doLogout" class="block w-full text-left px-4 py-2.5 hover:bg-gray-50 text-red-500">退出登录</button>
        </div>
    </div>
</header>

<style>
.no-scrollbar::-webkit-scrollbar { display: none; }
.no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
</style>

<!-- ══════ 修改密码弹窗 ══════ -->
<div v-if="showChangePwd" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" :style="mustResetPwd ? 'backdrop-filter:blur(4px);' : ''" @mousedown.self="mustResetPwd || (showChangePwd = false)">
    <div class="bg-white p-6 rounded-xl shadow-lg w-80" @click.stop>
        <h3 class="font-bold text-lg mb-4">🔑 {{ mustResetPwd ? '⚠️ 首次登录请重置密码' : '修改密码' }}</h3>
        <div class="space-y-3">
            <label class="text-xs text-gray-500 block">用户名
                <input type="text" v-model="changePwdNewUser" placeholder="用户名" class="border p-2 rounded w-full text-sm" :disabled="changingPwd">
                <span class="text-gray-400" v-if="mustResetPwd">可修改为你想要的用户名</span>
            </label>
            <input type="password" id="inpOldPwd" v-model="changePwdOld" placeholder="当前密码" class="border p-2 rounded w-full text-sm" :disabled="mustResetPwd || changingPwd">
            <input type="password" id="inpNewPwd" v-model="changePwdNew" placeholder="新密码（至少4位）" class="border p-2 rounded w-full text-sm" :disabled="changingPwd">
            <input type="password" id="inpCfmPwd" v-model="changePwdConfirm" placeholder="再次输入新密码" class="border p-2 rounded w-full text-sm" :disabled="changingPwd" @keyup.enter="doChangePwd">
            <div v-if="changePwdMsg" class="text-sm" :class="changePwdOk ? 'text-green-600' : 'text-red-500'">{{ changePwdMsg }}</div>
            <div class="flex gap-2">
                <button @click="doChangePwd" :disabled="changingPwd" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm flex-1" :class="{'opacity-50 cursor-not-allowed': changingPwd}">确认</button>
                <button v-if="!mustResetPwd" @click="showChangePwd = false" :disabled="changingPwd" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm" :class="{'opacity-50 cursor-not-allowed': changingPwd}">取消</button>
            </div>
        </div>
    </div>
</div>

<!-- ══════ 房间管理 ══════ -->
<div v-if="tab==='rooms'" class="max-w-5xl mx-auto">

    <!-- ── 房间列表 ── -->
    <div v-if="!selectedRoom">
        <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
            <div class="flex items-center justify-between flex-wrap gap-2">
                <h2 class="text-lg font-bold">🏠 房间管理</h2>
                <button v-show="userRole === 'admin'" @click="toggleCreateRoom"
                        class="bg-green-500 hover:bg-green-600 text-white px-3 sm:px-4 py-2 rounded text-xs sm:text-sm">
                    {{ showCreateRoom ? '取消' : '➕ 新建房间' }}
                </button>
            </div>

            <!-- 新建表单 -->
            <div v-if="showCreateRoom" class="border rounded-lg p-4 bg-gray-50 space-y-3">
                <h3 class="text-sm font-bold">新建直播间</h3>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <label class="text-xs text-gray-500">主播 UID
                        <input type="number" v-model.number="newRoomUid" placeholder="B站 主播 UID"
                               class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">直播间名称
                        <input type="text" v-model="newRoomName" placeholder="给房间起个名字（可选）"
                               class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">直播间号
                        <input type="number" v-model.number="newRoomDisplayId" placeholder="B站 直播间号，如 1946287911"
                               class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">关联 B站 账号
                        <select v-model="newRoomAccount" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="">暂不关联</option>
                            <option v-for="a in accounts" :key="a.uid" :value="a.uid">{{ a.nickname || 'UID:'+a.uid }}</option>
                        </select>
                    </label>
                    <label class="text-xs text-gray-500">AI回复模板
                        <select v-model="newRoomLlmTemplate" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="">默认（无模板）</option>
                            <option v-for="t in llmTemplates" :key="t.name" :value="t.name">{{ t.name }}</option>
                        </select>
                    </label>
                    <label class="text-xs text-gray-500">机器人配置模板
                        <select v-model="newRoomBotTemplate" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="">默认（无模板）</option>
                            <option v-for="t in botTemplates" :key="t.name" :value="t.name">{{ t.name }}</option>
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
                <template v-if="userRole === 'admin'">暂无房间。点击「新建房间」添加。</template>
                <template v-else>暂无可用房间。</template>
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
                            class="bg-green-500 hover:bg-green-600 text-white px-3 py-1 rounded text-xs"
                            v-show="userRole === 'admin'">启动</button>
                    <button v-if="r.status === 'running'"
                            @click="stopRoom(r.room_id)"
                            class="bg-yellow-500 hover:bg-yellow-600 text-white px-3 py-1 rounded text-xs"
                            v-show="userRole === 'admin'">停止</button>
                    <button @click="deleteRoom(r.room_id, r.room_id)"
                            class="text-red-400 hover:text-red-600 text-xs underline"
                            v-show="userRole === 'admin'">删除</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ── 房间详情 ── -->
    <div v-else>
        <div class="mb-4">
            <button @click="goBackRoomList()"
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
                        <button v-show="userRole === 'admin'" @click="startEditRoomName" class="text-gray-400 hover:text-blue-500 text-xs ml-1">✏️</button>
                    </template>
                </h2>
                <div class="text-xs text-gray-500 mt-1">
                    主播 UID: {{ selectedRoom.anchor_uid }}
                    | 状态: {{ selectedRoom.status === 'running' ? '🟢 运行中' : '⏹️ 已停止' }}
                </div>
            </div>
            <div class="flex items-center gap-2 flex-wrap" v-show="userRole === 'admin'">
                <span class="text-xs text-gray-500">B站账号:</span>
                <select v-model="selectedRoomAccount" class="border p-1 rounded text-sm">
                    <option value="">不关联</option>
                    <option v-for="a in accounts" :key="a.uid" :value="a.uid">{{ a.nickname || 'UID:'+a.uid }}</option>
                </select>
                <button @click="assignAccountToRoom" class="bg-blue-500 hover:bg-blue-600 text-white px-2 py-0.5 rounded text-xs">保存</button>
                <button @click="assignAccountAndRestart" class="bg-green-600 hover:bg-green-700 text-white px-2 py-0.5 rounded text-xs" :disabled="accountRestarting">{{ accountRestarting ? '重启中...' : '保存并重启' }}</button>
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
        <div v-if="roomSubTab==='ranking'" class="grid grid-cols-1 gap-6">
            <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm">
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
                    <div class="overflow-x-auto">
                    <table class="w-full text-xs sm:text-sm">
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

            <div v-if="exportList.length" class="text-center mb-4">
                <button @click="captureExport" class="bg-purple-600 hover:bg-purple-700 text-white px-6 py-2.5 rounded-lg text-sm font-semibold shadow-md transition">
                    📷 导出 PNG
                </button>
                <p class="text-xs text-gray-400 mt-2">所见即所得，完整捕获全部内容（不受滚动/遮挡影响）</p>
            </div>

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
        <div v-if="roomSubTab==='llm'" class="max-w-full sm:max-w-2xl mx-auto">
            <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
                <h2 class="text-lg font-bold">🤖 AI 回复设置</h2>
                <p class="text-xs text-gray-400 mb-2">用户发送 <code class="bg-gray-100 px-1 rounded">#{{ llmWakeWord || 'ayabot' }} &lt;聊天内容&gt;</code> 时调用 LLM API 自动回复。</p>
                <div class="flex flex-wrap gap-1 mb-3 text-xs">
                    <span class="text-gray-500 mr-1">当前触发模式：</span>
                    <span class="bg-blue-50 px-2 py-0.5 rounded">#唤醒词</span>
                    <span v-if="roomConfig?.features?.llm_bare_trigger" class="bg-green-50 px-2 py-0.5 rounded">唤醒词开头</span>
                    <span v-if="roomConfig?.features?.llm_bare_trigger && roomConfig?.features?.llm_keyword_trigger" class="bg-purple-50 px-2 py-0.5 rounded">含唤醒词触发</span>
                    <span v-if="!roomConfig?.features?.llm_bare_trigger && !roomConfig?.features?.llm_keyword_trigger" class="text-gray-400">仅 #唤醒词</span>
                </div>

                <label class="flex items-center gap-2 text-sm">
                    <input type="checkbox" v-model="llmEnabled" class="w-4 h-4">
                    启用 AI 回复
                </label>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
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
                    <textarea v-model="llmPrompt" rows="3" class="border p-2 rounded w-full text-sm mt-1" placeholder="你是ayabot，一个可爱温柔的虚拟主播助手。所有回复必须控制在40字以内。"></textarea>
                </label>

                <hr class="my-2">
                <h3 class="text-sm font-bold">🧠 对话上下文</h3>

                <label class="flex items-center gap-2 text-sm">
                    <input type="checkbox" v-model="ctxEnabled" class="w-4 h-4">
                    开启上下文记忆
                </label>

                <div v-if="ctxEnabled" class="grid grid-cols-1 sm:grid-cols-2 gap-4">
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

                <div class="flex items-center gap-4 flex-wrap">
                    <button @click="saveLlmConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
                    <span v-if="llmSaveMsg" class="text-sm" :class="llmSaveOk ? 'text-green-600' : 'text-red-500'">{{ llmSaveMsg }}</span>
                    <span v-if="llmSaveOk" class="text-xs text-green-600">✅ 即时生效，无需重启</span>
                    <select v-model="applyLlmTemplate" class="border p-1 rounded text-xs">
                        <option value="">套用AI模板...</option>
                        <option v-for="t in llmTemplates" :key="t.name" :value="t.name">{{ t.name }}</option>
                    </select>
                    <button v-if="applyLlmTemplate" @click="applyTemplateToLlm" class="bg-purple-500 hover:bg-purple-600 text-white px-3 py-1.5 rounded text-xs">应用</button>
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
        <div v-if="roomSubTab==='config'" class="max-w-full sm:max-w-2xl mx-auto">
            <div v-if="roomConfig" class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
                <h2 class="text-lg font-bold">⚙️ 机器人配置</h2>
                <p class="text-xs text-gray-500">配置保存后需重启对应房间服务才能生效。</p>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">主播 UID
                        <input type="number" v-model.number="roomConfig.anchor_uid" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold">⏱️ 冷却时间（秒）</h3>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">欢迎同用户间隔
                        <input type="number" v-model.number="roomConfig.cooldown.welcome_user_seconds" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500">感谢同用户间隔
                        <input type="number" v-model.number="roomConfig.cooldown.thanks_user_seconds" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('rateLimit')">
                    <span v-if="configSections.rateLimit">▼</span><span v-else>▶</span> 🚦 限流
                </h3>
                <div v-show="configSections.rateLimit" class="grid grid-cols-1 sm:grid-cols-2 gap-4">
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
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('features')">
                    <span v-if="configSections.features">▼</span><span v-else>▶</span> 🎛️ 功能开关
                </h3>
                <div v-show="configSections.features">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.welcome_enabled" class="w-4 h-4"> 欢迎</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.thanks_enabled" class="w-4 h-4"> 感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.blindbox_enabled" class="w-4 h-4"> 盲盒统计</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.guard_thanks_enabled" class="w-4 h-4"> 大航海感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.like_thanks_enabled" class="w-4 h-4"> 点赞感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.share_thanks_enabled" class="w-4 h-4"> 转发感谢</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.follow_thanks_enabled" class="w-4 h-4"> 关注感谢</label>

                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.connected_message_enabled" class="w-4 h-4"> 连接消息</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.danmaku_log_enabled" class="w-4 h-4"> 弹幕记录</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.allow_bare_commands" class="w-4 h-4"> 免#指令</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.llm_bare_trigger" class="w-4 h-4"> AI免#前缀唤醒</label>
                    <label class="flex items-center gap-2" v-if="roomConfig.features.llm_bare_trigger"><input type="checkbox" v-model="roomConfig.features.llm_keyword_trigger" class="w-4 h-4"> 包含关键词触发AI</label>
                    <label class="flex items-center gap-2"><input type="checkbox" v-model="roomConfig.features.use_chinese_numbers_global" class="w-4 h-4"> 全局大写数字（反屏蔽）</label>
                </div>
                <div class="text-xs text-gray-400 mb-2">免#指令：开启后可不带 # 使用指令（签到、今日盲盒等） | AI免#：弹幕开头匹配唤醒词即触发AI | 包含关键词：弹幕含唤醒词即触发AI（需AI免#前缀唤醒）</div>
                <div v-if="roomConfig.features.danmaku_log_enabled" class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">弹幕记录最大条数
                        <input type="number" v-model.number="roomConfig.features.danmaku_log_max_entries" min="100" max="100000" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <label class="text-xs text-gray-500">日志级别
                        <select v-model="roomConfig.runtime.log_level" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="DEBUG">DEBUG</option>
                            <option value="INFO">INFO</option>
                            <option value="WARNING">WARNING</option>
                            <option value="ERROR">ERROR</option>
                        </select>
                        <span class="text-xs text-gray-400">保存后重启机器人生效</span>
                    </label>
                </div>
                </div>

                <hr>
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('pk')">
                    <span v-if="configSections.pk">▼</span><span v-else>▶</span> ⚔️ PK汇报
                </h3>
                <div v-show="configSections.pk" class="space-y-3">
                    <label class="flex items-center gap-2 text-sm"><input type="checkbox" v-model="roomConfig.features.pk_report_enabled" class="w-4 h-4"> PK开始/结束时汇报对手信息</label>
                    <label class="text-xs text-gray-500 block">PK开始模板
                        <input type="text" v-model="roomConfig.features.pk_report_template" placeholder="PK开始！对手{opponent}，{fans}粉，{guards}，{audience}观众，贡献{score}" class="border p-2 rounded w-full text-sm mt-1">
                        <span class="text-gray-400 text-[10px]">支持占位符: {opponent}对手名 {fans}粉丝数 {guards}大航海 {audience}观众 {score}贡献值</span>
                    </label>
                    <label class="text-xs text-gray-500 block">PK结束模板
                        <input type="text" v-model="roomConfig.features.pk_end_template" placeholder="{result} 我方分数{score}" class="border p-2 rounded w-full text-sm mt-1">
                        <span class="text-gray-400 text-[10px]">支持占位符: {result}结果(胜利！/对方获胜/PK结束) {score}我方分数</span>
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('templates')">
                    <span v-if="configSections.templates">▼</span><span v-else>▶</span> 📝 回复模板
                </h3>
                <div v-show="configSections.templates" class="space-y-3">
                    <label class="text-xs text-gray-500 block">欢迎模板
                        <input type="text" v-model="roomConfig.features.welcome_template" placeholder="欢迎{uname}来到直播间" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <div class="border rounded p-3 bg-gray-50 flex items-center justify-between">
                        <span class="text-xs text-gray-500">📋 多模板（随机+时段）共 {{ ((roomConfig?.features?.welcome_templates_list||[]).length) }} 条</span>
                        <button @click="openWelcomeTplModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                    </div>
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
                    <div v-if="roomConfig.features.like_thanks_enabled" class="border rounded p-3 bg-gray-50 mt-2">
                        <h4 class="text-xs font-bold text-gray-600">👍 点赞感谢</h4>
                        <label class="text-xs text-gray-500 block">模板
                            <input type="text" v-model="roomConfig.features.like_thanks_template" placeholder="感谢 {uname} 的点赞~" class="border p-2 rounded w-full text-sm mt-1">
                            <span class="text-gray-400 text-[10px]">支持占位符: {uname}用户名</span>
                        </label>
                    </div>
                    <div v-if="roomConfig.features.share_thanks_enabled" class="border rounded p-3 bg-gray-50 space-y-2 mt-2">
                        <h4 class="text-xs font-bold text-gray-600">🔁 转发感谢</h4>
                        <label class="text-xs text-gray-500 block">模板
                            <input type="text" v-model="roomConfig.features.share_thanks_template" placeholder="感谢分享直播间~" class="border p-2 rounded w-full text-sm mt-1">
                            <span class="text-gray-400 text-[10px]">B站不提供分享者信息，无法使用 {uname}</span>
                        </label>
                    </div>
                    <div v-if="roomConfig.features.follow_thanks_enabled" class="border rounded p-3 bg-gray-50 space-y-2 mt-2">
                        <h4 class="text-xs font-bold text-gray-600">❤️ 关注感谢</h4>
                        <label class="text-xs text-gray-500 block">模板
                            <input type="text" v-model="roomConfig.features.follow_thanks_template" placeholder="感谢 {uname} 的关注~" class="border p-2 rounded w-full text-sm mt-1">
                            <span class="text-gray-400 text-[10px]">支持占位符: {uname}用户名</span>
                        </label>
                    </div>
                    <h4 class="text-xs font-bold text-gray-600 mt-2">大航海欢迎（优先级高于默认欢迎模板）</h4>
                    <div class="border rounded p-3 bg-gray-50 flex items-center justify-between">
                        <span class="text-xs text-gray-500">📋 多模板（随机+时段）共 {{ guardWelcomeTotal }} 条</span>
                        <button @click="openGuardWelcomeTplModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                    </div>
                    <label class="text-xs text-gray-500 block">连接消息
                        <input type="text" v-model="roomConfig.features.connected_message" placeholder="来了喵~" class="border p-2 rounded w-full text-sm mt-1">
                    </label>

                    <h4 class="text-xs font-bold text-gray-600 mt-2">📦 盲盒统计（支持 {count} {cost} {profit} 占位符）</h4>
                    <p class="text-[10px] text-gray-400">修改盲盒指令的回复文本。如发送失败可尝试开启「数字转中文」或使用简短表述。</p>
                    <label class="flex items-center gap-2 text-xs mb-2">
                        <input type="checkbox" v-model="roomConfig.features.use_chinese_numbers" class="w-4 h-4" :disabled="roomConfig.features.use_chinese_numbers_global"> 盲盒数字转中文
                        <span v-if="roomConfig.features.use_chinese_numbers_global" class="text-gray-400 text-[10px]">（全局已开启，无需单独设置）</span>
                    </label>
                    <div class="border rounded p-3 bg-gray-50 space-y-2 mb-2">
                        <label class="flex items-center gap-2 text-xs">
                            <input type="checkbox" v-model="roomConfig.features.blindbox_glassheart_enabled" class="w-4 h-4"> 💔 玻璃心模式（亏损时隐藏真实收益）
                        </label>
                        <label v-if="roomConfig.features.blindbox_glassheart_enabled" class="text-xs text-gray-500 block">亏损回复
                            <input type="text" v-model="roomConfig.features.blindbox_glassheart_reply" placeholder="服务器繁忙，请稍后重试" class="border p-2 rounded w-full text-sm mt-1">
                        </label>
                    </div>
                    <label class="text-xs text-gray-500 block">#本月盲盒 有数据
                        <input type="text" v-model="roomConfig.features.blindbox_result_monthly" placeholder="本月盲盒共{count}个，花费{cost}，收益{profit}" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">#今日盲盒 有数据
                        <input type="text" v-model="roomConfig.features.blindbox_result_today" placeholder="今日盲盒共{count}个，花费{cost}，收益{profit}" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">有送礼无盲盒
                        <input type="text" v-model="roomConfig.features.blindbox_no_blindbox" placeholder="无盲盒记录" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <label class="text-xs text-gray-500 block">无任何送礼记录
                        <input type="text" v-model="roomConfig.features.blindbox_no_gift" placeholder="无送礼记录" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('periodic')">
                    <span v-if="configSections.periodic">▼</span><span v-else>▶</span> ⏰ 定时消息
                </h3>
                <div v-show="configSections.periodic" class="space-y-3">
                    <label class="flex items-center gap-2 text-sm">
                        <input type="checkbox" v-model="roomConfig.features.periodic_message_enabled" class="w-4 h-4">
                        启用定时消息（仅在开播时发送）
                    </label>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                        <label class="text-xs text-gray-500">间隔（秒）
                            <input type="number" v-model.number="roomConfig.features.periodic_message_interval_seconds" min="30" max="86400" class="border p-2 rounded w-full text-sm mt-1">
                            <span class="text-xs text-gray-400">默认 600 秒（10 分钟）</span>
                        </label>
                    </div>
                    <div class="border rounded p-3 bg-gray-50 flex items-center justify-between">
                        <span class="text-xs text-gray-500">📋 多模板（随机+时段）共 {{ ((roomConfig?.features?.periodic_messages_list||[]).length) }} 条</span>
                        <button @click="openPeriodicTplModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                    </div>
                    <label class="text-xs text-gray-500 block">旧版单模板（降级）
                        <input type="text" v-model="roomConfig.features.periodic_message_template" placeholder="欢迎关注直播间~点个关注不迷路！" class="border p-2 rounded w-full text-sm mt-1">
                        <span class="text-xs text-gray-400">新版多模板填空时使用此项</span>
                    </label>
                </div>

                <hr>
                <h3 class="text-sm font-bold cursor-pointer select-none hover:text-blue-600" @click="toggleSection('uidWelcome')">
                    <span v-if="configSections.uidWelcome">▼</span><span v-else>▶</span> 👤 UID特定欢迎模板
                </h3>
                <div v-show="configSections.uidWelcome">
                <p class="text-xs text-gray-500">为特定UID的用户设置专属欢迎词（优先级高于默认和大航海欢迎）。</p>
                <div class="border rounded p-3 bg-gray-50 flex items-center justify-between">
                    <span class="text-xs text-gray-500">共 {{ (roomConfig.features.welcome_templates_for_uids_entries||[]).length }} 条配置</span>
                    <button @click="openUidWelcomeModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                </div>
                </div>

                <!-- UID欢迎模板编辑弹窗 -->
                <div v-if="showUidWelcomeModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showUidWelcomeModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">👤 UID特定欢迎模板</h3>
                            <button @click="closeUidWelcomeModal" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-3">
                            <div v-for="(wt, wti) in uidWelcomeEditEntries" :key="wti" class="border rounded p-3 bg-gray-50 space-y-2">
                                <div class="flex items-center justify-between">
                                    <span class="text-xs font-bold">#{{ wti+1 }}</span>
                                    <button @click="uidWelcomeEditEntries.splice(wti, 1)" class="text-red-400 text-xs underline">删除</button>
                                </div>
                                <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                                    <label class="text-xs text-gray-500">UID
                                        <input type="number" v-model.number="wt.uid" class="border p-1 rounded w-full text-sm mt-1" placeholder="用户UID">
                                    </label>
                                    <label class="text-xs text-gray-500">欢迎模板
                                        <input type="text" v-model="wt.template" class="border p-1 rounded w-full text-sm mt-1" placeholder="欢迎{uname}！">
                                    </label>
                                </div>
                                <label class="flex items-center gap-1 text-xs text-gray-500 mt-1">
                                    <input type="checkbox" v-model="wt.allDay" class="w-3.5 h-3.5" @change="wt.time_start=0; wt.time_end=23">
                                    全天生效
                                </label>
                                <div v-if="!wt.allDay" class="grid grid-cols-2 gap-2">
                                    <label class="text-xs text-gray-500">起始小时
                                        <input type="number" v-model.number="wt.time_start" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                    <label class="text-xs text-gray-500">结束小时
                                        <input type="number" v-model.number="wt.time_end" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                </div>
                            </div>
                            <button @click="uidWelcomeEditEntries.push({uid: 0, template: '', time_start: 0, time_end: 23, allDay: true})" class="text-blue-500 text-xs underline">➕ 添加</button>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="closeUidWelcomeModal" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="saveUidWelcomeModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <hr>
                <h3 class="text-sm font-bold">🔑 关键词回复</h3>
                <p class="text-xs text-gray-500">设置弹幕关键词自动回复。支持包含、精确匹配。</p>
                <div class="border rounded p-3 bg-gray-50 space-y-2">
                    <label class="flex items-center gap-2 text-xs">
                        <input type="checkbox" v-model="roomConfig.keyword_reply.enabled" class="w-4 h-4"> 启用
                    </label>
                    <label class="text-xs text-gray-500">冷却时间(秒)
                        <input type="number" v-model.number="roomConfig.keyword_reply.cooldown" class="border p-2 rounded w-full text-sm mt-1">
                    </label>
                    <div class="flex items-center justify-between pt-1">
                        <span class="text-xs text-gray-500">共 {{ (roomConfig.keyword_reply.rules||[]).length }} 条规则</span>
                        <button @click="openKeywordModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                    </div>
                </div>

                <!-- 关键词回复编辑弹窗 -->
                <div v-if="showKeywordModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showKeywordModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-xl mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">🔑 关键词规则</h3>
                            <button @click="closeKeywordModal" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-3">
                            <div v-for="(rule, ri) in keywordEditRules" :key="ri" class="border rounded p-3 bg-gray-50 space-y-2">
                                <div class="flex items-center justify-between">
                                    <span class="text-xs font-bold">规则 #{{ ri+1 }}</span>
                                    <button @click="keywordEditRules.splice(ri, 1)" class="text-red-400 text-xs underline">删除</button>
                                </div>
                                <label class="text-xs text-gray-500">触发关键词（逗号分隔）
                                    <input type="text" v-model="rule.keywordsStr" class="border p-1 rounded w-full text-sm mt-1" placeholder="价格,多少钱">
                                </label>
                                <label class="text-xs text-gray-500">回复内容
                                    <textarea v-model="rule.reply" class="border p-1 rounded w-full text-sm mt-1" rows="2"></textarea>
                                </label>
                                <label class="text-xs text-gray-500">匹配模式
                                    <select v-model="rule.match_mode" class="border p-1 rounded text-sm">
                                        <option value="contains">包含</option>
                                        <option value="exact">精确</option>
                                    </select>
                                </label>
                                <label class="text-xs text-gray-500">限制UID（逗号分隔，留空=全部用户）
                                    <input type="text" v-model="rule.allowedUidsStr" class="border p-1 rounded w-full text-sm mt-1" placeholder="12345, 67890">
                                </label>
                                <label class="flex items-center gap-1 text-xs text-gray-500 mt-1">
                                    <input type="checkbox" v-model="rule.allDay" class="w-3.5 h-3.5" @change="rule.time_start=0; rule.time_end=23">
                                    全天生效
                                </label>
                                <div v-if="!rule.allDay" class="grid grid-cols-2 gap-2">
                                    <label class="text-xs text-gray-500">起始小时
                                        <input type="number" v-model.number="rule.time_start" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                    <label class="text-xs text-gray-500">结束小时
                                        <input type="number" v-model.number="rule.time_end" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                </div>
                            </div>
                            <button @click="keywordEditRules.push({keywordsStr: '', reply: '', match_mode: 'contains', allowedUidsStr: '', time_start: 0, time_end: 23, allDay: true})" class="text-blue-500 text-xs underline">➕ 添加规则</button>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="closeKeywordModal" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="saveKeywordModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <!-- ── 欢迎模板多模板编辑弹窗 ── -->
                <div v-if="showWelcomeTplModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showWelcomeTplModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">📝 欢迎多模板</h3>
                            <button @click="showWelcomeTplModal = false" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-3">
                            <p class="text-xs text-gray-500">每条模板可设置生效时段，机器人每次随机选一条当前时段生效的发送。</p>
                            <div v-for="(t, ti) in welcomeTplEntries" :key="ti" class="border rounded p-3 bg-gray-50 space-y-2">
                                <div class="flex items-center justify-between">
                                    <span class="text-xs font-bold">模板 #{{ ti+1 }}</span>
                                    <button @click="welcomeTplEntries.splice(ti, 1)" class="text-red-400 text-xs underline">删除</button>
                                </div>
                                <label class="text-xs text-gray-500">内容（支持 {uname}）
                                    <input type="text" v-model="t.text" class="border p-1 rounded w-full text-sm mt-1" placeholder="欢迎 {uname} 来到直播间喵~">
                                </label>
                                <label class="flex items-center gap-1 text-xs text-gray-500 mt-1">
                                    <input type="checkbox" v-model="t.allDay" class="w-3.5 h-3.5" @change="t.time_start=0; t.time_end=23">
                                    全天生效
                                </label>
                                <div v-if="!t.allDay" class="grid grid-cols-2 gap-2">
                                    <label class="text-xs text-gray-500">起始小时
                                        <input type="number" v-model.number="t.time_start" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                    <label class="text-xs text-gray-500">结束小时
                                        <input type="number" v-model.number="t.time_end" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                </div>
                                <p class="text-[10px] text-gray-400">起始≤结束=当天时段，起始>结束=跨天（如 22~6 是深夜到凌晨）</p>
                            </div>
                            <button @click="welcomeTplEntries.push({text: '', time_start: 0, time_end: 23})" class="text-blue-500 text-xs underline">➕ 添加模板</button>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="showWelcomeTplModal = false" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="saveWelcomeTplModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <!-- ── 大航海欢迎多模板编辑弹窗 ── -->
                <div v-if="showGuardWelcomeTplModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showGuardWelcomeTplModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">🛳️ 大航海欢迎多模板</h3>
                            <button @click="showGuardWelcomeTplModal = false" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-4">
                            <p class="text-xs text-gray-500">每个等级可配置多条模板，支持时段，随机选择。</p>
                            <div v-for="(level, lk) in {'captain':'舰长','commander':'提督','governor':'总督'}" :key="lk" class="border rounded p-3 bg-gray-100">
                                <h4 class="text-xs font-bold mb-2">{{ level }} <span class="text-gray-400">({{ lk }})</span></h4>
                                <div v-for="(t, ti) in (guardWelcomeTplEntries[lk]||[])" :key="ti" class="border-t border-gray-200 pt-2 mt-2 space-y-1">
                                    <div class="flex items-center justify-between">
                                        <span class="text-[10px] text-gray-400">#{{ ti+1 }}</span>
                                        <button @click="guardWelcomeTplEntries[lk].splice(ti, 1)" class="text-red-400 text-[10px] underline">删除</button>
                                    </div>
                                    <label class="text-xs text-gray-500">内容
                                        <input type="text" v-model="t.text" class="border p-1 rounded w-full text-sm mt-1" :placeholder="'欢迎'+level+'{uname}~'">
                                    </label>
                                    <label class="flex items-center gap-1 text-[10px] text-gray-500 mt-1">
                                        <input type="checkbox" v-model="t.allDay" class="w-3 h-3" @change="t.time_start=0; t.time_end=23">
                                        全天生效
                                    </label>
                                    <div v-if="!t.allDay" class="grid grid-cols-2 gap-2">
                                        <label class="text-xs text-gray-500">起始
                                            <input type="number" v-model.number="t.time_start" min="0" max="23" class="border p-1 rounded w-full text-sm">
                                        </label>
                                        <label class="text-xs text-gray-500">结束
                                            <input type="number" v-model.number="t.time_end" min="0" max="23" class="border p-1 rounded w-full text-sm">
                                        </label>
                                    </div>
                                </div>
                                <button @click="addGuardTpl(lk)" class="text-blue-500 text-[10px] underline mt-1">➕ 添加{{ level }}模板</button>
                            </div>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="showGuardWelcomeTplModal = false" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="saveGuardWelcomeTplModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <!-- ── 定时消息多模板编辑弹窗 ── -->
                <div v-if="showPeriodicTplModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showPeriodicTplModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">⏰ 定时消息多模板</h3>
                            <button @click="showPeriodicTplModal = false" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-3">
                            <p class="text-xs text-gray-500">每条消息可设置生效时段，机器人每次随机选一条当前时段生效的发送。</p>
                            <div v-for="(t, ti) in periodicTplEntries" :key="ti" class="border rounded p-3 bg-gray-50 space-y-2">
                                <div class="flex items-center justify-between">
                                    <span class="text-xs font-bold">消息 #{{ ti+1 }}</span>
                                    <button @click="periodicTplEntries.splice(ti, 1)" class="text-red-400 text-xs underline">删除</button>
                                </div>
                                <label class="text-xs text-gray-500">内容
                                    <input type="text" v-model="t.text" class="border p-1 rounded w-full text-sm mt-1" placeholder="点个关注不迷路喵~">
                                </label>
                                <label class="flex items-center gap-1 text-xs text-gray-500 mt-1">
                                    <input type="checkbox" v-model="t.allDay" class="w-3.5 h-3.5" @change="t.time_start=0; t.time_end=23">
                                    全天生效
                                </label>
                                <div v-if="!t.allDay" class="grid grid-cols-2 gap-2">
                                    <label class="text-xs text-gray-500">起始小时
                                        <input type="number" v-model.number="t.time_start" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                    <label class="text-xs text-gray-500">结束小时
                                        <input type="number" v-model.number="t.time_end" min="0" max="23" class="border p-1 rounded w-full text-sm mt-1">
                                    </label>
                                </div>
                                <p class="text-[10px] text-gray-400">起始≤结束=当天，起始>结束=跨天（如 22~6）</p>
                            </div>
                            <button @click="periodicTplEntries.push({text: '', time_start: 0, time_end: 23})" class="text-blue-500 text-xs underline">➕ 添加消息</button>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="showPeriodicTplModal = false" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="savePeriodicTplModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <!-- ── 签文多文本编辑弹窗 ── -->
                <div v-if="showFortuneTplModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showFortuneTplModal = false">
                    <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" @click.stop>
                        <div class="p-4 border-b flex items-center justify-between">
                            <h3 class="font-bold text-lg">🎴 签文编辑</h3>
                            <button @click="showFortuneTplModal = false" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                        </div>
                        <div class="p-4 overflow-y-auto flex-1 space-y-4">
                            <p class="text-xs text-gray-500">每个类型可添加多条签文，抽签时随机选一条使用。留空则使用默认签文。</p>
                            <div v-for="(ft, fti) in fortuneTypes" :key="ft.key" class="border rounded p-3 bg-gray-50">
                                <h4 class="text-xs font-bold mb-1">{{ ft.label }}</h4>
                                <div v-for="(txt, txi) in (fortuneTplEntries[ft.key]||[])" :key="txi" class="flex items-center gap-2 mb-1">
                                    <input type="text" v-model="fortuneTplEntries[ft.key][txi]" class="border p-1 rounded w-full text-sm" :placeholder="ft.placeholder">
                                    <button @click="fortuneTplEntries[ft.key].splice(txi, 1)" class="text-red-400 text-xs underline shrink-0">删</button>
                                </div>
                                <button @click="addFortuneText(ft.key)" class="text-blue-500 text-[10px] underline mt-1">➕ 添加{{ ft.label }}签文</button>
                            </div>
                        </div>
                        <div class="p-4 border-t flex items-center gap-2 justify-end">
                            <button @click="showFortuneTplModal = false" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
                            <button @click="saveFortuneTplModal" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">保存</button>
                        </div>
                    </div>
                </div>

                <hr>
                <h3 class="text-sm font-bold">🎴 自定义签文</h3>
                <p class="text-xs text-gray-500">修改 #抽签 命令的签文内容。每个类型可填多条，随机选。留空用默认。</p>
                <div class="border rounded p-3 bg-gray-50 flex items-center justify-between">
                    <span class="text-xs text-gray-500">📋 全部签文共 {{ fortuneTotalEntries }} 条</span>
                    <button @click="openFortuneTplModal" class="text-blue-500 text-xs underline">✏️ 编辑</button>
                </div>

                <div class="flex items-center gap-4 mt-4 flex-wrap">
                    <button @click="saveRoomConfig" class="bg-blue-500 hover:bg-blue-600 text-white px-6 py-2 rounded text-sm">保存</button>
                    <span v-if="roomSaveMsg" class="text-sm" :class="roomSaveOk ? 'text-green-600' : 'text-red-500'">{{ roomSaveMsg }}</span>
                    <span v-if="roomSaveOk" class="text-xs text-yellow-600">💡 更改配置后需重启机器人才能生效</span>
                    <button v-if="selectedRoom" @click="restartSingleBot(selectedRoom.room_id)" class="bg-yellow-500 hover:bg-yellow-600 text-white px-3 py-2 rounded text-sm text-xs">🔄 重启此机器人</button>
                    <button @click="editRoomConfig(selectedRoom?.room_id)" class="text-gray-500 hover:text-gray-700 underline text-sm">刷新</button>
                </div>

                <hr>
                <h3 class="text-sm font-bold">📋 套用预设模板</h3>
                <div class="flex items-center gap-2 flex-wrap">
                    <select v-model="applyBotTemplate" class="border p-1 rounded text-xs">
                        <option value="">选择机器人模板...</option>
                        <option v-for="t in botTemplates" :key="t.name" :value="t.name">{{ t.name }}</option>
                    </select>
                    <button v-if="applyBotTemplate" @click="applyTemplateToBot" class="bg-purple-500 hover:bg-purple-600 text-white px-3 py-1.5 rounded text-xs">应用</button>
                    <span v-if="applyTemplateMsg" class="text-xs" :class="applyTemplateOk ? 'text-green-600' : 'text-red-500'">{{ applyTemplateMsg }}</span>
                </div>
            </div>
        </div>

        <!-- 数据管理 -->
        <div v-if="roomSubTab==='manage'" class="max-w-full sm:max-w-lg mx-auto bg-white p-4 sm:p-6 rounded-xl shadow-sm">
            <h2 class="text-lg font-bold mb-4 text-red-600">⚠️ 数据管理</h2>
            <p class="text-sm text-gray-500 mb-4">注意：删除操作不可恢复。</p>
            <div class="flex gap-2 items-end">
                <label class="text-xs text-gray-500 flex-1">删除此日期之前的数据<input type="date" v-model="delDate" class="border p-2 rounded w-full text-sm mt-1"></label>
                <button @click="confirmDelete" class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded text-sm h-[38px]">删除</button>
            </div>
            <div v-if="delResult" class="mt-4 text-sm">{{ delResult }}</div>
        </div>

        <!-- Bot 日志 -->
        <div v-if="roomSubTab==='log'" class="max-w-full sm:max-w-4xl mx-auto">
            <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
                <div class="flex items-center justify-between">
                    <h2 class="text-lg font-bold">📋 Bot 日志</h2>
                    <div class="flex items-center gap-2">
                        <span class="text-xs text-gray-400">日志级别在「机器人配置」中修改，重启后生效</span>
                        <button @click="clearBotLog" class="bg-red-500 hover:bg-red-600 text-white px-3 py-2 rounded text-sm">清空</button>
                        <button @click="loadBotLog" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">刷新</button>
                    </div>
                </div>
                <div ref="logContainer" class="bg-gray-900 text-green-300 p-4 rounded text-xs font-mono max-h-[600px] overflow-y-auto whitespace-pre-wrap" v-html="botLogContent"></div>
                <div v-if="!botLogContent" class="text-gray-400 text-center py-4">暂无日志或日志文件不存在</div>
            </div>
        </div>

        <!-- 弹幕记录 -->
        <div v-if="roomSubTab==='danmaku'" class="max-w-full sm:max-w-4xl mx-auto">
            <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
                <div class="flex items-center justify-between">
                    <h2 class="text-lg font-bold">💬 弹幕记录</h2>
                    <div class="flex items-center gap-2">
                        <button @click="loadDanmakuLog"
                                class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">刷新</button>
                        <button @click="clearDanmakuLog"
                                class="bg-red-500 hover:bg-red-600 text-white px-4 py-2 rounded text-sm">清空</button>
                    </div>
                </div>
                <div v-if="danmakuErr" class="text-red-500 text-sm">{{ danmakuErr }}</div>
                <div v-if="danmakuRows.length" class="overflow-x-auto">
                    <table class="w-full text-sm">
                        <thead><tr class="bg-gray-50 sticky top-0"><th class="p-2 text-left">时间</th><th class="p-2 text-left">用户</th><th class="p-2 text-left">UID</th><th class="p-2 text-left">内容</th></tr></thead>
                        <tbody>
                            <tr v-for="r in danmakuRows" :key="r.id" class="border-t hover:bg-gray-50">
                                <td class="p-2 whitespace-nowrap text-xs text-gray-500">{{ fmtDanmakuTime(r.ts) }}</td>
                                <td class="p-2 font-medium">{{ r.uname }}</td>
                                <td class="p-2 text-xs text-gray-400">{{ r.uid }}</td>
                                <td class="p-2 max-w-xs truncate">{{ r.content }}</td>
                            </tr>
                        </tbody>
                    </table>
                    <div class="flex items-center justify-between mt-4 text-sm">
                        <div class="text-gray-500">共 {{ danmakuTotal }} 条</div>
                        <div class="flex gap-2 items-center">
                            <button @click="danmakuOffset = Math.max(0, danmakuOffset - danmakuLimit)"
                                    :disabled="danmakuOffset === 0"
                                    class="px-3 py-1 rounded border text-sm"
                                    :class="danmakuOffset === 0 ? 'text-gray-300 cursor-not-allowed' : 'hover:bg-gray-100'">上一页</button>
                            <span class="text-gray-500">第 {{ danmakuPage }} 页</span>
                            <button @click="danmakuOffset += danmakuLimit"
                                    :disabled="danmakuOffset + danmakuLimit >= danmakuTotal"
                                    class="px-3 py-1 rounded border text-sm"
                                    :class="danmakuOffset + danmakuLimit >= danmakuTotal ? 'text-gray-300 cursor-not-allowed' : 'hover:bg-gray-100'">下一页</button>
                        </div>
                    </div>
                </div>
                <div v-else-if="!danmakuErr" class="text-gray-400 text-center py-8">暂无弹幕记录</div>
            </div>
        </div>
    </div>
</div>

<!-- ══════ B站账号管理（已合并到 用户管理） ══════ -->

<!-- ══════ 全局配置 ══════ -->
<div v-if="tab==='global'" class="max-w-3xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4">
        <h2 class="text-lg font-bold">⚙️ 全局配置</h2>
        <div class="text-sm space-y-2">
            <div class="flex items-center gap-2">
                <span class="text-gray-500">Web 端口:</span>
                <span class="font-mono font-bold">{{ cfgPort }}</span>
            </div>
            <div class="flex items-center gap-2">
                <span class="text-gray-500">监听地址:</span>
                <span class="font-mono">{{ cfgHost }}</span>
            </div>
        </div>
        <div class="bg-yellow-50 border border-yellow-200 rounded p-3 text-xs text-yellow-700">
            💡 端口和监听地址通过 <code class="bg-yellow-100 px-1 rounded">config.yaml</code> 或环境变量
            <code class="bg-yellow-100 px-1 rounded">AYABOT_PORT</code> 配置，修改后需重启服务生效。
        </div>
    </div>

    <!-- ══════ 预设模板 ══════ -->
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4 mt-4">
        <div class="flex items-center justify-between">
            <h2 class="text-lg font-bold">📋 预设模板</h2>
            <div class="flex gap-2">
                <button @click="showAddTemplate = 'llm'" class="bg-blue-500 hover:bg-blue-600 text-white px-3 py-1.5 rounded text-xs">➕ AI模板</button>
                <button @click="showAddTemplate = 'bot'" class="bg-green-500 hover:bg-green-600 text-white px-3 py-1.5 rounded text-xs">➕ 机器人模板</button>
            </div>
        </div>
        <p class="text-xs text-gray-500">新建直播间时可选择预设模板快速配置。</p>

        <!-- AI模板列表 -->
        <div>
            <h3 class="text-sm font-bold mb-2">🤖 AI回复模板</h3>
            <div v-if="llmTemplates.length === 0" class="text-xs text-gray-400 py-2">暂无模板</div>
            <div v-for="(t, ti) in llmTemplates" :key="ti" class="border rounded p-3 flex items-center justify-between mb-2">
                <div>
                    <span class="font-bold text-sm">{{ t.name }}</span>
                    <span class="text-xs text-gray-400 ml-2">{{ t.config.model || t.config.provider || '' }}</span>
                </div>
                <button @click="deleteTemplate('llm', t.name)" class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
            </div>
        </div>

        <!-- 机器人模板列表 -->
        <div>
            <h3 class="text-sm font-bold mb-2">🤖 机器人配置模板</h3>
            <div v-if="botTemplates.length === 0" class="text-xs text-gray-400 py-2">暂无模板</div>
            <div v-for="(t, ti) in botTemplates" :key="ti" class="border rounded p-3 flex items-center justify-between mb-2">
                <div>
                    <span class="font-bold text-sm">{{ t.name }}</span>
                    <span class="text-xs text-gray-400 ml-2">{{ Object.keys(t.config).length }} 项配置</span>
                </div>
                <button @click="deleteTemplate('bot', t.name)" class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
            </div>
        </div>

        <!-- 添加模板弹窗 -->
        <div v-if="showAddTemplate" class="fixed inset-0 bg-black/40 flex items-center justify-center z-50" @click="showAddTemplate = null">
            <div class="bg-white rounded-xl shadow-lg w-full max-w-lg mx-4" @click.stop>
                <div class="p-4 border-b flex items-center justify-between">
                    <h3 class="font-bold text-lg">📋 {{ showAddTemplate === 'llm' ? 'AI回复模板' : '机器人配置模板' }}</h3>
                    <button @click="showAddTemplate = null" class="text-gray-400 hover:text-gray-600 text-xl">✕</button>
                </div>
                <div class="p-4 space-y-3">
                    <label class="text-xs text-gray-500 block">模板名称
                        <input type="text" v-model="templateFormName" class="border p-2 rounded w-full text-sm mt-1" placeholder="例如：标准AI配置">
                    </label>
                    <div class="text-xs text-gray-500">
                        从已有房间导入配置：
                        <select v-model="templateFormImportRoom" class="border p-2 rounded w-full text-sm mt-1">
                            <option value="">请选择房间...</option>
                            <option v-for="r in rooms" :key="r.room_id" :value="r.room_id">#{{ r.room_id }} {{ r.room_name || '' }}</option>
                        </select>
                    </div>
                    <button @click="saveTemplate" :disabled="savingTemplate"
                            class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                        {{ savingTemplate ? '保存中...' : '保存模板' }}
                    </button>
                    <div v-if="templateFormMsg" class="text-sm" :class="templateFormOk ? 'text-green-600' : 'text-red-500'">{{ templateFormMsg }}</div>
                </div>
            </div>
        </div>
    </div>

    <!-- ══════ 全局控制 ══════ -->
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-4 mt-4">
        <h2 class="text-lg font-bold">🔄 全局控制</h2>
        <div class="flex flex-wrap gap-3 items-center">
            <button @click="restartAllBots" :disabled="restartingAll"
                    class="bg-yellow-500 hover:bg-yellow-600 text-white px-5 py-2 rounded text-sm">
                {{ restartingAll ? '重启中...' : '🔄 重启所有机器人' }}
            </button>
            <span v-if="restartAllMsg" class="text-sm" :class="restartAllOk ? 'text-green-600' : 'text-red-500'">{{ restartAllMsg }}</span>
        </div>
    </div>
</div>

<!-- ══════ 用户管理 ══════ -->
<div v-if="tab==='users'" class="max-w-full sm:max-w-3xl mx-auto px-2 sm:px-0">
    <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4">
        <div class="flex items-center justify-between">
            <h2 class="text-lg font-bold">👥 用户管理</h2>
            <button @click="openAddUser" class="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded text-sm">➕ 添加用户</button>
        </div>

        <div v-if="showUserForm" class="border rounded-lg p-4 bg-gray-50 space-y-3">
            <h3 class="text-sm font-bold">{{ editingUser ? '编辑用户' : '添加用户' }}</h3>
            <div class="grid grid-cols-2 gap-3">
                <label class="text-xs text-gray-500">用户名
                    <input type="text" v-model="userFormUsername" :disabled="!!editingUser" class="border p-2 rounded w-full text-sm mt-1">
                </label>
                <label class="text-xs text-gray-500">密码 <span v-if="editingUser" class="text-gray-400">(留空不修改)</span>
                    <input type="password" v-model="userFormPassword" :placeholder="editingUser ? '留空不修改' : ''" class="border p-2 rounded w-full text-sm mt-1">
                </label>
                <label class="text-xs text-gray-500">角色
                    <select v-model="userFormRole" class="border p-2 rounded w-full text-sm mt-1">
                        <option value="user">普通用户</option>
                        <option value="admin">管理员</option>
                    </select>
                </label>
            </div>
            <label class="text-xs text-gray-500">授权直播间</label>
            <div v-if="userFormRole === 'user'" class="relative">
                <button @click="showUserRoomDropdown = !showUserRoomDropdown"
                        class="border rounded w-full text-sm p-2 text-left bg-white flex items-center justify-between">
                    <span v-if="userFormRooms.length === 0" class="text-gray-400">选择房间...</span>
                    <span v-else class="text-gray-700">{{ userFormRooms.length }} 个房间已选</span>
                    <span class="text-gray-400">▼</span>
                </button>
                <div v-if="showUserRoomDropdown" class="absolute z-50 mt-1 bg-white border rounded shadow-lg w-full max-h-48 overflow-y-auto">
                    <div v-for="r in allRooms" :key="r.room_id"
                         class="flex items-center gap-2 px-3 py-2 hover:bg-gray-50 cursor-pointer text-sm"
                         @click="toggleUserRoom(r.room_id)">
                        <input type="checkbox" :checked="userFormRooms.includes(r.room_id)" class="w-3.5 h-3.5">
                        <span>{{ r.room_name || ('#'+r.room_id) }}</span>
                    </div>
                    <div v-if="!allRooms.length" class="text-xs text-gray-400 px-3 py-2">暂无房间</div>
                </div>
            </div>
            <div v-if="userFormRole !== 'user'" class="text-xs text-gray-400 mt-1">管理员可查看所有直播间</div>
            <div class="flex gap-2 mt-2">
                <button @click="saveUserForm" :disabled="savingUserForm"
                        class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                    {{ savingUserForm ? '保存中...' : (editingUser ? '保存' : '添加') }}
                </button>
                <button @click="showUserForm = false; editingUser = null" class="bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded text-sm">取消</button>
            </div>
            <div v-if="userFormMsg" class="text-sm" :class="userFormOk ? 'text-green-600' : 'text-red-500'">{{ userFormMsg }}</div>
        </div>

        <div v-if="adminUsers.length === 0" class="text-sm text-gray-400 text-center py-8">暂无用户</div>
        <div v-for="u in adminUsers" :key="u.username"
             class="border rounded-lg p-4 flex items-center justify-between">
            <div>
                <div class="font-bold text-sm">
                    {{ u.username }}
                    <span class="text-xs ml-2 px-2 py-0.5 rounded"
                          :class="u.role === 'admin' ? 'bg-red-100 text-red-600' : 'bg-blue-100 text-blue-600'">{{ u.role === 'admin' ? '管理员' : '普通用户' }}</span>
                </div>
                <div class="text-xs text-gray-500 mt-1">
                    授权房间: {{ u.role === 'admin' ? '全部直播间' : (u.allowed_rooms?.length ? u.allowed_rooms.join(', ') : '无') }}
                </div>
            </div>
            <div class="flex gap-2">
                <button @click="editUser(u)" class="text-blue-500 hover:text-blue-700 text-xs underline">编辑</button>
                <button v-if="u.username !== loginUser" @click="deleteUser(u.username)" class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
            </div>
        </div>
    </div>

    <!-- ── B站账号（合并到用户管理） ── -->
    <div class="bg-white p-4 sm:p-6 rounded-xl shadow-sm space-y-4 mt-4">
        <div class="flex items-center justify-between">
            <h2 class="text-lg font-bold">🤖 B站账号</h2>
            <button @click="toggleNewAccount"
                    class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded text-sm">
                {{ showNewAccount ? '取消' : '📱 扫码登录' }}
            </button>
        </div>

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
                <div v-if="accountQrState === 'scanned'" class="text-yellow-600 text-sm">已扫码，请在手机上确认</div>
                <div v-if="accountQrState === 'done'" class="text-green-600 text-sm font-bold">✅ 登录成功，即将返回...</div>
                <div v-if="accountQrState === 'timeout' || accountQrState === 'expired'" class="text-red-500 text-sm">⏰ 二维码已过期</div>
                <div v-if="accountQrState === 'error'" class="text-red-500 text-sm">{{ accountQrError }}</div>
                <div v-if="accountQrState === 'waiting' || accountQrState === 'scanned' || accountQrState === 'timeout' || accountQrState === 'expired'" class="flex gap-2 justify-center">
                    <button @click="refreshQrCode" class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-1.5 rounded text-sm">🔄 刷新二维码</button>
                </div>
            </div>
        </div>

        <div v-if="!accounts || accounts.length === 0" class="text-sm text-gray-400 text-center py-8">暂无已登录的 B站 账号。</div>
        <div v-if="accounts && accounts.length > 1" class="flex justify-end">
            <button @click="verifyAllAccounts" :disabled="verifyingAll" class="text-blue-500 hover:text-blue-700 text-xs underline">
                {{ verifyingAll ? (`验证中 ${verifyQueue.length + 1}/${accounts.length}...`) : '🔍 一键检测所有' }}
            </button>
        </div>
        <div v-for="a in accounts" :key="a.uid" class="border rounded-lg p-4 flex items-center justify-between">
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
                <button @click="deleteAccount(a.uid, a.nickname)" class="text-red-400 hover:text-red-600 text-xs underline">删除</button>
            </div>
        </div>
    </div>
</div>
<div v-if="tab==='help'" class="max-w-3xl mx-auto">
    <div class="bg-white p-6 rounded-xl shadow-sm space-y-6 text-sm leading-relaxed">
        <h2 class="text-lg font-bold">📖 使用指南</h2>
        <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 space-y-3">
            <h3 class="font-bold text-blue-700">🚀 快速开始</h3>
            <ol class="list-decimal pl-4 space-y-2 text-xs">
                <li><strong class="text-blue-600">登录 B站 账号</strong> — 进入「用户管理」→「B站账号」→「扫码登录」，用 B站 App 扫描二维码完成登录。机器人需要通过你的账号发送弹幕。</li>
                <li><strong class="text-blue-600">新建直播间</strong> — 进入「房间管理」→「新建房间」，填写主播 UID 和直播间号，关联上一步登录的 B站 账号。</li>
                <li><strong class="text-blue-600">配置并启动</strong> — 创建后在房间详情中按需配置功能开关、回复模板、AI 回复等，然后点击「启动」按钮。</li>
                <li><strong class="text-blue-600">创建主播账号（可选）</strong> — 如果是帮别人部署，可在「用户管理」中新建「普通用户」账号并授权指定房间，对方登录后仅可查看/导出自己房间的数据。</li>
            </ol>
        </div>
        <div>
            <h3 class="font-bold text-blue-600 mb-1">🎯 弹幕命令</h3>
            <table class="w-full text-xs border-collapse">
                <thead><tr class="bg-gray-100"><th class="border p-1 text-left">命令</th><th class="border p-1 text-left">说明</th></tr></thead>
                <tbody>
                    <tr><td class="border p-1"><code>#签到</code></td><td class="border p-1">每日签到（直播场次连续签到）</td></tr>
                    <tr><td class="border p-1"><code>#抽签</code></td><td class="border p-1">今日运势抽签（支持自定义签文）</td></tr>
                    <tr><td class="border p-1"><code>#今日盲盒:用户名</code></td><td class="border p-1">今日盲盒统计，不加用户名查自己</td></tr>
                    <tr><td class="border p-1"><code>#本月盲盒:用户名</code></td><td class="border p-1">本月盲盒统计，不加用户名查自己</td></tr>
                    <tr><td class="border p-1"><code>#&lt;唤醒词&gt; &lt;聊天&gt;</code></td><td class="border p-1">AI 智能回复</td></tr>
                    <tr><td class="border p-1"><code>#帮助</code></td><td class="border p-1">显示所有命令</td></tr>
                </tbody>
            </table>
            <p class="text-xs text-gray-400 mt-2">开启「免#指令」后可不带 # 前缀触发指令。
            开启「AI免#前缀唤醒」后弹幕以唤醒词开头即触发 AI。</p>
        </div>
        <div>
            <h3 class="font-bold text-blue-600 mb-1">🤖 功能特性</h3>
            <ul class="list-disc pl-4 space-y-1 text-xs">
                <li>欢迎 — 新观众进入时自动欢迎</li>
                <li>感谢 — 送礼物/盲盒时自动感谢</li>
                <li>大航海感谢 — 舰长/提督/总督专属感谢</li>
                <li>关键词回复 — 支持包含/精确匹配 + UID限制</li>
                <li>AI 回复 — 三种触发方式 + 上下文记忆</li>
                <li>多房间 — 一个 WebUI 管理多个主播</li>
                <li>用户权限 — 管理员/普通用户，控制房间可见范围</li>
                <li>跨平台 — Linux/macOS/Windows，无需 systemd/sudo</li>
            </ul>
        </div>
        <div class="pt-4 border-t border-gray-200">
            <p class="text-xs text-gray-400 text-center">
                <a href="https://github.com/yujianke100/ayabot" target="_blank" class="text-blue-500 hover:underline">⭐ 去 GitHub 给个 Star</a>
                <span class="mx-1">·</span>
                本软件<span class="text-red-500 font-bold">完全免费</span>，以 GPLv3 协议开源
                <span class="block mt-1">Ayabot __AYABOT_VERSION__</span>
            </p>
            <div class="mt-4 pt-4 border-t border-gray-100 text-center">
                <p class="text-xs text-gray-500 mb-3">☕ 觉得好用？请作者喝杯咖啡吧 ❤️</p>
                <div class="flex justify-center gap-6">
                    <div class="text-center">
                        <img src="/alipay.jpg" width="150" height="150" alt="支付宝" class="rounded-xl border inline-block">
                        <p class="text-xs text-gray-400 mt-1">支付宝</p>
                    </div>
                    <div class="text-center">
                        <img src="/wechat.png" width="150" height="150" alt="微信" class="rounded-xl border inline-block">
                        <p class="text-xs text-gray-400 mt-1">微信</p>
                    </div>
                </div>
            </div>
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
        const userRole = ref('');
        const mustResetPwd = ref(false);

        // 修改密码
        const showChangePwd = ref(false);
        const changePwdOld = ref('');
        const changePwdNew = ref('');
        const changePwdConfirm = ref('');
        const changePwdNewUser = ref('');
        const changePwdMsg = ref('');
        const changePwdOk = ref(false);
        const changingPwd = ref(false);

        // 用户菜单
        const showUserMenu = ref(false);

        // 管理员用户管理
        const adminUsers = ref([]);
        const showUserForm = ref(false);
        const editingUser = ref(null);
        const userFormUsername = ref('');
        const userFormPassword = ref('');
        const userFormRole = ref('user');
        const userFormRooms = ref([]);
        const showUserRoomDropdown = ref(false);
        const savingUserForm = ref(false);
        const userFormMsg = ref('');
        const userFormOk = ref(false);

        // Room detail
        const selectedRoom = ref(null);
        const roomSubTab = ref('ranking');
        const roomSubTabs = computed(() => {
            const tabs = [
                {key: 'ranking', label: '送礼排行'},
                {key: 'export', label: '精美导出'},
                {key: 'danmaku', label: '弹幕记录'},
            ];
            if (userRole.value === 'admin') {
                tabs.push(
                    {key: 'llm', label: 'AI回复'},
                    {key: 'config', label: '机器人配置'},
                    {key: 'manage', label: '数据管理'},
                    {key: 'log', label: '📋 日志'},
                );
            }
            return tabs;
        });
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

        // Danmaku Log
        const danmakuRows = ref([]);
        const danmakuErr = ref('');
        const danmakuOffset = ref(0);
        const danmakuLimit = ref(50);
        const danmakuTotal = ref(0);
        const danmakuPage = Vue.computed(() => Math.floor(danmakuOffset.value / danmakuLimit.value) + 1);

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

        // 模板管理
        const llmTemplates = ref([]);
        const botTemplates = ref([]);
        const showAddTemplate = ref(null);  // 'llm' | 'bot' | null
        const templateFormName = ref('');
        const templateFormImportRoom = ref('');
        const savingTemplate = ref(false);
        const templateFormMsg = ref('');
        const templateFormOk = ref(false);
        const newRoomLlmTemplate = ref('');
        const newRoomBotTemplate = ref('');
        const restartingAll = ref(false);
        const restartAllMsg = ref('');
        const restartAllOk = ref(false);
        const applyLlmTemplate = ref('');
        const applyBotTemplate = ref('');
        const applyTemplateMsg = ref('');
        const applyTemplateOk = ref(false);

        // 配置区折叠状态（默认全部收起，accordion 模式：开一个关其他）
        const configSections = ref({
            rateLimit: false,
            features: false,
            pk: false,
            templates: false,
            periodic: false,
            uidWelcome: false,
        });
        function toggleSection(key) {
            const next = !configSections.value[key];
            // 收起所有
            for (const k in configSections.value) configSections.value[k] = false;
            // 只打开点击的那个
            configSections.value[key] = next;
        }

        // LLM Config
        const llmEnabled = ref(false);
        const llmProvider = ref('openai');
        const llmApiKey = ref('');
        const llmApiKeyReal = ref('');  // 真实的 API key（不掩码）
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
        const cfgPort = ref(19810);
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
                const data = await res.json();
                loggedIn.value = true;
                userRole.value = data.role || '';
                if (!loginUser.value && data.username) loginUser.value = data.username;
                if (data.must_reset_password) {
                    mustResetPwd.value = true;
                    showChangePwd.value = true;
                    changePwdNewUser.value = loginUser.value;
                    changePwdOld.value = loginPass.value; // 默认密码即登录密码
                    changePwdNew.value = '';
                    changePwdConfirm.value = '';
                    changePwdMsg.value = '⚠️ 首次登录请先修改默认密码';
                    changePwdOk.value = false;
                    return;
                }
                await loadLlmConfig();
                await loadGeneralConfig();
                await loadRooms();
                await loadAccounts();
                await loadUsers();
                await loadTemplates();
            } catch(e) { loginErr.value = '登录失败: ' + e.message; }
        }
        function doLogout() {
            document.cookie = 'session=;max-age=0';
            loggedIn.value = false;
            tab.value = 'rooms';
            userRole.value = '';
            showUserMenu.value = false;
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
            // 清除上一个房间的查询状态
            _clearRoomState();
        }
        function goBackRoomList() {
            selectedRoom.value = null;
            roomSubTab.value = 'ranking';
            _clearRoomState();
        }
        function _clearRoomState() {
            ranking.value = [];
            errRanking.value = '';
            exportList.value = [];
            exportDates.value = [];
            exportDatesSet.value = new Set();
            errExport.value = '';
            eUid.value = 0;
            eName.value = '';
            eDate.value = new Date().toISOString().slice(0,10);
            eType.value = 'all';
            ePerCol.value = 6;
            eColWidth.value = 340;
            showCalendar.value = false;
            roomConfig.value = null;
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
            if ((key === 'config' || key === 'llm') && selectedRoom.value) {
                editRoomConfig(selectedRoom.value.room_id);
            }
            if (key === 'llm') {
                // AI 页面启动后等待 roomConfig 加载完毕再刷新 LLM 表单
                setTimeout(() => loadLlmConfig(), 100);
            }
            if (key === 'danmaku') {
                danmakuOffset.value = 0;
                loadDanmakuLog();
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
        // ── 验证队列（串行执行，避免并发卡顿） ──
        const verifyQueue = ref([]);
        const verifyingAll = ref(false);
        let _verifyProcessing = false;
        async function _processVerifyQueue() {
            if (_verifyProcessing || verifyQueue.value.length === 0) return;
            _verifyProcessing = true;
            while (verifyQueue.value.length > 0) {
                const uid = verifyQueue.value.shift();
                const acct = accounts.value.find(a => a.uid === uid);
                if (!acct) continue;
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
            verifyingAll.value = false;
            _verifyProcessing = false;
        }
        async function verifyAccount(uid) {
            const acct = accounts.value.find(a => a.uid === uid);
            if (!acct || acct.verifying) return;
            if (verifyQueue.value.includes(uid)) return;
            verifyQueue.value.push(uid);
            _processVerifyQueue();
        }
        async function verifyAllAccounts() {
            if (!accounts.value.length) return;
            verifyingAll.value = true;
            for (const a of accounts.value) {
                if (!a.verifying && !verifyQueue.value.includes(a.uid)) {
                    verifyQueue.value.push(a.uid);
                }
            }
            _processVerifyQueue();
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
        async function captureExport() {
            const el = document.getElementById('capture');
            if (!el) return;
            if (typeof window.modernScreenshot === 'undefined') { alert('截图库加载中，请稍后重试'); return; }
            // 1. 【精准测量】在克隆前拿到原 DOM 真实内容尺寸
            //    以 capture-grid（多列网格）为基准，避免离屏后 Flex 无限拉伸
            const innerGrid = el.querySelector('.capture-grid') || el.querySelector('.capture-inner') || el;
            const targetWidth = innerGrid.scrollWidth;
            const targetHeight = el.scrollHeight;
            // 2. 离屏克隆
            const clone = el.cloneNode(true);
            // 3. 在挂载前给所有 img 补上 crossorigin
            clone.querySelectorAll('img').forEach(img => {
                img.setAttribute('crossorigin', 'anonymous');
            });
            // 4. 锁定克隆节点宽高，防止离屏 fixed 容器中无限拉伸/坍塌导致黑边
            clone.style.width = targetWidth + 'px';
            clone.style.minWidth = targetWidth + 'px';
            clone.style.maxWidth = targetWidth + 'px';
            clone.style.overflow = 'visible';
            clone.style.overflowX = 'visible';
            clone.style.maxHeight = 'none';
            // 5. 挂载到离屏容器 + 复制 body className 保留 Tailwind 字体上下文
            const container = document.createElement('div');
            container.style.position = 'fixed';
            container.style.left = '-9999px';
            container.style.top = '0';
            container.style.zIndex = '-1000';
            container.style.pointerEvents = 'none';
            container.className = document.body.className;
            container.appendChild(clone);
            document.body.appendChild(container);
            // 6. 等待所有图片加载
            await Promise.all(Array.from(clone.querySelectorAll('img')).map(img => {
                if (img.complete && img.naturalWidth > 0) return Promise.resolve();
                return new Promise(resolve => { img.onload = resolve; img.onerror = resolve; });
            }));
            // 7. 等一帧完成布局重排与字体渲染
            await new Promise(r => requestAnimationFrame(r));
            try {
                // 8. modern-screenshot：基于 SVG foreignObject 原生渲染
                //    完美继承 CSS truncate/Flex/毛玻璃，100% 像素级还原
                const dataUrl = await window.modernScreenshot.domToPng(clone, {
                    scale: 2,
                    backgroundColor: null,
                    width: targetWidth,
                    height: targetHeight,
                    features: {
                        router: false,  // 关闭路由防错，提升速度
                    },
                });
                const link = document.createElement('a');
                link.download = `${eName.value || eUid.value}_${eDate.value}_礼物明细.png`;
                link.href = dataUrl;
                link.click();
            } catch(e) {
                alert('导出失败: ' + e.message);
            } finally {
                if (container.parentNode) container.parentNode.removeChild(container);
            }
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

        // ── Bot Log ──
        const botLogContent = ref('');
        const logLevel = ref('INFO');
        const logContainer = ref(null);
        async function loadBotLog() {
            if (!selectedRoom.value?.room_id) return;
            try {
                const res = await fetch(`/api/rooms/${selectedRoom.value.room_id}/log?lines=500`, {credentials: 'include'});
                if (!res.ok) return;
                const data = await res.json();
                if (data.error) { botLogContent.value = '加载失败: ' + data.error; return; }
                const lines = data.lines || [];
                botLogContent.value = lines.map(l => {
                    return l
                        .replace(/ERROR/g, '<span class="text-red-400">ERROR</span>')
                        .replace(/WARNING/g, '<span class="text-yellow-400">WARNING</span>')
                        .replace(/INFO/g, '<span class="text-green-300">INFO</span>')
                        .replace(/DEBUG/g, '<span class="text-gray-500">DEBUG</span>');
                }).join('\n');
                setTimeout(() => { if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight; }, 50);
            } catch(e) { botLogContent.value = '加载失败: ' + e.message; }
        }

        async function clearBotLog() {
            if (!selectedRoom.value?.room_id) return;
            if (!confirm('确定清空 Bot 日志？')) return;
            try {
                await fetch(`/api/rooms/${selectedRoom.value.room_id}/log/clear`, {method:'POST', credentials:'include'});
                botLogContent.value = '';
            } catch(e) { /* ignore */ }
        }

        // ── Danmaku Log ──
        async function loadDanmakuLog() {
            danmakuErr.value = '';
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            try {
                const res = await fetch(`/api/danmaku_log?room_id=${roomId}&limit=${danmakuLimit.value}&offset=${danmakuOffset.value}`, {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                danmakuRows.value = data.rows || [];
                danmakuTotal.value = data.total || 0;
            } catch(e) { danmakuErr.value = '加载失败: ' + e.message; }
        }
        async function clearDanmakuLog() {
            if (!confirm('确定清空所有弹幕记录？此操作不可恢复！')) return;
            const roomId = selectedRoom.value?.room_id;
            if (!roomId) return;
            danmakuErr.value = '';
            try {
                const res = await fetch(`/api/danmaku_log?room_id=${roomId}`, {method: 'DELETE', credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                const data = await res.json();
                danmakuRows.value = [];
                danmakuOffset.value = 0;
                danmakuTotal.value = 0;
                // 重新加载弹幕记录列表
                loadDanmakuLog();
            } catch(e) { danmakuErr.value = '清空失败: ' + e.message; }
        }
        function fmtDanmakuTime(ts) {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            return d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});
        }
        // 翻页时自动重新加载弹幕
        Vue.watch([danmakuOffset, danmakuLimit], () => {
            if (selectedRoom.value?.room_id) loadDanmakuLog();
        });

        // ── LLM Config ──
        async function loadLlmConfig() {
            // 如果当前在房间详情中，从房间配置加载 LLM 设置
            if (selectedRoom.value) {
                // roomConfig 可能尚未加载，直接取房间配置
                let llm = roomConfig.value?.llm;
                if (!llm) {
                    try {
                        const res = await fetch(`/api/rooms/${selectedRoom.value.room_id}/config`, {credentials: 'include'});
                        if (res.ok) {
                            const data = await res.json();
                            if (data && !data.error) llm = data.llm;
                        }
                    } catch(e) { /* fallback to global */ }
                }
                // 编辑房间配置时确保 roomConfig 有 llm 字段
                if (llm && !roomConfig.value?.llm && roomConfig.value) {
                    roomConfig.value.llm = JSON.parse(JSON.stringify(llm));
                }
                if (llm) {
                    llmEnabled.value = llm.enabled ?? false;
                    llmProvider.value = llm.provider || 'openai';
                    llmBaseUrl.value = llm.base_url || '';
                    llmModel.value = llm.model || '';
                    llmWakeWord.value = llm.wake_word || 'ayabot';
                    llmTemp.value = llm.temperature ?? 0.7;
                    llmTopP.value = llm.top_p ?? 0.9;
                    llmMaxTokens.value = llm.max_tokens ?? 150;
                    llmPrompt.value = llm.system_prompt || '';
                    llmApiKeyReal.value = llm.api_key || '';
                    llmApiKey.value = llm.api_key ? '********' : '';
                    if (llm.context) {
                        ctxEnabled.value = llm.context.enabled ?? true;
                        ctxMode.value = llm.context.mode || 'isolated';
                        ctxContent.value = llm.context.content || 'llm_only';
                        ctxMaxMsg.value = llm.context.max_messages ?? 10;
                    }
                    return;
                }
            }
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
                llmApiKeyReal.value = data.api_key || '';
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
            // 如果在房间内，同时保存到房间配置
            if (selectedRoom.value) {
                try {
                    const roomBody = JSON.parse(JSON.stringify(roomConfig.value || {}));
                    roomBody.llm = JSON.parse(JSON.stringify(body));
                    const res2 = await fetch(`/api/rooms/${selectedRoom.value.room_id}/config`, {
                        method: 'POST', headers: {'Content-Type':'application/json'},
                        credentials: 'include', body: JSON.stringify(roomBody),
                    });
                    if (res2.ok) {
                        // 更新内存中的 roomConfig
                        if (roomConfig.value) roomConfig.value.llm = JSON.parse(JSON.stringify(body));
                    }
                } catch(e) { /* room save best-effort */ }
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
                cfgPort.value = data.web_ui?.port || 19810;
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
            if (!newRoomDisplayId.value) { createRoomMsg.value = '请填写直播间号'; createRoomOk.value = false; return; }
            creatingRoom.value = true;
            createRoomMsg.value = '';
            try {
                const body = {
                    anchor_uid: newRoomUid.value,
                };
                if (newRoomName.value) body.room_name = newRoomName.value;
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
                    createRoomMsg.value = `✅ 房间 ${data.room_id} 创建成功！`;
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
                    // 应用预设模板
                    if (newRoomLlmTemplate.value || newRoomBotTemplate.value) {
                        const tplRes = await fetch('/api/templates', {credentials: 'include'});
                        const tplData = await tplRes.json();
                        const body = {};
                        if (newRoomLlmTemplate.value) {
                            const tpl = (tplData.llm_templates || []).find(t => t.name === newRoomLlmTemplate.value);
                            if (tpl) body.llm = tpl.config;
                        }
                        if (newRoomBotTemplate.value) {
                            const tpl = (tplData.bot_templates || []).find(t => t.name === newRoomBotTemplate.value);
                            if (tpl) body.features = tpl.config;
                        }
                        if (Object.keys(body).length) {
                            await fetch(`/api/rooms/${data.room_id}/config`, {
                                method: 'POST',
                                headers: {'Content-Type':'application/json'},
                                credentials: 'include',
                                body: JSON.stringify(body),
                            });
                        }
                    }
                    newRoomUid.value = 0;
                    newRoomName.value = '';
                    newRoomPort.value = 8001;
                    newRoomDisplayId.value = 0;
                    newRoomAccount.value = '';
                    newRoomLlmTemplate.value = '';
                    newRoomBotTemplate.value = '';
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
                // 确保 keyword_reply 存在（优先从 features 读取，兼容旧格式）
                const kr = data.keyword_reply || data.features?.keyword_reply;
                if (kr) {
                    roomConfig.value.keyword_reply = JSON.parse(JSON.stringify(kr));
                    roomConfig.value.keyword_reply.rules = (kr.rules || []).map(r => ({
                        ...r,
                        keywordsStr: (r.keywords || []).join(", "),
                        allowedUidsStr: (r.allowed_uids || []).join(", "),
                    }));
                } else {
                    roomConfig.value.keyword_reply = {enabled: false, cooldown: 30, rules: []};
                }
                if (!roomConfig.value.custom_fortunes) {
                    roomConfig.value.custom_fortunes = {daiji: "", zhongji: "", xiaoji: "", moji: "", xiong: "", daxiong: ""};
                }
                if (!roomConfig.value.features) roomConfig.value.features = {};
                // 转换 UID 欢迎模板 dict → entries 数组
                const wtfu = roomConfig.value.features.welcome_templates_for_uids || {};
                roomConfig.value.features.welcome_templates_for_uids_entries = Object.entries(wtfu).map(([uid, tmpl]) => ({uid: Number(uid), template: tmpl}));
                // 确保大航海欢迎模板存在
                if (!roomConfig.value.features.guard_welcome_templates) {
                    roomConfig.value.features.guard_welcome_templates = {captain: "", commander: "", governor: ""};
                }
            } catch(e) { alert('加载配置失败: ' + e.message); }
        }
        async function saveRoomConfig() {
            if (!roomConfig.value || !editingRoom.value) return;
            roomSaveMsg.value = '';
            const body = JSON.parse(JSON.stringify(roomConfig.value));
            if (body.keyword_reply && body.keyword_reply.rules) {
                body.keyword_reply.rules = body.keyword_reply.rules.map(r => ({
                    ...r,
                    keywords: r.keywordsStr ? r.keywordsStr.split(/[,，]\s*/).filter(Boolean) : [],
                    keywordsStr: undefined,
                    allowed_uids: r.allowedUidsStr ? r.allowedUidsStr.split(/[,，]\s*/).map(s => Number(s.trim())).filter(n => !isNaN(n)) : null,
                    allowedUidsStr: undefined,
                }));
            }
            // 转换 UID 欢迎 entries → dict
            if (body.features && body.features.welcome_templates_for_uids_entries) {
                const wtfu = {};
                for (const entry of body.features.welcome_templates_for_uids_entries) {
                    if (entry.uid && entry.template) wtfu[entry.uid] = entry.template;
                }
                body.features.welcome_templates_for_uids = Object.keys(wtfu).length ? wtfu : null;
                delete body.features.welcome_templates_for_uids_entries;
            }
            try {
                const res = await fetch(`/api/rooms/${editingRoom.value}/config`, {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify(body),
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

        // ── Change Password ──
        function openChangePwd() {
            showUserMenu.value = false;
            showChangePwd.value = true;
            changePwdOld.value = '';
            changePwdNew.value = '';
            changePwdConfirm.value = '';
            changePwdNewUser.value = loginUser.value;
            changePwdMsg.value = '';
            changePwdOk.value = false;
            changingPwd.value = false;
        }
        async function doChangePwd() {
            // 从 DOM 读取（id 选择器最可靠）
            const oldPwd = (document.getElementById('inpOldPwd')?.value || '').trim();
            const newPwd = (document.getElementById('inpNewPwd')?.value || '').trim();
            const confirmPwd = (document.getElementById('inpCfmPwd')?.value || '').trim();
            if (!oldPwd || !newPwd) { changePwdMsg.value = '请填写当前密码和新密码'; changePwdOk.value = false; return; }
            if (newPwd.length < 4) { changePwdMsg.value = '密码至少4位'; changePwdOk.value = false; return; }
            if (newPwd !== confirmPwd) {
                changePwdMsg.value = '两次输入的密码不一致';
                changePwdOk.value = false;
                return;
            }
            if (!changePwdNewUser.value) { changePwdMsg.value = '请输入用户名'; changePwdOk.value = false; return; }
            changePwdMsg.value = '';
            changingPwd.value = true;
            try {
                const body = {old_password: oldPwd, new_password: newPwd};
                if (changePwdNewUser.value !== loginUser.value) {
                    body.new_username = changePwdNewUser.value;
                }
                const res = await fetch('/api/user/password', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify(body),
                });
                const data = await res.json();
                if (data.ok) {
                    changePwdMsg.value = '✅ 密码已修改';
                    changePwdOk.value = true;
                    if (data.new_username) {
                        loginUser.value = data.new_username;
                    }
                    if (mustResetPwd.value) {
                        mustResetPwd.value = false;
                        changePwdMsg.value = '✅ 密码已修改，即将进入管理后台...';
                        setTimeout(() => {
                            showChangePwd.value = false;
                            loadLlmConfig();
                            loadGeneralConfig();
                            loadRooms();
                            loadAccounts();
                            loadUsers();
                            loadTemplates();
                        }, 1000);
                    } else {
                        setTimeout(() => { showChangePwd.value = false; }, 1500);
                    }
                } else {
                    changePwdMsg.value = '❌ ' + (data.error || '修改失败');
                    changePwdOk.value = false;
                    changingPwd.value = false;
                }
            } catch(e) {
                changePwdMsg.value = '❌ ' + e.message;
                changePwdOk.value = false;
                changingPwd.value = false;
            }
        }

        // ── Admin User Management ──
        async function loadAdminUsers() {
            try {
                const res = await fetch('/api/admin/users', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                const users = data.users || {};
                const list = [];
                for (const [uname, info] of Object.entries(users)) {
                    list.push({username: uname, ...info});
                }
                adminUsers.value = list;
            } catch(e) { /* ignore */ }
        }
        function openAddUser() {
            editingUser.value = null;
            userFormUsername.value = '';
            userFormPassword.value = '';
            userFormRole.value = 'user';
            userFormRooms.value = [];
            showUserForm.value = true;
            userFormMsg.value = '';
            showUserRoomDropdown.value = false;
        }
        function editUser(u) {
            editingUser.value = u;
            userFormUsername.value = u.username;
            userFormPassword.value = '';
            userFormRole.value = u.role || 'user';
            userFormRooms.value = [...(u.allowed_rooms || [])];
            showUserForm.value = true;
            userFormMsg.value = '';
            showUserRoomDropdown.value = false;
        }
        function toggleUserRoom(roomId) {
            const idx = userFormRooms.value.indexOf(roomId);
            if (idx >= 0) userFormRooms.value.splice(idx, 1);
            else userFormRooms.value.push(roomId);
        }
        async function saveUserForm() {
            if (!userFormUsername.value) { userFormMsg.value = '请填写用户名'; userFormOk.value = false; return; }
            if (!editingUser.value && !userFormPassword.value) { userFormMsg.value = '请填写密码'; userFormOk.value = false; return; }
            savingUserForm.value = true;
            userFormMsg.value = '';
            try {
                let res;
                if (editingUser.value) {
                    const body = {role: userFormRole.value, allowed_rooms: userFormRooms.value};
                    if (userFormPassword.value) body.password = userFormPassword.value;
                    res = await fetch(`/api/admin/users/${editingUser.value.username}`, {
                        method: 'PUT',
                        headers: {'Content-Type':'application/json'},
                        credentials: 'include',
                        body: JSON.stringify(body),
                    });
                } else {
                    res = await fetch('/api/admin/users', {
                        method: 'POST',
                        headers: {'Content-Type':'application/json'},
                        credentials: 'include',
                        body: JSON.stringify({
                            username: userFormUsername.value,
                            password: userFormPassword.value,
                            role: userFormRole.value,
                            allowed_rooms: userFormRooms.value,
                        }),
                    });
                }
                const data = await res.json();
                if (data.ok) {
                    userFormMsg.value = '✅ 保存成功';
                    userFormOk.value = true;
                    showUserForm.value = false;
                    editingUser.value = null;
                    await loadAdminUsers();
                } else {
                    userFormMsg.value = '❌ ' + (data.error || '保存失败');
                    userFormOk.value = false;
                }
            } catch(e) {
                userFormMsg.value = '❌ ' + e.message;
                userFormOk.value = false;
            } finally {
                savingUserForm.value = false;
            }
        }
        async function deleteUser(username) {
            if (!confirm(`确定删除用户「${username}」？`)) return;
            try {
                const res = await fetch(`/api/admin/users/${username}`, {
                    method: 'DELETE', credentials: 'include',
                });
                if (!res.ok) throw new Error((await res.text()).slice(0,80));
                await loadAdminUsers();
            } catch(e) { alert('删除失败: ' + e.message); }
        }

        // ── User Management (legacy, kept for backward compat) ──
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
                    if (info.role === 'streamer' || info.role === 'user') {
                        list.push({username: uname, ...info, rooms: info.allowed_rooms || info.rooms || []});
                    }
                }
                streamers.value = list;
                // Also load admin users for the admin tab
                if (userRole.value === 'admin') {
                    loadAdminUsers();
                }
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

        function addKeywordRule() {
            if (!roomConfig.value.keyword_reply.rules) roomConfig.value.keyword_reply.rules = [];
            roomConfig.value.keyword_reply.rules.push({keywords: [], keywordsStr: "", reply: "", match_mode: "contains", allowedUidsStr: ""});
        }
        function removeKeywordRule(idx) {
            roomConfig.value.keyword_reply.rules.splice(idx, 1);
        }
        // ── UID欢迎模板弹窗 ──
        const showUidWelcomeModal = ref(false);
        const uidWelcomeEditEntries = ref([]);
        function openUidWelcomeModal() {
            const raw = roomConfig.value.features.welcome_templates_for_uids_entries || [];
            uidWelcomeEditEntries.value = raw.map(e => {
                const entry = JSON.parse(JSON.stringify(e));
                entry.allDay = (entry.time_start === 0 && entry.time_end === 23);
                return entry;
            });
            showUidWelcomeModal.value = true;
        }
        function closeUidWelcomeModal() {
            showUidWelcomeModal.value = false;
            uidWelcomeEditEntries.value = [];
        }
        function saveUidWelcomeModal() {
            roomConfig.value.features.welcome_templates_for_uids_entries =
                JSON.parse(JSON.stringify(uidWelcomeEditEntries.value.map(e => ({
                    uid: e.uid, template: e.template,
                    time_start: e.time_start || 0, time_end: e.time_end || 23,
                }))));
            closeUidWelcomeModal();
        }
        function addUidWelcomeTemplate() {
            if (!roomConfig.value.features.welcome_templates_for_uids_entries) roomConfig.value.features.welcome_templates_for_uids_entries = [];
            roomConfig.value.features.welcome_templates_for_uids_entries.push({uid: 0, template: ""});
        }
        function removeUidWelcomeTemplate(idx) {
            roomConfig.value.features.welcome_templates_for_uids_entries.splice(idx, 1);
        }
        // ── 关键词回复弹窗 ──
        const showKeywordModal = ref(false);
        const keywordEditRules = ref([]);
        function openKeywordModal() {
            keywordEditRules.value = JSON.parse(JSON.stringify((roomConfig.value.keyword_reply.rules || []).map(r => ({
                ...r,
                keywordsStr: r.keywordsStr || (r.keywords || []).join(", "),
                allowedUidsStr: r.allowedUidsStr || (r.allowed_uids || []).join(", "),
                time_start: r.time_start || 0,
                time_end: r.time_end || 23,
                allDay: (r.time_start === 0 && r.time_end === 23),
            }))));
            showKeywordModal.value = true;
        }
        function closeKeywordModal() {
            showKeywordModal.value = false;
            keywordEditRules.value = [];
        }
        function saveKeywordModal() {
            roomConfig.value.keyword_reply.rules = JSON.parse(JSON.stringify(keywordEditRules.value.map(r => ({
                ...r,
                time_start: r.time_start || 0,
                time_end: r.time_end || 23,
            }))));
            closeKeywordModal();
        }

        // ── 欢迎多模板弹窗 ──
        const showWelcomeTplModal = ref(false);
        const welcomeTplEntries = ref([]);
        function openWelcomeTplModal() {
            welcomeTplEntries.value = JSON.parse(JSON.stringify(roomConfig.value.features.welcome_templates_list || []));
            // 补齐 allDay 字段
            welcomeTplEntries.value.forEach(e => { if (e.time_start === 0 && e.time_end === 23) e.allDay = true; else e.allDay = false; });
            showWelcomeTplModal.value = true;
        }
        function saveWelcomeTplModal() {
            const valid = welcomeTplEntries.value.filter(e => e.text && e.text.trim());
            roomConfig.value.features.welcome_templates_list = valid.length ? valid.map(e => ({text: e.text, time_start: e.time_start || 0, time_end: e.time_end || 23})) : null;
            showWelcomeTplModal.value = false;
        }
        // ── 大航海欢迎多模板弹窗 ──
        const showGuardWelcomeTplModal = ref(false);
        const guardWelcomeTplEntries = ref({captain: [], commander: [], governor: []});
        function openGuardWelcomeTplModal() {
            const src = roomConfig.value.features.guard_welcome_templates_list || {};
            function load(lk) {
                const arr = JSON.parse(JSON.stringify(src[lk] || []));
                arr.forEach(e => { if (e.time_start === 0 && e.time_end === 23) e.allDay = true; else e.allDay = false; });
                return arr;
            }
            guardWelcomeTplEntries.value = {
                captain: load('captain'),
                commander: load('commander'),
                governor: load('governor'),
            };
            showGuardWelcomeTplModal.value = true;
        }
        function saveGuardWelcomeTplModal() {
            function save(lk) {
                return (guardWelcomeTplEntries.value[lk] || []).filter(e => e.text && e.text.trim()).map(e => ({text: e.text, time_start: e.time_start || 0, time_end: e.time_end || 23}));
            }
            const out = {};
            for (const lk of ['captain', 'commander', 'governor']) {
                const valid = save(lk);
                if (valid.length) out[lk] = valid;
            }
            roomConfig.value.features.guard_welcome_templates_list = Object.keys(out).length ? out : null;
            showGuardWelcomeTplModal.value = false;
        }
        function addGuardTpl(level) {
            if (!guardWelcomeTplEntries.value[level]) guardWelcomeTplEntries.value[level] = [];
            guardWelcomeTplEntries.value[level].push({text: '', time_start: 0, time_end: 23, allDay: true});
        }
        // ── 定时消息多模板弹窗 ──
        const showPeriodicTplModal = ref(false);
        const periodicTplEntries = ref([]);
        function openPeriodicTplModal() {
            periodicTplEntries.value = JSON.parse(JSON.stringify(roomConfig.value.features.periodic_messages_list || []));
            periodicTplEntries.value.forEach(e => { if (e.time_start === 0 && e.time_end === 23) e.allDay = true; else e.allDay = false; });
            showPeriodicTplModal.value = true;
        }
        function savePeriodicTplModal() {
            const valid = periodicTplEntries.value.filter(e => e.text && e.text.trim());
            roomConfig.value.features.periodic_messages_list = valid.length ? valid.map(e => ({text: e.text, time_start: e.time_start || 0, time_end: e.time_end || 23})) : null;
            showPeriodicTplModal.value = false;
        }
        // ── 签文多文本弹窗 ──
        const showFortuneTplModal = ref(false);
        const fortuneTypes = [
            {key: 'daiji', label: '大吉', placeholder: '今天运气爆棚！'},
            {key: 'zhongji', label: '中吉', placeholder: '运势不错~'},
            {key: 'xiaoji', label: '小吉', placeholder: '平稳的一天~'},
            {key: 'moji', label: '末吉', placeholder: '平淡是福~'},
            {key: 'xiong', label: '凶', placeholder: '今天低调行事~'},
            {key: 'daxiong', label: '大凶', placeholder: '吃顿好的安慰自己~'},
        ];
        const fortuneTplEntries = ref({daiji: [], zhongji: [], xiaoji: [], moji: [], xiong: [], daxiong: []});
        function openFortuneTplModal() {
            const src = roomConfig.value.custom_fortunes || {};
            const out = {};
            for (const ft of fortuneTypes) {
                const val = src[ft.key];
                if (Array.isArray(val)) {
                    out[ft.key] = JSON.parse(JSON.stringify(val));
                } else if (typeof val === 'string' && val) {
                    out[ft.key] = [val];
                } else {
                    out[ft.key] = [];
                }
            }
            fortuneTplEntries.value = out;
            showFortuneTplModal.value = true;
        }
        function saveFortuneTplModal() {
            const out = {};
            for (const ft of fortuneTypes) {
                const arr = (fortuneTplEntries.value[ft.key] || []).filter(t => t && t.trim());
                if (arr.length) {
                    out[ft.key] = arr;
                }
            }
            roomConfig.value.custom_fortunes = Object.keys(out).length ? out : {};
            showFortuneTplModal.value = false;
        }
        function addFortuneText(key) {
            if (!fortuneTplEntries.value[key]) fortuneTplEntries.value[key] = [];
            fortuneTplEntries.value[key].push('');
        }
        // ── 计算属性: 统计条数 ──
        const guardWelcomeTotal = computed(() => {
            const gwl = roomConfig.value.features.guard_welcome_templates_list || {};
            return (gwl.captain||[]).length + (gwl.commander||[]).length + (gwl.governor||[]).length;
        });
        const fortuneTotalEntries = computed(() => {
            const cf = roomConfig.value.custom_fortunes || {};
            return Object.values(cf).reduce((sum, arr) => sum + (Array.isArray(arr) ? arr.length : (arr ? 1 : 0)), 0);
        });

        // ── 预设模板管理 ──
        async function loadTemplates() {
            try {
                const res = await fetch('/api/templates', {credentials: 'include'});
                if (res.status === 401) { loggedIn.value = false; return; }
                if (!res.ok) return;
                const data = await res.json();
                llmTemplates.value = data.llm_templates || [];
                botTemplates.value = data.bot_templates || [];
            } catch(e) { /* ignore */ }
        }
        async function saveTemplate() {
            if (!templateFormName.value) { templateFormMsg.value = '请输入模板名称'; templateFormOk.value = false; return; }
            if (!templateFormImportRoom.value) { templateFormMsg.value = '请选择要导入的房间'; templateFormOk.value = false; return; }
            const ttype = showAddTemplate.value;
            if (!ttype) return;
            savingTemplate.value = true;
            templateFormMsg.value = '';
            try {
                // 从房间导入配置
                const configRes = await fetch(`/api/rooms/${templateFormImportRoom.value}/config`, {credentials: 'include'});
                if (!configRes.ok) {
                    const errText = await configRes.text().catch(() => '');
                    throw new Error('读取房间配置失败: ' + (errText || configRes.status));
                }
                const roomCfg = await configRes.json();
                let config = {};
                if (ttype === 'llm') {
                    // AI 配置是全局的（存于 _LLM_CONFIG_DICT），使用当前表单值
                    config = {
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
                    // API key：表单里如果是掩码则用真实值
                    if (llmApiKey.value && llmApiKey.value !== '********') {
                        config.api_key = llmApiKey.value;
                    } else if (llmApiKeyReal.value) {
                        config.api_key = llmApiKeyReal.value;
                    }
                } else {
                    config = roomCfg.features || {};
                }
                const res = await fetch('/api/templates', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({type: ttype, name: templateFormName.value, config}),
                });
                if (!res.ok) throw new Error('保存失败');
                templateFormMsg.value = '✅ 模板已保存';
                templateFormOk.value = true;
                showAddTemplate.value = null;
                templateFormName.value = '';
                templateFormImportRoom.value = '';
                await loadTemplates();
            } catch(e) {
                templateFormMsg.value = '❌ ' + e.message;
                templateFormOk.value = false;
            } finally { savingTemplate.value = false; }
        }
        async function deleteTemplate(ttype, name) {
            try {
                await fetch('/api/templates', {
                    method: 'DELETE',
                    headers: {'Content-Type':'application/json'},
                    credentials: 'include',
                    body: JSON.stringify({type: ttype, name}),
                });
                await loadTemplates();
            } catch(e) { /* ignore */ }
        }
        async function restartAllBots() {
            restartingAll.value = true;
            restartAllMsg.value = '';
            try {
                const res = await fetch('/api/restart_all_bots', {method: 'POST', credentials: 'include'});
                const data = await res.json();
                if (data.ok) {
                    restartAllMsg.value = `✅ 已触发 ${data.count} 个机器人重启`;
                    restartAllOk.value = true;
                } else {
                    restartAllMsg.value = '❌ 重启失败';
                    restartAllOk.value = false;
                }
            } catch(e) {
                restartAllMsg.value = '❌ ' + e.message;
                restartAllOk.value = false;
            } finally { restartingAll.value = false; }
        }
        async function restartSingleBot(roomId) {
            try {
                await fetch(`/api/rooms/${roomId}/restart`, {method: 'POST', credentials: 'include'});
                roomSaveMsg.value = '✅ 已触发重启';
                roomSaveOk.value = true;
                setTimeout(() => { roomSaveMsg.value = ''; }, 3000);
            } catch(e) { /* ignore */ }
        }
        // ── 套用模板 ──
        async function applyTemplateToLlm() {
            const tpl = llmTemplates.value.find(t => t.name === applyLlmTemplate.value);
            if (!tpl) return;
            const c = tpl.config;
            llmEnabled.value = c.enabled ?? false;
            llmProvider.value = c.provider || 'openai';
            if (c.api_key) llmApiKey.value = c.api_key;
            llmBaseUrl.value = c.base_url || '';
            llmModel.value = c.model || '';
            llmWakeWord.value = c.wake_word || 'ayabot';
            llmTemp.value = c.temperature ?? 0.7;
            llmTopP.value = c.top_p ?? 0.9;
            llmMaxTokens.value = c.max_tokens ?? 150;
            llmPrompt.value = c.system_prompt || '';
            if (c.context) {
                ctxEnabled.value = c.context.enabled ?? true;
                ctxMode.value = c.context.mode || 'isolated';
                ctxContent.value = c.context.content || 'llm_only';
                ctxMaxMsg.value = c.context.max_messages ?? 10;
            }
            applyLlmTemplate.value = '';
            llmSaveMsg.value = '✅ 已套用模板，请点击保存生效';
            llmSaveOk.value = true;
        }
        async function applyTemplateToBot() {
            const tpl = botTemplates.value.find(t => t.name === applyBotTemplate.value);
            if (!tpl || !roomConfig.value) return;
            const c = tpl.config;
            // 套用到 roomConfig（注意先转成 entries 格式）
            for (const key of Object.keys(c)) {
                if (key === 'welcome_templates_for_uids') {
                    roomConfig.value.features.welcome_templates_for_uids_entries = Object.entries(c[key] || {}).map(([uid, tmpl]) => ({uid: Number(uid), template: tmpl}));
                } else {
                    roomConfig.value.features[key] = c[key];
                }
            }
            applyBotTemplate.value = '';
            applyTemplateMsg.value = '✅ 已套用模板，请点击保存';
            applyTemplateOk.value = true;
            setTimeout(() => { applyTemplateMsg.value = ''; }, 5000);
        }

        return {loggedIn, loginUser, loginPass, loginErr, doLogin, doLogout,
                tab, userRole,
                showUserMenu, showChangePwd, changePwdOld, changePwdNew, changePwdNewUser, changePwdMsg, changePwdOk, changingPwd,
                mustResetPwd,
                openChangePwd, doChangePwd,
                adminUsers, showUserForm, editingUser, userFormUsername, userFormPassword,
                userFormRole, userFormRooms, showUserRoomDropdown, savingUserForm, userFormMsg, userFormOk,
                openAddUser, editUser, saveUserForm, deleteUser, toggleUserRoom,
                rStart, rEnd, rType, ranking, errRanking, loadRanking,
                eUid, eName, eDate, eType, ePerCol, eColWidth, exportList, exportDates, exportCols, errExport,
                loadExport, gotoExport, loadUserDates, onUidInput, pickDate, captureExport,
                showCalendar, calYear, calMonth, calDays, exportDatesSet,
                proxyImg, fmtTime, cardBgClass, guardLabel, guardBadgeClass,
                delDate, delResult, confirmDelete,
                danmakuRows, danmakuErr, danmakuOffset, danmakuLimit, danmakuTotal, danmakuPage,
                loadDanmakuLog, clearDanmakuLog, fmtDanmakuTime,
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
                cfgSaveMsg, cfgSaveOk, loadGeneralConfig,
                restartMsg, restartOk, restartService,
                selectedRoom, roomSubTab, roomSubTabs, newRoomAccount, selectedRoomAccount,
                selectRoom, goBackRoomList, assignAccountToRoom, toggleCreateRoom, toggleNewAccount,
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
                verifyQueue, verifyingAll, verifyAllAccounts,
                streamers, allRooms, showStreamerForm, editStreamerUser, editStreamerPass,
                editStreamerRooms, savingStreamer, streamerMsg, streamerOk,
                loadUsers, saveStreamer, editStreamer, deleteStreamer, toggleStreamerRoom, showRoomDropdown,
                addUidWelcomeTemplate, removeUidWelcomeTemplate,
                loadBotLog, clearBotLog, botLogContent, logLevel, logContainer,
                showUidWelcomeModal, uidWelcomeEditEntries, openUidWelcomeModal, closeUidWelcomeModal, saveUidWelcomeModal,
                showKeywordModal, keywordEditRules, openKeywordModal, closeKeywordModal, saveKeywordModal,
                showWelcomeTplModal, welcomeTplEntries, openWelcomeTplModal, saveWelcomeTplModal,
                showGuardWelcomeTplModal, guardWelcomeTplEntries, openGuardWelcomeTplModal, saveGuardWelcomeTplModal, addGuardTpl,
                showPeriodicTplModal, periodicTplEntries, openPeriodicTplModal, savePeriodicTplModal,
                showFortuneTplModal, fortuneTypes, fortuneTplEntries, openFortuneTplModal, saveFortuneTplModal, addFortuneText,
                guardWelcomeTotal, fortuneTotalEntries,
                llmTemplates, botTemplates, showAddTemplate, templateFormName, templateFormImportRoom,
                savingTemplate, templateFormMsg, templateFormOk, newRoomLlmTemplate, newRoomBotTemplate,
                restartingAll, restartAllMsg, restartAllOk,
                loadTemplates, saveTemplate, deleteTemplate, restartAllBots, restartSingleBot,
                applyLlmTemplate, applyBotTemplate, applyTemplateMsg, applyTemplateOk,
                applyTemplateToLlm, applyTemplateToBot,
                configSections, toggleSection};
    }
}).mount('#app');
</script>
</body>
</html>
"""

# 注入版本号（从 VERSION 文件读取，一处修改全局同步）
INDEX_HTML = INDEX_HTML.replace("__AYABOT_VERSION__", _APP_VERSION)


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/favicon.ico")
async def favicon():
    favicon_path = Path(__file__).resolve().parent.parent.parent / "icon.png"
    if favicon_path.exists():
        return FastResponse(content=favicon_path.read_bytes(), media_type="image/x-icon")
    return Response(status_code=204)


# ── 打赏二维码 ──


@app.get("/alipay.jpg")
async def alipay_qr():
    p = Path(__file__).resolve().parent.parent.parent / "alipay.jpg"
    if p.exists():
        return FastResponse(content=p.read_bytes(), media_type="image/jpeg")
    return Response(status_code=404)


@app.get("/wechat.png")
async def wechat_qr():
    p = Path(__file__).resolve().parent.parent.parent / "wechat.png"
    if p.exists():
        return FastResponse(content=p.read_bytes(), media_type="image/png")
    return Response(status_code=404)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8000)
