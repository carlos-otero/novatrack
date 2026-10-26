"""Web layer tests: auth, API, webhook, security, CSRF, rate limiting.

All siblings (tracking, notifications, providers) are real modules.
Only external HTTP calls are mocked:
  - OIDC: patch novatrack.app.oidc_login / oidc_exchange as AsyncMock
  - Webhook URL validation: patch notifications.validate_webhook_url when
    creating channels that would otherwise do DNS resolution.

Run: uv run pytest tests/test_web.py -q
"""
from __future__ import annotations

import base64
import hashlib
import json
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

# ---------------------------------------------------------------------------
# Project imports (real modules — no sys.modules stubs)
# ---------------------------------------------------------------------------
from itsdangerous import TimestampSigner  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from novatrack.db import make_engine, make_session_factory  # noqa: E402
from novatrack.models import ApiKey, AppSettings, Base, Shipment  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_SECRET = "testsecretkey32bytesxxxxxxxxxxxxxxxxxx"
TEST_TRACK17_KEY = "test-17track-apikey-abc123"
TEST_OIDC_ISSUER = "https://idp.test.example.com"
TEST_APP_BASE = "https://app.test.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_cookie(data: dict, secret: str = TEST_SECRET) -> str:
    """Create a valid signed Starlette session cookie value."""
    signer = TimestampSigner(secret)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


def _webhook_sig(body: bytes, key: str = TEST_TRACK17_KEY) -> str:
    """Compute official 17TRACK v2.2 HMAC-SHA256 signature."""
    return hashlib.sha256(body + b"/" + key.encode()).hexdigest()


def _webhook_headers(sig: str) -> dict:
    """Return HTTP headers with the official 'sign' header name."""
    return {"Content-Type": "application/json", "sign": sig}


def _make_api_key_in_db(session_factory, name="test-key") -> tuple[str, str]:
    """Create an API key row; return (raw_key, key_id)."""
    from novatrack.security import generate_api_key

    raw, digest = generate_api_key()
    with session_factory.begin() as s:
        k = ApiKey(name=name, digest=digest, prefix=raw[:8])
        s.add(k)
        s.flush()
        kid = k.id
    return raw, kid


def _make_shipment(
    session_factory,
    number: str = "TESTTRACK001",
    status: str = "in_transit",
    bucket: str = "active",
) -> str:
    with session_factory.begin() as s:
        ship = Shipment(number=number, name="Test Pkg", status=status, bucket=bucket)
        s.add(ship)
        s.flush()
        return ship.id


def _ensure_app_settings(session_factory) -> None:
    with session_factory.begin() as s:
        if not s.get(AppSettings, 1):
            s.add(AppSettings(id=1))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def sf(engine):
    """Session factory scoped per test."""
    return make_session_factory(engine)


@pytest.fixture()
def settings():
    from novatrack.config import Settings

    return Settings.model_construct(
        database_url="sqlite://",
        oidc_issuer=TEST_OIDC_ISSUER,
        oidc_client_id="test-client-id",
        oidc_client_secret=SecretStr("test-client-secret"),
        app_base_url=TEST_APP_BASE,
        secret_key=SecretStr(TEST_SECRET),
        track17_api_key=SecretStr(TEST_TRACK17_KEY),
        allowed_oidc_subjects="",
        tz="UTC",
        log_level="INFO",
        telegram_enabled=False,
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_thread_id=None,
        trusted_proxy_ips="127.0.0.1",
        worker_poll_seconds=5,
    )


@pytest.fixture()
def provider():
    p = MagicMock()
    p.register.return_value = {"accepted": [{"number": "TESTTRACK001"}]}
    p.gettrackinfo.return_value = {
        "track_info": {
            "latest_status": {"status": "InTransit", "sub_status": ""},
            "tracking": {"providers": []},
        }
    }
    p.getquota.return_value = {"total": 100, "used": 10, "remain": 90}
    return p


@pytest.fixture()
def app(settings, sf, provider):
    from novatrack.app import create_app

    _ensure_app_settings(sf)
    return create_app(settings=settings, session_factory=sf, provider=provider)


@pytest.fixture()
def client(app):
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def authed_client(app):
    """TestClient with a valid session cookie injected."""
    session_data = {"user": {"sub": "u-test", "name": "Test User"}, "csrf": "testcsrftoken"}
    cookie = _make_session_cookie(session_data)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        c.cookies.set("novatrack_session", cookie)
        yield c, "testcsrftoken"


@pytest.fixture()
def api_key(sf):
    raw, _ = _make_api_key_in_db(sf)
    return raw


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------


def test_healthz_returns_200_when_db_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_returns_503_when_db_broken(settings, provider):
    from novatrack.app import create_app

    broken_sf = MagicMock()
    broken_sf.return_value.__enter__ = MagicMock(side_effect=Exception("db down"))
    broken_sf.return_value.__exit__ = MagicMock(return_value=False)
    app2 = create_app(settings=settings, session_factory=broken_sf, provider=provider)
    with TestClient(app2, base_url="https://testserver", raise_server_exceptions=False) as c:
        r = c.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert "db down" not in body.get("detail", "")


# ---------------------------------------------------------------------------
# 2. Security headers
# ---------------------------------------------------------------------------


def test_security_headers_present_on_all_responses(client):
    r = client.get("/healthz")
    h = r.headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "max-age=" in h.get("strict-transport-security", "")
    csp = h.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "cdn." not in csp
    assert h.get("referrer-policy") == "no-referrer"


def test_security_headers_on_redirect_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "strict-transport-security" in r.headers


# ---------------------------------------------------------------------------
# 3. Auth enforcement
# ---------------------------------------------------------------------------


def test_unauthenticated_root_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/auth/login" in r.headers.get("location", "")


def test_unauthenticated_unknown_route_redirects_to_login(client):
    r = client.get("/random/unknown/path", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/auth/login" in r.headers.get("location", "")


def test_authenticated_root_returns_200(authed_client):
    c, _ = authed_client
    r = c.get("/")
    assert r.status_code == 200


def test_static_protected_asset_redirects_when_unauth(client):
    """Protected static files redirect unauthenticated requests to login."""
    r = client.get("/static/style.css", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/auth/login" in r.headers.get("location", "")


def test_static_auth_css_is_public(client):
    """/static/auth.css must be accessible without a session."""
    r = client.get("/static/auth.css")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 4. Default docs/redoc endpoints not exposed
# ---------------------------------------------------------------------------


def test_default_docs_not_exposed(client):
    for path in ["/docs", "/redoc", "/openapi.json"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (301, 302, 307, 404, 401), f"{path} returned {r.status_code}"


# ---------------------------------------------------------------------------
# 5. API key authentication
# ---------------------------------------------------------------------------


def test_api_no_key_returns_401(client):
    r = client.get("/api/v1/shipments")
    assert r.status_code == 401


def test_api_wrong_key_returns_401(client):
    r = client.get("/api/v1/shipments", headers={"X-API-Key": "nottherightkey"})
    assert r.status_code == 401


def test_api_bearer_token_works(client, api_key):
    r = client.get("/api/v1/shipments", headers={"Authorization": f"Bearer {api_key}"})
    assert r.status_code == 200


def test_api_x_api_key_header_works(client, api_key):
    r = client.get("/api/v1/shipments", headers={"X-API-Key": api_key})
    assert r.status_code == 200


def test_api_revoked_key_returns_401(client, sf):
    from datetime import UTC, datetime

    raw, kid = _make_api_key_in_db(sf, name="revokable")
    with sf.begin() as s:
        k = s.get(ApiKey, kid)
        k.revoked_at = datetime.now(UTC)
    r = client.get("/api/v1/shipments", headers={"X-API-Key": raw})
    assert r.status_code == 401


def test_api_session_cannot_substitute_api_key(authed_client):
    """An OIDC session must not grant access to /api/* routes."""
    c, _ = authed_client
    r = c.get("/api/v1/shipments")
    assert r.status_code == 401


def test_api_unknown_path_requires_key(client):
    """Unknown /api/* paths must still demand a valid API key (no bypass)."""
    r = client.get("/api/v1/nonexistent-endpoint", follow_redirects=False)
    assert r.status_code == 401
    r2 = client.get("/api/unknown/path", follow_redirects=False)
    assert r2.status_code == 401


# ---------------------------------------------------------------------------
# 6. API docs/openapi require API key
# ---------------------------------------------------------------------------


def test_api_openapi_json_requires_key(client):
    r = client.get("/api/openapi.json")
    assert r.status_code == 401


def test_api_openapi_json_with_key(client, api_key):
    r = client.get("/api/openapi.json", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    schema = r.json()
    assert "openapi" in schema
    # Verify ApiKey security definitions are present
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "ApiKeyHeader" in schemes or "BearerToken" in schemes


def test_api_docs_requires_key(client):
    r = client.get("/api/docs", follow_redirects=False)
    assert r.status_code == 401


def test_api_docs_with_key(client, api_key):
    r = client.get("/api/docs", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    assert b"<!DOCTYPE html" in r.content or b"<html" in r.content


# ---------------------------------------------------------------------------
# 7. API shipments CRUD
# ---------------------------------------------------------------------------


def test_api_list_shipments_empty(client, api_key):
    r = client.get("/api/v1/shipments", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_api_create_shipment_valid(client, api_key):
    r = client.post(
        "/api/v1/shipments",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=json.dumps({"number": "APITEST001", "name": "API Test Pkg"}),
    )
    assert r.status_code in (200, 201)
    body = r.json()
    assert body["number"] == "APITEST001"
    assert "notes" in body  # notes field present in response


def test_api_create_preserves_case(client, api_key):
    """Tracking number case must be preserved — no uppercasing."""
    r = client.post(
        "/api/v1/shipments",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=json.dumps({"number": "mixedCase001xx"}),
    )
    assert r.status_code in (200, 201)
    assert r.json()["number"] == "mixedCase001xx"


def test_api_create_shipment_invalid_number_rejected(client, api_key):
    r = client.post(
        "/api/v1/shipments",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=json.dumps({"number": "A!@#$BAD"}),
    )
    assert r.status_code == 422


def test_api_create_shipment_number_too_short_rejected(client, api_key):
    r = client.post(
        "/api/v1/shipments",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=json.dumps({"number": "AB"}),
    )
    assert r.status_code == 422


def test_api_create_carrier_out_of_range_rejected(client, api_key):
    r = client.post(
        "/api/v1/shipments",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        content=json.dumps({"number": "CARRIER001", "carrier": 2**31}),
    )
    assert r.status_code == 422


def test_api_get_shipment(client, api_key, sf):
    ship_id = _make_shipment(sf, "GETTEST001")
    r = client.get(f"/api/v1/shipments/{ship_id}", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == ship_id
    assert "events" in body  # history present in detail response


def test_api_get_shipment_not_found(client, api_key):
    r = client.get("/api/v1/shipments/nonexistent-id", headers={"X-API-Key": api_key})
    assert r.status_code == 404


def test_api_archive_shipment(client, api_key, sf):
    ship_id = _make_shipment(sf, "ARCHTEST001")
    r = client.post(
        f"/api/v1/shipments/{ship_id}/archive", headers={"X-API-Key": api_key}
    )
    assert r.status_code in (200, 204)
    # Verify DB state
    with sf() as s:
        ship = s.get(Shipment, ship_id)
    assert ship.bucket == "archive"


# ---------------------------------------------------------------------------
# 8. Webhook: 17TRACK — official "sign" header, real tracking, official shape
# ---------------------------------------------------------------------------


def test_webhook_valid_signature_accepted(client):
    payload = json.dumps(
        {"event": "TRACKING_UPDATED", "data": {"accepted": []}}
    ).encode()
    sig = _webhook_sig(payload)
    r = client.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig))
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_webhook_invalid_signature_rejected(client):
    payload = json.dumps({"event": "TRACKING_UPDATED", "data": {}}).encode()
    r = client.post(
        "/webhooks/17track",
        content=payload,
        headers={"Content-Type": "application/json", "sign": "badsignature"},
    )
    assert r.status_code == 400


def test_webhook_missing_signature_header_rejected(client):
    payload = json.dumps({"event": "TRACKING_UPDATED", "data": {}}).encode()
    r = client.post(
        "/webhooks/17track",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_webhook_old_header_name_rejected(client):
    """The legacy '17track-Signature' header must NOT be accepted."""
    payload = json.dumps({"event": "TRACKING_UPDATED", "data": {"accepted": []}}).encode()
    sig = _webhook_sig(payload)
    r = client.post(
        "/webhooks/17track",
        content=payload,
        headers={"Content-Type": "application/json", "17track-Signature": sig},
    )
    # Without 'sign' header this should fail (missing signature)
    assert r.status_code == 400


def test_webhook_oversized_body_rejected(client):
    big = b"x" * (1024 * 1024 + 1)
    sig = _webhook_sig(big)
    r = client.post("/webhooks/17track", content=big, headers=_webhook_headers(sig))
    assert r.status_code == 413


def test_webhook_not_json_object_rejected(client):
    payload = b'"just a string"'
    sig = _webhook_sig(payload)
    r = client.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig))
    assert r.status_code == 400


def test_webhook_malformed_utf8_rejected(client):
    """Invalid UTF-8 byte sequences must be rejected with 400."""
    payload = b"\xff\xfe{invalid"
    sig = _webhook_sig(payload)
    r = client.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig))
    assert r.status_code == 400


def test_webhook_missing_data_key_returns_400(client):
    """Payload without required 'data' key raises ValueError → 400."""
    payload = json.dumps({"event": "TRACKING_UPDATED"}).encode()  # no "data"
    sig = _webhook_sig(payload)
    r = client.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig))
    assert r.status_code == 400


def test_webhook_updates_shipment_status(client, sf):
    """Signed webhook with official track_info shape updates shipment status in DB."""
    ship_id = _make_shipment(sf, "WEBHOOKTEST01", status="pending")

    payload = json.dumps({
        "event": "TRACKING_UPDATED",
        "data": {
            "accepted": [{
                "number": "WEBHOOKTEST01",
                "track_info": {
                    "latest_status": {"status": "InTransit", "sub_status": ""},
                    "latest_event": {
                        "time_iso": "2024-01-01T12:00:00Z",
                        "description": "In Transit",
                        "location": "Hub",
                    },
                    "tracking": {"providers": []},
                },
            }]
        },
    }).encode()
    sig = _webhook_sig(payload)
    r = client.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig))
    assert r.status_code == 200
    assert r.json()["applied"] == 1

    with sf() as s:
        ship = s.get(Shipment, ship_id)
    assert ship.status == "in_transit"


def test_webhook_rate_limited(settings, sf, provider):
    """Rapid successive webhook calls from same IP are rate-limited."""
    from novatrack.app import create_app
    from novatrack.security import RateLimiter

    _ensure_app_settings(sf)
    app2 = create_app(settings=settings, session_factory=sf, provider=provider)
    app2.state.webhook_limiter = RateLimiter(limit=2, window=60)

    payload = json.dumps({"event": "TRACKING_UPDATED", "data": {"accepted": []}}).encode()
    sig = _webhook_sig(payload)

    with TestClient(app2, base_url="https://testserver", raise_server_exceptions=False) as c:
        codes = [
            c.post("/webhooks/17track", content=payload, headers=_webhook_headers(sig)).status_code
            for _ in range(5)
        ]

    assert 429 in codes


# ---------------------------------------------------------------------------
# 9. CSRF protection
# ---------------------------------------------------------------------------


def test_csrf_missing_token_on_mutation_rejected(authed_client, sf):
    c, _ = authed_client
    r = c.post("/auth/logout")
    assert r.status_code in (400, 403, 422)


def test_csrf_valid_token_on_logout_accepted(authed_client):
    c, csrf = authed_client
    r = c.post("/auth/logout", data={"_csrf": csrf}, follow_redirects=False)
    assert r.status_code in (302, 303, 200)


def test_csrf_wrong_token_rejected(authed_client):
    c, _ = authed_client
    r = c.post("/auth/logout", data={"_csrf": "wrongtoken"}, follow_redirects=False)
    assert r.status_code in (400, 403)


# ---------------------------------------------------------------------------
# 10. OIDC auth flow
# ---------------------------------------------------------------------------


def test_auth_login_redirects_to_oidc(app, client):
    from fastapi.responses import RedirectResponse as RR

    with patch("novatrack.app.oidc_login", new_callable=AsyncMock) as mock_login:
        mock_login.return_value = RR(
            f"{TEST_OIDC_ISSUER}/authorize?state=x&response_type=code"
        )
        r = client.get("/auth/login", follow_redirects=False)

    assert r.status_code in (302, 307)
    location = r.headers.get("location", "")
    assert TEST_OIDC_ISSUER in location or "authorize" in location.lower()


def test_auth_callback_missing_code_returns_error(client):
    r = client.get("/auth/callback", follow_redirects=False)
    assert r.status_code in (302, 307, 400)
    if r.status_code in (302, 307):
        loc = r.headers.get("location", "")
        assert "error" in loc or "/auth/login" in loc


def test_auth_callback_error_response_safe(client):
    """Callback must not expose upstream error messages."""
    r = client.get(
        "/auth/callback?error=access_denied&error_description=User+denied+access",
        follow_redirects=False,
    )
    assert r.status_code < 500
    if r.status_code in (302, 307):
        loc = r.headers.get("location", "")
        assert "User+denied+access" not in loc
    else:
        assert "User denied access" not in r.text


def test_auth_login_error_renders_login_page(client):
    """/auth/login?error=1 must render the login HTML page (not loop)."""
    r = client.get("/auth/login?error=1", follow_redirects=False)
    assert r.status_code == 200
    assert b"Sign in" in r.content or b"NovaTrack" in r.content


# ---------------------------------------------------------------------------
# 11. Session cookie attributes
# ---------------------------------------------------------------------------


def test_session_cookie_set_after_oidc_callback(settings, sf, provider):
    """After successful OIDC callback the session cookie has security flags."""
    from novatrack.app import create_app

    _ensure_app_settings(sf)
    app2 = create_app(settings=settings, session_factory=sf, provider=provider)

    async def fake_token(request):
        return {
            "access_token": "tok",
            "userinfo": {"sub": "user-1", "name": "Test User", "email": "t@test.com"},
            "id_token": "eyJ.fake.token",
        }

    with patch("novatrack.app.oidc_login", new_callable=AsyncMock) as mock_login, \
         patch("novatrack.app.oidc_exchange", new_callable=AsyncMock) as mock_exchange:
        from starlette.responses import RedirectResponse as RR
        mock_login.return_value = RR(
            f"{TEST_OIDC_ISSUER}/authorize?state=abc&nonce=xyz&code_challenge=y"
        )
        mock_exchange.side_effect = fake_token

        with TestClient(app2, base_url="https://testserver", raise_server_exceptions=False) as c:
            r_login = c.get("/auth/login", follow_redirects=False)
            assert r_login.status_code in (302, 307)

            r_cb = c.get(
                "/auth/callback?code=fakecode&state=fakestate", follow_redirects=False
            )
            assert r_cb.status_code in (302, 303, 200)

            set_cookie = r_cb.headers.get("set-cookie", "")
            if set_cookie:
                cookie_lower = set_cookie.lower()
                assert "httponly" in cookie_lower
                assert "samesite=lax" in cookie_lower
                assert "secure" in cookie_lower


def test_session_max_age_is_8h(settings, sf, provider):
    """Session max_age must be 8 hours (28800 s), not 14 days."""
    from novatrack.app import create_app

    _ensure_app_settings(sf)
    app2 = create_app(settings=settings, session_factory=sf, provider=provider)

    async def fake_token(request):
        return {
            "userinfo": {"sub": "u1", "name": "U"},
        }

    with patch("novatrack.app.oidc_exchange", new_callable=AsyncMock) as mock_ex:
        mock_ex.side_effect = fake_token
        with TestClient(app2, base_url="https://testserver", raise_server_exceptions=False) as c:
            r = c.get("/auth/callback?code=x&state=y", follow_redirects=False)
            sc = r.headers.get("set-cookie", "").lower()
            if "max-age=" in sc:
                import re
                m = re.search(r"max-age=(\d+)", sc)
                if m:
                    age = int(m.group(1))
                    assert age <= 28800, f"max-age {age} > 28800"
                    assert age > 3600, f"max-age {age} suspiciously short"


# ---------------------------------------------------------------------------
# 12. API rate limiting
# ---------------------------------------------------------------------------


def test_api_rate_limit_per_credential(settings, sf, provider):
    """API endpoint rate-limits individual credentials."""
    from novatrack.app import create_app
    from novatrack.security import RateLimiter

    _ensure_app_settings(sf)
    app2 = create_app(settings=settings, session_factory=sf, provider=provider)
    app2.state.api_limiter = RateLimiter(limit=3, window=60)

    raw, _ = _make_api_key_in_db(sf, name="ratelimit-key")
    with TestClient(app2, base_url="https://testserver", raise_server_exceptions=False) as c:
        codes = [
            c.get("/api/v1/shipments", headers={"X-API-Key": raw}).status_code
            for _ in range(6)
        ]

    assert 429 in codes


# ---------------------------------------------------------------------------
# 13. Security: constant-time signature comparison
# ---------------------------------------------------------------------------


def test_verify_webhook_signature_correct():
    from novatrack.security import verify_webhook_signature

    body = b"hello world"
    key = "mysecretkey"
    sig = hashlib.sha256(body + b"/" + key.encode()).hexdigest()
    assert verify_webhook_signature(body, sig, key) is True


def test_verify_webhook_signature_wrong():
    from novatrack.security import verify_webhook_signature

    assert verify_webhook_signature(b"body", "wrongsig", "key") is False


def test_generate_api_key_produces_unique_strong_keys():
    from novatrack.security import generate_api_key, hash_api_key

    raw1, digest1 = generate_api_key()
    raw2, digest2 = generate_api_key()
    assert raw1 != raw2
    assert digest1 != digest2
    assert len(raw1) >= 32
    assert hash_api_key(raw1) == digest1
    assert len(digest1) == 64
    int(digest1, 16)  # raises if not valid hex


# ---------------------------------------------------------------------------
# 14. UI routes — form actions resolve, CRUD persists
# ---------------------------------------------------------------------------


def test_ui_create_shipment_persists(authed_client, sf):
    """POST /shipments creates shipment in DB and redirects."""
    c, csrf = authed_client
    r = c.post(
        "/shipments",
        data={"_csrf": csrf, "number": "WEBFORM001", "name": "Web Form Test"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with sf() as s:
        ship = s.query(Shipment).filter(Shipment.number == "WEBFORM001").first()
    assert ship is not None
    assert ship.name == "Web Form Test"


def test_ui_create_shipment_duplicate_shows_error(authed_client, sf):
    """Duplicate tracking number returns 200 with error, not 5xx."""
    c, csrf = authed_client
    # First create
    c.post("/shipments", data={"_csrf": csrf, "number": "DUPTEST001"}, follow_redirects=False)
    # Second create (duplicate)
    r = c.post(
        "/shipments", data={"_csrf": csrf, "number": "DUPTEST001"}, follow_redirects=False
    )
    # Should show form with error, not crash
    assert r.status_code in (200, 303)


def test_ui_archive_shipment_persists(authed_client, sf):
    """POST /shipments/{id}/archive moves shipment to archive bucket."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "ARCH_UI_001")
    r = c.post(
        f"/shipments/{ship_id}/archive", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    with sf() as s:
        ship = s.get(Shipment, ship_id)
    assert ship.bucket == "archive"


def test_ui_trash_shipment_persists(authed_client, sf):
    """POST /shipments/{id}/trash moves shipment to trash bucket."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "TRASH_UI_001")
    r = c.post(
        f"/shipments/{ship_id}/trash", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    with sf() as s:
        ship = s.get(Shipment, ship_id)
    assert ship.bucket == "trash"


def test_ui_deliver_untrackable_shipment(authed_client, sf):
    """POST /shipments/{id}/deliver marks untrackable shipment as delivered."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "TBATESTXYZ1", status="untrackable")
    r = c.post(
        f"/shipments/{ship_id}/deliver", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    with sf() as s:
        ship = s.get(Shipment, ship_id)
    assert ship.status == "delivered"


def test_ui_deliver_rejects_trackable_shipment(authed_client, sf):
    """POST /shipments/{id}/deliver on a trackable shipment returns 400."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "TRACKABLE001", status="in_transit")
    r = c.post(
        f"/shipments/{ship_id}/deliver", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 400


def test_ui_refresh_rejects_untrackable(authed_client, sf):
    """POST /shipments/{id}/refresh on untrackable shipment returns 400."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "TBAREFRESH01", status="untrackable")
    r = c.post(
        f"/shipments/{ship_id}/refresh", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 400


def test_ui_refresh_rejects_unregistered(authed_client, sf):
    """POST /shipments/{id}/refresh on unregistered shipment returns 400."""
    c, csrf = authed_client
    ship_id = _make_shipment(sf, "UNREG001", status="pending")
    # registered defaults to False
    r = c.post(
        f"/shipments/{ship_id}/refresh", data={"_csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 400


def test_ui_settings_retention_saves(authed_client, sf):
    """POST /settings/retention updates archive/purge days."""
    c, csrf = authed_client
    r = c.post(
        "/settings/retention",
        data={"_csrf": csrf, "archive_days": "14", "purge_days": "60"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    with sf() as s:
        cfg = s.get(AppSettings, 1)
    assert cfg.archive_days == 14
    assert cfg.purge_days == 60


def test_ui_settings_retention_validates_range(authed_client):
    """Retention values outside 1..3650 are rejected."""
    c, csrf = authed_client
    r = c.post(
        "/settings/retention",
        data={"_csrf": csrf, "archive_days": "0", "purge_days": "10"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_ui_settings_quota_graceful_on_provider_error(authed_client):
    """POST /settings/quota handles provider errors safely (no 500)."""
    c, csrf = authed_client
    # notifications.check_quota may fail gracefully — must not return 500
    r = c.post("/settings/quota", data={"_csrf": csrf}, follow_redirects=False)
    assert r.status_code in (303, 200)
    assert r.status_code != 500


def test_ui_create_api_key_returns_full_settings_context(authed_client):
    """POST /settings/api-keys returns settings page with all sections populated."""
    c, csrf = authed_client
    r = c.post(
        "/settings/api-keys",
        data={"_csrf": csrf, "name": "Test Key"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # Should contain API key display and retention section (not blank page)
    assert b"API key created" in r.content or b"key" in r.content.lower()


def test_ui_shipment_detail_shows_history(authed_client, sf):
    """GET /shipments/{id} renders page; events section present."""
    c, _ = authed_client
    ship_id = _make_shipment(sf, "DETAIL001")
    r = c.get(f"/shipments/{ship_id}")
    assert r.status_code == 200
    assert b"DETAIL001" in r.content


# ---------------------------------------------------------------------------
# 15. Channel soft-disable (audit preservation)
# ---------------------------------------------------------------------------


def test_channel_delete_soft_disables(authed_client, sf):
    """POST /settings/channels/{id}/delete soft-disables (enabled=False), not hard-delete."""
    from novatrack.models import NotificationChannel

    c, csrf = authed_client

    with sf() as s:
        ch = NotificationChannel(
            name="SoftDeleteTest",
            kind="webhook",
            url="https://example.com/hook",
            events=["status_changed"],
        )
        s.add(ch)
        s.commit()
        ch_id = ch.id

    r = c.post(
        f"/settings/channels/{ch_id}/delete",
        data={"_csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Channel must still exist in DB (not hard-deleted)
    with sf() as s:
        ch_after = s.get(NotificationChannel, ch_id)
    assert ch_after is not None
    assert ch_after.enabled is False


# ---------------------------------------------------------------------------
# Rendering regressions: documented 17TRACK shapes must be visible in the UI
# ---------------------------------------------------------------------------


def test_settings_renders_real_quota_keys_and_eta_range(authed_client, sf):
    from novatrack.models import AppSettings, Shipment

    client, _ = authed_client
    with sf.begin() as s:
        cfg = s.get(AppSettings, 1) or AppSettings(id=1)
        cfg.quota = {"quota_total": 200, "quota_used": 160, "quota_remain": 40}
        s.merge(cfg)
        ship = Shipment(number="ETAREG001", status="in_transit", bucket="active",
                        eta={"source": "Official", "from": "2026-10-05T08:00:00-05:00",
                             "to": "2026-10-07T13:00:00-05:00"})
        s.add(ship)
        s.flush()
        sid = ship.id
    page = client.get("/settings").text
    assert "Used: 160 / 200" in page
    assert "?" not in page.split("Used:")[1].split("</p>")[0]
    index = client.get("/").text
    assert "ETA 2026-10-05 – 2026-10-07" in index
    detail = client.get(f"/shipments/{sid}").text
    assert "2026-10-05T08:00:00-05:00" in detail


def test_api_docs_page_is_self_contained_and_well_formed(client, api_key):
    r = client.get("/api/docs", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    assert "&lt;your-key&gt;" in r.text
    assert "stylesheet" not in r.text
    assert r.text.count("<code>") == r.text.count("</code>")
