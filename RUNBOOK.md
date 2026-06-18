# OpenClaw Usage Dashboard Runbook

This dashboard is a small Python HTTP service with a long-lived SSE endpoint.
It should be run under a **systemd user service**, not as an ad-hoc shell
process.

## Files

- `scripts/start-usage-dashboard.sh` — canonical startup wrapper
- `scripts/install-systemd-user-service.sh` — installs/updates the user service
- `server.py` — dashboard server
- `index.html` — frontend

## Install or Update the Service

```bash
cd /path/to/openclaw-usage-dashboard
./scripts/install-systemd-user-service.sh
```

What it does:

- writes `~/.config/systemd/user/openclaw-usage-dashboard.service`
- points `ExecStart` at the repo's startup wrapper
- reloads the user systemd daemon
- enables and starts the service
- enables user lingering when allowed, so the service can start after reboot
  without an interactive login

## Day-2 Operations

Check status:

```bash
systemctl --user status openclaw-usage-dashboard.service
```

Restart after code changes:

```bash
systemctl --user restart openclaw-usage-dashboard.service
```

Tail logs:

```bash
journalctl --user -u openclaw-usage-dashboard.service -f
```

Stop it:

```bash
systemctl --user stop openclaw-usage-dashboard.service
```

Disable boot-start:

```bash
systemctl --user disable --now openclaw-usage-dashboard.service
```

## Verification

The service is healthy when both checks pass:

```bash
systemctl --user is-active openclaw-usage-dashboard.service
curl --fail http://127.0.0.1:9393/api/status
```

Open the dashboard at:

```text
http://127.0.0.1:9393/
```

## Configuration

The installer writes these environment values into the unit:

- `USAGE_DASHBOARD_PORT` — defaults to `9393`
- `OPENCLAW_STATE_DIR` — defaults to `~/.openclaw`
- `PYTHONUNBUFFERED=1` — keeps logs visible in `journalctl`

To change port or state dir later:

1. Export the desired values in your shell.
2. Re-run `./scripts/install-systemd-user-service.sh`.
3. Confirm with `systemctl --user status openclaw-usage-dashboard.service`.

## Troubleshooting

### Port already in use

Symptoms:

- service flaps or exits immediately
- `journalctl` shows bind failures

Fix:

```bash
ss -ltnp | rg ':9393\b'
systemctl --user restart openclaw-usage-dashboard.service
```

If a stray manual process owns the port, stop it before restarting the unit.

### Dashboard loads once, then stops responding

This was previously caused by the service using Python's single-threaded
`HTTPServer`, where one open SSE connection could block every other request.
The server now uses `ThreadingHTTPServer`, so `/events` no longer starves
`/api/*` or `/`.

### Service does not start after reboot

Check user lingering:

```bash
loginctl show-user "${USER}" -p Linger
```

If it says `Linger=no`, enable it:

```bash
loginctl enable-linger "${USER}"
```

### No runs appear

Check that OpenClaw has written trajectory files under:

```text
~/.openclaw/agents/*/sessions/*.trajectory.jsonl
```

If your state directory lives elsewhere, reinstall the unit with:

```bash
OPENCLAW_STATE_DIR=/path/to/.openclaw ./scripts/install-systemd-user-service.sh
```
