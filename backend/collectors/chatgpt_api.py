"""ChatGPT / OpenAI API usage collector.

Primary:  Playwright session on platform.openai.com — intercepts internal
          billing API calls the dashboard makes.
Fallback: OPENAI_API_KEY in .env → httpx to billing REST endpoint.

Auth: llm-tracker auth chatgpt --api
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from backend.collectors.base import BaseCollector, CollectionError, SESSIONS_DIR
from backend.db.models import UsageSnapshot

load_dotenv()

PLATFORM_LOGIN_URL = "https://platform.openai.com/login"
PLATFORM_USAGE_URL = "https://platform.openai.com/usage"
BILLING_USAGE_URL = "https://api.openai.com/v1/dashboard/billing/usage"
BILLING_SUB_URL = "https://api.openai.com/v1/dashboard/billing/subscription"


class ChatGPTAPICollector(BaseCollector):
    """Collects OpenAI API spend/tokens via platform.openai.com or API key."""

    provider = "chatgpt"
    source = "api"

    def __init__(self) -> None:
        super().__init__()
        self._session_path = SESSIONS_DIR / "chatgpt-api.json"
        self._api_key = os.getenv("OPENAI_API_KEY", "")

    def has_credentials(self) -> bool:
        return self.has_session() or bool(self._api_key)

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(PLATFORM_LOGIN_URL)

    async def collect(self) -> UsageSnapshot:
        if not self.has_credentials():
            raise CollectionError(
                "No ChatGPT API credentials. Run: llm-tracker auth chatgpt --api  "
                "or set OPENAI_API_KEY in .env"
            )
        if self.has_session():
            try:
                return await self._collect_via_browser()
            except CollectionError:
                pass
        if self._api_key:
            return await self._collect_via_key()
        raise CollectionError("ChatGPT API collection failed (no session, no key).")

    # ------------------------------------------------------------------
    # Primary: browser session on platform.openai.com
    # ------------------------------------------------------------------

    async def _collect_via_browser(self) -> UsageSnapshot:
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                page = await context.new_page()

                usage_data: dict = {}
                sub_data: dict = {}

                async def handle_response(response):
                    nonlocal usage_data, sub_data
                    try:
                        if response.status != 200:
                            return
                        ct = response.headers.get("content-type", "")
                        if "json" not in ct:
                            return
                        url = response.url
                        if "billing/usage" in url:
                            usage_data = await response.json()
                        elif "billing/subscription" in url or "billing/credit_grants" in url:
                            sub_data = await response.json()
                    except Exception:
                        pass

                page.on("response", handle_response)
                await page.goto(PLATFORM_USAGE_URL, wait_until="networkidle", timeout=45_000)

                if not usage_data and not sub_data:
                    raise CollectionError("No billing data intercepted from platform.openai.com")

                snapshot = self._base_snapshot()
                snapshot.source = "api"
                snapshot = self._parse(snapshot, usage_data, sub_data)
                snapshot.raw = {"usage": usage_data, "subscription": sub_data, "source": "browser"}
                return snapshot
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # Fallback: OPENAI_API_KEY + httpx
    # ------------------------------------------------------------------

    async def _collect_via_key(self) -> UsageSnapshot:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        today = date.today()
        start = today.replace(day=1)
        end = today + timedelta(days=1)

        async with httpx.AsyncClient(timeout=20) as client:
            usage_resp = await client.get(
                BILLING_USAGE_URL,
                headers=headers,
                params={"start_date": start.isoformat(), "end_date": end.isoformat()},
            )
            if usage_resp.status_code == 401:
                raise CollectionError("OPENAI_API_KEY is invalid or expired.")
            if not usage_resp.is_success:
                raise CollectionError(f"OpenAI billing API returned HTTP {usage_resp.status_code}")
            usage_data = usage_resp.json()

            sub_data: dict = {}
            sub_resp = await client.get(BILLING_SUB_URL, headers=headers)
            if sub_resp.is_success:
                sub_data = sub_resp.json()

        snapshot = self._base_snapshot()
        snapshot.source = "api"
        snapshot = self._parse(snapshot, usage_data, sub_data)
        snapshot.raw = {"usage": usage_data, "subscription": sub_data, "source": "api_key"}
        return snapshot

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, snapshot: UsageSnapshot, usage: dict, sub: dict) -> UsageSnapshot:
        # Billing usage: { total_usage: cents, daily_costs: [...] }
        total_cents = usage.get("total_usage", 0)
        if total_cents:
            snapshot.api_spend_usd = total_cents / 100.0
            snapshot.api_spend_period = "monthly"

        # Token counts from /v1/usage format
        total_input, total_output = 0, 0
        for item in usage.get("data", []):
            total_input += item.get("n_context_tokens_total", 0)
            total_output += item.get("n_generated_tokens_total", 0)
        if total_input or total_output:
            snapshot.tokens_input = total_input
            snapshot.tokens_output = total_output
            snapshot.tokens_period = "monthly"

        # Subscription / hard limits
        if sub:
            hard = sub.get("hard_limit_usd")
            soft = sub.get("soft_limit_usd")
            if hard or soft:
                snapshot.features = {
                    "hard_limit_usd": hard,
                    "soft_limit_usd": soft,
                    "plan": sub.get("plan", {}).get("title"),
                }
            snapshot.model_tier = sub.get("plan", {}).get("id", "").lower() or None

        return snapshot
