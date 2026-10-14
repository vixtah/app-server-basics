"""app_server_basics.auth: tunnel and private-network rules, tokens, the FastAPI
dependencies, the ASGI gate (parity with Tasks' AppTokenGate) and log redaction."""

import hmac
import io
import logging
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app_server_basics import auth
from app_server_basics.auth import (
    RedactTokens,
    TokenGate,
    Tokens,
    hash_token,
    install_log_redaction,
    private_address,
    redact,
    require_owner,
    require_token,
    through_tunnel,
)

SECRET = "s3cret-plain-token"
OTHER = "another-token-hashed"
PLAYER = "invite-token-of-ana"
PUBLIC = {"Cf-Connecting-Ip": "203.0.113.1"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", SECRET)
    monkeypatch.setenv("APP_TOKEN_HASHES", f" ,{hash_token(OTHER).upper()}, deadbeef")


def tokens(**kw) -> Tokens:
    return Tokens(env="APP_TOKEN", hashes_env="APP_TOKEN_HASHES", **kw)


# --- the two rules ---------------------------------------------------------------------


def test_tunnel_is_the_cloudflare_header_in_any_case():
    assert through_tunnel({"Cf-Connecting-Ip": "203.0.113.1"})
    assert through_tunnel({"cf-connecting-ip": ""})
    assert not through_tunnel({"x-forwarded-for": "203.0.113.1", "host": "x"})
    assert through_tunnel({"X-Edge": "1"}, header="x-edge")


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.20",
    "100.64.0.1", "100.127.255.254", "169.254.10.1", "::1", "fd7a:115c:a1e0::1",
    "fd12:3456::1", "fe80::1", "fe80::1%en0", "::ffff:192.168.1.5", "::ffff:100.100.1.1",
])
def test_private_addresses(host):
    assert private_address(host)


@pytest.mark.parametrize("host", [
    "8.8.8.8", "203.0.113.1", "172.32.0.1", "100.128.0.1", "2606:4700::1",
    "::ffff:8.8.8.8", "testclient", "", None, "localhost",
])
def test_public_addresses(host):
    assert not private_address(host)


# --- tokens ------------------------------------------------------------------------------


def test_plain_hashed_and_lookup_tokens(env):
    seen = []

    def lookup(digest):
        seen.append(digest)
        return {hash_token(PLAYER): "ana"}.get(digest)

    t = tokens(lookup=lookup)
    assert t.match(SECRET) == "owner"
    assert t.match(OTHER) == "owner"
    assert t.match(PLAYER) == "ana"
    assert t.match("wrong") is None
    assert t.match("") is None and t.match(None) is None
    # The lookup only ever sees hashes.
    assert PLAYER not in seen and hash_token(PLAYER) in seen
    assert tokens(identity="admin").match(SECRET) == "admin"


def test_env_is_read_on_every_check(env, monkeypatch):
    t = tokens()
    assert t.match(SECRET)
    monkeypatch.setenv("APP_TOKEN", "rotated")
    assert t.match(SECRET) is None and t.match("rotated") == "owner"
    monkeypatch.delenv("APP_TOKEN")
    monkeypatch.delenv("APP_TOKEN_HASHES")
    assert t.match(SECRET) is None and t.match("") is None


def test_comparisons_are_constant_time(env, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert tokens().match("wrong") is None
    # One plain comparison plus one per configured hash (blanks skipped), no early exit.
    assert len(calls) == 3
    calls.clear()
    assert tokens().match(OTHER) == "owner"
    assert len(calls) == 3


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_tokens_hold_no_secret(env):
    assert SECRET not in repr(tokens())


# --- dependencies ------------------------------------------------------------------------


def _app(*, owner_kw=None, token_kw=None) -> FastAPI:
    app = FastAPI()
    t = tokens(**(token_kw or {}))

    @app.get("/app")
    async def app_route(who: Annotated[Any, Depends(require_token(t))]):
        return {"who": who}

    @app.get("/owner")
    async def owner_route(who: Annotated[Any, Depends(require_owner(t, **(owner_kw or {})))]):
        return {"who": who}

    @app.websocket("/ws")
    async def ws_route(socket: WebSocket, who: Annotated[Any, Depends(require_token(t))]):
        await socket.accept()
        await socket.send_json({"who": who})
        await socket.close()

    return app


def _client(app, host="192.168.1.20") -> TestClient:
    return TestClient(app, client=(host, 5000))


def test_require_token_everywhere(env):
    c = _client(_app())
    for headers in ({}, PUBLIC):
        r = c.get("/app", headers=headers)
        assert r.status_code == 401 and r.json() == {"detail": "an access token is needed"}
        assert c.get("/app", headers=headers | {"X-App-Token": "nope"}).status_code == 401
        r = c.get("/app", headers=headers | {"X-App-Token": SECRET})
        assert r.status_code == 200 and r.json() == {"who": "owner"}
    with c.websocket_connect("/ws", headers={"X-App-Token": OTHER}) as ws:
        assert ws.receive_json() == {"who": "owner"}
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws"):
        pass


def test_header_first_query_only_when_allowed(env, caplog):
    c = _client(_app())
    assert c.get("/app", params={"token": SECRET}).status_code == 401
    auth._query_warned = False
    c = _client(_app(token_kw={"deprecated_query": "token"}))
    with caplog.at_level(logging.WARNING, logger="app_server_basics.auth"):
        assert c.get("/app", params={"token": SECRET}).status_code == 200
    assert "deprecated" in caplog.text and SECRET not in caplog.text
    # The header wins over the query parameter.
    assert c.get("/app", params={"token": SECRET},
                 headers={"X-App-Token": "wrong"}).status_code == 401


def test_custom_header(env):
    c = _client(_app(token_kw={"header": "x-rift-token"}))
    assert c.get("/app", headers={"X-App-Token": SECRET}).status_code == 401
    assert c.get("/app", headers={"X-Rift-Token": SECRET}).status_code == 200


def test_owner_refusals(env):
    app = _app()
    home = _client(app)
    ok = {"X-App-Token": SECRET}
    assert home.get("/owner", headers=ok).json() == {"who": "owner"}
    assert home.get("/owner").status_code == 401
    assert home.get("/owner", headers={"X-App-Token": "nope"}).status_code == 401
    # Through the tunnel: 404 with or without the token.
    assert home.get("/owner", headers=PUBLIC | ok).status_code == 404
    assert home.get("/owner", headers=PUBLIC).status_code == 404
    # A public client: 404, token or not, and a forged forwarding header changes nothing.
    public = _client(app, host="203.0.113.9")
    for headers in ({}, ok, ok | {"X-Forwarded-For": "127.0.0.1", "X-Real-Ip": "10.0.0.1",
                                   "Forwarded": "for=127.0.0.1"}):
        r = public.get("/owner", headers=headers)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    for host in ("127.0.0.1", "100.101.102.103", "fd7a:115c:a1e0::5", "::1"):
        assert _client(app, host=host).get("/owner", headers=ok).status_code == 200


def test_owner_statuses_and_identities_configurable(env):
    app = _app(owner_kw={"refuse_status": 403, "refuse_detail": "home only",
                         "identities": {"admin"}}, token_kw={"identity": "player"})
    c = _client(app)
    r = c.get("/owner", headers=PUBLIC | {"X-App-Token": SECRET})
    assert r.status_code == 403 and r.json() == {"detail": "home only"}
    # A valid token belonging to someone who isn't an owner.
    assert c.get("/owner", headers={"X-App-Token": SECRET}).status_code == 403
    assert c.get("/owner").status_code == 401


# --- TokenGate: parity with Tasks' AppTokenGate (automations tests/test_http_auth.py) -------


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("TASKS_ACCESS_TOKEN", "secret")
    app = FastAPI()
    app.add_middleware(TokenGate, tokens=Tokens(env="TASKS_ACCESS_TOKEN"),
                       exempt_hosts={"hellodash.cc", "www.hellodash.cc"}, cookie="tasks_session")

    @app.get("/private")
    async def private():
        return {"ok": True}

    @app.post("/private")
    async def write():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"ok": True})
        await socket.close()

    return TestClient(app, base_url="https://tasks.hellodash.cc")


def test_gate_public_http_requires_token_and_cannot_spoof_access(gate):
    assert gate.get("/private", headers=PUBLIC).status_code == 401
    assert gate.get("/private", headers=PUBLIC | {"Cf-Access-Jwt-Assertion": "fake"}
                    ).status_code == 401
    r = gate.get("/private", headers=PUBLIC | {"X-App-Token": "wrong"})
    assert r.status_code == 401 and r.json() == {"detail": "an app access token is needed"}
    r = gate.get("/private", headers=PUBLIC | {"X-App-Token": "secret"})
    assert r.status_code == 200
    assert all(part in r.headers["set-cookie"]
               for part in ("tasks_session=", "Secure", "HttpOnly", "SameSite=Strict"))
    assert gate.get("/private", headers=PUBLIC).status_code == 200      # the cookie
    assert gate.post("/private", headers=PUBLIC).status_code == 401     # cookie write, no Origin
    assert gate.post("/private", headers=PUBLIC | {"Origin": "https://evil.example"}
                     ).status_code == 401
    assert gate.post("/private", headers=PUBLIC | {"Origin": "https://tasks.hellodash.cc"}
                     ).status_code == 200


def test_gate_header_writes_need_no_origin(gate):
    assert gate.post("/private", headers=PUBLIC | {"X-App-Token": "secret"}).status_code == 200


def test_gate_wrong_header_does_not_fall_back_to_cookie(gate):
    gate.get("/private", headers=PUBLIC | {"X-App-Token": "secret"})
    assert gate.get("/private", headers=PUBLIC | {"X-App-Token": "wrong"}).status_code == 401


def test_gate_bad_cookie_refused(gate):
    gate.cookies.set("tasks_session", "forged")
    assert gate.get("/private", headers=PUBLIC).status_code == 401


def test_gate_public_websocket_requires_token(gate):
    with pytest.raises(WebSocketDisconnect) as closed, \
            gate.websocket_connect("/ws", headers=PUBLIC):
        pass
    assert closed.value.code == 4401
    with gate.websocket_connect("/ws", headers=PUBLIC | {"X-App-Token": "secret"}) as ws:
        assert ws.receive_json() == {"ok": True}


def test_gate_cookie_websocket_needs_same_site_origin(gate):
    cookie = PUBLIC | {"Cookie": "tasks_session=secret"}   # the test client's websockets
    with pytest.raises(WebSocketDisconnect), \
            gate.websocket_connect("/ws", headers=cookie):   # don't send its cookie jar
        pass
    with pytest.raises(WebSocketDisconnect), \
            gate.websocket_connect("/ws", headers=cookie | {"Origin": "https://evil.example"}):
        pass
    # The test client's websockets are always to host "testserver".
    with gate.websocket_connect("/ws", headers=cookie | {"Origin": "https://testserver"}) as ws:
        assert ws.receive_json() == {"ok": True}


def test_gate_private_processes_and_access_web_host_preserved(gate):
    assert gate.get("/private").status_code == 200
    assert gate.post("/private").status_code == 200
    assert gate.get("https://hellodash.cc/private", headers=PUBLIC).status_code == 200
    assert gate.get("https://www.hellodash.cc/private", headers=PUBLIC).status_code == 200


def test_gate_without_cookie_sets_none(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", SECRET)
    app = FastAPI()
    app.add_middleware(TokenGate, tokens=Tokens(env="APP_TOKEN", header="x-rift-token",
                                                deprecated_query="token"))

    @app.get("/x")
    async def x():
        return {"ok": True}

    c = TestClient(app)
    r = c.get("/x", headers=PUBLIC | {"X-Rift-Token": SECRET})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert c.get("/x", headers=PUBLIC, params={"token": SECRET}).status_code == 200
    assert c.get("/x", headers=PUBLIC | {"X-App-Token": SECRET}).status_code == 401


# --- nothing leaks a token -----------------------------------------------------------------


def test_refusals_never_echo_the_token(env):
    c = _client(_app(token_kw={"deprecated_query": "token"}))
    bad = "bad-" + SECRET
    for path in ("/app", "/owner"):
        for kw in ({"headers": {"X-App-Token": bad}}, {"params": {"token": bad}},
                   {"headers": PUBLIC | {"X-App-Token": SECRET}}):
            r = c.get(path, **kw)
            assert SECRET not in r.text and SECRET not in str(r.headers)


# --- log redaction -------------------------------------------------------------------------


def test_redact_query_strings_headers_and_cookies():
    assert redact("GET /api/x?token=abc&player=p HTTP/1.1") == \
        "GET /api/x?token=[redacted]&player=p HTTP/1.1"
    assert redact("/ws?player=p&TOKEN=abc") == "/ws?player=p&TOKEN=[redacted]"
    assert redact("/ws?%74oken=abc") == "/ws?%74oken=[redacted]"
    assert redact("/x?mytoken=abc") == "/x?mytoken=abc"           # another parameter
    assert redact("X-App-Token: abc def") == "X-App-Token: [redacted] def"
    assert redact("{'x-rift-token': 'abc'}") == "{'x-rift-token': '[redacted]'}"
    assert redact("cookie a=1; tasks_session=abc", params=("tasks_session",)) == \
        "cookie a=1; tasks_session=[redacted]"


def _capture(logger_name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_uvicorn_access_records_are_redacted():
    logger, stream = _capture("uvicorn.access")
    try:
        install_log_redaction(root=False)
        # Uvicorn's access line: (client, method, full path, http version, status).
        logger.info('%s - "%s %s HTTP/%s" %d', "203.0.113.1:5000", "GET",
                    f"/api/riftbound/game?player=p&token={SECRET}", "1.1", 200)
        logging.getLogger("uvicorn.error").error("bad socket /ws?token=%s", SECRET)
        out = stream.getvalue()
        assert SECRET not in out
        assert "token=[redacted]" in out and "player=p" in out and "200" in out
    finally:
        for name in auth.UVICORN_LOGGERS:
            for f in list(logging.getLogger(name).filters):
                logging.getLogger(name).removeFilter(f)
        logger.handlers, logger.propagate = [], True


def test_filters_survive_uvicorn_logging_setup():
    from logging.config import dictConfig

    from uvicorn.config import LOGGING_CONFIG

    try:
        install_log_redaction(root=False)
        dictConfig(LOGGING_CONFIG)
        assert any(isinstance(f, RedactTokens)
                   for f in logging.getLogger("uvicorn.access").filters)
    finally:
        for name in auth.UVICORN_LOGGERS:
            for f in list(logging.getLogger(name).filters):
                logging.getLogger(name).removeFilter(f)


def test_root_handlers_messages_and_exceptions_are_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        install_log_redaction(loggers=(), params=("token", "tasks_session"),
                              headers=("x-app-token",))
        install_log_redaction(loggers=(), params=("token", "tasks_session"),
                              headers=("x-app-token",))     # twice: still one filter
        assert sum(isinstance(f, RedactTokens) for f in handler.filters) == 1
        app_log = logging.getLogger("myapp")
        app_log.warning("headers %s", {"x-app-token": SECRET})
        app_log.warning("cookie: tasks_session=%s", SECRET)
        try:
            raise RuntimeError(f"failed fetching /x?token={SECRET}")
        except RuntimeError:
            app_log.exception("boom")
        out = stream.getvalue()
        assert SECRET not in out and out.count("[redacted]") >= 3 and "boom" in out
    finally:
        root.removeHandler(handler)


def test_malformed_record_is_kept():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%d", ("nope",), None)
    assert RedactTokens().filter(record) is True
