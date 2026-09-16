# OpenClaw Usage Dashboard

A lightweight, real-time cost-tracking dashboard for [OpenClaw](https://openclaw.ai). Reads local trajectory events from OpenClaw's per-agent SQLite stores, computes estimated costs per session, and lets you cross-reference against your billing console to monitor variance.

No external dependencies. Pure Python stdlib backend, single-file HTML/JS frontend.

![Dashboard screenshot](https://github.com/gunnoej5/openclaw-usage-dashboard/raw/main/screenshot.png)

### Model routing reference

![Model routes](https://github.com/gunnoej5/openclaw-usage-dashboard/raw/main/model-routes.png)

---

## Features

- **Real-time updates** via Server-Sent Events — new runs stream in without a page refresh
- **Active session tracking** — the active per-agent SQLite store is re-read every 5 s so in-flight session costs appear within one turn, with a pulsing banner when a session is open
- **Time-window filter** — All time / 30 days / 14 days / 7 days / 24 hours
- **Per-run table** — timestamp, provider, model, channel, status, input/output/cache tokens, estimated cost, duration
- **Sidebar stats** — totals card, sessions-by-provider donut chart, cost-by-provider/model/channel bar charts, token breakdown
- **Combinable filters** — time window + provider + status + channel + freetext search all stack
- **Console source-of-truth tracker** — enter your billing console total for any window; dashboard shows the gap in real time, color-coded green/yellow/red (≤5% / 5–15% / >15%)
- **Vendor-sourced pricing table** — Anthropic and OpenAI model rates are embedded with source URLs and retrieval dates; plugin catalog entries override when present
- **Accuracy disclaimer** — collapsible sidebar note explaining known gap sources so the numbers are always honest
- **Zero deps** — Python 3.8+ stdlib only; no npm, no pip

---

## How it works

OpenClaw stores runtime trajectory events in each agent's SQLite database:

```
~/.openclaw/agents/*/agent/openclaw-agent.sqlite
```

The server reads the `trajectory_runtime_events` table, parses `session.started`, `model.completed`, and `session.ended` events, computes costs from model pricing, and pushes updates to browsers via SSE. The per-agent stores are re-read on every poll cycle so active sessions stay current.

Pricing starts from `pricing.py`, a vendor-sourced table transcribed from:

- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- OpenAI: https://developers.openai.com/api/docs/pricing

If plugin catalog files are present under `~/.openclaw/agents/*/agent/plugins/*/catalog.json`, non-zero catalog costs override the embedded table. Models without a published rate are intentionally left unpriced.

---

## Cost accuracy

Dashboard figures are **estimates** derived from local trajectory events. They can differ from your billing console. Known gap sources:

| Source | Recoverable? |
|---|---|
| **Crashed/killed sessions** — API call billed but `model.completed` never reached SQLite | ✗ No |
| **Active session lag** — current open turn not yet flushed | ✓ Auto-updates every 5 s |
| **Multiple API keys** — console aggregates all keys; dashboard sees one local process | ✗ No |
| **Unpublished model pricing** — absent vendor rates are left unpriced instead of guessed | ~ Mostly |

Use the **Console Source of Truth** panel in the sidebar to enter your billing console total and track the variance live. Saved per time-window in `~/.openclaw/usage-dashboard-console.json`.

Use your provider billing console as the invoice source of truth; this dashboard is a fast local estimator.

---

## Requirements

- [OpenClaw](https://openclaw.ai) installed with at least one completed session
- Python 3.8+
- A modern browser

---

## Installation

```bash
git clone https://github.com/gunnoej5/openclaw-usage-dashboard.git
cd openclaw-usage-dashboard
```

No pip install, no npm install.

---

## Running

```bash
./scripts/start-usage-dashboard.sh
```

Open **http://127.0.0.1:9393/** in your browser.

> **Note:** Initial load parses all retained trajectory rows. On a busy instance this can take a few seconds before the first response. The server is ready when it starts responding to requests.

### Custom port

```bash
USAGE_DASHBOARD_PORT=9394 ./scripts/start-usage-dashboard.sh
```

### Custom OpenClaw state directory

```bash
OPENCLAW_STATE_DIR=/path/to/your/.openclaw ./scripts/start-usage-dashboard.sh
```

---

## Auto-start with systemd (Linux)

```bash
./scripts/install-systemd-user-service.sh
```

This installs `~/.config/systemd/user/openclaw-usage-dashboard.service`,
enables it, starts it immediately, and attempts to enable user lingering so it
comes back after reboot without requiring an interactive login.

Operational details live in [RUNBOOK.md](RUNBOOK.md).

---

## Console source-of-truth tracker

The sidebar includes a **Console Source of Truth** panel for cross-referencing against Anthropic/OpenAI billing:

1. Select a time window (e.g. **7 days**)
2. Open your Anthropic console, filter to the same window
3. Enter the console total in the input field and click **Save**
4. The variance display shows the gap — color-coded:
   - 🟢 **≤5%** — normal noise (crashed sessions, minor rounding)
   - 🟡 **5–15%** — expected if you have a second API key or occasional hard crashes
   - 🔴 **>15%** — worth investigating (wrong window, missing key, systematic data loss)

Values are persisted per-window in localStorage and synced to the server so they survive browser and server restarts.

---

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `GET /` | GET | Dashboard HTML |
| `GET /api/runs?limit=N` | GET | Recent run records (default 200) |
| `GET /api/stats` | GET | Aggregate totals by model / provider / channel |
| `GET /api/pricing` | GET | Effective model pricing table (vendor table + catalog overrides) |
| `GET /api/status` | GET | Server health + active session file |
| `GET /api/console` | GET | Saved console totals by window |
| `POST /api/console` | POST | Save a console total `{"window":"7d","amount":94.82}` |
| `GET /events` | GET | SSE stream — pushes `{"type":"runs","data":[...]}` on changes |

---

## Cost calculation

```
cost = (input_tokens       × input_rate
      + output_tokens      × output_rate
      + cache_read_tokens  × cache_read_rate
      + cache_write_tokens × cache_write_rate
      + cache_write_1h_tokens × cache_write_1h_rate) / 1_000_000
```

Rates (USD per 1M tokens) are loaded from `pricing.py`, with optional OpenClaw plugin catalog overrides when a non-zero catalog cost exists:

| Model | Input | Output | Cache read | Cache write | Cache write 1h |
|---|---|---|---|---|---|
| claude-opus-5 | $5.00 | $25.00 | $0.50 | $6.25 | $10.00 |
| claude-sonnet-5 | $2.00 | $10.00 | $0.20 | $2.50 | $4.00 |
| claude-fable-5-1 | $10.00 | $50.00 | $0.25 | $12.50 | $20.00 |
| gpt-5.5 | $5.00 | $30.00 | $0.50 | — | — |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 | — | — |
| o4-mini | $1.10 | $4.40 | $0.275 | — | — |

Local/self-hosted models (LM Studio, etc.) show `$0.00`.

---

## Project structure

```
openclaw-usage-dashboard/
├── server.py                              # Python stdlib HTTP + SSE + parser
├── index.html                             # Single-file frontend
├── RUNBOOK.md                             # Install, operations, troubleshooting
├── scripts/
│   ├── install-systemd-user-service.sh    # Installs/updates the user service
│   └── start-usage-dashboard.sh           # Canonical startup wrapper
├── LICENSE                                # MIT
└── README.md
```

---

## License

[MIT](LICENSE)
