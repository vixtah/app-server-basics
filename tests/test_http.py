"""app_server_basics.http: cooldowns, refusal pauses, counts and stats, with fake transports
(no network)."""

import datetime as dt
import sqlite3
import threading
from contextlib import contextmanager

import httpx
import pytest

from app_server_basics import http
from app_server_basics.http import (
    Blocked,
    CoolingDown,
    Hosts,
    HostState,
    Policy,
    PoliteClient,
)

HOST = "portal.example.invalid"
SEARCH = f"https://{HOST}/api/search?key=secret"
INIT = f"https://{HOST}/api/init"
START = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC).timestamp()

# CardScanner's news sources: 403 and 429 stop a source until the next UTC midnight (or a
# later Retry-After); a Retry-After on any other status is honoured as given.
DAILY = Policy(cooldown_statuses=frozenset({403, 429}), cooldown_default=0, cooldown_min=0,
               cooldown_max=None, cooldown_until_utc_midnight=True,
               retry_after_any_status=True, block_statuses=frozenset())


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Fake:
    """A response with only what PoliteClient reads (as curl_cffi and Mock responses)."""

    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}


class Transport:
    """Answers each request with the next queued response; records every request."""

    def __init__(self):
        self.calls, self.queue = [], []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.queue.pop(0) if self.queue else Fake()
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def path(tmp_path):
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    http.create_tables(conn)
    conn.close()
    return path


@pytest.fixture
def connect(path):
    @contextmanager
    def connect():
        conn = sqlite3.connect(path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    return connect


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hosts(connect, clock):
    return Hosts(connect, Policy(), clock=clock, batch=1)


@pytest.fixture
def transport():
    return Transport()


@pytest.fixture
def client(hosts, transport):
    return PoliteClient(hosts, transport, retries=2, backoff=lambda attempt: 0)


def pause_hours(hosts, clock):
    return round((hosts.state(HOST).block_until - clock.now) / 3600)


# --- refusals ------------------------------------------------------------------------


def test_repeated_403_pauses_the_host_with_backoff_capped_at_a_day(client, hosts,
                                                                   transport, clock):
    transport.queue.append(Fake(403))
    with pytest.raises(Blocked):
        client.request("POST", SEARCH)
    assert hosts.state(HOST).paused(clock.now) is None  # one refusal is not yet a block
    hours = []
    for _ in range(7):
        transport.queue.append(Fake(403))
        with pytest.raises(Blocked, match=r"refused this server \(HTTP 403\); paused"):
            client.request("POST", SEARCH)
        hours.append(pause_hours(hosts, clock))
        sent = len(transport.calls)
        for url in (SEARCH, INIT, f"https://{HOST}/other"):  # any path, no request
            with pytest.raises(Blocked, match="refuses this server; retrying"):
                client.request("GET", url)
        assert len(transport.calls) == sent
        clock.now = hosts.state(HOST).block_until + 1  # the pause passes
    assert hours == [1, 2, 4, 8, 16, 24, 24]
    state = hosts.state(HOST)
    assert state.block_request == "POST /api/search"  # never the query string
    assert "secret" not in repr(state)


def test_only_the_refused_request_succeeding_clears_the_count(client, hosts, transport,
                                                              clock):
    transport.queue += [Fake(403), Fake(403)]
    for _ in range(2):
        with pytest.raises(Blocked):
            client.request("POST", SEARCH)
    clock.now = hosts.state(HOST).block_until + 1
    client.request("GET", INIT)  # another request on the host succeeds
    assert hosts.state(HOST).block_count == 2
    transport.queue.append(Fake(403))
    with pytest.raises(Blocked):
        client.request("POST", SEARCH)
    assert pause_hours(hosts, clock) == 2  # escalation kept across the other success
    clock.now = hosts.state(HOST).block_until + 1
    client.request("POST", SEARCH)
    assert hosts.state(HOST) is None


def test_the_count_is_forgotten_after_its_memory(client, hosts, transport, clock):
    transport.queue += [Fake(403), Fake(403)]
    for _ in range(2):
        with pytest.raises(Blocked):
            client.request("POST", SEARCH)
    clock.now += 3600 + 30 * 86400 + 1
    assert hosts.state(HOST) is None
    transport.queue.append(Fake(403))
    with pytest.raises(Blocked):
        client.request("POST", SEARCH)
    assert hosts.state(HOST).block_count == 1


def test_401_is_not_a_refusal_and_is_returned(client, hosts, transport):
    transport.queue.append(Fake(401))
    assert client.request("GET", INIT).status_code == 401
    assert hosts.state(HOST) is None


def test_a_bot_wall_instead_of_data_is_a_refusal(client, hosts, transport, clock):
    def html(response):
        kind = response.headers.get("content-type", "")
        return "HTML instead of data" if "html" in kind else None

    transport.queue += [Fake(200, {"content-type": "text/html"})] * 2
    for _ in range(2):
        with pytest.raises(Blocked, match="HTML instead of data"):
            client.request("GET", INIT, refused=html)
    assert hosts.state(HOST).paused(clock.now) > clock.now + 3500
    assert len(transport.calls) == 2


def test_state_survives_a_new_hosts_object(connect, client, transport, clock):
    transport.queue += [Fake(403), Fake(403)]
    for _ in range(2):
        with pytest.raises(Blocked):
            client.request("POST", SEARCH)
    fresh = PoliteClient(Hosts(connect, Policy(), clock=clock), transport)
    with pytest.raises(Blocked):
        fresh.request("GET", INIT)
    assert len(transport.calls) == 2


# --- cooldowns -----------------------------------------------------------------------


def test_429_cools_the_host_without_retrying_or_touching_others(client, hosts, transport,
                                                                clock):
    transport.queue.append(Fake(429, {"Retry-After": "120"}))
    assert client.request("GET", INIT).status_code == 429
    assert len(transport.calls) == 1  # a 429 is never retried
    state = hosts.state(HOST)
    assert state.cooling(clock.now) == clock.now + 120 and state.cooldown_status == 429
    with pytest.raises(CoolingDown, match="rate limited us; retrying in 3 min") as caught:
        client.request("GET", SEARCH)
    assert caught.value.until == clock.now + 120 and caught.value.status_code == 429
    client.request("GET", "https://other.example.invalid/")
    assert len(transport.calls) == 2
    clock.now += 121
    client.request("GET", SEARCH)
    assert len(transport.calls) == 3


@pytest.mark.parametrize("header,seconds", [
    (None, 300), ("garbage", 300), ("-1", 300), ("0", 60), ("10", 60), ("120", 120),
    ("999999", 6 * 3600), ("Tue, 13 Oct 2026 12:10:00 GMT", 600),
    ("Tue, 13 Oct 2026 11:00:00 GMT", 300),
])
def test_cooldown_length_within_the_policy(hosts, clock, header, seconds):
    headers = {"retry-after": header} if header else {}
    assert hosts.cooldown(INIT, 429, headers) == clock.now + seconds


@pytest.mark.parametrize("header,seconds", [
    ("60", 43200), ("172800", 172800), ("garbage", 43200), ("-1", 43200), ("0", 43200),
    ("Tue, 13 Oct 2026 10:00:00 GMT", 43200), ("Thu, 15 Oct 2026 12:00:00 GMT", 172800),
])
def test_daily_policy_lasts_at_least_until_utc_midnight(connect, clock, header, seconds):
    hosts = Hosts(connect, DAILY, clock=clock)
    assert hosts.cooldown(INIT, 429, {"Retry-After": header}) == clock.now + seconds


@pytest.mark.parametrize("header,seconds", [
    ("3600", 3600), ("172800", 172800), ("invalid", None), ("0", None), ("-3", None),
    ("Tue, 13 Oct 2026 10:00:00 GMT", None),
])
def test_daily_policy_honours_retry_after_on_other_statuses(connect, clock, header, seconds):
    hosts = Hosts(connect, DAILY, clock=clock)
    until = hosts.cooldown(INIT, 503, {"Retry-After": header})
    assert until == (clock.now + seconds if seconds else None)
    assert hosts.cooldown(INIT, 200, {}) is None


def test_403_under_the_daily_policy_is_a_cooldown_not_a_block(connect, clock, transport):
    client = PoliteClient(Hosts(connect, DAILY, clock=clock), transport)
    transport.queue.append(Fake(403))
    assert client.request("GET", INIT).status_code == 403
    with pytest.raises(CoolingDown) as caught:
        client.request("GET", INIT)
    assert caught.value.status_code == 403
    assert caught.value.until == dt.datetime(2026, 10, 14, tzinfo=dt.UTC).timestamp()


def test_key_groups_hosts_into_one_bucket(connect, clock, transport):
    def source(host):
        return "reddit" if host == "reddit.com" or host.endswith(".reddit.com") else host

    hosts = Hosts(connect, DAILY, key=source, clock=clock)
    client = PoliteClient(hosts, transport)
    transport.queue.append(Fake(429))
    client.request("GET", "https://www.reddit.com/r/x.rss")
    for url in ("https://old.reddit.com/", "https://REDDIT.com./search"):
        with pytest.raises(CoolingDown):
            client.request("GET", url)
    client.request("GET", "https://reddit.com.evil.example/")
    assert hosts.key("https://old.reddit.com/") == "reddit"
    assert len(transport.calls) == 2


# --- retries, transports and counts --------------------------------------------------


@pytest.mark.parametrize("status,attempts", [(200, 1), (400, 1), (429, 1), (500, 3)])
def test_only_server_errors_are_retried(client, transport, status, attempts):
    transport.queue += [Fake(status)] * 3
    assert client.request("GET", INIT).status_code == status
    assert len(transport.calls) == attempts


def test_pace_wraps_each_attempt(hosts, transport):
    entered = []

    @contextmanager
    def pace(url):
        entered.append(url)
        yield

    transport.queue += [Fake(503), Fake(200)]
    PoliteClient(hosts, transport, retries=2, backoff=lambda a: 0, pace=pace).request(
        "GET", INIT)
    assert entered == [INIT, INIT]


def test_counts_every_attempt_but_not_skips(client, hosts, transport, clock):
    transport.queue += [Fake(200), Fake(503), Fake(503), Fake(503), Fake(429)]
    client.request("GET", INIT)
    client.request("GET", INIT)  # three attempts
    client.request("GET", INIT)  # 429
    with pytest.raises(CoolingDown):
        client.request("GET", INIT)  # skipped: not an attempt
    transport.queue.append(TimeoutError("timed out"))
    other = "https://overpass.example.invalid/api?q=1"
    with pytest.raises(TimeoutError):
        client.request("GET", other)
    stats = {s["host"]: s for s in hosts.stats()}
    row = stats[HOST]
    assert tuple(row) == http.STATS_FIELDS
    assert (row["requests_24h"], row["ok_24h"], row["errors_24h"],
            row["rate_limited_24h"]) == (5, 1, 3, 1)
    assert row["status"] == "rate_limited" and row["last_status_code"] == 429
    assert row["cooldown_until"] == "2026-10-13T12:05:00Z"
    assert row["last_ok_at"] == "2026-10-13T12:00:00Z"
    assert row["id"] == HOST
    timed_out = stats["overpass.example.invalid"]
    assert timed_out["errors_24h"] == 1 and timed_out["status"] == "failing"
    assert timed_out["last_status_code"] is None


def test_works_with_httpx(connect, clock):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/limited":
            return httpx.Response(429, headers={"Retry-After": "90"})
        return httpx.Response(200, json={"ok": True})

    hosts = Hosts(connect, Policy(), clock=clock)
    with httpx.Client(transport=httpx.MockTransport(handler)) as session:
        client = PoliteClient(hosts, session.request)
        assert client.request("GET", "https://api.example.invalid/a").json() == {"ok": True}
        response = client.request("GET", "https://api.example.invalid/limited")
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()
        with pytest.raises(CoolingDown):
            client.request("GET", "https://api.example.invalid/a")
    assert hosts.state("api.example.invalid").cooling(clock.now) == clock.now + 90
    assert len(seen) == 2


def test_classification_rules(connect, clock):
    hosts = Hosts(connect, Policy(), clock=clock)

    def log(host, statuses, ago=60):
        for i, status in enumerate(statuses):
            clock.now = START - ago + i
            hosts.record(f"https://{host}/x", status, None if status else "timeout")
        clock.now = START

    log("refused.example", [403, 403, 403, 200])  # mostly refused
    log("fine.example", [200, 200, 403])
    log("failing.example", [500, 502, None, 200])  # mostly 5xx / timeouts
    log("limited-earlier.example", [429, 200, 200, 200], ago=7200)  # a 429, 2 h ago
    log("quiet.example", [200], ago=2 * 86400)  # nothing in 24 h
    with connect() as conn:
        http.merge_state(conn, HostState("paused.example", block_count=2,
                                         block_until=START + 3600, block_reason="HTTP 403",
                                         block_forget_at=START + 3600 + 30 * 86400),
                         START)
    found = {s["host"]: s for s in hosts.stats()}
    assert found["refused.example"]["status"] == "blocked"
    assert found["refused.example"]["note"] == "mostly HTTP 401/403"
    assert found["fine.example"]["status"] == "ok"
    assert found["fine.example"]["note"] == "2 of 3 requests answered in 24 hours"
    assert found["failing.example"]["status"] == "failing"
    assert found["limited-earlier.example"]["status"] == "ok"
    assert found["quiet.example"]["status"] == "idle"
    paused = found["paused.example"]  # paused now, no requests needed to show it
    assert paused["status"] == "blocked" and paused["requests_24h"] == 0
    assert paused["cooldown_until"] == "2026-10-13T13:00:00Z"
    assert paused["note"] == ("refuses this server (HTTP 403); paused until "
                              "2026-10-13T13:00:00Z")


def test_counts_are_batched_scoped_and_pruned(connect, clock):
    scope = ["db1"]
    hosts = Hosts(connect, Policy(), clock=clock, scope=lambda: scope[0], batch=3,
                  flush_seconds=3600)
    hosts.flush()
    hosts.record(INIT, 200)
    hosts.record(INIT, 200)
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 0
    hosts.record(INIT, 200)  # the batch is full
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 3
    hosts.record(INIT, 200)
    scope[0] = "db2"  # the app moved to another database: the buffered row is dropped
    hosts.flush()
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM http_attempts").fetchone()[0] == 3
    hosts._pruned = 0
    clock.now += 8 * 86400
    hosts.record(INIT, 200)
    hosts.flush()
    with connect() as conn:
        assert conn.execute("SELECT at FROM http_attempts").fetchall() == [(clock.now,)]


def test_counting_never_breaks_a_request(clock, transport):
    @contextmanager
    def broken():
        raise sqlite3.OperationalError("disk I/O error")
        yield

    hosts = Hosts(broken, Policy(), clock=clock, batch=1)
    hosts.record(INIT, 200)  # logged, not raised


def test_merge_keeps_the_later_cooldown_and_the_larger_count(connect):
    now = START
    with connect() as conn:
        http.merge_state(conn, HostState(HOST, cooldown_until=now + 100, cooldown_status=429,
                                         block_count=3, block_until=now + 7200,
                                         block_request="POST /s", block_reason="HTTP 403",
                                         block_forget_at=now + 7200 + 30 * 86400), now)
        http.merge_state(conn, HostState(HOST, cooldown_until=now + 50, block_count=2,
                                         block_until=now + 3600, block_request="GET /x",
                                         block_forget_at=now + 99999), now)
        http.merge_state(conn, HostState("gone.example", cooldown_until=now - 1), now)
        state = http.read_state(conn, HOST, now)
        assert http.read_state(conn, "gone.example", now) is None
    assert state.cooldown_until == now + 100 and state.block_count == 3
    assert state.block_request == "POST /s"


def test_concurrent_refusals_are_all_counted(connect, clock):
    hosts = Hosts(connect, Policy(block_after=100), clock=clock)

    def refuse():
        for _ in range(5):
            hosts.refuse("POST", SEARCH, "HTTP 403")

    threads = [threading.Thread(target=refuse) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert hosts.state(HOST).block_count == 20


# --- redaction -----------------------------------------------------------------------


def test_errors_and_labels_carry_no_query_or_token():
    url = "https://user:pw@api.example.invalid:8443/v1/search?api_key=abc123&q=x#frag"
    assert http.redact_url(url) == "https://api.example.invalid:8443/v1/search"
    assert http.request_label("post", url) == "POST /v1/search"
    exc = RuntimeError(f"Client error '403 Forbidden' for url '{url}'\nmore detail")
    text = http.describe_error(exc)
    assert "abc123" not in text and "pw" not in text and "more detail" not in text
    assert "api.example.invalid" in text
    assert http.describe_error(httpx.ConnectError("offline")) == "offline"
    assert http.error_category(TimeoutError("timed out")) == "timeout"
    assert http.error_category(httpx.ReadTimeout("x")) == "timeout"
    assert http.error_category(ConnectionError("refused")) == "transport"


def test_host_of():
    assert http.host_of("https://WWW.Example.com./a?b") == "www.example.com"
    assert http.host_of("Example.com.") == "example.com"
