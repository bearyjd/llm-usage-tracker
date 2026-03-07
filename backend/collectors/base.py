"""Abstract base collector + Playwright session management."""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright_stealth import Stealth

from backend.db.models import UsageSnapshot

load_dotenv()

_PROJECT_ROOT = Path(__file__).parent.parent.parent
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", str(_PROJECT_ROOT / "auth" / "sessions")))
BROWSER_PROFILES_DIR = Path(
    os.getenv("BROWSER_PROFILES_DIR", str(_PROJECT_ROOT / "auth" / "browser_profiles"))
)
BROWSER_TYPE = os.getenv("PLAYWRIGHT_BROWSER", "chromium")
HEADFUL = os.getenv("PLAYWRIGHT_HEADFUL", "0") == "1"

_stealth = Stealth()

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--exclude-switches=enable-automation",
    "--disable-infobars",
    "--no-first-run",
]

_CHROME_USER_DATA_CANDIDATES = [
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "chromium",
    # Flatpak-installed Chrome
    Path.home() / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome",
    Path.home() / ".var" / "app" / "org.chromium.Chromium" / "config" / "chromium",
]


def _is_lock_stale(lock_path: Path) -> bool:
    """Check if a Chrome SingletonLock is stale (owner process is dead)."""
    if not lock_path.exists() and not lock_path.is_symlink():
        return False
    try:
        target = os.readlink(lock_path)
        # SingletonLock symlink target format: "hostname-pid"
        parts = target.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            pid = int(parts[1])
            try:
                os.kill(pid, 0)
                return False
            except OSError:
                return True
    except (OSError, ValueError):
        pass
    return False


def _clear_stale_lock(user_data_dir: Path) -> bool:
    """Remove stale SingletonLock if the owning process is dead. Returns True if removed."""
    lock = user_data_dir / "SingletonLock"
    if _is_lock_stale(lock):
        try:
            lock.unlink()
            print(f"[browser] Removed stale profile lock: {lock}")
            return True
        except OSError:
            pass
    return False


def _find_system_chrome() -> tuple[str | None, Path | None]:
    """Return (executable_path, user_data_dir) for the system Chrome, or (None, None)."""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        exe = shutil.which(name)
        if exe:
            for candidate in _CHROME_USER_DATA_CANDIDATES:
                if (candidate / "Default").is_dir():
                    _clear_stale_lock(candidate)
                    return exe, candidate
            return exe, None
    return None, None


class CollectionError(Exception):
    """Raised when data collection fails."""


class BaseCollector(ABC):
    """Abstract base for all LLM usage collectors."""

    provider: str  # must be set by subclass

    def __init__(self) -> None:
        self._session_path = SESSIONS_DIR / f"{self.provider}.json"
        self._browser_profile_dir = BROWSER_PROFILES_DIR / self.provider

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def has_session(self) -> bool:
        return self._session_path.exists()

    def has_browser_profile(self) -> bool:
        return self._browser_profile_dir.exists()

    def _session_state(self) -> dict[str, Any] | None:
        if self._session_path.exists():
            return json.loads(self._session_path.read_text())
        return None

    def _save_session(self, state: Any) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._session_path.write_text(json.dumps(state, indent=2))

    def _get_session_cookies(self, domain_filter: str = "") -> dict[str, str]:
        state = self._session_state()
        if not state:
            return {}
        return {
            c["name"]: c["value"]
            for c in state.get("cookies", [])
            if domain_filter in c.get("domain", "")
        }

    # ------------------------------------------------------------------
    # Browser helpers
    # ------------------------------------------------------------------

    async def _launch_persistent(
        self,
        playwright: Playwright,
        headless: bool = True,
    ) -> BrowserContext:
        """Launch Playwright Chromium with a dedicated per-provider persistent profile.

        This ensures the same browser fingerprint is used for both auth and
        collection, which is required to pass Cloudflare's checks.
        """
        self._browser_profile_dir.mkdir(parents=True, exist_ok=True)
        _clear_stale_lock(self._browser_profile_dir)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._browser_profile_dir),
            headless=headless,
            args=_STEALTH_ARGS,
        )
        await _stealth.apply_stealth_async(context)
        return context

    async def _new_context(
        self,
        playwright: Playwright,
        headless: bool = True,
    ) -> tuple[Browser, BrowserContext]:
        browser_launcher = getattr(playwright, BROWSER_TYPE)
        browser = await browser_launcher.launch(
            headless=headless,
            args=_STEALTH_ARGS,
        )

        state = self._session_state()
        if state:
            context = await browser.new_context(storage_state=state)
        else:
            context = await browser.new_context()

        await _stealth.apply_stealth_async(context)
        return browser, context

    async def auth(self, start_url: str) -> None:
        """
        Open a headed browser at start_url, wait for the user to log in,
        then save the session state.

        Uses a dedicated persistent Playwright profile so the same browser
        fingerprint is reused for headless collection (required by Cloudflare).
        Falls back to system Chrome if available and unlocked.
        """
        print(f"\n[{self.provider}] Auth URL: {start_url}")

        async with async_playwright() as p:
            print(f"[{self.provider}] Using dedicated Playwright profile: {self._browser_profile_dir}")
            context = await self._launch_persistent(p, headless=False)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(start_url)

            print(f"\n[{self.provider}] Log in to: {start_url}")
            print(f"[{self.provider}] Press Enter here when done...")
            input()

            state = await context.storage_state()
            self._save_session(state)
            await context.close()
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
