"""Gemini API usage collector.

Primary:  Playwright session on aistudio.google.com — intercepts internal
          usage/quota API calls the dashboard makes.
Fallback: GOOGLE_API_KEY in .env → httpx to generativelanguage.googleapis.com
          (models list + quota info only; Google has no simple spend endpoint).

Auth: llm-tracker auth gemini --api
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError, SESSIONS_DIR
from backend.db.models import UsageSnapshot

load_dotenv()

AISTUDIO_LOGIN_URL = "https://accounts.google.com"
AISTUDIO_USAGE_URL = "https://aistudio.google.com/app/apikey"
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiAPICollector(BaseCollector):
    """Collects Gemini API quota info via aistudio.google.com or API key."""

    provider = "gemini"
    source = "api"

    def __init__(self) -> None:
        super().__init__()
        self._session_path = SESSIONS_DIR / "gemini-api.json"
        self._api_key = os.getenv("GOOGLE_API_KEY", "")

    def has_credentials(self) -> bool:
        return self.has_session() or bool(self._api_key)

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(AISTUDIO_LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_credentials():
            raise CollectionError(
                "No Gemini API credentials. Run: llm-tracker auth gemini --api  "
                "or set GOOGLE_API_KEY in .env"
            )
        if self.has_session():
            try:
                return await self._collect_via_browser()
            except CollectionError:
                pass
        if self._api_key:
            return await self._collect_via_key()
        raise CollectionError("Gemini API collection failed (no session, no key).")

    # ------------------------------------------------------------------
    # Primary: browser session on aistudio.google.com
    # ------------------------------------------------------------------

    async def _collect_via_browser(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                usage_data: dict = {}

                async def handle_response(response):
                    nonlocal usage_data
                    try:
                        if response.status != 200:
                            return
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        url = response.url
                        if any(kw in url for kw in ("quota", "usage", "billing", "limit")):
                            data = await response.json()
                            if data:
                                usage_data = data
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(AISTUDIO_USAGE_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()
                snapshot.source = "api"

                if usage_data:
                    snapshot = self._parse_usage(snapshot, usage_data)

                # Scrape DOM for any quota/tier info
                snapshot = await self._scrape_dom(page, snapshot)

                snapshot.raw = {"usage": usage_data, "source": "browser"}
                return snapshot
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Fallback: GOOGLE_API_KEY + httpx (models list only)
    # ------------------------------------------------------------------

    async def _collect_via_key(self) -> UsageSnapshot:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(MODELS_URL, params={"key": self._api_key})
            if resp.status_code in (400, 403):
                raise CollectionError(
                    f"Google API key invalid or permission denied (HTTP {resp.status_code})."
                )
            if not resp.is_success:
                raise CollectionError(f"Gemini models API returned HTTP {resp.status_code}")
            data = resp.json()

        snapshot = self._base_snapshot()
        snapshot.source = "api"
        snapshot.raw = data
        models = [m.get("name", "").replace("models/", "") for m in data.get("models", [])]
        snapshot.features = {
            "available_models": models[:10],
            "note": (
                "Google AI does not expose cumulative spend via a simple API. "
                "For billing data, set up GCP billing export to BigQuery, "
                "or use: llm-tracker auth gemini --api"
            ),
        }
        snapshot.model_tier = "api"
        return snapshot

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_usage(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        for key in ("quotaUsage", "usage", "currentUsage", "data"):
            if key in data:
                val = data[key]
                if isinstance(val, dict):
                    snapshot.tokens_input = val.get("inputTokenCount") or val.get("used")
                    snapshot.tokens_output = val.get("outputTokenCount")
                    if snapshot.tokens_input or snapshot.tokens_output:
                        snapshot.tokens_period = "monthly"
                elif isinstance(val, list):
                    total_in = sum(
                        item.get("inputTokenCount", 0) or item.get("input_tokens", 0)
                        for item in val
                    )
                    total_out = sum(
                        item.get("outputTokenCount", 0) or item.get("output_tokens", 0)
                        for item in val
                    )
                    if total_in or total_out:
                        snapshot.tokens_input = total_in
                        snapshot.tokens_output = total_out
                        snapshot.tokens_period = "monthly"
        return snapshot

    async def _scrape_dom(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        try:
            content = await page.content()
            # Look for tier indicators
            if "gemini ultra" in content.lower() or "advanced" in content.lower():
                snapshot.model_tier = "advanced"
            elif "pro" in content.lower():
                snapshot.model_tier = "pro"
            else:
                snapshot.model_tier = "free"

            # Look for quota numbers
            m = re.search(r"([\d,]+)\s+/\s*([\d,]+)\s*(?:requests?|tokens?)", content, re.I)
            if m:
                snapshot.tokens_input = int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return snapshot
