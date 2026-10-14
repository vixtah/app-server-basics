"""Server plumbing shared by the apps (Tasks, Reserve, CardScanner, Riftbound).

Modules:

- :mod:`app_server_basics.heartbeats` — the ``GET /heartbeats`` report: how every
  scheduled job, loop, systemd unit and timer an app runs is doing.

Coming: ``auth`` (the owner/admin gate), ``sqlite`` (connection helpers) and ``http``
(polite outside-API calls). Each module is imported on its own; nothing here needs
FastAPI except the route factories, which import it when called.
"""

__version__ = "0.1.1"
