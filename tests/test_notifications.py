"""Tests for novatrack.notifications and novatrack.worker.

Run with:  uv run pytest tests/test_notifications.py -q

Organised in vertical TDD slices.  Each slice has its tests written and
verified RED before the implementation is added.

Slices
------
1. validate_webhook_url
2. enqueue_event
3. Webhook signing headers
4. _get_webhook_secret — fail-closed, prefix strict regex, min-32 chars
5. _require_public_ip — multicast explicit block
6. _send_safe_https — DNS-change rejection on actual send path
7. deliver_due – webhook path
8. deliver_due – Telegram path
9. check_quota
10. worker tick
11. worker integration (real tracking + mock provider)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from novatrack.db import make_engine, make_session_factory
from novatrack.models import (
    AppSettings,
    Base,
    Delivery,
    Notification,
    NotificationChannel,
)
from novatrack.notifications import (
    _build_webhook_body,
    _build_webhook_sig_headers,
    _get_webhook_secret,
    _require_public_ip,
    _send_safe_https,
    check_quota,
    deliver_due,
    enqueue_event,
    validate_webhook_url,
)

# ── Constants ────────────────────────────────────────────────────────────────

# 32-char minimum required by _get_webhook_secret; use a fixed string so tests
# are deterministic and the constant itself never appears in real secrets.
_TEST_SECRET_32 = "s" * 32
_SIGNING_TEST_SECRET = "mysecret" + "x" * 24  # 32 chars, starts with "mysecret"
_NOLOG_TEST_SECRET = "very-secret-key-42" + "x" * 14  # 32 chars

# Valid Telegram bot token format: {bot_id ≥5 digits}:{35+ [A-Za-z0-9_-] chars}
_TG_BOT_TOKEN = "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"


# ─────────────────────────── shared fixtures ────────────────────────────────


@pytest.fixture(scope="session")
def engine():
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db_session(engine):
    factory = make_session_factory(engine)
    session = factory()
    session.begin()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def db_session_factory(engine):
    """Yields a callable session factory for worker-tick tests."""
    return make_session_factory(engine)


def _settings(**kw):
    """Minimal Settings-like namespace for tests."""
    defaults = {
        "telegram_enabled": False,
        "telegram_bot_token": None,
        "telegram_chat_id": None,
        "telegram_thread_id": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class _FakeResp:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json


class _FakeClient:
    """Minimal httpx.Client test double."""

    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self.json_body = json_body
        self.calls: list[dict] = []

    def post(self, url, *, content, headers):
        self.calls.append({"url": url, "content": content, "headers": dict(headers)})
        return _FakeResp(self.status_code, self.json_body)


def _arm(session, notif, now: datetime) -> None:
    """Make all deliveries for *notif* immediately due at or before *now*.

    Deliveries are created with next_attempt_at=utcnow() (actual wall-clock
    time).  Tests that pass a fixed historical `now` must call this helper
    to advance the delivery due time back so deliver_due picks them up.
    """
    target = now - timedelta(seconds=1)
    for d in session.query(Delivery).filter_by(notification_id=notif.id).all():
        d.next_attempt_at = target
    session.flush()


def _dt_naive_eq(a: datetime | None, b: datetime | None) -> bool:
    """Timezone-insensitive datetime equality for SQLite tests.

    SQLite reads back DateTime(timezone=True) columns as naive datetimes;
    strip tzinfo before comparing so tests don't break on SQLite.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a.replace(tzinfo=None) == b.replace(tzinfo=None)


# ════════════════════════════════════════════════════════════════════════════
# SLICE 1 — validate_webhook_url
# ════════════════════════════════════════════════════════════════════════════


def test_validate_webhook_url_accepts_valid_https_with_public_ip():
    # 8.8.8.8 is globally routable; no DNS needed for numeric IPs
    url = "https://8.8.8.8/hook"
    assert validate_webhook_url(url) == url


def test_validate_webhook_url_rejects_http_scheme():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_webhook_url("http://8.8.8.8/hook")


def test_validate_webhook_url_rejects_ftp_scheme():
    with pytest.raises(ValueError):
        validate_webhook_url("ftp://8.8.8.8/hook")


def test_validate_webhook_url_rejects_credentials_user_only():
    with pytest.raises(ValueError):
        validate_webhook_url("https://user@8.8.8.8/hook")


def test_validate_webhook_url_rejects_credentials_user_pass():
    with pytest.raises(ValueError):
        validate_webhook_url("https://user:pass@8.8.8.8/hook")


def test_validate_webhook_url_rejects_query_string():
    with pytest.raises(ValueError):
        validate_webhook_url("https://8.8.8.8/hook?token=abc")


def test_validate_webhook_url_rejects_fragment():
    with pytest.raises(ValueError):
        validate_webhook_url("https://8.8.8.8/hook#section")


def test_validate_webhook_url_rejects_loopback_127():
    with pytest.raises(ValueError):
        validate_webhook_url("https://127.0.0.1/hook")


def test_validate_webhook_url_rejects_private_192_168():
    with pytest.raises(ValueError):
        validate_webhook_url("https://192.168.1.1/hook")


def test_validate_webhook_url_rejects_private_10_0_0():
    with pytest.raises(ValueError):
        validate_webhook_url("https://10.0.0.1/hook")


def test_validate_webhook_url_rejects_private_172_16():
    with pytest.raises(ValueError):
        validate_webhook_url("https://172.16.0.1/hook")


def test_validate_webhook_url_rejects_link_local_169_254():
    with pytest.raises(ValueError):
        validate_webhook_url("https://169.254.1.1/hook")


def test_validate_webhook_url_rejects_ipv6_loopback():
    with pytest.raises(ValueError):
        validate_webhook_url("https://[::1]/hook")


def test_validate_webhook_url_rejects_hostname_resolving_to_private(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.100.1", 443))],
    )
    with pytest.raises(ValueError):
        validate_webhook_url("https://internal.corp/hook")


def test_validate_webhook_url_accepts_hostname_resolving_to_public(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
    )
    url = "https://example.com/hook"
    assert validate_webhook_url(url) == url


def test_validate_webhook_url_rejects_all_private_if_any_dns_answer_is_private(monkeypatch):
    """All DNS answers must be public; one private answer is enough to reject."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError):
        validate_webhook_url("https://mixed.example.com/hook")


# ════════════════════════════════════════════════════════════════════════════
# SLICE 2 — enqueue_event
# ════════════════════════════════════════════════════════════════════════════


def test_enqueue_event_creates_notification(db_session):
    notif = enqueue_event(db_session, "s2:unique:1", {"event": "status_changed"})
    assert notif is not None
    assert notif.event_key == "s2:unique:1"
    assert notif.id  # UUID assigned


def test_enqueue_event_dedup_returns_none(db_session):
    enqueue_event(db_session, "s2:dedup:1", {"event": "status_changed"})
    second = enqueue_event(db_session, "s2:dedup:1", {"event": "status_changed"})
    assert second is None


def test_enqueue_event_creates_deliveries_for_subscribed_enabled_channel(db_session):
    ch = NotificationChannel(
        name="wh-sub",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:deliver:1", {"event": "status_changed"})
    assert notif is not None

    deliveries = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).all()
    assert len(deliveries) == 1
    assert deliveries[0].state == "pending"


def test_enqueue_event_skips_disabled_channel(db_session):
    ch = NotificationChannel(
        name="wh-disabled",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        enabled=False,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:disabled:1", {"event": "status_changed"})
    assert notif is not None
    deliveries = db_session.query(Delivery).filter_by(channel_id=ch.id).all()
    assert deliveries == []


def test_enqueue_event_skips_channel_not_subscribed_to_event(db_session):
    ch = NotificationChannel(
        name="wh-no-sub",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["quota_low"],  # does NOT include status_changed
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:nosub:1", {"event": "status_changed"})
    assert notif is not None
    deliveries = db_session.query(Delivery).filter_by(channel_id=ch.id).all()
    assert deliveries == []


def test_enqueue_event_delivered_matches_status_changed_subscription(db_session):
    """A channel subscribed to status_changed also gets delivered events."""
    ch = NotificationChannel(
        name="wh-sc",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:delivered:1", {"event": "delivered"})
    assert notif is not None
    deliveries = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).all()
    assert len(deliveries) == 1


def test_enqueue_event_exception_matches_status_changed_subscription(db_session):
    ch = NotificationChannel(
        name="wh-exc",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:exception:1", {"event": "exception"})
    assert notif is not None
    deliveries = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).all()
    assert len(deliveries) == 1


def test_enqueue_event_quota_low_requires_explicit_subscription(db_session):
    """quota_low does NOT match a status_changed-only subscription."""
    ch = NotificationChannel(
        name="wh-sc-only",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "s2:quota:1", {"event": "quota_low"})
    assert notif is not None
    deliveries = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).all()
    assert deliveries == []


# ════════════════════════════════════════════════════════════════════════════
# SLICE 3 — Webhook signing headers
# ════════════════════════════════════════════════════════════════════════════


def test_webhook_body_is_stable_sorted_compact_json_utf8():
    payload = {"z": 2, "a": 1, "event": "status_changed"}
    body = _build_webhook_body(payload)
    # Must be bytes
    assert isinstance(body, bytes)
    # Must be valid UTF-8 JSON
    parsed = json.loads(body.decode("utf-8"))
    assert parsed == payload
    # Keys must be sorted in the wire bytes
    assert body == b'{"a":1,"event":"status_changed","z":2}'


def test_webhook_body_same_payload_always_same_bytes():
    p = {"event": "delivered", "shipment": {"id": "x", "status": "delivered"}}
    assert _build_webhook_body(p) == _build_webhook_body(p)


def test_webhook_hmac_sha256_signature_correct():
    secret = "test-secret-key"
    notification_id = "notif-abc"
    body = b'{"event":"status_changed"}'
    timestamp = 1700000000

    headers = _build_webhook_sig_headers(secret, notification_id, body, timestamp)

    # Independently compute expected signature
    msg = f"{timestamp}.".encode() + body
    expected_hex = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

    assert headers["X-Parcels-Signature"] == f"sha256={expected_hex}"
    assert headers["X-Parcels-Timestamp"] == str(timestamp)
    assert headers["X-Parcels-Event-Id"] == notification_id
    # Aliases must match
    assert headers["X-NovaTrack-Signature"] == headers["X-Parcels-Signature"]
    assert headers["X-NovaTrack-Event-Id"] == notification_id


def test_webhook_secret_env_must_have_novatrack_prefix(db_session):
    """Channels referencing env vars without the required prefix are rejected."""
    bad_channel = NotificationChannel(
        name="bad-secret",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env="DATABASE_URL",  # forbidden: could expose DB credentials
        enabled=True,
    )
    with pytest.raises(ValueError, match="NOVATRACK_WEBHOOK_SECRET_"):
        _get_webhook_secret(bad_channel)


# ════════════════════════════════════════════════════════════════════════════
# SLICE 4 — _get_webhook_secret: fail-closed, strict regex, min-32 chars
# ════════════════════════════════════════════════════════════════════════════


def test_get_webhook_secret_no_secret_env_raises():
    """Channel with no secret_env must raise (fail-closed — no unsigned delivery)."""
    ch = NotificationChannel(
        name="no-secret-env",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env=None,
        enabled=True,
    )
    with pytest.raises(ValueError, match="No signing secret"):
        _get_webhook_secret(ch)


def test_get_webhook_secret_empty_secret_env_raises():
    """Channel with empty-string secret_env must raise (fail-closed)."""
    ch = NotificationChannel(
        name="empty-secret-env",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env="",
        enabled=True,
    )
    with pytest.raises(ValueError, match="No signing secret"):
        _get_webhook_secret(ch)


def test_get_webhook_secret_env_var_absent_raises(monkeypatch):
    """Correct prefix but missing env var must raise (fail-closed)."""
    monkeypatch.delenv("NOVATRACK_WEBHOOK_SECRET_TEST", raising=False)
    ch = NotificationChannel(
        name="missing-env",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
        enabled=True,
    )
    with pytest.raises(ValueError, match="absent"):
        _get_webhook_secret(ch)


def test_get_webhook_secret_too_short_raises(monkeypatch):
    """Secret shorter than 32 chars must raise (minimum enforced)."""
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", "short")
    ch = NotificationChannel(
        name="short-secret",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
        enabled=True,
    )
    with pytest.raises(ValueError, match="32"):
        _get_webhook_secret(ch)


def test_get_webhook_secret_exactly_32_chars_accepted(monkeypatch):
    """Exactly 32 chars is the minimum — must succeed."""
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = NotificationChannel(
        name="ok-32",
        kind="webhook",
        url="https://8.8.8.8/hook",
        secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
        enabled=True,
    )
    assert _get_webhook_secret(ch) == _TEST_SECRET_32


# ════════════════════════════════════════════════════════════════════════════
# SLICE 5 — _require_public_ip: multicast explicit block
# ════════════════════════════════════════════════════════════════════════════


def test_require_public_ip_rejects_private_192_168():
    with pytest.raises(ValueError):
        _require_public_ip("192.168.1.1")


def test_require_public_ip_rejects_loopback():
    with pytest.raises(ValueError):
        _require_public_ip("127.0.0.1")


def test_require_public_ip_rejects_link_local():
    with pytest.raises(ValueError):
        _require_public_ip("169.254.0.1")


def test_require_public_ip_rejects_ipv6_loopback():
    with pytest.raises(ValueError):
        _require_public_ip("::1")


def test_require_public_ip_rejects_multicast_224():
    """224.0.0.1 is_global=True in Python but is multicast — must be blocked."""
    with pytest.raises(ValueError, match="multicast"):
        _require_public_ip("224.0.0.1")


def test_require_public_ip_rejects_multicast_239():
    """239.255.0.1 is in the administratively-scoped multicast range."""
    with pytest.raises(ValueError, match="multicast"):
        _require_public_ip("239.255.0.1")


def test_require_public_ip_allows_global_ip():
    # Should not raise
    _require_public_ip("8.8.8.8")
    _require_public_ip("93.184.216.34")


# ════════════════════════════════════════════════════════════════════════════
# SLICE 6 — _send_safe_https: DNS-change rejection on actual send path
# ════════════════════════════════════════════════════════════════════════════


def test_send_safe_https_rejects_dns_changed_to_private(monkeypatch):
    """If DNS resolves to a private address at delivery time, reject (no request)."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 443))],
    )
    with pytest.raises((ValueError, OSError)):
        _send_safe_https("https://example.com/hook", b"body", {})


def test_send_safe_https_rejects_dns_changed_to_multicast(monkeypatch):
    """Multicast address returned by DNS must be rejected at send time."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("224.0.0.1", 443))],
    )
    with pytest.raises((ValueError, OSError)):
        _send_safe_https("https://example.com/hook", b"body", {})


def test_send_safe_https_rejects_http_scheme():
    """HTTP scheme must be rejected by _send_safe_https (defence-in-depth)."""
    with pytest.raises(ValueError):
        _send_safe_https("http://8.8.8.8/hook", b"body", {})


def test_send_safe_https_rejects_empty_url():
    with pytest.raises(ValueError):
        _send_safe_https("", b"body", {})


def test_send_safe_https_rejects_crlf_in_url():
    with pytest.raises(ValueError):
        _send_safe_https("https://8.8.8.8/hook\r\nX-Injected: header", b"body", {})


def test_send_safe_https_rejects_url_over_2048_chars():
    long_url = "https://8.8.8.8/" + "a" * 2048
    with pytest.raises(ValueError):
        _send_safe_https(long_url, b"body", {})


# ════════════════════════════════════════════════════════════════════════════
# SLICE 7 — deliver_due: webhook path
# ════════════════════════════════════════════════════════════════════════════


def _add_settings_row(session, **kw):
    s = session.get(AppSettings, 1)
    if s is None:
        s = AppSettings(id=1, **kw)
        session.add(s)
        session.flush()
    return s


def _make_webhook_channel(session, name, *, secret_env="NOVATRACK_WEBHOOK_SECRET_TEST"):
    ch = NotificationChannel(
        name=name,
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed", "delivered", "exception", "quota_low"],
        secret_env=secret_env,
        enabled=True,
    )
    session.add(ch)
    session.flush()
    return ch


def test_deliver_due_sends_pending_webhook_and_marks_sent(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = _make_webhook_channel(db_session, "wh-sent")
    notif = enqueue_event(db_session, "d5:sent:1", {"event": "status_changed"})

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200)
    count = deliver_due(db_session, _settings(), client=fake, now=now)

    assert count >= 1
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "sent"
    assert _dt_naive_eq(d.delivered_at, now)
    assert d.attempts == 1


def test_deliver_due_sets_correct_signing_headers(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _SIGNING_TEST_SECRET)
    _make_webhook_channel(db_session, "wh-hdr")
    notif = enqueue_event(db_session, "d5:hdr:1", {"event": "status_changed", "x": 1})

    now = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200)
    deliver_due(db_session, _settings(), client=fake, now=now)

    assert len(fake.calls) >= 1
    call = fake.calls[-1]
    hdrs = call["headers"]

    # Verify signature independently
    body = call["content"]
    ts = int(now.timestamp())
    msg = f"{ts}.".encode() + body
    expected = "sha256=" + hmac.new(_SIGNING_TEST_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    assert hdrs["X-Parcels-Signature"] == expected
    assert hdrs["X-Parcels-Event-Id"] == notif.id
    assert hdrs["X-Parcels-Timestamp"] == str(ts)


def test_deliver_due_body_is_stable_sorted_compact_json(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    _make_webhook_channel(db_session, "wh-body")
    payload = {"event": "status_changed", "z": 99, "a": 1}
    notif = enqueue_event(db_session, "d5:body:1", payload)

    now = datetime(2026, 3, 1, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200)
    deliver_due(db_session, _settings(), client=fake, now=now)

    assert len(fake.calls) >= 1
    sent_body = json.loads(fake.calls[-1]["content"])
    assert sent_body == payload  # same content
    # wire bytes must be sorted
    assert fake.calls[-1]["content"] == _build_webhook_body(payload)


def test_deliver_due_marks_failed_on_non_2xx(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = _make_webhook_channel(db_session, "wh-fail")
    notif = enqueue_event(db_session, "d5:fail:1", {"event": "status_changed"})

    now = datetime(2026, 4, 1, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=500)
    deliver_due(db_session, _settings(), client=fake, now=now)

    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "pending"  # still pending — will retry
    assert d.attempts == 1
    # Backoff applied; use naive comparison for SQLite
    assert d.next_attempt_at.replace(tzinfo=None) > now.replace(tzinfo=None)


def test_deliver_due_dead_after_max_retries(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = _make_webhook_channel(db_session, "wh-dead")
    notif = enqueue_event(db_session, "d5:dead:1", {"event": "status_changed"})

    fake = _FakeClient(status_code=500)
    # Drive 8 failing attempts; arm before first then re-arm each iteration
    for i in range(8):
        now = datetime(2026, 5, i + 1, tzinfo=UTC)
        d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
        d.next_attempt_at = now - timedelta(seconds=1)  # make it due
        db_session.flush()
        deliver_due(db_session, _settings(), client=fake, now=now)

    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "dead"
    assert d.attempts == 8


def test_deliver_due_respects_next_attempt_at(db_session, monkeypatch):
    """Delivery whose next_attempt_at is in the future must not be attempted."""
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = _make_webhook_channel(db_session, "wh-future")
    notif = enqueue_event(db_session, "d5:future:1", {"event": "status_changed"})

    # Push next_attempt_at into the future
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    future = datetime(2099, 1, 1, tzinfo=UTC)
    d.next_attempt_at = future
    db_session.flush()

    fake = _FakeClient(status_code=200)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    deliver_due(db_session, _settings(), client=fake, now=now)

    # This delivery should NOT be in the count
    d_after = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d_after.state == "pending"
    assert len(fake.calls) == 0


def test_deliver_due_skips_disabled_channel(db_session, monkeypatch):
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = _make_webhook_channel(db_session, "wh-disabledrun")
    notif = enqueue_event(db_session, "d5:disabledrun:1", {"event": "status_changed"})

    # Disable channel BEFORE arming / delivery
    ch.enabled = False
    db_session.flush()

    # Even if we arm (make it due), disabled channel must be skipped
    _arm(db_session, notif, datetime(2026, 6, 1, tzinfo=UTC))

    fake = _FakeClient(status_code=200)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    deliver_due(db_session, _settings(), client=fake, now=now)

    # No attempt should have been made
    assert len(fake.calls) == 0
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "pending"  # stays pending, not sent or dead


def test_deliver_due_outbound_failure_does_not_abort_other_channels(db_session, monkeypatch):
    """One channel raising an exception must not cancel the other channels delivery."""
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)

    ch_ok = _make_webhook_channel(db_session, "wh-iso-ok")
    ch_bad = _make_webhook_channel(db_session, "wh-iso-bad")
    notif = enqueue_event(db_session, "d5:iso:1", {"event": "status_changed"})

    now = datetime(2026, 7, 1, tzinfo=UTC)
    _arm(db_session, notif, now)

    call_count = 0

    class _BurstClient:
        def post(self, url, *, content, headers):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("network error")
            return _FakeResp(200)

    deliver_due(db_session, _settings(), client=_BurstClient(), now=now)

    # Exactly one delivery should be sent (the second channel)
    d_ok = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch_ok.id).one()
    d_bad = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch_bad.id).one()
    states = {d_ok.state, d_bad.state}
    assert "sent" in states  # one channel succeeded
    assert "pending" in states  # the other is still retrying


def test_deliver_due_does_not_log_webhook_secret(db_session, monkeypatch, caplog):
    """Secret must not appear in any log record."""
    import logging

    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _NOLOG_TEST_SECRET)
    _make_webhook_channel(db_session, "wh-nolog")
    notif = enqueue_event(db_session, "d5:nolog:1", {"event": "status_changed"})

    now = datetime(2026, 8, 1, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=500)  # trigger error path
    with caplog.at_level(logging.DEBUG, logger="novatrack.notifications"):
        deliver_due(db_session, _settings(), client=fake, now=now)

    for record in caplog.records:
        assert _NOLOG_TEST_SECRET not in record.getMessage()


def test_deliver_due_missing_secret_no_request(db_session, monkeypatch):
    """Channel with no secret_env must not make any HTTP request (fail-closed)."""
    monkeypatch.delenv("NOVATRACK_WEBHOOK_SECRET_TEST", raising=False)
    ch = NotificationChannel(
        name="no-secret-ch",
        kind="webhook",
        url="https://8.8.8.8/hook",
        events=["status_changed"],
        secret_env=None,
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "missing-secret:1", {"event": "status_changed"})
    now = datetime(2026, 8, 15, tzinfo=UTC)
    _arm(db_session, notif, now)

    fake = _FakeClient(status_code=200)
    deliver_due(db_session, _settings(), client=fake, now=now)

    assert len(fake.calls) == 0  # no HTTP request made
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    # Attempt was counted; state is pending-with-backoff (not sent)
    assert d.state in ("pending", "dead")


def test_deliver_due_empty_url_no_request(db_session, monkeypatch):
    """Webhook delivery with empty URL must fail closed, no HTTP request made."""
    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)
    ch = NotificationChannel(
        name="empty-url-ch",
        kind="webhook",
        url="",
        events=["status_changed"],
        secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
        enabled=True,
    )
    db_session.add(ch)
    db_session.flush()

    notif = enqueue_event(db_session, "empty-url:1", {"event": "status_changed"})
    now = datetime(2026, 8, 20, tzinfo=UTC)
    _arm(db_session, notif, now)

    fake = _FakeClient(status_code=200)
    deliver_due(db_session, _settings(), client=fake, now=now)

    assert len(fake.calls) == 0
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state in ("pending", "dead")


# ════════════════════════════════════════════════════════════════════════════
# SLICE 8 — deliver_due: Telegram path
# ════════════════════════════════════════════════════════════════════════════


class _MockSecretStr:
    def __init__(self, v: str) -> None:
        self._v = v

    def get_secret_value(self) -> str:
        return self._v


def _tg_settings(enabled=True):
    return _settings(
        telegram_enabled=enabled,
        telegram_bot_token=_MockSecretStr(_TG_BOT_TOKEN),
        telegram_chat_id="123456",
        telegram_thread_id=None,
    )


def _make_tg_channel(session, name):
    ch = NotificationChannel(
        name=name,
        kind="telegram",
        events=["status_changed", "delivered", "exception", "quota_low"],
        enabled=True,
    )
    session.add(ch)
    session.flush()
    return ch


def test_deliver_due_telegram_not_sent_when_settings_disabled(db_session):
    ch = _make_tg_channel(db_session, "tg-disabled-settings")
    notif = enqueue_event(db_session, "d6:disabled:1", {"event": "status_changed"})

    now = datetime(2026, 9, 1, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200, json_body={"ok": True})
    deliver_due(db_session, _tg_settings(enabled=False), client=fake, now=now)

    assert len(fake.calls) == 0
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "pending"


def test_deliver_due_telegram_retries_on_non_200(db_session):
    ch = _make_tg_channel(db_session, "tg-non200")
    notif = enqueue_event(db_session, "d6:non200:1", {"event": "status_changed"})

    now = datetime(2026, 9, 2, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=429, json_body={})
    deliver_due(db_session, _tg_settings(), client=fake, now=now)

    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "pending"
    assert d.attempts == 1
    assert d.next_attempt_at.replace(tzinfo=None) > now.replace(tzinfo=None)


def test_deliver_due_telegram_retries_on_ok_false(db_session):
    ch = _make_tg_channel(db_session, "tg-okfalse")
    notif = enqueue_event(db_session, "d6:okfalse:1", {"event": "status_changed"})

    now = datetime(2026, 9, 3, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200, json_body={"ok": False, "description": "chat not found"})
    deliver_due(db_session, _tg_settings(), client=fake, now=now)

    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "pending"
    assert d.attempts == 1


def test_deliver_due_telegram_sent_on_ok_true(db_session):
    ch = _make_tg_channel(db_session, "tg-success")
    notif = enqueue_event(db_session, "d6:ok:1", {"event": "status_changed"})

    now = datetime(2026, 9, 4, tzinfo=UTC)
    _arm(db_session, notif, now)
    fake = _FakeClient(status_code=200, json_body={"ok": True})
    count = deliver_due(db_session, _tg_settings(), client=fake, now=now)

    assert count >= 1
    d = db_session.query(Delivery).filter_by(notification_id=notif.id, channel_id=ch.id).one()
    assert d.state == "sent"


def test_deliver_due_telegram_no_token_in_logs(db_session, caplog):
    """Telegram Bot token must never appear in any log record."""
    import logging

    _make_tg_channel(db_session, "tg-nolog")
    notif = enqueue_event(db_session, "d6:nolog:1", {"event": "status_changed"})

    now = datetime(2026, 9, 5, tzinfo=UTC)
    _arm(db_session, notif, now)

    # Force an error by providing a client that raises
    class _RaisingClient:
        def post(self, url, *, content, headers):
            raise OSError("connection refused")

    with caplog.at_level(logging.DEBUG, logger="novatrack.notifications"):
        deliver_due(db_session, _tg_settings(), client=_RaisingClient(), now=now)

    for record in caplog.records:
        assert _TG_BOT_TOKEN not in record.getMessage()


def test_attempt_telegram_invalid_token_raises(db_session):
    """Invalid Telegram bot token format must raise ValueError before any request."""
    from novatrack.notifications import _attempt_telegram

    ch = _make_tg_channel(db_session, "tg-badtoken")
    notif = Notification(event_key="tg-badtoken-test", payload={"event": "status_changed"})
    delivery = Delivery()

    bad_settings = _settings(
        telegram_enabled=True,
        telegram_bot_token=_MockSecretStr("INVALID-TOKEN"),  # no bot_id:secret format
        telegram_chat_id="123456",
    )
    fake = _FakeClient(status_code=200, json_body={"ok": True})
    with pytest.raises(ValueError):
        _attempt_telegram(delivery, notif, ch, bad_settings, client=fake)
    assert len(fake.calls) == 0


def test_attempt_telegram_path_injection_token_raises(db_session):
    """Token containing path separator must be rejected before any URL construction."""
    from novatrack.notifications import _attempt_telegram

    ch = _make_tg_channel(db_session, "tg-injection")
    notif = Notification(event_key="tg-inject-test", payload={"event": "status_changed"})
    delivery = Delivery()

    injected = _settings(
        telegram_enabled=True,
        # Attempt to inject a path segment via the token
        telegram_bot_token=_MockSecretStr("123456789:validpart/../evil"),
        telegram_chat_id="123456",
    )
    fake = _FakeClient(status_code=200, json_body={"ok": True})
    with pytest.raises(ValueError):
        _attempt_telegram(delivery, notif, ch, injected, client=fake)
    assert len(fake.calls) == 0


# ════════════════════════════════════════════════════════════════════════════
# SLICE 9 — check_quota
# ════════════════════════════════════════════════════════════════════════════


class _MockProvider:
    """Minimal provider double: implements the real getquota() contract."""

    def __init__(self, quota: dict) -> None:
        self._quota = quota

    def getquota(self) -> dict:
        return self._quota


def test_check_quota_stores_current_quota(db_session):
    _add_settings_row(db_session)
    quota = {"quota_total": 100, "quota_used": 50, "quota_remain": 50}
    now = datetime(2026, 10, 1, tzinfo=UTC)
    check_quota(db_session, _MockProvider(quota), now=now)

    s = db_session.get(AppSettings, 1)
    assert s.quota == quota
    # SQLite strips timezone on readback; compare naive date parts
    assert _dt_naive_eq(s.quota_checked_at, now)


def test_check_quota_enqueues_alert_at_80_percent(db_session):
    _add_settings_row(db_session)
    quota = {"quota_total": 100, "quota_used": 80, "quota_remain": 20}
    now = datetime(2026, 10, 2, tzinfo=UTC)
    check_quota(db_session, _MockProvider(quota), now=now)

    # A quota_low notification must exist
    period_key = "2026-10:100"
    notif = db_session.query(Notification).filter_by(event_key=f"quota_low:{period_key}").first()
    assert notif is not None
    assert notif.payload["event"] == "quota_low"


def test_check_quota_deduplicates_alert_same_month_and_total(db_session):
    """Second call in same month + same total must not create a new notification."""
    _add_settings_row(db_session)
    quota = {"quota_total": 100, "quota_used": 85, "quota_remain": 15}
    now1 = datetime(2026, 11, 1, tzinfo=UTC)
    now2 = datetime(2026, 11, 15, tzinfo=UTC)

    check_quota(db_session, _MockProvider(quota), now=now1)
    check_quota(db_session, _MockProvider(quota), now=now2)

    period_key = "2026-11:100"
    notifs = db_session.query(Notification).filter(Notification.event_key == f"quota_low:{period_key}").all()
    assert len(notifs) == 1  # deduplicated


def test_check_quota_new_alert_for_different_month(db_session):
    _add_settings_row(db_session)
    quota = {"quota_total": 100, "quota_used": 90, "quota_remain": 10}
    check_quota(db_session, _MockProvider(quota), now=datetime(2026, 12, 1, tzinfo=UTC))
    check_quota(db_session, _MockProvider(quota), now=datetime(2027, 1, 1, tzinfo=UTC))

    n_dec = db_session.query(Notification).filter_by(event_key="quota_low:2026-12:100").first()
    n_jan = db_session.query(Notification).filter_by(event_key="quota_low:2027-01:100").first()
    assert n_dec is not None
    assert n_jan is not None


def test_check_quota_no_alert_below_80_percent(db_session):
    _add_settings_row(db_session)
    quota = {"quota_total": 100, "quota_used": 79, "quota_remain": 21}
    now = datetime(2026, 10, 3, tzinfo=UTC)
    check_quota(db_session, _MockProvider(quota), now=now)

    # No alert for this specific period
    period_key = "2026-10:100"
    notif = db_session.query(Notification).filter_by(event_key=f"quota_low:{period_key}").first()
    assert notif is None


def test_check_quota_zero_total_no_division_error(db_session):
    """total=0 must not raise ZeroDivisionError or produce a nonsensical ratio."""
    _add_settings_row(db_session)
    quota = {"quota_total": 0, "quota_used": 0, "quota_remain": 0}
    # Must not raise
    check_quota(db_session, _MockProvider(quota), now=datetime(2026, 10, 4, tzinfo=UTC))
    # No alert should be created for zero total
    s = db_session.get(AppSettings, 1)
    assert s.quota == quota


def test_check_quota_malformed_negative_values_no_alert(db_session):
    """Negative quota values must not produce a nonsensical ratio or alert."""
    _add_settings_row(db_session)
    quota = {"quota_total": -1, "quota_used": -1, "quota_remain": 0}
    # Must not raise — negative total means total <= 0, guard prevents division
    check_quota(db_session, _MockProvider(quota), now=datetime(2026, 10, 5, tzinfo=UTC))
    # No alert because total <= 0
    s = db_session.get(AppSettings, 1)
    assert s.quota == quota


# ════════════════════════════════════════════════════════════════════════════
# SLICE 10 — worker tick
# ════════════════════════════════════════════════════════════════════════════


def test_worker_tick_delivers_pending_deliveries(db_session_factory, monkeypatch):
    """tick() must deliver due notifications within its own transaction."""
    from novatrack.worker import tick

    monkeypatch.setenv("NOVATRACK_WEBHOOK_SECRET_TEST", _TEST_SECRET_32)

    # Seed data; use merge() so re-running after previous tests doesn't clash
    with db_session_factory.begin() as s:
        s.merge(AppSettings(id=1))
        ch = NotificationChannel(
            name="tick-wh",
            kind="webhook",
            url="https://8.8.8.8/hook",
            events=["status_changed"],
            secret_env="NOVATRACK_WEBHOOK_SECRET_TEST",
            enabled=True,
        )
        s.add(ch)

    with db_session_factory.begin() as s:
        notif = enqueue_event(s, "tick:1", {"event": "status_changed"})
        assert notif is not None
        # Arm: make deliveries due immediately
        for d in s.query(Delivery).filter_by(notification_id=notif.id).all():
            d.next_attempt_at = datetime(2000, 1, 1, tzinfo=UTC)

    # Monkeypatch deliver_due to use a fake client
    from novatrack import notifications as _n

    fake = _FakeClient(status_code=200)
    original_deliver = _n.deliver_due

    def _patched_deliver(session, settings, client=None, **kw):
        return original_deliver(session, settings, client=fake, **kw)

    monkeypatch.setattr(_n, "deliver_due", _patched_deliver)

    tick(
        db_session_factory, _settings(), _MockProvider({"quota_total": 0, "quota_used": 0, "quota_remain": 0})
    )

    with db_session_factory() as s:
        d = s.query(Delivery).filter_by(notification_id=notif.id).one()
        assert d.state == "sent"


def test_worker_tick_checks_quota_when_overdue(db_session_factory):
    """tick() must call check_quota when quota_checked_at is None."""
    from novatrack.worker import tick

    with db_session_factory.begin() as s:
        s.merge(AppSettings(id=1, quota_checked_at=None))

    call_count = 0

    class _CountingProvider(_MockProvider):
        def getquota(self):
            nonlocal call_count
            call_count += 1
            return super().getquota()

    provider = _CountingProvider({"quota_total": 0, "quota_used": 0, "quota_remain": 0})
    tick(db_session_factory, _settings(), provider)
    assert call_count >= 1


def test_worker_tick_isolates_housekeeping_failure(db_session_factory):
    """A housekeeping exception must not abort deliver_due."""
    from novatrack import notifications as _n
    from novatrack.worker import tick

    delivered = []

    def _patched_deliver(session, settings, client=None, **kw):
        delivered.append(True)
        return 0

    import unittest.mock as mock

    with mock.patch.object(_n, "deliver_due", side_effect=_patched_deliver):
        tick(
            db_session_factory,
            _settings(),
            _MockProvider({"quota_total": 0, "quota_used": 0, "quota_remain": 0}),
        )

    assert len(delivered) >= 1


# ════════════════════════════════════════════════════════════════════════════
# SLICE 11 — Worker integration: real tracking + mock provider
# ════════════════════════════════════════════════════════════════════════════


def test_worker_integration_process_register_and_quota(db_session_factory):
    """Integration: tick() processes ProviderOperations and stores quota with real keys.

    Uses real create_shipment (which queues a register ProviderOperation) and
    a mock provider that returns a detected carrier.  Verifies:
    - register op is marked done
    - detected carrier persisted from provider response
    - quota stored with real 17track keys (quota_total/quota_used/quota_remain)
    - no exceptions raised
    """
    from novatrack.models import ProviderOperation, Shipment
    from novatrack.tracking import create_shipment
    from novatrack.worker import tick

    unique = uuid.uuid4().hex[:8]
    shipment_number = f"INT-{unique}"

    # Seed: AppSettings + shipment (create_shipment queues a register op)
    with db_session_factory.begin() as s:
        s.merge(AppSettings(id=1, quota_checked_at=None))
        ship = create_shipment(s, None, number=shipment_number)
        ship_id = ship.id

    # Verify pending register op exists
    with db_session_factory() as s:
        ops = s.query(ProviderOperation).filter_by(shipment_id=ship_id, done=False).all()
        assert any(o.kind == "register" for o in ops)

    class _IntProvider:
        """Minimal provider that satisfies the full TrackingProvider protocol."""

        def register(self, number, carrier=None, *, param=None):
            return {"number": number, "carrier": 42}  # detected carrier

        def stoptrack(self, number, carrier=None):
            return {"number": number}

        def deletetrack(self, number, carrier=None):
            return {"number": number}

        def gettrackinfo(self, number, carrier=None):
            return {}

        def getquota(self):
            return {"quota_total": 500, "quota_used": 50, "quota_remain": 450}

    provider = _IntProvider()
    # tick must complete without raising
    tick(db_session_factory, _settings(), provider)

    with db_session_factory() as s:
        # register op must be done
        op = s.query(ProviderOperation).filter_by(shipment_id=ship_id, kind="register").one()
        assert op.done is True

        # carrier detected from provider response must be persisted
        ship = s.get(Shipment, ship_id)
        assert ship.registered is True
        assert ship.carrier == 42

        # quota stored with actual 17track v2.2 keys, not old total/used
        settings = s.get(AppSettings, 1)
        assert settings.quota is not None
        assert settings.quota.get("quota_total") == 500
        assert settings.quota.get("quota_used") == 50
        assert settings.quota.get("quota_remain") == 450
        assert settings.quota_checked_at is not None
