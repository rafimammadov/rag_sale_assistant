from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings


class ChatProvider(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class MockChatProvider:
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        source_match = re.search(
            r"### \[S1\].*?\n(.*?)(?=\n### \[S2\]|\nCustomer request:|\Z)",
            user_prompt,
            flags=re.S,
        )
        excerpt = ""
        if source_match:
            excerpt = re.sub(r"\s+", " ", source_match.group(1)).strip()[:650]
        if excerpt:
            answer = f"Here is the relevant catalog information: {excerpt[:280]} [S1]"
        else:
            answer = (
                "I do not have enough verified information to answer that accurately. "
                "Please ask the company to confirm it."
            )
        return json.dumps(
            {
                "answer": answer,
                "recommendations": [],
                "next_question": "What will you use the product for?",
                "should_offer_quote": False,
            },
            ensure_ascii=False,
        )


class OpenAIChatProvider:
    def __init__(self, settings: Settings):
        from openai import OpenAI

        self.settings = settings
        self.client = OpenAI(
            api_key=settings.llm_api_key or "not-set",
            base_url=settings.llm_base_url or None,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        kwargs = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.settings.llm_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or "{}"


def parse_json_response(raw: str) -> dict:
    value = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", value, flags=re.S)
    if fenced:
        value = fenced.group(1)
    if not value.startswith("{"):
        match = re.search(r"\{.*\}", value, flags=re.S)
        if match:
            value = match.group(0)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {"answer": raw.strip()}


@lru_cache
def get_chat_provider() -> ChatProvider:
    settings = get_settings()
    provider = settings.llm_provider.casefold()
    if provider == "mock":
        return MockChatProvider()
    if provider in {"openai", "openai-compatible"}:
        return OpenAIChatProvider(settings)
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
