"""Who may call an app's server: the rules Tasks, Reserve, CardScanner and Riftbound each
used to write for themselves.

Two facts about a request decide almost everything:

- **Did it come through the public tunnel?** Cloudflare stamps every request it carries
  through the tunnel with a ``Cf-Connecting-Ip`` header; requests over Wi-Fi, Tailscale
  or from the same machine have none. :func:`through_tunnel` is that one rule (the
  header name is a parameter, ``cf-connecting-ip`` by default). A forged header only
  makes a request *less* trusted, never more.
- **Is the client on a private network?** :func:`private_address` says whether an
  address is loopback, RFC 1918, Tailscale (``100.64.0.0/10``, ``fd7a:115c:a1e0::/48``),
  IPv6 unique-local or link-local. :func:`client_host` reads only the socket peer as
  Uvicorn reports it; ``X-Forwarded-For`` and friends are never read here. (Uvicorn
  itself rewrites the peer from ``X-Forwarded-For`` only for the addresses in its
  ``forwarded_allow_ips``, e.g. the local cloudflared, whose requests also carry the
  tunnel header.)

Tokens are checked by :class:`Tokens`: an app's plain token from one environment
variable, SHA-256 hashes of more tokens from another (comma-separated), and/or a lookup
the app provides (token hash to identity, for per-user tokens kept in a database). Each
check is constant-time, a token is read from one named header (and, for old clients
only, an optional query parameter), and no token is ever put into an exception, a log
line or a response body.

On top of those:

- :func:`require_token` and :func:`require_owner`: FastAPI dependencies. The first
  wants a valid token on every transport; the second also wants a private network and
  never the tunnel, and refuses with 404 by default so owner routes don't advertise
  themselves.
- :class:`TokenGate`: ASGI middleware for apps whose direct connections are trusted
  (Tasks): requests through the tunnel need a token, everything else passes.
- :func:`install_log_redaction`: a logging filter that hides token query parameters and
  token headers in log records (Uvicorn logs every request line).

Standard library only, except :func:`require_token` / :func:`require_owner`, which
import FastAPI when called (the ``fastapi`` extra).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any

__all__ = [
    "PRIVATE_NETWORKS",
    "REDACTED",
    "TUNNEL_HEADER",
    "RedactTokens",
    "TokenGate",
    "Tokens",
    "client_host",
    "hash_token",
    "install_log_redaction",
    "private_address",
    "redact",
    "require_owner",
    "require_token",
    "through_tunnel",
]

log = logging.getLogger(__name__)

#: Cloudflare puts this header on every request it carries through the tunnel.
TUNNEL_HEADER = "cf-connecting-ip"

#: Loopback, RFC 1918, Tailscale, IPv4/IPv6 link-local and IPv6 unique-local addresses.
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10",          # Tailscale (carrier-grade NAT range)
    "169.254.0.0/16",         # IPv4 link-local
    "::1/128",
    "fd7a:115c:a1e0::/48",    # Tailscale IPv6 (inside fc00::/7, listed for clarity)
    "fc00::/7",               # IPv6 unique-local
    "fe80::/10",              # IPv6 link-local
))


# --- the two rules -----------------------------------------------------------------------


def through_tunnel(headers: Mapping[str, str], header: str = TUNNEL_HEADER) -> bool:
    """True when the request came through the public tunnel: ``header`` is present
    (any case). ``headers`` is a request's headers (Starlette's, or a plain dict)."""
    header = header.lower()
    return any(key.lower() == header for key in headers)


def private_address(host: str | None) -> bool:
    """True for a loopback, RFC 1918, Tailscale, unique-local or link-local address.
    Anything else, a hostname, or no address at all is public. An IPv4 address written
    as IPv6 (``::ffff:10.0.0.5``) counts as that IPv4 address."""
    try:
        ip = ipaddress.ip_address((host or "").split("%", 1)[0])  # drop an IPv6 zone
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in PRIVATE_NETWORKS)


def client_host(conn: Any) -> str | None:
    """The client's address as Uvicorn reports it (``request.client.host`` for a
    Starlette request or WebSocket, or ``scope["client"]`` for an ASGI scope). Never a
    forwarding header."""
    if isinstance(conn, Mapping):
        client = conn.get("client")
        return client[0] if client else None
    client = getattr(conn, "client", None)
    return client.host if client else None


# --- tokens --------------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """The SHA-256 hex digest apps store instead of a token."""
    return hashlib.sha256(token.encode()).hexdigest()


_query_warned = False


@dataclass(frozen=True)
class Tokens:
    """The tokens an app accepts, and where requests carry them.

    - ``env``: the environment variable holding the app's plain token (e.g.
      ``CARDS_ACCESS_TOKEN``). Read on every check, so changing the environment takes
      effect without rebuilding anything.
    - ``hashes_env``: the variable holding SHA-256 hashes of more accepted tokens,
      comma-separated (e.g. ``CARDS_ACCESS_TOKEN_HASHES``).
    - ``lookup``: for per-user tokens kept by the app: called with a token's hash, it
      returns who the token belongs to, or None. It is only ever given the hash.
    - ``identity``: what a match on ``env`` or ``hashes_env`` returns (default "owner").
    - ``header``: the request header carrying the token (default ``X-App-Token``).
    - ``deprecated_query``: a query parameter old clients put the token in (e.g.
      ``"token"``); None (the default) reads no query parameter. Tokens in URLs end up in
      logs, so also call :func:`install_log_redaction`.

    The object holds variable names and a callable, never a token, so printing it
    reveals nothing.
    """

    env: str | None = None
    hashes_env: str | None = None
    lookup: Callable[[str], Any] | None = None
    identity: Any = "owner"
    header: str = "x-app-token"
    deprecated_query: str | None = None

    def given(self, conn: Any) -> str:
        """The token a request carries: the header first, then the deprecated query
        parameter if one is set; "" when there is none. ``conn`` is a Starlette
        request or WebSocket."""
        token = conn.headers.get(self.header) or ""
        if not token and self.deprecated_query:
            token = conn.query_params.get(self.deprecated_query) or ""
            if token:
                _note_query_token(self.deprecated_query)
        return token

    def match(self, token: str | None) -> Any:
        """Who ``token`` belongs to, or None when it isn't accepted. Comparisons are
        constant-time; a per-user lookup is given the token's hash, never the token."""
        if not token:
            return None
        found = None
        plain = os.environ.get(self.env, "") if self.env else ""
        if plain and hmac.compare_digest(token.encode(), plain.encode()):
            found = self.identity
        digest = hash_token(token)
        if self.hashes_env:
            for known in os.environ.get(self.hashes_env, "").split(","):
                known = known.strip().lower()
                # Every configured hash is compared, match or not.
                if known and hmac.compare_digest(digest.encode(), known.encode()) \
                        and found is None:
                    found = self.identity
        if found is None and self.lookup is not None:
            found = self.lookup(digest)
        return found

    async def amatch(self, token: str | None) -> Any:
        """:meth:`match` without blocking the event loop on a per-user lookup."""
        if self.lookup is None:
            return self.match(token)
        return await asyncio.to_thread(self.match, token)

    async def identify(self, conn: Any) -> Any:
        """Who the request's token belongs to, or None."""
        return await self.amatch(self.given(conn))


def _note_query_token(name: str) -> None:
    global _query_warned
    if not _query_warned:
        _query_warned = True
        log.warning("a client sent its token as ?%s= (deprecated: send the header instead)",
                    name)


# --- FastAPI dependencies ------------------------------------------------------------------


def require_token(tokens: Tokens, *, status: int = 401,
                  detail: str = "an access token is needed") -> Callable:
    """A FastAPI dependency: the request (HTTP or WebSocket) must carry a token
    ``tokens`` accepts, on every transport. Returns who it belongs to; refuses with
    ``status``."""
    from fastapi import HTTPException
    from fastapi.requests import HTTPConnection

    async def dependency(conn: HTTPConnection) -> Any:
        who = await tokens.identify(conn)
        if who is None:
            raise HTTPException(status, detail)
        return who

    dependency.__annotations__["conn"] = HTTPConnection  # resolved here, not by name
    return dependency


def require_owner(tokens: Tokens, *, refuse_status: int = 404, refuse_detail: str = "Not Found",
                  token_status: int = 401, token_detail: str = "an access token is needed",
                  identities: Iterable[Any] | None = None,
                  tunnel_header: str = TUNNEL_HEADER) -> Callable:
    """A FastAPI dependency for owner-only routes: never through the tunnel, only from
    a private network, and only with a token ``tokens`` accepts (when ``identities``
    is given, only a token belonging to one of them).

    Checked in that order: through the tunnel or from a public address is refused with
    ``refuse_status`` (404 by default, so the route doesn't advertise itself), token or
    not; a private client without an accepted token gets ``token_status`` (401), and one
    whose token belongs to someone else gets ``refuse_status``. Returns who the token
    belongs to."""
    from fastapi import HTTPException
    from fastapi.requests import HTTPConnection

    allowed = None if identities is None else frozenset(identities)

    async def dependency(conn: HTTPConnection) -> Any:
        if through_tunnel(conn.headers, tunnel_header) or \
                not private_address(client_host(conn)):
            raise HTTPException(refuse_status, refuse_detail)
        who = await tokens.identify(conn)
        if who is None:
            raise HTTPException(token_status, token_detail)
        if allowed is not None and who not in allowed:
            raise HTTPException(refuse_status, refuse_detail)
        return who

    dependency.__annotations__["conn"] = HTTPConnection
    return dependency


# --- ASGI middleware -----------------------------------------------------------------------


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class TokenGate:
    """ASGI middleware: requests through the tunnel need a token; direct connections
    (Wi-Fi, Tailscale, this machine) pass untouched.

    - ``exempt_hosts``: hostnames protected another way at the edge (Cloudflare Access);
      tunnel requests for them pass too.
    - ``cookie``: a cookie name. After a request authenticates with the header, the
      response sets this cookie (``Secure; HttpOnly; SameSite=Strict``) holding the
      token, so a web view's later same-site requests authenticate without the token in
      a URL. A request authenticated by the cookie alone may read (GET/HEAD/OPTIONS);
      writes and WebSockets need an ``Origin`` of ``https://<its own host>``.
      This cookie is the one place a token goes into a response, and only to the
      client that just sent it.
    - Refusals: HTTP 401 ``{"detail": detail}``; a WebSocket is closed with
      ``ws_close_code`` (4401).

    The identity a token belongs to is put in ``scope["state"]["token_identity"]``
    (``request.state.token_identity``) for tunnel requests.

        app.add_middleware(TokenGate, tokens=Tokens(env="TASKS_ACCESS_TOKEN"),
                           exempt_hosts={"hellodash.cc"}, cookie="tasks_session")
    """

    def __init__(self, app: Callable, *, tokens: Tokens, exempt_hosts: Iterable[str] = (),
                 cookie: str | None = None, tunnel_header: str = TUNNEL_HEADER,
                 detail: str = "an app access token is needed",
                 ws_close_code: int = 4401) -> None:
        self.app = app
        self.tokens = tokens
        self.exempt_hosts = frozenset(h.lower() for h in exempt_hosts)
        self.cookie = cookie
        self.tunnel_header = tunnel_header.lower()
        self.detail = detail
        self.ws_close_code = ws_close_code

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        host = headers.get("host", "").split(":")[0].lower()
        if not through_tunnel(headers, self.tunnel_header) or host in self.exempt_hosts:
            return await self.app(scope, receive, send)

        token = headers.get(self.tokens.header.lower(), "")
        if not token and self.tokens.deprecated_query:
            token = _query_value(scope, self.tokens.deprecated_query)
            if token:
                _note_query_token(self.tokens.deprecated_query)
        cookie_auth = False
        if not token and self.cookie:
            token = _cookie_value(headers.get("cookie", ""), self.cookie)
            cookie_auth = bool(token)
        who = await self.tokens.amatch(token)
        # Cookie-authenticated writes and WebSockets must come from this site.
        if who is not None and cookie_auth and (
                scope["type"] == "websocket" or scope.get("method") not in _SAFE_METHODS) \
                and headers.get("origin") != f"https://{host}":
            who = None
        if who is None:
            return await self._refuse(scope, send)
        scope.setdefault("state", {})["token_identity"] = who

        if not self.cookie or cookie_auth:
            return await self.app(scope, receive, send)
        set_cookie = (f"{self.cookie}={token}; Path=/; Secure; HttpOnly; "
                      "SameSite=Strict").encode("latin-1")

        async def send_with_cookie(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = dict(message, headers=[*message.get("headers", []),
                                                 (b"set-cookie", set_cookie)])
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    async def _refuse(self, scope: dict, send: Callable) -> None:
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": self.ws_close_code})
        import json
        body = json.dumps({"detail": self.detail}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _cookie_value(header: str, name: str) -> str:
    cookies = SimpleCookie()
    try:
        cookies.load(header)
    except CookieError:
        return ""
    return cookies[name].value if name in cookies else ""


def _query_value(scope: dict, name: str) -> str:
    from urllib.parse import parse_qsl
    query = scope.get("query_string", b"").decode("latin-1")
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == name:
            return value
    return ""


# --- log redaction -------------------------------------------------------------------------


REDACTED = "[redacted]"
#: The loggers Uvicorn writes to (each record goes through its own logger's filters).
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_DONE = re.escape(REDACTED)  # a value already redacted is left alone


def _letters(name: str) -> str:
    # A letter or its percent-encoding (``token`` may arrive as ``%74oken``).
    return "".join(f"(?:{re.escape(c)}|%{ord(c):02x})" if c.isalnum() else re.escape(c)
                   for c in name.lower())


def _patterns(params: Iterable[str], headers: Iterable[str]) -> tuple[re.Pattern, ...]:
    out = []
    params = [p for p in params if p]
    headers = [h for h in headers if h]
    if params:
        names = "|".join(_letters(p) for p in params)
        # name=value after a URL, cookie or text separator, up to the next separator.
        out.append(re.compile(rf"(?i)((?:^|[?&;\s\"'])(?:{names})=)(?!{_DONE})[^&#;\s\"']*"))
    if headers:
        names = "|".join(re.escape(h) for h in headers)
        # "X-App-Token: value", "'x-app-token': 'value'", "x-app-token=value".
        out.append(re.compile(rf"(?i)((?:{names})[\"']?\s*[:=]\s*[\"']?)(?!{_DONE})[^\s\"',;}}\]]+"))
    return tuple(out)


_DEFAULT_PARAMS = ("token",)
_DEFAULT_HEADERS = ("x-app-token", "x-rift-token")


def redact(text: str, params: Iterable[str] = _DEFAULT_PARAMS,
           headers: Iterable[str] = _DEFAULT_HEADERS) -> str:
    """``text`` with the values of the named query parameters (or cookies) and headers
    replaced by ``[redacted]``."""
    for pattern in _patterns(params, headers):
        text = pattern.sub(rf"\1{REDACTED}", text)
    return text


class RedactTokens(logging.Filter):
    """A logging filter that hides token values in each record's message, arguments and
    exception text before any handler formats it. It changes records, never drops one.

    Uvicorn's access records keep their argument tuple (its formatter unpacks it), with
    each argument redacted; any other record whose text holds a token gets the redacted
    text as its message (redacting only the format string could break ``%`` formatting).
    """

    def __init__(self, params: Iterable[str] = _DEFAULT_PARAMS,
                 headers: Iterable[str] = _DEFAULT_HEADERS) -> None:
        super().__init__()
        self._patterns = _patterns(params, headers)

    def _redact(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        for pattern in self._patterns:
            value = pattern.sub(rf"\1{REDACTED}", value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        else:
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 — a malformed record: logging reports it
                message = None
            if message is not None:
                cleaned = self._redact(message)
                if cleaned != message:
                    record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:  # formatters print this text instead of formatting exc_info
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def install_log_redaction(*, loggers: Iterable[str] = UVICORN_LOGGERS, root: bool = True,
                          params: Iterable[str] = _DEFAULT_PARAMS,
                          headers: Iterable[str] = _DEFAULT_HEADERS) -> RedactTokens:
    """Put a :class:`RedactTokens` filter on the named loggers (Uvicorn's by default)
    and, with ``root``, on the root logger's current handlers (after
    ``logging.basicConfig``), so the app's own records are covered too. Safe to call
    more than once; returns the filter.

    Call it before ``uvicorn.run``: Uvicorn's logging setup replaces its loggers'
    handlers but keeps their filters. ``params`` are query parameter (and cookie) names
    whose values are hidden; ``headers`` are header names."""
    redactor = RedactTokens(params, headers)
    targets = [logging.getLogger(name) for name in loggers]
    if root:
        targets += logging.getLogger().handlers
    for target in targets:
        for old in [f for f in target.filters if isinstance(f, RedactTokens)]:
            target.removeFilter(old)
        target.addFilter(redactor)
    return redactor
