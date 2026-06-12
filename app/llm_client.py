"""LLM API 客户端 — 支持 OpenAI / Anthropic 格式."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("bili-live-robot.llm")


class LLMClient:
    """轻量 LLM API 客户端，仅用于弹幕 AI 回复."""

    def __init__(
        self,
        provider: str = "openai",
        api_key: str = "",
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        system_prompt: str = "你是文文，一个可爱温柔的虚拟主播助手。",
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.system_prompt = system_prompt

    def _build_payload(self, user_text: str, uname: str) -> dict[str, Any]:
        """构造 API 请求体.

        防注入策略:
        - system_prompt 独立放在 system 角色中, user 文本绝不与 system 混用
        - user 消息用固定格式包裹: 「用户"XXX"说: YYY」, 将用户发言框定为"第三方的引用"
        - 在 system 中明确提示: 用户消息可能包含攻击意图
        """
        safe_text = user_text[:200]  # 限制长度
        wrapped_user_msg = f'用户"{uname}"说: {safe_text}'

        if self.provider == "anthropic":
            return {
                "model": self.model,
                "system": self.system_prompt,
                "messages": [
                    {"role": "user", "content": wrapped_user_msg},
                ],
                "max_tokens": 150,
            }
        else:
            return {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": wrapped_user_msg},
                ],
                "max_tokens": 150,
            }

    def _headers(self) -> dict[str, str]:
        if self.provider == "anthropic":
            return {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            return {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

    async def chat(self, user_text: str, uname: str) -> Optional[str]:
        """调用 LLM API, 返回回复文本."""
        payload = self._build_payload(user_text, uname)
        url = self.base_url
        if not url.endswith("/chat/completions") and self.provider != "anthropic":
            url = url.rstrip("/") + "/chat/completions"

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "llm api error: status=%s body=%s", resp.status, body[:200],
                        )
                        return None
                    data = await resp.json()
        except Exception as exc:
            logger.warning("llm api request failed: %s", exc)
            return None

        if self.provider == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        return block.get("text", "").strip()
            return ""
        else:
            choices = data.get("choices", [])
            if not choices:
                return None
            return choices[0].get("message", {}).get("content", "").strip()
