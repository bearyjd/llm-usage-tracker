"""Claude usage collector.

Primary:  GET https://claude.ai/api/oauth/usage
Fallback: Playwright DOM scrape of claude.ai/settings/usage
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

USAGE_API_URL = "https://claude.ai/api/oauth/usage"
SETTINGS_URL = "https://claude.ai/settings/usage"
LOGIN_URL = "https://claude.ai/login"


class ClaudeCollector(BaseCollector):
    provider = "claude"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_session():
            raise CollectionError(
                "No Claude session found. Run: llm-tracker auth claude"
            )

        try:
            return await self._collect_via_api()
        except CollectionError:
            return await self._collect_via_dom()

    # ------------------------------------------------------------------
    # Primary: internal API endpoint
    # ------------------------------------------------------------------

    async def _collect_via_api(self) -> UsageSnapshot:
        data = await self._fetch_json(USAGE_API_URL)
        snapshot = self._base_snapshot()
        snapshot.raw = data

        # Claude Pro: five_hour_utilization tracks rolling 5h window
        five_hour = data.get("five_hour_utilization", {})
        seven_day = data.get("seven_day_utilization", {})

        if five_hour:
            snapshot.messages_used = five_hour.get("messages_sent")
            snapshot.messages_limit = five_hour.get("messages_limit")
            snapshot.messages_window_hours = 5.0

            reset_at_str = five_hour.get("reset_at") or data.get("reset_at")
            if reset_at_str:
                snapshot.messages_reset_at = _parse_iso(reset_at_str)

        elif seven_day:
            snapshot.messages_used = seven_day.get("messages_sent")
            snapshot.messages_limit = seven_day.get("messages_limit")
            snapshot.messages_window_hours = 168.0  # 7 days

            reset_at_str = seven_day.get("reset_at") or data.get("reset_at")
            if reset_at_str:
                snapshot.messages_reset_at = _parse_iso(reset_at_str)

        # Subscription tier
        snapshot.model_tier = data.get("plan_name") or data.get("subscription_tier")

        # Feature flags
        features: dict = {}
        for key in ("models_available", "context_window", "priority_access"):
            if key in data:
                features[key] = data[key]
        if features:
            snapshot.features = features

        return snapshot

    # ------------------------------------------------------------------
    # Fallback: DOM scrape
    # ------------------------------------------------------------------

    async def _collect_via_dom(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                # Intercept the usage API response while navigating
                captured: list[dict] = []

                async def handle_response(response):
                    if "api/oauth/usage" in response.url and response.status == 200:
                        try:
                            captured.append(await response.json())
                        except Exception:
                            pass

                page.on("response", handle_response)
                await page.goto(SETTINGS_URL, wait_until="networkidle", timeout=30_000)

                # If API was intercepted during navigation, use it
                if captured:
                    snapshot = self._base_snapshot()
                    data = captured[0]
                    snapshot.raw = data
                    return self._parse_api_response(snapshot, data)

                # Pure DOM fallback
                snapshot = self._base_snapshot()
                snapshot.raw = {"source": "dom", "url": SETTINGS_URL}

                # Try to find usage text like "23 of 40 messages"
                usage_text = await page.locator("text=/\\d+ of \\d+/").first.text_content(
                    timeout=5_000
                )
                if usage_text:
                    import re
                    match = re.search(r"(\d+)\s+of\s+(\d+)", usage_text)
                    if match:
                        snapshot.messages_used = int(match.group(1))
                        snapshot.messages_limit = int(match.group(2))

                return snapshot
            finally:
                await browser.close()

    def _parse_api_response(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        five_hour = data.get("five_hour_utilization", {})
        if five_hour:
            snapshot.messages_used = five_hour.get("messages_sent")
            snapshot.messages_limit = five_hour.get("messages_limit")
            snapshot.messages_window_hours = 5.0
            reset_at_str = five_hour.get("reset_at") or data.get("reset_at")
            if reset_at_str:
                snapshot.messages_reset_at = _parse_iso(reset_at_str)
        return snapshot


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)  # store as UTC naive
    except (ValueError, AttributeError):
        return None
