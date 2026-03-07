"""SQLAlchemy models for LLM usage tracking."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UsageSnapshot(Base):
    """One row per collection snapshot per provider."""

    __tablename__ = "usage_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # 'claude' | 'chatgpt' | 'gemini'
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="subscription")  # 'subscription' | 'api'
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # Subscription message limits
    messages_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    messages_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    messages_window_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    messages_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # API costs (if applicable)
    api_spend_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_spend_period: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'daily' | 'monthly'

    # API token usage
    tokens_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_period: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'daily' | 'monthly'

    # Rate limits
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_tpm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Feature access
    model_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 'free' | 'plus' | 'pro' | 'team'
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON blob

    # Raw data for debugging
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def features(self) -> dict[str, Any]:
        if self.features_json:
            return json.loads(self.features_json)
        return {}

    @features.setter
    def features(self, value: dict[str, Any]) -> None:
        self.features_json = json.dumps(value) if value else None

    @property
    def raw(self) -> dict[str, Any]:
        if self.raw_json:
            return json.loads(self.raw_json)
        return {}

    @raw.setter
    def raw(self, value: dict[str, Any]) -> None:
        self.raw_json = json.dumps(value) if value else None

    @property
    def messages_remaining(self) -> int | None:
        if self.messages_used is not None and self.messages_limit is not None:
            return self.messages_limit - self.messages_used
        return None

    @property
    def usage_pct(self) -> float | None:
        if self.messages_used is not None and self.messages_limit and self.messages_limit > 0:
            return self.messages_used / self.messages_limit
        return None

    def minutes_until_reset(self) -> float | None:
        if self.messages_reset_at is None:
            return None
        delta = self.messages_reset_at - datetime.utcnow()
        return max(0.0, delta.total_seconds() / 60)

    def __repr__(self) -> str:
        return (
            f"<UsageSnapshot provider={self.provider!r} "
            f"used={self.messages_used}/{self.messages_limit} "
            f"at={self.collected_at}>"
        )
