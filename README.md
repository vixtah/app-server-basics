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
| `app_server_basics.sqlite` | SQLite connections with the apps' settings, the worktree guard on live files, added columns, migrations, leases. |
| `app_server_basics.http` | Polite calls to outside APIs: cooldowns on 429, growing pauses for hosts that refuse the server, per-host request counts. |

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
   A timer counting from boot or from its service (`OnBootSec=`, `OnUnitActiveSec=`)
   has its next run on the monotonic clock (`NextElapseUSecMonotonic`), read against
   the machine's own.
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

## SQLite

Every app opens its SQLite files the same way: WAL, a busy timeout
(`AUTOMATIONS_SQLITE_BUSY_TIMEOUT_MS`, default 30 000), `sqlite3.Row` rows, and foreign
keys off unless asked for. Code running from a git worktree (an agent's checkout) opens
the app's **live** file read-only, so an agent never writes production data by
accident; `AUTOMATIONS_ALLOW_LIVE_DB_WRITES=1` lifts that. Each app keeps its own paths
and schemas and tells the guard where its code lives:

```python
from app_server_basics import sqlite as db

LIVE = Path.home() / ".local/share/automations/reserve.db"
DB_PATH = Path(os.environ.get("AUTOMATIONS_RESERVE_DB_PATH") or LIVE)

conn, guarded = db.connect(DB_PATH, live_path=LIVE, checkout=__file__)
if not guarded:                                # a guarded connection skips DDL
    conn.execute("CREATE TABLE IF NOT EXISTS venues (id TEXT PRIMARY KEY, data TEXT)")
    db.ensure_columns(conn, "venues", {"enrichment": "TEXT"})
    db.apply_migrations(conn, "reserve", [(1, migrate_one), (2, migrate_two)])
```

### Public API

- `connect(path, *, live_path=None, checkout=None, timeout=None, foreign_keys=False)`:
  `(conn, guarded)`, with the parent directory made, the guard or the pragmas applied.
- `configure(conn, *, foreign_keys=False)`: WAL and the busy timeout on a connection;
  `busy_timeout_ms()`.
- `guarded_connect(path, *, live_path, checkout, timeout=5.0)`: `(conn, guarded)` without
  pragmas; `guard_active_for(path, live_path, *, checkout)`; `in_worktree(checkout)`.
- `ensure_columns(conn, table, {column: ddl})`: add the columns a live table lacks
  (returns the ones added).
- `apply_migrations(conn, module, [(version, fn), ...])`: run each numbered migration once
  per database (`schema_migrations`), in order (returns the versions run).
- `create_lease_table(conn, table="leases")`, `acquire_lease(conn, key, owner, seconds=900, *, table="leases")`,
  `release_lease(conn, key, owner, *, table="leases")`: one owner per key across
  processes; an expired lease (a crashed owner) is taken over. The caller commits.
- `now_iso()`: UTC ISO timestamp for writes; `simulated_clock(fn)` replaces it in tests.

Standard library only.

## Polite outside-API calls

`app_server_basics.http` keeps an app from hammering the sites it reads, and shows how
each site is doing. Per host, kept in the app's own SQLite file so restarts, deploys and
other processes see the same state:

- **Cooldowns.** A 429 stops the host until its `Retry-After` (seconds or an HTTP date),
  within the policy's minimum and maximum. No request is sent meanwhile, and a 429 is
  never retried at once (that is what extends a ban).
- **Pauses.** A host that refuses the server (403, or a page the caller says is a bot wall
  instead of data) is paused from the second refusal in a row: 1 hour, then 2, 4, 8, 16,
  at most 24. The count is remembered 30 days after the pause, so expiry or a restart
  doesn't start over; only a success on the request that was refused (same method and
  path) clears it.
- **Counts.** Every request sent is counted (status, or `timeout`/`transport`); skipped
  ones are not. `Hosts.stats()` gives each host's last 24 hours.
- **No secrets in errors.** A refusal is remembered as `METHOD /path`, never the query;
  `describe_error(exc)` is a redacted one-line error (`orbit_jobs.redact`).

The rules are a `Policy`; the defaults are Reserve's. CardScanner's news sources use a
daily one:

```python
DAILY = Policy(cooldown_statuses=frozenset({403, 429}), cooldown_default=0,
               cooldown_min=0, cooldown_max=None, cooldown_until_utc_midnight=True,
               retry_after_any_status=True, block_statuses=frozenset())
```

It works with any HTTP library: `PoliteClient` needs a `send(method, url, **kwargs)`
returning a response with `status_code` and `headers` (`httpx.request`, an
`httpx.Client`'s or a curl_cffi `Session`'s `request`).

```python
from app_server_basics import http

# In the app's schema setup, on its own database:
http.create_tables(conn)

HOSTS = http.Hosts(store.connection, http.Policy())   # connect(): a context manager
client = http.PoliteClient(HOSTS, httpx.request, retries=2)

try:
    response = client.request("GET", "https://api.example.com/sheet?key=...", timeout=20,
                              refused=lambda r: "HTML instead of data"
                              if "html" in r.headers.get("content-type", "") else None)
    response.raise_for_status()
except http.HostUnavailable as exc:                  # CoolingDown or Blocked: not sent
    ...
```

`Hosts.stats()` is one entry per host with requests in the last 7 days or a cooldown or
remembered refusal now, in the shape of Reserve's `/api/reserve/sources`:

```json
{"id": "hardingpark.ezlinksgolf.com", "host": "hardingpark.ezlinksgolf.com",
 "requests_24h": 5, "ok_24h": 3, "rate_limited_24h": 0, "rejected_24h": 2, "errors_24h": 0,
 "last_status_code": 403, "last_ok_at": "2026-10-13T21:10:04Z",
 "cooldown_until": "2026-10-14T03:36:26Z", "status": "blocked",
 "note": "refuses this server (HTTP 403); paused until 2026-10-14T03:36:26Z"}
```

`status` is the first match of `blocked` (paused now, or mostly 401/403 in 24 hours),
`rate_limited` (cooling down now, or a 429 in the last hour), `failing` (mostly 5xx or
no answer), `idle` (no requests in 24 hours), else `ok`. `cooldown_until` is the later of
the cooldown and the pause. The app adds its own names and groups (`id`).

### Public API

- `Policy(cooldown_statuses={429}, cooldown_default=300, cooldown_min=60, cooldown_max=21600, cooldown_until_utc_midnight=False, retry_after_any_status=False, block_statuses={403}, block_after=2, block_first=3600, block_cap=86400, block_memory=2592000, keep_attempts=604800)`.
- `Hosts(connect, policy=None, *, key=None, clock=None, scope=None, batch=200, flush_seconds=30)`:
  `key(url)`, `state(url)`, `check(method, url)` (raises `CoolingDown` / `Blocked`),
  `cooldown(url, status, headers)`, `refuse(method, url, reason)` (returns the `Blocked`
  to raise), `accept(method, url, state)`, `states(now=None)`, `record(url, status=None, error=None)`,
  `flush()`, `stats(now=None, *, window=86400, recent=3600)`. `key` maps a host name to the
  bucket its state and counts are kept under; `scope` names the database counts belong
  to (tests that repoint it).
- `PoliteClient(hosts, send, retries=0, backoff=..., pace=None)` with
  `request(method, url, *, refused=None, **kwargs)`.
- `HostUnavailable` (`host`, `until`, `status_code`, `reason`), `CoolingDown`, `Blocked`.
- `HostState`; `create_tables(conn)`, `read_state(conn, host, now)`,
  `merge_state(conn, state, now=None)` (to import an app's older records).
- `retry_after(value, now=None)`, `host_of(url)`, `request_label(method, url)`,
  `redact_url(url)`, `error_category(exc)`, `describe_error(exc)`; `STATS_FIELDS`,
  `STATUSES`.

## Developing

```bash
uv sync
uv run pytest
```

Tests need no network and no systemd (systemctl is faked).
