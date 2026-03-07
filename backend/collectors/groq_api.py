"""Groq API usage collector.

Primary:  Playwright session on console.groq.com (same as subscription collector
          but looking at the usage/spend section rather than rate limits).
Fallback: GROQ_API_KEY in .env — validates key and reports available models
          (Groq has no public cumulative usage REST endpoint as of 2025).

Auth: llm-tracker auth groq --api
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

CONSOLE_LOGIN_URL = "https://console.groq.com"
CONSOLE_USAGE_URL = "https://console.groq.com/settings/billing"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"


class GroqAPICollector(BaseCollector):
    provider = "groq"
    source = "api"

    def __init__(self) -> None:
        super().__init__()
        self._session_path = SESSIONS_DIR / "groq-api.json"
        self._api_key = os.getenv("GROQ_API_KEY", "")

    def has_credentials(self) -> bool:
        return self.has_session() or bool(self._api_key)

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(CONSOLE_LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_credentials():
            raise CollectionError(
                "No Groq API credentials. Run: llm-tracker auth groq --api  "
                "or set GROQ_API_KEY in .env"
            )
        if self.has_session():
            try:
                return await self._collect_via_browser()
            except CollectionError:
                pass
        if self._api_key:
            return await self._collect_via_key()
        raise CollectionError("Groq API collection failed.")

    async def _collect_via_browser(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                spend_data: dict = {}

                async def handle_response(response):
                    nonlocal spend_data
                    try:
                        if response.status != 200:
                            return
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        url = response.url
                        if any(kw in url for kw in ("billing", "spend", "invoice", "usage")):
                            data = await response.json()
                            if data:
                                spend_data = data
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(CONSOLE_USAGE_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()
                snapshot.source = "api"

                if spend_data:
                    snapshot = self._parse_spend(snapshot, spend_data)

                if snapshot.api_spend_usd is None:
                    snapshot = await self._scrape_dom(page, snapshot)

                snapshot.raw = {"spend": spend_data, "source": "browser"}
                return snapshot
            finally:
                await browser.close()

    async def _collect_via_key(self) -> UsageSnapshot:
        """Validate the API key and report available models."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                GROQ_MODELS_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            if resp.status_code == 401:
                raise CollectionError("GROQ_API_KEY is invalid or expired.")
            if not resp.is_success:
                raise CollectionError(f"Groq API returned HTTP {resp.status_code}")
            data = resp.json()

        snapshot = self._base_snapshot()
        snapshot.source = "api"
        snapshot.raw = data
        models = [m.get("id", "") for m in data.get("data", [])]
        snapshot.features = {
            "available_models": models,
            "note": (
                "Groq does not expose cumulative spend via a public API. "
                "For billing data, use: llm-tracker auth groq --api"
            ),
        }
        snapshot.model_tier = "api"
        return snapshot

    def _parse_spend(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        spend = (
            data.get("total_spend")
            or data.get("amount")
            or data.get("total_cost")
            or data.get("balance_used")
        )
        if spend is not None:
            snapshot.api_spend_usd = float(spend)
            snapshot.api_spend_period = "monthly"

        tokens_in = data.get("input_tokens") or data.get("prompt_tokens")
        tokens_out = data.get("output_tokens") or data.get("completion_tokens")
        if tokens_in:
            snapshot.tokens_input = int(tokens_in)
            snapshot.tokens_period = "monthly"
        if tokens_out:
            snapshot.tokens_output = int(tokens_out)

        return snapshot

    async def _scrape_dom(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        try:
            content = await page.content()
            m = re.search(r"\$\s*([\d,]+\.?\d*)", content)
            if m:
                snapshot.api_spend_usd = float(m.group(1).replace(",", ""))
                snapshot.api_spend_period = "monthly"
        except Exception:
            pass
        return snapshot
