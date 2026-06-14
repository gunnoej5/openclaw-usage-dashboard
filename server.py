#!/usr/bin/env python3
"""
OpenClaw Usage Dashboard — backend server
Reads trajectory JSONL files from ~/.openclaw/agents/*/sessions/
Serves:
  GET /         → HTML dashboard
  GET /api/runs → recent run records (JSON)
  GET /api/stats → aggregate stats (JSON)
  GET /api/pricing → known model pricing (JSON)
  GET /events   → SSE stream of new run events
"""

import json
import os
import glob
import time
import threading
import queue
import pathlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

OPENCLAW_STATE = pathlib.Path(os.environ.get("OPENCLAW_STATE_DIR", os.path.expanduser("~/.openclaw")))
SESSIONS_GLOB = str(OPENCLAW_STATE / "agents" / "*" / "sessions" / "*.trajectory.jsonl")
CATALOG_GLOB  = str(OPENCLAW_STATE / "agents" / "*" / "agent" / "plugins" / "*" / "catalog.json")

PORT = int(os.environ.get("USAGE_DASHBOARD_PORT", "9393"))

# ── Pricing table ──────────────────────────────────────────────────────────────

# Fallback pricing ($/1M tokens) for models not present in local catalog.
# Catalog entries take precedence when found.
FALLBACK_PRICING: dict = {
    "anthropic/claude-opus-4-7": {
        "name": "Claude Opus 4.7", "provider": "anthropic", "modelId": "claude-opus-4-7",
        "input": 5.0, "output": 25.0, "cacheRead": 0.50, "cacheWrite": 6.25,
    },
    "anthropic/claude-opus-4-6": {
        "name": "Claude Opus 4.6", "provider": "anthropic", "modelId": "claude-opus-4-6",
        "input": 5.0, "output": 25.0, "cacheRead": 0.50, "cacheWrite": 6.25,
    },
    "anthropic/claude-opus-4-5": {
        "name": "Claude Opus 4.5", "provider": "anthropic", "modelId": "claude-opus-4-5",
        "input": 5.0, "output": 25.0, "cacheRead": 0.50, "cacheWrite": 6.25,
    },
    "anthropic/claude-opus-4": {
        "name": "Claude Opus 4", "provider": "anthropic", "modelId": "claude-opus-4",
        "input": 5.0, "output": 25.0, "cacheRead": 0.50, "cacheWrite": 6.25,
    },
    "anthropic/claude-sonnet-4-6": {
        "name": "Claude Sonnet 4.6", "provider": "anthropic", "modelId": "claude-sonnet-4-6",
        "input": 3.0, "output": 15.0, "cacheRead": 0.30, "cacheWrite": 3.75,
    },
    "anthropic/claude-sonnet-4-5": {
        "name": "Claude Sonnet 4.5", "provider": "anthropic", "modelId": "claude-sonnet-4-5",
        "input": 3.0, "output": 15.0, "cacheRead": 0.30, "cacheWrite": 3.75,
    },
    "anthropic/claude-haiku-3-5": {
        "name": "Claude Haiku 3.5", "provider": "anthropic", "modelId": "claude-haiku-3-5",
        "input": 0.8, "output": 4.0, "cacheRead": 0.08, "cacheWrite": 1.0,
    },
    "openai/gpt-5.4": {
        "name": "GPT-5.4", "provider": "openai", "modelId": "gpt-5.4",
        "input": 10.0, "output": 40.0, "cacheRead": 2.50, "cacheWrite": 0.0,
    },
    "openai/gpt-5.4-mini": {
        "name": "GPT-5.4 Mini", "provider": "openai", "modelId": "gpt-5.4-mini",
        "input": 0.40, "output": 1.60, "cacheRead": 0.10, "cacheWrite": 0.0,
    },
    "openai/o4-mini": {
        "name": "o4-mini", "provider": "openai", "modelId": "o4-mini",
        "input": 1.10, "output": 4.40, "cacheRead": 0.275, "cacheWrite": 0.0,
    },
}


def load_pricing() -> dict:
    """Returns {provider/modelId: {input, output, cacheRead, cacheWrite}} in $/1M tokens.
    Starts from FALLBACK_PRICING; catalog entries override fallbacks when present.
    """
    pricing: dict = dict(FALLBACK_PRICING)  # start with fallbacks
    for cat_path in glob.glob(CATALOG_GLOB):
        try:
            with open(cat_path) as f:
                data = json.load(f)
            for pname, pdata in data.get("providers", {}).items():
                for m in pdata.get("models", []):
                    cost = m.get("cost")
                    if cost:
                        key = f"{pname}/{m['id']}"
                        pricing[key] = {
                            "name":       m.get("name", m["id"]),
                            "provider":   pname,
                            "modelId":    m["id"],
                            "input":      cost.get("input", 0),
                            "output":     cost.get("output", 0),
                            "cacheRead":  cost.get("cacheRead", 0),
                            "cacheWrite": cost.get("cacheWrite", 0),
                        }
        except Exception:
            pass
    return pricing


def estimate_cost(usage: dict, pricing_entry: dict | None) -> float:
    if not pricing_entry:
        return 0.0
    M = 1_000_000
    inp        = usage.get("input", 0)
    out        = usage.get("output", 0)
    cache_read = usage.get("cacheRead", 0)
    cache_write= usage.get("cacheWrite", 0)
    return (
        inp        * pricing_entry["input"]      / M +
        out        * pricing_entry["output"]     / M +
        cache_read * pricing_entry["cacheRead"]  / M +
        cache_write* pricing_entry["cacheWrite"] / M
    )


# ── Run record builder ─────────────────────────────────────────────────────────

class RunRecord:
    def __init__(self):
        self.run_id       = None
        self.session_id   = None
        self.session_key  = None
        self.provider     = None
        self.model_id     = None
        self.model_api    = None
        self.channel      = None
        self.agent_id     = None
        self.trigger      = None
        self.started_ts   = None
        self.ended_ts     = None
        self.status       = "running"
        self.usage        = {}
        self.cost_usd     = 0.0
        self.aborted      = False
        self.timed_out    = False

    def to_dict(self) -> dict:
        duration_ms = None
        if self.started_ts and self.ended_ts:
            try:
                from datetime import datetime, timezone
                s = datetime.fromisoformat(self.started_ts.replace("Z", "+00:00"))
                e = datetime.fromisoformat(self.ended_ts.replace("Z", "+00:00"))
                duration_ms = int((e - s).total_seconds() * 1000)
            except Exception:
                pass
        return {
            "runId":       self.run_id,
            "sessionId":   self.session_id,
            "sessionKey":  self.session_key,
            "provider":    self.provider,
            "modelId":     self.model_id,
            "modelApi":    self.model_api,
            "channel":     self.channel,
            "agentId":     self.agent_id,
            "trigger":     self.trigger,
            "startedTs":   self.started_ts,
            "endedTs":     self.ended_ts,
            "status":      self.status,
            "usage":       self.usage,
            "costUsd":     round(self.cost_usd, 8),
            "durationMs":  duration_ms,
            "aborted":     self.aborted,
            "timedOut":    self.timed_out,
        }


def parse_trajectory_file(path: str, pricing: dict) -> list[dict]:
    """Parse a trajectory file into a list of completed run records."""
    runs: dict[str, RunRecord] = {}
    completed: list[dict] = []

    try:
        with open(path, errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                t      = ev.get("type", "")
                run_id = ev.get("runId")
                if not run_id:
                    continue

                if run_id not in runs:
                    runs[run_id] = RunRecord()

                r = runs[run_id]
                r.run_id     = run_id
                r.session_id = ev.get("sessionId", r.session_id)
                r.session_key= ev.get("sessionKey", r.session_key)
                r.provider   = ev.get("provider", r.provider)
                r.model_id   = ev.get("modelId", r.model_id)
                r.model_api  = ev.get("modelApi", r.model_api)

                if t == "session.started":
                    r.started_ts = ev.get("ts", r.started_ts)
                    d = ev.get("data", {})
                    r.trigger    = d.get("trigger", r.trigger)
                    r.agent_id   = d.get("agentId", r.agent_id)
                    r.channel    = d.get("messageProvider", r.channel)
                    r.status     = "running"

                elif t == "model.completed":
                    d = ev.get("data", {})
                    usage = d.get("usage", {})
                    if usage:
                        # accumulate across multi-turn within a run
                        for k, v in usage.items():
                            r.usage[k] = r.usage.get(k, 0) + v
                    r.aborted    = d.get("aborted", r.aborted)
                    r.timed_out  = d.get("timedOut", r.timed_out)

                elif t == "session.ended":
                    r.ended_ts = ev.get("ts", r.ended_ts)
                    d = ev.get("data", {})
                    r.aborted    = d.get("aborted", r.aborted)
                    r.timed_out  = d.get("timedOut", r.timed_out)
                    outcome      = d.get("status", "success")
                    if r.aborted or r.timed_out:
                        r.status = "aborted"
                    else:
                        r.status = outcome or "success"

                    # compute cost
                    pk = f"{r.provider}/{r.model_id}" if r.provider and r.model_id else None
                    pe = pricing.get(pk)
                    r.cost_usd = estimate_cost(r.usage, pe)
                    completed.append(r.to_dict())
                    del runs[run_id]

    except Exception:
        pass

    # Flush still-running
    for r in runs.values():
        pk = f"{r.provider}/{r.model_id}" if r.provider and r.model_id else None
        pe = pricing.get(pk)
        r.cost_usd = estimate_cost(r.usage, pe)
        completed.append(r.to_dict())

    return completed


# ── State store ────────────────────────────────────────────────────────────────

def _hottest_trajectory_file() -> str | None:
    """Return the path of the most-recently-modified trajectory file, or None."""
    files = glob.glob(SESSIONS_GLOB)
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


class Store:
    MAX_RUNS = 2000

    def __init__(self):
        self.runs:    list[dict] = []
        self.pricing: dict = {}
        self.lock     = threading.Lock()
        self.sse_clients: list[queue.Queue] = []
        self._known_files: dict[str, int] = {}  # path → mtime
        self._hot_file:    str | None = None    # always-reread active file
        self.active_session_file: str | None = None  # basename exposed to UI

    def reload_pricing(self):
        self.pricing = load_pricing()

    def initial_load(self):
        self.reload_pricing()
        all_runs: list[dict] = []
        for path in glob.glob(SESSIONS_GLOB):
            runs = parse_trajectory_file(path, self.pricing)
            all_runs.extend(runs)
        all_runs.sort(key=lambda r: r.get("startedTs") or "", reverse=True)
        hot = _hottest_trajectory_file()
        with self.lock:
            self.runs = all_runs[:self.MAX_RUNS]
            self._known_files = {p: int(os.path.getmtime(p)) for p in glob.glob(SESSIONS_GLOB)}
            self._hot_file = hot
            self.active_session_file = os.path.basename(hot) if hot else None

    def poll_new(self):
        """Called by background thread. Returns list of newly completed runs.

        Strategy:
        - Track mtime for all files; re-parse any that changed.
        - Additionally, ALWAYS re-parse the hottest (most-recently-modified)
          file on every poll cycle so in-flight sessions are reflected without
          waiting for an mtime tick between two consecutive turns.
        """
        self.reload_pricing()
        new_runs: list[dict] = []
        current_files = set(glob.glob(SESSIONS_GLOB))
        hot = _hottest_trajectory_file()

        # Files changed since last poll (mtime-based)
        changed: list[str] = []
        for path in current_files:
            try:
                mtime = int(os.path.getmtime(path))
            except Exception:
                continue
            if self._known_files.get(path, 0) != mtime:
                changed.append(path)
                self._known_files[path] = mtime

        # Always include the hottest file so active sessions stream in real-time
        if hot and hot not in changed:
            changed.append(hot)

        for path in changed:
            runs = parse_trajectory_file(path, self.pricing)
            new_runs.extend(runs)

        # Update hot-file tracking
        with self.lock:
            self._hot_file = hot
            self.active_session_file = os.path.basename(hot) if hot else None

        if new_runs:
            new_runs.sort(key=lambda r: r.get("startedTs") or "", reverse=True)
            with self.lock:
                # merge: remove existing records with same runId, prepend new
                existing_ids = {r["runId"] for r in new_runs}
                self.runs = [r for r in self.runs if r["runId"] not in existing_ids]
                self.runs = (new_runs + self.runs)[:self.MAX_RUNS]
            self._broadcast(new_runs)

        return new_runs

    def _broadcast(self, runs: list[dict]):
        payload = json.dumps({"type": "runs", "data": runs})
        dead = []
        for q in list(self.sse_clients):
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                self.sse_clients.remove(q)
            except ValueError:
                pass

    def get_runs(self, limit: int = 200) -> list[dict]:
        with self.lock:
            return self.runs[:limit]

    def get_status(self) -> dict:
        """Return server health / active-session metadata for the UI banner."""
        with self.lock:
            hot = self.active_session_file
        return {
            "activeSessionFile": hot,
            "pollIntervalMs": 5000,
            "note": (
                "Active session detected. Costs for the current open session are "
                "re-read every 5 s and may lag by up to one turn."
            ) if hot else None,
        }

    def get_stats(self) -> dict:
        with self.lock:
            runs = self.runs

        total_cost = sum(r.get("costUsd", 0) for r in runs)
        total_input  = sum(r.get("usage", {}).get("input", 0) for r in runs)
        total_output = sum(r.get("usage", {}).get("output", 0) for r in runs)
        total_cache_read  = sum(r.get("usage", {}).get("cacheRead", 0) for r in runs)
        total_cache_write = sum(r.get("usage", {}).get("cacheWrite", 0) for r in runs)

        by_model: dict[str, dict] = {}
        by_provider: dict[str, dict] = {}
        by_channel: dict[str, dict] = {}

        def acc(bucket: dict, key: str, r: dict):
            if key not in bucket:
                bucket[key] = {"runs": 0, "costUsd": 0.0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
            b = bucket[key]
            b["runs"] += 1
            b["costUsd"] += r.get("costUsd", 0)
            u = r.get("usage", {})
            b["input"]      += u.get("input", 0)
            b["output"]     += u.get("output", 0)
            b["cacheRead"]  += u.get("cacheRead", 0)
            b["cacheWrite"] += u.get("cacheWrite", 0)

        for r in runs:
            if r.get("modelId"):
                acc(by_model, r["modelId"], r)
            if r.get("provider"):
                acc(by_provider, r["provider"], r)
            if r.get("channel"):
                acc(by_channel, r["channel"], r)

        # sort by cost desc
        def sort_bucket(b):
            return dict(sorted(b.items(), key=lambda x: x[1]["costUsd"], reverse=True))

        return {
            "totalRuns":       len(runs),
            "totalCostUsd":    round(total_cost, 6),
            "totalTokens": {
                "input":      total_input,
                "output":     total_output,
                "cacheRead":  total_cache_read,
                "cacheWrite": total_cache_write,
                "total":      total_input + total_output + total_cache_read + total_cache_write,
            },
            "byModel":    sort_bucket(by_model),
            "byProvider": sort_bucket(by_provider),
            "byChannel":  sort_bucket(by_channel),
        }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        self.sse_clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        try:
            self.sse_clients.remove(q)
        except ValueError:
            pass


STORE = Store()


def background_poller():
    while True:
        time.sleep(5)
        try:
            STORE.poll_new()
        except Exception:
            pass


# ── HTTP handler ───────────────────────────────────────────────────────────────

def read_html() -> bytes:
    here = pathlib.Path(__file__).parent
    html_path = here / "index.html"
    with open(html_path, "rb") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence noisy request logs

    def send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        params = {}
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v

        if path == "/" or path == "/index.html":
            try:
                body = read_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_json({"error": "index.html not found"}, 404)

        elif path == "/api/runs":
            limit = int(params.get("limit", "200"))
            self.send_json(STORE.get_runs(limit))

        elif path == "/api/stats":
            self.send_json(STORE.get_stats())

        elif path == "/api/status":
            self.send_json(STORE.get_status())

        elif path == "/api/pricing":
            self.send_json(STORE.pricing)

        elif path == "/events":
            q = STORE.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                # send a heartbeat immediately
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()

                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # heartbeat
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                STORE.unsubscribe(q)

        else:
            self.send_json({"error": "not found"}, 404)


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Loading trajectory data from {OPENCLAW_STATE}/agents/*/sessions/ …")
    STORE.initial_load()
    runs = STORE.get_runs(5)
    print(f"Loaded {len(STORE.runs)} runs ({len(STORE.pricing)} priced models).")

    t = threading.Thread(target=background_poller, daemon=True)
    t.start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard → http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")
