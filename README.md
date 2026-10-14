# app-server-basics

The server plumbing the apps (Tasks, Reserve, CardScanner, Riftbound) each used to
write for themselves, in one small Python library. It holds code only: never
tokens, passwords or data.

Install it with uv from a git tag (it is not on PyPI):

```toml
# pyproject.toml of an app
[project]
dependencies = ["app-server-basics[fastapi]"]

[tool.uv.sources]
app-server-basics = { git = "https://github.com/vixtah/app-server-basics", tag = "v0.1.0" }
```

It depends on [orbit-jobs](https://github.com/vixtah/orbit-jobs) (the shared job
queue and scheduler), which uv fetches from its own git tag. FastAPI is optional:
leave out `[fastapi]` if you only need the non-web parts.

## What's in it

| Module | What it is for |
|--------|----------------|
| `app_server_basics.heartbeats` | `GET /heartbeats`: how every scheduled job, loop and systemd unit the app runs is doing. |
| `app_server_basics.auth` | *Coming.* The owner/admin gate every app now writes itself. |
| `app_server_basics.sqlite` | *Coming.* SQLite connection helpers. |
| `app_server_basics.http` | *Coming.* Polite calls to outside APIs (rate limits, backoff, cooldowns). |

## Heartbeats

`/heartbeats` answers the contract in the automations repository
(`docs/plans/HEARTBEATS.md`): one entry per service, with when it last ran, when it
last succeeded, its last status and error, and how many runs and failures it had in
24 hours. The Orbit Book collector on the Mac reads it every 5 minutes.

```json
{"app": "reserve", "generated_at": "2026-10-13T22:00:00Z", "services": [{
  "id": "refresh-planner", "name": "Venue refresh planner", "kind": "loop",
  "where": "VPS", "expected_every_minutes": 5,
  "last_run_at": "2026-10-13T21:55:00Z", "last_ok_at": "2026-10-13T21:55:00Z",
  "last_status": "ok", "last_error": null, "last_duration_ms": 1234,
  "runs_24h": 288, "failures_24h": 0}]}
```

It collects services from three places:

1. **orbit-jobs schedules.** Entries the app runs with `orbit_jobs.schedules.run_due`,
   and hand-written loops declared as `Service(...)` that wrap each pass in
   `record_run(...)`.
2. **systemd units** the app names. They are read with `systemctl show` (no
   privileges needed). A unit that is up is `ok`; one that failed or is restarting
   after a crash is `failed`, with the result, exit status and restart count as the
   error. If systemctl or the unit is missing, the status is `unknown`.
3. **Extra checks.** Any function that returns one service as a dict, for things
   the other two can't see.

The route never fails because one source is broken: that source's services show
`unknown` with the reason. Errors are cut to 300 characters, timestamps are UTC,
and each service carries exactly the contract's keys (anything else a check returns
is dropped). Don't put secrets, payloads or message contents in a check's error.

### Adding `/heartbeats` to an app

```python
from app_server_basics.heartbeats import SystemdUnit, heartbeats_router
from orbit_jobs.schedules import Service

from myapp.scheduler import ENTRIES            # the app's orbit-jobs entries
from myapp.auth import require_admin           # the app's own admin gate (a FastAPI dependency)

SWEEPER = Service("fleet-sweeper", expected_every_minutes=2, title="Fleet sweeper")

app.include_router(heartbeats_router(
    "riftbound",
    entries=[*ENTRIES, SWEEPER],
    units=[SystemdUnit("rift-lab.service", name="Training lab")],
    auth=require_admin,
    where="VPS",
), prefix="/api/riftbound")
```

`auth` is required: pass the same dependency the app's other owner-only routes use
(home network or Tailscale only). It refuses by raising, as FastAPI dependencies do.

The hand-written loop records its passes like this:

```python
from orbit_jobs.schedules import record_run

while True:
    try:
        async with record_run(SWEEPER):        # running, then ok or failed
            await sweep_fleet()
    except Exception:
        log.exception("sweep failed")
    await asyncio.sleep(120)
```

Without FastAPI, `build_heartbeats(app, entries=..., units=..., extra=...)` returns
the same response as a dict, and `systemd_service(unit)` one systemd service.

### Public API

- `build_heartbeats(app, *, entries=(), units=(), extra=(), where=None, now=None, tick_minutes=1)`:
  the whole response.
- `heartbeats_router(app_name, *, entries=(), units=(), extra=(), auth, path="/heartbeats", where=None, tick_minutes=1)`:
  a FastAPI router serving it (needs the `fastapi` extra).
- `SystemdUnit(unit, name=None, expected_every_minutes=5, where="VPS", id=None)`:
  a unit to report (a plain unit name also works).
- `systemd_service(unit, *, now=None)`: one unit's service.
- `FIELDS`, `STATUSES`, `ERROR_CHARS`: the contract's keys, statuses and error length.

Entries default to kind `scheduled` and services to `loop`, unless they declare
`kind=`; `where` defaults to the app's `where=`. systemd doesn't count runs, so a
unit's `runs_24h` and `failures_24h` are null.

## Developing

```bash
uv sync
uv run pytest
```

Tests need no network and no systemd (systemctl is faked).
