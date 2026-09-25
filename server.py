#!/usr/bin/env python3
"""
OpenClaw Usage Dashboard — backend server
Reads trajectory events from the per-agent SQLite store in
~/.openclaw/agents/*/agent/openclaw-agent.sqlite
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
import re
import time
import shutil
import sqlite3
import tempfile
import threading
import queue
import uuid
import pathlib
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any

from pricing import MODEL_PRICING, pricing_freshness, warn_if_stale, PRICING_MAX_AGE_DAYS

OPENCLAW_STATE  = pathlib.Path(os.environ.get("OPENCLAW_STATE_DIR", os.path.expanduser("~/.openclaw")))
# OpenClaw 2026.8.x moved runtime trajectory capture out of per-session JSONL
# sidecars and into the per-agent SQLite database.
AGENT_DB_GLOB   = str(OPENCLAW_STATE / "agents" / "*" / "agent" / "openclaw-agent.sqlite")
CATALOG_GLOB    = str(OPENCLAW_STATE / "agents" / "*" / "agent" / "plugins" / "*" / "catalog.json")
LMSTUDIO_LOGS   = str(pathlib.Path(os.environ.get("LMSTUDIO_LOGS", os.path.expanduser("~/.lmstudio/server-logs"))))

PORT = int(os.environ.get("USAGE_DASHBOARD_PORT", "9393"))

# ── Pricing table ──────────────────────────────────────────────────────────────

# Real pricing lives in pricing.py, transcribed from vendor pricing pages with
# source URLs and retrieval dates. Never hand-edit prices here.
def load_pricing() -> dict:
    """Returns {provider/modelId: {input, output, cacheRead, cacheWrite, cacheWrite1h}}
    in $/1M tokens.

    Starts from the vendor-sourced table in pricing.py. A plugin catalog, if one
    is present, may override an entry — but only when it carries a non-zero cost
    block. As of the 2026-09-02 SQLite migration the plugin catalog directories
    on this host are empty, so pricing.py is the effective source of truth.
    """
    pricing: dict = {k: dict(v) for k, v in MODEL_PRICING.items()}
    for cat_path in glob.glob(CATALOG_GLOB):
        try:
            with open(cat_path) as f:
                data = json.load(f)
            for pname, pdata in data.get("providers", {}).items():
                for m in pdata.get("models", []):
                    cost = m.get("cost")
                    # Skip empty or all-zero catalog cost blocks so the
                    # vendor-sourced table can supply real pricing.
                    if cost and any(
                        cost.get(k) for k in ("input", "output", "cacheRead", "cacheWrite", "cacheWrite1h")
                    ):
                        key = f"{pname}/{m['id']}"
                        pricing[key] = {
                            "name":         m.get("name", m["id"]),
                            "provider":     pname,
                            "modelId":      m["id"],
                            "input":        cost.get("input", 0),
                            "output":       cost.get("output", 0),
                            "cacheRead":    cost.get("cacheRead", 0),
                            "cacheWrite":   cost.get("cacheWrite", 0),
                            "cacheWrite1h": cost.get("cacheWrite1h", 0),
                        }
        except Exception:
            pass
    return pricing


# Usage keys that represent billable token volume. Anything outside this set is
# ignored by the accumulator and the cost math.
#
# Deliberately excluded:
#   total           — a vendor-computed sum of the other fields; adding it
#                     would double-count every token.
#   reasoningTokens — already included inside `output` on OpenAI reasoning
#                     models (verified: reasoningTokens <= output on every
#                     observed event). Billing it again would overcharge.
#   cost, contextUsage, promptCache — nested objects, not token counts.
BILLABLE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "cacheWrite1h")


def sanitize_usage(usage: dict | None) -> dict:
    """Extract only numeric billable token counts from a usage block.

    The runtime `usage` object nests dicts (`cost`, `contextUsage`). The previous
    implementation summed values blindly and raised TypeError on the first
    nested dict, which aborted parsing of an entire file inside a broad
    `except Exception: pass`. Whitelisting scalars makes that failure
    structurally impossible.
    """
    if not isinstance(usage, dict):
        return {}
    clean: dict = {}
    for key in BILLABLE_KEYS:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            clean[key] = value
    return clean


def estimate_cost(usage: dict, pricing_entry: dict | None) -> float:
    if not pricing_entry:
        return 0.0
    M = 1_000_000
    total = 0.0
    for key in BILLABLE_KEYS:
        tokens = usage.get(key, 0)
        if not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
            continue
        total += tokens * pricing_entry.get(key, 0) / M
    return total


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


def parse_events(events, pricing: dict) -> list[dict]:
    """Fold an iterable of trajectory events into completed run records.

    A malformed single event is skipped; it never aborts the whole stream.
    """
    runs: dict[str, RunRecord] = {}
    completed: list[dict] = []

    for ev in events:
        if not isinstance(ev, dict):
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
            usage = sanitize_usage(d.get("usage"))
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

    # Flush still-running
    for r in runs.values():
        pk = f"{r.provider}/{r.model_id}" if r.provider and r.model_id else None
        pe = pricing.get(pk)
        r.cost_usd = estimate_cost(r.usage, pe)
        completed.append(r.to_dict())

    return completed

# ── SQLite trajectory reader ──────────────────────────────────────────
#
# Since the 2026-09-02 migration, runtime trajectory events live in the
# per-agent database at:
#   ~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite
# table `trajectory_runtime_events` (session_id, seq, run_id, event_json,
# created_at).
#
# The database is opened read-only via a file: URI. If the live database is
# locked or mid-write, we fall back to reading a temporary copy so a busy
# gateway can never block or corrupt the dashboard's view.

TRAJECTORY_TABLE = "trajectory_runtime_events"


def _agent_db_paths() -> list[str]:
    return sorted(glob.glob(AGENT_DB_GLOB))


def _agent_id_from_db(path: str) -> str | None:
    # .../agents/<agentId>/agent/openclaw-agent.sqlite
    parts = pathlib.Path(path).parts
    try:
        return parts[parts.index("agents") + 1]
    except (ValueError, IndexError):
        return None


def _query_trajectory(db_path: str, since_created_at: int | None):
    """Yield event dicts from one agent database, newest-safe and read-only."""
    sql = f"SELECT event_json FROM {TRAJECTORY_TABLE}"
    params: tuple = ()
    if since_created_at is not None:
        sql += " WHERE created_at > ?"
        params = (since_created_at,)
    sql += " ORDER BY session_id, seq"

    def _run(target: str):
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA query_only = ON")
            for (event_json,) in conn.execute(sql, params):
                try:
                    yield json.loads(event_json)
                except Exception:
                    continue
        finally:
            conn.close()

    try:
        yield from _run(db_path)
        return
    except sqlite3.DatabaseError:
        pass

    # Locked or mid-checkpoint: work from a snapshot copy instead.
    tmp_dir = tempfile.mkdtemp(prefix="usage-dash-")
    try:
        snapshot = os.path.join(tmp_dir, "snapshot.sqlite")
        shutil.copyfile(db_path, snapshot)
        for suffix in ("-wal", "-shm"):
            side = db_path + suffix
            if os.path.exists(side):
                shutil.copyfile(side, snapshot + suffix)
        yield from _run(snapshot)
    except Exception:
        return
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def parse_agent_db(db_path: str, pricing: dict,
                   since_created_at: int | None = None) -> list[dict]:
    """Parse one per-agent SQLite database into completed run records."""
    agent_id = _agent_id_from_db(db_path)
    runs = parse_events(_query_trajectory(db_path, since_created_at), pricing)
    for r in runs:
        if not r.get("agentId"):
            r["agentId"] = agent_id
    return runs


def _hottest_agent_db() -> str | None:
    """Return the most-recently-modified per-agent SQLite database."""
    files = _agent_db_paths()
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


# ── LM Studio log parser ──────────────────────────────────────────────────────
#
# Parses ~/.lmstudio/server-logs/YYYY-MM/YYYY-MM-DD.N.log files into the same
# run-record shape as the SQLite trajectory reader. Each completion is identified by:
#   START : [TIMESTAMP][INFO][model/id] Running chat completion ...
#   TIMING: print_timing ... prompt eval time = X ms / N tokens  (prompt tokens)
#           print_timing ... eval time = X ms / N tokens          (completion tokens)
#   END   : [TIMESTAMP][INFO][model/id] Finished streaming response
#
# Token counts come from the last print_timing block before the Finished line
# (LM Studio may emit incremental n_decoded lines; final one has full eval time).

_LMS_TS_RE      = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]')
_LMS_START_RE   = re.compile(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\[INFO\]\[([^\]]+)\] Running chat completion')
_LMS_FINISH_RE  = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\[INFO\]\[([^\]]+)\] Finished streaming response')
_LMS_PROMPT_RE  = re.compile(r'prompt eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens')
_LMS_EVAL_RE    = re.compile(r'(?<!prompt )eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens')


def _lmstudio_log_files() -> list[str]:
    """Return all LM Studio server log files, newest first."""
    pattern = os.path.join(LMSTUDIO_LOGS, "**", "*.log")
    files = glob.glob(pattern, recursive=True)
    return sorted(files, key=os.path.getmtime, reverse=True)


def parse_lmstudio_logs(pricing: dict, since_ts: str | None = None) -> list[dict]:
    """
    Parse LM Studio server logs into run records.
    If since_ts (ISO string) is provided, skip runs that ended before it.
    Returns list of dicts in the same shape as RunRecord.to_dict().
    """
    runs: list[dict] = []
    since_dt = None
    if since_ts:
        try:
            since_dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))
        except Exception:
            pass

    for log_path in _lmstudio_log_files():
        file_runs = _parse_lmstudio_log_file(log_path, pricing, since_dt)
        runs.extend(file_runs)
        # If the oldest run in this file is older than since_dt, we can stop
        if since_dt and file_runs:
            oldest = min(r["startedTs"] or "" for r in file_runs)
            if oldest and oldest < since_ts:
                break

    return runs


def _parse_lmstudio_log_file(
    path: str, pricing: dict, since_dt: datetime | None
) -> list[dict]:
    runs: list[dict] = []
    # State for current in-progress run
    cur_model:   str | None = None
    cur_start:   str | None = None      # ISO timestamp
    cur_prompt:  int = 0
    cur_eval:    int = 0
    # Incremental timing buffers (we take the last pair before Finished)
    last_prompt: int = 0
    last_eval:   int = 0

    try:
        with open(path, errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return runs

    for line in lines:
        # START
        m = _LMS_START_RE.match(line)
        if m:
            cur_model  = m.group(1)
            ts_m = _LMS_TS_RE.match(line)
            cur_start  = _lms_ts_to_iso(ts_m.group(1)) if ts_m else None
            last_prompt = 0
            last_eval   = 0
            continue

        if cur_model is None:
            continue

        # TIMING lines (accumulate; keep last complete pair)
        pm = _LMS_PROMPT_RE.search(line)
        if pm:
            last_prompt = int(pm.group(1))
        em = _LMS_EVAL_RE.search(line)
        if em:
            last_eval = int(em.group(1))

        # FINISH
        fm = _LMS_FINISH_RE.match(line)
        if fm:
            end_ts    = _lms_ts_to_iso(fm.group(1))
            model_id  = fm.group(2)   # e.g. "qwen/qwen3-4b"
            provider  = "lmstudio"
            pk        = f"{provider}/{model_id}"
            usage     = {"input": last_prompt, "output": last_eval}
            pe        = pricing.get(pk)
            cost      = estimate_cost(usage, pe)  # $0 for local, but tracks tokens

            # duration
            dur_ms = None
            if cur_start and end_ts:
                try:
                    s = datetime.fromisoformat(cur_start)
                    e = datetime.fromisoformat(end_ts)
                    dur_ms = int((e - s).total_seconds() * 1000)
                except Exception:
                    pass

            # skip if older than since_dt
            if since_dt and end_ts:
                try:
                    end_dt = datetime.fromisoformat(end_ts)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    if end_dt < since_dt:
                        cur_model = None
                        continue
                except Exception:
                    pass

            runs.append({
                "runId":      f"lms-{uuid.uuid5(uuid.NAMESPACE_URL, f'{cur_start or end_ts}/{model_id}/{last_prompt}/{last_eval}')}",
                "sessionId":  None,
                "sessionKey": None,
                "provider":   provider,
                "modelId":    model_id,
                "modelApi":   None,
                "channel":    "lmstudio-local",
                "agentId":    None,
                "trigger":    "lmstudio",
                "startedTs":  cur_start,
                "endedTs":    end_ts,
                "status":     "success",
                "usage":      usage,
                "costUsd":    round(cost, 8),
                "durationMs": dur_ms,
                "aborted":    False,
                "timedOut":   False,
            })
            cur_model  = None
            cur_start  = None
            last_prompt = 0
            last_eval   = 0

    return runs


def _lms_ts_to_iso(ts: str) -> str:
    """Convert '2026-07-07 17:47:07' to ISO-8601 UTC string."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return ts


# ── Store ──────────────────────────────────────────────────────────────────────

class Store:
    MAX_RUNS = 2000

    def __init__(self):
        self.runs:    list[dict] = []
        self.pricing: dict = {}
        self.lock     = threading.Lock()
        self.sse_clients: list[queue.Queue] = []
        self.active_session_file: str | None = None  # basename exposed to UI

    def reload_pricing(self):
        self.pricing = load_pricing()

    def initial_load(self):
        self.reload_pricing()
        all_runs: list[dict] = []
        for path in _agent_db_paths():
            all_runs.extend(parse_agent_db(path, self.pricing))
        # Merge LM Studio local runs
        lms_runs = parse_lmstudio_logs(self.pricing)
        all_runs.extend(lms_runs)
        all_runs.sort(key=lambda r: r.get("startedTs") or "", reverse=True)
        hot = _hottest_agent_db()
        with self.lock:
            self.runs = all_runs[:self.MAX_RUNS]
            self._lms_log_mtimes = {p: int(os.path.getmtime(p)) for p in _lmstudio_log_files()}
            self.active_session_file = os.path.basename(hot) if hot else None

    def poll_new(self):
        """Called by background thread. Returns list of newly completed runs."""
        self.reload_pricing()
        parsed_runs: list[dict] = []
        for path in _agent_db_paths():
            parsed_runs.extend(parse_agent_db(path, self.pricing))

        # Poll LM Studio logs for new completions
        lms_files = _lmstudio_log_files()
        lms_changed = []
        for path in lms_files:
            try:
                mtime = int(os.path.getmtime(path))
            except Exception:
                continue
            if getattr(self, '_lms_log_mtimes', {}).get(path, 0) != mtime:
                lms_changed.append(path)
                self._lms_log_mtimes = getattr(self, '_lms_log_mtimes', {})
                self._lms_log_mtimes[path] = mtime
        # Always re-parse the hottest LM Studio log (active completions)
        if lms_files and lms_files[0] not in lms_changed:
            lms_changed.append(lms_files[0])
        for path in lms_changed:
            lms_runs = _parse_lmstudio_log_file(path, self.pricing, since_dt=None)
            parsed_runs.extend(lms_runs)

        # Update hot-file tracking
        with self.lock:
            hot = _hottest_agent_db()
            self.active_session_file = os.path.basename(hot) if hot else None

        if not parsed_runs:
            return []

        parsed_runs.sort(key=lambda r: r.get("startedTs") or "", reverse=True)

        with self.lock:
            existing_by_id = {r["runId"]: r for r in self.runs}

        changed_runs: list[dict] = []
        changed_ids: set[str] = set()
        for r in parsed_runs:
            run_id = r.get("runId")
            if not run_id:
                continue
            prev = existing_by_id.get(run_id)
            if prev != r:
                changed_runs.append(r)
                changed_ids.add(run_id)

        if changed_runs:
            with self.lock:
                self.runs = [r for r in self.runs if r["runId"] not in changed_ids]
                self.runs = (changed_runs + self.runs)[:self.MAX_RUNS]
            self._broadcast(changed_runs)

        return changed_runs

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
                "re-read from SQLite every 5 s and may lag by up to one turn."
            ) if hot else None,
            "pricingFreshness": pricing_freshness(),
        }

    def get_stats(self) -> dict:
        with self.lock:
            runs = self.runs

        total_cost = sum(r.get("costUsd", 0) for r in runs)
        total_input  = sum(r.get("usage", {}).get("input", 0) for r in runs)
        total_output = sum(r.get("usage", {}).get("output", 0) for r in runs)
        total_cache_read  = sum(r.get("usage", {}).get("cacheRead", 0) for r in runs)
        total_cache_write = sum(r.get("usage", {}).get("cacheWrite", 0) for r in runs)
        total_cache_write_1h = sum(r.get("usage", {}).get("cacheWrite1h", 0) for r in runs)

        by_model: dict[str, dict] = {}
        by_provider: dict[str, dict] = {}
        by_channel: dict[str, dict] = {}

        def acc(bucket: dict, key: str, r: dict):
            if key not in bucket:
                bucket[key] = {"runs": 0, "costUsd": 0.0, "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "cacheWrite1h": 0}
            b = bucket[key]
            b["runs"] += 1
            b["costUsd"] += r.get("costUsd", 0)
            u = r.get("usage", {})
            b["input"]      += u.get("input", 0)
            b["output"]     += u.get("output", 0)
            b["cacheRead"]  += u.get("cacheRead", 0)
            b["cacheWrite"] += u.get("cacheWrite", 0)
            b["cacheWrite1h"] += u.get("cacheWrite1h", 0)

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
                "cacheWrite1h": total_cache_write_1h,
                "total":      total_input + total_output + total_cache_read + total_cache_write + total_cache_write_1h,
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


# ── Console truth persistence ─────────────────────────────────────────────────

CONSOLE_STATE_PATH = OPENCLAW_STATE / "usage-dashboard-console.json"

def load_console_state() -> dict:
    try:
        with open(CONSOLE_STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_console_state(data: dict):
    try:
        with open(CONSOLE_STATE_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


STORE = Store()


def background_poller():
    while True:
        time.sleep(5)
        try:
            STORE.poll_new()
        except Exception:
            pass


def schedule_weekly_freshness_check():
    """Re-check pricing-source age once a day and warn loudly once it has
    gone over PRICING_MAX_AGE_DAYS. This only ever reads dates baked into
    pricing.py; it never fetches or rewrites prices (see pricing.py's
    "Freshness enforcement" section for why)."""
    while True:
        try:
            warn_if_stale()
        except Exception:
            pass
        time.sleep(24 * 60 * 60)


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

        elif path == "/api/console":
            self.send_json(load_console_state())

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

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body)
        except Exception:
            self.send_json({"error": "bad json"}, 400)
            return

        if path == "/api/console":
            window  = str(data.get("window", ""))[:20]
            amount  = data.get("amount")
            if not window or not isinstance(amount, (int, float)) or amount < 0:
                self.send_json({"error": "invalid"}, 400)
                return
            state = load_console_state()
            state[window] = round(float(amount), 4)
            save_console_state(state)
            self.send_json({"ok": True, "window": window, "amount": state[window]})
        else:
            self.send_json({"error": "not found"}, 404)


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Loading trajectory data from {OPENCLAW_STATE}/agents/*/agent/openclaw-agent.sqlite …")
    STORE.initial_load()
    runs = STORE.get_runs(5)
    print(f"Loaded {len(STORE.runs)} runs ({len(STORE.pricing)} priced models).")

    warn_if_stale()

    t = threading.Thread(target=background_poller, daemon=True)
    t.start()

    freshness_thread = threading.Thread(
        target=schedule_weekly_freshness_check, daemon=True)
    freshness_thread.start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard → http://127.0.0.1:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")
