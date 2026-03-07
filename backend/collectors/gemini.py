"""Gemini usage collector.

Strategy:
1. Navigate to gemini.google.com and intercept any usage-related API calls
2. Check myaccount.google.com/subscriptions for Gemini Advanced status
3. DOM scrape of visible usage indicators
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

LOGIN_URL = "https://accounts.google.com"
GEMINI_URL = "https://gemini.google.com"
SUBSCRIPTIONS_URL = "https://myaccount.google.com/subscriptions"


class GeminiCollector(BaseCollector):
    provider = "gemini"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_session():
            raise CollectionError(
                "No Gemini session found. Run: llm-tracker auth gemini"
            )

        return await self._collect_via_browser()

    async def _collect_via_browser(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                api_data: dict = {}
                subscription_data: dict = {}

                async def handle_response(response):
                    nonlocal api_data, subscription_data
                    try:
                        url = response.url
                        # Gemini uses various internal endpoints
                        if any(
                            kw in url
                            for kw in ("quota", "usage", "limits", "subscription")
                        ) and response.status == 200:
                            ct = response.headers.get("content-type", "")
                            if "json" in ct:
                                api_data = await response.json()
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(GEMINI_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()

                # Check subscription tier via DOM on the main page
                tier = await self._detect_tier(page)
                snapshot.model_tier = tier

                # Try to find usage indicators
                snapshot = await self._scrape_usage(page, snapshot)

                # If we got API data, prefer it
                if api_data:
                    snapshot = self._parse_api_data(snapshot, api_data)

                snapshot.raw = {
                    "api_data": api_data,
                    "tier_detected": tier,
                    "source": "browser_scrape",
                }

                # Gemini Advanced: typically shows usage for image gen / extensions
                # Base Gemini: no hard message limit but rate-limits apply
                if snapshot.messages_limit is None and tier == "advanced":
                    # Gemini Advanced doesn't publish hard message limits publicly
                    # but image generation has ~200/month limit
                    snapshot.features = {
                        "note": "Gemini Advanced — message limits not publicly displayed",
                        "image_gen_monthly": 200,
                    }

                return snapshot
            finally:
                await browser.close()

    async def _detect_tier(self, page) -> str:
        """Detect if user has Gemini Advanced or free tier."""
        try:
            content = await page.content()
            content_lower = content.lower()
            if "advanced" in content_lower or "gemini ultra" in content_lower:
                return "advanced"
            if "1.5 pro" in content_lower or "2.0 pro" in content_lower:
                return "pro"
        except Exception:
            pass
        return "free"

    async def _scrape_usage(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        """Try to find visible usage counts on the Gemini page."""
        selectors = [
            "text=/\\d+\\s*\\/\\s*\\d+/",
            "text=/\\d+\\s+of\\s+\\d+/i",
            "[aria-label*='usage']",
            "[data-usage]",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                text = await el.text_content(timeout=2_000)
                if text:
                    m = re.search(r"(\d+)\s*[/of]+\s*(\d+)", text, re.I)
                    if m:
                        snapshot.messages_used = int(m.group(1))
                        snapshot.messages_limit = int(m.group(2))
                        break
            except Exception:
                continue

        return snapshot

    def _parse_api_data(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        """Parse any structured API response we intercepted."""
        # Try common fields across Google API patterns
        for key in ("quotaUsage", "usage", "currentUsage"):
            if key in data:
                usage = data[key]
                if isinstance(usage, dict):
                    snapshot.messages_used = usage.get("used") or usage.get("count")
                    snapshot.messages_limit = usage.get("limit") or usage.get("quota")

        return snapshot
