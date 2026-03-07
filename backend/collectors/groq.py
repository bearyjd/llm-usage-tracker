"""Groq subscription/rate-limit collector.

When LiteLLM is configured (LITELLM_BASE_URL + LITELLM_API_KEY):
  - Queries LiteLLM for today's groq/* token usage
  - Compares against Groq free tier daily token limits
  - Gives a per-model breakdown in features_json

When LiteLLM is not configured:
  - Playwright scrape of console.groq.com/settings/limits

Groq tracks usage as tokens/day per model. messages_used/messages_limit here
represent total tokens used today vs the tightest daily limit across your models.

Auth (fallback only): llm-tracker auth groq

Free tier limits: https://console.groq.com/docs/rate-limits
These are hardcoded but can be overridden via GROQ_DAILY_TOKEN_LIMIT in .env.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone

from dotenv import load_dotenv

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

load_dotenv()

LOGIN_URL = "https://console.groq.com"
USAGE_URL = "https://console.groq.com/settings/limits"

# Groq free tier tokens-per-day limits by model (as of early 2026).
# Verify current values at: https://console.groq.com/docs/rate-limits
# Set GROQ_DAILY_TOKEN_LIMIT in .env to override the aggregate limit.
GROQ_FREE_TPD: dict[str, int] = {
    # Production models
    "llama-3.3-70b-versatile":        100_000,
    "llama-3.1-70b-versatile":        200_000,
    "llama-3.1-8b-instant":           500_000,
    "llama3-70b-8192":                  6_000,
    "llama3-8b-8192":                 500_000,
    "mixtral-8x7b-32768":             500_000,
    "gemma2-9b-it":                   500_000,
    "gemma-7b-it":                    500_000,
    # Preview / vision models (more constrained)
    "llama-3.2-11b-vision-preview":     7_000,
    "llama-3.2-90b-vision-preview":     3_500,
    "llama-3.3-70b-specdec":          100_000,
    "llama-3.2-1b-preview":           500_000,
    "llama-3.2-3b-preview":           500_000,
}
_DEFAULT_FREE_TPD = 500_000  # fallback for unknown models


def _strip_groq_prefix(model: str) -> str:
    """'groq/llama-3.1-8b-instant' → 'llama-3.1-8b-instant'"""
    return model.split("/", 1)[-1] if "/" in model else model


def _tpd_for(model: str) -> int:
    bare = _strip_groq_prefix(model).lower()
    for key, limit in GROQ_FREE_TPD.items():
        if key.lower() in bare or bare in key.lower():
            return limit
    return _DEFAULT_FREE_TPD


def _midnight_utc() -> datetime:
    """Next UTC midnight — when Groq daily limits reset."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).replace(
        day=now.day + 1
    )


class GroqCollector(BaseCollector):
    provider = "groq"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        from backend.collectors.litellm import is_configured

        if is_configured():
            return await self._collect_via_litellm()

        if not self.has_session():
            raise CollectionError(
                "No Groq session and LiteLLM not configured.\n"
                "  Option A (recommended): set LITELLM_BASE_URL + LITELLM_API_KEY\n"
                "  Option B: run llm-tracker auth groq"
            )
        return await self._collect_from_console()

    # ------------------------------------------------------------------
    # Primary: query LiteLLM for today's Groq token usage
    # ------------------------------------------------------------------

    async def _collect_via_litellm(self) -> UsageSnapshot:
        from backend.collectors.litellm import LiteLLMCollector

        today_models = await LiteLLMCollector().fetch_daily_by_model("groq")

        snapshot = self._base_snapshot()

        if not today_models:
            # No Groq traffic today — still record zero usage
            snapshot.messages_used = 0
            override = int(os.getenv("GROQ_DAILY_TOKEN_LIMIT", "0"))
            snapshot.messages_limit = override or min(GROQ_FREE_TPD.values())
            snapshot.messages_window_hours = 24.0
            snapshot.messages_reset_at = _midnight_utc().replace(tzinfo=None)
            snapshot.model_tier = "free"
            snapshot.features = {"note": "No Groq requests today via LiteLLM"}
            snapshot.raw = {"source": "litellm_daily", "models": {}}
            return snapshot

        # Sum today's tokens across all Groq models
        total_tokens = sum(
            d.get("prompt_tokens", 0) + d.get("completion_tokens", 0)
            for d in today_models.values()
        )

        # Per-model breakdown with headroom
        per_model = {}
        for model, data in today_models.items():
            used = data.get("prompt_tokens", 0) + data.get("completion_tokens", 0)
            limit = _tpd_for(model)
            per_model[_strip_groq_prefix(model)] = {
                "tokens_used_today": used,
                "daily_limit": limit,
                "remaining": max(0, limit - used),
                "pct_used": round(used / limit * 100, 1) if limit else 0,
            }

        # Tightest constraint = model closest to its limit
        tightest_model = min(
            per_model,
            key=lambda m: per_model[m]["remaining"] / max(per_model[m]["daily_limit"], 1),
        )
        tightest = per_model[tightest_model]

        # Override aggregate limit via env if user knows their actual plan
        override_limit = int(os.getenv("GROQ_DAILY_TOKEN_LIMIT", "0"))
        if override_limit:
            snapshot.messages_used = total_tokens
            snapshot.messages_limit = override_limit
        else:
            # Use the tightest per-model limit as the headline figure
            snapshot.messages_used = tightest["tokens_used_today"]
            snapshot.messages_limit = tightest["daily_limit"]

        snapshot.messages_window_hours = 24.0
        snapshot.messages_reset_at = _midnight_utc().replace(tzinfo=None)
        snapshot.model_tier = "free"
        snapshot.features = {
            "note": "messages_used/limit = tokens used today vs free-tier daily limit",
            "tightest_model": tightest_model,
            "total_tokens_today": total_tokens,
            "per_model": per_model,
        }
        snapshot.raw = {"source": "litellm_daily", "models": today_models}
        return snapshot

    # ------------------------------------------------------------------
    # Fallback: scrape console.groq.com
    # ------------------------------------------------------------------

    async def _collect_from_console(self) -> UsageSnapshot:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()
                api_data: dict = {}

                async def handle_response(response):
                    nonlocal api_data
                    try:
                        if response.status != 200:
                            return
                        if "json" not in response.headers.get("content-type", ""):
                            return
                        url = response.url
                        if any(kw in url for kw in ("limit", "usage", "quota", "rate")):
                            data = await response.json()
                            if data:
                                api_data = data
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(USAGE_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()
                if api_data:
                    snapshot = self._parse_api(snapshot, api_data)
                if snapshot.messages_used is None:
                    snapshot = await self._scrape_dom(page, snapshot)

                snapshot.messages_window_hours = 24.0
                snapshot.messages_reset_at = _midnight_utc().replace(tzinfo=None)
                snapshot.raw = {"api": api_data, "source": "console_scrape"}
                return snapshot
            finally:
                await browser.close()

    def _parse_api(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        limits = data.get("data") or data.get("limits") or data.get("results") or []
        if isinstance(limits, list) and limits:
            total_used, total_limit = 0, 0
            for item in limits:
                total_used += item.get("tokens_used") or item.get("used") or 0
                total_limit += item.get("tokens_limit") or item.get("limit") or 0
            if total_used or total_limit:
                snapshot.messages_used = total_used
                snapshot.messages_limit = total_limit
        elif isinstance(data, dict):
            used = data.get("tokens_used") or data.get("used")
            limit = data.get("tokens_limit") or data.get("limit")
            if used is not None:
                snapshot.messages_used = int(used)
            if limit is not None:
                snapshot.messages_limit = int(limit)
        return snapshot

    async def _scrape_dom(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        try:
            content = await page.content()
            content_lower = content.lower()
            if "flex" in content_lower:
                snapshot.model_tier = "flex"
            elif "on-demand" in content_lower or "pay" in content_lower:
                snapshot.model_tier = "on-demand"
            else:
                snapshot.model_tier = "free"
            m = re.search(r"([\d,]+)\s*/\s*([\d,]+)\s*(?:tokens?|toks?)", content, re.I)
            if m:
                snapshot.messages_used = int(m.group(1).replace(",", ""))
                snapshot.messages_limit = int(m.group(2).replace(",", ""))
        except Exception:
            pass
        return snapshot
