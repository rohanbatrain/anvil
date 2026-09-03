"""Tests for the layer between the public internet and the console.

The console is unauthenticated on localhost by design, and that design is only
safe if the switch that changes it actually works. These tests exist because
"we added auth" is a claim, and the failure mode of a broken auth gate is that
nobody notices until it matters.
"""

from __future__ import annotations

import pytest
from anvil.api import security
from anvil.core.config import Settings, get_settings
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials


class FakeURL:
    def __init__(self, path: str, scheme: str = "http") -> None:
        self.path = path
        self.scheme = scheme


class FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class FakeRequest:
    """The narrow slice of Request the security layer actually reads."""

    def __init__(
        self,
        path: str = "/",
        host: str = "203.0.113.7",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = FakeURL(path)
        self.client = FakeClient(host)
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _clear_state() -> None:
    security._hits.clear()
    get_settings.cache_clear()
    yield
    security._hits.clear()
    get_settings.cache_clear()


def _with_password(monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    monkeypatch.setattr(security, "get_settings", lambda: Settings(console_password=password))


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_no_password_means_no_authentication() -> None:
    """Localhost stays frictionless. This is the default and it must hold."""
    assert not security.console_auth_enabled()
    security.require_console_auth(FakeRequest(), None)


def test_a_password_gates_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_password(monkeypatch, "s3cret")
    with pytest.raises(HTTPException) as caught:
        security.require_console_auth(FakeRequest(), None)
    assert caught.value.status_code == 401
    assert "Basic" in caught.value.headers["WWW-Authenticate"]


def test_correct_credentials_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_password(monkeypatch, "s3cret")
    security.require_console_auth(
        FakeRequest(), HTTPBasicCredentials(username="reviewer", password="s3cret")
    )


@pytest.mark.parametrize(
    ("user", "password"),
    [("reviewer", "wrong"), ("wrong", "s3cret"), ("wrong", "wrong"), ("", "")],
)
def test_wrong_credentials_are_refused(
    monkeypatch: pytest.MonkeyPatch, user: str, password: str
) -> None:
    _with_password(monkeypatch, "s3cret")
    with pytest.raises(HTTPException) as caught:
        security.require_console_auth(
            FakeRequest(), HTTPBasicCredentials(username=user, password=password)
        )
    assert caught.value.status_code == 401


def test_health_answers_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe must not need a secret, and a config mistake must not lock it out."""
    _with_password(monkeypatch, "s3cret")
    security.require_console_auth(FakeRequest(path="/health"), None)
    security.require_console_auth(FakeRequest(path="/robots.txt"), None)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_the_expensive_endpoint_is_bounded() -> None:
    request = FakeRequest(path="/api/batch")
    for _ in range(security._RATE_LIMIT):
        security.check_rate_limit(request)
    with pytest.raises(HTTPException) as caught:
        security.check_rate_limit(request)
    assert caught.value.status_code == 429
    assert "Retry-After" in caught.value.headers


def test_cheap_endpoints_are_not_bounded() -> None:
    request = FakeRequest(path="/api/taxonomy")
    for _ in range(security._RATE_LIMIT * 3):
        security.check_rate_limit(request)


def test_one_client_cannot_exhaust_another(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limits are per client, or a single abuser denies service to everyone."""
    noisy = FakeRequest(path="/api/batch", host="198.51.100.1")
    quiet = FakeRequest(path="/api/batch", host="198.51.100.2")
    for _ in range(security._RATE_LIMIT):
        security.check_rate_limit(noisy)
    with pytest.raises(HTTPException):
        security.check_rate_limit(noisy)
    security.check_rate_limit(quiet)


def test_the_forwarded_header_identifies_the_client_behind_a_proxy() -> None:
    """Behind a proxy the socket address is the proxy, so every caller would
    share one bucket and the first busy user would lock out the rest."""
    a = FakeRequest(path="/api/batch", headers={"x-forwarded-for": "192.0.2.1, 10.0.0.1"})
    b = FakeRequest(path="/api/batch", headers={"x-forwarded-for": "192.0.2.2, 10.0.0.1"})
    for _ in range(security._RATE_LIMIT):
        security.check_rate_limit(a)
    with pytest.raises(HTTPException):
        security.check_rate_limit(a)
    security.check_rate_limit(b)


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_the_policy_permits_only_what_the_console_uses() -> None:
    csp = security._CSP
    assert "frame-ancestors 'none'" in csp
    assert "https://fonts.googleapis.com" in csp
    assert "https://fonts.gstatic.com" in csp
    # A wildcard would document nothing and permit everything.
    assert "*" not in csp


def test_every_header_has_a_value() -> None:
    assert set(security._HEADERS) >= {
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    }
    assert all(security._HEADERS.values())
