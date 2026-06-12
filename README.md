# OpenClaw Usage Dashboard

The 'Usage' nav in the default OpenClaw instance is ok.  I needed something to watch costs closer based on a specific model route design that I've been using.

So here is a lightweight, real-time web dashboard for [OpenClaw](https://openclaw.ai) that shows which AI model each session is using, along with token counts and estimated costs — broken down by provider, model, and channel.

No external dependencies. Pure Python stdlib backend, single-file HTML/JS frontend.

![Dashboard screenshot](https://github.com/gunnoej5/openclaw-usage-dashboard/raw/main/screenshot.png)

### Model routing reference

![Model routes](https://github.com/gunnoej5/openclaw-usage-dashboard/raw/main/model-routes.png)

---

## Features

- **Real-time updates** via Server-Sent Events — new runs appear instantly without a page refresh
- **Time-window filter** — All time / 30 days / 14 days / 7 days / 24 hours, in the header bar
- **Per-run table** — timestamp, provider, model, channel, status, input tokens, output tokens, cache tokens, estimated cost, duration
- **Sidebar stats** — totals card, sessions-by-provider donut chart, cost-by-provider/model/channel bar charts, token breakdown
- **Combinable filters** — time window + provider + status + channel + freetext search all stack
- **Cost parity** — uses the same per-model pricing rates from OpenClaw's plugin catalogs that `/status` and `/usage cost` use
- **Zero deps** — Python 3.8+ standard library only; no npm, no pip installs

---

## How it works

OpenClaw writes a [trajectory JSONL](https://openclaw.ai) file for every session under:

```
~/.openclaw/agents/*/sessions/*.trajectory.jsonl
```

The dashboard server tails these files every 5 seconds, parses `session.started`, `model.completed`, and `session.ended` events, and computes costs from the model pricing catalogs at:

```
~/.openclaw/agents/*/agent/plugins/*/catalog.json
```

A background thread polls for file changes and pushes new run records to connected browsers via SSE.

---

## Requirements

- [OpenClaw](https://openclaw.ai) installed and have run at least one session
- Python 3.8+
- A modern browser

---

## Installation

```bash
git clone https://github.com/gunnoej5/openclaw-usage-dashboard.git
cd openclaw-usage-dashboard
```

That's it. No pip install, no npm install.

---

## Running

```bash
python3 server.py
```

Then open **http://127.0.0.1:9393/** in your browser.

### Custom port

```bash
USAGE_DASHBOARD_PORT=9394 python3 server.py
```

### Custom OpenClaw state directory

By default the server reads from `~/.openclaw`. Override with:

```bash
OPENCLAW_STATE_DIR=/path/to/your/.openclaw python3 server.py
```

---

## Auto-start with systemd (Linux)

Save the service file:

```ini
# ~/.config/systemd/user/openclaw-usage-dashboard.service
[Unit]
Description=OpenClaw Usage Dashboard
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/openclaw-usage-dashboard/server.py
Restart=on-failure
RestartSec=5
Environment=USAGE_DASHBOARD_PORT=9393

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user enable --now openclaw-usage-dashboard
```

---

## API endpoints

The server also exposes a small JSON API if you want to build on top of it:

| Endpoint | Description |
|---|---|
| `GET /` | The dashboard HTML |
| `GET /api/runs?limit=N` | Recent run records (default 200, max 500 loaded at startup) |
| `GET /api/stats` | Aggregate stats: totals, by-model, by-provider, by-channel |
| `GET /api/pricing` | Known model pricing entries from catalogs |
| `GET /events` | SSE stream — pushes `{"type":"runs","data":[...]}` on changes |

---

## Cost calculation

```
cost = (input_tokens      × input_rate
      + output_tokens     × output_rate
      + cache_read_tokens × cache_read_rate
      + cache_write_tokens× cache_write_rate) / 1_000_000
```

Rates (in USD per 1M tokens) come directly from OpenClaw's plugin catalog files, so they stay in sync with whatever OpenClaw has configured. Local/self-hosted models (e.g. LM Studio) show `$0.00`.

---

## Parity with OpenClaw native reporting

| OpenClaw surface | Dashboard equivalent |
|---|---|
| `/status` per-reply cost estimate | Per-run `costUsd` column |
| `/usage cost` aggregate | Sidebar "Totals" card |
| `/usage full` per-reply token footer | Per-row input/output/cache/cost breakdown |
| `openclaw status --usage` provider breakdown | "By Provider" bar group |
| Prometheus `openclaw_model_cost_usd_total` | `byModel[*].costUsd` in `/api/stats` |
| Prometheus `openclaw_model_tokens_total` | `byModel[*].{input,output,cacheRead,cacheWrite}` |

---

## Project structure

```
openclaw-usage-dashboard/
├── server.py    # Python stdlib HTTP server + SSE + trajectory parser
├── index.html   # Single-file frontend (vanilla JS, SVG pie chart, no frameworks)
├── LICENSE      # MIT
└── README.md
```

---

## License

[MIT](LICENSE)
