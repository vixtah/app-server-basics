"""SQLite helpers the apps (Tasks, Reserve, CardScanner) each used to write for themselves.

Each app keeps its own database paths and schemas; this module holds what was the same
in all of them:

- **Opening a connection** with the apps' settings: WAL (readers never wait for the
  writer), a busy timeout (a contended write queues instead of failing with "database is
  locked"; ``AUTOMATIONS_SQLITE_BUSY_TIMEOUT_MS``, default 30 000) and ``sqlite3.Row``
  rows. Foreign keys are off unless asked for, as in the apps.
- **The live-database guard.** Code running from a git worktree (an agent's checkout)
  opens the app's live database file read-only, unless ``AUTOMATIONS_ALLOW_LIVE_DB_WRITES``
  is set. Each app says where its code lives (``checkout=__file__``) and which file is
  live; tests and worktrees point the files elsewhere.
- **Schema changes.** :func:`ensure_columns` adds the columns a live table lacks;
  :func:`apply_migrations` runs numbered migrations once per database.
- **Leases.** :func:`acquire_lease` / :func:`release_lease`: one owner at a time per key
  across processes, recovered when the owner dies (the lease expires).
- **The write clock.** :func:`now_iso` stamps writes in UTC; :func:`simulated_clock`
  replaces it in tests.

Standard library only.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "ALLOW_LIVE_WRITES_ENV",
    "BUSY_TIMEOUT_ENV",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "acquire_lease",
    "apply_migrations",
    "busy_timeout_ms",
    "configure",
    "connect",
    "create_lease_table",
    "ensure_columns",
    "guard_active_for",
    "guarded_connect",
    "in_worktree",
    "now_iso",
    "release_lease",
    "simulated_clock",
]

log = logging.getLogger(__name__)

BUSY_TIMEOUT_ENV = "AUTOMATIONS_SQLITE_BUSY_TIMEOUT_MS"
DEFAULT_BUSY_TIMEOUT_MS = 30_000
ALLOW_LIVE_WRITES_ENV = "AUTOMATIONS_ALLOW_LIVE_DB_WRITES"

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name: str) -> str:
    """A table or column name put into SQL text; code constants only, checked anyway."""
    if not _NAME.match(name):
        raise ValueError(f"not a plain SQL identifier: {name!r}")
    return name


# --- connections ---------------------------------------------------------------------


def busy_timeout_ms() -> int:
    """``AUTOMATIONS_SQLITE_BUSY_TIMEOUT_MS``, or 30 000 when unset or not a number."""
    raw = os.environ.get(BUSY_TIMEOUT_ENV)
    try:
        return int(raw) if raw else DEFAULT_BUSY_TIMEOUT_MS
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS


def configure(conn: sqlite3.Connection, *, foreign_keys: bool = False) -> None:
    """``journal_mode=WAL`` and the busy timeout; ``foreign_keys=ON`` when asked."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms()}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")


@functools.cache
def _worktree(start: Path) -> bool:
    for directory in (start, *start.parents):
        marker = directory / ".git"
        if marker.exists():
            try:
                return marker.is_file() and marker.read_text(
                    errors="ignore").lstrip().startswith("gitdir:")
            except OSError:
                return False
    return False


def in_worktree(checkout: str | os.PathLike | None) -> bool:
    """True when ``checkout`` (any path inside the app's code, usually ``__file__``) is in a
    linked git worktree: the nearest ``.git`` above it is a ``gitdir:`` file. A main
    checkout, a deployed copy without ``.git`` and ``None`` are not."""
    if checkout is None:
        return False
    return _worktree(Path(checkout).resolve())


def guard_active_for(path: str | os.PathLike, live_path: str | os.PathLike | None, *,
                     checkout: str | os.PathLike | None) -> bool:
    """True when :func:`guarded_connect` opens ``path`` read-only: the code at ``checkout``
    runs from a worktree, ``path`` is the live file and ``AUTOMATIONS_ALLOW_LIVE_DB_WRITES``
    is not set."""
    return (live_path is not None and Path(path) == Path(live_path)
            and in_worktree(checkout) and not os.environ.get(ALLOW_LIVE_WRITES_ENV))


# Statements a guarded (read-only) connection refuses; PRAGMA stays allowed so
# ``query_only`` can be set after the authorizer is installed.
_DENIED = frozenset({
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_ATTACH,
})


def guarded_connect(path: str | os.PathLike, *, live_path: str | os.PathLike | None,
                    checkout: str | os.PathLike | None,
                    timeout: float = 5.0) -> tuple[sqlite3.Connection, bool]:
    """Open ``path`` with ``sqlite3.Row`` rows; returns ``(conn, guarded)``. A guarded
    connection (:func:`guard_active_for`) is read-only, and its caller must skip DDL. No
    pragmas are set: :func:`configure`, or use :func:`connect`."""
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if not guard_active_for(path, live_path, checkout=checkout):
        return conn, False
    log.critical("LIVE DB OPENED FROM A WORKTREE (read-only): %s — point the "
                 "AUTOMATIONS_*_DB_PATH variables elsewhere, or set "
                 "%s=1 if intended", path, ALLOW_LIVE_WRITES_ENV)

    def authorizer(action, *_args):
        return sqlite3.SQLITE_DENY if action in _DENIED else sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    conn.execute("PRAGMA query_only=ON")
    return conn, True


def connect(path: str | os.PathLike, *, live_path: str | os.PathLike | None = None,
            checkout: str | os.PathLike | None = None, timeout: float | None = None,
            foreign_keys: bool = False) -> tuple[sqlite3.Connection, bool]:
    """Open ``path`` the way the apps do; returns ``(conn, guarded)``.

    Creates the parent directory (unless guarded), connects with ``sqlite3.Row`` rows,
    then either applies the guard (read-only, busy timeout only) or :func:`configure`.
    ``timeout`` defaults to the busy timeout. Without ``live_path`` nothing is guarded.
    """
    guarded = guard_active_for(path, live_path, checkout=checkout)
    if not guarded:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn, guarded = guarded_connect(
        path, live_path=live_path, checkout=checkout,
        timeout=busy_timeout_ms() / 1000 if timeout is None else timeout)
    if guarded:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms()}")
    else:
        configure(conn, foreign_keys=foreign_keys)
    return conn, guarded


# --- schema changes ------------------------------------------------------------------


def ensure_columns(conn: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> list[str]:
    """Add each ``{column: ddl}`` that ``table`` lacks (``ALTER TABLE ... ADD COLUMN``);
    returns the columns added. ``ddl`` is the type plus constraints, e.g.
    ``"TEXT NOT NULL DEFAULT ''"``. A new column goes in the table's ``CREATE TABLE`` too,
    so fresh and live databases match. Names are code constants, never user input."""
    table = _identifier(table)
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    added = []
    for column, ddl in columns.items():
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {_identifier(column)} {ddl}")
            added.append(column)
    return added


def apply_migrations(
    conn: sqlite3.Connection,
    module: str,
    migrations: Sequence[tuple[int, Callable[[sqlite3.Connection], None]]],
) -> list[int]:
    """Run any ``(version, fn)`` migrations not yet recorded for ``module`` in
    ``schema_migrations``, in order, committing after each; returns the versions run.

    Each ``fn(conn)`` must be idempotent against the fresh-install baseline (guarded
    ALTERs, ``IF NOT EXISTS``). Versions must be unique and increasing, checked before
    anything runs."""
    versions = [v for v, _ in migrations]
    if versions != sorted(set(versions)):
        raise ValueError(
            f"migrations for {module!r} must have unique, increasing versions: {versions}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            module      TEXT NOT NULL,
            version     INTEGER NOT NULL,
            applied_at  TEXT NOT NULL,
            PRIMARY KEY (module, version)
        )
    """)
    applied = {r[0] for r in conn.execute(
        "SELECT version FROM schema_migrations WHERE module = ?", (module,))}
    ran = []
    for version, fn in migrations:
        if version in applied:
            continue
        fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (module, version, applied_at) VALUES (?, ?, ?)",
            (module, version, now_iso()))
        conn.commit()
        ran.append(version)
    return ran


# --- leases --------------------------------------------------------------------------


def create_lease_table(conn: sqlite3.Connection, table: str = "leases") -> None:
    """The lease table: ``(key TEXT PRIMARY KEY, owner TEXT, expires REAL)``, epoch
    seconds."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {_identifier(table)} "
                 "(key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")


def acquire_lease(conn: sqlite3.Connection, key: str, owner: str, seconds: float = 900, *,
                  table: str = "leases", now: float | None = None) -> bool:
    """Take ``key`` for ``owner`` for ``seconds``; False while another owner holds it. An
    expired lease (its owner crashed) is taken over. The caller commits."""
    table = _identifier(table)
    now = time.time() if now is None else now
    conn.execute(f"DELETE FROM {table} WHERE key=? AND expires<?", (key, now))
    cursor = conn.execute(f"INSERT OR IGNORE INTO {table} VALUES (?,?,?)",
                          (key, owner, now + seconds))
    return cursor.rowcount == 1


def release_lease(conn: sqlite3.Connection, key: str, owner: str, *,
                  table: str = "leases") -> None:
    """Give ``key`` back, if ``owner`` still holds it. The caller commits."""
    conn.execute(f"DELETE FROM {_identifier(table)} WHERE key=? AND owner=?", (key, owner))


# --- the write clock -----------------------------------------------------------------


def _real_clock() -> datetime:
    return datetime.now(UTC)


_clock: Callable[[], datetime] = _real_clock


def now_iso() -> str:
    """Current UTC time as an ISO string, the timestamp every write stamps."""
    return _clock().isoformat()


@contextmanager
def simulated_clock(fn: Callable[[], datetime]) -> Iterator[None]:
    """Make :func:`now_iso` follow ``fn`` inside the ``with`` block (tests)."""
    global _clock
    previous = _clock
    _clock = fn
    try:
        yield
    finally:
        _clock = previous
