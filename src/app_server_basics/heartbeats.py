"""``GET /heartbeats``: how every scheduled job, loop and systemd unit an app runs is
doing, in the shape of the heartbeat contract (automations
``docs/plans/HEARTBEATS.md``)::

    {"app": "reserve", "generated_at": "2026-10-13T22:00:00Z", "services": [{
        "id", "name", "kind", "where", "expected_every_minutes", "last_run_at",
        "last_ok_at", "last_status", "last_error", "last_duration_ms", "runs_24h",
        "failures_24h"}]}

The services come from three places, in this order:

1. orbit-jobs ``health()`` over the app's schedule entries and hand-written loops
   (``orbit_jobs.schedules.Entry`` / ``Service``);
2. systemd units the app names (:class:`SystemdUnit` or a unit name, and
   :class:`SystemdTimer` for a timer plus the oneshot service it starts), read with
   ``systemctl show`` (no privileges);
3. extra checks: callables the app passes, each returning one service dict.

Nothing here raises: a broken source reports its services as ``unknown`` with the
reason in ``last_error``. Every service carries exactly the contract's keys, errors
are cut to 300 characters, and timestamps are UTC ``...Z``. ``expected_every_minutes``
null means on demand: the service has no cadence, so it is never late; only a failed
last run matters. Read-only; never reports
secrets, payloads or message contents (extra checks must not put them in either).

:func:`build_heartbeats` needs only orbit-jobs; :func:`heartbeats_router` needs
FastAPI (the ``fastapi`` extra) and imports it when called.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orbit_jobs.schedules import Entry, Service, health

__all__ = [
    "ERROR_CHARS", "FIELDS", "STATUSES", "SystemdTimer", "SystemdUnit", "build_heartbeats",
    "heartbeats_router", "systemd_service", "systemd_timer_service",
]

#: Errors are cut to this many characters.
ERROR_CHARS = 300
#: The keys of one service, in contract order; nothing else is reported.
FIELDS = (
    "id", "name", "kind", "where", "expected_every_minutes", "last_run_at",
    "last_ok_at", "last_status", "last_error", "last_duration_ms", "runs_24h",
    "failures_24h",
)
STATUSES = frozenset({"ok", "failed", "running", "skipped", "unknown"})
_TIMESTAMPS = ("last_run_at", "last_ok_at")
_PROPS = ("LoadState,ActiveState,Result,ExecMainStatus,ActiveEnterTimestamp,NRestarts,"
          "ExecMainStartTimestamp,ExecMainExitTimestamp")
_TIMER_PROPS = "LoadState,ActiveState,UnitFileState,LastTriggerUSec,NextElapseUSecRealtime"
_SYSTEMCTL_TIMEOUT = 5
#: A timer whose next run is this far in the past has stopped firing.
TIMER_OVERDUE_MINUTES = 30

Runner = Callable[..., subprocess.CompletedProcess]
Check = Callable[[], dict[str, Any]]


@dataclass(frozen=True)
class SystemdUnit:
    """A systemd unit the app owns. ``id`` defaults to the unit name and ``name``
    (shown to the owner) to the id. ``expected_every_minutes`` matters for
    timer-run units; an always-on unit that is up counts as ok right now."""

    unit: str
    name: str | None = None
    expected_every_minutes: float | None = 5
    where: str = "VPS"
    id: str | None = None


@dataclass(frozen=True)
class SystemdTimer:
    """A systemd timer and the oneshot service it starts, reported as one service of
    kind ``timer``. ``service`` defaults to the timer's own name with ``.service``
    (systemd's default). ``id`` defaults to the timer unit and ``name`` to the id;
    ``expected_every_minutes`` is the timer's cadence (a day by default)."""

    timer: str
    service: str | None = None
    name: str | None = None
    expected_every_minutes: float | None = 1440
    where: str = "VPS"
    id: str | None = None

    @property
    def service_unit(self) -> str:
        if self.service:
            return self.service
        return self.timer.removesuffix(".timer") + ".service"


# ── helpers ───────────────────────────────────────────────────────────


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value: Any) -> str | None:
    """An ISO timestamp (or datetime) as UTC ``...Z``; None when absent or unreadable."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return _z(dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def _short(text: Any) -> str | None:
    return None if text is None else str(text)[:ERROR_CHARS]


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:ERROR_CHARS]


def _service(raw: dict[str, Any], **defaults: Any) -> dict[str, Any]:
    """One service with exactly the contract's keys: missing ones from ``defaults``
    or None, an unknown status as ``unknown``, errors cut, timestamps as ``...Z``."""
    item = {key: raw.get(key, defaults.get(key)) for key in FIELDS}
    if item["last_status"] not in STATUSES:
        item["last_status"] = "unknown"
    item["last_error"] = _short(item["last_error"])
    for key in _TIMESTAMPS:
        item[key] = _timestamp(item[key])
    if item["id"] is not None:
        item["id"] = str(item["id"])
    if item["name"] is None:
        item["name"] = item["id"]
    return item


def _unknown(id_: str, error: str | None, **fields: Any) -> dict[str, Any]:
    return _service({"id": id_, "last_status": "unknown", "last_error": error}, **fields)


# ── systemd ───────────────────────────────────────────────────────────


def _systemctl_time(value: str) -> datetime | None:
    """``Mon 2026-10-13 22:00:00 UTC`` (``--timestamp=utc``; a local zone name from an
    older systemd is read as local time); None when empty or unreadable."""
    parts = value.split()
    if len(parts) != 4:
        return None
    try:
        dt = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if parts[3] in ("UTC", "GMT"):
        return dt.replace(tzinfo=UTC)
    if parts[3] in time.tzname:
        return dt.astimezone()  # naive = local time
    return None


def _show(unit: str, run: Runner, props: str = _PROPS) -> dict[str, str] | str:
    """``systemctl show`` properties, or why they couldn't be read."""
    if run is subprocess.run and shutil.which("systemctl") is None:
        return "systemctl not available"
    base = ["systemctl", "show", unit, "-p", props]
    out = None
    try:
        # --timestamp=utc needs systemd 247+; an older one rejects it, so retry without.
        for cmd in ([*base, "--timestamp=utc"], base):
            out = run(cmd, capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT)
            if out.returncode == 0:
                break
    except FileNotFoundError:
        return "systemctl not available"
    except Exception as exc:  # noqa: BLE001 - never raise; report unknown
        return _error_text(exc)
    if out is None or out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip() if out is not None else ""
        return _short(f"systemctl show failed: {detail}" if detail else "systemctl show failed")
    props = {}
    for line in out.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def systemd_service(unit: SystemdUnit | str, *, now: datetime | None = None,
                    run: Runner = subprocess.run) -> dict[str, Any]:
    """One contract service for a systemd unit, from ``systemctl show``.

    ``last_status``: ``ok`` while active (or after a clean exit), ``running`` while
    starting or stopping, ``failed`` when the unit failed or is restarting after a
    failure, ``unknown`` when systemctl or the unit is missing or the unit never ran.
    ``last_run_at`` is when it last started; ``last_ok_at`` is ``now`` while it is up
    and its clean exit time after one. ``runs_24h`` / ``failures_24h`` are None:
    systemd doesn't count them. ``run`` is the subprocess runner (tests fake it)."""
    spec = SystemdUnit(unit) if isinstance(unit, str) else unit
    fields = {"id": spec.id or spec.unit, "name": spec.name, "kind": "systemd",
              "where": spec.where, "expected_every_minutes": spec.expected_every_minutes}
    props = _show(spec.unit, run)
    if isinstance(props, str):
        return _unknown(fields["id"], props, **fields)
    if props.get("LoadState") == "not-found":
        return _unknown(fields["id"], "unit not found", **fields)

    state, result = props.get("ActiveState", ""), props.get("Result", "")
    started = (_systemctl_time(props.get("ExecMainStartTimestamp", ""))
               or _systemctl_time(props.get("ActiveEnterTimestamp", "")))
    exited = _systemctl_time(props.get("ExecMainExitTimestamp", ""))
    clean = result in ("", "success")
    ok_at = None
    if state == "failed" or not clean:
        status = "failed"
    elif state in ("active", "reloading"):
        status, ok_at = "ok", now or datetime.now(UTC)
    elif state in ("activating", "deactivating"):
        status = "running"
    elif state == "inactive" and started is not None:
        status, ok_at = "ok", exited or started
    else:
        status = "unknown"

    error = None
    if status == "failed":
        error = f"{state or 'unknown'}: result {result or 'unknown'}"
        if props.get("ExecMainStatus") not in (None, "", "0"):
            error += f", exit status {props['ExecMainStatus']}"
        if props.get("NRestarts") not in (None, "", "0"):
            error += f", {props['NRestarts']} restarts"
    duration = None
    if started is not None and exited is not None and exited >= started and state != "active":
        duration = int((exited - started).total_seconds() * 1000)
    return _service({"last_run_at": started, "last_ok_at": ok_at, "last_status": status,
                     "last_error": error, "last_duration_ms": duration,
                     "runs_24h": None, "failures_24h": None}, **fields)


def systemd_timer_service(timer: SystemdTimer, *, now: datetime | None = None,
                          run: Runner = subprocess.run) -> dict[str, Any]:
    """One contract service (kind ``timer``) for a timer and the oneshot it starts.

    The last run comes from the service: ``last_run_at`` is its last start (the timer's
    last trigger when systemd no longer remembers the start, e.g. after a reboot),
    ``last_ok_at`` the end of its last clean run, ``last_duration_ms`` that run's length.
    ``last_status`` is ``running`` while the service runs, ``failed`` when its last run
    failed, ``ok`` after a clean run and ``unknown`` before any run. The timer decides
    whether more runs will come: a timer that is stopped, disabled, missing, has no
    next run (except while its service runs: a timer counting from its service's
    start, ``OnUnitActiveSec=``, schedules the next run only once it ends) or is overdue by :data:`TIMER_OVERDUE_MINUTES` makes the service
    ``failed`` with that reason (``last_ok_at`` stays the real last success, so a
    collector also sees it late). ``runs_24h`` / ``failures_24h`` are None."""
    at = now or datetime.now(UTC)
    service_unit = timer.service_unit
    fields = {"id": timer.id or timer.timer, "name": timer.name, "kind": "timer",
              "where": timer.where, "expected_every_minutes": timer.expected_every_minutes}
    tprops = _show(timer.timer, run, _TIMER_PROPS)
    if isinstance(tprops, str):
        return _unknown(fields["id"], tprops, **fields)
    sprops = _show(service_unit, run)
    if isinstance(sprops, str):
        return _unknown(fields["id"], sprops, **fields)

    started = _systemctl_time(sprops.get("ExecMainStartTimestamp", ""))
    exited = _systemctl_time(sprops.get("ExecMainExitTimestamp", ""))
    triggered = _systemctl_time(tprops.get("LastTriggerUSec", ""))
    following = _systemctl_time(tprops.get("NextElapseUSecRealtime", ""))
    state, result = sprops.get("ActiveState", ""), sprops.get("Result", "")
    code = sprops.get("ExecMainStatus", "")
    ran = started is not None and exited is not None and exited >= started

    ok_at, error = None, None
    if sprops.get("LoadState") == "not-found":
        status, error = "failed", f"service {service_unit} not found"
    elif state in ("activating", "deactivating", "reloading") or (state == "active" and not ran):
        status = "running"  # an active oneshot that already exited is RemainAfterExit
    elif state == "failed" or result not in ("", "success") or (ran and code not in ("", "0")):
        status = "failed"
        error = f"last run failed: result {result or 'unknown'}"
        if code not in ("", "0"):
            error += f", exit status {code}"
    elif ran:
        status, ok_at = "ok", exited
    else:
        status = "unknown"

    problems = []
    if tprops.get("LoadState") == "not-found":
        problems.append(f"timer {timer.timer} not found: nothing starts {service_unit}")
    elif tprops.get("ActiveState") != "active":
        problems.append(f"timer {timer.timer} is stopped ({tprops.get('ActiveState') or 'unknown'}):"
                        f" nothing starts {service_unit}")
    else:
        if tprops.get("UnitFileState") in ("disabled", "masked"):
            problems.append(f"timer {timer.timer} is {tprops['UnitFileState']}:"
                            " it won't start after a reboot")
        # A timer counting from its service (OnUnitActiveSec=, OnUnitInactiveSec=) has no
        # next run while that service runs: systemd schedules it once the run ends.
        if following is None:
            if status != "running":
                problems.append(f"timer {timer.timer} has no next run scheduled")
        elif (at - following).total_seconds() > TIMER_OVERDUE_MINUTES * 60:
            problems.append(f"timer {timer.timer} is overdue: its run was due {_z(following)}")
    if problems:
        error = "; ".join([*problems, *([error] if error else [])])
        status = "failed"

    duration = None
    if ran and status != "running":
        duration = int((exited - started).total_seconds() * 1000)
    last_run = max((t for t in (started, triggered) if t is not None), default=None)
    return _service({"last_run_at": last_run, "last_ok_at": ok_at, "last_status": status,
                     "last_error": error, "last_duration_ms": duration,
                     "runs_24h": None, "failures_24h": None}, **fields)


# ── the report ────────────────────────────────────────────────────────


def _default_kind(item: Entry | Service) -> str:
    return "loop" if isinstance(item, Service) else "scheduled"


def _from_orbit(entries: Sequence[Entry | Service], now: datetime | None,
                tick_minutes: float, where: str | None) -> list[dict[str, Any]]:
    entries = list(entries)
    if not entries:
        return []
    try:
        rows = health(entries, now, tick_minutes=tick_minutes)
    except Exception as exc:  # noqa: BLE001 - never raise; report unknown
        error = _error_text(exc)
        return [_unknown(e.name, error, name=getattr(e, "title", None),
                         kind=getattr(e, "kind", None) or _default_kind(e),
                         where=getattr(e, "where", None) or where)
                for e in entries]
    return [_service(row, kind=_default_kind(entry), where=where)
            for entry, row in zip(entries, rows, strict=True)]


def _from_check(check: Check) -> dict[str, Any]:
    fallback = getattr(check, "__name__", None) or type(check).__name__
    try:
        raw = check()
    except Exception as exc:  # noqa: BLE001 - never raise; report unknown
        return _unknown(fallback, _error_text(exc))
    if not isinstance(raw, dict):
        return _unknown(fallback, f"check returned {type(raw).__name__}, not a dict")
    return _service(raw, id=fallback)


def _from_unit(unit: SystemdUnit | SystemdTimer | str, at: datetime,
               run: Runner) -> dict[str, Any]:
    if isinstance(unit, SystemdTimer):
        return systemd_timer_service(unit, now=at, run=run)
    return systemd_service(unit, now=at, run=run)


def build_heartbeats(app: str, *, entries: Iterable[Entry | Service] = (),
                     units: Iterable[SystemdUnit | SystemdTimer | str] = (),
                     extra: Iterable[Check] = (),
                     where: str | None = None, now: datetime | None = None,
                     tick_minutes: float = 1, run: Runner = subprocess.run) -> dict[str, Any]:
    """The ``/heartbeats`` response: ``{app, generated_at, services}``.

    ``entries`` are orbit-jobs schedule entries and services (``kind`` defaults to
    ``scheduled`` for an Entry and ``loop`` for a Service; ``where`` to the ``where``
    given here). ``units`` are systemd units and timers, ``extra`` callables returning one
    service dict each (missing keys become null). ``tick_minutes`` is how often the
    app's scheduler ticks (what a TICK entry expects)."""
    at = now or datetime.now(UTC)
    services = _from_orbit(list(entries), now, tick_minutes, where)
    services += [_from_unit(u, at, run) for u in units]
    services += [_from_check(check) for check in extra]
    return {"app": app, "generated_at": _z(at), "services": services}


def heartbeats_router(app_name: str, *, entries: Iterable[Entry | Service] = (),
                      units: Iterable[SystemdUnit | SystemdTimer | str] = (),
                      extra: Iterable[Check] = (),
                      auth: Callable[..., Any], path: str = "/heartbeats",
                      where: str | None = None, tick_minutes: float = 1) -> Any:
    """A FastAPI ``APIRouter`` serving ``GET {path}`` (read-only) behind ``auth``, a
    FastAPI dependency (the app's own admin gate: it raises to refuse). Mount it
    under the app's prefix with ``app.include_router(router, prefix=...)``."""
    from fastapi import APIRouter, Depends

    entries, units, extra = tuple(entries), tuple(units), tuple(extra)
    router = APIRouter()

    @router.get(path, dependencies=[Depends(auth)])
    def heartbeats() -> dict[str, Any]:  # sync: FastAPI runs it off the event loop
        return build_heartbeats(app_name, entries=entries, units=units, extra=extra,
                                where=where, tick_minutes=tick_minutes)

    return router
