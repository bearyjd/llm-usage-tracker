# CLAUDE.md

Guidance for an AI agent working in this repository. See also
`.agent_native/agent_roadmap.md` for a prioritized list of gaps and next steps toward
full agent-autonomy (test coverage, reproduction fixtures, structural refactors).

## What this repo is

A Python backend (FastAPI + Typer CLI + SQLAlchemy/SQLite) that tracks LLM subscription
usage and API spend across Claude, ChatGPT, Gemini, and Groq, plus a TypeScript
OpenCode plugin (`plugin/`) that surfaces that data inside OpenCode sessions.

- `backend/collectors/` — one collector per provider per data type (subscription vs API
  spend). Each talks to a live external service via Playwright (headless browser) or
  `httpx` with saved session cookies (`auth/sessions/*.json`, gitignored).
- `backend/db/models.py` — single `UsageSnapshot` SQLAlchemy model, one row per
  collection run per (provider, source).
- `backend/recommendations.py` — pure rule-based ranking logic, no I/O. Highest-value
  target for unit tests (see roadmap item 1).
- `backend/api/routes.py` — FastAPI REST API, default port 48372.
- `backend/cli.py` — Typer CLI entry point (`llm-tracker ...`).
- `plugin/src/` — TypeScript OpenCode plugin; calls the backend over HTTP.

## Environment setup (verified on this machine)

There is **no committed virtualenv and the project's own dependencies are not
installed** anywhere discoverable (`pip show llm-usage-tracker` → not found; `import
sqlalchemy` fails at the system Python). Before running or testing backend code:

```bash
# From the repo root
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Do **not** run `playwright install chromium` unless you are actually exercising a
collector's live network/browser path — it is a large download and is not required for
logic-only tests (recommendation engine, fixture-based parse tests).

The TypeScript plugin already has `node_modules/` present (verified: `plugin/typescript`
and `plugin/@opencode-ai` resolve). From `plugin/`:

```bash
npm run typecheck   # tsc --noEmit — verified runs, this is also the lint script
```

## Testing

**There are currently no test files in this repository** (`find . -iname "*test*"`
returns nothing outside `auth/browser_profiles/` browser cache artifacts). `pytest` and
`pytest-asyncio` are declared as dev dependencies in `pyproject.toml`
(`asyncio_mode = "auto"` is already configured) but no `tests/` directory exists yet.

When adding tests:
```bash
pytest tests/ -v
```

Prioritize pure-logic modules first (`backend/recommendations.py`) since they require no
mocking, no fixtures, and no network/browser setup — see
`.agent_native/agent_roadmap.md` items 1–2 for the concrete plan (fixture-based collector
parse tests, in-memory SQLite replay harness).

## Linting

`ruff` is available and configured (`pyproject.toml`: `line-length = 100`,
`target-version = "py312"`):

```bash
ruff check backend/          # verified: currently reports 14 auto-fixable issues
ruff check backend/ --fix    # unused imports, mostly in recommendations.py
```

Run `ruff check` before committing; fix or explicitly justify any new findings.

## Conventions specific to this codebase

- **Percentage-based vs count-based usage** is determined by
  `recommendations.py::_is_percentage_based`, currently a hardcoded check
  (`provider == "claude" and messages_limit == 100`). If you add a provider whose
  subscription usage is percentage-based rather than a message count, extend this
  check explicitly — do not assume `messages_limit == 100` is a safe universal signal.
- **Collector auth precedence for Claude** deliberately checks both a saved Playwright
  session (`auth/sessions/claude.json`) and `~/.claude/.credentials.json`, preferring
  the session file because it yields real message counts; the credentials file only
  yields subscription tier. Preserve this ordering if refactoring `ClaudeCollector`.
- **API spend collection** prefers a configured LiteLLM proxy
  (`LITELLM_BASE_URL`/`LITELLM_API_KEY` in `.env`) over per-provider API keys, and only
  reports per-provider fallback for providers LiteLLM did *not* return data for
  (`backend/collection.py::collect_api`). Do not silently change this precedence.
- **Datetimes are naive UTC throughout** (`datetime.utcnow()`, no tzinfo). Do not
  introduce timezone-aware datetimes into `UsageSnapshot` fields without auditing every
  comparison site (`minutes_until_reset`, CLI table rendering, recommendation sorting).
- **Never commit** `auth/sessions/`, `auth/browser_profiles/`, `.env`, or `data/` — all
  are gitignored because they contain live session cookies, browser fingerprint state,
  or usage history. If a bug report includes any of these, redact before writing a test
  fixture (see roadmap item 2 for the pattern: extract only the parsed JSON shape, not
  raw cookies).
- **Collectors mix network/browser fetch and response parsing in one method** today
  (e.g. `ClaudeCollector._collect_via_http`). When fixing a parsing bug, look for a
  private `_parse_*` method to test/patch directly rather than the full `collect()` —
  this avoids needing live credentials to reproduce most bugs. See roadmap item 3 for
  the proposed `fetch()`/`parse()` split that would make this uniform across all 8
  collectors.

## Do not invent commands

Only the commands above have been verified against this machine. If you need a command
not listed here (e.g., running the FastAPI server, running the daemon), read the
relevant section of `README.md` first — it documents `llm-tracker serve`,
`llm-tracker daemon --interval N`, `llm-tracker status`, `llm-tracker recommend`,
`llm-tracker history`, and `llm-tracker auth <provider>` in detail. Those were not
executed during this audit (they require either a live server bind or live provider
credentials) and should be verified before being asserted as working.
