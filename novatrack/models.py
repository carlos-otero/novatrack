"""Persistent domain, durable operations and notification delivery audit."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(50), unique=True)
    carrier: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200), default="")
    store: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    bucket: Mapped[str] = mapped_column(String(16), default="active", index=True)
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta: Mapped[dict | None] = mapped_column(JSON)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=0)
    provider_error: Mapped[str | None] = mapped_column(String(200))
    # Optional additional tracking parameters required by some carriers
    destination_country: Mapped[str | None] = mapped_column(String(2))
    destination_postal_code: Mapped[str | None] = mapped_column(String(20))
    phone_number_last_4: Mapped[str | None] = mapped_column(String(10))


class TrackingEvent(Base):
    __tablename__ = "tracking_events"
    __table_args__ = (UniqueConstraint("shipment_id", "fingerprint"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(500), default="")
    provider: Mapped[str] = mapped_column(String(200), default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Transition(Base):
    __tablename__ = "transitions"
    __table_args__ = (UniqueConstraint("shipment_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer)


class ProviderOperation(Base):
    __tablename__ = "provider_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shipment_id: Mapped[str] = mapped_column(ForeignKey("shipments.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(200))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSettings(Base):
    __tablename__ = "app_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    archive_days: Mapped[int] = mapped_column(Integer, default=7)
    purge_days: Mapped[int] = mapped_column(Integer, default=30)
    quota: Mapped[dict | None] = mapped_column(JSON)
    quota_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_alert_period: Mapped[str | None] = mapped_column(String(100))


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16), default="webhook")
    url: Mapped[str | None] = mapped_column(String(2048))
    events: Mapped[list] = mapped_column(
        JSON, default=lambda: ["status_changed", "delivered", "exception", "quota_low"]
    )
    secret_env: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("notification_id", "channel_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    notification_id: Mapped[str] = mapped_column(ForeignKey("notifications.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(ForeignKey("notification_channels.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(200))


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("deliveries.id", ondelete="CASCADE"), index=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status_code: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(String(200))


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
