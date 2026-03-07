"""FastAPI routes — REST API backing the web UI."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.db import get_session, init_db
from backend.db.models import UsageSnapshot

app = FastAPI(
    title="LLM Usage Tracker",
    description="Tracks subscription limits and API spend across Claude, ChatGPT, and Gemini.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PROVIDERS = ["claude", "chatgpt", "gemini", "groq"]


@app.on_event("startup")
async def startup():
    await init_db()


# ------------------------------------------------------------------
# Response models
# ------------------------------------------------------------------

class SnapshotOut(BaseModel):
    id: int
    provider: str
    source: str
    collected_at: datetime
    # Subscription
    messages_used: int | None
    messages_limit: int | None
    messages_window_hours: float | None
    messages_reset_at: datetime | None
    # API
    api_spend_usd: float | None
    api_spend_period: str | None
    tokens_input: int | None
    tokens_output: int | None
    tokens_period: str | None
    # Meta
    model_tier: str | None

    class Config:
        from_attributes = True


class RecommendationOut(BaseModel):
    provider: str
    message: str
    action: str
    priority: int


class CollectResult(BaseModel):
    triggered_at: datetime
    providers: list[str]


class ConfigOut(BaseModel):
    litellm_configured: bool
    litellm_base_url: str | None
    sessions: dict[str, dict[str, bool]]  # {provider: {subscription: bool, api: bool}}
    openai_key_set: bool
    google_key_set: bool


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _latest_per_provider_source(session: AsyncSession) -> list[UsageSnapshot]:
    rows = []
    for p in PROVIDERS:
        for src in ("subscription", "api"):
            stmt = (
                select(UsageSnapshot)
                .where(UsageSnapshot.provider == p)
                .where(UsageSnapshot.source == src)
                .order_by(UsageSnapshot.collected_at.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row:
                rows.append(row)
    return rows


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/config", response_model=ConfigOut)
async def get_config():
    """Show what credentials and sessions are configured."""
    from backend.collectors.base import SESSIONS_DIR
    from backend.collectors.litellm import is_configured, LITELLM_BASE_URL

    sessions = {}
    for p in PROVIDERS:
        sessions[p] = {
            "subscription": (SESSIONS_DIR / f"{p}.json").exists(),
            "api": (SESSIONS_DIR / f"{p}-api.json").exists(),
        }

    return ConfigOut(
        litellm_configured=is_configured(),
        litellm_base_url=LITELLM_BASE_URL or None,
        sessions=sessions,
        openai_key_set=bool(os.getenv("OPENAI_API_KEY")),
        google_key_set=bool(os.getenv("GOOGLE_API_KEY")),
    )


@app.get("/api/status", response_model=list[SnapshotOut])
async def current_status(session: SessionDep):
    """Latest snapshot per (provider, source). Primary endpoint for dashboards."""
    return await _latest_per_provider_source(session)


@app.get("/api/snapshots", response_model=list[SnapshotOut])
async def list_snapshots(
    session: SessionDep,
    provider: str | None = Query(None),
    source: str | None = Query(None, description="subscription or api"),
    days: int = Query(7),
):
    """Paginated snapshot history."""
    since = datetime.utcnow() - timedelta(days=days)
    stmt = select(UsageSnapshot).where(UsageSnapshot.collected_at >= since)
    if provider:
        stmt = stmt.where(UsageSnapshot.provider == provider)
    if source:
        stmt = stmt.where(UsageSnapshot.source == source)
    stmt = stmt.order_by(UsageSnapshot.collected_at.desc())
    return (await session.execute(stmt)).scalars().all()


@app.get("/api/recommend", response_model=list[RecommendationOut])
async def get_recommendations(session: SessionDep):
    """Ranked recommendations based on current subscription usage."""
    from backend.recommendations import recommend
    snapshots = await _latest_per_provider_source(session)
    return [
        RecommendationOut(
            provider=r.provider,
            message=r.message,
            action=r.action,
            priority=r.priority,
        )
        for r in recommend(snapshots)
    ]


@app.get("/api/spend/summary")
async def spend_summary(
    session: SessionDep,
    days: int = Query(30),
):
    """Aggregate API spend per provider over the past N days."""
    since = datetime.utcnow() - timedelta(days=days)
    stmt = (
        select(UsageSnapshot)
        .where(UsageSnapshot.source == "api")
        .where(UsageSnapshot.collected_at >= since)
        .order_by(UsageSnapshot.provider, UsageSnapshot.collected_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    # Latest snapshot per provider (most recent has cumulative monthly spend)
    seen: dict[str, UsageSnapshot] = {}
    for row in rows:
        if row.provider not in seen:
            seen[row.provider] = row

    return {
        p: {
            "spend_usd": s.api_spend_usd,
            "period": s.api_spend_period,
            "tokens_input": s.tokens_input,
            "tokens_output": s.tokens_output,
            "collected_at": s.collected_at.isoformat(),
        }
        for p, s in seen.items()
    }


@app.post("/api/collect", response_model=CollectResult)
async def trigger_collection(
    background_tasks: BackgroundTasks,
    providers: list[str] | None = None,
):
    """
    Trigger a fresh collection in the background.
    Returns immediately; collection runs async.
    """
    targets = [p for p in (providers or PROVIDERS) if p in PROVIDERS]

    async def _run():
        from backend.collection import collect_all
        await collect_all(targets)

    background_tasks.add_task(asyncio.create_task, _run())

    return CollectResult(triggered_at=datetime.utcnow(), providers=targets)
