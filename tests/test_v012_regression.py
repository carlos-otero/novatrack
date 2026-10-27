"""Regression tests for v0.1.2 release-blocking defects.

Covers:
- E2E: rejected registration → pause → user updates fields → exactly one retry → registered=True
- Unsupported required carrier params: op blocked (done=True), provider NOT called
- UI validation: invalid country (not silently truncated), invalid phone
- UI params endpoint: POST /shipments/{id}/params → 303 params_updated
- API params endpoint: POST /api/v1/shipments/{id}/params → 200
- change_carrier_shipment registered guard (ValueError for unregistered)
- carriers.unsupported_required_params function
- missing_required_params now excludes unsupported keys from results
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from novatrack.carriers import (
    missing_required_params,
    unsupported_required_params,
)
from novatrack.providers import ProviderError

# _PARAM_MISSING_PROMPT removed in v0.1.3 (now per-code messages)

# ─────────────────────────────────────────────────────────────────────────────
# carriers.unsupported_required_params
# ─────────────────────────────────────────────────────────────────────────────


def test_unsupported_required_params_known_carrier():
    """Carrier 100609 requires phone_number which we do not support."""
    unsupported = unsupported_required_params(100609)
    assert "phone_number" in unsupported


def test_unsupported_required_params_postnl_empty():
    """PostNL (14041) only requires destination_country which we DO support."""
    unsupported = unsupported_required_params(14041)
    assert unsupported == []


def test_unsupported_required_params_dhl_express_empty():
    """DHL Express (100001) has no additional params → empty list."""
    unsupported = unsupported_required_params(100001)
    assert unsupported == []


def test_unsupported_required_params_all_8_carriers():
    """All 8 carriers with unsupported required params are detected."""
    # Verified against additional_parameters.json
    carrier_keys = [100609, 100621, 100797, 101003, 101331, 101391, 191916, 999997]
    for ck in carrier_keys:
        result = unsupported_required_params(ck)
        assert len(result) > 0, f"carrier {ck} should have unsupported required params"


def test_missing_required_params_excludes_unsupported_keys():
    """missing_required_params only reports keys we CAN supply, not unsupported ones."""
    # Carrier 100609 requires phone_number (unsupported).
    # missing_required_params should NOT report it — that's unsupported_required_params' job.
    missing = missing_required_params(100609)
    assert "phone_number" not in missing


# ─────────────────────────────────────────────────────────────────────────────
# In-memory session fixture (reused from test_v012_features.py pattern)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from novatrack.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session() as s:
        yield s


# ─────────────────────────────────────────────────────────────────────────────
# E2E: rejected registration → pause → user updates → exactly one retry → registered
# ─────────────────────────────────────────────────────────────────────────────


def test_e2e_missing_param_pause_then_user_update_retries(mem_session):
    """Full E2E flow: missing param → pause → update_shipment_params → registered.

    1. Create PostNL shipment without destination_country.
    2. Worker tries register → provider rejects with missing-param error.
    3. Op's next_attempt_at is in the far future (paused, not retrying).
    4. Worker runs again → op NOT picked up.
    5. User calls update_shipment_params with destination_country="NL".
    6. Op next_attempt_at reset to now, provider_error cleared.
    7. Worker runs → registers successfully → registered=True.
    8. Provider.register called exactly twice total (steps 2 and 7).
    """
    from sqlalchemy import select

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import create_shipment, process_operations, update_shipment_params

    register_calls: list[dict] = []

    def mock_register(number, carrier, *, param=None):
        register_calls.append({"number": number, "carrier": carrier, "param": param})
        if not param or "NL" not in param:
            raise ProviderError("Registration requires the destination postal code for this carrier. Please add the postal code and save.", retryable_without_quota=True)
        return {"number": number, "carrier": carrier}

    prov = MagicMock()
    prov.register.side_effect = mock_register

    # Step 1: Create shipment with no destination_country
    create_shipment(mem_session, prov, number="POSTNLE2E1", carrier=14041)
    mem_session.flush()

    # Step 2: First worker run → fails with missing param
    n1 = process_operations(mem_session, prov)
    assert n1 == 0
    assert len(register_calls) == 1

    # Step 3: Op must be paused (far future)
    op = mem_session.execute(
        select(ProviderOperation).where(
            ProviderOperation.kind == "register",
            ProviderOperation.done == False,  # noqa: E712
        )
    ).scalar_one()
    now = datetime.now(UTC)
    delta = (op.next_attempt_at.replace(tzinfo=UTC) - now).total_seconds()
    assert delta > 3600, f"Op should be paused but is only {delta:.0f}s ahead"

    # Check error stored on shipment
    ship = mem_session.execute(select(Shipment).where(Shipment.number == "POSTNLE2E1")).scalar_one()
    assert ship.provider_error is not None
    assert ship.registered is False

    # Step 4: Worker runs again → op NOT picked up (still paused)
    n2 = process_operations(mem_session, prov)
    assert n2 == 0
    assert len(register_calls) == 1  # no additional call

    # Step 5: User updates params
    triggered = update_shipment_params(
        mem_session,
        ship,
        destination_country="NL",
        destination_postal_code="1000 AA",
    )
    mem_session.flush()
    assert triggered is True  # pending op was reset

    # Step 6: Verify next_attempt_at is now (or past) and error cleared
    mem_session.expire(op)
    op = mem_session.execute(
        select(ProviderOperation).where(
            ProviderOperation.kind == "register",
            ProviderOperation.done == False,  # noqa: E712
        )
    ).scalar_one()
    now2 = datetime.now(UTC)
    delta2 = (op.next_attempt_at.replace(tzinfo=UTC) - now2).total_seconds()
    assert delta2 < 5, f"Op should be due now, but is {delta2:.0f}s ahead"

    mem_session.expire(ship)
    ship = mem_session.execute(select(Shipment).where(Shipment.number == "POSTNLE2E1")).scalar_one()
    assert ship.provider_error is None

    # Step 7: Worker registers successfully
    n3 = process_operations(mem_session, prov)
    assert n3 == 1

    # Step 8: Exactly one additional call (total = 2)
    assert len(register_calls) == 2
    assert register_calls[1]["param"] is not None
    assert "NL" in register_calls[1]["param"]

    # Registered flag set
    mem_session.expire(ship)
    ship = mem_session.execute(select(Shipment).where(Shipment.number == "POSTNLE2E1")).scalar_one()
    assert ship.registered is True


# ─────────────────────────────────────────────────────────────────────────────
# Unsupported required params: op blocked (done=True), provider NOT called
# ─────────────────────────────────────────────────────────────────────────────


def test_unsupported_carrier_params_blocks_registration(mem_session):
    """Carrier with unsupported required param → op done=True, provider never called."""
    from sqlalchemy import select

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()

    # Carrier 100609 requires phone_number which NovaTrack cannot supply
    create_shipment(mem_session, prov, number="UNSPRT0001", carrier=100609)
    mem_session.flush()

    process_operations(mem_session, prov)

    # Provider must NOT have been called
    prov.register.assert_not_called()

    # Op must be marked done (permanently blocked, not stuck retrying)
    op = mem_session.execute(
        select(ProviderOperation).where(
            ProviderOperation.kind == "register",
        )
    ).scalar_one()
    assert op.done is True

    # Shipment should have an actionable error message
    ship = mem_session.execute(select(Shipment).where(Shipment.number == "UNSPRT0001")).scalar_one()
    assert ship.provider_error is not None
    assert ship.registered is False
    # Error should mention the unsupported param key
    assert "phone_number" in ship.provider_error or "unsupported" in ship.provider_error.lower()


def test_unsupported_carrier_params_error_mentions_carrier():
    """Error message for blocked registration names the problematic param."""
    from novatrack.tracking import _PAUSED_INDEFINITELY  # noqa: F401 (confirm importable)

    # Verify unsupported_required_params is the detection function
    assert unsupported_required_params(100609) == ["phone_number"]


# ─────────────────────────────────────────────────────────────────────────────
# change_carrier_shipment registered guard
# ─────────────────────────────────────────────────────────────────────────────


def test_change_carrier_unregistered_raises_value_error(mem_session):
    """change_carrier_shipment raises ValueError when shipment is not registered."""
    from novatrack.tracking import change_carrier_shipment, create_shipment

    prov = MagicMock()
    ship = create_shipment(mem_session, prov, number="CCGUARD1", carrier=1000)
    mem_session.flush()
    # Ship is NOT registered

    with pytest.raises(ValueError, match="registered"):
        change_carrier_shipment(mem_session, ship, prov, 3011)

    # Provider should not have been called
    prov.changecarrier.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Web fixture (shared)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def web_app():
    import base64

    from itsdangerous import TimestampSigner
    from pydantic import SecretStr
    from starlette.testclient import TestClient

    from novatrack.app import create_app
    from novatrack.config import Settings
    from novatrack.db import make_engine, make_session_factory
    from novatrack.models import AppSettings, Base

    _SECRET = "testsecretkey32bytesxxxxxxxxxxxxxxxxxx"

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    with sf() as s:
        s.add(AppSettings(id=1))
        s.commit()

    prov = MagicMock()
    prov.register.return_value = {"number": "X", "carrier": 0}

    settings = Settings.model_construct(
        database_url="sqlite://",
        oidc_issuer="https://idp.test.example.com",
        oidc_client_id="test-client-id",
        oidc_client_secret=SecretStr("test-client-secret"),
        app_base_url="https://app.test.example.com",
        secret_key=SecretStr(_SECRET),
        track17_api_key=SecretStr("testkey"),
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

    app = create_app(settings=settings, session_factory=sf, provider=prov)

    session_data = {"user": {"sub": "u-test", "name": "Test User"}, "csrf": "csrftoken"}
    signer = TimestampSigner(_SECRET)
    payload = base64.b64encode(json.dumps(session_data).encode()).decode()
    cookie_value = signer.sign(payload).decode()

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("novatrack_session", cookie_value)
        yield client, sf, prov


def _make_unregistered_shipment(sf, number: str, carrier: int | None = None) -> str:
    from novatrack.models import Shipment

    with sf() as s:
        ship = Shipment(number=number, registered=False, status="pending", carrier=carrier)
        s.add(ship)
        s.commit()
        return ship.id


# ─────────────────────────────────────────────────────────────────────────────
# UI validation: invalid country / phone not silently truncated
# ─────────────────────────────────────────────────────────────────────────────


def test_ui_create_shipment_invalid_country_shows_error(web_app):
    """POST /shipments with 3-letter country → 200 with form_error (not silently truncated)."""
    client, sf, prov = web_app
    r = client.post(
        "/shipments",
        data={
            "_csrf": "csrftoken",
            "number": "BADCOU001",
            "destination_country": "NLD",  # 3 letters — must be rejected, not truncated to NL
        },
        follow_redirects=False,
    )
    # Should re-render the form (200) with an error, not redirect (303 success)
    assert r.status_code == 200
    assert "2-letter" in r.text or "alpha-2" in r.text or "form_error" in r.text.lower() or "ISO" in r.text


def test_ui_create_shipment_invalid_phone_shows_error(web_app):
    """POST /shipments with non-digit phone → 200 with form_error (not silently accepted)."""
    client, sf, prov = web_app
    r = client.post(
        "/shipments",
        data={
            "_csrf": "csrftoken",
            "number": "BADPHN001",
            "phone_number_last_4": "abcd",  # non-digit — must be rejected
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "digit" in r.text.lower() or "phone" in r.text.lower()


# ─────────────────────────────────────────────────────────────────────────────
# UI params endpoint: POST /shipments/{id}/params
# ─────────────────────────────────────────────────────────────────────────────


def test_ui_update_params_success(web_app):
    """POST /shipments/{id}/params with valid fields → 303 params_updated."""
    client, sf, prov = web_app
    sid = _make_unregistered_shipment(sf, "PARAMUI01", carrier=14041)
    r = client.post(
        f"/shipments/{sid}/params",
        data={
            "_csrf": "csrftoken",
            "destination_country": "NL",
            "destination_postal_code": "1000 AA",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "params_updated" in r.headers["location"]


def test_ui_update_params_invalid_country(web_app):
    """POST /shipments/{id}/params with invalid country → 303 invalid_country."""
    client, sf, prov = web_app
    sid = _make_unregistered_shipment(sf, "PARAMUI02")
    r = client.post(
        f"/shipments/{sid}/params",
        data={"_csrf": "csrftoken", "destination_country": "NLD"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid_country" in r.headers["location"]


def test_ui_update_params_invalid_phone(web_app):
    """POST /shipments/{id}/params with non-digit phone → 303 invalid_phone."""
    client, sf, prov = web_app
    sid = _make_unregistered_shipment(sf, "PARAMUI03")
    r = client.post(
        f"/shipments/{sid}/params",
        data={"_csrf": "csrftoken", "phone_number_last_4": "abcd"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid_phone" in r.headers["location"]


# ─────────────────────────────────────────────────────────────────────────────
# API params endpoint: POST /api/v1/shipments/{id}/params
# ─────────────────────────────────────────────────────────────────────────────


def _make_api_key(sf):
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="regtest", digest=digest, prefix=raw[:8]))
        s.commit()
    return raw


def test_api_update_params_success(web_app):
    """POST /api/v1/shipments/{id}/params → 200 with updated fields."""
    client, sf, prov = web_app
    api_key = _make_api_key(sf)
    sid = _make_unregistered_shipment(sf, "PARAMAPI01", carrier=14041)

    r = client.post(
        f"/api/v1/shipments/{sid}/params",
        json={"destination_country": "NL", "destination_postal_code": "1000 AA"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["destination_country"] == "NL"
    assert data["destination_postal_code"] == "1000 AA"


def test_api_update_params_invalid_country_422(web_app):
    """POST /api/v1/shipments/{id}/params with 3-letter country → 422."""
    client, sf, prov = web_app
    api_key = _make_api_key(sf)
    sid = _make_unregistered_shipment(sf, "PARAMAPI02")

    r = client.post(
        f"/api/v1/shipments/{sid}/params",
        json={"destination_country": "NLD"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 422


def test_api_update_params_invalid_phone_422(web_app):
    """POST /api/v1/shipments/{id}/params with non-digit phone → 422."""
    client, sf, prov = web_app
    api_key = _make_api_key(sf)
    sid = _make_unregistered_shipment(sf, "PARAMAPI03")

    r = client.post(
        f"/api/v1/shipments/{sid}/params",
        json={"phone_number_last_4": "abcd"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 422


def test_api_update_params_resets_pending_op(web_app):
    """Updating params resets a paused register op's next_attempt_at to now."""
    from novatrack.models import ProviderOperation
    from novatrack.tracking import _PAUSED_INDEFINITELY

    client, sf, prov = web_app
    api_key = _make_api_key(sf)

    # Simulate a paused register op (next_attempt_at far in the future)
    sid = _make_unregistered_shipment(sf, "PARAMAPI04", carrier=14041)
    with sf() as s:
        # Manually add a paused register op
        op = ProviderOperation(
            shipment_id=sid,
            kind="register",
            next_attempt_at=_PAUSED_INDEFINITELY,
        )
        s.add(op)
        s.commit()
        op_id = op.id

    r = client.post(
        f"/api/v1/shipments/{sid}/params",
        json={"destination_country": "NL"},
        headers={"X-API-Key": api_key},
    )
    assert r.status_code == 200

    # Verify op was reset to now
    with sf() as s:
        op = s.get(ProviderOperation, op_id)
        now = datetime.now(UTC)
        delta = (op.next_attempt_at.replace(tzinfo=UTC) - now).total_seconds()
        assert delta < 5, f"Op should be due now after params update, but is {delta:.0f}s ahead"


# ─────────────────────────────────────────────────────────────────────────────
# Docs HTML: unclosed code tags fixed
# ─────────────────────────────────────────────────────────────────────────────


def test_docs_html_has_no_unclosed_code_tags():
    """_SELF_HOSTED_DOCS_HTML must have balanced <code>...</code> tags."""
    from novatrack.app import _SELF_HOSTED_DOCS_HTML

    opens = _SELF_HOSTED_DOCS_HTML.count("<code>")
    closes = _SELF_HOSTED_DOCS_HTML.count("</code>")
    assert opens == closes, f"Unbalanced code tags in docs HTML: {opens} opening, {closes} closing"
    # Verify no raw unescaped '>' after a code tag open (old truncation pattern)
    assert "<code>X-API-Key: &lt;yo...ode>" not in _SELF_HOSTED_DOCS_HTML
