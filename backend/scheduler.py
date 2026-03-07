"""APScheduler-based background refresh daemon."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from rich.console import Console

from backend.db.db import init_db

console = Console()
logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "15"))


def run_daemon(
    providers: list[str],
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
) -> None:
    """
    Start the background scheduler and block until interrupted.

    Runs both subscription and API collection every interval_minutes.
    """
    console.print(
        f"[bold cyan]LLM Usage Tracker daemon[/bold cyan] — "
        f"refreshing every [bold]{interval_minutes}[/bold] min"
    )
    console.print(f"  Providers: {', '.join(providers)}")
    console.print("  Press Ctrl+C to stop.\n")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _tick():
        from backend.collection import collect_all
        await collect_all(providers)

    async def _main():
        await init_db()

        scheduler = AsyncIOScheduler(event_loop=loop)
        scheduler.add_job(
            _tick,
            "interval",
            minutes=interval_minutes,
            id="collect_all",
            replace_existing=True,
            next_run_time=datetime.now(),  # run immediately on start
        )
        scheduler.start()

        stop_event = asyncio.Event()

        def _shutdown():
            stop_event.set()

        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)

        await stop_event.wait()
        scheduler.shutdown(wait=False)
        console.print("\n[dim]Daemon stopped.[/dim]")

    loop.run_until_complete(_main())
