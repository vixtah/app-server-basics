"""Server plumbing shared by the apps (Tasks, Reserve, CardScanner, Riftbound).

Modules:

- :mod:`app_server_basics.heartbeats` — the ``GET /heartbeats`` report: how every
  scheduled job, loop, systemd unit and timer an app runs is doing.
- :mod:`app_server_basics.auth` — who may call the server: the public-tunnel and
  private-network rules, app and per-user tokens, the token and owner gates, and
  keeping tokens out of logs.
- :mod:`app_server_basics.sqlite` — SQLite connections with the apps' settings, the
  worktree read-only guard on live files, added columns, numbered migrations, leases.
- :mod:`app_server_basics.http` — polite calls to outside APIs: per-host cooldowns
  (``Retry-After``), growing pauses for hosts that refuse the server, no request during
  either, and per-host request counts for a status page, all kept in the app's SQLite.

Each module is imported on its own; nothing here needs FastAPI except the route
factories, which import it when called.
"""

__version__ = "0.3.0"
