"""Claude API usage collector — scrapes console.anthropic.com.

Anthropic does not expose a public REST endpoint for usage/billing data,
so we use a Playwright session on console.anthropic.com to intercept the
internal API calls the dashboard makes.

Auth: llm-tracker auth claude --api
"""

from __future__ import annotations

import re
from datetime import datetime

from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

CONSOLE_LOGIN_URL = "https://console.anthropic.com/login"
CONSOLE_USAGE_URL = "https://console.anthropic.com/settings/usage"


class ClaudeAPICollector(BaseCollector):
    provider = "claude"
    source = "api"

    # Override session path to use a separate file from the OAuth collector
    def __init__(self) -> None:
        super().__init__()
        from backend.collectors.base import SESSIONS_DIR
        self._session_path = SESSIONS_DIR / "claude-api.json"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(CONSOLE_LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_session():
            raise CollectionError(
                "No Claude API console session. Run: llm-tracker auth claude --api"
            )
        return await self._collect_from_console()

    async def _collect_from_console(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                usage_data: dict = {}
                cost_data: dict = {}

                async def handle_response(response):
                    nonlocal usage_data, cost_data
                    try:
                        url = response.url
                        if response.status != 200:
                            return
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        if "usage" in url and "console.anthropic.com" in url:
                            usage_data = await response.json()
                        elif "cost" in url or "billing" in url or "spend" in url:
                            cost_data = await response.json()
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(CONSOLE_USAGE_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()
                snapshot.source = "api"

                # Parse intercepted API responses
                if usage_data:
                    snapshot = self._parse_usage(snapshot, usage_data)
                if cost_data:
                    snapshot = self._parse_cost(snapshot, cost_data)

                # Fallback: scrape DOM for cost/token numbers
                if snapshot.api_spend_usd is None and snapshot.tokens_input is None:
                    snapshot = await self._scrape_dom(page, snapshot)

                snapshot.raw = {
                    "usage": usage_data,
                    "cost": cost_data,
                    "source": "console_scrape",
                }
                return snapshot
            finally:
                await browser.close()

    def _parse_usage(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        # Console API response patterns vary — try common paths
        for key in ("data", "results", "usage"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                total_input = sum(
                    item.get("input_tokens", 0) or item.get("tokens_input", 0)
                    for item in items
                )
                total_output = sum(
                    item.get("output_tokens", 0) or item.get("tokens_output", 0)
                    for item in items
                )
                if total_input or total_output:
                    snapshot.tokens_input = total_input
                    snapshot.tokens_output = total_output
                    snapshot.tokens_period = "monthly"
                total_cost = sum(
                    float(item.get("cost", 0) or item.get("total_cost", 0))
                    for item in items
                )
                if total_cost:
                    snapshot.api_spend_usd = total_cost
                    snapshot.api_spend_period = "monthly"
                break

        # Direct fields
        if "total_cost" in data:
            snapshot.api_spend_usd = float(data["total_cost"])
            snapshot.api_spend_period = "monthly"
        if "input_tokens" in data:
            snapshot.tokens_input = data["input_tokens"]
        if "output_tokens" in data:
            snapshot.tokens_output = data["output_tokens"]

        return snapshot

    def _parse_cost(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        spend = (
            data.get("total_spend")
            or data.get("total_cost")
            or data.get("amount")
        )
        if spend is not None:
            snapshot.api_spend_usd = float(spend)
            snapshot.api_spend_period = "monthly"
        return snapshot

    async def _scrape_dom(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        """Try to pull cost/token numbers directly from the rendered page."""
        try:
            content = await page.content()
            # Look for dollar amounts like "$12.34"
            m = re.search(r"\$\s*([\d,]+\.?\d*)", content)
            if m:
                snapshot.api_spend_usd = float(m.group(1).replace(",", ""))
                snapshot.api_spend_period = "monthly"

            # Look for token counts like "1,234,567 tokens"
            m = re.search(r"([\d,]+)\s+(?:input\s+)?tokens", content, re.I)
            if m:
                snapshot.tokens_input = int(m.group(1).replace(",", ""))
        except Exception:
            pass
        return snapshot
