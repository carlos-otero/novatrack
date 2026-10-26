import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from novatrack.config import Settings
from novatrack.db import make_engine, make_session_factory
from novatrack.models import AppSettings, Base, Shipment


def test_config_requires_secure_canonical_urls():
    with pytest.raises(ValidationError):
        Settings(
            database_url="sqlite://",
            oidc_issuer="http://idp.invalid",
            oidc_client_id="test",
            oidc_client_secret="test",
            app_base_url="http://example.invalid",
            secret_key="short",
            track17_api_key="test",
        )


def test_empty_optional_telegram_values_are_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_THREAD_ID", "")
    settings = Settings(
        database_url="sqlite://",
        oidc_issuer="https://idp.invalid",
        oidc_client_id="test",
        oidc_client_secret="test",
        app_base_url="https://example.invalid",
        secret_key="s" * 48,
        track17_api_key="test",
    )
    assert settings.telegram_thread_id is None


def test_database_models_have_durable_operation_outbox_and_unique_numbers():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert {
        "shipments",
        "provider_operations",
        "notifications",
        "deliveries",
        "delivery_attempts",
        "webhook_receipts",
        "api_keys",
        "app_settings",
        "transitions",
        "tracking_events",
        "notification_channels",
    } <= set(inspect(engine).get_table_names())
    factory = make_session_factory(engine)
    with factory.begin() as s:
        shipment = Shipment(number="TBA12345678")
        s.add(shipment)
        s.add(AppSettings(id=1))
        s.flush()
        assert shipment.status == "pending"
        assert shipment.bucket == "active"
        assert shipment.id
        assert s.get(AppSettings, 1).archive_days == 7
    with pytest.raises(Exception):
        with factory.begin() as s:
            s.add(Shipment(number="TBA12345678"))
