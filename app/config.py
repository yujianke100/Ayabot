from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(slots=True)
class CooldownConfig:
    welcome_user_seconds: int
    thanks_user_seconds: int


@dataclass(slots=True)
class RateLimitConfig:
    send_interval_seconds: float
    retry_count: int
    max_queue_size: int = 50
    reply_delay_seconds: float = 3.0


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


@dataclass(slots=True)
class LLMConfig:
    enabled: bool = False
    provider: str = "openai"        # "openai" or "anthropic"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    system_prompt: str = "你是文文，一个可爱温柔的虚拟主播助手。"


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
        ),
        llm=LLMConfig(
            enabled=bool(llm.get("enabled", False)),
            provider=str(llm.get("provider", "openai")),
            api_key=str(llm.get("api_key", "")),
            base_url=str(llm.get("base_url", "https://api.openai.com/v1")),
            model=str(llm.get("model", "gpt-4o-mini")),
            system_prompt=str(llm.get("system_prompt", "你是文文，一个可爱温柔的虚拟主播助手。")),
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
