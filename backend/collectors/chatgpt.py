"""ChatGPT usage collector.

Primary:  GET https://chat.openai.com/backend-api/accounts/check/v4-2023-04-27
          GET https://chat.openai.com/backend-api/user_system_messages (rate limit info)
Fallback: Playwright DOM scrape of chat.openai.com
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

LOGIN_URL = "https://chat.openai.com/auth/login"
CHAT_URL = "https://chatgpt.com"
ACCOUNTS_CHECK_URL = "https://chat.openai.com/backend-api/accounts/check/v4-2023-04-27"
ME_URL = "https://chat.openai.com/backend-api/me"


class ChatGPTCollector(BaseCollector):
    provider = "chatgpt"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_session():
            raise CollectionError(
                "No ChatGPT session found. Run: llm-tracker auth chatgpt"
            )

        return await self._collect_via_browser()

    async def _collect_via_browser(self) -> UsageSnapshot:
        """Navigate to ChatGPT and intercept API calls for usage data."""
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                accounts_data: dict = {}
                me_data: dict = {}

                async def handle_response(response):
                    nonlocal accounts_data, me_data
                    try:
                        if "accounts/check" in response.url and response.status == 200:
                            accounts_data = await response.json()
                        elif "/backend-api/me" in response.url and response.status == 200:
                            me_data = await response.json()
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(CHAT_URL, wait_until="networkidle", timeout=45_000)

                snapshot = self._base_snapshot()

                # If not enough data from interception, try direct API calls
                if not accounts_data:
                    try:
                        accounts_data = await self._fetch_json(ACCOUNTS_CHECK_URL)
                    except CollectionError:
                        pass

                if accounts_data:
                    snapshot = self._parse_accounts(snapshot, accounts_data)

                # Try to scrape usage from DOM if no message count found
                if snapshot.messages_used is None:
                    snapshot = await self._scrape_usage_dom(page, snapshot)

                # Merge me_data for tier info
                if me_data and snapshot.model_tier is None:
                    snapshot.model_tier = me_data.get("plan_type") or me_data.get(
                        "subscription_plan"
                    )

                snapshot.raw = {
                    "accounts": accounts_data,
                    "me": me_data,
                    "source": "browser_intercept",
                }

                return snapshot
            finally:
                await browser.close()

    def _parse_accounts(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        # accounts/check response structure varies; try common paths
        account = data.get("account", data)

        # Subscription plan
        plan = account.get("plan_type") or account.get("subscription", {}).get("plan_name", "")
        if plan:
            snapshot.model_tier = plan.lower()

        # Some versions expose message_cap_rollover or similar
        caps = account.get("message_caps") or account.get("rate_limits", {})
        if isinstance(caps, dict):
            snapshot.messages_limit = caps.get("limit") or caps.get("max_messages")
            snapshot.messages_used = caps.get("used") or caps.get("messages_sent")
            window = caps.get("window_seconds")
            if window:
                snapshot.messages_window_hours = window / 3600

        # Feature flags
        features = {}
        for key in ("models", "tools", "plugins", "code_interpreter"):
            if key in account:
                features[key] = account[key]
        if features:
            snapshot.features = features

        return snapshot

    async def _scrape_usage_dom(self, page, snapshot: UsageSnapshot) -> UsageSnapshot:
        """Try to find GPT-4 message count in the sidebar or settings."""
        try:
            # Look for text like "10 messages remaining" or "X / Y messages"
            selectors = [
                "text=/\\d+\\s+messages? remaining/i",
                "text=/\\d+\\s*\\/\\s*\\d+\\s+messages?/i",
                "[data-testid*='limit']",
                "[aria-label*='message limit']",
            ]
            for sel in selectors:
                try:
                    el = page.locator(sel).first
                    text = await el.text_content(timeout=2_000)
                    if text:
                        parsed = _parse_message_count(text)
                        if parsed:
                            snapshot.messages_used, snapshot.messages_limit = parsed
                            break
                except Exception:
                    continue
        except Exception:
            pass
        return snapshot


def _parse_message_count(text: str) -> tuple[int, int] | None:
    """Parse 'X of Y' or 'X / Y' or 'X remaining out of Y'."""
    # "X / Y messages"
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # "X messages remaining" (no limit shown)
    m = re.search(r"(\d+)\s+messages?\s+remaining", text, re.I)
    if m:
        return None  # can't determine used vs limit without both
    # "X of Y"
    m = re.search(r"(\d+)\s+of\s+(\d+)", text, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None
