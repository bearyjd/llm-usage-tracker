"""Typer CLI entrypoint for LLM Usage Tracker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box
from sqlalchemy import select

app = typer.Typer(
    name="llm-tracker",
    help="Track usage limits across Claude, ChatGPT, and Gemini.",
    no_args_is_help=True,
)
console = Console()

PROVIDERS = ["claude", "chatgpt", "gemini", "groq"]


def _run(coro):
    return asyncio.run(coro)


async def _init():
    from backend.db.db import init_db
    await init_db()


async def _get_latest_snapshots(providers=None, source=None):
    """Return latest snapshot per (provider, source) pair."""
    from backend.db.db import AsyncSessionLocal
    from backend.db.models import UsageSnapshot

    targets = providers or PROVIDERS
    sources = [source] if source else ["subscription", "api"]
    snapshots = []
    async with AsyncSessionLocal() as session:
        for p in targets:
            for src in sources:
                stmt = (
                    select(UsageSnapshot)
                    .where(UsageSnapshot.provider == p)
                    .where(UsageSnapshot.source == src)
                    .order_by(UsageSnapshot.collected_at.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                row = result.scalar_one_or_none()
                if row:
                    snapshots.append(row)
    return snapshots


# ------------------------------------------------------------------
# auth
# ------------------------------------------------------------------

@app.command()
def auth(
    provider: Annotated[
        Optional[str],
        typer.Argument(help="Provider: claude, chatgpt, gemini, groq"),
    ] = None,
    api: Annotated[
        bool,
        typer.Option("--api", help="Auth the API dashboard instead of the subscription app"),
    ] = False,
    all_providers: Annotated[
        bool,
        typer.Option("--all", help="Auth all providers"),
    ] = False,
):
    """
    One-time browser login. Saves session for future headless collection.

    \b
    Subscription (message limit tracking):
      llm-tracker auth claude       → claude.ai
      llm-tracker auth chatgpt      → chat.openai.com
      llm-tracker auth gemini       → gemini.google.com

    API dashboards (spend/token tracking — only needed if not using LiteLLM):
      llm-tracker auth claude --api    → console.anthropic.com
      llm-tracker auth chatgpt --api   → platform.openai.com
      llm-tracker auth gemini --api    → aistudio.google.com

    Use --all to auth all providers at once.
    """
    targets = PROVIDERS if all_providers else ([provider] if provider else None)
    if targets is None:
        console.print("[red]Specify a provider or use --all[/red]")
        raise typer.Exit(1)
    if provider and provider not in PROVIDERS:
        console.print(f"[red]Unknown provider: {provider!r}. Choose from: {', '.join(PROVIDERS)}[/red]")
        raise typer.Exit(1)

    async def _do_auth():
        from backend.collectors.claude import ClaudeCollector
        from backend.collectors.chatgpt import ChatGPTCollector
        from backend.collectors.gemini import GeminiCollector
        from backend.collectors.groq import GroqCollector
        from backend.collectors.claude_api import ClaudeAPICollector
        from backend.collectors.chatgpt_api import ChatGPTAPICollector
        from backend.collectors.gemini_api import GeminiAPICollector
        from backend.collectors.groq_api import GroqAPICollector

        collector_map = (
            {"claude": ClaudeAPICollector, "chatgpt": ChatGPTAPICollector, "gemini": GeminiAPICollector, "groq": GroqAPICollector}
            if api else
            {"claude": ClaudeCollector, "chatgpt": ChatGPTCollector, "gemini": GeminiCollector, "groq": GroqCollector}
        )
        label = "API" if api else "subscription"
        for t in targets:
            console.print(f"\n[bold cyan]Authenticating {t} ({label})...[/bold cyan]")
            await collector_map[t]().auth()

    _run(_do_auth())
    console.print("\n[green]Authentication complete.[/green]")


# ------------------------------------------------------------------
# status
# ------------------------------------------------------------------

@app.command()
def status(
    no_refresh: Annotated[
        bool,
        typer.Option("--no-refresh", help="Show cached data only"),
    ] = False,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", "-p", help="Limit to one provider"),
    ] = None,
):
    """Refresh and display usage across all providers."""

    async def _status():
        await _init()
        targets = [provider] if provider else PROVIDERS

        if not no_refresh:
            from backend.collection import collect_subscription, collect_api
            console.print("[dim]Collecting subscription usage...[/dim]")
            sub_results = await asyncio.gather(*[collect_subscription(p) for p in targets])
            for prov, ok, msg in sub_results:
                icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
                suffix = "" if ok else f": {msg}"
                console.print(f"  {icon} {prov} (subscription){suffix}")

            console.print("[dim]Collecting API usage...[/dim]")
            api_results = await collect_api(targets)
            for prov, ok, msg in api_results:
                icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
                if ok:
                    console.print(f"  {icon} {prov} (api: {msg})")
                else:
                    console.print(f"  {icon} {prov} (api): {msg}")

        snapshots = await _get_latest_snapshots(providers=[provider] if provider else None)
        from backend.recommendations import print_status_table, print_recommendations, recommend
        print_status_table(snapshots)
        print_recommendations(recommend(snapshots))

    _run(_status())


# ------------------------------------------------------------------
# recommend
# ------------------------------------------------------------------

@app.command()
def recommend():
    """Show which LLM to use right now based on cached data."""

    async def _recommend():
        await _init()
        snapshots = await _get_latest_snapshots()
        from backend.recommendations import print_status_table, print_recommendations, recommend as _rec
        print_status_table(snapshots)
        print_recommendations(_rec(snapshots))
        if not snapshots:
            console.print("[yellow]Tip:[/yellow] Run [bold]llm-tracker status[/bold] first.")

    _run(_recommend())


# ------------------------------------------------------------------
# history
# ------------------------------------------------------------------

@app.command()
def history(
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", "-p", help="Filter by provider"),
    ] = None,
    source: Annotated[
        Optional[str],
        typer.Option("--source", "-s", help="Filter by source: subscription, api"),
    ] = None,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Days of history"),
    ] = 7,
):
    """Show historical usage snapshots."""

    async def _history():
        await _init()
        from backend.db.db import AsyncSessionLocal
        from backend.db.models import UsageSnapshot

        since = datetime.utcnow() - timedelta(days=days)
        async with AsyncSessionLocal() as session:
            stmt = select(UsageSnapshot).where(UsageSnapshot.collected_at >= since)
            if provider:
                if provider not in PROVIDERS:
                    console.print(f"[red]Unknown provider: {provider}[/red]")
                    raise typer.Exit(1)
                stmt = stmt.where(UsageSnapshot.provider == provider)
            if source:
                if source not in ("subscription", "api"):
                    console.print("[red]--source must be 'subscription' or 'api'[/red]")
                    raise typer.Exit(1)
                stmt = stmt.where(UsageSnapshot.source == source)
            stmt = stmt.order_by(UsageSnapshot.collected_at.desc())
            rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            console.print(
                f"[yellow]No data for the past {days} day(s).[/yellow] "
                "Run [bold]llm-tracker status[/bold] to collect some."
            )
            return

        # Split into two tables for readability
        sub_rows = [r for r in rows if r.source == "subscription"]
        api_rows = [r for r in rows if r.source == "api"]

        if sub_rows and source != "api":
            t = Table(
                title=f"Subscription History — last {days} day(s)",
                box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan",
            )
            t.add_column("Time", style="dim", min_width=14)
            t.add_column("Provider", style="bold")
            t.add_column("Used", justify="right")
            t.add_column("Limit", justify="right")
            t.add_column("% Used", justify="right")
            t.add_column("Resets In", justify="right")
            t.add_column("Tier")
            for r in sub_rows:
                pct = r.usage_pct
                pct_str = f"{pct:.0%}" if pct is not None else "?"
                color = "red" if (pct or 0) >= 0.9 else "yellow" if (pct or 0) >= 0.8 else "green"
                reset_str = _fmt_min(r.minutes_until_reset()) if r.minutes_until_reset() is not None else "?"
                t.add_row(
                    r.collected_at.strftime("%m-%d %H:%M"),
                    r.provider.title(),
                    str(r.messages_used) if r.messages_used is not None else "?",
                    str(r.messages_limit) if r.messages_limit is not None else "?",
                    f"[{color}]{pct_str}[/{color}]",
                    reset_str,
                    r.model_tier or "?",
                )
            console.print()
            console.print(t)

        if api_rows and source != "subscription":
            t = Table(
                title=f"API Usage History — last {days} day(s)",
                box=box.SIMPLE_HEAVY, show_header=True, header_style="bold magenta",
            )
            t.add_column("Time", style="dim", min_width=14)
            t.add_column("Provider", style="bold")
            t.add_column("Spend (USD)", justify="right")
            t.add_column("Input Tokens", justify="right")
            t.add_column("Output Tokens", justify="right")
            t.add_column("Period")
            for r in api_rows:
                t.add_row(
                    r.collected_at.strftime("%m-%d %H:%M"),
                    r.provider.title(),
                    f"${r.api_spend_usd:.4f}" if r.api_spend_usd is not None else "?",
                    f"{r.tokens_input:,}" if r.tokens_input is not None else "?",
                    f"{r.tokens_output:,}" if r.tokens_output is not None else "?",
                    r.api_spend_period or r.tokens_period or "?",
                )
            console.print()
            console.print(t)

    _run(_history())


def _fmt_min(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    return f"{int(minutes // 60)}h {int(minutes % 60):02d}m"


# ------------------------------------------------------------------
# check
# ------------------------------------------------------------------

@app.command()
def check():
    """Show configuration status and verify connectivity."""

    async def _check():
        await _init()
        import os
        from backend.collectors.base import SESSIONS_DIR
        from backend.collectors.litellm import is_configured, LITELLM_BASE_URL, LITELLM_API_KEY

        console.print("\n[bold]Configuration check[/bold]\n")

        # Session files
        console.print("[bold cyan]Browser sessions (subscription):[/bold cyan]")
        for p in PROVIDERS:
            path = SESSIONS_DIR / f"{p}.json"
            if path.exists():
                console.print(f"  [green]✓[/green] {p}: {path}")
            else:
                console.print(f"  [dim]✗[/dim] {p}: not set up — run [bold]llm-tracker auth {p}[/bold]")

        console.print()
        console.print("[bold cyan]Browser sessions (API dashboards):[/bold cyan]")
        api_sessions = {"claude": "claude-api", "chatgpt": "chatgpt-api", "gemini": "gemini-api", "groq": "groq-api"}
        for p, fname in api_sessions.items():
            path = SESSIONS_DIR / f"{fname}.json"
            if path.exists():
                console.print(f"  [green]✓[/green] {p}: {path}")
            else:
                console.print(f"  [dim]✗[/dim] {p}: not set up — run [bold]llm-tracker auth {p} --api[/bold]")

        console.print()
        console.print("[bold cyan]LiteLLM proxy (preferred for API tracking):[/bold cyan]")
        if is_configured():
            console.print(f"  [green]✓[/green] LITELLM_BASE_URL = {LITELLM_BASE_URL}")
            console.print(f"  [green]✓[/green] LITELLM_API_KEY  = {'*' * 8}{LITELLM_API_KEY[-4:]}")
            # Verify connectivity
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{LITELLM_BASE_URL}/health",
                        headers={"Authorization": f"Bearer {LITELLM_API_KEY}"},
                    )
                if resp.is_success:
                    console.print("  [green]✓[/green] Connectivity: OK")
                else:
                    console.print(f"  [yellow]⚠[/yellow] Connectivity: HTTP {resp.status_code}")
            except Exception as e:
                console.print(f"  [red]✗[/red] Connectivity: {e}")
        else:
            missing = []
            if not LITELLM_BASE_URL:
                missing.append("LITELLM_BASE_URL")
            if not LITELLM_API_KEY:
                missing.append("LITELLM_API_KEY")
            console.print(f"  [dim]✗[/dim] Not configured (missing: {', '.join(missing)})")
            console.print("    Set these in .env to use your LiteLLM proxy for API spend tracking.")

        console.print()
        console.print("[bold cyan]Per-provider API keys (fallback):[/bold cyan]")
        for var, label in [("OPENAI_API_KEY", "ChatGPT"), ("GOOGLE_API_KEY", "Gemini"), ("GROQ_API_KEY", "Groq")]:
            val = os.getenv(var, "")
            if val:
                console.print(f"  [green]✓[/green] {label}: {var} = {'*' * 8}{val[-4:]}")
            else:
                console.print(f"  [dim]✗[/dim] {label}: {var} not set")

        console.print()

    _run(_check())


# ------------------------------------------------------------------
# daemon
# ------------------------------------------------------------------

@app.command()
def daemon(
    interval: Annotated[
        int,
        typer.Option("--interval", "-i", help="Refresh interval in minutes"),
    ] = 15,
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", "-p", help="Only track a specific provider"),
    ] = None,
):
    """Start background auto-refresh daemon (subscription + API)."""
    targets = [provider] if provider else PROVIDERS
    from backend.scheduler import run_daemon
    run_daemon(targets, interval_minutes=interval)


# ------------------------------------------------------------------
# serve
# ------------------------------------------------------------------

@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Bind port")] = 8080,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on code changes")] = False,
):
    """Start the FastAPI server (REST API + future web UI)."""
    import uvicorn
    console.print(f"[bold cyan]Starting API server[/bold cyan] on http://{host}:{port}")
    console.print(f"  Docs: http://{host}:{port}/docs")
    console.print("  Press Ctrl+C to stop.\n")
    uvicorn.run("backend.api.routes:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
