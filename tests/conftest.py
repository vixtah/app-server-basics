"""Every test gets its own orbit-jobs SQLite file and default hooks."""

from __future__ import annotations

import pytest
from orbit_jobs import config


@pytest.fixture(autouse=True)
def orbit_jobs_db(tmp_path, monkeypatch):
    saved = config.snapshot()
    config.restore(config.Settings())
    monkeypatch.setenv("ORBIT_JOBS_DB", str(tmp_path / "jobs.db"))
    yield tmp_path / "jobs.db"
    config.restore(saved)
