"""Durable tracking lifecycle tests — all DB interactions via in-memory SQLite."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from novatrack.db import make_engine, make_session_factory
from novatrack.models import (
    AppSettings,
    Base,
    Notification,
    ProviderOperation,
    Shipment,
    TrackingEvent,
    Transition,
    WebhookReceipt,
)
from novatrack.providers import ProviderError
from novatrack.tracking import (
    apply_snapshot,
    apply_webhook,
    archive_shipment,
    create_shipment,
    housekeeping,
    manual_deliver,
    process_operations,
    refresh_shipment,
    trash_shipment,
)

# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def session(db):
    s = db()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def session_s(session):
    """Session pre-loaded with AppSettings(id=1)."""
    session.add(AppSettings(id=1, archive_days=7, purge_days=30))
    session.flush()
    return session


@pytest.fixture
def provider():
    m = MagicMock()
    m.register.return_value = {"number": "TRACK123", "carrier": 100}
    m.stoptrack.return_value = {"number": "TRACK123"}
    m.deletetrack.return_value = {"number": "TRACK123"}
    return m


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _track_info(
    *,
    status="InTransit",
    sub_status="InTransit_Other",
    event_time="2024-01-15T10:00:00+00:00",
    event_time_utc="2024-01-15T10:00:00Z",
    description="Arrived",
    location="NY",
    eta_from="2024-01-17",
    eta_to="2024-01-18",
):
    """Build a canonical v2.2 track_info dict."""
    return {
        "latest_status": {"status": status, "sub_status": sub_status, "sub_status_descr": None},
        "latest_event": {
            "time_iso": event_time,
            "time_utc": event_time_utc,
            "time_raw": {"date": event_time[:10], "time": "10:00:00", "timezone": "+00:00"},
            "description": description,
            "location": location,
            "stage": status,
            "sub_status": sub_status,
        },
        "time_metrics": {
            "estimated_delivery_date": {"source": "17TRACK", "from": eta_from, "to": eta_to},
        },
        "tracking": {
            "providers_hash": 12345,
            "providers": [
                {
                    "provider": {"key": 100, "name": "TestCarrier"},
                    "events": [
                        {
                            "time_iso": event_time,
                            "time_utc": event_time_utc,
                            "description": description,
                            "location": location,
                            "stage": status,
                            "sub_status": sub_status,
                        }
                    ],
                }
            ],
        },
    }


def _snapshot(
    number,
    *,
    carrier=100,
    status="InTransit",
    sub_status="InTransit_Other",
    event_time="2024-01-15T10:00:00+00:00",
    event_time_utc="2024-01-15T10:00:00Z",
    description="Arrived",
    location="NY",
    eta_from="2024-01-17",
    eta_to="2024-01-18",
):
    """Build a v2.2-shaped gettrackinfo / webhook item with track_info."""
    return {
        "number": number,
        "carrier": carrier,
        "track_info": _track_info(
            status=status,
            sub_status=sub_status,
            event_time=event_time,
            event_time_utc=event_time_utc,
            description=description,
            location=location,
            eta_from=eta_from,
            eta_to=eta_to,
        ),
    }


# ─────────────────────────────────────────────────────────────
# create_shipment
# ─────────────────────────────────────────────────────────────

def test_create_shipment_creates_pending_and_queues_register(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    assert s.number == "TRACK123"
    assert s.carrier == 100
    assert s.status == "pending"
    assert s.bucket == "active"

    ops = session.execute(select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)).scalars().all()
    assert len(ops) == 1
    assert ops[0].kind == "register"
    assert ops[0].done is False


def test_create_shipment_stores_metadata(session, provider):
    s = create_shipment(session, provider, number="TRACK123", name="My Box", store="Amazon", notes="Fragile")
    assert s.name == "My Box"
    assert s.store == "Amazon"
    assert s.notes == "Fragile"


def test_create_shipment_tba_prefix_is_untrackable_no_register(session, provider):
    s = create_shipment(session, provider, number="TBA123456789000")
    assert s.status == "untrackable"
    assert s.bucket == "active"
    ops = session.execute(select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)).scalars().all()
    assert len(ops) == 0


def test_create_shipment_tba_case_insensitive(session, provider):
    """TBA detection must be case-insensitive."""
    s = create_shipment(session, provider, number="tba999000")
    assert s.status == "untrackable"
    ops = session.execute(select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)).scalars().all()
    assert len(ops) == 0


def test_create_shipment_duplicate_number_raises_value_error(session, provider):
    create_shipment(session, provider, number="TRACK123")
    with pytest.raises(ValueError, match="already"):
        create_shipment(session, provider, number="TRACK123")


def test_create_shipment_empty_number_raises_value_error(session, provider):
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="")


def test_create_shipment_whitespace_number_raises_value_error(session, provider):
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="   ")


def test_create_shipment_too_short_number_raises_value_error(session, provider):
    """Number shorter than 5 characters must be rejected (17track docs: 5-50)."""
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="AB1")  # 3 chars


def test_create_shipment_too_long_number_raises_value_error(session, provider):
    """Number longer than 50 characters must be rejected."""
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="A" * 51)


def test_create_shipment_invalid_chars_raises_value_error(session, provider):
    """Non-alphanumeric/hyphen characters must be rejected."""
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="TRACK 123!")  # space + !


def test_create_shipment_hyphen_allowed_in_number(session, provider):
    """Hyphens are explicitly permitted in tracking numbers."""
    s = create_shipment(session, provider, number="TRACK-123-XY")
    assert s.number == "TRACK-123-XY"


def test_create_shipment_preserves_identifier_exactly(session, provider):
    """Tracking number must not be silently uppercased or stripped."""
    s = create_shipment(session, provider, number="track123")
    assert s.number == "track123"  # preserved as-is, not uppercased


def test_create_shipment_zero_carrier_raises_value_error(session, provider):
    """Carrier=0 must be rejected (must be a positive integer)."""
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="TRACK123", carrier=0)


def test_create_shipment_negative_carrier_raises_value_error(session, provider):
    with pytest.raises(ValueError):
        create_shipment(session, provider, number="TRACK123", carrier=-1)


def test_create_shipment_none_carrier_is_allowed(session, provider):
    """carrier=None means auto-detect; must be accepted."""
    s = create_shipment(session, provider, number="TRACK123", carrier=None)
    assert s.carrier is None


def test_create_shipment_positive_carrier_is_allowed(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=3011)
    assert s.carrier == 3011


# ─────────────────────────────────────────────────────────────
# archive_shipment
# ─────────────────────────────────────────────────────────────

def test_archive_shipment_sets_bucket_and_archives_at(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    archive_shipment(session, s, provider)
    assert s.bucket == "archive"
    assert s.archived_at is not None


def test_archive_registered_shipment_queues_stop(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    s.registered = True
    session.flush()
    archive_shipment(session, s, provider)
    ops = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id, ProviderOperation.kind == "stop")
    ).scalars().all()
    assert len(ops) == 1


def test_archive_shipment_with_pending_register_queues_stop(session, provider):
    """Archive immediately after create must queue stop even when not yet registered.

    Previously no stop was queued because registered=False, leaving the
    external resource active forever after the register eventually succeeded.
    """
    s = create_shipment(session, provider, number="TRACK123")
    assert s.registered is False  # not registered yet, but register op is pending
    archive_shipment(session, s, provider)
    ops = session.execute(
        select(ProviderOperation).where(
            ProviderOperation.shipment_id == s.id,
            ProviderOperation.kind == "stop",
        )
    ).scalars().all()
    assert len(ops) == 1


def test_archive_truly_unregistered_no_pending_ops_no_stop(session, provider):
    """If not registered AND no pending register op, no stop should be queued."""
    s = create_shipment(session, provider, number="TRACK123")
    # Mark the register op as done (e.g. it ran but registration failed permanently)
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    op.done = True
    session.flush()

    archive_shipment(session, s, provider)
    stop_ops = session.execute(
        select(ProviderOperation).where(
            ProviderOperation.shipment_id == s.id, ProviderOperation.kind == "stop"
        )
    ).scalars().all()
    assert len(stop_ops) == 0


def test_archive_already_archived_is_idempotent(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    archive_shipment(session, s, provider)
    archive_shipment(session, s, provider)  # must not raise
    assert s.bucket == "archive"


# ─────────────────────────────────────────────────────────────
# trash_shipment
# ─────────────────────────────────────────────────────────────

def test_trash_shipment_sets_bucket_and_trashed_at(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    trash_shipment(session, s, provider)
    assert s.bucket == "trash"
    assert s.trashed_at is not None


def test_trash_shipment_queues_delete_op(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    trash_shipment(session, s, provider)
    ops = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id, ProviderOperation.kind == "delete")
    ).scalars().all()
    assert len(ops) == 1


def test_trash_shipment_idempotent_does_not_duplicate_delete_op(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    trash_shipment(session, s, provider)
    trash_shipment(session, s, provider)
    ops = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id, ProviderOperation.kind == "delete")
    ).scalars().all()
    assert len(ops) == 1  # only one delete op


def test_tba_trash_shipment_no_delete_op(session, provider):
    """TBA (Amazon internal) numbers must never queue a remote delete operation.

    They were never registered externally — a remote delete call is a paid
    operation that must not be triggered for TBA numbers.
    """
    s = create_shipment(session, provider, number="TBA987654321000")
    trash_shipment(session, s, provider)
    all_ops = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalars().all()
    assert len(all_ops) == 0  # no ops ever: no register, no delete


# ─────────────────────────────────────────────────────────────
# manual_deliver
# ─────────────────────────────────────────────────────────────

def test_manual_deliver_sets_delivered_status_and_timestamp(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    manual_deliver(session, s)
    assert s.status == "delivered"
    assert s.delivered_at is not None
    assert s.status_at is not None


def test_manual_deliver_creates_transition_record(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    manual_deliver(session, s)
    t = session.execute(select(Transition).where(Transition.shipment_id == s.id)).scalar_one()
    assert t.from_status == "pending"
    assert t.to_status == "delivered"
    assert t.version == 1


def test_manual_deliver_emits_delivered_notification(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    manual_deliver(session, s)
    n = session.execute(select(Notification)).scalar_one()
    assert n.payload["event"] == "delivered"
    assert n.payload["to_status"] == "delivered"


def test_manual_deliver_increments_version(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    assert s.version == 0
    manual_deliver(session, s)
    assert s.version == 1


def test_manual_deliver_already_delivered_is_idempotent(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    manual_deliver(session, s)
    manual_deliver(session, s)
    notifications = session.execute(select(Notification)).scalars().all()
    assert len(notifications) == 1  # no duplicate notification


# ─────────────────────────────────────────────────────────────
# apply_snapshot  (v2.2 track_info shape)
# ─────────────────────────────────────────────────────────────

def test_apply_snapshot_changes_status_and_returns_true(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    changed = apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit"))
    assert changed is True
    assert s.status == "in_transit"


def test_apply_snapshot_adds_tracking_events(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123"))
    events = session.execute(select(TrackingEvent).where(TrackingEvent.shipment_id == s.id)).scalars().all()
    assert len(events) >= 1


def test_apply_snapshot_deduplicates_events(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    item = _snapshot("TRACK123")
    apply_snapshot(session, s, item)
    apply_snapshot(session, s, item)
    events = session.execute(select(TrackingEvent).where(TrackingEvent.shipment_id == s.id)).scalars().all()
    assert len(events) == 1  # fingerprint dedup


def test_apply_snapshot_stores_eta(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", eta_from="2024-02-01", eta_to="2024-02-03"))
    assert s.eta is not None
    assert s.eta.get("from") == "2024-02-01"


def test_apply_snapshot_increments_version_and_creates_transition(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit"))
    assert s.version == 1
    t = session.execute(select(Transition).where(Transition.shipment_id == s.id)).scalar_one()
    assert t.version == 1
    assert t.from_status == "pending"
    assert t.to_status == "in_transit"


def test_apply_snapshot_emits_notification(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit"))
    n = session.execute(select(Notification)).scalar_one()
    assert n.payload["event"] in ("status_changed", "in_transit", "delivered", "exception")


def test_apply_snapshot_no_change_returns_false(session, provider):
    """Same status twice: second call must return False."""
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit"))
    changed = apply_snapshot(
        session, s,
        _snapshot("TRACK123", status="InTransit", event_time="2024-01-16T10:00:00+00:00",
                  event_time_utc="2024-01-16T10:00:00Z"),
    )
    assert changed is False


def test_apply_snapshot_stale_guard_does_not_downgrade_status(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    # Fresh snapshot with later event time
    apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit",
                                         event_time="2024-01-20T10:00:00+00:00",
                                         event_time_utc="2024-01-20T10:00:00Z"))
    assert s.status == "in_transit"
    # Stale snapshot with earlier event time must not update status
    changed = apply_snapshot(session, s, _snapshot("TRACK123", status="Delivered",
                                                    event_time="2024-01-15T10:00:00+00:00",
                                                    event_time_utc="2024-01-15T10:00:00Z"))
    assert changed is False
    assert s.status == "in_transit"


def test_apply_snapshot_delivered_terminal_blocks_status_change(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    manual_deliver(session, s)
    # Later snapshot says InTransit — must not change back
    changed = apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit",
                                                    event_time="2099-01-01T00:00:00+00:00",
                                                    event_time_utc="2099-01-01T00:00:00Z"))
    assert changed is False
    assert s.status == "delivered"


def test_apply_snapshot_archived_bucket_blocks_status_change(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    s.bucket = "archive"
    session.flush()
    changed = apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit",
                                                    event_time="2099-01-01T00:00:00+00:00",
                                                    event_time_utc="2099-01-01T00:00:00Z"))
    assert changed is False


def test_apply_snapshot_trashed_bucket_blocks_status_change(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    s.bucket = "trash"
    session.flush()
    changed = apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit",
                                                    event_time="2099-01-01T00:00:00+00:00",
                                                    event_time_utc="2099-01-01T00:00:00Z"))
    assert changed is False


def test_apply_snapshot_preserves_historical_events(session, provider):
    """Older snapshots with fewer events must not erase already-stored events."""
    s = create_shipment(session, provider, number="TRACK123")
    snap1 = _snapshot("TRACK123", event_time="2024-01-15T10:00:00+00:00",
                       event_time_utc="2024-01-15T10:00:00Z", description="Event A")
    apply_snapshot(session, s, snap1)

    snap2 = _snapshot("TRACK123", event_time="2024-01-16T10:00:00+00:00",
                       event_time_utc="2024-01-16T10:00:00Z", description="Event B")
    apply_snapshot(session, s, snap2)

    events = session.execute(select(TrackingEvent).where(TrackingEvent.shipment_id == s.id)).scalars().all()
    descs = {e.description for e in events}
    assert "Event A" in descs
    assert "Event B" in descs


def test_apply_snapshot_exception_event_key(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", status="Exception",
                                         event_time="2024-01-15T10:00:00+00:00",
                                         event_time_utc="2024-01-15T10:00:00Z"))
    n = session.execute(select(Notification)).scalar_one()
    assert n.payload["event"] == "exception"


def test_apply_snapshot_delivered_event_key(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    apply_snapshot(session, s, _snapshot("TRACK123", status="Delivered",
                                         event_time="2024-01-15T10:00:00+00:00",
                                         event_time_utc="2024-01-15T10:00:00Z"))
    n = session.execute(select(Notification)).scalar_one()
    assert n.payload["event"] == "delivered"
    assert s.delivered_at is not None


def test_apply_snapshot_eta_not_overwritten_by_stale_snapshot(session, provider):
    """ETA must not be overwritten by a stale snapshot."""
    s = create_shipment(session, provider, number="TRACK123")
    # First snapshot with a later event time; sets ETA
    apply_snapshot(session, s, _snapshot("TRACK123",
                                          event_time="2024-01-20T10:00:00+00:00",
                                          event_time_utc="2024-01-20T10:00:00Z",
                                          eta_from="2024-02-01", eta_to="2024-02-03"))
    original_eta = dict(s.eta)

    # Stale snapshot (older event time) with different ETA
    apply_snapshot(session, s, _snapshot("TRACK123",
                                          event_time="2024-01-15T10:00:00+00:00",
                                          event_time_utc="2024-01-15T10:00:00Z",
                                          eta_from="2024-03-01", eta_to="2024-03-05"))
    assert s.eta == original_eta


def test_apply_snapshot_no_timestamps_with_cursor_treated_as_stale(session, provider):
    """Snapshot with no event timestamps + existing cursor must not regress status."""
    s = create_shipment(session, provider, number="TRACK123")
    # First snapshot sets the cursor
    apply_snapshot(session, s, _snapshot("TRACK123", status="InTransit",
                                          event_time="2024-01-20T10:00:00+00:00",
                                          event_time_utc="2024-01-20T10:00:00Z"))
    assert s.status == "in_transit"

    # Second snapshot: no timestamps in any events — must not change status
    snap_no_ts = {
        "number": "TRACK123",
        "carrier": 100,
        "track_info": {
            "latest_status": {"status": "Delivered", "sub_status": "Delivered_Other"},
            "tracking": {
                "providers": [{
                    "provider": {"key": 100, "name": "Test"},
                    "events": [
                        # events without time_iso or time_utc
                        {"description": "Out for delivery", "location": "NYC"},
                    ],
                }]
            },
        },
    }
    changed = apply_snapshot(session, s, snap_no_ts)
    assert changed is False
    assert s.status == "in_transit"  # not regressed to delivered


def test_apply_snapshot_empty_status_does_not_transition_to_untrackable(session, provider):
    """Missing/empty status string must not cause a transition to 'untrackable'."""
    s = create_shipment(session, provider, number="TRACK123")
    snap_empty_status = {
        "number": "TRACK123",
        "carrier": 100,
        "track_info": {
            "latest_status": {},  # no status field
            "latest_event": {
                "time_iso": "2024-01-25T10:00:00+00:00",
                "time_utc": "2024-01-25T10:00:00Z",
                "description": "Status update",
                "location": "NY",
            },
            "tracking": {
                "providers": [{
                    "provider": {"key": 100, "name": "T"},
                    "events": [{
                        "time_iso": "2024-01-25T10:00:00+00:00",
                        "time_utc": "2024-01-25T10:00:00Z",
                        "description": "Status update",
                    }],
                }]
            },
        },
    }
    changed = apply_snapshot(session, s, snap_empty_status)
    assert changed is False
    assert s.status == "pending"  # not changed to "untrackable"


def test_apply_snapshot_null_fields_normalize_to_empty_string(session, provider):
    """Null description/location must normalize to empty string, not crash."""
    s = create_shipment(session, provider, number="TRACK123")
    snap_nulls = {
        "number": "TRACK123",
        "carrier": 100,
        "track_info": {
            "latest_status": {"status": "InTransit", "sub_status": "InTransit_Other"},
            "tracking": {
                "providers": [{
                    "provider": None,  # null provider
                    "events": [{
                        "time_iso": "2024-01-15T10:00:00+00:00",
                        "time_utc": "2024-01-15T10:00:00Z",
                        "description": None,  # null description
                        "location": None,    # null location
                    }],
                }]
            },
        },
    }
    apply_snapshot(session, s, snap_nulls)
    ev = session.execute(select(TrackingEvent).where(TrackingEvent.shipment_id == s.id)).scalar_one()
    assert ev.description == ""
    assert ev.location == ""
    assert ev.provider == ""


# ─────────────────────────────────────────────────────────────
# refresh_shipment
# ─────────────────────────────────────────────────────────────

def test_refresh_shipment_calls_gettrackinfo_and_applies_snapshot(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    provider.gettrackinfo.return_value = _snapshot("TRACK123", carrier=100, status="InTransit")
    changed = refresh_shipment(session, s, provider)
    provider.gettrackinfo.assert_called_once_with("TRACK123", 100)
    assert changed is True
    assert s.status == "in_transit"


# ─────────────────────────────────────────────────────────────
# apply_webhook
# ─────────────────────────────────────────────────────────────

def test_apply_webhook_stores_receipt_and_updates_shipment(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "TRACKING_UPDATED",
        "data": _snapshot("TRACK123", carrier=100, status="InfoReceived"),
    }
    count = apply_webhook(session, payload, "digest001")
    assert count >= 1
    assert session.get(WebhookReceipt, "digest001") is not None
    assert s.status == "info_received"


def test_apply_webhook_duplicate_digest_is_noop(session, provider):
    create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "TRACKING_UPDATED",
        "data": _snapshot("TRACK123", carrier=100, status="InfoReceived"),
    }
    apply_webhook(session, payload, "digest_dup")
    count2 = apply_webhook(session, payload, "digest_dup")
    assert count2 == 0


def test_apply_webhook_tracking_stopped_no_invented_delivery(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {"event": "TRACKING_STOPPED", "data": {"number": "TRACK123", "carrier": 100}}
    apply_webhook(session, payload, "digest_stop")
    assert s.status != "delivered"


def test_apply_webhook_unknown_event_acknowledged_no_mutation(session, provider):
    """Unknown event types must store a receipt but must NOT mutate shipment state."""
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "SOME_FUTURE_EVENT",
        "data": _snapshot("TRACK123", carrier=100, status="Delivered"),
    }
    count = apply_webhook(session, payload, "digest_unknown_event")
    # Receipt is stored (acknowledged)
    assert session.get(WebhookReceipt, "digest_unknown_event") is not None
    # Status must NOT have changed
    assert s.status == "pending"
    assert count == 0


def test_apply_webhook_list_data_also_works(session, provider):
    """Data as a list of items (bulk/legacy) must also be processed."""
    create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "TRACKING_UPDATED",
        "data": [_snapshot("TRACK123", carrier=100, status="InfoReceived")],
    }
    count = apply_webhook(session, payload, "digest_list")
    assert count >= 0  # must not raise


def test_apply_webhook_accepts_accepted_list_data(session, provider):
    create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "TRACKING_UPDATED",
        "data": {"accepted": [_snapshot("TRACK123", carrier=100, status="InfoReceived")]},
    }
    count = apply_webhook(session, payload, "digest_acc")
    assert count >= 0


def test_apply_webhook_list_entry_not_dict_raises_value_error(session, provider):
    """Non-dict items in data list must raise ValueError, not AttributeError."""
    payload = {
        "event": "TRACKING_UPDATED",
        "data": ["not-a-dict"],
    }
    with pytest.raises(ValueError, match="dict"):
        apply_webhook(session, payload, "digest_bad_list")


def test_apply_webhook_malformed_not_dict_raises_value_error(session):
    with pytest.raises(ValueError):
        apply_webhook(session, "not a dict", "digest_bad")


def test_apply_webhook_missing_data_key_raises_value_error(session):
    with pytest.raises(ValueError):
        apply_webhook(session, {"event": "TRACKING_UPDATED"}, "digest_nodata")


def test_apply_webhook_unknown_number_does_not_crash(session):
    """Webhook for unknown tracking number is silently ignored (no match)."""
    payload = {
        "event": "TRACKING_UPDATED",
        "data": _snapshot("UNKNOWN999", carrier=100),
    }
    count = apply_webhook(session, payload, "digest_unk")
    assert count == 0


def test_apply_webhook_null_carrier_filled_from_webhook(session, provider):
    """Shipment with carrier=NULL must have its carrier filled from the signed webhook."""
    s = create_shipment(session, provider, number="TRACK123")  # carrier=None
    assert s.carrier is None
    payload = {
        "event": "TRACKING_UPDATED",
        "data": _snapshot("TRACK123", carrier=100, status="InfoReceived"),
    }
    apply_webhook(session, payload, "digest_fill_carrier")
    assert s.carrier == 100
    assert s.status == "info_received"


def test_apply_webhook_mismatched_carrier_ignored(session, provider):
    """Webhook with a carrier that differs from the known shipment carrier is ignored."""
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    payload = {
        "event": "TRACKING_UPDATED",
        "data": _snapshot("TRACK123", carrier=999, status="Delivered"),
    }
    apply_webhook(session, payload, "digest_mismatch")
    assert s.status == "pending"  # not updated


# ─────────────────────────────────────────────────────────────
# process_operations
# ─────────────────────────────────────────────────────────────

def test_process_operations_runs_register_marks_done(session, provider):
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    provider.register.return_value = {"number": "TRACK123", "carrier": 100}
    count = process_operations(session, provider)
    assert count == 1
    provider.register.assert_called_once_with("TRACK123", 100)
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    assert op.done is True
    assert s.registered is True


def test_process_operations_persists_detected_carrier(session, provider):
    s = create_shipment(session, provider, number="TRACK123")  # no carrier
    provider.register.return_value = {"number": "TRACK123", "carrier": 200}
    process_operations(session, provider)
    assert s.carrier == 200


def test_process_operations_persists_corrected_carrier_even_when_original_set(session, provider):
    """Provider may correct a wrong carrier; always persist the response carrier."""
    s = create_shipment(session, provider, number="TRACK123", carrier=100)
    # Provider auto-corrects to carrier 3011
    provider.register.return_value = {"number": "TRACK123", "carrier": 3011}
    process_operations(session, provider)
    assert s.carrier == 3011


def test_process_operations_register_before_stop_for_same_shipment(session, provider):
    """Register must execute before stop even if stop is 'more due'."""
    s = create_shipment(session, provider, number="TRACK123")
    # Register op exists; add a stop op that is due slightly earlier
    stop_op = ProviderOperation(
        shipment_id=s.id, kind="stop",
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    session.add(stop_op)
    session.flush()

    call_order = []
    provider.register.side_effect = lambda *a, **kw: call_order.append("register") or {"number": "TRACK123"}
    provider.stoptrack.side_effect = lambda *a, **kw: call_order.append("stop") or {"number": "TRACK123"}

    process_operations(session, provider)

    assert call_order.index("register") < call_order.index("stop")


def test_process_operations_stop_waits_for_pending_register(session, provider):
    """Stop must not run while a register op for the same shipment is pending (retrying)."""
    s = create_shipment(session, provider, number="TRACK123")
    stop_op = ProviderOperation(shipment_id=s.id, kind="stop")
    session.add(stop_op)
    session.flush()

    # Register fails → goes to retry state, remains done=False
    provider.register.side_effect = ProviderError("network glitch")
    process_operations(session, provider)

    provider.register.assert_called_once()
    provider.stoptrack.assert_not_called()


def test_process_operations_delete_waits_for_pending_register(session, provider):
    """Delete must not run while register is still pending."""
    s = create_shipment(session, provider, number="TRACK123")
    del_op = ProviderOperation(shipment_id=s.id, kind="delete")
    session.add(del_op)
    session.flush()

    provider.register.side_effect = ProviderError("fail")
    process_operations(session, provider)

    provider.deletetrack.assert_not_called()


def test_process_operations_retry_on_provider_error(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    provider.register.side_effect = ProviderError("network glitch")
    count = process_operations(session, provider)
    assert count == 0
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    assert op.done is False
    assert op.attempts == 1
    assert op.next_attempt_at > datetime.now(UTC)
    assert s.provider_error is not None


def test_process_operations_error_message_is_sanitized(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    provider.register.side_effect = ProviderError("provider error code -1")
    process_operations(session, provider)
    assert s.provider_error is not None
    assert len(s.provider_error) <= 200


def test_process_operations_skips_not_yet_due(session, provider):
    s = create_shipment(session, provider, number="TRACK123")
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    op.next_attempt_at = datetime.now(UTC) + timedelta(hours=1)
    session.flush()
    count = process_operations(session, provider)
    assert count == 0
    provider.register.assert_not_called()


def test_process_operations_respects_limit(session, provider):
    """With limit=1, only one operation runs even if two are due."""
    create_shipment(session, provider, number="TRACK001")
    create_shipment(session, provider, number="TRACK002")
    provider.register.return_value = {"number": "dummy"}
    count = process_operations(session, provider, limit=1)
    assert count == 1


def test_process_operations_unknown_kind_fails_not_marks_done(session, provider):
    """Unknown op kind must be recorded as failure, NOT silently marked done."""
    s = create_shipment(session, provider, number="TRACK123")
    # Mark the register op as already done
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    op.done = True
    session.flush()

    # Add an op with an unknown kind
    bad_op = ProviderOperation(shipment_id=s.id, kind="retrack")
    session.add(bad_op)
    session.flush()

    process_operations(session, provider)

    session.refresh(bad_op)
    assert bad_op.done is False
    assert bad_op.last_error is not None
    assert "retrack" in bad_op.last_error.lower() or "unknown" in bad_op.last_error.lower()


def test_process_operations_delete_sets_registered_false(session, provider):
    """After a successful remote delete, registered must become False."""
    s = create_shipment(session, provider, number="TRACK123")
    s.registered = True
    reg_op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    reg_op.done = True
    # Add delete op
    del_op = ProviderOperation(shipment_id=s.id, kind="delete")
    session.add(del_op)
    session.flush()

    provider.deletetrack.return_value = {"number": "TRACK123"}
    count = process_operations(session, provider)
    assert count == 1
    assert s.registered is False


def test_process_operations_backoff_caps_exponent(session, provider):
    """High attempt counts must not cause huge integer arithmetic."""
    s = create_shipment(session, provider, number="TRACK123")
    op = session.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalar_one()
    # Simulate many attempts
    op.attempts = 200
    session.flush()
    provider.register.side_effect = ProviderError("fail")

    # Must not raise OverflowError or similar
    process_operations(session, provider)

    session.refresh(op)
    # delay must still be within the cap (3600 seconds)
    from novatrack.tracking import _backoff_delay
    assert _backoff_delay(201) <= 3600


# ─────────────────────────────────────────────────────────────
# housekeeping
# ─────────────────────────────────────────────────────────────

def _ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def test_housekeeping_archives_delivered_older_than_archive_days(session_s, provider):
    s = create_shipment(session_s, provider, number="TRACK123")
    s.status = "delivered"
    s.delivered_at = _ago(8)  # 8 days > archive_days=7
    session_s.flush()
    housekeeping(session_s, provider, now=datetime.now(UTC))
    assert s.bucket == "archive"


def test_housekeeping_does_not_archive_recently_delivered(session_s, provider):
    s = create_shipment(session_s, provider, number="TRACK123")
    s.status = "delivered"
    s.delivered_at = _ago(3)  # only 3 days
    session_s.flush()
    housekeeping(session_s, provider, now=datetime.now(UTC))
    assert s.bucket == "active"


def test_housekeeping_purges_trash_when_no_pending_ops(session_s, provider):
    # TBA: never registered, no ops ever queued, safe to purge
    s = create_shipment(session_s, provider, number="TBA999000000001")
    s.bucket = "trash"
    s.trashed_at = _ago(31)
    session_s.flush()
    sid = s.id
    housekeeping(session_s, provider, now=datetime.now(UTC))
    assert session_s.get(Shipment, sid) is None


def test_housekeeping_does_not_purge_when_delete_still_pending(session_s, provider):
    s = create_shipment(session_s, provider, number="TRACK123")
    trash_shipment(session_s, s, provider)
    s.trashed_at = _ago(31)
    session_s.flush()
    sid = s.id
    housekeeping(session_s, provider, now=datetime.now(UTC))
    assert session_s.get(Shipment, sid) is not None


def test_housekeeping_does_not_purge_when_register_still_pending(session_s, provider):
    """Pending register op (any kind) must block purge — not just delete ops.

    Previously only delete ops were checked; a pending register left a dangling
    remote resource if housekeeping purged the local shipment.
    """
    s = create_shipment(session_s, provider, number="TRACK123")
    trash_shipment(session_s, s, provider)
    s.trashed_at = _ago(31)
    # Mark delete done but leave register pending
    for op in session_s.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalars().all():
        if op.kind == "delete":
            op.done = True
    session_s.flush()

    sid = s.id
    housekeeping(session_s, provider, now=datetime.now(UTC))
    # Still should NOT purge: register op is still pending
    assert session_s.get(Shipment, sid) is not None


def test_housekeeping_purges_after_all_ops_marked_done(session_s, provider):
    s = create_shipment(session_s, provider, number="TRACK123")
    trash_shipment(session_s, s, provider)
    s.trashed_at = _ago(31)
    for op in session_s.execute(
        select(ProviderOperation).where(ProviderOperation.shipment_id == s.id)
    ).scalars().all():
        op.done = True
    session_s.flush()
    sid = s.id
    housekeeping(session_s, provider, now=datetime.now(UTC))
    assert session_s.get(Shipment, sid) is None


def test_housekeeping_retains_notifications_on_purge(session_s, provider):
    """Notification records must survive after shipment purge."""
    s = create_shipment(session_s, provider, number="TBA000000000001")
    manual_deliver(session_s, s)
    s.bucket = "trash"
    s.trashed_at = _ago(31)
    session_s.flush()
    housekeeping(session_s, provider, now=datetime.now(UTC))
    # Notification still present even though shipment is gone
    notes = session_s.execute(select(Notification)).scalars().all()
    assert len(notes) >= 1
