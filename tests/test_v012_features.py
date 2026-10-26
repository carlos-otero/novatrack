"""Tests for v0.1.2 features.

Covers:
- novatrack/carriers.py (carrier catalogue + additional params)
- Track17Provider.changecarrier
- Track17Provider.register with param
- ProviderError.retryable_without_quota
- tracking.change_carrier_shipment
- tracking.process_operations param build + retryable-without-quota reset
- tracking.create_shipment additional fields
- HTML refresh of unregistered shipment → redirect with msg
- UI/API change-carrier routes
- API carriers search endpoint
- Alembic migration 0002 round-trip (real PG)

All provider calls use mocks; PG migration tests use a disposable real DB when
TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import pytest

from novatrack.carriers import (
    build_param_string,
    get_additional_params,
    get_carrier,
    missing_required_params,
    search_carriers,
)
from novatrack.providers import ProviderError
from novatrack.providers.track17 import (
    _CC_INVALID_CARRIER,
    _CC_LIMIT_EXCEEDED,
    _CC_NOT_ACTIVE,
    _CC_NOT_REGISTERED,
    _CC_NULL_CARRIER,
    _CC_PROCESSING,
    _CC_SAME_CARRIER,
    _INVALID_PARAM_FORMAT,
    _MISSING_PARAM,
    _PARAM_MISSING_PROMPT,
    Track17Provider,
)

# ─────────────────────────────────────────────────────────────────────────────
# carriers.py
# ─────────────────────────────────────────────────────────────────────────────


def test_get_carrier_known():
    """get_carrier returns entry for a known code."""
    c = get_carrier(100001)  # DHL Express is in the list
    # The bundled list uses the exact 17TRACK names
    assert c is not None
    assert c["key"] == 100001
    assert isinstance(c["name"], str) and len(c["name"]) > 0


def test_get_carrier_unknown():
    assert get_carrier(0) is None
    assert get_carrier(999999999) is None


def test_search_carriers_by_name():
    results = search_carriers("DHL Express", limit=5)
    assert any(c["key"] == 100001 for c in results)


def test_search_carriers_by_code_string():
    results = search_carriers("100001", limit=5)
    assert any(c["key"] == 100001 for c in results)


def test_search_carriers_empty_query_returns_first_n():
    results = search_carriers("", limit=5)
    assert len(results) == 5


def test_search_carriers_no_match_returns_empty():
    results = search_carriers("XYZXYZXYZNOTACARRIER_abc123")
    assert results == []


def test_get_additional_params_postnl():
    """PostNL (14041) requires destination_country and optionally postal_code."""
    params = get_additional_params(14041)
    keys = [p["paramKey"] for p in params]
    assert "destination_country" in keys
    assert "postal_code" in keys
    required = [p["paramKey"] for p in params if p["require"]]
    assert "destination_country" in required


def test_get_additional_params_jnt_id():
    """J&T Express ID (100074) requires phone_number_last_4."""
    params = get_additional_params(100074)
    assert any(p["paramKey"] == "phone_number_last_4" and p["require"] for p in params)


def test_get_additional_params_unknown():
    assert get_additional_params(999999999) == []


def test_build_param_string_no_params():
    """Carriers with no additional params return None."""
    # Carrier 100001 (DHL Express) has no additional params
    result = build_param_string(100001, destination_country="NL")
    assert result is None


def test_build_param_string_postnl_full():
    """PostNL: destination_country + postal_code → semicolon-separated."""
    result = build_param_string(
        14041,
        destination_country="nl",  # should be upcased
        destination_postal_code="1234 AB",
    )
    assert result is not None
    assert "NL" in result
    assert "1234 AB" in result


def test_build_param_string_postnl_country_only():
    result = build_param_string(14041, destination_country="FR")
    assert result is not None
    assert "FR" in result


def test_build_param_string_phone_last4():
    result = build_param_string(100074, phone_number_last_4="8888")
    assert result is not None
    assert "8888" in result


def test_build_param_string_nothing_provided():
    """When we have no values for any paramKey, returns None."""
    result = build_param_string(14041)  # PostNL needs params but none provided
    assert result is None


def test_missing_required_params_postnl_missing():
    missing = missing_required_params(14041)
    assert "destination_country" in missing


def test_missing_required_params_postnl_satisfied():
    missing = missing_required_params(14041, destination_country="NL")
    assert "destination_country" not in missing


def test_missing_required_params_no_reqs():
    """Carrier with no required params → empty list."""
    # Find a carrier with only optional params; DHL Paket (7041) has optional postal_code
    missing = missing_required_params(7041)
    assert missing == []


# ─────────────────────────────────────────────────────────────────────────────
# ProviderError.retryable_without_quota
# ─────────────────────────────────────────────────────────────────────────────


def test_provider_error_default_not_retryable():
    e = ProviderError("some error")
    assert e.retryable_without_quota is False


def test_provider_error_retryable_flag():
    e = ProviderError("missing param", retryable_without_quota=True)
    assert e.retryable_without_quota is True
    assert "missing param" in str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Track17Provider.register with param
# ─────────────────────────────────────────────────────────────────────────────


def _mock_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload)


def _provider(response: httpx.Response) -> Track17Provider:
    transport = httpx.MockTransport(lambda r: response)
    client = httpx.Client(transport=transport)
    return Track17Provider(api_key="testkey", client=client)


def test_register_sends_param_field():
    """When param is passed, it appears in the request body."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        json.loads(request.content)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"accepted": [{"number": "RR123", "carrier": 3011}], "rejected": []}},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)

    prov.register("RR123", 3011, param="FR;1234 AB")

    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body[0]["param"] == "FR;1234 AB"


def test_register_omits_param_when_none():
    """When param is None, the param key is NOT sent."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"accepted": [{"number": "RR123", "carrier": 3011}], "rejected": []}},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)

    prov.register("RR123", 3011, param=None)

    body = json.loads(calls[0].content)
    assert "param" not in body[0]


def test_register_missing_param_raises_retryable():
    """Register rejection with -18010018 raises retryable_without_quota=True."""
    resp = _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [
                    {
                        "number": "RR123",
                        "carrier": 0,
                        "error": {"code": _MISSING_PARAM, "message": "Missing param"},
                    }
                ],
            },
        }
    )
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc_info:
        prov.register("RR123", 14041)
    assert exc_info.value.retryable_without_quota is True
    assert "additional information" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_register_invalid_param_format_raises_retryable():
    """Register rejection with -18010019 also raises retryable_without_quota=True."""
    resp = _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [
                    {
                        "number": "RR123",
                        "carrier": 0,
                        "error": {"code": _INVALID_PARAM_FORMAT, "message": "Bad format"},
                    }
                ],
            },
        }
    )
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc_info:
        prov.register("RR123", 14041)
    assert exc_info.value.retryable_without_quota is True


# ─────────────────────────────────────────────────────────────────────────────
# Track17Provider.changecarrier
# ─────────────────────────────────────────────────────────────────────────────


def _cc_accepted_response(number: str, carrier: int) -> httpx.Response:
    return _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [{"number": number, "carrier": carrier}],
                "rejected": [],
            },
        }
    )


def _cc_rejected_response(number: str, err_code: int) -> httpx.Response:
    return _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [
                    {"number": number, "carrier": 0, "error": {"code": err_code, "message": "error"}}
                ],
            },
        }
    )


def test_changecarrier_accepted_returns_dict():
    prov = _provider(_cc_accepted_response("RR123", 3011))
    result = prov.changecarrier("RR123", 3011)
    assert result["carrier"] == 3011
    assert result["number"] == "RR123"


def test_changecarrier_same_carrier_idempotent():
    prov = _provider(_cc_rejected_response("RR123", _CC_SAME_CARRIER))
    result = prov.changecarrier("RR123", 3011)
    assert result["carrier"] == 3011  # idempotent: no error raised


def test_changecarrier_not_registered_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_NOT_REGISTERED))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 3011)
    assert "registered" in str(exc_info.value).lower()


def test_changecarrier_processing_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_PROCESSING))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 3011)
    assert "processed" in str(exc_info.value).lower() or "processing" in str(exc_info.value).lower()


def test_changecarrier_null_carrier_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_NULL_CARRIER))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 3011)
    assert "empty" in str(exc_info.value).lower() or "null" in str(exc_info.value).lower()


def test_changecarrier_invalid_carrier_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_INVALID_CARRIER))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 99999)
    assert "does not exist" in str(exc_info.value).lower()


def test_changecarrier_not_active_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_NOT_ACTIVE))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 3011)
    assert "active" in str(exc_info.value).lower()


def test_changecarrier_limit_exceeded_raises():
    prov = _provider(_cc_rejected_response("RR123", _CC_LIMIT_EXCEEDED))
    with pytest.raises(ProviderError) as exc_info:
        prov.changecarrier("RR123", 3011)
    assert "5" in str(exc_info.value) or "limit" in str(exc_info.value).lower()


def test_changecarrier_sends_carrier_old():
    """When carrier_old is passed, it appears in the request body as carrier_old."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"accepted": [{"number": "RR123", "carrier": 3011}], "rejected": []}},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)
    prov.changecarrier("RR123", carrier_new=3011, carrier_old=2000)

    body = json.loads(calls[0].content)
    assert body[0]["carrier_old"] == 2000
    assert body[0]["carrier"] == 3011


# ─────────────────────────────────────────────────────────────────────────────
# tracking.create_shipment — additional fields
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem_session():
    """In-memory SQLite session for unit tests."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from novatrack.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session() as s:
        yield s


def test_create_shipment_stores_additional_fields(mem_session):
    from novatrack.tracking import create_shipment

    prov = MagicMock()
    s = create_shipment(
        mem_session,
        prov,
        number="EXTRA001",
        carrier=14041,
        destination_country="NL",
        destination_postal_code="1234 AB",
        phone_number_last_4="9999",
    )
    mem_session.flush()
    assert s.destination_country == "NL"
    assert s.destination_postal_code == "1234 AB"
    assert s.phone_number_last_4 == "9999"


def test_create_shipment_empty_strings_stored_as_none(mem_session):
    from novatrack.tracking import create_shipment

    prov = MagicMock()
    s = create_shipment(
        mem_session,
        prov,
        number="EXTRA002",
        destination_country="",
        destination_postal_code="1234",  # non-empty postal preserved
        phone_number_last_4="",
    )
    mem_session.flush()
    assert s.destination_country is None
    assert s.destination_postal_code == "1234"
    assert s.phone_number_last_4 is None


# ─────────────────────────────────────────────────────────────────────────────
# tracking.process_operations — param building + retryable reset
# ─────────────────────────────────────────────────────────────────────────────


def test_process_operations_builds_param_for_postnl(mem_session):
    """PostNL (14041) register call includes the param string."""
    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()
    prov.register.return_value = {"number": "RR123", "carrier": 14041}

    create_shipment(
        mem_session,
        prov,
        number="RR123POSTNL",
        carrier=14041,
        destination_country="NL",
        destination_postal_code="1000 AA",
    )
    mem_session.flush()

    count = process_operations(mem_session, prov)
    assert count == 1
    call_kwargs = prov.register.call_args
    assert call_kwargs.kwargs["param"] is not None
    assert "NL" in call_kwargs.kwargs["param"]


def test_process_operations_retryable_resets_next_attempt(mem_session):
    """A retryable_without_quota ProviderError pauses the op (not immediate retry).

    Missing-param rejections set next_attempt_at to a far future so the worker
    does NOT loop retrying every tick.  The user must update the missing fields
    via the UI/API, which resets next_attempt_at to now.
    """
    from sqlalchemy import select

    from novatrack.models import ProviderOperation
    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()
    prov.register.side_effect = ProviderError(_PARAM_MISSING_PROMPT, retryable_without_quota=True)

    create_shipment(mem_session, prov, number="RETRYME1", carrier=14041)
    mem_session.flush()

    now_before = datetime.now(UTC)
    process_operations(mem_session, prov)

    op = (
        mem_session.execute(
            select(ProviderOperation).where(ProviderOperation.shipment_id != None)  # noqa: E711
        )
        .scalars()
        .first()
    )
    assert op is not None
    assert op.done is False
    # next_attempt_at must be effectively indefinite — the sentinel year 9999.
    # The worker must NEVER auto-retry; only update_shipment_params() can reset it.
    assert op.next_attempt_at is not None
    delta = (op.next_attempt_at.replace(tzinfo=UTC) - now_before).total_seconds()
    one_year_secs = 365 * 24 * 3600
    assert delta > one_year_secs, (
        f"Missing-param op must be paused INDEFINITELY (>1 year), but "
        f"next_attempt_at is only {delta:.0f}s ahead — worker would auto-retry"
    )


def test_process_operations_normal_error_uses_backoff(mem_session):
    """A normal ProviderError (not retryable) applies exponential backoff."""
    from sqlalchemy import select

    from novatrack.models import ProviderOperation
    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()
    prov.register.side_effect = ProviderError("network error")

    create_shipment(mem_session, prov, number="BACKOFF01")
    mem_session.flush()

    process_operations(mem_session, prov)

    op = (
        mem_session.execute(
            select(ProviderOperation).where(ProviderOperation.shipment_id != None)  # noqa: E711
        )
        .scalars()
        .first()
    )
    assert op is not None
    assert op.done is False
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    delta = (op.next_attempt_at.replace(tzinfo=UTC) - now).total_seconds()
    assert delta >= 25, "Normal error should apply backoff >= 25s"


# ─────────────────────────────────────────────────────────────────────────────
# tracking.change_carrier_shipment
# ─────────────────────────────────────────────────────────────────────────────


def test_change_carrier_shipment_success(mem_session):
    """change_carrier_shipment updates carrier on success."""
    from novatrack.tracking import change_carrier_shipment, create_shipment

    prov = MagicMock()
    # create registered shipment
    ship = create_shipment(mem_session, prov, number="CHGCARR1", carrier=1000)
    mem_session.flush()
    ship.registered = True
    mem_session.flush()

    prov.changecarrier.return_value = {"number": "CHGCARR1", "carrier": 3011}
    change_carrier_shipment(mem_session, ship, prov, 3011)

    assert ship.carrier == 3011
    prov.changecarrier.assert_called_once_with("CHGCARR1", carrier_new=3011, carrier_old=1000)


def test_change_carrier_shipment_provider_error_propagates(mem_session):
    """ProviderError from changecarrier propagates without DB change."""
    from novatrack.tracking import change_carrier_shipment, create_shipment

    prov = MagicMock()
    ship = create_shipment(mem_session, prov, number="CHGCARR2", carrier=1000)
    mem_session.flush()
    ship.registered = True
    mem_session.flush()

    prov.changecarrier.side_effect = ProviderError("limit exceeded")
    with pytest.raises(ProviderError):
        change_carrier_shipment(mem_session, ship, prov, 3011)

    # carrier not changed on error
    assert ship.carrier == 1000


# ─────────────────────────────────────────────────────────────────────────────
# Web routes — change carrier + flash messages + carriers API
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def web_app():
    """Minimal app for web-layer tests (SQLite in-memory, mock provider)."""
    import base64

    from itsdangerous import TimestampSigner
    from pydantic import SecretStr
    from starlette.testclient import TestClient

    from novatrack.app import create_app
    from novatrack.config import Settings
    from novatrack.db import make_engine, make_session_factory
    from novatrack.models import AppSettings, Base

    _SECRET = "testsecretkey32bytesxxxxxxxxxxxxxxxxxx"
    _OIDC_ISSUER = "https://idp.test.example.com"
    _APP_BASE = "https://app.test.example.com"

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
        oidc_issuer=_OIDC_ISSUER,
        oidc_client_id="test-client-id",
        oidc_client_secret=SecretStr("test-client-secret"),
        app_base_url=_APP_BASE,
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

    # Build an authenticated session cookie
    session_data = {"user": {"sub": "u-test", "name": "Test User"}, "csrf": "csrftoken"}
    signer = TimestampSigner(_SECRET)
    payload = base64.b64encode(json.dumps(session_data).encode()).decode()
    cookie_value = signer.sign(payload).decode()

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("novatrack_session", cookie_value)
        yield client, sf, prov


def _make_registered_shipment(sf, number: str) -> str:
    from novatrack.models import Shipment

    with sf() as s:
        ship = Shipment(number=number, registered=True, carrier=1000, status="pending")
        s.add(ship)
        s.commit()
        return ship.id


def _make_unregistered_shipment(sf, number: str) -> str:
    from novatrack.models import Shipment

    with sf() as s:
        ship = Shipment(number=number, registered=False, status="pending")
        s.add(ship)
        s.commit()
        return ship.id


def test_ui_change_carrier_unregistered_redirects(web_app):
    """POST /shipments/{id}/carrier on unregistered shipment → 303 not_registered."""
    client, sf, prov = web_app
    sid = _make_unregistered_shipment(sf, "CHGWEB01")
    r = client.post(
        f"/shipments/{sid}/carrier", data={"_csrf": "csrftoken", "carrier": "3011"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "not_registered" in r.headers["location"]


def test_ui_change_carrier_registered_success(web_app):
    """POST /shipments/{id}/carrier for registered shipment → 303 carrier_changed."""
    client, sf, prov = web_app
    sid = _make_registered_shipment(sf, "CHGWEB02")
    prov.changecarrier.return_value = {"number": "CHGWEB02", "carrier": 3011}
    r = client.post(
        f"/shipments/{sid}/carrier", data={"_csrf": "csrftoken", "carrier": "3011"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "carrier_changed" in r.headers["location"]


def test_ui_change_carrier_provider_error_redirects_carrier_error(web_app):
    """ProviderError from changecarrier → 303 carrier_error."""
    client, sf, prov = web_app
    sid = _make_registered_shipment(sf, "CHGWEB03")
    prov.changecarrier.side_effect = ProviderError("carrier limit exceeded")
    r = client.post(
        f"/shipments/{sid}/carrier", data={"_csrf": "csrftoken", "carrier": "3011"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "carrier_error" in r.headers["location"]


def test_ui_change_carrier_invalid_code_redirects(web_app):
    """Invalid carrier code (non-numeric) → 303 invalid_carrier."""
    client, sf, prov = web_app
    sid = _make_registered_shipment(sf, "CHGWEB04")
    r = client.post(
        f"/shipments/{sid}/carrier",
        data={"_csrf": "csrftoken", "carrier": "notanumber"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "invalid_carrier" in r.headers["location"]


def test_ui_refresh_unregistered_redirects_not_registered(web_app):
    """POST /shipments/{id}/refresh on unregistered → 303 with ?msg=not_registered."""
    client, sf, prov = web_app
    sid = _make_unregistered_shipment(sf, "REFUNREG1")
    r = client.post(f"/shipments/{sid}/refresh", data={"_csrf": "csrftoken"}, follow_redirects=False)
    assert r.status_code == 303
    assert "not_registered" in r.headers["location"]


def test_api_change_carrier_not_registered_returns_400(web_app):
    """API POST /api/v1/shipments/{id}/carrier unregistered → 400."""
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    client, sf, prov = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="test", digest=digest, prefix=raw[:8]))
        s.commit()

    sid = _make_unregistered_shipment(sf, "APICARR01")
    r = client.post(
        f"/api/v1/shipments/{sid}/carrier",
        json={"carrier": 3011},
        headers={"X-API-Key": raw},
    )
    assert r.status_code == 400


def test_api_change_carrier_registered_success(web_app):
    """API POST /api/v1/shipments/{id}/carrier registered → 200 with new carrier."""
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    client, sf, prov = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="test2", digest=digest, prefix=raw[:8]))
        s.commit()

    sid = _make_registered_shipment(sf, "APICARR02")
    prov.changecarrier.return_value = {"number": "APICARR02", "carrier": 3011}
    r = client.post(
        f"/api/v1/shipments/{sid}/carrier",
        json={"carrier": 3011},
        headers={"X-API-Key": raw},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["carrier"] == 3011


def test_api_carriers_search(web_app):
    """GET /api/v1/carriers returns carrier list with key+name."""
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    client, sf, _ = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="testcarr", digest=digest, prefix=raw[:8]))
        s.commit()

    r = client.get("/api/v1/carriers?q=DHL&limit=5", headers={"X-API-Key": raw})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert len(data["items"]) > 0
    assert all("key" in c and "name" in c for c in data["items"])


def test_api_create_shipment_with_additional_fields(web_app):
    """POST /api/v1/shipments with additional fields stores them."""
    from novatrack.models import ApiKey, Shipment
    from novatrack.security import generate_api_key

    client, sf, prov = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="testaddl", digest=digest, prefix=raw[:8]))
        s.commit()

    prov.register.return_value = {"number": "ADDL001", "carrier": 14041}
    r = client.post(
        "/api/v1/shipments",
        json={
            "number": "ADDL001",
            "carrier": 14041,
            "destination_country": "NL",
            "destination_postal_code": "1000 AA",
            "phone_number_last_4": "9999",
        },
        headers={"X-API-Key": raw},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["destination_country"] == "NL"
    assert data["destination_postal_code"] == "1000 AA"
    assert data["phone_number_last_4"] == "9999"

    with sf() as s:
        ship = s.query(Shipment).filter_by(number="ADDL001").one()
        assert ship.destination_country == "NL"


def test_api_create_shipment_invalid_country_returns_422(web_app):
    """POST /api/v1/shipments with 3-letter country code → 422."""
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    client, sf, _ = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="testbad", digest=digest, prefix=raw[:8]))
        s.commit()

    r = client.post(
        "/api/v1/shipments",
        json={"number": "BADDCT01", "destination_country": "NLD"},
        headers={"X-API-Key": raw},
    )
    assert r.status_code == 422


def test_api_create_shipment_invalid_phone_returns_422(web_app):
    """POST /api/v1/shipments with non-digit phone_last4 → 422."""
    from novatrack.models import ApiKey
    from novatrack.security import generate_api_key

    client, sf, _ = web_app
    raw, digest = generate_api_key()
    with sf() as s:
        s.add(ApiKey(name="testbad2", digest=digest, prefix=raw[:8]))
        s.commit()

    r = client.post(
        "/api/v1/shipments",
        json={"number": "BADPH001", "phone_number_last_4": "abcd"},
        headers={"X-API-Key": raw},
    )
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Alembic migration 0002 upgrade/downgrade (real PG when TEST_DATABASE_URL set)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="Requires TEST_DATABASE_URL (disposable PostgreSQL 16)",
)
def test_migration_0002_upgrade_downgrade():
    """Alembic 0002: upgrade adds columns; downgrade removes them."""
    import subprocess
    import sys

    import sqlalchemy as sa

    db_url = os.environ["TEST_DATABASE_URL"]
    repo = os.path.dirname(os.path.dirname(__file__))

    def run_alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=repo,
            env={**os.environ, "DATABASE_URL": db_url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"alembic {args} failed:\n{result.stderr}"

    run_alembic("upgrade", "head")

    engine = sa.create_engine(db_url)
    with engine.connect() as conn:
        cols = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name='shipments'")
            )
        }
    assert "destination_country" in cols
    assert "destination_postal_code" in cols
    assert "phone_number_last_4" in cols

    run_alembic("downgrade", "0001")

    with engine.connect() as conn:
        cols2 = {
            row[0]
            for row in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name='shipments'")
            )
        }
    assert "destination_country" not in cols2
    assert "destination_postal_code" not in cols2
    assert "phone_number_last_4" not in cols2

    # Restore to head
    run_alembic("upgrade", "head")
    engine.dispose()
