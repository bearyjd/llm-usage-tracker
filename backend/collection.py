"""
Core collection logic — used by both the CLI and the scheduler.

Separating this from cli.py avoids circular imports and lets the daemon
run the same logic as `llm-tracker status`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from rich.console import Console

from backend.db.db import AsyncSessionLocal

console = Console()
logger = logging.getLogger(__name__)


async def collect_subscription(provider: str) -> tuple[str, bool, str]:
    """
    Run the OAuth/subscription collector for one provider.
    Returns (provider, success, message).
    Persists the snapshot on success.
    """
    from backend.collectors.claude import ClaudeCollector
    from backend.collectors.chatgpt import ChatGPTCollector
    from backend.collectors.gemini import GeminiCollector
    from backend.collectors.groq import GroqCollector
    from backend.collectors.base import CollectionError

    collector_map = {
        "claude": ClaudeCollector,
        "chatgpt": ChatGPTCollector,
        "gemini": GeminiCollector,
        "groq": GroqCollector,
    }
    collector = collector_map[provider]()
    try:
        snapshot = await collector.collect()
        snapshot.source = "subscription"
        async with AsyncSessionLocal() as session:
            session.add(snapshot)
            await session.commit()
        return provider, True, "ok"
    except CollectionError as e:
        return provider, False, str(e)
    except Exception as e:
        logger.exception("Unexpected error collecting subscription/%s", provider)
        return provider, False, f"unexpected error: {e}"


async def collect_api(providers: list[str]) -> list[tuple[str, bool, str]]:
    """
    Collect API spend/token data for the given providers.

    Preference order:
    1. LiteLLM proxy (LITELLM_BASE_URL + LITELLM_API_KEY) — one call, all providers
    2. Per-provider API collectors (browser sessions or API keys)
    """
    from backend.collectors.litellm import LiteLLMCollector, is_configured
    from backend.collectors.claude_api import ClaudeAPICollector
    from backend.collectors.chatgpt_api import ChatGPTAPICollector
    from backend.collectors.gemini_api import GeminiAPICollector
    from backend.collectors.groq_api import GroqAPICollector
    from backend.collectors.base import CollectionError

    results: list[tuple[str, bool, str]] = []

    # --- LiteLLM path ---
    if is_configured():
        try:
            snapshots = await LiteLLMCollector().collect_all()
            snapshots = [s for s in snapshots if s.provider in providers]
            async with AsyncSessionLocal() as session:
                for s in snapshots:
                    session.add(s)
                await session.commit()
            collected = {s.provider for s in snapshots}
            for p in providers:
                if p in collected:
                    results.append((p, True, "litellm"))
                else:
                    results.append((p, False, "no data in LiteLLM for this provider"))
            return results
        except CollectionError as e:
            console.print(f"  [yellow]⚠ LiteLLM: {e} — falling back to per-provider collectors[/yellow]")
        except Exception as e:
            logger.exception("Unexpected LiteLLM error")
            console.print(f"  [yellow]⚠ LiteLLM unexpected error: {e} — falling back[/yellow]")

    # --- Per-provider fallback ---
    api_collector_map = {
        "claude": ClaudeAPICollector,
        "chatgpt": ChatGPTAPICollector,
        "gemini": GeminiAPICollector,
        "groq": GroqAPICollector,
    }
    for provider in providers:
        collector = api_collector_map[provider]()
        has_creds = (
            collector.has_credentials()
            if hasattr(collector, "has_credentials")
            else collector.has_session()
        )
        if not has_creds:
            continue
        try:
            snapshot = await collector.collect()
            async with AsyncSessionLocal() as session:
                session.add(snapshot)
                await session.commit()
            results.append((provider, True, "ok"))
        except CollectionError as e:
            results.append((provider, False, str(e)))
        except Exception as e:
            logger.exception("Unexpected error collecting api/%s", provider)
            results.append((provider, False, f"unexpected error: {e}"))

    return results


async def collect_all(providers: list[str]) -> dict:
    """
    Run both subscription and API collection for all providers.
    Returns a summary dict suitable for logging.
    """
    ts = datetime.utcnow().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}] Starting collection for: {', '.join(providers)}[/dim]")

    sub_results = await asyncio.gather(*[collect_subscription(p) for p in providers])
    api_results = await collect_api(providers)

    summary = {"subscription": {}, "api": {}}
    for prov, ok, msg in sub_results:
        summary["subscription"][prov] = {"ok": ok, "msg": msg}
        icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
        console.print(f"  {icon} {prov} (subscription){'' if ok else ': ' + msg}")

    for prov, ok, msg in api_results:
        summary["api"][prov] = {"ok": ok, "msg": msg}
        icon = "[green]✓[/green]" if ok else "[yellow]⚠[/yellow]"
        console.print(f"  {icon} {prov} (api: {msg})" if ok else f"  {icon} {prov} (api): {msg}")

    return summary
