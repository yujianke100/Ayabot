from __future__ import annotations

from dataclasses import dataclass, field
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
    # 新版多模板（支持随机+时段）
    welcome_templates_list: list[dict] | None = None       # [{"text":..., "time_start":0, "time_end":23}, ...]
    guard_welcome_templates_list: dict[str, list[dict]] | None = None  # {"captain":[...], "commander":[...], "governor":[...]}
    periodic_messages_list: list[dict] | None = None       # [{"text":..., "time_start":0, "time_end":23}, ...]
    welcome_templates_for_uids: dict[int, str] | None = None  # uid -> template
    guard_welcome_templates: dict[str, str] | None = None     # captain/commander/governor -> template
    danmaku_log_enabled: bool = False
    danmaku_log_max_entries: int = 1000
    blindbox_no_gift: str = "无送礼记录"  # 无任何送礼时的回复
    blindbox_no_blindbox: str = "无盲盒记录"  # 有送礼但无盲盒时的回复
    blindbox_result_monthly: str = "本月盲盒共{count}个，花费{cost}，收益{profit}"  # 本月盲盒统计
    blindbox_result_today: str = "今日盲盒共{count}个，花费{cost}，收益{profit}"  # 今日盲盒统计
    blindbox_glassheart_enabled: bool = False  # 玻璃心模式：亏损时隐藏真实收益
    blindbox_glassheart_reply: str = "服务器繁忙，请稍后重试"  # 亏损时的回复
    use_chinese_numbers: bool = False  # 数字转中文（避开数字拦截）
    use_chinese_numbers_global: bool = False  # 全局数字转大写中文，所有回复都生效
    uid_configs: list[dict] | None = None  # list of {uid, welcome_template, keyword_rules}
    allow_bare_commands: bool = False  # 允许不带 # 前缀触发指令
    llm_bare_trigger: bool = False  # AI回复单独免#：弹幕开头匹配唤醒词即触发
    llm_keyword_trigger: bool = False  # 允许包含关键词就触发AI回复（需 AI免#前缀唤醒）
    pk_report_enabled: bool = True  # PK开始时汇报对手信息
    pk_report_template: str = "PK开始！对手{opponent}，{fans}粉，{guards}，{audience}观众，贡献{score}"  # PK汇报模板
    pk_end_template: str = "{result} 我方分数{score}"  # PK结束模板，{result}结果(胜利！/对方获胜/PK结束) {score}我方分数
    # 点赞感谢
    like_thanks_enabled: bool = False
    like_thanks_template: str = "感谢 {uname} 的点赞~"
    # 转发感谢
    share_thanks_enabled: bool = False
    share_thanks_template: str = "感谢分享直播间~"
    # 关注感谢
    follow_thanks_enabled: bool = False
    follow_thanks_template: str = "感谢 {uname} 的关注~"


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
    allowed_uids: list[int] | None = None  # None = all users
    time_start: int = 0    # 生效起始小时 0-23
    time_end: int = 23     # 生效结束小时 0-23（支持跨天）


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
    system_prompt: str = "你是ayabot，一个可爱温柔的虚拟主播助手。所有回复必须控制在40字以内。"
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
    account_uid: str = ""
    custom_fortunes: dict = field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def load_config(path: str = "config.yaml") -> AppConfig:
    """加载配置文件，path 相对于项目根目录或绝对路径.

    Args:
        path: 配置文件路径。返回的 AppConfig 中，所有相对路径
              (storage.sqlite_path, auth.credential_store_path)
              会被解析为基于配置文件所在目录的绝对路径。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}. Copy config.example.yaml to config.yaml")

    raw = _read_yaml(p)
    cfg_dir = p.resolve().parent

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

    # 解析相对路径 → 基于配置文件的绝对路径
    sqlite_path = str(storage.get("sqlite_path", "data/bot.db"))
    if not Path(sqlite_path).is_absolute():
        sqlite_path = str(cfg_dir / sqlite_path)

    cred_store_path = str(auth.get("credential_store_path", "data/credential.json"))
    if not Path(cred_store_path).is_absolute():
        cred_store_path = str(cfg_dir / cred_store_path)

    return AppConfig(
        room_display_id=int(raw["room_display_id"]),
        anchor_uid=int(raw["anchor_uid"]),
        account_uid=str(raw.get("account_uid", "")),
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
            welcome_templates_for_uids=_ensure_int_keys(features.get("welcome_templates_for_uids", None) or None),
            guard_welcome_templates=features.get("guard_welcome_templates", None) or None,
            # 新版多模板（支持随机+时段）
            welcome_templates_list=_normalize_template_list(features.get("welcome_templates_list") or features.get("welcome_templates")),
            guard_welcome_templates_list=_normalize_guard_template_list(features.get("guard_welcome_templates_list") or features.get("guard_welcome_templates")),
            periodic_messages_list=_normalize_template_list(features.get("periodic_messages_list") or features.get("periodic_messages")),
            danmaku_log_enabled=bool(features.get("danmaku_log_enabled", False)),
            danmaku_log_max_entries=int(features.get("danmaku_log_max_entries", 1000)),
            uid_configs=features.get("uid_configs", None) or None,
            allow_bare_commands=bool(features.get("allow_bare_commands", False)),
            llm_bare_trigger=bool(features.get("llm_bare_trigger", False)),
            llm_keyword_trigger=bool(features.get("llm_keyword_trigger", False)),
            blindbox_no_gift=str(features.get("blindbox_no_gift", "无送礼记录")),
            blindbox_no_blindbox=str(features.get("blindbox_no_blindbox", "无盲盒记录")),
            blindbox_result_monthly=str(features.get("blindbox_result_monthly", "本月盲盒共{count}个，花费{cost}，收益{profit}")),
            blindbox_result_today=str(features.get("blindbox_result_today", "今日盲盒共{count}个，花费{cost}，收益{profit}")),
            blindbox_glassheart_enabled=bool(features.get("blindbox_glassheart_enabled", False)),
            blindbox_glassheart_reply=str(features.get("blindbox_glassheart_reply", "服务器繁忙，请稍后重试")),
            use_chinese_numbers=bool(features.get("use_chinese_numbers", False)),
            use_chinese_numbers_global=bool(features.get("use_chinese_numbers_global", False)),
            pk_report_enabled=bool(features.get("pk_report_enabled", True)),
            pk_report_template=str(features.get("pk_report_template", "PK开始！对手{opponent}，{fans}粉，{guards}，{audience}观众，贡献{score}")),
            pk_end_template=str(features.get("pk_end_template", "{result} 我方分数{score}")),
            like_thanks_enabled=bool(features.get("like_thanks_enabled", False)),
            like_thanks_template=str(features.get("like_thanks_template", "感谢 {uname} 的点赞~")),
            share_thanks_enabled=bool(features.get("share_thanks_enabled", False)),
            share_thanks_template=str(features.get("share_thanks_template", "感谢分享直播间~")),
            follow_thanks_enabled=bool(features.get("follow_thanks_enabled", False)),
            follow_thanks_template=str(features.get("follow_thanks_template", "感谢 {uname} 的关注~")),
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
            sqlite_path=sqlite_path,
        ),
        auth=AuthConfig(
            credential_store_path=cred_store_path,
            auto_login=bool(auth.get("auto_login", True)),
            qr_poll_seconds=float(auth.get("qr_poll_seconds", 1.0)),
            refresh_interval_seconds=int(auth.get("refresh_interval_seconds", 3600)),
        ),
        web_ui=WebUIConfig(
            enabled=bool(web_ui.get("enabled", True)),
            host=str(web_ui.get("host", "0.0.0.0")),
            port=int(web_ui.get("port", 19810)),
            username=str(web_ui.get("username", "ayabot")),
            password=str(web_ui.get("password", "123456")),
            session_timeout=int(web_ui.get("session_timeout", 3600)),
            title=str(web_ui.get("title", "Ayabot")),
            bot_name=str(web_ui.get("bot_name", "bot")),
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
            system_prompt=str(llm.get("system_prompt", "你是ayabot，一个可爱温柔的虚拟主播助手。所有回复必须控制在40字以内。")),
            context=LLMContextConfig(
                enabled=bool(llm_ctx.get("enabled", True)),
                mode=str(llm_ctx.get("mode", "isolated")),
                content=str(llm_ctx.get("content", "llm_only")),
                max_messages=int(llm_ctx.get("max_messages", 10)),
            ),
        ),
        custom_fortunes=raw.get("custom_fortunes", {}),
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


def _ensure_int_keys(d: dict | None) -> dict[int, str] | None:
    """将 YAML 加载后可能为字符串的 dict 键统一转为 int."""
    if not d:
        return None
    result: dict[int, str] = {}
    for k, v in d.items():
        try:
            result[int(k)] = str(v)
        except (ValueError, TypeError):
            result[int(k) if isinstance(k, (int, str)) else 0] = str(v)
    return result if result else None


def _normalize_template_list(raw: Any) -> list[dict] | None:
    """标准化模板列表为统一格式 [{"text":..., "time_start":0, "time_end":23}, ...]
    支持输入：单字符串、dict列表、None
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        # 单字符串 → 转为一条全天候模板
        return [{"text": raw, "time_start": 0, "time_end": 23}]
    if isinstance(raw, list):
        result = []
        for item in raw:
            if isinstance(item, str):
                result.append({"text": item, "time_start": 0, "time_end": 23})
            elif isinstance(item, dict):
                entry = {
                    "text": str(item.get("text", "")),
                    "time_start": int(item.get("time_start", 0)),
                    "time_end": int(item.get("time_end", 23)),
                }
                if entry["text"]:
                    result.append(entry)
        return result if result else None
    return None


def _normalize_guard_template_list(raw: Any) -> dict[str, list[dict]] | None:
    """标准化大航海欢迎模板。
    旧格式: {"captain": "模板", "commander": "模板", "governor": "模板"}
    新格式: {"captain": [{"text":"模板","time_start":0,"time_end":23}], ...}
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        result = {}
        for level in ("captain", "commander", "governor"):
            val = raw.get(level)
            if val is None:
                continue
            if isinstance(val, str):
                result[level] = [{"text": val, "time_start": 0, "time_end": 23}]
            elif isinstance(val, list):
                entries = []
                for item in val:
                    if isinstance(item, str):
                        entries.append({"text": item, "time_start": 0, "time_end": 23})
                    elif isinstance(item, dict):
                        entry = {
                            "text": str(item.get("text", "")),
                            "time_start": int(item.get("time_start", 0)),
                            "time_end": int(item.get("time_end", 23)),
                        }
                        if entry["text"]:
                            entries.append(entry)
                if entries:
                    result[level] = entries
        return result if result else None
    return None


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
            "welcome_templates_for_uids": config.features.welcome_templates_for_uids,
            "guard_welcome_templates": config.features.guard_welcome_templates,
            "welcome_templates_list": config.features.welcome_templates_list,
            "guard_welcome_templates_list": config.features.guard_welcome_templates_list,
            "periodic_messages_list": config.features.periodic_messages_list,
            "danmaku_log_enabled": config.features.danmaku_log_enabled,
            "danmaku_log_max_entries": config.features.danmaku_log_max_entries,
            "uid_configs": config.features.uid_configs,
            "allow_bare_commands": config.features.allow_bare_commands,
            "llm_bare_trigger": config.features.llm_bare_trigger,
            "llm_keyword_trigger": config.features.llm_keyword_trigger,
            "blindbox_no_gift": config.features.blindbox_no_gift,
            "blindbox_no_blindbox": config.features.blindbox_no_blindbox,
            "blindbox_result_monthly": config.features.blindbox_result_monthly,
            "blindbox_result_today": config.features.blindbox_result_today,
            "blindbox_glassheart_enabled": config.features.blindbox_glassheart_enabled,
            "blindbox_glassheart_reply": config.features.blindbox_glassheart_reply,
            "use_chinese_numbers": config.features.use_chinese_numbers,
            "use_chinese_numbers_global": config.features.use_chinese_numbers_global,
            "pk_report_enabled": config.features.pk_report_enabled,
            "pk_report_template": config.features.pk_report_template,
            "pk_end_template": config.features.pk_end_template,
            "like_thanks_enabled": config.features.like_thanks_enabled,
            "like_thanks_template": config.features.like_thanks_template,
            "share_thanks_enabled": config.features.share_thanks_enabled,
            "share_thanks_template": config.features.share_thanks_template,
            "follow_thanks_enabled": config.features.follow_thanks_enabled,
            "follow_thanks_template": config.features.follow_thanks_template,
        },
        "keyword_reply": {
            "enabled": config.features.keyword_reply.enabled,
            "cooldown": config.features.keyword_reply.cooldown_seconds,
            "rules": [
                {
                    "keywords": rule.keywords,
                    "reply": rule.reply,
                    "match_mode": rule.match_mode,
                    "allowed_uids": rule.allowed_uids,
                }
                for rule in config.features.keyword_reply.rules
            ],
        },
        "runtime": {
            "poll_interval_seconds": config.runtime.poll_interval_seconds,
            "timezone": config.runtime.timezone,
            "log_level": config.runtime.log_level,
        },
        "custom_fortunes": config.custom_fortunes,
        "web_ui": {
            "host": config.web_ui.host,
            "port": config.web_ui.port,
        },
        "llm": {
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
        if "account_uid" in raw:
            existing["account_uid"] = raw["account_uid"]
        if "room_name" in raw:
            existing["room_name"] = raw["room_name"]
        if "web_ui" in raw:
            existing.setdefault("web_ui", {}).update(raw["web_ui"])
        if "keyword_reply" in raw:
            existing.setdefault("features", {})["keyword_reply"] = raw["keyword_reply"]
        if "custom_fortunes" in raw:
            existing["custom_fortunes"] = raw["custom_fortunes"]
        if "llm" in raw:
            existing.setdefault("llm", {}).update(raw["llm"])
        if "runtime" in raw:
            existing.setdefault("runtime", {}).update(raw["runtime"])

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
            allowed_raw = item.get("allowed_uids")
            allowed_uids = None
            if isinstance(allowed_raw, list):
                allowed_uids = [int(u) for u in allowed_raw if isinstance(u, (int, str)) and str(u).isdigit()]
            rules.append(KeywordRule(
                keywords=[str(k) for k in keywords],
                reply=reply,
                match_mode=str(item.get("match_mode", "contains")),
                allowed_uids=allowed_uids,
                time_start=int(item.get("time_start", 0)),
                time_end=int(item.get("time_end", 23)),
            ))
    return KeywordReplyConfig(
        enabled=enabled,
        cooldown_seconds=cooldown,
        rules=rules,
    )


# ── 房间隔离支持 ──

DEFAULT_ROOMS_DIR = "rooms"


def get_room_path(room_id: str | int, base_dir: str | Path | None = None) -> Path:
    """返回房间目录路径: <base_dir>/rooms/<room_id>/"""
    base = Path(base_dir or ".").resolve()
    return base / DEFAULT_ROOMS_DIR / str(room_id)


def load_room_config(room_id: str | int, base_dir: str | Path | None = None) -> AppConfig:
    """按房间加载配置: rooms/<room_id>/config.yaml"""
    room_dir = get_room_path(room_id, base_dir)
    cfg_path = room_dir / "config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Room config not found: {cfg_path}. "
            f"Create room config first or use '--room init'."
        )
    return load_config(str(cfg_path))


def ensure_room_dirs(room_id: str | int, base_dir: str | Path | None = None) -> Path:
    """
    确保房间目录结构存在，返回房间目录 Path。

    目录结构:
        rooms/<room_id>/
            config.yaml     ← 房间配置（由外部或模板创建）
            data/
                bot.db      ← 房间专用数据库
                credential.json
    """
    room_dir = get_room_path(room_id, base_dir)
    (room_dir / "data").mkdir(parents=True, exist_ok=True)
    return room_dir


def migrate_legacy_data(room_id: str | int, base_dir: str | Path | None = None) -> bool:
    """
    如果检测到旧的 data/bot.db，自动迁移到 rooms/<room_id>/data/bot.db。
    返回 True 表示执行了迁移。
    """
    base = Path(base_dir or ".").resolve()
    old_db = base / "data" / "bot.db"
    old_credential = base / "data" / "credential.json"
    target_dir = get_room_path(room_id, base) / "data"
    target_dir.mkdir(parents=True, exist_ok=True)

    migrated = False
    if old_db.exists() and not (target_dir / "bot.db").exists():
        import shutil
        shutil.move(str(old_db), str(target_dir / "bot.db"))
        logging.getLogger("config").info("migrated data/bot.db → %s", target_dir / "bot.db")
        migrated = True

    if old_credential.exists() and not (target_dir / "credential.json").exists():
        import shutil
        shutil.move(str(old_credential), str(target_dir / "credential.json"))
        logging.getLogger("config").info("migrated data/credential.json → %s", target_dir / "credential.json")
        migrated = True

    return migrated
