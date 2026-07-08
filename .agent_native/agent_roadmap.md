# Agent-Native Roadmap — llm-usage-tracker

Goal: an AI coding agent should be able to take a raw bug report or feature request,
reproduce it, implement a fix, test it, and verify it end-to-end with minimal human
input. This audit maps the gaps that currently force a human into the loop, ranked by
**Human-Attention-Saved per Unit of Effort** (HAS/UE) — highest leverage first.

Current state (verified, not assumed):
- **Zero test files exist anywhere in the repo.** `find . -iname "*test*"` returns nothing.
  `pytest` and `ruff` are installed on the host but the project's own dependencies
  (`sqlalchemy`, `fastapi`, `httpx`, `playwright`, etc.) are **not installed** in any
  discoverable environment (`pip show llm-usage-tracker` → not found, no `.venv`,
  `import sqlalchemy` fails). An agent cannot currently run anything beyond `ruff check`
  without first creating a venv and installing `-e .[dev]`.
- ~~`ruff check backend/` runs clean-ish today: 14 auto-fixable issues (unused imports in
  `backend/recommendations.py` and elsewhere), all `--fix`-able.~~ ✅ DONE — ran
  `ruff check backend/ --fix`; `ruff check backend/` now reports "All checks passed!"
  (0 issues). No type errors caught because nothing type-checks the code (no
  mypy/pyright config).
- ✅ Environment now set up: `.venv/` created, `pip install -e ".[dev]"` succeeds,
  `pytest tests/` runs (34 tests passing as of this pass — see items 1 and 2 below).
  Playwright browser binaries were not installed (not needed for logic/parse-layer
  tests).
- The four "subscription" collectors (`claude.py`, `chatgpt.py`, `gemini.py`, `groq.py`)
  and four "API spend" collectors talk to live external services (claude.ai, OpenAI,
  Google, Groq) via Playwright browser automation or authenticated HTTP, using session
  cookies stored in `auth/sessions/*.json` (gitignored) or live credentials
  (`~/.claude/.credentials.json`). **None of this is reproducible offline.** There is no
  fixture capturing what a real `usage` API response looks like for any provider.
- `backend/recommendations.py` (pure logic, zero I/O, the rule-based ranking engine) is
  the single highest-value, easiest-to-test module in the repo and currently has 0%
  coverage.
- The TypeScript plugin (`plugin/src/`) has a `tsc --noEmit` script wired up
  (`npm run typecheck` inside `plugin/`) but no test runner, no unit tests for
  `format.ts` (pure formatting logic, same profile as `recommendations.py`).

## Top 5 — immediately actionable

### 1. Unit-test `backend/recommendations.py` (pure logic, no I/O, no mocks needed) — ✅ DONE
Implemented in `tests/test_recommendations.py` (24 tests, all passing). Covers pct
boundaries (0/79/80/89/90/100), reset timing (past/future/none, including the
`minutes_until_reset()` negative-clamp-to-zero behavior), `source="api"` exclusion,
empty-input "no data" message, priority ordering, `_best_alternative`, and
`_is_percentage_based` (including the footgun case: a non-Claude provider with
`messages_limit == 100` must NOT be read as percentage-based). Assertions target
`.action`/`.priority`, not message strings, per the acceptance criteria.

**HAS/UE: highest.** This is the module a bug report is most likely to name ("wrong
provider recommended", "priority ordering off", "% Used bar wrong color"), and it's
pure functions over `UsageSnapshot` objects — no browser, no DB, no network.
- **Files:** create `tests/test_recommendations.py`.
- **Command:** `pytest tests/test_recommendations.py -v` (after `pip install -e ".[dev]"`).
- **Acceptance criteria:**
  - Construct `UsageSnapshot` instances directly (no DB) covering: `pct` at 0%, 79%,
    80%, 89%, 90%, 100%; `messages_reset_at` in the past/future/None; `source="api"`
    snapshots excluded from ranking; empty snapshot list returns the "no data" message.
  - Assert on `Recommendation.action` and `.priority` ordering, not string message
    text (strings will churn; the review copy in `agent_roadmap` item 3 fixes this
    properly).
  - Cover `_best_alternative` and `_is_percentage_based` (the Claude-specific
    percentage-vs-count branch is a real footgun — `messages_limit == 100` is used as
    a proxy for "this is a percentage, not a count," which will silently misfire for
    any other provider that happens to have a 100-message limit).

### 2. Record fixture HTTP responses for each collector's `_parse_usage_response` / equivalent — 🟡 PARTIAL (claude done, 7 collectors remain)
Implemented for `ClaudeCollector` only: `tests/fixtures/claude_usage_response*.json` (3
synthetic fixtures: both-windows, message-count-shape, empty/missing-fields) and
`tests/test_claude_collector.py` (10 tests) calling `ClaudeCollector()._parse_usage_response`
directly — no Playwright, no network. Confirmed during this pass that every other collector
(`chatgpt.py::_parse_accounts`, `gemini.py::_parse_api_data`, `groq.py::_parse_api`,
`claude_api.py::_parse_usage`/`_parse_cost`, `chatgpt_api.py`, `gemini_api.py::_parse_usage`,
`groq_api.py::_parse_spend`) already has the same `(snapshot, data) -> UsageSnapshot`
shape, so the same pattern applies directly — remaining work is fixture authoring + tests
per collector, no code changes needed first.
**HAS/UE: very high.** Every collector splits cleanly into "get authenticated data" (network/
browser, not reproducible) and "parse the response into a `UsageSnapshot`" (pure,
reproducible). Right now the parse logic is untested and only exercisable by live auth +
network calls.
- **Files:**
  - `tests/fixtures/claude_usage_response.json` — capture one real (redacted) response
    shape from `backend/collectors/claude.py:_parse_usage_response` (fields:
    `five_hour_utilization` / `seven_day_utilization`, `plan_name`, `extra_usage`).
  - `tests/test_claude_collector.py` — load the fixture, call
    `ClaudeCollector()._parse_usage_response(snapshot, data)` directly, assert
    `messages_used`, `messages_limit == 100`, `messages_window_hours`,
    `messages_reset_at`.
  - Repeat for `chatgpt.py`, `gemini.py`, `groq.py`, `*_api.py` — 8 collectors total,
    each with one `_parse_*` or equivalent method that takes raw dict → `UsageSnapshot`.
- **Command:** `pytest tests/test_*_collector.py -v`.
- **Acceptance criteria:** each fixture-driven test exercises the parsing branch logic
  (missing fields, both utilization windows present, reset timestamp present/absent)
  without touching Playwright or the network. This is what lets an agent reproduce
  "Claude shows 0% used when it should show 45%" from a bug report that includes a
  pasted API response, without needing live credentials.

### 3. Refactor collectors to separate `fetch()` (network/browser) from `parse()` (pure)
**HAS/UE: high, one-time structural cost.** `base.py:_fetch_json` and each collector's
`collect()` currently interleave network I/O and parsing in the same method (see
`ClaudeCollector._collect_via_http`, `groq.py`, `gemini.py` likely follow the same
shape). Item 2 above works around this by calling private `_parse_*` methods directly,
but a formal `fetch() -> dict` / `parse(dict) -> UsageSnapshot` split on `BaseCollector`
would make every collector uniformly testable and let an agent add a 5th provider by
only writing `parse()` and copy-pasting `fetch()`.
- **Files:** `backend/collectors/base.py` (add abstract `parse(raw: dict) -> UsageSnapshot`),
  each of the 8 collector files.
- **Acceptance criteria:** `collect()` on every collector becomes
  `return self.parse(await self.fetch())`; existing fixture tests from item 2 continue
  to pass unchanged.

### 4. Add a `--fixture` / offline mode to `collect_all` for full-pipeline replay
**HAS/UE: medium-high.** Once fixtures exist (item 2), wire `backend/collection.py`
to accept an injected fixture directory instead of real collectors, so an agent can run
`llm-tracker status` (or a new `llm-tracker replay tests/fixtures/`) against canned data
and see the exact same Rich table + recommendation output a human would see, without any
live session. This is the reproduction harness for "recommend picked the wrong
provider" bug reports end-to-end, not just at the parse layer.
- **Files:** `backend/collection.py`, `backend/cli.py` (new `replay` command).
- **Acceptance criteria:** `llm-tracker replay tests/fixtures/scenario_claude_at_90pct/`
  loads JSON fixtures per provider, builds `UsageSnapshot` rows in a throwaway in-memory
  SQLite DB, and runs the real `recommend()` + `print_status_table()` — an agent can
  hand-craft a scenario directory and get the exact CLI output for a bug repro.

### 5. Pin down and document a runnable environment (venv + install command) so agents don't guess
**HAS/UE: high, near-zero effort, blocks everything else.** Right now nothing in the repo
tells an agent how to get `pytest`, `ruff`, or the app's own dependencies onto the path.
`pip install -e ".[dev]"` is the correct command (per `pyproject.toml`) but it's not
written anywhere outside this audit, and it hasn't been verified to succeed (no lockfile,
Playwright browser binaries not installed either — `playwright install chromium` is
required separately and is a multi-hundred-MB download, worth flagging to the agent as
"skip this for logic-only test runs").
- **Files:** `CLAUDE.md` (this audit adds it — see repo root).
- **Acceptance criteria:** a fresh agent can run the exact commands in `CLAUDE.md` and get
  `pytest` passing (once items 1-2 add tests) without needing `playwright install`
  unless it's touching a collector's live-network path.

## Broader structural notes (lower individual leverage, still worth tracking)

- **No `tests/` directory, no `conftest.py`, no `pytest.ini` markers** despite
  `pyproject.toml` already declaring `pytest-asyncio` as a dev dependency and
  `asyncio_mode = "auto"` — the scaffolding intent is there, just unbuilt.
- **`backend/db/db.py` was not read in this pass** but `AsyncSessionLocal` is imported
  everywhere; item 4's in-memory SQLite replay depends on `init_db()` accepting an
  override URL (`DATABASE_URL` env var already exists per `.env.example`, so this is
  likely just `DATABASE_URL=sqlite+aiosqlite:///:memory:` — verify before relying on it).
  Confirm `backend/db/db.py::init_db` reads `DATABASE_URL` at call time, not import time,
  or per-test DB isolation will silently share state.
- **Plugin (`plugin/src/`) has zero tests**, only `tsc --noEmit`. `format.ts` (pure
  string formatting from snapshot/recommendation objects, same shape as
  `recommendations.py`) is the equivalent quick win on the TypeScript side — same
  HAS/UE profile as item 1, just smaller surface area. Not in the top 5 only because the
  Python side has 4x the untested surface.
- **Two stray copies of session/db state exist**: `plugin/auth/sessions/claude.json`
  and `plugin/data/usage.db` alongside the backend's own `auth/` and `data/` — both are
  gitignored (`plugin/auth/`, `plugin/data/` in `.gitignore`) but their presence suggests
  the plugin has at some point run its own backend copy rather than talking to the
  shared one at `127.0.0.1:48372`. Worth a human sanity check on whether this is
  intentional (e.g., a second dev checkout) or a symptom of the auto-start logic in
  `plugin/src/index.ts::ensureBackend` spawning a backend rooted in the wrong directory.
- **CollectionError swallowing is inconsistent in verbosity**: some paths truncate
  `response.text[:200]` (good — bounds error messages), others via `Exception as e:
  ... f"unexpected error: {e}"` (in `collection.py`) don't bound the stringified
  exception at all. Low risk (logged, not stored raw), but worth a lint rule if this
  repo grows a security-review gate.

## Human-judgment chokepoints worth codifying into CLAUDE.md

1. **Which snapshot fields are percentage-based vs count-based** — currently encoded as
   a magic check (`provider == "claude" and messages_limit == 100`) in
   `recommendations.py::_is_percentage_based`. An agent adding a 5th provider needs to
   know this convention exists and where to extend it.
2. **LiteLLM-first, per-provider-fallback ordering** in `collect_api()` — the precedence
   and the "only report providers LiteLLM actually returned data for" logic
   (`backend/collection.py:73-92`) is a design decision, not derivable from types alone.
3. **Cookie/session vs credentials-file auth precedence** for Claude specifically
   (`ClaudeCollector.has_session()` checks both `auth/sessions/claude.json` *and*
   `~/.claude/.credentials.json`, preferring the session file) — this dual-path is easy
   to break silently if refactored without the "try http first, fall back to
   creds-file, but creds-file is percentage/tier-only, not real message counts"
   ordering.
4. **Reset time correctness across `_fmt_min`/`minutes_until_reset`** assumes
   `datetime.utcnow()` naive comparisons throughout the codebase — mixing timezone-aware
   and naive datetimes here is a classic latent bug an agent could introduce while
   "fixing" something unrelated.
