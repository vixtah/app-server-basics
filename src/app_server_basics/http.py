"""Polite calls to outside APIs: what Reserve's booking providers and CardScanner's news
sources each built for themselves.

An outside host that answers 429 is left alone until the time it asked for; a host that
keeps refusing the server (403, or a bot wall instead of data) is paused for longer and
longer; no request is sent to a host during its cooldown or pause; and every request
sent is counted per host, for a status page. The state lives in the app's SQLite
database, so a restart, a deploy or another process sees the same pauses.

The pieces:

- :class:`Policy`: the backoff rules (which statuses cool a host down and for how long,
  which statuses count as refusals and how the pause grows). Each app keeps its own
  numbers.
- :class:`Hosts`: the per-host state and counters, persisted through the app's own
  connection factory (``connect()`` returning a context manager that yields a
  ``sqlite3.Connection`` and commits on a clean exit). The app creates the tables with
  :func:`create_tables` in its schema setup. ``key`` maps a host name to the bucket its
  state is kept under (CardScanner keeps every reddit.com host as one source).
- :class:`PoliteClient`: one request at a time through any HTTP library. It needs a
  ``send(method, url, **kwargs)`` function returning a response with ``status_code``
  and ``headers`` (``httpx.request``, an ``httpx.Client().request``, a curl_cffi
  ``Session.request``); nothing here imports an HTTP library.
- :meth:`Hosts.stats`: each host's last 24 hours, in the shape of Reserve's
  ``/api/reserve/sources``.

Errors never carry a query string or a token: a refused request is remembered as
``METHOD /path``, a transport failure is counted as ``timeout`` or ``transport`` (never
its message), and :func:`describe_error` gives a redacted one-line error for logs and
status pages (``orbit_jobs.redact``).
"""

from __future__ import annotations

import atexit
import calendar
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from orbit_jobs.redact import redact

__all__ = [
    "ERROR_CHARS",
    "STATS_FIELDS",
    "STATUSES",
    "Blocked",
    "CoolingDown",
    "HostState",
    "HostUnavailable",
    "Hosts",
    "PoliteClient",
    "Policy",
    "create_tables",
    "describe_error",
    "error_category",
    "host_of",
    "merge_state",
    "read_state",
    "redact_url",
    "request_label",
    "retry_after",
]

log = logging.getLogger(__name__)

#: The keys of each :meth:`Hosts.stats` entry, in order.
STATS_FIELDS = (
    "id", "host", "requests_24h", "ok_24h", "rate_limited_24h", "rejected_24h", "errors_24h",
    "last_status_code", "last_ok_at", "cooldown_until", "status", "note",
)
#: A host's status, first match wins: ``blocked`` (paused now, or mostly 401/403 in the
#: window), ``rate_limited`` (cooling down now, or a 429 in the last hour), ``failing``
#: (mostly 5xx or transport errors), ``idle`` (no requests in the window), else ``ok``.
STATUSES = ("ok", "rate_limited", "blocked", "failing", "idle")
ERROR_CHARS = 300


# --- policy --------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """The backoff rules. The defaults are Reserve's.

    **Cooldowns.** A response whose status is in ``cooldown_statuses`` stops the host for
    its ``Retry-After`` (seconds or an HTTP date), or ``cooldown_default`` without one,
    kept within ``cooldown_min`` and ``cooldown_max``. ``cooldown_until_utc_midnight``
    makes it last at least until the next UTC midnight (a daily quota).
    ``retry_after_any_status`` also honours a positive ``Retry-After`` on any other
    status (a 503 asking for an hour).

    **Blocks.** A response whose status is in ``block_statuses`` (or that the caller
    says is a refusal, such as an HTML page from a JSON endpoint) is a refusal. From the
    ``block_after``-th refusal in a row the host is paused for ``block_first`` seconds,
    doubling per further refusal up to ``block_cap``. The count is remembered for
    ``block_memory`` seconds after the pause ends, so expiry or a restart does not start
    over. Only a success on the request that was refused (same method and path) clears
    it.
    """

    cooldown_statuses: frozenset[int] = frozenset({429})
    cooldown_default: float = 300
    cooldown_min: float = 60
    cooldown_max: float | None = 6 * 3600
    cooldown_until_utc_midnight: bool = False
    retry_after_any_status: bool = False
    block_statuses: frozenset[int] = frozenset({403})
    block_after: int = 2
    block_first: float = 3600
    block_cap: float = 86400
    block_memory: float = 30 * 86400
    #: How long counted requests are kept (the status page reads 24 hours of them).
    keep_attempts: float = 7 * 86400

    def cooldown_until(self, status: int, retry_after_seconds: float | None,
                       now: float) -> float | None:
        """When a host that answered ``status`` may be asked again, or None."""
        if status in self.cooldown_statuses:
            seconds = self._clamp(self.cooldown_default if retry_after_seconds is None
                                  else retry_after_seconds)
            until = now + seconds
            if self.cooldown_until_utc_midnight:
                until = max(until, _next_utc_midnight(now))
            return until
        if self.retry_after_any_status and retry_after_seconds and retry_after_seconds > 0:
            return now + self._clamp(retry_after_seconds)
        return None

    def block_seconds(self, count: int) -> float:
        """The pause after the ``count``-th refusal in a row (0 before ``block_after``)."""
        if count < self.block_after:
            return 0
        return min(self.block_first * 2 ** min(count - self.block_after, 8), self.block_cap)

    def _clamp(self, seconds: float) -> float:
        seconds = max(self.cooldown_min, seconds)
        return seconds if self.cooldown_max is None else min(seconds, self.cooldown_max)


def _next_utc_midnight(now: float) -> float:
    tomorrow = datetime.fromtimestamp(now, UTC).date() + timedelta(days=1)
    return calendar.timegm(tomorrow.timetuple())


def retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds a ``Retry-After`` header asks for: delay-seconds (``"120"``) or an HTTP
    date. None when missing, malformed, negative or a date already past; ``"0"`` is 0."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(int(value))
    now = time.time() if now is None else now
    try:
        date = parsedate_to_datetime(value)
    except (ValueError, TypeError, IndexError, OverflowError):
        return None
    if date is None:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    seconds = date.timestamp() - now
    return seconds if seconds > 0 else None


# --- redaction -----------------------------------------------------------------------


def host_of(url_or_host: str) -> str:
    """The lowercase host name of a URL (or of a bare host name), without a trailing dot."""
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    return (host or "").lower().rstrip(".")


def request_label(method: str, url: str) -> str:
    """``"POST /api/search"``: what a refusal is remembered by. Never the query string,
    which can carry keys."""
    return f"{method.upper()} {urlsplit(url).path or '/'}"


def redact_url(url: str) -> str:
    """``scheme://host[:port]/path``: no credentials, query string or fragment."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return f"{parts.scheme}://{netloc}{parts.path}" if parts.scheme else parts.path


def error_category(exc: BaseException) -> str:
    """``timeout`` or ``transport``: how a request that got no answer is counted (never
    its message, which can carry a URL)."""
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" if "timed out" in text or "timeout" in text else "transport"


def describe_error(exc: BaseException | str, limit: int = ERROR_CHARS) -> str:
    """One redacted line for logs and status pages: the first line of the error with URL
    query strings, credentials and token-like values removed (``orbit_jobs.redact``)."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    return redact(first)[:limit]


# --- errors --------------------------------------------------------------------------


class HostUnavailable(RuntimeError):
    """No request was sent: the host is cooling down or paused. ``until`` is when it may
    be asked again (epoch seconds), ``status_code`` the answer that stopped it."""

    def __init__(self, message: str, *, host: str | None = None, until: float | None = None,
                 status_code: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.host = host
        self.until = until
        self.status_code = status_code
        self.reason = reason


class CoolingDown(HostUnavailable):
    """The host asked us to wait (429, ``Retry-After``)."""


class Blocked(HostUnavailable):
    """The host refuses this server (403, or a bot wall instead of data)."""


# --- persisted state -----------------------------------------------------------------


@dataclass(frozen=True)
class HostState:
    """One host's cooldown and refusal record."""

    host: str
    cooldown_until: float | None = None
    cooldown_status: int | None = None
    block_count: int = 0
    block_until: float | None = None
    block_request: str | None = None
    block_reason: str | None = None
    #: When the refusal count is forgotten (the pause's end plus ``block_memory``).
    block_forget_at: float | None = None

    def cooling(self, now: float) -> float | None:
        """The end of the cooldown, while it lasts."""
        return self.cooldown_until if self.cooldown_until and self.cooldown_until > now \
            else None

    def paused(self, now: float) -> float | None:
        """The end of the pause, while it lasts."""
        return self.block_until if self.block_until and self.block_until > now else None

    def remembers_block(self, now: float) -> bool:
        return bool(self.block_count and self.block_forget_at and self.block_forget_at > now)


_COLUMNS = ("host", "cooldown_until", "cooldown_status", "block_count", "block_until",
            "block_request", "block_reason", "block_forget_at")


def create_tables(conn: sqlite3.Connection) -> None:
    """``http_hosts`` (one row per host with a cooldown or a remembered refusal) and
    ``http_attempts`` (one row per request sent: host, epoch seconds, status or NULL, and
    ``timeout``/``transport`` for a request that got no answer). Idempotent."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS http_hosts ("
        "host TEXT PRIMARY KEY, cooldown_until REAL, cooldown_status INTEGER, "
        "block_count INTEGER NOT NULL DEFAULT 0, block_until REAL, block_request TEXT, "
        "block_reason TEXT, block_forget_at REAL, updated_at REAL NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS http_attempts "
        "(host TEXT NOT NULL, at REAL NOT NULL, status INTEGER, error TEXT)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS http_attempts_by_host ON http_attempts(host, at)")


def _state(row) -> HostState:
    return HostState(*(row[i] for i in range(len(_COLUMNS))))


def _live(state: HostState, now: float) -> HostState | None:
    """``state`` without what has expired; None when nothing is left."""
    cooling = state.cooling(now) is not None
    block = state.remembers_block(now)
    if not cooling and not block:
        return None
    if not cooling:
        state = replace(state, cooldown_until=None, cooldown_status=None)
    if not block:
        state = replace(state, block_count=0, block_until=None, block_request=None,
                        block_reason=None, block_forget_at=None)
    return state


def read_state(conn: sqlite3.Connection, host: str, now: float) -> HostState | None:
    """``host``'s current state (expired parts dropped), or None."""
    row = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM http_hosts WHERE host=?",
                       (host,)).fetchone()
    return _live(_state(row), now) if row else None


def _write(conn: sqlite3.Connection, state: HostState, now: float) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO http_hosts ({', '.join(_COLUMNS)}, updated_at) "
        f"VALUES ({', '.join('?' * (len(_COLUMNS) + 1))})",
        (*(getattr(state, c) for c in _COLUMNS), now))
    # Pruned on write: reads are hot (every request checks its host).
    conn.execute("DELETE FROM http_hosts WHERE COALESCE(cooldown_until, 0) <= ? "
                 "AND COALESCE(block_forget_at, 0) <= ?", (now, now))


def merge_state(conn: sqlite3.Connection, state: HostState, now: float | None = None) -> None:
    """Fold ``state`` into what is stored for its host, keeping the later cooldown and the
    larger refusal count (for importing an app's older records). The caller commits."""
    now = time.time() if now is None else now
    incoming = _live(state, now)
    if incoming is None:
        return
    current = read_state(conn, state.host, now)
    if current is not None:
        if (current.cooling(now) or 0) > (incoming.cooling(now) or 0):
            incoming = replace(incoming, cooldown_until=current.cooldown_until,
                               cooldown_status=current.cooldown_status)
        if current.block_count >= incoming.block_count and current.remembers_block(now):
            incoming = replace(
                incoming, block_count=current.block_count, block_until=current.block_until,
                block_request=current.block_request, block_reason=current.block_reason,
                block_forget_at=current.block_forget_at)
    _write(conn, incoming, now)


# --- hosts ---------------------------------------------------------------------------


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if epoch else None


class Hosts:
    """Per-host cooldowns, refusals and request counts, persisted in the app's SQLite.

    ``connect``: the app's connection factory (a context manager yielding a connection
    that commits on a clean exit; read-only connections are fine for reading).
    ``key``: host name to the bucket its state and counts are kept under (default: the
    host itself). ``clock``: epoch seconds (default ``time.time``, read on every call).
    ``scope``: names the database counted requests belong to; a request buffered under
    one scope is dropped if the app has moved to another before it is written (tests
    that repoint the database). Counted requests are written in batches (``batch``
    requests or ``flush_seconds``, before :meth:`stats` reads them, and at exit).
    """

    def __init__(self, connect: Callable[[], AbstractContextManager[sqlite3.Connection]],
                 policy: Policy | None = None, *, key: Callable[[str], str] | None = None,
                 clock: Callable[[], float] | None = None,
                 scope: Callable[[], Any] | None = None, batch: int = 200,
                 flush_seconds: float = 30):
        self.connect = connect
        self.policy = policy or Policy()
        self._key = key
        self._clock = clock
        self._scope = scope
        self.batch = batch
        self.flush_seconds = flush_seconds
        self._lock = threading.Lock()
        self._buffer: list[tuple] = []
        self._flushed = time.monotonic()
        self._pruned = 0.0
        atexit.register(self.flush)

    # -- keys and time

    def now(self) -> float:
        return self._clock() if self._clock else time.time()

    def key(self, url_or_host: str) -> str:
        """The bucket ``url_or_host`` is kept under."""
        host = host_of(url_or_host)
        return self._key(host) if self._key else host

    # -- state

    def state(self, url_or_host: str) -> HostState | None:
        """The host's current cooldown and refusal record, or None."""
        with self.connect() as conn:
            return read_state(conn, self.key(url_or_host), self.now())

    def check(self, method: str, url: str) -> HostState | None:
        """Raise :class:`CoolingDown` or :class:`Blocked` if the host must not be asked
        now; else return its state (pass it to :meth:`accept` after a success)."""
        host, now = self.key(url), self.now()
        state = self.state(url)
        if state is None:
            return None
        if until := state.cooling(now):
            raise CoolingDown(
                f"{host} rate limited us; retrying in {int(until - now) // 60 + 1} min",
                host=host, until=until, status_code=state.cooldown_status)
        if until := state.paused(now):
            raise Blocked(
                f"{host} refuses this server; retrying in {int(until - now) // 60 + 1} min",
                host=host, until=until, reason=state.block_reason)
        return state

    def cooldown(self, url: str, status: int, headers: Mapping[str, str] | None = None
                 ) -> float | None:
        """Record the cooldown ``status`` (and its ``Retry-After``) calls for under the
        policy; returns its end, or None when the answer asks for none."""
        policy = self.policy
        if status not in policy.cooldown_statuses and not policy.retry_after_any_status:
            return None
        now = self.now()
        header = _header(headers, "Retry-After")
        until = policy.cooldown_until(status, retry_after(header, now), now)
        if until is None:
            return None
        host = self.key(url)
        with self.connect() as conn:
            _begin(conn)
            current = read_state(conn, host, now) or HostState(host)
            _write(conn, replace(current, cooldown_until=until, cooldown_status=status), now)
        return until

    def refuse(self, method: str, url: str, reason: str) -> Blocked:
        """Record one refusal of ``method url``; returns the :class:`Blocked` to raise."""
        host, now = self.key(url), self.now()
        with self.connect() as conn:
            _begin(conn)
            current = read_state(conn, host, now) or HostState(host)
            count = current.block_count + 1
            seconds = self.policy.block_seconds(count)
            until = now + seconds
            _write(conn, replace(
                current, block_count=count, block_until=until,
                block_request=request_label(method, url), block_reason=reason,
                block_forget_at=until + self.policy.block_memory), now)
        note = f"; paused {int(seconds // 3600)} h" if seconds else ""
        return Blocked(f"{host} refused this server ({reason}){note}", host=host,
                       until=until if seconds else None, reason=reason)

    def accept(self, method: str, url: str, state: HostState | None) -> None:
        """A success: clear the host's refusal count if ``method url`` is the request that
        was refused (``state`` as :meth:`check` returned it before the request). Another
        request succeeding (a page that loads while its search is refused) changes
        nothing."""
        if not state or not state.block_count or \
                state.block_request != request_label(method, url):
            return
        now = self.now()
        with self.connect() as conn:
            _begin(conn)
            current = read_state(conn, state.host, now)
            if current and current.block_request == state.block_request:
                _write(conn, replace(current, block_count=0, block_until=None,
                                     block_request=None, block_reason=None,
                                     block_forget_at=None), now)

    def states(self, now: float | None = None) -> list[HostState]:
        """Every host with a cooldown or a remembered refusal."""
        now = self.now() if now is None else now
        with self.connect() as conn:
            rows = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM http_hosts").fetchall()
        return [s for s in (_live(_state(r), now) for r in rows) if s is not None]

    # -- counting

    def record(self, url: str, status: int | None = None, error: str | None = None) -> None:
        """Count one request sent: its HTTP status, or ``error`` (:func:`error_category`)
        when it got no answer. Requests a cooldown or pause skipped are not counted."""
        host = self.key(url)
        if not host:
            return
        row = (self._scope() if self._scope else None, host, self.now(),
               status if type(status) is int else None, error)
        with self._lock:
            self._buffer.append(row)
            due = (len(self._buffer) >= self.batch
                   or time.monotonic() - self._flushed > self.flush_seconds)
        if due:
            self.flush()

    def flush(self) -> None:
        """Write the buffered requests (pruning old ones, hourly). Never raises."""
        with self._lock:
            rows, self._buffer = self._buffer, []
            self._flushed = time.monotonic()
        scope = self._scope() if self._scope else None
        rows = [r[1:] for r in rows if r[0] == scope]
        if not rows:
            return
        try:
            with self.connect() as conn:
                conn.executemany("INSERT INTO http_attempts VALUES (?,?,?,?)", rows)
                if time.time() - self._pruned > 3600:
                    self._pruned = time.time()
                    conn.execute("DELETE FROM http_attempts WHERE at < ?",
                                 (self.now() - self.policy.keep_attempts,))
        except Exception:  # noqa: BLE001 — counting must never break a request
            log.exception("writing HTTP request counts failed")

    def stats(self, now: float | None = None, *, window: float = 86400,
              recent: float = 3600) -> list[dict]:
        """One entry per host with requests kept or a cooldown/remembered refusal now,
        sorted by host, with the keys of :data:`STATS_FIELDS` (``id`` is the host;
        timestamps are ISO UTC; ``cooldown_until`` is the later of the cooldown and the
        pause). Counts cover ``window`` seconds; a 429 within ``recent`` seconds still
        reads ``rate_limited``."""
        self.flush()
        now = self.now() if now is None else now
        since, recent_since = now - window, now - recent
        with self.connect() as conn:
            rows = {r[0]: r for r in conn.execute(
                "SELECT host, SUM(at>=?), "
                "SUM(at>=? AND status BETWEEN 200 AND 399), "
                "SUM(at>=? AND status=429), SUM(at>=? AND status=429), "
                "SUM(at>=? AND status IN (401,403)), "
                "SUM(at>=? AND (status>=500 OR status IS NULL)), "
                "MAX(CASE WHEN status BETWEEN 200 AND 399 THEN at END) "
                "FROM http_attempts GROUP BY host",
                (since, since, since, recent_since, since, since))}
            last = {r[0]: r[1] for r in conn.execute(
                "SELECT a.host, a.status FROM http_attempts a JOIN (SELECT host, "
                "MAX(at) AS at FROM http_attempts GROUP BY host) m "
                "ON a.host=m.host AND a.at=m.at")}
        states = {s.host: s for s in self.states(now)}
        result = []
        for host in sorted(set(rows) | set(states)):
            r = rows.get(host)
            requests, ok, limited, limited_recent, rejected, errors = (
                (r[i] or 0 for i in range(1, 7)) if r else (0,) * 6)
            state = states.get(host) or HostState(host)
            cooling, paused = state.cooling(now), state.paused(now)
            if paused or (requests and rejected * 2 > requests):
                status = "blocked"
            elif cooling or limited_recent:
                status = "rate_limited"
            elif requests and errors * 2 > requests:
                status = "failing"
            else:
                status = "ok" if requests else "idle"
            result.append({
                "id": host, "host": host, "requests_24h": requests, "ok_24h": ok,
                "rate_limited_24h": limited, "rejected_24h": rejected, "errors_24h": errors,
                "last_status_code": last.get(host), "last_ok_at": _iso(r[7] if r else None),
                "cooldown_until": _iso(max(cooling or 0, paused or 0) or None),
                "status": status,
                "note": _note(status, ok, requests, paused, cooling, state.block_reason),
            })
        return result


def _note(status, ok, requests, paused, cooling, reason) -> str:
    if status == "blocked":
        why = reason or "HTTP 401/403"
        return (f"refuses this server ({why}); paused until {_iso(paused)}" if paused
                else f"mostly {why}")
    if status == "rate_limited":
        return "answers 429 under load" + (
            f"; cooling down until {_iso(cooling)}" if cooling else "")
    if status == "failing":
        return "mostly server errors or timeouts"
    if status == "idle":
        return "no requests in 24 hours"
    return f"{ok} of {requests} requests answered in 24 hours"


def _begin(conn: sqlite3.Connection) -> None:
    """Read-modify-write under one write lock, so two processes never lose a count."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    value = headers.get(name)
    if value is None and isinstance(headers, dict):
        # A plain dict is case-sensitive; HTTP libraries' header maps are not.
        lowered = name.lower()
        value = next((v for k, v in headers.items() if k.lower() == lowered), None)
    return value


# --- the client ----------------------------------------------------------------------


class Response(Protocol):
    status_code: int
    headers: Mapping[str, str]


def _default_backoff(attempt: int) -> float:
    return 2 ** attempt + random.uniform(0, 0.3)


@dataclass
class PoliteClient:
    """Requests through ``send`` that respect :class:`Hosts`.

    Each :meth:`request` checks the host first (raising :class:`CoolingDown` or
    :class:`Blocked` without sending anything), counts every attempt, records the
    cooldown a 429 calls for (never retrying it), retries 5xx answers up to ``retries``
    times (sleeping ``backoff(attempt)`` seconds), raises :class:`Blocked` for a refusal
    status and for a response ``refused`` names a reason for, and clears the host's
    refusal count when the refused request succeeds. Any other response is returned as
    is, for the caller's ``raise_for_status``. ``pace(url)``, if given, is a context
    manager entered around each attempt (a shared throttle).
    """

    hosts: Hosts
    send: Callable[..., Any]
    retries: int = 0
    backoff: Callable[[int], float] = field(default=_default_backoff)
    pace: Callable[[str], AbstractContextManager] | None = None

    def request(self, method: str, url: str, *,
                refused: Callable[[Any], str | None] | None = None, **kwargs) -> Any:
        hosts, policy = self.hosts, self.hosts.policy
        state = hosts.check(method, url)
        for attempt in range(self.retries + 1):
            with self.pace(url) if self.pace else nullcontext():
                try:
                    response = self.send(method, url, **kwargs)
                except Exception as exc:
                    hosts.record(url, error=error_category(exc))
                    raise
            status = response.status_code
            hosts.record(url, status)
            # Retrying a 429 at once is what extends a ban: stop the host instead.
            if hosts.cooldown(url, status, response.headers) is not None:
                break
            if status < 500:
                break
            if attempt < self.retries:
                time.sleep(self.backoff(attempt))
        if status in policy.block_statuses:
            raise hosts.refuse(method, url, f"HTTP {status}")
        if status < 400:
            if refused is not None and (reason := refused(response)):
                raise hosts.refuse(method, url, reason)
            hosts.accept(method, url, state)
        return response
