from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class CredentialConfig:
    sessdata: str
    bili_jct: str
    buvid3: str
    dedeuserid: str


@dataclass(slots=True)
class AnchorExclusiveRule:
    trigger_keyword: str
    reply_template: str
    is_regex: bool = False


@dataclass(slots=True)
class FeatureConfig:
    welcome_enabled: bool
    welcome_template: str
    thanks_enabled: bool
    thanks_template: str
    blindbox_enabled: bool
    allow_admin_as_anchor: bool
    guard_thanks_enabled: bool
    guard_thanks_template_captain: str
    guard_thanks_template_commander: str
    guard_thanks_template_governor: str
    guard_thanks_template_default: str
    anchor_exclusive_reply: list[AnchorExclusiveRule]
    keyword_reply: KeywordReplyConfig
    connected_message: str
    connected_message_enabled: bool
    periodic_message_enabled: bool = True
    periodic_message_interval_seconds: int = 600
    periodic_message_template: str = ""


@dataclass(slots=True)
class CooldownConfig:
    welcome_user_seconds: int
    thanks_user_seconds: int


@dataclass(slots=True)
class RateLimitConfig:
    send_interval_seconds: float
    retry_count: int
    max_queue_size: int = 50
    reply_delay_seconds: float = 1.0


@dataclass(slots=True)
class KeywordRule:
    keywords: list[str]
    reply: str
    match_mode: str = "contains"


@dataclass(slots=True)
class KeywordReplyConfig:
    enabled: bool
    cooldown_seconds: int
    rules: list[KeywordRule]


@dataclass(slots=True)
class RuntimeConfig:
    poll_interval_seconds: int
    timezone: str
    log_level: str


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: str


@dataclass(slots=True)
class AuthConfig:
    credential_store_path: str
    auto_login: bool
    qr_poll_seconds: float
    refresh_interval_seconds: int


@dataclass(slots=True)
class WebUIConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    session_timeout: int
    title: str
    bot_name: str = "ayabot"


@dataclass(slots=True)
class LLMContextConfig:
    enabled: bool = True        # 上下文记忆是否开启
    mode: str = "isolated"       # "isolated"=单用户隔离, "merged"=所有用户合并
    content: str = "llm_only"    # "llm_only"=仅文文对话, "all"=所有弹幕
    max_messages: int = 10       # 保留的消息条数


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    provider: str = "openai"        # "openai" or "anthropic"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    wake_word: str = "ayabot"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 150
    system_prompt: str = "你是ayabot，一个可爱温柔的虚拟主播助手。"
    context: LLMContextConfig = None    # type: ignore[assignment]


@dataclass(slots=True)
class AppConfig:
    room_display_id: int
    anchor_uid: int
    credential: CredentialConfig
    features: FeatureConfig
    cooldown: CooldownConfig
    rate_limit: RateLimitConfig
    runtime: RuntimeConfig
    storage: StorageConfig
    auth: AuthConfig
    web_ui: WebUIConfig
    llm: LLMConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def load_config(path: str = "config.yaml") -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}. Copy config.example.yaml to config.yaml")

    raw = _read_yaml(p)

    credential = raw.get("credential", {})
    features = raw.get("features", {})
    cooldown = raw.get("cooldown", {})
    rate_limit = raw.get("rate_limit", {})
    runtime = raw.get("runtime", {})
    storage = raw.get("storage", {})
    auth = raw.get("auth", {})
    web_ui = raw.get("web_ui", {})
    llm = raw.get("llm", {})
    llm_ctx = llm.get("context", {})

    return AppConfig(
        room_display_id=int(raw["room_display_id"]),
        anchor_uid=int(raw["anchor_uid"]),
        credential=CredentialConfig(
            sessdata=str(credential.get("sessdata", "")),
            bili_jct=str(credential.get("bili_jct", "")),
            buvid3=str(credential.get("buvid3", "")),
            dedeuserid=str(credential.get("dedeuserid", "")),
        ),
        features=FeatureConfig(
            welcome_enabled=bool(features.get("welcome_enabled", True)),
            welcome_template=str(features.get("welcome_template", "Welcome {uname}")),
            thanks_enabled=bool(features.get("thanks_enabled", True)),
            thanks_template=str(
                features.get(
                    "thanks_template",
                    "感谢 {uname} 送出的 {gift_name} x{gift_num}",
                )
            ),
            blindbox_enabled=bool(features.get("blindbox_enabled", True)),
            allow_admin_as_anchor=bool(features.get("allow_admin_as_anchor", False)),
            guard_thanks_enabled=bool(features.get("guard_thanks_enabled", True)),
            guard_thanks_template_captain=str(
                features.get(
                    "guard_thanks_template_captain",
                    "Thanks {uname} for buying Captain x{months}",
                )
            ),
            guard_thanks_template_commander=str(
                features.get(
                    "guard_thanks_template_commander",
                    "Thanks {uname} for buying Commander x{months}",
                )
            ),
            guard_thanks_template_governor=str(
                features.get(
                    "guard_thanks_template_governor",
                    "Thanks {uname} for buying Governor x{months}",
                )
            ),
            guard_thanks_template_default=str(
                features.get(
                    "guard_thanks_template_default",
                    "Thanks {uname} for guard support ({guard_type}) x{months}",
                )
            ),
            anchor_exclusive_reply=_parse_anchor_reply(features.get("anchor_exclusive_reply", {})),
            keyword_reply=_parse_keyword_reply(features.get("keyword_reply", {})),
            connected_message=str(features.get("connected_message", "")),
            connected_message_enabled=bool(features.get("connected_message_enabled", True)),
            periodic_message_enabled=bool(features.get("periodic_message_enabled", True)),
            periodic_message_interval_seconds=int(features.get("periodic_message_interval_seconds", 600)),
            periodic_message_template=str(features.get("periodic_message_template", "")),
        ),
        cooldown=CooldownConfig(
            welcome_user_seconds=int(cooldown.get("welcome_user_seconds", 600)),
            thanks_user_seconds=int(cooldown.get("thanks_user_seconds", 10)),
        ),
        rate_limit=RateLimitConfig(
            send_interval_seconds=float(rate_limit.get("send_interval_seconds", 1.2)),
            retry_count=int(rate_limit.get("retry_count", 2)),
        ),
        runtime=RuntimeConfig(
            poll_interval_seconds=int(runtime.get("poll_interval_seconds", 20)),
            timezone=str(runtime.get("timezone", "Asia/Shanghai")),
            log_level=str(runtime.get("log_level", "INFO")),
        ),
        storage=StorageConfig(
            sqlite_path=str(storage.get("sqlite_path", "data/bot.db")),
        ),
        auth=AuthConfig(
            credential_store_path=str(auth.get("credential_store_path", "data/credential.json")),
            auto_login=bool(auth.get("auto_login", True)),
            qr_poll_seconds=float(auth.get("qr_poll_seconds", 1.0)),
            refresh_interval_seconds=int(auth.get("refresh_interval_seconds", 3600)),
        ),
        web_ui=WebUIConfig(
            enabled=bool(web_ui.get("enabled", True)),
            host=str(web_ui.get("host", "0.0.0.0")),
            port=int(web_ui.get("port", 8000)),
            username=str(web_ui.get("username", "admin")),
            password=str(web_ui.get("password", "admin")),
            session_timeout=int(web_ui.get("session_timeout", 3600)),
            title=str(web_ui.get("title", "BILIBILI-LIVE-ROBOT")),
            bot_name=str(web_ui.get("bot_name", "ayabot")),
        ),
        llm=LLMConfig(
            enabled=bool(llm.get("enabled", False)),
            provider=str(llm.get("provider", "openai")),
            api_key=str(llm.get("api_key", "")),
            base_url=str(llm.get("base_url", "https://api.openai.com/v1")),
            model=str(llm.get("model", "gpt-4o-mini")),
            wake_word=str(llm.get("wake_word", "ayabot")),
            temperature=float(llm.get("temperature", 0.7)),
            top_p=float(llm.get("top_p", 0.9)),
            max_tokens=int(llm.get("max_tokens", 150)),
            system_prompt=str(llm.get("system_prompt", "你是ayabot，一个可爱温柔的虚拟主播助手。")),
            context=LLMContextConfig(
                enabled=bool(llm_ctx.get("enabled", True)),
                mode=str(llm_ctx.get("mode", "isolated")),
                content=str(llm_ctx.get("content", "llm_only")),
                max_messages=int(llm_ctx.get("max_messages", 10)),
            ),
        ),
    )


def _parse_anchor_reply(raw: Any) -> list[AnchorExclusiveRule]:
    if not isinstance(raw, list):
        return []
    
    rules: list[AnchorExclusiveRule] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rules.append(AnchorExclusiveRule(
            trigger_keyword=str(item.get("trigger_keyword", "")),
            reply_template=str(item.get("reply_template", "")),
            is_regex=bool(item.get("is_regex", False)),
        ))
    return rules


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """将 AppConfig 序列化为 dict, 用于 Web UI 前端展示."""
    return {
        "bot_name": config.web_ui.bot_name,
        "room_display_id": config.room_display_id,
        "anchor_uid": config.anchor_uid,
        "cooldown": {
            "welcome_user_seconds": config.cooldown.welcome_user_seconds,
            "thanks_user_seconds": config.cooldown.thanks_user_seconds,
        },
        "rate_limit": {
            "send_interval_seconds": config.rate_limit.send_interval_seconds,
            "retry_count": config.rate_limit.retry_count,
            "max_queue_size": config.rate_limit.max_queue_size,
            "reply_delay_seconds": config.rate_limit.reply_delay_seconds,
        },
        "features": {
            "welcome_enabled": config.features.welcome_enabled,
            "welcome_template": config.features.welcome_template,
            "thanks_enabled": config.features.thanks_enabled,
            "thanks_template": config.features.thanks_template,
            "blindbox_enabled": config.features.blindbox_enabled,
            "guard_thanks_enabled": config.features.guard_thanks_enabled,
            "guard_thanks_template_captain": config.features.guard_thanks_template_captain,
            "guard_thanks_template_commander": config.features.guard_thanks_template_commander,
            "guard_thanks_template_governor": config.features.guard_thanks_template_governor,
            "guard_thanks_template_default": config.features.guard_thanks_template_default,
            "connected_message": config.features.connected_message,
            "connected_message_enabled": config.features.connected_message_enabled,
            "periodic_message_enabled": config.features.periodic_message_enabled,
            "periodic_message_interval_seconds": config.features.periodic_message_interval_seconds,
            "periodic_message_template": config.features.periodic_message_template,
        },
        "web_ui": {
            "host": config.web_ui.host,
            "port": config.web_ui.port,
        },
    }


def update_config_from_dict(raw: dict[str, Any], cfg_path: str) -> bool:
    """从 dict 更新 config.yaml (覆盖写入)."""
    try:
        import yaml
        from pathlib import Path

        existing = yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}

        if "cooldown" in raw:
            existing.setdefault("cooldown", {}).update(raw["cooldown"])
        if "rate_limit" in raw:
            existing.setdefault("rate_limit", {}).update(raw["rate_limit"])
        if "features" in raw:
            existing.setdefault("features", {}).update(raw["features"])
        if "templates" in raw:
            existing.setdefault("features", {}).update(raw["templates"])
        if "room_display_id" in raw:
            existing["room_display_id"] = raw["room_display_id"]
        if "anchor_uid" in raw:
            existing["anchor_uid"] = raw["anchor_uid"]
        if "bot_name" in raw:
            existing.setdefault("web_ui", {})["bot_name"] = raw["bot_name"]
        if "web_ui" in raw:
            existing.setdefault("web_ui", {}).update(raw["web_ui"])

        Path(cfg_path).write_text(
            yaml.dump(existing, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger = logging.getLogger("config")
        logger.warning("config save failed: %s", exc)
        return False


def _parse_keyword_reply(raw: Any) -> KeywordReplyConfig:
    if not isinstance(raw, dict):
        return KeywordReplyConfig(enabled=False, cooldown_seconds=30, rules=[])
    enabled = bool(raw.get("enabled", False))
    cooldown = int(raw.get("cooldown", 30))
    rules_raw = raw.get("rules", [])
    rules: list[KeywordRule] = []
    if isinstance(rules_raw, list):
        for item in rules_raw:
            if not isinstance(item, dict):
                continue
            keywords = item.get("keywords")
            if not isinstance(keywords, list) or not keywords:
                continue
            reply = str(item.get("reply", ""))
            if not reply:
                continue
            rules.append(KeywordRule(
                keywords=[str(k) for k in keywords],
                reply=reply,
                match_mode=str(item.get("match_mode", "contains")),
            ))
    return KeywordReplyConfig(
        enabled=enabled,
        cooldown_seconds=cooldown,
        rules=rules,
    )
