"""Abstract base collector + Playwright session management."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from backend.db.models import UsageSnapshot

load_dotenv()

SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "auth/sessions"))
BROWSER_TYPE = os.getenv("PLAYWRIGHT_BROWSER", "chromium")
HEADFUL = os.getenv("PLAYWRIGHT_HEADFUL", "0") == "1"


class CollectionError(Exception):
    """Raised when data collection fails."""


class BaseCollector(ABC):
    """Abstract base for all LLM usage collectors."""

    provider: str  # must be set by subclass

    def __init__(self) -> None:
        self._session_path = SESSIONS_DIR / f"{self.provider}.json"

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def has_session(self) -> bool:
        return self._session_path.exists()

    def _session_state(self) -> dict[str, Any] | None:
        if self._session_path.exists():
            return json.loads(self._session_path.read_text())
        return None

    def _save_session(self, state: dict[str, Any]) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._session_path.write_text(json.dumps(state, indent=2))

    # ------------------------------------------------------------------
    # Browser helpers
    # ------------------------------------------------------------------

    async def _new_context(
        self,
        playwright: Playwright,
        headless: bool = True,
    ) -> tuple[Browser, BrowserContext]:
        browser_launcher = getattr(playwright, BROWSER_TYPE)
        browser = await browser_launcher.launch(headless=headless)

        state = self._session_state()
        if state:
            context = await browser.new_context(storage_state=state)
        else:
            context = await browser.new_context()

        return browser, context

    async def auth(self, start_url: str) -> None:
        """
        Open a headed browser at start_url, wait for the user to log in,
        then save the session state.
        """
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=False)
            page = await context.new_page()
            await page.goto(start_url)

            print(f"\n[{self.provider}] Browser opened. Please log in, then press Enter here...")
            input()

            state = await context.storage_state()
            self._save_session(state)
            await browser.close()
            print(f"[{self.provider}] Session saved to {self._session_path}")

    # ------------------------------------------------------------------
    # Collection interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def collect(self) -> UsageSnapshot:
        """
        Collect current usage data and return an unsaved UsageSnapshot.
        Raise CollectionError on failure.
        """

    def _base_snapshot(self) -> UsageSnapshot:
        return UsageSnapshot(
            provider=self.provider,
            collected_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # HTTP helpers (reuse session cookies via Playwright fetch)
    # ------------------------------------------------------------------

    async def _fetch_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        """
        Make an authenticated GET request using saved Playwright session cookies.
        Returns parsed JSON or raises CollectionError.
        """
        async with async_playwright() as p:
            browser, context = await self._new_context(p, headless=True)
            try:
                response = await context.request.get(
                    url,
                    headers=headers or {},
                    timeout=timeout_ms,
                )
                if not response.ok:
                    raise CollectionError(
                        f"HTTP {response.status} from {url}"
                    )
                return await response.json()
            finally:
                await browser.close()
