"""Shared pytest fixtures/helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

from backend.db.models import UsageSnapshot


def make_snapshot(
    provider: str,
    *,
    messages_used: int | None = None,
    messages_limit: int | None = None,
    source: str = "subscription",
    reset_in_minutes: float | None = None,
    window_hours: float | None = None,
    model_tier: str | None = None,
) -> UsageSnapshot:
    """Build an in-memory UsageSnapshot without touching the DB.

    `reset_in_minutes` may be negative (past reset), positive (future reset),
    or None (no reset info).
    """
    reset_at = None
    if reset_in_minutes is not None:
        reset_at = datetime.utcnow() + timedelta(minutes=reset_in_minutes)

    return UsageSnapshot(
        provider=provider,
        source=source,
        collected_at=datetime.utcnow(),
        messages_used=messages_used,
        messages_limit=messages_limit,
        messages_window_hours=window_hours,
        messages_reset_at=reset_at,
        model_tier=model_tier,
    )
