"""Shared chat-LLM client for LLM-based KB indexing and retrieval.

Model resolution order: explicit ``model=`` param > ``TAU2_KB_LLM_MODEL``
env var > DEFAULT_KB_LLM_MODEL. Keeps the model swappable (gpt-5.4-nano
today, anything OpenAI-compatible tomorrow) without touching call sites.
"""

import os
from typing import List, Optional, Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_KB_LLM_MODEL = "gpt-5.4-nano"


def get_kb_llm_model() -> str:
    return os.getenv("TAU2_KB_LLM_MODEL", DEFAULT_KB_LLM_MODEL)


class KBLLMClient:
    """Minimal wrapper: one structured-output call, one plain-text call."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.model = model or get_kb_llm_model()
        self.client = OpenAI(
            api_key=api_key
            or os.getenv("LEANMCP_API_KEY")
            or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("LEANMCP_OPENAI_GATEWAY"),
        )

    def parse(
        self,
        prompt: str,
        schema: Type[T],
        system: Optional[str] = None,
    ) -> T:
        messages: List[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.parse_messages(messages, schema)

    def parse_messages(self, messages: List[dict], schema: Type[T]) -> T:
        """Structured-output call with caller-controlled message list.

        Callers that want provider-side prompt caching should put the large
        static content (system prompt, index, documents) in the FIRST
        messages and the per-call variable part (the query) in the LAST
        message — providers cache the longest byte-identical message prefix.
        """
        response = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=schema,
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError(
                f"LLM returned unparseable output for schema {schema.__name__}"
            )
        return parsed

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        messages: List[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        return response.choices[0].message.content or ""
