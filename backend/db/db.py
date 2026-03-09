"""Database engine and session factory."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.db.models import Base

load_dotenv()

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "usage.db"


def _resolve_db_url() -> str:
    url = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}")
    if "sqlite" in url:
        raw_path = url.replace("sqlite+aiosqlite:///", "")
        if not raw_path.startswith(":"):
            db_path = Path(raw_path)
            if not db_path.is_absolute():
                db_path = _PROJECT_ROOT / db_path
                url = f"sqlite+aiosqlite:///{db_path}"
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return url


_DATABASE_URL = _resolve_db_url()

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
