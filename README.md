# LLM Usage Tracker

Track subscription limits and API spend across Claude, ChatGPT, Gemini, and Groq. Get recommendations on which provider to use based on remaining headroom and upcoming resets. Works as a standalone CLI and as an OpenCode plugin with live in-session usage display.

## Features

- **Multi-provider tracking** -- Claude, ChatGPT, Gemini, Groq subscription limits and API spend in one place
- **Smart recommendations** -- rule-based engine ranks providers by available headroom, reset timing, and cost
- **OpenCode integration** -- inline usage summary after every AI response, slash commands, and an `llm_usage` tool the AI can call
- **LiteLLM support** -- single integration point for API spend tracking across all providers, with automatic model-to-provider attribution (handles routing prefixes like `openai/claude-opus`)
- **Cloudflare-safe auth** -- dedicated persistent browser profiles per provider so session cookies survive across collection runs
- **Background daemon** -- auto-refreshes data on a configurable interval
- **REST API** -- FastAPI server backing the plugin, web UIs, and programmatic access
- **SQLite storage** -- full usage history with snapshots for trend analysis

## Quick Start

```bash
# Install
pip install -e .

# Install Playwright browsers
playwright install chromium

# Copy and edit environment config
cp .env.example .env

# Authenticate (opens a headed browser -- log in manually, press Enter when done)
llm-tracker auth claude
llm-tracker auth chatgpt
llm-tracker auth gemini

# Check current usage
llm-tracker status

# Get provider recommendation
llm-tracker recommend
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `llm-tracker auth <provider>` | One-time browser login (saves session cookies) |
| `llm-tracker auth --all` | Authenticate all providers sequentially |
| `llm-tracker status` | Refresh data and show subscription + API spend table |
| `llm-tracker status --no-refresh` | Show cached data only (no network calls) |
| `llm-tracker recommend` | Ranked recommendation of which provider to use now |
| `llm-tracker history` | Last 7 days of usage snapshots |
| `llm-tracker history --provider claude --days 30` | Provider-specific history |
| `llm-tracker daemon --interval 15` | Background auto-refresh every N minutes |
| `llm-tracker serve` | Start API server on port 48372 |
| `llm-tracker serve --port 9999` | Start API server on a custom port |

## OpenCode Plugin

The plugin displays live LLM usage data inside OpenCode as you work. After each AI response, a compact usage summary is injected into the chat. Slash commands provide detailed reports, and the `llm_usage` tool lets the AI check usage programmatically.

### Setup

1. Start the backend:

```bash
llm-tracker serve
# or for continuous collection:
llm-tracker daemon
```

2. Install the plugin into OpenCode:

```bash
cd ~/.config/opencode
npm install /path/to/llm-usage-tracker/plugin
```

3. Register it in `~/.config/opencode/opencode.json`:

```json
{
  "plugin": ["opencode-llm-usage"]
}
```

4. Restart OpenCode.

### What You See

**After every AI response** (session.idle event), a one-liner is injected into the chat:

```
✓ Claude: 93/100 left (4h 26m) | ✓ Groq: 100000/100000 left (1h 26m) | API: $8.93
```

**Slash commands** for detailed reports:

| Command | Description |
|---------|-------------|
| `/usage` | Full usage report -- subscription limits + API spend table |
| `/spend` | API spend summary (last 30 days) |
| `/recommend` | Which provider to use right now |
| `/collect` | Trigger a fresh data collection |

**AI-callable tool**: The `llm_usage` tool is available to the AI agent. It can call it with `detail: "summary"`, `"full"`, or `"recommend"` to check usage without you asking.

### Configuration

Environment variables to customize plugin behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_TRACKER_URL` | `http://127.0.0.1:48372` | Backend API URL |
| `LLM_TRACKER_SHOW_ON_IDLE` | `true` | Inject usage after each AI response |
| `LLM_TRACKER_SHOW_ON_COMPACT` | `true` | Show toast on session compaction |
| `LLM_TRACKER_TOAST_DURATION` | `8000` | Toast display time (ms) |
| `LLM_TRACKER_MIN_INTERVAL` | `30000` | Minimum time between usage injections (ms) |
| `LLM_TRACKER_TIMEOUT` | `5000` | Backend request timeout (ms) |

## How It Works

### Authentication

`llm-tracker auth <provider>` opens a headed Playwright Chromium browser with a dedicated persistent profile (`auth/browser_profiles/<provider>/`). You log in manually and press Enter. The session cookies (including Cloudflare clearance) are saved to `auth/sessions/<provider>.json`.

Using a persistent profile means the browser fingerprint stays consistent across auth and collection, which is required to pass Cloudflare's bot detection on sites like claude.ai.

### Collection

Each provider has a tailored collection strategy:

| Provider | Method | How It Works | Reliability |
|----------|--------|--------------|-------------|
| Claude | httpx + session cookies | Calls `claude.ai/api/organizations/{orgId}/usage` with the saved `sessionKey` cookie. No browser needed for collection. | High |
| ChatGPT | Playwright headless | Scrapes UI + calls `/backend-api/accounts/check` | Medium |
| Gemini | Playwright headless | Scrapes the usage UI | Medium |
| Groq | LiteLLM or Playwright | LiteLLM for token tracking, or console scrape as fallback | High (LiteLLM) / Medium (scrape) |

### API Spend Tracking

API spend is tracked via **LiteLLM** (preferred) or per-provider API keys (fallback).

LiteLLM aggregates spend across all providers through a single proxy. The collector automatically maps model names to providers, stripping routing prefixes -- `openai/claude-opus` is correctly attributed to Claude, `groq/llama-3.1-8b-instant` to Groq.

Configure in `.env`:

```bash
LITELLM_BASE_URL=http://your-litellm-proxy:4000
LITELLM_API_KEY=sk-...
```

Without LiteLLM, per-provider API keys are used:

```bash
OPENAI_API_KEY=sk-...     # ChatGPT API spend
GOOGLE_API_KEY=...        # Gemini model availability
GROQ_API_KEY=gsk_...      # Groq validation
```

### Recommendations

The recommendation engine ranks providers by:

1. Available headroom (messages remaining / total limit)
2. Time until rate limit reset
3. Whether the provider is approaching capacity (>80% used = warn, >90% = avoid)
4. API spend relative to budget

### Storage

Usage snapshots are stored in SQLite (`data/usage.db`) via SQLAlchemy. Each snapshot records provider, source (subscription vs API), message counts, spend, tokens, and timestamps. Historical data enables trend analysis via `llm-tracker history`.

### REST API

The FastAPI server (default port 48372) exposes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check |
| `/api/status` | GET | Latest snapshot per provider + source |
| `/api/recommend` | GET | Ranked provider recommendations |
| `/api/spend/summary?days=30` | GET | API spend per provider |
| `/api/snapshots?provider=claude&days=7` | GET | Historical snapshots |
| `/api/config` | GET | Backend configuration status |
| `/api/collect` | POST | Trigger background data collection |

API docs available at `http://127.0.0.1:48372/docs` when the server is running.

## Architecture

```
backend/
├── collectors/
│   ├── base.py              # Base collector: auth, sessions, browser profiles, cookie extraction
│   ├── claude.py            # Claude: httpx collection, credentials fallback
│   ├── chatgpt.py           # ChatGPT: Playwright scraping
│   ├── gemini.py            # Gemini: Playwright scraping
│   ├── groq.py              # Groq: Playwright scraping
│   ├── litellm.py           # LiteLLM: API spend aggregation across providers
│   ├── claude_api.py        # Claude API spend (console.anthropic.com)
│   ├── chatgpt_api.py       # ChatGPT API spend (OpenAI billing)
│   ├── gemini_api.py        # Gemini API spend (Google AI Studio)
│   └── groq_api.py          # Groq API spend
├── db/
│   ├── models.py            # SQLAlchemy models (UsageSnapshot)
│   └── db.py                # Database init + session factory
├── api/
│   └── routes.py            # FastAPI endpoints
├── recommendations.py       # Provider ranking engine
├── scheduler.py             # Background refresh scheduler
├── collection.py            # Orchestrates collection across providers
└── cli.py                   # Typer CLI (auth, status, recommend, serve, daemon)

plugin/                      # OpenCode plugin (TypeScript)
├── src/
│   ├── index.ts             # Entry point: hooks, commands, llm_usage tool
│   └── lib/
│       ├── client.ts        # Backend API client (fetch with timeout)
│       ├── config.ts        # Plugin config from env vars
│       └── format.ts        # Inline, toast, report, and recommendation formatters
└── package.json

auth/
├── sessions/                # Saved session cookies (gitignored)
└── browser_profiles/        # Persistent Playwright profiles (gitignored)

data/
└── usage.db                 # SQLite database (gitignored)
```

## Notes

- Sessions expire -- re-run `llm-tracker auth <provider>` when collection starts failing.
- Collection interval should be 5+ minutes to avoid rate limits.
- The `data/`, `auth/sessions/`, and `auth/browser_profiles/` directories are gitignored.
- The plugin auto-starts the backend if it detects the server is down.
- Claude collection requires no browser for ongoing use -- only the initial auth needs a headed browser.
