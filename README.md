# LLM Usage Tracker

Automatically collects usage limit data from Claude, ChatGPT, Gemini, and Groq subscriptions and recommends which AI to use and when. Works standalone (CLI) and as an OpenCode plugin.

## Quick Start

```bash
# Install (with uv)
uv pip install -e .

# Or with pip
pip install -e .

# Install Playwright browsers
playwright install chromium

# Copy and edit environment config
cp .env.example .env

# Authenticate with each provider (opens headed browser — log in manually)
llm-tracker auth claude
llm-tracker auth chatgpt
llm-tracker auth gemini

# Check status
llm-tracker status

# Get recommendation
llm-tracker recommend
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `llm-tracker auth <provider>` | One-time browser login (saves session) |
| `llm-tracker auth --all` | Authenticate all providers |
| `llm-tracker status` | Refresh data and show usage table |
| `llm-tracker status --no-refresh` | Show cached data only |
| `llm-tracker recommend` | Show what to use right now |
| `llm-tracker history` | Last 7 days of usage |
| `llm-tracker history --provider claude --days 30` | Provider-specific history |
| `llm-tracker daemon --interval 15` | Background auto-refresh (every 15 min) |
| `llm-tracker serve --port 8080` | Start API server (for plugin + web UI) |

## OpenCode Plugin

The plugin shows your LLM usage data inside OpenCode as you work — toast notifications on session completion, plus slash commands for detailed reports.

### Setup

1. Start the backend (keeps data fresh in the background):

```bash
llm-tracker serve
# or for continuous collection:
llm-tracker daemon
```

2. Install the plugin — copy the file into your OpenCode plugins directory:

```bash
# Project-level (this project only)
mkdir -p .opencode/plugins
cp plugin/src/index.ts .opencode/plugins/llm-usage.ts

# Or global (all projects)
cp plugin/src/index.ts ~/.config/opencode/plugins/llm-usage.ts
```

Since the plugin imports from local modules, the simplest approach is to symlink or publish:

```bash
# Option A: Symlink the plugin directory
ln -s "$(pwd)/plugin" ~/.config/opencode/plugins/llm-usage

# Option B: Add as npm package (after publishing)
# In opencode.json: { "plugin": ["opencode-llm-usage"] }
```

3. Add dependencies for local plugin loading. Create a `package.json` in your opencode config dir:

```bash
# For project-level
cat > .opencode/package.json << 'EOF'
{ "dependencies": { "@opencode-ai/plugin": "latest" } }
EOF

# For global
cat > ~/.config/opencode/package.json << 'EOF'
{ "dependencies": { "@opencode-ai/plugin": "latest" } }
EOF
```

### Plugin Commands

| Command | Description |
|---------|-------------|
| `/usage` | Full usage report — subscription limits + API spend |
| `/spend` | API spend summary (last 30 days) |
| `/recommend` | Which provider to use right now |
| `/collect` | Trigger a fresh data collection |

The plugin also shows a toast notification whenever a session completes, displaying your current subscription headroom and API spend.

### Configuration

Set environment variables to customize behavior:

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_TRACKER_URL` | `http://127.0.0.1:8080` | Backend API URL |
| `LLM_TRACKER_SHOW_ON_IDLE` | `true` | Show toast when session completes |
| `LLM_TRACKER_SHOW_ON_COMPACT` | `true` | Show toast on session compaction |
| `LLM_TRACKER_TOAST_DURATION` | `8000` | Toast display time (ms) |
| `LLM_TRACKER_MIN_INTERVAL` | `30000` | Minimum time between toasts (ms) |
| `LLM_TRACKER_TIMEOUT` | `5000` | Backend request timeout (ms) |

## How It Works

1. **Auth**: Playwright opens a real browser. You log in once. Session cookies are saved to `auth/sessions/<provider>.json`.
2. **Collection**: Playwright loads saved sessions (headless) and either calls internal APIs or scrapes the UI.
3. **Storage**: Usage snapshots stored in SQLite (`data/usage.db`).
4. **Recommendations**: Rule-based engine ranks providers by available headroom and upcoming resets.
5. **Plugin**: TypeScript OpenCode plugin queries the backend API and displays data via toasts + slash commands.

## Providers

| Provider | Method | Reliability |
|----------|--------|-------------|
| Claude | `/api/oauth/usage` endpoint | High |
| ChatGPT | UI scrape + `/backend-api/accounts/check` | Medium |
| Gemini | UI scrape | Medium |
| Groq | LiteLLM daily tokens or console scrape | High (LiteLLM) / Medium (scrape) |

## Architecture

```
backend/                     # Python backend (standalone CLI + API server)
├── collectors/              # Per-provider data collection (Playwright)
├── db/                      # SQLAlchemy models + SQLite
├── api/                     # FastAPI routes
├── recommendations.py
├── scheduler.py
└── cli.py

plugin/                      # OpenCode plugin (TypeScript)
├── src/
│   ├── index.ts             # Plugin entry point (hooks + commands)
│   └── lib/
│       ├── client.ts        # Backend API client
│       ├── config.ts        # Plugin configuration
│       └── format.ts        # Toast + report formatters
└── package.json
```

## Notes

- Sessions expire eventually — re-run `auth` to refresh.
- Don't poll more than once every 5–10 minutes to avoid rate limits.
- The `data/` and `auth/sessions/` directories are gitignored.
- The plugin requires the backend to be running (`llm-tracker serve` or `llm-tracker daemon`).
