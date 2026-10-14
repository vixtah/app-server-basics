"""Server plumbing shared by the apps (Tasks, Reserve, CardScanner, Riftbound).

Modules:

- :mod:`app_server_basics.heartbeats` — the ``GET /heartbeats`` report: how every
  scheduled job, loop, systemd unit and timer an app runs is doing.
- :mod:`app_server_basics.auth` — who may call the server: the public-tunnel and
  private-network rules, app and per-user tokens, the token and owner gates, and
  keeping tokens out of logs.

Coming: ``sqlite`` (connection helpers) and ``http``
(polite outside-API calls). Each module is imported on its own; nothing here needs
FastAPI except the route factories, which import it when called.
"""

__version__ = "0.2.0"
