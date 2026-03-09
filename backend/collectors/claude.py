"""Claude usage collector.

Collection priority:
  1. httpx with saved session cookies → claude.ai/api/organizations/{orgId}/usage
     (no browser needed, uses cookies from headed auth)
  2. ~/.claude/.credentials.json → subscription tier only (no message counts)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

from backend.collectors.base import BaseCollector, CollectionError
from backend.db.models import UsageSnapshot

ORGS_API_URL = "https://claude.ai/api/organizations"
LOGIN_URL = "https://claude.ai/login"

_CLAUDE_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _read_claude_credentials() -> dict | None:
    if not _CLAUDE_CREDS_PATH.exists():
        return None
    try:
        data = json.loads(_CLAUDE_CREDS_PATH.read_text())
        return data.get("claudeAiOauth")
    except (json.JSONDecodeError, KeyError):
        return None


class ClaudeCollector(BaseCollector):
    provider = "claude"

    async def auth(self) -> None:  # type: ignore[override]
        await super().auth(LOGIN_URL)

    def has_session(self) -> bool:
        return super().has_session() or _read_claude_credentials() is not None

    async def collect(self) -> UsageSnapshot:
        if super().has_session():
            return await self._collect_via_http()

        creds = _read_claude_credentials()
        if creds:
            return self._collect_from_credentials()

        raise CollectionError(
            "No Claude session or credentials found. Run: llm-tracker auth claude"
        )

    async def _collect_via_http(self) -> UsageSnapshot:
        cookies = self._get_session_cookies("claude")
        session_key = cookies.get("sessionKey")
        if not session_key:
            raise CollectionError("No sessionKey in saved session — re-run: llm-tracker auth claude")

        headers = {**_HTTP_HEADERS, "Cookie": f"sessionKey={session_key}"}
        async with httpx.AsyncClient(
            timeout=15,
            headers=headers,
            follow_redirects=True,
        ) as client:
            orgs_resp = await client.get(ORGS_API_URL)
            if not orgs_resp.is_success:
                raise CollectionError(f"HTTP {orgs_resp.status_code} from {ORGS_API_URL}: {orgs_resp.text[:200]}")
            orgs = orgs_resp.json()
            if not orgs:
                raise CollectionError("No organizations returned from Claude API")
            org_id = orgs[0]["uuid"]

            usage_url = f"{ORGS_API_URL}/{org_id}/usage"
            usage_resp = await client.get(usage_url)
            if not usage_resp.is_success:
                raise CollectionError(f"HTTP {usage_resp.status_code} from {usage_url}: {usage_resp.text[:200]}")
            data = usage_resp.json()

        snapshot = self._base_snapshot()
        snapshot.raw = data
        return self._parse_usage_response(snapshot, data)

    def _collect_from_credentials(self) -> UsageSnapshot:
        creds = _read_claude_credentials()
        if not creds:
            raise CollectionError("No ~/.claude/.credentials.json found")

        snapshot = self._base_snapshot()
        snapshot.model_tier = creds.get("subscriptionType")
        snapshot.raw = {
            "source": "credentials",
            "subscriptionType": creds.get("subscriptionType"),
            "rateLimitTier": creds.get("rateLimitTier"),
            "expiresAt": creds.get("expiresAt"),
        }

        tier = creds.get("rateLimitTier", "")
        if "max" in tier.lower():
            snapshot.features = {
                "rateLimitTier": tier,
                "note": "Tier info only — auth with browser for message counts",
            }

        return snapshot

    def _parse_usage_response(self, snapshot: UsageSnapshot, data: dict) -> UsageSnapshot:
        five_hour = data.get("five_hour_utilization") or data.get("five_hour") or {}
        seven_day = data.get("seven_day_utilization") or data.get("seven_day") or {}

        # Claude API returns utilization as a percentage (0–100), not message counts.
        # We pick the more restrictive window (higher utilization) to surface the
        # tightest constraint. Stored as messages_used=pct, messages_limit=100.
        best_window = None
        best_pct = -1.0

        for window_data, hours in [(five_hour, 5.0), (seven_day, 168.0)]:
            if not window_data:
                continue
            pct = None
            if "messages_sent" in window_data and "messages_limit" in window_data:
                sent = window_data["messages_sent"]
                limit = window_data["messages_limit"]
                if limit and limit > 0:
                    pct = round(sent / limit * 100)
            elif "utilization" in window_data:
                pct = round(float(window_data["utilization"]))

            if pct is not None and pct > best_pct:
                best_pct = pct
                reset_at_str = window_data.get("resets_at") or window_data.get("reset_at") or data.get("reset_at")
                best_window = (pct, hours, reset_at_str)

        if best_window:
            pct, hours, reset_at_str = best_window
            snapshot.messages_used = pct
            snapshot.messages_limit = 100
            snapshot.messages_window_hours = hours
            if reset_at_str:
                snapshot.messages_reset_at = _parse_iso(reset_at_str)

        snapshot.model_tier = data.get("plan_name") or data.get("subscription_tier")

        features: dict = {}
        for window_key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus",
                           "seven_day_cowork", "seven_day_oauth_apps"):
            window = data.get(window_key)
            if window and isinstance(window, dict) and "utilization" in window:
                features[window_key] = window
        extra = data.get("extra_usage")
        if extra and isinstance(extra, dict):
            features["extra_usage"] = extra
        if features:
            snapshot.features = features

        return snapshot


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None
