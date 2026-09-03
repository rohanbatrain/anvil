"""What stands between the public internet and the console.

The console is designed for localhost, where it has no authentication because it
holds nothing real and adding a login would imply a threat model it does not
have. A public demonstration is a different situation, and the difference is
handled here rather than by hoping nobody finds the URL.

Three things, and the reasoning for each is worth stating because "we added
security headers" is not a security posture.

**Basic authentication, off by default.** Set ``ANVIL_CONSOLE_PASSWORD`` and the
whole surface requires credentials; leave it unset and local development stays
frictionless. Comparison is constant-time. This is not protecting money -- the
deployed instance has no credentials and cannot move any -- it is keeping the
demonstration out of search engines and away from casual abuse, and giving the
person who shares the link some control over who follows it.

**A rate limit on the expensive endpoint.** ``/api/batch`` runs a three-arm
experiment over thousands of simulated subscriptions. It is cached per
parameter set, but the first call for a novel size is real CPU, and a single
process serving a demo is trivially exhausted by anyone who notices.

**Headers that assume the page will be framed and sniffed.** The console loads
its fonts from Google and nothing else, so the content security policy can be
narrow enough to be worth having rather than a wildcard that documents nothing.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from anvil.core.config import get_settings

#: Endpoints whose cost is worth bounding, and the ceiling per client per window.
_EXPENSIVE_PREFIXES: tuple[str, ...] = ("/api/batch",)
_RATE_LIMIT = 12
_RATE_WINDOW_SECONDS = 60

#: Paths that answer before authentication, so an uptime check does not need a
#: credential and a health probe cannot be locked out by a config mistake.
_UNAUTHENTICATED: frozenset[str] = frozenset({"/health", "/robots.txt"})

_basic = HTTPBasic(auto_error=False)
_hits: defaultdict[str, deque[float]] = defaultdict(deque)


def console_auth_enabled() -> bool:
    return bool(get_settings().console_password.get_secret_value())


def require_console_auth(request: Request, credentials: HTTPBasicCredentials | None = None) -> None:
    """Reject anything without the shared demonstration credential.

    Both comparisons run even when the username is already wrong, because
    short-circuiting on the first mismatch leaks which half was correct through
    timing. The cost is one wasted comparison per rejected request.
    """
    settings = get_settings()
    expected_password = settings.console_password.get_secret_value()
    if not expected_password:
        return
    if request.url.path in _UNAUTHENTICATED:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This demonstration is credential-gated.",
            headers={"WWW-Authenticate": 'Basic realm="Anvil"'},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.console_username)
    pass_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials.",
            headers={"WWW-Authenticate": 'Basic realm="Anvil"'},
        )


def _client_key(request: Request) -> str:
    """Identify the caller for rate limiting.

    Behind a proxy the socket address is the proxy, so the first hop in
    ``X-Forwarded-For`` is used when present. That header is client-controlled
    and therefore spoofable; it is adequate for throttling a demonstration and
    would not be adequate for anything that mattered.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> None:
    if not any(request.url.path.startswith(p) for p in _EXPENSIVE_PREFIXES):
        return
    key = _client_key(request)
    now = time.monotonic()
    window = _hits[key]
    while window and now - window[0] > _RATE_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"At most {_RATE_LIMIT} batch runs per minute. Each one simulates "
                "thousands of subscriptions; results are cached per seed and size, "
                "so repeating a request you have already made is free."
            ),
            headers={"Retry-After": str(_RATE_WINDOW_SECONDS)},
        )
    window.append(now)


#: The console loads fonts from Google and nothing else. Everything else is
#: inline and same-origin, so the policy can be narrow enough to mean something.
_CSP = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_HEADERS: dict[str, str] = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Attach the headers, and HSTS only where it is honest to do so.

    Sending HSTS over plain HTTP tells a browser to refuse the only scheme it
    can reach the service on, which breaks local development for as long as the
    max-age lasts. It is therefore set only when the request already arrived
    over TLS.
    """
    response = await call_next(request)
    for header, value in _HEADERS.items():
        response.headers.setdefault(header, value)
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if forwarded_proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response
