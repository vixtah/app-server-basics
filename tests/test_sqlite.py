"""app_server_basics.sqlite: connections, the worktree guard, columns, migrations, leases."""

import sqlite3
from datetime import UTC, datetime

import pytest

from app_server_basics import sqlite as db


@pytest.fixture
def checkouts(tmp_path):
    """A main checkout (``.git`` directory) and a linked worktree (``.git`` file)."""
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / "pkg").mkdir()
    worktree = tmp_path / "wt"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    return main / "pkg" / "store.py", worktree / "pkg" / "store.py"


def test_connect_sets_wal_busy_timeout_rows_and_makes_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(db.BUSY_TIMEOUT_ENV, "1234")
    path = tmp_path / "deep" / "app.db"
    conn, guarded = db.connect(path)
    assert not guarded and path.parent.is_dir()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
    conn, _ = db.connect(path, foreign_keys=True)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("raw,expected", [(None, 30_000), ("", 30_000), ("x", 30_000),
                                          ("500", 500)])
def test_busy_timeout_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(db.BUSY_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(db.BUSY_TIMEOUT_ENV, raw)
    assert db.busy_timeout_ms() == expected


def test_worktree_detection(checkouts, tmp_path):
    main, worktree = checkouts
    assert not db.in_worktree(main)
    assert db.in_worktree(worktree)
    assert not db.in_worktree(None)
    assert not db.in_worktree(tmp_path / "nowhere" / "x.py")  # a deployed copy, no .git


def test_live_file_opened_from_a_worktree_is_read_only(checkouts, tmp_path, monkeypatch):
    monkeypatch.delenv(db.ALLOW_LIVE_WRITES_ENV, raising=False)
    main, worktree = checkouts
    live = tmp_path / "live.db"
    conn, _ = db.connect(live)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()

    assert not db.guard_active_for(live, live, checkout=main)
    assert not db.guard_active_for(tmp_path / "copy.db", live, checkout=worktree)
    assert db.guard_active_for(live, live, checkout=worktree)

    conn, guarded = db.connect(live, live_path=live, checkout=worktree)
    assert guarded
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    for sql in ("INSERT INTO t VALUES (1)", "CREATE TABLE u (y)", "DROP TABLE t",
                "ALTER TABLE t ADD COLUMN z"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)

    monkeypatch.setenv(db.ALLOW_LIVE_WRITES_ENV, "1")
    conn, guarded = db.connect(live, live_path=live, checkout=worktree)
    assert not guarded
    conn.execute("INSERT INTO t VALUES (1)")


def test_ensure_columns_adds_only_missing_ones():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT)")
    assert db.ensure_columns(conn, "t", {"a": "TEXT", "b": "INTEGER NOT NULL DEFAULT 0"}) \
        == ["b"]
    assert db.ensure_columns(conn, "t", {"b": "INTEGER NOT NULL DEFAULT 0"}) == []
    assert [r[1] for r in conn.execute("PRAGMA table_info(t)")] == ["a", "b"]
    with pytest.raises(ValueError):
        db.ensure_columns(conn, "t; DROP TABLE t", {"c": "TEXT"})


def test_migrations_run_once_in_order_per_module():
    conn = sqlite3.connect(":memory:")
    ran = []
    steps = [(1, lambda c: ran.append(1)), (2, lambda c: ran.append(2))]
    assert db.apply_migrations(conn, "app", steps) == [1, 2]
    assert db.apply_migrations(conn, "app", [*steps, (3, lambda c: ran.append(3))]) == [3]
    assert db.apply_migrations(conn, "other", steps) == [1, 2]
    assert ran == [1, 2, 3, 1, 2]
    with pytest.raises(ValueError):
        db.apply_migrations(conn, "bad", [(2, lambda c: None), (1, lambda c: None)])
    with pytest.raises(ValueError):
        db.apply_migrations(conn, "bad", [(1, lambda c: None), (1, lambda c: None)])


def test_leases_are_exclusive_and_expire():
    conn = sqlite3.connect(":memory:")
    db.create_lease_table(conn, "app_leases")
    assert db.acquire_lease(conn, "warm", "a", 60, table="app_leases", now=1000)
    assert not db.acquire_lease(conn, "warm", "b", 60, table="app_leases", now=1030)
    assert db.acquire_lease(conn, "other", "b", 60, table="app_leases", now=1030)
    assert db.acquire_lease(conn, "warm", "b", 60, table="app_leases", now=1061)  # a crashed
    db.release_lease(conn, "warm", "a", table="app_leases")  # no longer a's: kept
    assert not db.acquire_lease(conn, "warm", "c", table="app_leases", now=1070)
    db.release_lease(conn, "warm", "b", table="app_leases")
    assert db.acquire_lease(conn, "warm", "c", table="app_leases", now=1070)


def test_simulated_clock():
    fixed = datetime(2026, 10, 13, 12, tzinfo=UTC)
    with db.simulated_clock(lambda: fixed):
        assert db.now_iso() == "2026-10-13T12:00:00+00:00"
    assert db.now_iso() != "2026-10-13T12:00:00+00:00"
