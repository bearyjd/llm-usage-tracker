"""Database engine and session factory."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.db.models import Base

load_dotenv()

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "usage.db"
_DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}")

# Ensure data directory exists
_db_path = _DEFAULT_DB_PATH
if "sqlite" in _DATABASE_URL:
    # Extract path from URL for directory creation
    _url_path = _DATABASE_URL.replace("sqlite+aiosqlite:///", "")
    if not _url_path.startswith(":"):
        _db_path = Path(_url_path)
        _db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    _DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Dependency-injectable async session (used by FastAPI)."""
    async with AsyncSessionLocal() as session:
        yield session
