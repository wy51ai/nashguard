"""
Base Agent

Wraps LLM API calls with structured JSON output via tool use.
Uses litellm as a unified backend — supports any provider:

  Provider         Model name example
  ────────         ─────────────────────────────────────────
  Anthropic        claude-haiku-4-5-20251001
  OpenAI           gpt-4o-mini  /  gpt-4o
  Google Gemini    gemini/gemini-2.0-flash
  Mistral          mistral/mistral-small-latest
  Ollama (local)   ollama/llama3.2  (no API key needed)
  DeepSeek         deepseek/deepseek-chat

Set the corresponding API key env var for each provider:
  ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY ...
Ollama runs locally — no key required.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Type, TypeVar

import litellm
from pydantic import BaseModel

litellm.set_verbose = False          # suppress per-request debug noise
litellm.drop_params = True           # silently drop unsupported params per provider

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class BaseAgent:
    """LLM-agnostic agent with structured JSON output via tool use (litellm)."""

    def __init__(
        self,
        model: str,
        system_prompt: str,
        api_key: str = "",          # optional — litellm also reads env vars directly
        name: str = "agent",
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.api_key = api_key      # passed as litellm `api_key` kwarg when non-empty
        self.name = name
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _call(self, user_message: str, response_model: Type[T]) -> T:
        """
        Call the LLM and parse the response into `response_model`.

        Uses the tool-calling interface (supported by Claude, GPT-4o, Gemini,
        Mistral, etc.) to enforce structured JSON output.  Falls back to
        JSON-mode parsing if the provider does not return a proper tool call.
        """
        schema = response_model.model_json_schema()
        tool_name = f"submit_{response_model.__name__.lower()}"

        tools = [
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": f"Submit your {response_model.__name__} as structured output.",
                    "parameters": schema,
                },
            }
        ]

        log.debug("[%s] Calling %s", self.name, self.model)

        kwargs: Dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user",   "content": user_message},
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        if self.api_key:
            kwargs["api_key"] = self.api_key

        response = litellm.completion(**kwargs)

        msg = response.choices[0].message

        # ── Path 1: tool_calls (OpenAI / Claude / Gemini / Mistral) ──────────
        if msg.tool_calls:
            for tc in msg.tool_calls:
                raw = tc.function.arguments
                data = json.loads(raw) if isinstance(raw, str) else raw
                return response_model.model_validate(data)

        # ── Path 2: content fallback (some providers / older models) ─────────
        content = msg.content or ""
        # Strip markdown code fences if present
        if "```" in content:
            lines = content.split("\n")
            content = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )
        try:
            return response_model.model_validate_json(content.strip())
        except Exception:
            pass

        raise RuntimeError(
            f"[{self.name}] Model did not return a parseable structured response. "
            f"Raw content: {msg.content!r}"
        )
