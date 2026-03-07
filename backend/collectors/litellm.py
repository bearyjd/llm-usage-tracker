"""LiteLLM proxy spend collector.

Reads API spend + token data from a running LiteLLM proxy.
Replaces the per-provider API scrapers when LITELLM_BASE_URL is configured.

Endpoints used:
  GET {base}/global/spend/models   — spend + tokens by model (current month)
  GET {base}/global/spend/keys     — per-key breakdown (optional)

Required env vars:
  LITELLM_BASE_URL   e.g. https://your-litellm-proxy.example.com
  LITELLM_API_KEY    master key or any valid key with read access

Produces one UsageSnapshot(source="api") per provider found in the spend data.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from typing import NamedTuple

import httpx
from dotenv import load_dotenv

from backend.collectors.base import CollectionError
from backend.db.models import UsageSnapshot

load_dotenv()

LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL", "").rstrip("/")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY", "")

# Model-name prefix → provider
_PROVIDER_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(claude|anthropic)", re.I), "claude"),
    (re.compile(r"^(gpt-|o1|o3|o4|openai|chatgpt)", re.I), "chatgpt"),
    (re.compile(r"^(gemini|google|palm)", re.I), "gemini"),
    (re.compile(r"^(groq/|groq-)", re.I), "groq"),
    # LiteLLM passes Groq models as "groq/llama-3.1-8b-instant" etc.
    # Also catch bare model names that are Groq-hosted
    (re.compile(r"^(llama|mixtral|gemma|whisper).*groq", re.I), "groq"),
]


def _model_to_provider(model: str) -> str | None:
    for pattern, provider in _PROVIDER_PATTERNS:
        if pattern.match(model):
            return provider
    return None


class ProviderSpend(NamedTuple):
    spend_usd: float
    tokens_input: int
    tokens_output: int
    models: list[str]


def is_configured() -> bool:
    return bool(LITELLM_BASE_URL and LITELLM_API_KEY)


class LiteLLMCollector:
    """Collects per-provider API spend from a LiteLLM proxy."""

    source = "api"

    def __init__(self) -> None:
        self._base = LITELLM_BASE_URL
        self._key = LITELLM_API_KEY
        self._headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def _base_snapshot(self, provider: str) -> UsageSnapshot:
        return UsageSnapshot(
            provider=provider,
            source=self.source,
            collected_at=datetime.utcnow(),
        )

    async def fetch_daily_by_model(self, provider_prefix: str) -> dict[str, dict]:
        """
        Return today's {model: {spend, prompt_tokens, completion_tokens}} for
        models whose names start with provider_prefix (e.g. "groq/").
        """
        today = date.today()
        start = today.isoformat()
        end = (today + timedelta(days=1)).isoformat()

        async with httpx.AsyncClient(timeout=20, verify=True) as client:
            all_models = await self._fetch_model_spend(client, start, end)

        return {
            model: data
            for model, data in all_models.items()
            if model.lower().startswith(provider_prefix.lower())
               or f"/{provider_prefix.strip('/')}" in model.lower()
        }

    async def collect_all(self) -> list[UsageSnapshot]:
        """
        Fetch LiteLLM spend data and return one snapshot per provider.
        """
        if not is_configured():
            raise CollectionError(
                "LiteLLM not configured. Set LITELLM_BASE_URL and LITELLM_API_KEY in .env"
            )

        today = date.today()
        start = today.replace(day=1).isoformat()
        end = (today + timedelta(days=1)).isoformat()

        async with httpx.AsyncClient(timeout=20, verify=True) as client:
            spend_by_model = await self._fetch_model_spend(client, start, end)

        if not spend_by_model:
            raise CollectionError(
                "LiteLLM returned no spend data. "
                "Check LITELLM_API_KEY has access to /global/spend/models"
            )

        # Aggregate by provider
        aggregated: dict[str, ProviderSpend] = {}
        for model, data in spend_by_model.items():
            provider = _model_to_provider(model)
            if provider is None:
                continue
            existing = aggregated.get(provider, ProviderSpend(0.0, 0, 0, []))
            aggregated[provider] = ProviderSpend(
                spend_usd=existing.spend_usd + data.get("spend", 0.0),
                tokens_input=existing.tokens_input + data.get("prompt_tokens", 0),
                tokens_output=existing.tokens_output + data.get("completion_tokens", 0),
                models=existing.models + [model],
            )

        snapshots = []
        for provider, agg in aggregated.items():
            snapshot = self._base_snapshot(provider)
            snapshot.api_spend_usd = round(agg.spend_usd, 6)
            snapshot.api_spend_period = "monthly"
            snapshot.tokens_input = agg.tokens_input
            snapshot.tokens_output = agg.tokens_output
            snapshot.tokens_period = "monthly"
            snapshot.features = {"models_used": agg.models}
            snapshot.raw = {
                "source": "litellm",
                "base_url": self._base,
                "period": f"{start}/{end}",
                "models": {m: spend_by_model[m] for m in agg.models},
            }
            snapshots.append(snapshot)

        return snapshots

    async def _fetch_model_spend(
        self, client: httpx.AsyncClient, start: str, end: str
    ) -> dict[str, dict]:
        """
        Try several LiteLLM endpoint variants and return {model: {spend, tokens}} dict.
        """
        # Variant 1: /global/spend/models with query params
        try:
            resp = await client.get(
                f"{self._base}/global/spend/models",
                headers=self._headers,
                params={"start_date": start, "end_date": end},
            )
            if resp.is_success:
                return self._parse_model_spend(resp.json())
        except Exception:
            pass

        # Variant 2: /global/spend/models without date params
        try:
            resp = await client.get(
                f"{self._base}/global/spend/models",
                headers=self._headers,
            )
            if resp.is_success:
                return self._parse_model_spend(resp.json())
        except Exception:
            pass

        # Variant 3: /spend/logs with date params, aggregate manually
        try:
            resp = await client.get(
                f"{self._base}/spend/logs",
                headers=self._headers,
                params={"start_date": start, "end_date": end},
            )
            if resp.is_success:
                return self._aggregate_logs(resp.json())
        except Exception:
            pass

        raise CollectionError(
            f"Could not reach LiteLLM spend endpoints at {self._base}. "
            "Check LITELLM_BASE_URL and that your key has spend read access."
        )

    def _parse_model_spend(self, data) -> dict[str, dict]:
        """
        Parse /global/spend/models response.
        Handles both list and dict formats LiteLLM uses across versions.
        """
        result: dict[str, dict] = {}

        if isinstance(data, list):
            for item in data:
                model = item.get("model") or item.get("model_name") or item.get("name", "")
                if not model:
                    continue
                result[model] = {
                    "spend": float(item.get("spend", 0) or item.get("total_cost", 0)),
                    "prompt_tokens": int(item.get("prompt_tokens", 0) or item.get("input_tokens", 0)),
                    "completion_tokens": int(item.get("completion_tokens", 0) or item.get("output_tokens", 0)),
                    "total_tokens": int(item.get("total_tokens", 0)),
                }
        elif isinstance(data, dict):
            # Some versions return {model_name: {spend: ..., tokens: ...}}
            for model, info in data.items():
                if isinstance(info, dict):
                    result[model] = {
                        "spend": float(info.get("spend", 0) or info.get("total_cost", 0)),
                        "prompt_tokens": int(info.get("prompt_tokens", 0)),
                        "completion_tokens": int(info.get("completion_tokens", 0)),
                        "total_tokens": int(info.get("total_tokens", 0)),
                    }

        return result

    def _aggregate_logs(self, data) -> dict[str, dict]:
        """Aggregate raw /spend/logs entries by model."""
        result: dict[str, dict] = {}

        logs = data if isinstance(data, list) else data.get("logs", data.get("data", []))
        for log in logs:
            model = log.get("model") or log.get("model_name", "unknown")
            entry = result.setdefault(model, {
                "spend": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0,
            })
            entry["spend"] += float(log.get("spend", 0) or log.get("cost", 0))
            entry["prompt_tokens"] += int(log.get("prompt_tokens", 0))
            entry["completion_tokens"] += int(log.get("completion_tokens", 0))
            entry["total_tokens"] += int(log.get("total_tokens", 0))

        return result
