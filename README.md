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
app-server-basics = { git = "https://github.com/vixtah/app-server-basics", tag = "v0.2.1" }
```

It depends on [orbit-jobs](https://github.com/vixtah/orbit-jobs) (the shared job
queue and scheduler), which uv fetches from its own git tag. FastAPI is optional:
leave out `[fastapi]` if you only need the non-web parts.

## What's in it

| Module | What it is for |
|--------|----------------|
| `app_server_basics.heartbeats` | `GET /heartbeats`: how every scheduled job, loop and systemd unit the app runs is doing. |
| `app_server_basics.auth` | Who may call the server: tunnel and home-network rules, tokens, owner-only routes, tokens kept out of logs. |
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
   A **systemd timer** and the oneshot service it starts are one service of kind
   `timer` (`SystemdTimer`): the last run, its length and outcome come from the
   service, and a timer that is stopped, disabled, missing or overdue is `failed`
   with that reason (its `last_ok_at` stays the real last success, never "now").
   A unit or timer whose process also records its own runs with `record_run` (the
   server's lifetime, each backup) names them with `runs=`: the report then has the
   live state from systemd plus the 24-hour run and failure counts from those
   records, and the recorded last run when systemd knows nothing (no systemd on the
   machine, or no run since the last reboot).
3. **Extra checks.** Any function that returns one service as a dict, for things
   the other two can't see.

A service that runs only when asked (a cache rebuilt on a request) has
`expected_every_minutes: null`: on demand, so the collector never calls it late and
only a failed last run counts. Declare it as
`Service("price-history", expected_every_minutes=None)` and wrap only the real
refresh in `record_run`.

The route never fails because one source is broken: that source's services show
`unknown` with the reason. Errors are redacted with orbit-jobs' rule
(`orbit_jobs.redact`: URL query strings, credentials and token-like values) and cut
to 300 characters, timestamps are UTC,
and each service carries exactly the contract's keys (anything else a check returns
is dropped). Don't put secrets, payloads or message contents in a check's error.

### Adding `/heartbeats` to an app

```python
from app_server_basics.heartbeats import SystemdTimer, SystemdUnit, heartbeats_router
from orbit_jobs.schedules import Service

from myapp.scheduler import ENTRIES            # the app's orbit-jobs entries
from myapp.auth import require_admin           # the app's own admin gate (a FastAPI dependency)

SWEEPER = Service("fleet-sweeper", expected_every_minutes=2, title="Fleet sweeper")

app.include_router(heartbeats_router(
    "riftbound",
    entries=[*ENTRIES, SWEEPER],
    units=[SystemdUnit("rift-lab.service", name="Training lab", runs="rift-lab"),
           SystemdTimer("rift-backup.timer", "rift-backup.service", name="Nightly backup",
                        expected_every_minutes=1440)],
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
the same response as a dict, `systemd_service(unit)` one systemd service and
`systemd_timer_service(timer)` one timer.

### Public API

- `build_heartbeats(app, *, entries=(), units=(), extra=(), where=None, now=None, tick_minutes=1)`:
  the whole response.
- `heartbeats_router(app_name, *, entries=(), units=(), extra=(), auth, path="/heartbeats", where=None, tick_minutes=1)`:
  a FastAPI router serving it (needs the `fastapi` extra).
- `SystemdUnit(unit, name=None, expected_every_minutes=5, where="VPS", id=None, runs=None)`:
  a unit to report (a plain unit name also works). `runs` is the orbit-jobs
  `Service` (or its name) whose `record_run` history belongs to the unit.
- `SystemdTimer(timer, service=None, name=None, expected_every_minutes=1440, where="VPS", id=None, runs=None)`:
  a timer and its oneshot service (`service` defaults to the timer's name with
  `.service`, `id` to the timer unit), reported as one service of kind `timer`;
  `runs` as for a unit.
- `systemd_service(unit, *, now=None)`: one unit's service;
  `systemd_timer_service(timer, *, now=None)`: one timer's.
- `FIELDS`, `STATUSES`, `ERROR_CHARS`: the contract's keys, statuses and error length.

Entries default to kind `scheduled` and services to `loop`, unless they declare
`kind=`; `where` defaults to the app's `where=`. systemd doesn't count runs, so a
unit's or timer's `runs_24h` and `failures_24h` are null unless it names its `runs`. A timer whose service
started before the last reboot has no recorded outcome: `unknown`, with
`last_run_at` from the timer's last trigger.

## Auth

Every app answers two kinds of callers: its own app (anywhere, through the public
Cloudflare tunnel or at home) and the owner (operational routes: feedback triage,
heartbeats, admin screens). `app_server_basics.auth` holds the rules they share.

**The two rules.**

- `through_tunnel(headers)`: did the request come through the public tunnel? Cloudflare
  puts a `Cf-Connecting-Ip` header on every tunnel request; Wi-Fi, Tailscale and local
  requests have none. Faking the header only makes a request less trusted.
- `private_address(host)` / `client_host(request)`: is the caller on a private network
  (loopback, home network ranges, Tailscale `100.64.0.0/10` and `fd7a:115c:a1e0::/48`,
  IPv6 unique-local, link-local)? Only the connection's own address counts, as Uvicorn
  reports it. `X-Forwarded-For` and similar headers are never read.

**Tokens.** `Tokens(...)` says which tokens are accepted and where a request carries them:

- `env`: the variable with the app's own token (read on every check);
- `hashes_env`: a variable with SHA-256 hashes of more tokens, comma-separated
  (`hash_token("...")` makes one);
- `lookup`: for per-user tokens the app stores (as hashes): a function from a token's
  hash to who it belongs to, or None;
- `header`: the header carrying it (`x-app-token` by default; Riftbound's `x-rift-token`);
- `deprecated_query`: a query parameter old clients still use (`"token"`). Off by
  default; when used, the server logs one warning (without the token).

Comparisons are constant-time, and no token is put into an error, a log line or a
response body. The one exception is `TokenGate`'s optional session cookie, which hands
the token back only to the client that just sent it.

**Gates.**

| Piece | For |
|-------|-----|
| `require_token(tokens)` | FastAPI dependency: a valid token on every transport (401). Returns who it belongs to. |
| `require_owner(tokens)` | FastAPI dependency for owner-only routes: never through the tunnel, only from a private network, and with a valid token. Tunnel or public callers get 404 (so the route doesn't advertise itself), a private caller without a token 401. Statuses, messages and allowed identities are parameters. |
| `TokenGate` | ASGI middleware for apps that trust direct connections: tunnel requests need a token, everything else passes. Exempt hostnames (behind Cloudflare Access) and an optional session cookie for web views, which must come from the same site to write or open a WebSocket. |
| `install_log_redaction()` | A logging filter that hides `?token=` values and token headers in Uvicorn's request lines, the app's own log lines and tracebacks. Call it before `uvicorn.run`. |

**One example per kind of app.**

An app whose every request needs its token, with owner-only routes (CardScanner, Reserve):

```python
from app_server_basics.auth import Tokens, install_log_redaction, require_owner, require_token

TOKENS = Tokens(env="CARDS_ACCESS_TOKEN", hashes_env="CARDS_ACCESS_TOKEN_HASHES")
app.include_router(api, dependencies=[Depends(require_token(TOKENS))])
app.include_router(heartbeats_router("cardscanner", auth=require_owner(TOKENS)))
install_log_redaction()
```

An app with one token per player stored as hashes (Riftbound):

```python
TOKENS = Tokens(header="x-rift-token", deprecated_query="token", lookup=store.name_for_hash)
player = Depends(require_token(TOKENS))                # the invite's name
admin = Depends(require_owner(TOKENS, identities=ADMIN_NAMES))
install_log_redaction(headers=("x-rift-token",))
uvicorn.run(app, ...)
```

An app whose home connections are trusted and whose tunnel needs a token (Tasks):

```python
app.add_middleware(TokenGate, tokens=Tokens(env="TASKS_ACCESS_TOKEN"),
                   exempt_hosts={"hellodash.cc", "www.hellodash.cc"},
                   cookie="tasks_session")
install_log_redaction(params=("token", "tasks_session"))
uvicorn.run(app, ...)
```

### Public API

- `TUNNEL_HEADER`, `PRIVATE_NETWORKS`: the header and address ranges the two rules use.
- `through_tunnel(headers, header=TUNNEL_HEADER)`, `private_address(host)`,
  `client_host(request_or_scope)`.
- `hash_token(token)`: SHA-256 hex.
- `Tokens(env=None, hashes_env=None, lookup=None, identity="owner", header="x-app-token", deprecated_query=None)`
  with `match(token)`, `amatch(token)` (lookup off the event loop), `given(request)` and
  `identify(request)`.
- `require_token(tokens, *, status=401, detail=...)`.
- `require_owner(tokens, *, refuse_status=404, refuse_detail="Not Found", token_status=401, token_detail=..., identities=None, tunnel_header=TUNNEL_HEADER)`.
- `TokenGate(app, *, tokens, exempt_hosts=(), cookie=None, tunnel_header=TUNNEL_HEADER, detail=..., ws_close_code=4401)`;
  the matched identity is in `request.state.token_identity` for tunnel requests.
- `install_log_redaction(*, loggers=UVICORN_LOGGERS, root=True, params=("token",), headers=("x-app-token", "x-rift-token"))`,
  `RedactTokens` (the filter), `redact(text, params, headers)`.

Everything but `require_token` and `require_owner` (which import FastAPI) is standard
library only.

## Developing

```bash
uv sync
uv run pytest
```

Tests need no network and no systemd (systemctl is faked).
