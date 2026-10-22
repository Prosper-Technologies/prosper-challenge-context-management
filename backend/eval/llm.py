#
# The evaluation's LLM seam: one async `complete()` call per model request, with the
# provider's reported usage and the measured latency. OpenAIChat calls the API;
# tests pass a scripted stand-in with the same method.
#

import time
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass(frozen=True)
class Usage:
    input: int = 0
    output: int = 0
    cached: int = 0  # the part of `input` served from the provider's prompt cache


@dataclass(frozen=True)
class Reply:
    content: Optional[str]
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    model: str = ""  # the model id the provider reports having used
    latency_s: float = 0.0


class LLM(Protocol):
    async def complete(self, model: str, messages: list[dict], tools: Optional[list[dict]] = None) -> Reply: ...


class OpenAIChat:
    """Chat Completions with the provider's default sampling settings, as the voice
    pipeline uses them (the scheduler document sets only the model)."""

    def __init__(self, api_key: Optional[str] = None):
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, max_retries=8, timeout=90)

    async def complete(self, model: str, messages: list[dict], tools: Optional[list[dict]] = None) -> Reply:
        started = time.perf_counter()
        response = await self._client.chat.completions.create(
            model=model, messages=messages, **({"tools": tools} if tools else {})
        )
        latency = time.perf_counter() - started
        message = response.choices[0].message
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return Reply(
            content=message.content,
            tool_calls=tuple(
                ToolCall(c.id, c.function.name, c.function.arguments) for c in message.tool_calls or ()
            ),
            usage=Usage(
                input=usage.prompt_tokens,
                output=usage.completion_tokens,
                cached=(getattr(details, "cached_tokens", None) or 0) if details else 0,
            ),
            model=response.model,
            latency_s=latency,
        )
