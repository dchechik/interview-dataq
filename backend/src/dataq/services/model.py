"""The model client handed to ``external``-mode plugins.

Plugins never construct their own client: centralising it keeps auth and model
choice in one place, makes cost accounting reliable, and lets tests inject a fake.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..jobs.context import Cost

# Claude Opus 5 list price, USD per million tokens.
PRICE_IN_PER_MTOK = 5.0
PRICE_OUT_PER_MTOK = 25.0


def estimate_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in / 1e6 * PRICE_IN_PER_MTOK + tokens_out / 1e6 * PRICE_OUT_PER_MTOK


class AnthropicModelClient:
    """Thin wrapper over the Anthropic SDK.

    Structured output is requested via ``output_config.format`` so plugins receive
    a validated object and never parse free text.
    """

    def __init__(self, settings: Settings) -> None:
        from anthropic import AsyncAnthropic

        self.settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def complete(
        self, *, system: str, prompt: str, output_schema: dict | None = None, **kwargs: Any
    ) -> tuple[Any, Cost]:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": kwargs.pop("max_tokens", 8000),
            "system": [
                # Stable prefix carries the cache breakpoint; the volatile row data
                # goes in the user turn, after it.
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "adaptive"},
        }
        if output_schema is not None:
            request["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": output_schema,
                }
            }
        request.update(kwargs)

        message = await self._client.messages.create(**request)

        usage = message.usage
        tokens_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        cost = Cost(
            calls=1,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=estimate_usd(tokens_in, tokens_out),
        )

        if getattr(message, "stop_reason", None) == "refusal":
            # A row-level outcome, not a crash: the runner isolates it.
            raise RuntimeError("model declined to answer (stop_reason=refusal)")

        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        if output_schema is not None:
            return json.loads(text), cost
        return text, cost


class FakeModelClient:
    """Deterministic stand-in used by tests and by ``--no-api-key`` runs."""

    def __init__(self, responder=None) -> None:
        self.calls: list[dict] = []
        self._responder = responder

    async def complete(
        self, *, system: str, prompt: str, output_schema: dict | None = None, **kwargs: Any
    ) -> tuple[Any, Cost]:
        self.calls.append({"system": system, "prompt": prompt})
        cost = Cost(calls=1, tokens_in=100, tokens_out=50, usd=estimate_usd(100, 50))
        if self._responder is not None:
            return self._responder(prompt), cost
        n = sum(1 for line in prompt.splitlines() if line.startswith("["))
        return {"results": [{"index": i, "entities": [f"entity{i}"]} for i in range(n)]}, cost


def make_model_client(settings: Settings):
    """Real client when a key is configured, otherwise the fake, so the external
    code path stays exercised in local development."""
    if settings.anthropic_api_key:
        return AnthropicModelClient(settings)
    return FakeModelClient()
