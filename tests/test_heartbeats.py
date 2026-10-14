"""app_server_basics.heartbeats: the contract's shape, systemd parsing (systemctl
faked), merging the three sources, truncation and the route's auth gate. No network,
no real systemctl."""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from orbit_jobs.schedules import TICK, Entry, Every, Service, record_run, run_due

from app_server_basics import heartbeats as hb
from app_server_basics.heartbeats import (
    FIELDS,
    SystemdUnit,
    build_heartbeats,
    heartbeats_router,
    systemd_service,
)

NOW = datetime(2026, 10, 13, 22, 0, 0, tzinfo=UTC)
Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class FakeSystemctl:
    """Stands in for subprocess.run: answers ``systemctl show <unit>`` from a table."""

    def __init__(self, units: dict[str, str], *, reject_timestamp_flag: bool = False):
        self.units, self.reject = units, reject_timestamp_flag
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        assert cmd[:2] == ["systemctl", "show"] and kwargs.get("timeout")
        if self.reject and "--timestamp=utc" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "unrecognized option '--timestamp=utc'")
        body = self.units.get(cmd[2], "LoadState=not-found\nActiveState=inactive\nResult=success\n"
                                      "ExecMainStatus=0\nActiveEnterTimestamp=\nNRestarts=0\n")
        return subprocess.CompletedProcess(cmd, 0, body, "")


ACTIVE = """LoadState=loaded
ActiveState=active
Result=success
ExecMainStatus=0
ActiveEnterTimestamp=Mon 2026-10-12 08:00:00 UTC
NRestarts=0
ExecMainStartTimestamp=Mon 2026-10-12 08:00:00 UTC
ExecMainExitTimestamp=
"""
FAILED = """LoadState=loaded
ActiveState=failed
Result=exit-code
ExecMainStatus=3
ActiveEnterTimestamp=Mon 2026-10-12 08:00:00 UTC
NRestarts=5
ExecMainStartTimestamp=Tue 2026-10-13 21:50:00 UTC
ExecMainExitTimestamp=Tue 2026-10-13 21:50:02 UTC
"""
TIMER_DONE = """LoadState=loaded
ActiveState=inactive
Result=success
ExecMainStatus=0
ActiveEnterTimestamp=
NRestarts=0
ExecMainStartTimestamp=Tue 2026-10-13 21:45:00 UTC
ExecMainExitTimestamp=Tue 2026-10-13 21:45:01 UTC
"""
RESTARTING = ACTIVE.replace("ActiveState=active", "ActiveState=activating").replace(
    "Result=success", "Result=exit-code").replace("NRestarts=0", "NRestarts=2")
STARTING = ACTIVE.replace("ActiveState=active", "ActiveState=activating")


def assert_contract(service: dict) -> None:
    assert list(service) == list(FIELDS)
    assert service["last_status"] in {"ok", "failed", "running", "skipped", "unknown"}
    for key in ("last_run_at", "last_ok_at"):
        assert service[key] is None or Z.match(service[key]), service[key]
    assert service["last_error"] is None or len(service["last_error"]) <= 300


# ── systemd ───────────────────────────────────────────────────────────


def test_active_unit_is_ok_now():
    s = systemd_service(SystemdUnit("rift-lab.service", name="Training lab"), now=NOW,
                        run=FakeSystemctl({"rift-lab.service": ACTIVE}))
    assert_contract(s)
    assert s == {
        "id": "rift-lab.service", "name": "Training lab", "kind": "systemd", "where": "VPS",
        "expected_every_minutes": 5, "last_run_at": "2026-10-12T08:00:00Z",
        "last_ok_at": "2026-10-13T22:00:00Z", "last_status": "ok", "last_error": None,
        "last_duration_ms": None, "runs_24h": None, "failures_24h": None,
    }


def test_failed_unit_reports_result_exit_status_and_restarts():
    s = systemd_service("reserve.service", now=NOW, run=FakeSystemctl({"reserve.service": FAILED}))
    assert_contract(s)
    assert s["last_status"] == "failed" and s["last_ok_at"] is None
    assert s["last_error"] == "failed: result exit-code, exit status 3, 5 restarts"
    assert s["last_run_at"] == "2026-10-13T21:50:00Z" and s["last_duration_ms"] == 2000
    assert s["name"] == s["id"] == "reserve.service"


def test_unit_restarting_after_a_crash_is_failed_and_starting_is_running():
    run = FakeSystemctl({"a.service": RESTARTING, "b.service": STARTING})
    assert systemd_service("a.service", now=NOW, run=run)["last_status"] == "failed"
    assert systemd_service("b.service", now=NOW, run=run)["last_status"] == "running"


def test_timer_unit_that_exited_cleanly_is_ok_at_its_exit():
    unit = SystemdUnit("backup.service", expected_every_minutes=60, id="backup")
    s = systemd_service(unit, now=NOW, run=FakeSystemctl({"backup.service": TIMER_DONE}))
    assert (s["id"], s["last_status"], s["last_ok_at"], s["last_duration_ms"],
            s["expected_every_minutes"]) == ("backup", "ok", "2026-10-13T21:45:01Z", 1000, 60)


def test_missing_unit_is_unknown():
    s = systemd_service("ghost.service", now=NOW, run=FakeSystemctl({}))
    assert_contract(s)
    assert (s["last_status"], s["last_error"], s["kind"]) == ("unknown", "unit not found", "systemd")


def test_no_systemctl_is_unknown_never_an_exception(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("systemctl")

    s = systemd_service("x.service", run=missing)
    assert (s["last_status"], s["last_error"]) == ("unknown", "systemctl not available")
    # The real runner on a machine without systemctl (this Mac, or a container).
    monkeypatch.setattr(hb.shutil, "which", lambda name: None)
    s = systemd_service("x.service")
    assert (s["last_status"], s["last_error"]) == ("unknown", "systemctl not available")


def test_systemctl_timeout_or_error_is_unknown():
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    assert systemd_service("x.service", run=slow)["last_status"] == "unknown"

    def broken(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus " + "x" * 400)

    s = systemd_service("x.service", run=broken)
    assert s["last_status"] == "unknown" and len(s["last_error"]) == 300
    assert s["last_error"].startswith("systemctl show failed: Failed to connect to bus")


def test_older_systemd_without_the_timestamp_flag():
    run = FakeSystemctl({"a.service": ACTIVE}, reject_timestamp_flag=True)
    s = systemd_service("a.service", now=NOW, run=run)
    assert s["last_status"] == "ok" and s["last_run_at"] == "2026-10-12T08:00:00Z"
    assert [("--timestamp=utc" in c) for c in run.calls] == [True, False]
    assert all("-p" in c and "ActiveState" in c[c.index("-p") + 1] for c in run.calls)


# ── the report ────────────────────────────────────────────────────────


def test_report_merges_orbit_systemd_and_extra_checks():
    planner = Entry("refresh-planner", Every(5), lambda now: None,
                    title="Venue refresh planner", kind="loop", where="VPS")
    sweep = Entry("sweep", TICK, lambda now: None)
    sweeper = Service("fleet-sweeper", 2, title="Fleet sweeper")
    asyncio.run(run_due([planner, sweep], NOW - timedelta(minutes=1)))
    with pytest.raises(RuntimeError), record_run(sweeper):
        raise RuntimeError("droplet API 503 " + "x" * 500)

    def gpu_queue():
        return {"id": "gpu-queue", "name": "GPU queue", "kind": "worker", "where": "Mac",
                "last_status": "ok", "last_ok_at": "2026-10-13T21:59:00+00:00",
                "payload": {"secret": "never shown"}}

    def broken_check():
        raise ValueError("no database " + "y" * 500)

    report = build_heartbeats(
        "riftbound", entries=[planner, sweep, sweeper], units=["rift-lab.service"],
        extra=[gpu_queue, broken_check, lambda: "not a dict"], where="VPS", now=NOW,
        run=FakeSystemctl({"rift-lab.service": ACTIVE}))

    assert list(report) == ["app", "generated_at", "services"]
    assert report["app"] == "riftbound" and report["generated_at"] == "2026-10-13T22:00:00Z"
    services = report["services"]
    for s in services:
        assert_contract(s)
    assert [s["id"] for s in services] == [
        "refresh-planner", "sweep", "fleet-sweeper", "rift-lab.service", "gpu-queue",
        "broken_check", "<lambda>"]
    by_id = {s["id"]: s for s in services}
    assert by_id["refresh-planner"]["last_status"] == "ok"
    assert by_id["refresh-planner"]["name"] == "Venue refresh planner"
    assert (by_id["refresh-planner"]["kind"], by_id["refresh-planner"]["runs_24h"]) == ("loop", 1)
    # Defaults: an Entry is "scheduled", a Service a "loop", where from the app.
    assert (by_id["sweep"]["kind"], by_id["sweep"]["where"]) == ("scheduled", "VPS")
    assert by_id["sweep"]["expected_every_minutes"] == 1
    fleet = by_id["fleet-sweeper"]
    assert (fleet["kind"], fleet["last_status"], fleet["failures_24h"]) == ("loop", "failed", 1)
    assert len(fleet["last_error"]) == 300
    assert by_id["rift-lab.service"]["last_status"] == "ok"
    assert by_id["gpu-queue"]["last_ok_at"] == "2026-10-13T21:59:00Z"
    assert "payload" not in by_id["gpu-queue"] and by_id["gpu-queue"]["runs_24h"] is None
    assert by_id["broken_check"]["last_status"] == "unknown"
    assert by_id["broken_check"]["last_error"].startswith("ValueError: no database")
    assert len(by_id["broken_check"]["last_error"]) == 300
    assert by_id["<lambda>"]["last_error"] == "check returned str, not a dict"


def test_unreadable_job_database_reports_entries_unknown(monkeypatch):
    def down(*args, **kwargs):
        raise OSError("disk I/O error")

    monkeypatch.setattr(hb, "health", down)
    entries = [Entry("planner", Every(5), lambda now: None, title="Planner"),
               Service("sweeper", 2, where="PC")]
    services = build_heartbeats("reserve", entries=entries, where="VPS", now=NOW)["services"]
    assert [(s["id"], s["name"], s["kind"], s["where"], s["last_status"], s["last_error"])
            for s in services] == [
        ("planner", "Planner", "scheduled", "VPS", "unknown", "OSError: disk I/O error"),
        ("sweeper", "sweeper", "loop", "PC", "unknown", "OSError: disk I/O error"),
    ]


def test_bad_status_from_a_check_becomes_unknown():
    services = build_heartbeats("x", extra=[lambda: {"id": "c", "last_status": "great"}],
                                now=NOW)["services"]
    assert services[0]["last_status"] == "unknown" and services[0]["name"] == "c"


def test_empty_report():
    assert build_heartbeats("cards", now=NOW) == {
        "app": "cards", "generated_at": "2026-10-13T22:00:00Z", "services": []}


def test_importable_without_fastapi(tmp_path):
    """The non-web part loads with FastAPI blocked."""
    import sys

    script = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('fastapi', 'starlette'):\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from app_server_basics.heartbeats import build_heartbeats\n"
        "print(build_heartbeats('x')['services'])\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         timeout=60, env={"ORBIT_JOBS_DB": str(tmp_path / "j.db"),
                                          "PATH": "/usr/bin:/bin"}, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ── the route ─────────────────────────────────────────────────────────


def test_route_serves_the_report_behind_the_apps_auth():
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient

    def admin_only(x_admin: str | None = Header(default=None)):
        if x_admin != "yes":
            raise HTTPException(status_code=403, detail="admin only")

    planner = Entry("planner", Every(5), lambda now: None, kind="loop", where="VPS")
    app = FastAPI()
    app.include_router(
        heartbeats_router("reserve", entries=[planner], units=["reserve.service"],
                          extra=[lambda: {"id": "c", "last_status": "ok"}], auth=admin_only),
        prefix="/api/reserve")
    client = TestClient(app)

    assert client.get("/api/reserve/heartbeats").status_code == 403
    assert client.get("/api/reserve/heartbeats", headers={"x-admin": "no"}).status_code == 403
    res = client.get("/api/reserve/heartbeats", headers={"x-admin": "yes"})
    assert res.status_code == 200
    body = res.json()
    assert body["app"] == "reserve" and Z.match(body["generated_at"])
    assert [s["id"] for s in body["services"]] == ["planner", "reserve.service", "c"]
    for s in body["services"]:
        assert_contract(s)
    assert client.post("/api/reserve/heartbeats", headers={"x-admin": "yes"}).status_code == 405


def test_route_requires_an_auth_dependency():
    with pytest.raises(TypeError):
        heartbeats_router("reserve", entries=[])  # type: ignore[call-arg]
