"""PostgreSQL-only integration gates; TEST_DATABASE_URL must be disposable."""

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from novatrack.db import make_engine, make_session_factory
from novatrack.models import (
    AppSettings,
    Base,
    Delivery,
    Notification,
    NotificationChannel,
    ProviderOperation,
    Shipment,
    TrackingEvent,
    Transition,
    WebhookReceipt,
)


@pytest.fixture
def pg_factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL 16 database")
    engine = make_engine(url)
    schema = "novatrack_test_" + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(scoped)
    sf = make_session_factory(scoped)
    with sf.begin() as session:
        session.add(AppSettings(id=1))
    yield sf
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


def snapshot(number="Concurrent001"):
    # Official v2.2 accepted-item / webhook shape, not the legacy v1 'track'.
    return {
        "number": number,
        "carrier": 100001,
        "track_info": {
            "latest_status": {"status": "InTransit", "sub_status": "InTransit_PickedUp"},
            "latest_event": {
                "time_iso": "2026-10-01T10:00:00Z",
                "time_utc": "2026-10-01T10:00:00Z",
                "description": "Collected",
                "location": "Origin",
            },
            "tracking": {
                "providers": [
                    {
                        "provider": {"name": "Test carrier"},
                        "events": [
                            {
                                "time_iso": "2026-10-01T10:00:00Z",
                                "time_utc": "2026-10-01T10:00:00Z",
                                "description": "Collected",
                                "location": "Origin",
                            }
                        ],
                    }
                ]
            },
            "time_metrics": {"estimated_delivery_date": {"from": "2026-10-05", "to": "2026-10-06"}},
        },
    }


def test_concurrent_snapshots_refresh_locked_identity_map(pg_factory):
    from novatrack.tracking import apply_snapshot, create_shipment

    with pg_factory.begin() as session:
        shipment = create_shipment(session, None, number="Concurrent001", carrier=100001)
        sid = shipment.id
        session.add(
            NotificationChannel(
                name="receiver",
                kind="webhook",
                url="https://example.org/receive",
                secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
                events=["status_changed"],
            )
        )
    barrier = threading.Barrier(2)

    def apply():
        with pg_factory.begin() as session:
            shipment = session.get(Shipment, sid)  # Both transactions preload stale pending state.
            barrier.wait(timeout=10)
            return apply_snapshot(session, shipment, snapshot())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(results) == [False, True]
    with pg_factory() as session:
        assert session.query(Transition).count() == 1
        assert session.query(Notification).count() == 1
        assert session.query(Delivery).count() == 1
        assert session.query(TrackingEvent).count() == 1
        assert session.get(Shipment, sid).status == "in_transit"


def test_concurrent_identical_webhook_is_transactionally_idempotent(pg_factory):
    from novatrack.tracking import apply_webhook, create_shipment

    with pg_factory.begin() as session:
        create_shipment(session, None, number="Concurrent001", carrier=100001)
    payload = {"event": "TRACKING_UPDATED", "data": snapshot()}
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    barrier = threading.Barrier(2)

    def apply():
        with pg_factory.begin() as session:
            barrier.wait(timeout=10)
            return apply_webhook(session, payload, digest)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(results) == [0, 1]
    with pg_factory() as session:
        assert session.query(WebhookReceipt).count() == 1
        assert session.query(Transition).count() == 1


def test_worker_uses_real_adapter_contract_and_processes_registration(pg_factory, monkeypatch):
    from novatrack.config import Settings
    from novatrack.tracking import create_shipment
    from novatrack.worker import tick

    cfg = Settings(
        database_url="sqlite://",
        oidc_issuer="https://idp.example.org",
        oidc_client_id="test",
        oidc_client_secret="test",
        app_base_url="https://app.example.org",
        secret_key="x" * 48,
        track17_api_key="test",
    )
    calls = []

    class Provider:
        def register(self, number, carrier=None, *, param=None):
            calls.append(number)
            return {"number": number, "carrier": 100001}

        def getquota(self):
            return {"quota_total": 200, "quota_used": 160, "quota_remain": 40}

    with pg_factory.begin() as session:
        shipment = create_shipment(session, None, number="Worker001")
        sid = shipment.id
    tick(pg_factory, cfg, Provider())
    assert calls == ["Worker001"]
    with pg_factory() as session:
        assert session.get(Shipment, sid).registered
        assert session.get(Shipment, sid).carrier == 100001
        assert session.query(ProviderOperation).filter_by(done=False).count() == 0
        assert session.get(AppSettings, 1).quota["quota_remain"] == 40
        assert session.query(Notification).filter(Notification.event_key.like("quota_low:%")).count() == 1


def test_postgres_deliveries_do_not_duplicate_between_workers(pg_factory, monkeypatch):
    from types import SimpleNamespace

    from novatrack.notifications import deliver_due, enqueue_event

    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", "s" * 48)
    with pg_factory.begin() as session:
        session.add(
            NotificationChannel(
                name="receiver",
                kind="webhook",
                url="https://example.org/receive",
                secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
                events=["status_changed"],
            )
        )
        session.flush()
        enqueue_event(session, "pg-delivery-one", {"event": "status_changed"})
    requests = []
    gate = threading.Lock()
    barrier = threading.Barrier(2)

    class Receiver:
        def post(self, url, **kwargs):
            with gate:
                requests.append(kwargs["headers"]["X-Parcels-Event-Id"])
            return SimpleNamespace(status_code=204)

    def deliver():
        with pg_factory.begin() as session:
            barrier.wait(timeout=10)
            return deliver_due(
                session,
                SimpleNamespace(telegram_enabled=False),
                client=Receiver(),
                now=datetime(2030, 1, 1, tzinfo=UTC),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))
    assert sum(results) == 1
    assert len(requests) == 1
    with pg_factory() as session:
        assert session.query(Delivery).one().state == "sent"
