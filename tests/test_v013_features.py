"""Tests for v0.1.3 features and fixes.

Covers:
- Provider exact-body fixture: changecarrier sends carrier_new key (not carrier)
- All 9 changecarrier error codes correctly mapped (-18019801..-18019809)
- -18019807 is limit-exceeded, never idempotent
- Register codes -18010018/-18010019/-18010020 accurate messages + retryable flag
- Register code -18010022 date: terminal flag, marks op done, no retry loop
- ProviderError.terminal flag handling in process_operations
- Trash re-add flow: unregistered can purge/recreate
- Trash re-add flow: registered must restore or purge explicitly
- Restore shipment: cancels pending delete, re-queues register for registered
- Purge shipment: blocked when pending ops exist
- UI create-shipment duplicate shows link to existing shipment in trash/archive/active
- ETA section hidden for empty dict {}
- ETA section hidden when both from/to are None
- ETA section shows em dash for missing individual date fields
- Alert position: flash_msg and provider_error appear before detail-header
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from novatrack.providers import ProviderError
from novatrack.providers.track17 import (
    _CC_DUPLICATE_INFO,
    _CC_INVALID_NEW_CARRIER,
    _CC_LIMIT_EXCEEDED,
    _CC_MISSING_NEW,
    _CC_MULTIPLE_CARRIERS,
    _CC_OLD_NOT_REGISTERED,
    _CC_SAME_AS_CURRENT,
    _CC_STOPPED,
    _CC_WAIT_RESULTS,
    _MISSING_COUNTRY_POSTAL,
    _MISSING_DATE,
    _MISSING_PHONE,
    _MISSING_POSTAL,
    Track17Provider,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _mock_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status, json=payload)


def _provider(response: httpx.Response) -> Track17Provider:
    transport = httpx.MockTransport(lambda r: response)
    client = httpx.Client(transport=transport)
    return Track17Provider(api_key="testkey", client=client)


def _cc_accepted_response(number: str, carrier: int) -> httpx.Response:
    return _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [{"number": number, "carrier": carrier, "final_carrier": 0}],
                "rejected": [],
            },
        }
    )


def _cc_rejected_response(number: str, err_code: int, msg: str = "error") -> httpx.Response:
    return _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [{"number": number, "carrier": 0, "error": {"code": err_code, "message": msg}}],
            },
        }
    )


def _register_rejected_response(number: str, err_code: int, msg: str = "error") -> httpx.Response:
    return _mock_response(
        {
            "code": 0,
            "data": {
                "accepted": [],
                "rejected": [{"number": number, "carrier": 0, "error": {"code": err_code, "message": msg}}],
            },
        }
    )


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

    session_data = {"user": {"sub": "u-test", "name": "Test User"}, "csrf": "csrftoken"}
    signer = TimestampSigner(_SECRET)
    payload = base64.b64encode(json.dumps(session_data).encode()).decode()
    cookie_value = signer.sign(payload).decode()

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        client.cookies.set("novatrack_session", cookie_value)
        yield client, sf, prov


# ─────────────────────────────────────────────────────────────────────────────
# Provider body fixture: carrier_new key
# ─────────────────────────────────────────────────────────────────────────────


def test_changecarrier_body_sends_carrier_new_not_carrier():
    """changecarrier POST body must use 'carrier_new', not 'carrier'.

    Official doc: https://api.17track.net/en/doc#change-carrier
    Parameter: carrier_new (required, Int.)
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "accepted": [{"number": "RR123", "carrier": 3011, "final_carrier": 0}],
                    "rejected": [],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)
    prov.changecarrier("RR123", carrier_new=3011)

    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert isinstance(body, list) and len(body) == 1
    item = body[0]
    # MUST have carrier_new (not carrier)
    assert "carrier_new" in item, "Request body must contain 'carrier_new' key per official API spec"
    assert item["carrier_new"] == 3011
    # 'carrier' key MUST NOT appear in the changecarrier body
    assert "carrier" not in item, (
        "changecarrier body must NOT have a 'carrier' key; "
        "this was the v0.1.2 bug that caused silent API rejections"
    )


def test_changecarrier_body_includes_carrier_old_when_given():
    """When carrier_old is passed, the body contains both carrier_new and carrier_old."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "accepted": [{"number": "RR123", "carrier": 3011, "final_carrier": 0}],
                    "rejected": [],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)
    prov.changecarrier("RR123", carrier_new=3011, carrier_old=2000)

    body = json.loads(calls[0].content)
    item = body[0]
    assert item["carrier_new"] == 3011
    assert item["carrier_old"] == 2000
    assert "carrier" not in item


def test_changecarrier_body_omits_carrier_old_when_none():
    """carrier_old must be absent from body when not given (optional per spec)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "accepted": [{"number": "RR123", "carrier": 3011, "final_carrier": 0}],
                    "rejected": [],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    prov = Track17Provider(api_key="testkey", client=client)
    prov.changecarrier("RR123", carrier_new=3011)

    body = json.loads(calls[0].content)
    assert "carrier_old" not in body[0]


# ─────────────────────────────────────────────────────────────────────────────
# Exact error-code fixture tests for changecarrier (-18019801..-18019809)
# ─────────────────────────────────────────────────────────────────────────────


def test_cc_801_multiple_carriers_raises():
    """-18019801: multiple carriers registered, must specify carrier_old."""
    prov = _provider(_cc_rejected_response("RR123", _CC_MULTIPLE_CARRIERS))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "multiple" in msg or "carrier_old" in msg


def test_cc_802_invalid_new_carrier_raises():
    """-18019802: carrier_new value is incorrect."""
    prov = _provider(_cc_rejected_response("RR123", _CC_INVALID_NEW_CARRIER))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 99999)
    assert "incorrect" in str(exc.value).lower() or "invalid" in str(exc.value).lower()


def test_cc_803_same_as_current_raises():
    """-18019803: new carrier code same as current — NOT idempotent."""
    prov = _provider(_cc_rejected_response("RR123", _CC_SAME_AS_CURRENT))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    assert "same" in str(exc.value).lower()


def test_cc_804_missing_new_raises():
    """-18019804: carrier_new / final_carrier_new not specified."""
    prov = _provider(_cc_rejected_response("RR123", _CC_MISSING_NEW))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "specified" in msg or "carrier_new" in msg


def test_cc_805_old_not_registered_raises():
    """-18019805: carrier_old not registered or incorrect."""
    prov = _provider(_cc_rejected_response("RR123", _CC_OLD_NOT_REGISTERED))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011, carrier_old=9999)
    msg = str(exc.value).lower()
    assert "registered" in msg or "incorrect" in msg


def test_cc_806_stopped_raises():
    """-18019806: number stopped — retrack before changing."""
    prov = _provider(_cc_rejected_response("RR123", _CC_STOPPED))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "stopped" in msg or "retrack" in msg


def test_cc_807_limit_exceeded_raises_never_idempotent():
    """-18019807 is limit-exceeded — must ALWAYS raise ProviderError.

    v0.1.2 wrongly returned an accepted result when the provider sent -18019807.
    This test verifies the fix: 807 is 'change limit reached', not 'same carrier'.
    """
    prov = _provider(_cc_rejected_response("RR123", _CC_LIMIT_EXCEEDED))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "limit" in msg or "5" in msg


def test_cc_808_wait_results_raises():
    """-18019808: tracking result not yet returned, wait before changing."""
    prov = _provider(_cc_rejected_response("RR123", _CC_WAIT_RESULTS))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "wait" in msg or "result" in msg


def test_cc_809_duplicate_info_raises():
    """-18019809: duplicate registration info."""
    prov = _provider(_cc_rejected_response("RR123", _CC_DUPLICATE_INFO))
    with pytest.raises(ProviderError) as exc:
        prov.changecarrier("RR123", 3011)
    msg = str(exc.value).lower()
    assert "duplicate" in msg or "exists" in msg


# ─────────────────────────────────────────────────────────────────────────────
# Register error codes accurate messages
# ─────────────────────────────────────────────────────────────────────────────


def test_register_18010018_country_postal_accurate_message():
    """-18010018: requires country + postal code — message mentions both."""
    resp = _register_rejected_response("RR123", _MISSING_COUNTRY_POSTAL)
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc:
        prov.register("RR123", 14041)
    assert exc.value.retryable_without_quota is True
    msg = str(exc.value).lower()
    assert "country" in msg or "postal" in msg


def test_register_18010019_postal_accurate_message():
    """-18010019: requires postal code — message mentions postal."""
    resp = _register_rejected_response("RR123", _MISSING_POSTAL)
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc:
        prov.register("RR123", 14041)
    assert exc.value.retryable_without_quota is True
    assert "postal" in str(exc.value).lower()


def test_register_18010020_phone_accurate_message():
    """-18010020: requires phone number — message mentions phone."""
    resp = _register_rejected_response("RR123", _MISSING_PHONE)
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc:
        prov.register("RR123", 100074)
    assert exc.value.retryable_without_quota is True
    assert "phone" in str(exc.value).lower()


def test_register_18010022_date_terminal_not_retryable():
    """-18010022: shipping date required — terminal flag, marks op done.

    NovaTrack cannot supply ship_date so this is an unsupported param.
    The ProviderError must have terminal=True so process_operations marks
    the op done=True rather than looping on backoff.
    """
    resp = _register_rejected_response("RR123", _MISSING_DATE)
    prov = _provider(resp)
    with pytest.raises(ProviderError) as exc:
        prov.register("RR123", 12345)
    err = exc.value
    assert err.terminal is True, (
        "Date-param rejection must have terminal=True — "
        "NovaTrack cannot supply ship_date and should not retry"
    )
    assert err.retryable_without_quota is False
    msg = str(err).lower()
    assert "date" in msg or "cannot supply" in msg or "support" in msg


# ─────────────────────────────────────────────────────────────────────────────
# process_operations: terminal ProviderError marks op done
# ─────────────────────────────────────────────────────────────────────────────


def test_process_operations_terminal_error_marks_op_done(mem_session):
    """A terminal ProviderError (e.g. date param) marks op done=True — no retry."""
    from sqlalchemy import select

    from novatrack.models import ProviderOperation
    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()
    prov.register.side_effect = ProviderError(
        "This carrier requires a shipping date that NovaTrack cannot supply.",
        terminal=True,
    )

    create_shipment(mem_session, prov, number="TERMINAL1", carrier=12345)
    mem_session.flush()

    count = process_operations(mem_session, prov)
    assert count == 0  # Did not succeed

    op = (
        mem_session.execute(
            select(ProviderOperation).where(ProviderOperation.shipment_id.isnot(None))
        )
        .scalars()
        .first()
    )
    assert op is not None
    assert op.done is True, (
        "Terminal ProviderError must mark op done=True — "
        "worker must NEVER retry a terminal op"
    )


def test_process_operations_terminal_error_no_auto_retry(mem_session):
    """Terminal op stays done=True after repeated process_operations calls."""

    from novatrack.tracking import create_shipment, process_operations

    prov = MagicMock()
    prov.register.side_effect = ProviderError("date param unsupported", terminal=True)

    create_shipment(mem_session, prov, number="TERMINAL2", carrier=12345)
    mem_session.flush()

    process_operations(mem_session, prov)

    # Simulate a second tick — register should NOT be called again
    prov.register.reset_mock()
    prov.register.side_effect = AssertionError("register must not be called after terminal error")

    process_operations(mem_session, prov)
    # If we get here without AssertionError, the terminal op was not retried


# ─────────────────────────────────────────────────────────────────────────────
# Trash re-add: restore_shipment
# ─────────────────────────────────────────────────────────────────────────────


def test_restore_shipment_unregistered_moves_to_active(mem_session):
    """Restoring an unregistered trashed shipment moves it back to active."""
    from novatrack.models import Shipment
    from novatrack.tracking import restore_shipment

    prov = MagicMock()
    ship = Shipment(number="RESTORE01", registered=False, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    reregister = restore_shipment(mem_session, ship, prov)

    assert ship.bucket == "active"
    assert ship.trashed_at is None
    assert reregister is False  # No quota used


def test_restore_shipment_registered_queues_register_op(mem_session):
    """Restoring a registered trashed shipment re-queues register (costs quota)."""
    from sqlalchemy import select

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import restore_shipment

    prov = MagicMock()
    ship = Shipment(number="RESTORE02", registered=True, carrier=3011, status="delivered", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    reregister = restore_shipment(mem_session, ship, prov)

    assert ship.bucket == "active"
    assert ship.registered is False  # reset for re-registration
    assert reregister is True  # caller should warn about quota

    # A new register op should have been queued
    new_ops = (
        mem_session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == ship.id,
                ProviderOperation.kind == "register",
                ProviderOperation.done == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    assert len(new_ops) == 1


def test_restore_shipment_cancels_pending_delete(mem_session):
    """Restore cancels any still-pending delete ops so they don't run."""

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import restore_shipment

    prov = MagicMock()
    ship = Shipment(number="RESTORE03", registered=True, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    # Simulate a pending delete op (queued when shipment was trashed)
    delete_op = ProviderOperation(shipment_id=ship.id, kind="delete", done=False)
    mem_session.add(delete_op)
    mem_session.flush()

    restore_shipment(mem_session, ship, prov)

    # The delete op should be marked done (cancelled)
    refreshed = mem_session.get(ProviderOperation, delete_op.id)
    assert refreshed is not None
    assert refreshed.done is True


def test_restore_shipment_after_delete_op_completed_requeues_register(mem_session):
    """State 2: restore when delete op already ran (registered=False, done delete op).

    If the worker ran deletetrack while the shipment was in trash,
    registered=False and the delete op is done=True.  restore_shipment must
    queue a new register op so the shipment gets tracked again (costs quota).
    This was a gap in the original logic which only handled registered=True.
    """
    from sqlalchemy import select

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import restore_shipment

    prov = MagicMock()
    # Simulate: was registered, worker deleted it while in trash
    ship = Shipment(number="RESTORE05", registered=False, status="delivered", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    # The delete op that the worker ran (done=True means provider deleted it)
    done_delete = ProviderOperation(shipment_id=ship.id, kind="delete", done=True)
    mem_session.add(done_delete)
    mem_session.flush()

    reregister = restore_shipment(mem_session, ship, prov)

    assert ship.bucket == "active"
    assert ship.trashed_at is None
    assert reregister is True, "Must warn caller that re-registration (quota) will happen"

    new_ops = (
        mem_session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == ship.id,
                ProviderOperation.kind == "register",
                ProviderOperation.done == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    assert len(new_ops) == 1, "Exactly one pending re-register op must be queued"


def test_restore_shipment_paused_register_preserved(mem_session):
    """State 3: restore when register op is paused (never confirmed registered).

    If the shipment's register op is paused (missing params), restoring
    preserves the existing register op and does NOT add a duplicate.
    Returns False (no quota charge until the user fixes params and it succeeds).
    """
    from sqlalchemy import select

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import _PAUSED_INDEFINITELY, restore_shipment

    prov = MagicMock()
    ship = Shipment(number="RESTORE06", registered=False, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    # Paused register op (missing postal code): next_attempt_at = year 9999
    paused_reg = ProviderOperation(
        shipment_id=ship.id,
        kind="register",
        done=False,
        next_attempt_at=_PAUSED_INDEFINITELY,
    )
    mem_session.add(paused_reg)
    # Also a pending delete that was queued when shipment was trashed
    pending_del = ProviderOperation(shipment_id=ship.id, kind="delete", done=False)
    mem_session.add(pending_del)
    mem_session.flush()

    reregister = restore_shipment(mem_session, ship, prov)

    assert ship.bucket == "active"
    assert reregister is False, "No new quota charge — existing paused register op retained"

    # The pending delete must be cancelled
    refreshed_del = mem_session.get(ProviderOperation, pending_del.id)
    assert refreshed_del is not None and refreshed_del.done is True

    # The paused register op must still be there (not duplicated)
    register_ops = (
        mem_session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == ship.id,
                ProviderOperation.kind == "register",
                ProviderOperation.done == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    assert len(register_ops) == 1, "Exactly the original paused register op; no duplicate"
    assert register_ops[0].id == paused_reg.id


def test_restore_shipment_only_trash(mem_session):
    """restore_shipment raises ValueError for non-trash shipments."""
    from novatrack.models import Shipment
    from novatrack.tracking import restore_shipment

    prov = MagicMock()
    ship = Shipment(number="RESTORE04", registered=False, status="pending", bucket="active")
    mem_session.add(ship)
    mem_session.flush()

    with pytest.raises(ValueError, match="trash"):
        restore_shipment(mem_session, ship, prov)


# ─────────────────────────────────────────────────────────────────────────────
# Trash re-add: purge_shipment
# ─────────────────────────────────────────────────────────────────────────────


def test_purge_shipment_unregistered_no_ops(mem_session):
    """Purging an unregistered trashed shipment with no pending ops succeeds."""

    from novatrack.models import Shipment
    from novatrack.tracking import purge_shipment

    ship = Shipment(number="PURGE01", registered=False, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()
    sid = ship.id

    purge_shipment(mem_session, ship)
    mem_session.commit()

    # Row should be gone
    assert mem_session.get(Shipment, sid) is None


def test_purge_shipment_blocked_by_pending_op(mem_session):
    """Purging when a delete op is still pending raises ValueError."""
    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import purge_shipment

    ship = Shipment(number="PURGE02", registered=True, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    op = ProviderOperation(shipment_id=ship.id, kind="delete", done=False)
    mem_session.add(op)
    mem_session.flush()

    with pytest.raises(ValueError, match="pending"):
        purge_shipment(mem_session, ship)


def test_purge_shipment_allowed_after_delete_op_done(mem_session):
    """Purging allowed once the delete op is marked done."""

    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import purge_shipment

    ship = Shipment(number="PURGE03", registered=True, status="pending", bucket="trash")
    mem_session.add(ship)
    mem_session.flush()

    op = ProviderOperation(shipment_id=ship.id, kind="delete", done=True)
    mem_session.add(op)
    mem_session.flush()
    sid = ship.id

    purge_shipment(mem_session, ship)
    mem_session.commit()

    assert mem_session.get(Shipment, sid) is None


def test_purge_shipment_only_trash(mem_session):
    """purge_shipment raises ValueError for non-trash shipments."""
    from novatrack.models import Shipment
    from novatrack.tracking import purge_shipment

    ship = Shipment(number="PURGE04", registered=False, status="pending", bucket="active")
    mem_session.add(ship)
    mem_session.flush()

    with pytest.raises(ValueError, match="trash"):
        purge_shipment(mem_session, ship)


# ─────────────────────────────────────────────────────────────────────────────
# UI: trash re-add duplicate handling
# ─────────────────────────────────────────────────────────────────────────────


def _make_shipment_in_bucket(sf, number: str, bucket: str, registered: bool = False) -> str:
    from novatrack.models import Shipment

    with sf() as s:
        ship = Shipment(number=number, registered=registered, status="pending", bucket=bucket)
        s.add(ship)
        s.commit()
        return ship.id


def test_ui_add_duplicate_trash_unregistered_auto_purge_recreates(web_app):
    """Adding a number that's in Trash+unregistered auto-purges and recreates → 303."""
    from novatrack.models import Shipment as _Ship

    client, sf, prov = web_app
    _make_shipment_in_bucket(sf, "DUPL001T", "trash", registered=False)

    prov.register.return_value = {"number": "DUPL001T", "carrier": 0}
    r = client.post(
        "/shipments",
        data={"_csrf": "csrftoken", "number": "DUPL001T"},
        follow_redirects=False,
    )
    # Auto-purge + recreate → should redirect to / (303)
    assert r.status_code == 303, f"Expected 303, got {r.status_code}"

    # Confirm new shipment is active (old trash row was purged)
    with sf() as s:
        ships = s.query(_Ship).filter_by(number="DUPL001T").all()
        assert len(ships) == 1
        assert ships[0].bucket == "active"


def test_ui_add_duplicate_trash_registered_shows_link(web_app):
    """Adding a number that's in Trash+registered shows restore/link — no auto-purge."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "DUPL001TR", "trash", registered=True)

    r = client.post(
        "/shipments",
        data={"_csrf": "csrftoken", "number": "DUPL001TR"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # Re-renders form with error
    text = r.text
    assert "Trash" in text or "trash" in text
    assert sid in text  # Link to existing shipment


def test_ui_add_duplicate_archive_shows_link(web_app):
    """Creating a shipment with a number already in Archive shows 'view in Archive' link."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "DUPL001A", "archive", registered=True)

    r = client.post(
        "/shipments",
        data={"_csrf": "csrftoken", "number": "DUPL001A"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    text = r.text
    assert "Archive" in text or "archive" in text
    assert sid in text


def test_ui_add_duplicate_active_shows_link(web_app):
    """Creating a shipment with an active number shows 'already tracked' message."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "DUPL001V", "active", registered=True)

    r = client.post(
        "/shipments",
        data={"_csrf": "csrftoken", "number": "DUPL001V"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    text = r.text
    assert sid in text


# ─────────────────────────────────────────────────────────────────────────────
# UI: restore + purge routes
# ─────────────────────────────────────────────────────────────────────────────


def test_ui_restore_unregistered_trash(web_app):
    """POST /shipments/{id}/restore for unregistered trash → 303 restored_unregistered."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "RESUI01", "trash", registered=False)

    r = client.post(
        f"/shipments/{sid}/restore",
        data={"_csrf": "csrftoken"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "restored_unregistered" in r.headers["location"]


def test_ui_restore_registered_trash(web_app):
    """POST /shipments/{id}/restore for registered trash → 303 restored."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "RESUI02", "trash", registered=True)

    r = client.post(
        f"/shipments/{sid}/restore",
        data={"_csrf": "csrftoken"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "restored" in r.headers["location"]


def test_ui_purge_no_pending_ops_redirects_to_trash(web_app):
    """POST /shipments/{id}/purge with no pending ops → 303 /trash."""
    client, sf, prov = web_app
    sid = _make_shipment_in_bucket(sf, "PRGUI01", "trash", registered=False)

    r = client.post(
        f"/shipments/{sid}/purge",
        data={"_csrf": "csrftoken"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/trash")


def test_ui_purge_pending_op_shows_blocked_message(web_app):
    """POST /shipments/{id}/purge with pending delete → 303 purge_blocked."""
    from novatrack.models import ProviderOperation, Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(number="PRGUI02", registered=True, status="pending", bucket="trash")
        s.add(ship)
        s.flush()
        op = ProviderOperation(shipment_id=ship.id, kind="delete", done=False)
        s.add(op)
        s.commit()
        sid = ship.id

    r = client.post(
        f"/shipments/{sid}/purge",
        data={"_csrf": "csrftoken"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "purge_blocked" in r.headers["location"]


# ─────────────────────────────────────────────────────────────────────────────
# ETA display: empty dict, null date fields
# ─────────────────────────────────────────────────────────────────────────────


def test_eta_empty_dict_hidden(web_app):
    """Shipment with eta={} must NOT render the ETA section."""
    from novatrack.models import Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(number="ETAEMPTY1", registered=True, status="in_transit", bucket="active", eta={})
        s.add(ship)
        s.commit()
        sid = ship.id

    r = client.get(f"/shipments/{sid}")
    assert r.status_code == 200
    # Empty dict ETA must not render the section
    assert "Estimated Delivery" not in r.text


def test_eta_none_hidden(web_app):
    """Shipment with eta=None must NOT render the ETA section."""
    from novatrack.models import Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(number="ETANONE1", registered=True, status="in_transit", bucket="active", eta=None)
        s.add(ship)
        s.commit()
        sid = ship.id

    r = client.get(f"/shipments/{sid}")
    assert r.status_code == 200
    assert "Estimated Delivery" not in r.text


def test_eta_with_valid_dates_shown(web_app):
    """Shipment with valid eta from/to must render the ETA section."""
    from novatrack.models import Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(
            number="ETAVALID1",
            registered=True,
            status="in_transit",
            bucket="active",
            eta={"from": "2026-11-01T00:00:00Z", "to": "2026-11-05T00:00:00Z", "source": "17TRACK"},
        )
        s.add(ship)
        s.commit()
        sid = ship.id

    r = client.get(f"/shipments/{sid}")
    assert r.status_code == 200
    assert "Estimated Delivery" in r.text
    assert "2026-11-01" in r.text


def test_eta_null_from_shows_emdash(web_app):
    """When eta.from is None but eta.to is present, show em dash for missing field."""
    from novatrack.models import Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(
            number="ETADASH1",
            registered=True,
            status="in_transit",
            bucket="active",
            eta={"from": None, "to": "2026-11-05T00:00:00Z", "source": "17TRACK"},
        )
        s.add(ship)
        s.commit()
        sid = ship.id

    r = client.get(f"/shipments/{sid}")
    assert r.status_code == 200
    assert "Estimated Delivery" in r.text
    assert "—" in r.text  # em dash for null from


# ─────────────────────────────────────────────────────────────────────────────
# Alert position: flash_msg and provider_error before detail-header
# ─────────────────────────────────────────────────────────────────────────────


def test_alert_before_detail_header_in_template():
    """Flash alert and provider_error must appear before detail-header in template."""
    from pathlib import Path

    tmpl_path = Path(__file__).parent.parent / "novatrack" / "templates" / "shipment.html"
    content = tmpl_path.read_text(encoding="utf-8")

    # Find positions
    alert_pos = content.find("alert alert-info")
    provider_error_pos = content.find("provider_error")
    header_pos = content.find("detail-header")

    assert alert_pos < header_pos, (
        f"Flash alert (pos {alert_pos}) must appear BEFORE detail-header (pos {header_pos}) "
        "in shipment.html — alerts must be visible without scrolling"
    )
    assert provider_error_pos < header_pos, (
        f"provider_error (pos {provider_error_pos}) must appear BEFORE detail-header (pos {header_pos}) "
        "in shipment.html"
    )


def test_eta_template_guards_empty_dict():
    """ETA section in template must not render when eta is empty/null."""
    from pathlib import Path

    tmpl_path = Path(__file__).parent.parent / "novatrack" / "templates" / "shipment.html"
    content = tmpl_path.read_text(encoding="utf-8")

    # The template must guard ETA with a check for from/to fields (not just eta truthiness)
    # Our implementation uses {% set _eta_from = ... %} variables and {% if _eta_from or _eta_to %}
    assert "_eta_from" in content and "_eta_to" in content, (
        "Template must use _eta_from/_eta_to variables to guard against empty dict"
    )
    assert "_eta_from or _eta_to" in content or "(_eta_from or _eta_to)" in content, (
        "Template must check both _eta_from and _eta_to before rendering ETA section"
    )


# ─────────────────────────────────────────────────────────────────────────────
# UI: refresh unregistered flash message
# ─────────────────────────────────────────────────────────────────────────────


def test_not_registered_flash_message_no_check_back_shortly(web_app):
    """The not_registered flash message must NOT say 'check back shortly'.

    This was the v0.1.2 bug: it told users to 'check back shortly' even when
    registration was permanently paused for missing required parameters.
    """
    from novatrack.models import Shipment

    client, sf, prov = web_app
    with sf() as s:
        ship = Shipment(
            number="FLASHTEST1",
            registered=False,
            status="pending",
            bucket="active",
            provider_error="Registration requires the destination postal code for this carrier.",
        )
        s.add(ship)
        s.commit()
        sid = ship.id

    r = client.get(f"/shipments/{sid}?msg=not_registered")
    assert r.status_code == 200
    text = r.text.lower()
    assert "check back shortly" not in text, (
        "Flash message must not say 'check back shortly' when registration "
        "may be paused indefinitely for missing parameters"
    )
    # Should mention 'error' or 'parameters'
    assert "error" in text or "param" in text or "update" in text


# ─────────────────────────────────────────────────────────────────────────────
# Version consistency
# ─────────────────────────────────────────────────────────────────────────────


def test_version_consistent_across_files():
    """Version must be 0.1.3 in all relevant files."""
    from pathlib import Path

    root = Path(__file__).parent.parent

    # novatrack/__init__.py
    init_src = (root / "novatrack" / "__init__.py").read_text()
    assert '0.1.3' in init_src, "__init__.py version must be 0.1.3"

    # pyproject.toml
    pyproject = (root / "pyproject.toml").read_text()
    assert 'version = "0.1.3"' in pyproject, "pyproject.toml version must be 0.1.3"

    # Dockerfile
    dockerfile = (root / "Dockerfile").read_text()
    assert "0.1.3" in dockerfile, "Dockerfile VERSION must be 0.1.3"

    # CHANGELOG.md
    changelog = (root / "CHANGELOG.md").read_text()
    assert "0.1.3" in changelog, "CHANGELOG.md must have 0.1.3 entry"
