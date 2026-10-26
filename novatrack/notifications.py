"""Notification channels, signed webhook delivery, quota monitoring.

Public API
----------
validate_webhook_url   — synchronous creation-time URL validation (exported for web lane)
enqueue_event          — transactional notification + delivery creation
deliver_due            — locked delivery dispatch (webhook / Telegram)
check_quota            — provider quota check + alert enqueue
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novatrack.models import (
    AppSettings,
    Delivery,
    DeliveryAttempt,
    Notification,
    NotificationChannel,
)

__all__ = ["validate_webhook_url", "enqueue_event", "deliver_due", "check_quota"]

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS: int = 8
_BACKOFF_BASE_SECS: int = 30
_BACKOFF_CAP_SECS: int = 3600
_RESPONSE_LIMIT: int = 8192
_WEBHOOK_SECRET_PREFIX: str = "NOVATRACK_WEBHOOK_SECRET_"
# Strict regex for env var names: prefix + at least one uppercase/digit/underscore char
_WEBHOOK_SECRET_ENV_RE = re.compile(r"^NOVATRACK_WEBHOOK_SECRET_[A-Z0-9_]+$")
_WEBHOOK_SECRET_MIN_LEN: int = 32
_MAX_URL_LEN: int = 2048
# Telegram bot token: {bot_id}:{secret}  — 5+ digits, colon, 35+ [A-Za-z0-9_-]
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{35,}$")


# ── Address & URL validation ─────────────────────────────────────────────────


def _require_public_ip(ip_str: str) -> None:
    """Raise ValueError if ip_str is not a globally-routable public address.

    Multicast is checked explicitly because Python's ipaddress module marks
    some multicast ranges (e.g. 224.0.0.1) as is_global=True.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise ValueError(f"Invalid IP address {ip_str!r}") from exc
    if addr.is_multicast:
        raise ValueError(
            f"Address {ip_str!r} is multicast — multicast ranges (224.0.0.0/4, ff00::/8) are blocked"
        )
    if not addr.is_global:
        raise ValueError(
            f"Address {ip_str!r} is not publicly routable — "
            "private, loopback, link-local, multicast and reserved ranges are blocked"
        )


def _check_url_safe(url: str) -> None:
    """Shared URL security checks applied at both creation and delivery time.

    Validates: non-empty, max 2048 chars, no CRLF/control chars,
    HTTPS scheme, no credentials, no query string, no fragment.
    Raises ValueError on any violation.
    """
    if not url:
        raise ValueError("URL must not be empty")
    if len(url) > _MAX_URL_LEN:
        raise ValueError(
            f"URL exceeds maximum allowed length of {_MAX_URL_LEN} characters ({len(url)} chars)"
        )
    for ch in url:
        if ord(ch) < 32:
            raise ValueError(
                f"URL contains illegal control character (U+{ord(ch):04X}); CRLF injection blocked"
            )
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"Only HTTPS webhook URLs are allowed (got scheme {parsed.scheme!r})")
    if not parsed.hostname:
        raise ValueError("URL must have a hostname")
    if parsed.username or parsed.password:
        raise ValueError("Credentials (user:password@) are not allowed in webhook URLs")
    if parsed.query:
        raise ValueError("Query strings are not allowed in webhook URLs — keep secrets out of the DB")
    if parsed.fragment:
        raise ValueError("Fragments (#…) are not allowed in webhook URLs")


def validate_webhook_url(url: str) -> str:
    """Synchronous creation-time validation.  Raises ValueError on rejection."""
    _check_url_safe(url)
    parsed = urlsplit(url)
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve webhook hostname {parsed.hostname!r}: {exc}") from exc
    if not infos:
        raise ValueError(f"Webhook hostname {parsed.hostname!r} resolved to no addresses")
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        _require_public_ip(sockaddr[0])
    return url


# ── Webhook signing ──────────────────────────────────────────────────────────


def _build_webhook_body(payload: dict) -> bytes:
    """Stable sorted compact JSON UTF-8 body."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _build_webhook_sig_headers(
    secret: str, notification_id: str, body: bytes, timestamp: int
) -> dict[str, str]:
    """Return HMAC-SHA256 signing headers (without Content-Type).

    The event-id header is stable across all retry attempts for the same
    notification so receivers can deduplicate idempotently.
    """
    msg = f"{timestamp}.".encode("utf-8") + body
    sig_hex = _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    sig_val = f"sha256={sig_hex}"
    return {
        "X-Parcels-Signature": sig_val,
        "X-Parcels-Timestamp": str(timestamp),
        "X-Parcels-Event-Id": notification_id,
        "X-NovaTrack-Signature": sig_val,
        "X-NovaTrack-Event-Id": notification_id,
    }


# ── Safe pinned HTTPS sender ─────────────────────────────────────────────────


class _PinnedHTTPSConn(http.client.HTTPSConnection):
    """HTTPS connection to a pinned IP while preserving SNI / Host."""

    def __init__(self, host: str, port: int, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        try:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()  # prevent FD leak on SSL handshake failure
            raise


def _send_safe_https(
    url: str, body: bytes, headers: dict[str, str], *, timeout: float = 10.0
) -> tuple[int, bytes]:
    """Deliver body to url via SSRF-protected, IP-pinned HTTPS connection.

    Validates scheme, credentials, query, fragment, CRLF, and max-length
    before DNS resolution, then pins the first resolved public IP.
    Returns (status_code, response_body_up_to_limit).
    Raises OSError / ValueError on network or validation errors.
    Never follows redirects.
    """
    # Security validation before DNS — catches DB-tampered or stale config
    _check_url_safe(url)

    parsed = urlsplit(url)
    hostname = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"

    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OSError(f"DNS resolution failed for {hostname!r}") from exc

    if not infos:
        raise OSError(f"No addresses resolved for {hostname!r}")

    for _fam, _typ, _proto, _canon, sockaddr in infos:
        _require_public_ip(sockaddr[0])

    pinned_ip = infos[0][4][0]
    # Preserve non-standard port in Host header (RFC 7230 §5.4) for correct TLS SNI
    host_header = hostname if port == 443 else f"{hostname}:{port}"

    conn = _PinnedHTTPSConn(hostname, port, pinned_ip, timeout=timeout)
    try:
        all_headers = {
            "Host": host_header,
            "Content-Length": str(len(body)),
            **headers,
        }
        conn.request("POST", path, body=body, headers=all_headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read(_RESPONSE_LIMIT)
    finally:
        conn.close()

    return status, resp_body


# ── Channel subscription logic ───────────────────────────────────────────────


def _channel_subscribes(subscribed_events: list, event_type: str) -> bool:
    """True if the channel's event list covers this event_type.

    Rules
    -----
    - Direct match always works.
    - 'delivered' and 'exception' also satisfy a 'status_changed' subscription
      (one Delivery per channel/notification; receiver deduplicates by event id).
    """
    if event_type in subscribed_events:
        return True
    if event_type in ("delivered", "exception") and "status_changed" in subscribed_events:
        return True
    return False


# ── Secret resolution ────────────────────────────────────────────────────────


def _get_webhook_secret(channel: NotificationChannel) -> str:
    """Fetch the webhook HMAC secret from the environment.

    Fail-closed: raises ValueError if:
    - secret_env is absent/empty (no unsigned delivery allowed)
    - the env var name does not match the strict NOVATRACK_WEBHOOK_SECRET_[A-Z0-9_]+ pattern
    - the resolved secret is absent, empty, or shorter than 32 characters

    Minimum-32-char requirement prevents use of weak HMAC keys.
    """
    env_name = channel.secret_env or ""
    if not env_name:
        raise ValueError(
            "No signing secret configured for this channel (secret_env is unset); "
            "webhook delivery refused (fail-closed — unsigned requests are not allowed)"
        )
    if not _WEBHOOK_SECRET_ENV_RE.match(env_name):
        raise ValueError(
            f"channel.secret_env {env_name!r} must match {_WEBHOOK_SECRET_ENV_RE.pattern!r}; "
            "refusing to resolve OIDC/database/tracking secrets"
        )
    secret = os.environ.get(env_name, "")
    if not secret:
        raise ValueError(
            f"Webhook signing secret {env_name!r} is absent or empty in environment; "
            "delivery refused (fail-closed)"
        )
    if len(secret) < _WEBHOOK_SECRET_MIN_LEN:
        raise ValueError(
            f"Webhook signing secret {env_name!r} is too short "
            f"(got {len(secret)} chars; minimum {_WEBHOOK_SECRET_MIN_LEN} required for HMAC-SHA256 security)"
        )
    return secret


# ── Per-channel delivery attempts ───────────────────────────────────────────


def _attempt_webhook(
    delivery: Delivery,
    notification: Notification,
    channel: NotificationChannel,
    now: datetime,
    *,
    client: Any = None,
) -> tuple[int | None, bool]:
    """Try to deliver via webhook.  Returns (status_code, success).  May raise."""
    url = channel.url or ""
    if not url:
        raise ValueError("Webhook channel has no URL configured")

    # Fail-closed: raises if secret absent, wrong prefix, or too short
    secret = _get_webhook_secret(channel)
    body = _build_webhook_body(notification.payload)
    ts = int(now.timestamp())
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        **_build_webhook_sig_headers(secret, notification.id, body, ts),
    }

    if client is not None:
        resp = client.post(url, content=body, headers=headers)
        status_code: int = resp.status_code
    else:
        # _send_safe_https re-validates URL + DNS at send time (DB-tamper / DNS-rebind defence)
        status_code, _body = _send_safe_https(url, body, headers)

    return status_code, 200 <= status_code < 300


def _validate_telegram_token(token_val: str) -> None:
    """Validate Telegram bot token format to prevent path/query injection.

    Format: {bot_id}:{secret}  where bot_id is 5+ digits and secret is
    35+ chars of [A-Za-z0-9_-].  Blocks '/', '?', '&', '#' and similar
    characters that could mutate the API URL.
    Token value is NEVER logged.
    """
    if not _TELEGRAM_TOKEN_RE.match(token_val):
        raise ValueError(
            "Telegram bot token has invalid format; "
            "expected '<bot_id>:<token>' with numeric bot_id and alphanumeric token part"
        )


def _attempt_telegram(
    delivery: Delivery,
    notification: Notification,
    channel: NotificationChannel,
    settings: Any,
    *,
    client: Any = None,
) -> tuple[int | None, bool]:
    """Try to deliver via Telegram Bot API.  Returns (status_code, success)."""
    if not settings.telegram_enabled:
        return None, False

    bot_token = settings.telegram_bot_token
    if not bot_token:
        return None, False
    token_val: str = (
        bot_token.get_secret_value() if hasattr(bot_token, "get_secret_value") else str(bot_token)
    )

    # Validate format before embedding in URL — raises ValueError (never logs token)
    _validate_telegram_token(token_val)

    chat_id = settings.telegram_chat_id
    if not chat_id:
        return None, False

    text = json.dumps(notification.payload, sort_keys=True, indent=2, ensure_ascii=False)
    data: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if settings.telegram_thread_id:
        data["message_thread_id"] = settings.telegram_thread_id
    # Intentionally no parse_mode — prevents injection via payload content

    req_body = json.dumps(data).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    # Token appears only in URL; NEVER log this URL
    tg_url = f"https://api.telegram.org/bot{token_val}/sendMessage"

    if client is not None:
        resp = client.post(tg_url, content=req_body, headers=req_headers)
        status_code = resp.status_code
        try:
            ok = status_code == 200 and resp.json().get("ok", False)
        except Exception:
            ok = False
    else:
        status_code, resp_bytes = _send_safe_https(tg_url, req_body, req_headers)
        try:
            resp_json = json.loads(resp_bytes)
            ok = status_code == 200 and resp_json.get("ok", False)
        except Exception:
            ok = False

    return status_code, ok


# ── Backoff ──────────────────────────────────────────────────────────────────


def _backoff_delay(attempts: int) -> int:
    """Seconds to wait after `attempts` total attempts (exponential, bounded)."""
    return min(_BACKOFF_BASE_SECS * (2 ** (attempts - 1)), _BACKOFF_CAP_SECS)


# ════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════


def enqueue_event(session: Session, event_key: str, payload: dict) -> Notification | None:
    """Idempotent: returns None if event_key already exists.  Caller commits.

    Creates one Delivery row for every enabled channel that subscribes
    to the event type (snapshot at enqueue time).  Nested transactions
    (savepoints) guard against concurrent duplicate inserts.
    """
    try:
        with session.begin_nested():
            notif = Notification(event_key=event_key, payload=payload)
            session.add(notif)
            session.flush()
    except IntegrityError:
        return None

    event_type = payload.get("event", "")
    channels = (
        session.query(NotificationChannel)
        .filter(NotificationChannel.enabled == True)  # noqa: E712
        .all()
    )
    for ch in channels:
        if not _channel_subscribes(ch.events or [], event_type):
            continue
        try:
            with session.begin_nested():
                session.add(Delivery(notification_id=notif.id, channel_id=ch.id))
                session.flush()
        except IntegrityError:
            pass  # unique constraint — shouldn't happen for a brand-new notification

    return notif


def deliver_due(
    session: Session,
    settings: Any,
    client: Any = None,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    """Attempt delivery for all pending Delivery rows that are due.

    Uses SELECT … FOR UPDATE SKIP LOCKED on PostgreSQL (silently ignored on
    SQLite) so concurrent workers don't double-deliver.  ``of=Delivery``
    limits the lock to Delivery rows so joined NotificationChannel rows are
    not locked, preventing unrelated deliveries from being blocked.

    Disabled Telegram rows are filtered BEFORE the LIMIT to prevent them
    from starving webhook or other channel deliveries.

    Deterministic ordering by (next_attempt_at, id) ensures fairness.

    At-least-once guarantee: receivers should deduplicate on X-Parcels-Event-Id.
    Returns number of delivery attempts made (excludes skipped rows).
    Caller commits to release row locks.
    """
    if now is None:
        now = datetime.now(UTC)

    q = (
        session.query(Delivery)
        .join(NotificationChannel, Delivery.channel_id == NotificationChannel.id)
        .filter(
            Delivery.state == "pending",
            Delivery.next_attempt_at <= now,
            NotificationChannel.enabled == True,  # noqa: E712
        )
    )

    # Filter globally-disabled Telegram BEFORE LIMIT to avoid crowding out other channels
    if not settings.telegram_enabled:
        q = q.filter(NotificationChannel.kind != "telegram")

    # Deterministic fair order: oldest due first, then by id for stable tie-break
    q = q.order_by(Delivery.next_attempt_at, Delivery.id).limit(limit)

    # FOR UPDATE SKIP LOCKED only on PostgreSQL; of=Delivery avoids locking channel rows
    try:
        dialect = session.get_bind().dialect.name
    except Exception:
        dialect = "unknown"
    if dialect == "postgresql":
        q = q.with_for_update(of=Delivery, skip_locked=True)

    rows = q.all()
    attempted = 0

    for delivery in rows:
        channel = session.get(NotificationChannel, delivery.channel_id)
        notification = session.get(Notification, delivery.notification_id)
        if channel is None or notification is None:
            continue

        # Telegram: skip (don't count as attempt) when globally disabled — leave pending
        if channel.kind == "telegram" and not settings.telegram_enabled:
            continue

        status_code: int | None = None
        success = False
        error_msg: str | None = None

        try:
            if channel.kind == "webhook":
                status_code, success = _attempt_webhook(delivery, notification, channel, now, client=client)
            elif channel.kind == "telegram":
                status_code, success = _attempt_telegram(
                    delivery, notification, channel, settings, client=client
                )
            else:
                error_msg = f"unknown channel kind: {channel.kind!r}"
        except Exception as exc:
            # Log only the exception class — never log URL, token, or secret
            error_msg = type(exc).__name__
            logger.error(
                "Delivery %s channel=%s kind=%s raised %s",
                delivery.id,
                delivery.channel_id,
                channel.kind,
                type(exc).__name__,
            )

        session.add(
            DeliveryAttempt(
                delivery_id=delivery.id,
                attempted_at=now,
                status_code=status_code,
                success=success,
                error=error_msg if not success else None,
            )
        )

        delivery.attempts += 1
        delivery.last_status = status_code
        delivery.last_error = error_msg

        if success:
            delivery.state = "sent"
            delivery.delivered_at = now
        elif delivery.attempts >= _MAX_ATTEMPTS:
            delivery.state = "dead"
            logger.warning(
                "Delivery %s dead after %d attempts (channel %s)",
                delivery.id,
                delivery.attempts,
                delivery.channel_id,
            )
        else:
            delivery.next_attempt_at = now + timedelta(seconds=_backoff_delay(delivery.attempts))

        session.flush()
        attempted += 1

    return attempted


def check_quota(session: Session, provider: Any, now: datetime | None = None) -> None:
    """Fetch provider quota, persist it, and enqueue a quota_low alert at >=80%.

    Provider contract: ``provider.getquota()`` returns a dict with keys
    ``quota_total``, ``quota_used``, and ``quota_remain`` (17track v2.2 API).

    Alert is throttled to once per calendar month per configured quota total
    (the unique event_key provides the dedup guarantee).
    Caller commits.
    """
    if now is None:
        now = datetime.now(UTC)

    settings = session.get(AppSettings, 1)
    if settings is None:
        return

    quota: dict = provider.getquota()
    settings.quota = quota
    settings.quota_checked_at = now
    session.flush()

    total = quota.get("quota_total", 0)
    used = quota.get("quota_used", 0)
    if total > 0 and used / total >= 0.80:
        period_key = f"{now.year}-{now.month:02d}:{total}"
        if settings.quota_alert_period != period_key:
            event_key = f"quota_low:{period_key}"
            notif = enqueue_event(
                session,
                event_key,
                {"event": "quota_low", "quota": quota, "period": period_key},
            )
            if notif is not None:
                settings.quota_alert_period = period_key
                session.flush()
