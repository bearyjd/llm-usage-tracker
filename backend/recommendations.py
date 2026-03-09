"""Recommendation engine for LLM usage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from backend.db.models import UsageSnapshot

console = Console()

# Thresholds
WARN_PCT = 0.80       # warn when 80%+ used
CRITICAL_PCT = 0.90   # critical when 90%+ used
RESET_SOON_MIN = 30   # "resets soon" if within 30 minutes
WAIT_WINDOW_MIN = 60  # suggest waiting if reset within 1 hour


@dataclass
class Recommendation:
    provider: str
    message: str
    priority: int = 0  # lower = more urgent to act on
    action: str = "use"  # 'use' | 'wait' | 'avoid' | 'upgrade'


def recommend(snapshots: list[UsageSnapshot]) -> list[Recommendation]:
    """
    Rank providers and generate recommendations.

    Rules:
    - 90%+ used: avoid / wait for reset
    - 80–90% used: warn, suggest alternatives
    - Reset within 30min AND high usage: suggest waiting
    - Rank by: remaining_pct DESC, reset_soon ASC
    """
    recs: list[Recommendation] = []

    # Only subscription snapshots have message counts for recommendations
    snapshots = [s for s in snapshots if s.source == "subscription"]

    if not snapshots:
        return [Recommendation("all", "No usage data collected yet. Run: llm-tracker status", 99)]

    # Sort by most available first
    def sort_key(s: UsageSnapshot):
        pct = s.usage_pct or 0.0
        reset_min = s.minutes_until_reset() or 999
        return (pct, -reset_min)  # low usage + late reset = use first

    ranked = sorted(snapshots, key=sort_key)

    for snapshot in ranked:
        pct = snapshot.usage_pct
        remaining = snapshot.messages_remaining
        reset_min = snapshot.minutes_until_reset()
        is_pct = _is_percentage_based(snapshot)
        name = snapshot.provider.title()

        if pct is None:
            recs.append(
                Recommendation(snapshot.provider, f"{name}: usage data unavailable", priority=50, action="unknown")
            )
            continue

        remaining_label = f"{100 - int(pct * 100)}% free" if is_pct else f"{remaining} messages left ({(1 - pct):.0%} free)"

        if pct >= CRITICAL_PCT:
            if reset_min is not None and reset_min <= WAIT_WINDOW_MIN:
                recs.append(
                    Recommendation(snapshot.provider, f"{name} is nearly exhausted — resets in {_fmt_min(reset_min)}. Wait or switch.", priority=10, action="wait")
                )
            else:
                recs.append(
                    Recommendation(snapshot.provider, f"{name} is at {pct:.0%} — avoid until reset.", priority=20, action="avoid")
                )
        elif pct >= WARN_PCT:
            alt = _best_alternative(snapshot.provider, snapshots)
            alt_str = f" Consider {alt.title()}." if alt else ""
            recs.append(
                Recommendation(snapshot.provider, f"{name} is at {pct:.0%} ({remaining_label}).{alt_str}", priority=30, action="warn")
            )
        else:
            recs.append(
                Recommendation(snapshot.provider, f"{name} has {remaining_label} — good to use.", priority=60 + int(pct * 10), action="use")
            )

    return sorted(recs, key=lambda r: r.priority)


def _best_alternative(exclude_provider: str, snapshots: list[UsageSnapshot]) -> str | None:
    others = [s for s in snapshots if s.provider != exclude_provider and s.usage_pct is not None]
    if not others:
        return None
    best = min(others, key=lambda s: s.usage_pct or 1.0)
    return best.provider if (best.usage_pct or 1.0) < WARN_PCT else None


def _fmt_min(minutes: float) -> str:
    if minutes < 60:
        return f"{int(minutes)}m"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m:02d}m"


# ------------------------------------------------------------------
# Rich display
# ------------------------------------------------------------------

def _is_percentage_based(s: UsageSnapshot) -> bool:
    return s.provider == "claude" and s.messages_limit == 100


STATUS_ICONS = {
    "use": "[green]✓ Good[/green]",
    "warn": "[yellow]⚠ Low[/yellow]",
    "avoid": "[red]✗ Avoid[/red]",
    "wait": "[yellow]⏳ Wait[/yellow]",
    "unknown": "[dim]? Unknown[/dim]",
}

ACTION_ARROWS = {
    "use": "[green]→[/green]",
    "warn": "[yellow]⚠[/yellow]",
    "avoid": "[red]✗[/red]",
    "wait": "[yellow]⏳[/yellow]",
    "unknown": "[dim]?[/dim]",
}


def print_status_table(snapshots: list[UsageSnapshot]) -> None:
    """Print a Rich table showing current usage across all providers."""
    sub_snapshots = [s for s in snapshots if s.source == "subscription"]
    api_snapshots = [s for s in snapshots if s.source == "api"]

    # --- Subscription table ---
    sub_table = Table(
        title="Subscription Usage",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        expand=False,
    )
    sub_table.add_column("Provider", style="bold", min_width=10)
    sub_table.add_column("Used", justify="right")
    sub_table.add_column("Limit", justify="right")
    sub_table.add_column("Remaining", justify="right")
    sub_table.add_column("Window", justify="right")
    sub_table.add_column("Resets In", justify="right")
    sub_table.add_column("Status", justify="center")

    if not sub_snapshots:
        sub_table.add_row("(no data)", "—", "—", "—", "—", "—", "[dim]run status[/dim]")
    else:
        for s in sorted(sub_snapshots, key=lambda x: x.provider):
            is_pct = _is_percentage_based(s)
            if is_pct and s.messages_used is not None:
                used_str = f"{s.messages_used}%"
                limit_str = "—"
                remaining_str = f"{100 - s.messages_used}%"
            else:
                used_str = str(s.messages_used) if s.messages_used is not None else "?"
                limit_str = str(s.messages_limit) if s.messages_limit is not None else "?"
                remaining_str = str(s.messages_remaining) if s.messages_remaining is not None else "?"
            window_str = f"{s.messages_window_hours:.0f}h" if s.messages_window_hours else "?"
            reset_str = _fmt_min(s.minutes_until_reset()) if s.minutes_until_reset() is not None else "?"

            pct = s.usage_pct or 0.0
            if pct >= CRITICAL_PCT:
                action = "avoid"
            elif pct >= WARN_PCT:
                action = "warn"
            elif s.messages_used is None:
                action = "unknown"
            else:
                action = "use"

            sub_table.add_row(
                s.provider.title(),
                used_str,
                limit_str,
                remaining_str,
                window_str,
                reset_str,
                STATUS_ICONS[action],
            )

    console.print()
    console.print(sub_table)

    # --- API usage table (only if we have data) ---
    if api_snapshots:
        api_table = Table(
            title="API Usage (this month)",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold magenta",
            expand=False,
        )
        api_table.add_column("Provider", style="bold", min_width=10)
        api_table.add_column("Spend (USD)", justify="right")
        api_table.add_column("Input Tokens", justify="right")
        api_table.add_column("Output Tokens", justify="right")
        api_table.add_column("Tier")

        for s in sorted(api_snapshots, key=lambda x: x.provider):
            spend_str = f"${s.api_spend_usd:.4f}" if s.api_spend_usd is not None else "?"
            input_str = f"{s.tokens_input:,}" if s.tokens_input is not None else "?"
            output_str = f"{s.tokens_output:,}" if s.tokens_output is not None else "?"
            tier_str = s.model_tier or "?"
            api_table.add_row(
                s.provider.title(),
                spend_str,
                input_str,
                output_str,
                tier_str,
            )

        console.print()
        console.print(api_table)


def print_recommendations(recs: list[Recommendation]) -> None:
    """Print recommendation bullets."""
    if not recs:
        console.print("\n[dim]No recommendations available.[/dim]")
        return

    console.print("\n[bold]Recommendations:[/bold]")
    for rec in recs:
        arrow = ACTION_ARROWS.get(rec.action, "→")
        console.print(f"  {arrow} {rec.message}")
    console.print()
