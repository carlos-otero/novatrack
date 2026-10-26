"""Real Authlib authorization-code/PKCE/JWKS validation against a simulated IdP.

Only the HTTP boundary is replaced. No oauth/oidc_exchange/session-auth mocks.
"""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from itsdangerous import TimestampSigner
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.testclient import TestClient

from novatrack.config import Settings
from novatrack.db import make_engine, make_session_factory
from novatrack.models import AppSettings, Base


@pytest.fixture
def oidc_harness():
    from novatrack.app import create_app
    issuer = "https://idp.example.org/application/o/novatrack"
    cfg = Settings(database_url="sqlite://", oidc_issuer=issuer, oidc_client_id="novatrack-test",
                   oidc_client_secret="client-test-secret", app_base_url="https://app.example.org",
                   secret_key="test-session-secret-" * 3, track17_api_key="test-provider-key")
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    with sf.begin() as session:
        session.add(AppSettings(id=1))
    app = create_app(cfg, sf, provider=object())
    signing_key = RSAKey.generate_key(2048, {"kid": "test-idp-signing-key"})
    wrong_key = RSAKey.generate_key(2048, {"kid": "test-idp-signing-key"})
    state = {"mode": "valid", "nonce": "", "challenge": "", "token_requests": 0, "calls": []}

    def idp(request):
        state["calls"].append(request.url.path)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": issuer,
                "authorization_endpoint": "https://idp.example.org/authorize",
                "token_endpoint": "https://idp.example.org/token",
                "jwks_uri": "https://idp.example.org/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
            })
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [signing_key.as_dict()]})
        if path == "/token":
            state["token_requests"] += 1
            body = parse_qs(request.content.decode())
            assert body["grant_type"] == ["authorization_code"]
            assert body["code"] == ["simulated-authorization-code"]
            assert body["redirect_uri"] == [cfg.app_base_url + "/auth/callback"]
            verifier = body["code_verifier"][0]
            expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            assert expected_challenge == state["challenge"]
            now = int(time.time())
            claims = {"iss": issuer, "sub": "household-user", "aud": cfg.oidc_client_id,
                      "exp": now + 300, "iat": now, "nonce": state["nonce"], "name": "Test household"}
            if state["mode"] == "wrong_nonce":
                claims["nonce"] = "unrelated-nonce"
            if state["mode"] == "wrong_issuer":
                claims["iss"] = "https://untrusted.example.org"
            if state["mode"] == "wrong_audience":
                claims["aud"] = "unrelated-client"
            if state["mode"] == "expired":
                claims["exp"] = now - 600
            key = wrong_key if state["mode"] == "wrong_signature" else signing_key
            signed = jwt.encode({"alg": "RS256", "kid": "test-idp-signing-key"}, claims, key)
            return httpx.Response(200, json={"access_token": "simulated-idp-access-token",
                                           "token_type": "Bearer", "expires_in": 300, "id_token": signed})
        raise AssertionError("Unexpected IdP HTTP path: " + path)

    app.state.oauth.create_client("oidc").client_kwargs["transport"] = httpx.MockTransport(idp)
    with TestClient(app, base_url=cfg.app_base_url) as client:
        yield client, cfg, state
    engine.dispose()


def begin_login(client, cfg, state):
    response = client.get("/auth/login", follow_redirects=False,
                          headers={"X-Forwarded-Host": "attacker.example.org", "X-Forwarded-Proto": "http"})
    assert response.status_code == 302
    redirect = response.headers["location"]
    assert urlsplit(redirect).netloc == "idp.example.org"
    params = parse_qs(urlsplit(redirect).query)
    assert params["redirect_uri"] == [cfg.app_base_url + "/auth/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert {"openid", "profile", "email"} <= set(params["scope"][0].split())
    state["nonce"] = params["nonce"][0]
    state["challenge"] = params["code_challenge"][0]
    return params["state"][0]


def test_real_oidc_handshake_authenticates_without_storing_provider_tokens(oidc_harness):
    client, cfg, state = oidc_harness
    returned_state = begin_login(client, cfg, state)
    response = client.get("/auth/callback", params={"state": returned_state, "code": "simulated-authorization-code"},
                          follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert state["token_requests"] == 1
    assert "/jwks" in state["calls"]
    assert client.get("/").status_code == 200
    raw_cookie = client.cookies.get("novatrack_session")
    unsigned = TimestampSigner(cfg.secret_key.get_secret_value()).unsign(raw_cookie)
    data = json.loads(base64.b64decode(unsigned))
    assert data["user"]["sub"] == "household-user"
    assert data["csrf"]
    serialized = json.dumps(data)
    assert "simulated-idp-access-token" not in serialized
    assert "id_token" not in serialized
    assert "client-test-secret" not in serialized
    assert not any(k.startswith("_state_") for k in data)
    # Authlib consumes state; replay never creates a new token/session.
    replay = client.get("/auth/callback", params={"state": returned_state, "code": "simulated-authorization-code"},
                        follow_redirects=False)
    assert replay.headers["location"] != "/"
    assert state["token_requests"] == 1


@pytest.mark.parametrize("mode", ["wrong_nonce", "wrong_issuer", "wrong_audience", "expired", "wrong_signature", "wrong_state"])
def test_oidc_rejects_invalid_state_and_signed_claims(oidc_harness, mode, caplog):
    client, cfg, state = oidc_harness
    state["mode"] = mode
    returned_state = begin_login(client, cfg, state)
    if mode == "wrong_state":
        returned_state = "not-the-saved-browser-state"
    response = client.get("/auth/callback", params={"state": returned_state, "code": "simulated-authorization-code"},
                          follow_redirects=False)
    assert response.status_code in {302, 400, 401, 403}
    assert response.headers.get("location") != "/"
    assert client.get("/", follow_redirects=False).status_code in {302, 307}
    assert "simulated-idp-access-token" not in caplog.text
    assert "client-test-secret" not in caplog.text
    assert "simulated-authorization-code" not in caplog.text
    if mode == "wrong_state":
        assert state["token_requests"] == 0
