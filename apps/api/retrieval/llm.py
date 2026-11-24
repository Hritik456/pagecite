"""Chat-model access behind a small protocol (tests use a fake).

Timeouts are explicit so a hung upstream cannot hold a gunicorn thread:
- JSON calls (decompose, rerank): `json_timeout_s`, 1 SDK retry.
- The answer stream: `answer_timeout_s` per read AND as a total deadline, 0 SDK retries
  (a retried stream would repeat tokens the client already received).
Nothing here logs prompts, completions or the API key.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


import openai
from openai import OpenAI


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    @classmethod
    def from_openai(cls, u) -> "Usage":
        if u is None:
            return cls()
        details = getattr(u, "completion_tokens_details", None)
        prompt_details = getattr(u, "prompt_tokens_details", None)
        return cls(
            prompt_tokens=u.prompt_tokens or 0,
            completion_tokens=u.completion_tokens or 0,
            reasoning_tokens=(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
            cached_tokens=(getattr(prompt_details, "cached_tokens", 0) or 0) if prompt_details else 0,
        )


class LLMError(Exception):
    """The model call failed (HTTP error, bad output). `code` goes into logs and error events."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class LLMTimeout(LLMError):
    def __init__(self, message: str):
        super().__init__("UPSTREAM_TIMEOUT", message)


class LLM(Protocol):
    def json(
        self, model: str, system: str, user: str, schema_name: str, schema: dict, max_tokens: int,
        reasoning_effort: str | None = None,
    ) -> tuple[dict, Usage]: ...

    def stream(
        self, model: str, system: str, user: str, max_tokens: int, reasoning_effort: str | None = None,
    ) -> Iterator[str | Usage]:
        """Yields text deltas, then exactly one Usage."""
        ...


def _effort(reasoning_effort: str | None) -> dict:
    return {"reasoning_effort": reasoning_effort} if reasoning_effort else {}


class OpenAILLM:
    def __init__(self, api_key: str, json_timeout_s: float = 20.0, answer_timeout_s: float = 60.0):
        self.json_timeout_s = json_timeout_s
        self.answer_timeout_s = answer_timeout_s
        self._json_client = OpenAI(
            api_key=api_key, timeout=openai.Timeout(json_timeout_s, connect=5.0), max_retries=1
        )
        self._stream_client = OpenAI(
            api_key=api_key, timeout=openai.Timeout(answer_timeout_s, connect=5.0), max_retries=0
        )

    def json(self, model, system, user, schema_name, schema, max_tokens, reasoning_effort=None):
        try:
            resp = self._json_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
                max_completion_tokens=max_tokens,
                **_effort(reasoning_effort),
            )
        except openai.APITimeoutError as exc:
            raise LLMTimeout(f"{schema_name} timed out after {self.json_timeout_s:.0f} s") from exc
        except openai.APIError as exc:
            raise LLMError("UPSTREAM_ERROR", f"{schema_name} failed ({type(exc).__name__})") from exc
        content = resp.choices[0].message.content if resp.choices else None
        if not content:
            raise LLMError("EMPTY_OUTPUT", f"{schema_name} returned no content")
        try:
            return json.loads(content), Usage.from_openai(resp.usage)
        except json.JSONDecodeError as exc:
            raise LLMError("BAD_OUTPUT", f"{schema_name} returned invalid JSON") from exc

    def stream(self, model, system, user, max_tokens, reasoning_effort=None):
        deadline = time.monotonic() + self.answer_timeout_s
        usage = Usage()
        try:
            stream = self._stream_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_completion_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **_effort(reasoning_effort),
            )
            with stream:
                for event in stream:
                    if time.monotonic() > deadline:
                        raise LLMTimeout(f"answer exceeded {self.answer_timeout_s:.0f} s")
                    if event.usage is not None:
                        usage = Usage.from_openai(event.usage)
                    if event.choices and event.choices[0].delta.content:
                        yield event.choices[0].delta.content
        except openai.APITimeoutError as exc:
            raise LLMTimeout(f"answer timed out after {self.answer_timeout_s:.0f} s") from exc
        except openai.APIError as exc:
            raise LLMError("UPSTREAM_ERROR", f"answer failed ({type(exc).__name__})") from exc
        yield usage


class UnconfiguredLLM:
    """No OPENAI_API_KEY: every call fails with a clear code instead of a crash."""

    def json(self, *args, **kwargs):
        raise LLMError("LLM_NOT_CONFIGURED", "OPENAI_API_KEY is not set")

    def stream(self, *args, **kwargs):
        raise LLMError("LLM_NOT_CONFIGURED", "OPENAI_API_KEY is not set")
