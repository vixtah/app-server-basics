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
    SystemdTimer,
    SystemdUnit,
    build_heartbeats,
    heartbeats_router,
    systemd_service,
    systemd_timer_service,
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


# ── systemd timers ────────────────────────────────────────────────────

TIMER_ON = """LoadState=loaded
ActiveState=active
UnitFileState=enabled
LastTriggerUSec=Tue 2026-10-13 11:39:36 UTC
NextElapseUSecRealtime=Wed 2026-10-14 11:43:35 UTC
"""
ONESHOT_OK = """LoadState=loaded
ActiveState=inactive
Result=success
ExecMainStatus=0
ActiveEnterTimestamp=
NRestarts=0
ExecMainStartTimestamp=Tue 2026-10-13 11:39:36 UTC
ExecMainExitTimestamp=Tue 2026-10-13 11:39:38 UTC
"""
BACKUP = SystemdTimer("cards-backup.timer", "cards-backup.service", name="Daily snapshot",
                      id="cards-backup")


def timer(timer_props=TIMER_ON, service_props=ONESHOT_OK, spec=BACKUP, now=NOW):
    run = FakeSystemctl({"cards-backup.timer": timer_props, "cards-backup.service": service_props})
    s = systemd_timer_service(spec, now=now, run=run)
    assert_contract(s)
    return s


def test_timer_reports_the_oneshot_services_last_run():
    assert timer() == {
        "id": "cards-backup", "name": "Daily snapshot", "kind": "timer", "where": "VPS",
        "expected_every_minutes": 1440, "last_run_at": "2026-10-13T11:39:36Z",
        "last_ok_at": "2026-10-13T11:39:38Z", "last_status": "ok", "last_error": None,
        "last_duration_ms": 2000, "runs_24h": None, "failures_24h": None,
    }


def test_timer_defaults_its_service_and_id():
    spec = SystemdTimer("cards-backup.timer", expected_every_minutes=60)
    assert spec.service_unit == "cards-backup.service"
    s = timer(spec=spec)
    assert (s["id"], s["name"], s["expected_every_minutes"], s["last_status"]) == (
        "cards-backup.timer", "cards-backup.timer", 60, "ok")


def test_timer_whose_last_run_failed():
    failed = ONESHOT_OK.replace("ActiveState=inactive", "ActiveState=failed").replace(
        "Result=success", "Result=exit-code").replace("ExecMainStatus=0", "ExecMainStatus=2")
    s = timer(service_props=failed)
    assert (s["last_status"], s["last_ok_at"], s["last_run_at"]) == (
        "failed", None, "2026-10-13T11:39:36Z")
    assert s["last_error"] == "last run failed: result exit-code, exit status 2"


def test_timer_while_its_service_runs():
    running = ONESHOT_OK.replace("ActiveState=inactive", "ActiveState=activating").replace(
        "ExecMainStartTimestamp=Tue 2026-10-13 11:39:36", "ExecMainStartTimestamp=Tue 2026-10-13 21:59:00")
    s = timer(service_props=running)
    assert (s["last_status"], s["last_run_at"], s["last_duration_ms"]) == (
        "running", "2026-10-13T21:59:00Z", None)


def test_a_timer_counting_from_its_service_has_no_next_run_while_it_runs():
    # OnUnitActiveSec=1h: systemd schedules the next run only once the service ends.
    running = ONESHOT_OK.replace("ActiveState=inactive", "ActiveState=activating").replace(
        "ExecMainStartTimestamp=Tue 2026-10-13 11:39:36", "ExecMainStartTimestamp=Tue 2026-10-13 21:59:00")
    s = timer(TIMER_ON.replace("NextElapseUSecRealtime=Wed 2026-10-14 11:43:35 UTC",
                               "NextElapseUSecRealtime="), running)
    assert (s["last_status"], s["last_error"]) == ("running", None)


@pytest.mark.parametrize(("timer_props", "error"), [
    (TIMER_ON.replace("ActiveState=active", "ActiveState=inactive"),
     "timer cards-backup.timer is stopped (inactive): nothing starts cards-backup.service"),
    (TIMER_ON.replace("UnitFileState=enabled", "UnitFileState=disabled"),
     "timer cards-backup.timer is disabled: it won't start after a reboot"),
    (TIMER_ON.replace("NextElapseUSecRealtime=Wed 2026-10-14 11:43:35 UTC",
                      "NextElapseUSecRealtime="),
     "timer cards-backup.timer has no next run scheduled"),
    (TIMER_ON.replace("Wed 2026-10-14 11:43:35", "Tue 2026-10-13 21:00:00"),
     "timer cards-backup.timer is overdue: its run was due 2026-10-13T21:00:00Z"),
    ("LoadState=not-found\nActiveState=inactive\n",
     "timer cards-backup.timer not found: nothing starts cards-backup.service"),
])
def test_a_timer_that_wont_fire_again_is_failed_not_ok(timer_props, error):
    s = timer(timer_props=timer_props)
    assert (s["last_status"], s["last_error"]) == ("failed", error)
    # The last success stays the real one, so a collector also sees it go late.
    assert s["last_ok_at"] == "2026-10-13T11:39:38Z"


def test_a_stopped_timer_keeps_the_services_own_failure():
    failed = ONESHOT_OK.replace("Result=success", "Result=exit-code").replace(
        "ExecMainStatus=0", "ExecMainStatus=1")
    s = timer(TIMER_ON.replace("ActiveState=active", "ActiveState=failed"), failed)
    assert s["last_error"] == (
        "timer cards-backup.timer is stopped (failed): nothing starts cards-backup.service;"
        " last run failed: result exit-code, exit status 1")


def test_timer_after_a_reboot_uses_the_last_trigger():
    forgotten = ONESHOT_OK.replace("Tue 2026-10-13 11:39:36 UTC", "").replace(
        "Tue 2026-10-13 11:39:38 UTC", "")
    s = timer(service_props=forgotten)
    assert (s["last_status"], s["last_run_at"], s["last_ok_at"]) == (
        "unknown", "2026-10-13T11:39:36Z", None)


def test_timer_never_ran_and_missing_service():
    never = TIMER_ON.replace("Tue 2026-10-13 11:39:36 UTC", "n/a")
    fresh = ONESHOT_OK.replace("Tue 2026-10-13 11:39:36 UTC", "").replace(
        "Tue 2026-10-13 11:39:38 UTC", "")
    s = timer(never, fresh)
    assert (s["last_status"], s["last_run_at"], s["last_error"]) == ("unknown", None, None)
    run = FakeSystemctl({"cards-backup.timer": TIMER_ON})
    s = systemd_timer_service(BACKUP, now=NOW, run=run)
    assert (s["last_status"], s["last_error"]) == ("failed", "service cards-backup.service not found")


def test_timer_without_systemctl_is_unknown(monkeypatch):
    monkeypatch.setattr(hb.shutil, "which", lambda name: None)
    s = systemd_timer_service(BACKUP)
    assert (s["kind"], s["last_status"], s["last_error"]) == (
        "timer", "unknown", "systemctl not available")


def test_timer_reads_only_the_properties_it_needs():
    run = FakeSystemctl({"cards-backup.timer": TIMER_ON, "cards-backup.service": ONESHOT_OK})
    systemd_timer_service(BACKUP, now=NOW, run=run)
    timer_call, service_call = run.calls
    assert timer_call[2] == "cards-backup.timer" and "NextElapseUSecRealtime" in timer_call[4]
    assert service_call[2] == "cards-backup.service" and "ExecMainExitTimestamp" in service_call[4]


# ── the report ────────────────────────────────────────────────────────


def test_report_merges_orbit_systemd_and_extra_checks():
    planner = Entry("refresh-planner", Every(5), lambda now: None,
                    title="Venue refresh planner", kind="loop", where="VPS")
    sweep = Entry("sweep", TICK, lambda now: None)
    sweeper = Service("fleet-sweeper", 2, title="Fleet sweeper")
    refresh = Service("price-history", expected_every_minutes=None, title="Price history")
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
        "riftbound", entries=[planner, sweep, sweeper, refresh],
        units=["rift-lab.service", BACKUP],
        extra=[gpu_queue, broken_check, lambda: "not a dict"], where="VPS", now=NOW,
        run=FakeSystemctl({"rift-lab.service": ACTIVE, "cards-backup.timer": TIMER_ON,
                           "cards-backup.service": ONESHOT_OK}))

    assert list(report) == ["app", "generated_at", "services"]
    assert report["app"] == "riftbound" and report["generated_at"] == "2026-10-13T22:00:00Z"
    services = report["services"]
    for s in services:
        assert_contract(s)
    assert [s["id"] for s in services] == [
        "refresh-planner", "sweep", "fleet-sweeper", "price-history", "rift-lab.service",
        "cards-backup", "gpu-queue", "broken_check", "<lambda>"]
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
    # On demand: no cadence (never late), reported as null.
    assert by_id["price-history"]["expected_every_minutes"] is None
    assert by_id["price-history"]["last_status"] == "unknown"
    assert (by_id["cards-backup"]["kind"], by_id["cards-backup"]["last_status"]) == ("timer", "ok")
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
