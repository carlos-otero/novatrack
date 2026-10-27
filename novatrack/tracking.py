"""Durable tracking lifecycle — all functions flush only, caller commits.

Ownership note: this module is the tracking lane.  Providers are in
novatrack/providers/.  Notification delivery is in novatrack/notifications.py
(notifications lane).  Web/auth routes own app.py.  No other modules are
written or imported beyond the project's own packages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novatrack.carriers import build_param_string, unsupported_required_params
from novatrack.models import (
    AppSettings,
    ProviderOperation,
    Shipment,
    TrackingEvent,
    Transition,
    WebhookReceipt,
    utcnow,
)
from novatrack.notifications import enqueue_event
from novatrack.providers import ProviderError
from novatrack.providers.track17 import normalize_status

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────

_TBA_PREFIX = "TBA"

# Tracking number format: 5–50 alphanumeric or hyphen characters (from 17track docs)
_TRACKING_NUMBER_RE = re.compile(r"^[A-Za-z0-9\-]{5,50}$")

# Kind sort order: register must run before stop before delete
_OP_PRIORITY = case(
    (ProviderOperation.kind == "register", 0),
    (ProviderOperation.kind == "stop", 1),
    (ProviderOperation.kind == "delete", 2),
    else_=3,
)

# Sentinel value used as next_attempt_at when a register op is paused indefinitely
# because the provider rejected it for missing/invalid additional parameters.
# The worker will never pick up the op again until update_shipment_params() resets
# next_attempt_at to now.  Using a far-future datetime avoids needing an extra DB
# column while still making the "paused until user acts" semantics explicit.
_PAUSED_INDEFINITELY = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)
_BACKOFF_BASE_SECS = 30
_BACKOFF_CAP_SECS = 3600
_BACKOFF_MAX_EXPONENT = 20  # 2^20 >> cap, but avoids huge integer arithmetic


def _ensure_utc(dt: datetime) -> datetime:
    """Return a UTC-aware datetime regardless of input naivety."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_time_str(time_str: str | None) -> datetime | None:
    """Parse ISO 8601 string to UTC-aware datetime, returning None on failure."""
    if not time_str:
        return None
    try:
        dt = datetime.fromisoformat(time_str)
        return _ensure_utc(dt)
    except (ValueError, TypeError):
        return None


def _event_time(ev: dict) -> datetime | None:
    """Get event time from a dict: prefer time_utc (pure UTC) then time_iso."""
    return _parse_time_str(ev.get("time_utc")) or _parse_time_str(ev.get("time_iso"))


def _event_fingerprint(event: dict) -> str:
    """Stable SHA-256 dedup key for a tracking event dict.

    Uses stable JSON serialisation instead of '|' separator to avoid
    collisions when description/location contains the separator character.
    """
    raw = json.dumps(
        {
            "t": event.get("time_iso") or "",
            "d": event.get("description") or "",
            "l": event.get("location") or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _notification_event_type(status: str) -> str:
    """Map canonical status to notification event type."""
    if status == "delivered":
        return "delivered"
    if status == "exception":
        return "exception"
    return "status_changed"


def _persist_events(session: Session, shipment: Shipment, events: list[tuple[dict, str]]) -> None:
    """Insert TrackingEvent rows, deduplicating by fingerprint per shipment."""
    for ev, prov_name in events:
        fp = _event_fingerprint(ev)
        existing = session.execute(
            select(TrackingEvent).where(
                TrackingEvent.shipment_id == shipment.id,
                TrackingEvent.fingerprint == fp,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            TrackingEvent(
                shipment_id=shipment.id,
                fingerprint=fp,
                occurred_at=_event_time(ev),
                description=(ev.get("description") or "")[:1000],
                location=(ev.get("location") or "")[:500],
                provider=(prov_name or "")[:200],
                raw=ev,
            )
        )
    session.flush()


def _do_transition(session: Session, shipment: Shipment, new_status: str, status_at: datetime) -> None:
    """Perform a status transition: update shipment, write Transition, emit Notification."""
    old_status = shipment.status
    shipment.status = new_status
    shipment.status_at = status_at
    if new_status == "delivered":
        shipment.delivered_at = status_at
    shipment.version += 1
    shipment.updated_at = utcnow()

    session.add(
        Transition(
            shipment_id=shipment.id,
            from_status=old_status,
            to_status=new_status,
            occurred_at=status_at,
            version=shipment.version,
        )
    )
    session.flush()

    event_type = _notification_event_type(new_status)
    event_key = f"{event_type}:{shipment.id}:{shipment.version}"
    enqueue_event(
        session,
        event_key,
        {
            "event": event_type,
            "shipment": {
                "id": shipment.id,
                "number": shipment.number,
                "name": shipment.name,
                "status": new_status,
            },
            "from_status": old_status,
            "to_status": new_status,
        },
    )


def _backoff_delay(attempts: int) -> int:
    """Seconds to wait after `attempts` total failed attempts (exponential, capped).

    Exponent is capped to avoid computing huge integers for high attempt counts.
    """
    exponent = min(max(attempts - 1, 0), _BACKOFF_MAX_EXPONENT)
    return min(_BACKOFF_BASE_SECS * (2**exponent), _BACKOFF_CAP_SECS)


def _normalize_webhook_data(data) -> list:
    """Coerce webhook payload data to a list of tracking item dicts.

    v2.2 webhook sends data as a single dict; older/bulk variants may send lists.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "accepted" in data:
            return data["accepted"]
        return [data]
    raise ValueError(f"webhook data has unexpected type {type(data).__name__!r}; expected list or dict")


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


def create_shipment(
    session: Session,
    provider,
    *,
    number: str,
    carrier: int | None = None,
    name: str = "",
    store: str = "",
    notes: str = "",
    destination_country: str | None = None,
    destination_postal_code: str | None = None,
    phone_number_last_4: str | None = None,
) -> Shipment:
    """Create a Shipment and atomically queue a register ProviderOperation.

    TBA-prefixed numbers (Amazon internal) are stored as *untrackable* and
    never registered with the external provider.

    Raises ValueError for blank/invalid number, negative carrier, or duplicate.
    Caller must commit.
    """
    if not number:
        raise ValueError("Tracking number must not be blank")

    if not _TRACKING_NUMBER_RE.match(number):
        raise ValueError("Tracking number must be 5–50 alphanumeric characters or hyphens")

    if carrier is not None and (not isinstance(carrier, int) or carrier <= 0):
        raise ValueError(f"Carrier must be a positive integer, got {carrier!r}")

    # Duplicate check: explicit read gives a clear ValueError before DB constraint fires
    existing = session.execute(select(Shipment).where(Shipment.number == number)).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"A shipment with number {number!r} already exists")

    is_tba = number.upper().startswith(_TBA_PREFIX)
    shipment = Shipment(
        number=number,
        carrier=carrier,
        name=name,
        store=store,
        notes=notes,
        status="untrackable" if is_tba else "pending",
        destination_country=destination_country or None,
        destination_postal_code=destination_postal_code or None,
        phone_number_last_4=phone_number_last_4 or None,
    )
    session.add(shipment)

    # Savepoint: catches concurrent duplicate inserts and turns IntegrityError → ValueError
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        raise ValueError(f"A shipment with number {number!r} already exists")

    if not is_tba:
        session.add(ProviderOperation(shipment_id=shipment.id, kind="register"))
        session.flush()

    return shipment


def archive_shipment(session: Session, shipment: Shipment, provider) -> None:
    """Move shipment to the *archive* bucket and queue a stop operation when needed.

    Stop is queued if the shipment is already registered OR if a pending
    register operation exists (so the external resource gets stopped once
    the register eventually completes).

    Idempotent: a second call on an already-archived shipment is a no-op.
    Caller must commit.
    """
    if shipment.bucket == "archive":
        return
    shipment.bucket = "archive"
    shipment.archived_at = utcnow()
    shipment.updated_at = utcnow()
    session.flush()

    needs_stop = shipment.registered
    if not needs_stop:
        # Also queue stop if a register op is still pending (not done yet)
        pending_register = session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == shipment.id,
                ProviderOperation.kind == "register",
                ProviderOperation.done == False,  # noqa: E712
            )
        ).scalar_one_or_none()
        needs_stop = pending_register is not None

    if needs_stop:
        session.add(ProviderOperation(shipment_id=shipment.id, kind="stop"))
        session.flush()


def trash_shipment(session: Session, shipment: Shipment, provider) -> None:
    """Move shipment to the *trash* bucket and queue a delete operation.

    TBA shipments (Amazon internal) were never registered remotely and must
    never trigger a remote delete — paid external call forbidden.

    Idempotent: a second call is a no-op.
    Caller must commit.
    """
    if shipment.bucket == "trash":
        return
    shipment.bucket = "trash"
    shipment.trashed_at = utcnow()
    shipment.updated_at = utcnow()
    session.flush()

    is_tba = shipment.number.upper().startswith(_TBA_PREFIX)
    if not is_tba:
        session.add(ProviderOperation(shipment_id=shipment.id, kind="delete"))
        session.flush()


def manual_deliver(session: Session, shipment: Shipment) -> None:
    """Manually mark a shipment as delivered.

    Idempotent: a second call on an already-delivered shipment is a no-op.
    Creates a Transition record and emits a *delivered* Notification.
    Caller must commit.
    """
    if shipment.status == "delivered":
        return
    _do_transition(session, shipment, "delivered", utcnow())


def change_carrier_shipment(
    session: Session,
    shipment: Shipment,
    provider,
    new_carrier: int,
) -> None:
    """Change the carrier for a registered shipment via 17TRACK changecarrier.

    Calls provider.changecarrier and updates shipment.carrier in DB on success.
    Raises ValueError if the shipment is not yet registered (guard against
    accidental calls on unregistered shipments).
    Raises ProviderError on all provider rejections (including -18019801..-18019808).
    The UI layer should warn the user that this costs one shipment quota.
    Caller must commit.
    """
    if not shipment.registered:
        raise ValueError("Shipment must be registered with the provider before changing carrier")
    old_carrier = shipment.carrier
    result = provider.changecarrier(
        shipment.number,
        carrier_new=new_carrier,
        carrier_old=old_carrier,
    )
    # Use carrier returned by provider (may differ from requested)
    confirmed_carrier = result.get("carrier") or new_carrier
    shipment.carrier = confirmed_carrier
    shipment.updated_at = utcnow()
    session.flush()


def update_shipment_params(
    session: Session,
    shipment: Shipment,
    *,
    destination_country: str | None = None,
    destination_postal_code: str | None = None,
    phone_number_last_4: str | None = None,
) -> bool:
    """Update the additional tracking parameters for a shipment.

    Updates the three stored param fields and, if there is a pending register
    operation that was paused for missing params, resets its ``next_attempt_at``
    to now so the worker picks it up on the next tick.

    Returns True if a pending register op was reset (i.e. an immediate retry
    was triggered), False otherwise.

    Caller must commit.
    """
    shipment.destination_country = destination_country
    shipment.destination_postal_code = destination_postal_code
    shipment.phone_number_last_4 = phone_number_last_4
    shipment.updated_at = utcnow()
    session.flush()

    # Reset any pending (paused) register op so the worker retries immediately.
    pending_reg = session.execute(
        select(ProviderOperation).where(
            ProviderOperation.shipment_id == shipment.id,
            ProviderOperation.kind == "register",
            ProviderOperation.done == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if pending_reg is not None:
        pending_reg.next_attempt_at = utcnow()
        shipment.provider_error = None  # clear stale error shown in UI
        session.flush()
        return True
    return False


def apply_snapshot(session: Session, shipment: Shipment, item: dict) -> bool:
    """Apply a gettrackinfo accepted-item dict (v2.2 track_info shape) to the shipment.
    - Updates ETA from time_metrics.estimated_delivery_date (only when not stale).
    - Uses *latest event time* (not API sync time) as the monotonic cursor
      (last_snapshot_at).  A snapshot whose latest event is older than the
      stored cursor is considered stale and does not update status or ETA.
    - If all events lack timestamps and a cursor already exists, treated as stale.
    - Delivered is a terminal status; archived/trash buckets block status changes.
    - An empty/missing status string does not cause a transition to "untrackable".
    - Emits exactly one Notification per actual status transition.
    Returns True if the shipment status changed.
    Caller must commit.
    """
    # Re-fetch with row lock; populate_existing refreshes the identity-map object
    # so concurrent writes are visible after the lock is acquired.
    shipment = session.execute(
        select(Shipment).where(Shipment.id == shipment.id).with_for_update(),
        execution_options={"populate_existing": True},
    ).scalar_one()

    # v2.2 item shape: item.track_info contains all tracking data
    track = item.get("track_info") or item.get("track") or {}  # 17TRACK v2.2 uses track_info

    # ── Collect all events ──────────────────────────────────────────────
    all_events: list[tuple[dict, str]] = []
    seen_fps: set[str] = set()
    tracking_data = track.get("tracking") or {}
    for prov in tracking_data.get("providers") or []:
        prov_info = prov.get("provider") or {}
        prov_name = (prov_info.get("name") or "") if isinstance(prov_info, dict) else ""
        for ev in prov.get("events") or []:
            if not isinstance(ev, dict):
                continue
            fp = _event_fingerprint(ev)
            if fp not in seen_fps:
                seen_fps.add(fp)
                all_events.append((ev, prov_name))

    # Also include latest_event if not already captured from providers list
    latest_event = track.get("latest_event")
    if isinstance(latest_event, dict):
        le_fp = _event_fingerprint(latest_event)
        if le_fp not in seen_fps:
            seen_fps.add(le_fp)
            all_events.append((latest_event, ""))

    # ── Find max event time ─────────────────────────────────────────────
    max_event_time: datetime | None = None
    for ev, _ in all_events:
        t = _event_time(ev)
        if t is not None and (max_event_time is None or t > max_event_time):
            max_event_time = t

    # ── Stale guard ─────────────────────────────────────────────────────
    last_snap = _ensure_utc(shipment.last_snapshot_at) if shipment.last_snapshot_at else None
    is_stale = (
        # Events have no timestamps but we already have a cursor → don't regress
        (max_event_time is None and last_snap is not None)
        # Events are older than (or equal to) current cursor
        or (max_event_time is not None and last_snap is not None and max_event_time <= last_snap)
    )

    # Always persist events (fingerprint dedup is idempotent)
    _persist_events(session, shipment, all_events)

    if is_stale:
        session.flush()
        return False

    # ── Update ETA — only for non-stale snapshots ────────────────────────
    time_metrics = track.get("time_metrics") or {}
    eta = time_metrics.get("estimated_delivery_date")
    if eta:
        shipment.eta = eta

    # Advance snapshot cursor
    if max_event_time is not None:
        shipment.last_snapshot_at = max_event_time

    # ── Status update ───────────────────────────────────────────────────
    # Terminal: delivered (manual or prior snapshot)
    if shipment.status == "delivered":
        session.flush()
        return False

    # Archived/trashed buckets: no status changes
    if shipment.bucket in ("archive", "trash"):
        session.flush()
        return False

    latest_status_dict = track.get("latest_status") or {}
    status_str = latest_status_dict.get("status", "")
    if not status_str:
        # Missing/empty status: do not transition (guards against untrackable regression)
        session.flush()
        return False

    new_status = normalize_status(status_str, latest_status_dict.get("sub_status"))

    if new_status == shipment.status:
        session.flush()
        return False

    _do_transition(session, shipment, new_status, max_event_time or utcnow())
    return True


def refresh_shipment(session: Session, shipment: Shipment, provider) -> bool:
    """Fetch the latest tracking info from the provider and apply it.

    Returns True if the shipment status changed.
    Caller must commit.
    """
    item = provider.gettrackinfo(shipment.number, shipment.carrier)
    return apply_snapshot(session, shipment, item)


def apply_webhook(session: Session, payload: dict, raw_digest: str) -> int:
    """Process a 17track webhook payload transactionally.

    - Stores a WebhookReceipt keyed on raw_digest (SHA-256 hex of raw body).
    - Duplicate digests are a no-op (returns 0).
    - Only TRACKING_UPDATED events mutate shipment state.
    - TRACKING_STOPPED is acknowledged harmlessly (receipt stored, no mutation).
    - Unknown event types are acknowledged (receipt stored) without mutation.
    - Accepts data as a single dict (v2.2) or list (bulk/legacy).
    - Validates each data item is a dict; raises ValueError for non-dict items.
    - If a webhook carries an explicit carrier and the stored shipment has carrier=NULL,
      fills the carrier atomically; mismatched known carriers are silently skipped.
    - Raises ValueError for malformed payload (→ HTTP 400 in web layer).
    Returns the count of shipments whose status was updated.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"webhook payload must be a dict, got {type(payload).__name__!r}")
    if "data" not in payload:
        raise ValueError("webhook payload is missing required 'data' key")

    # Idempotent receipt: primary key is the raw_digest.
    # Use savepoint to handle concurrent duplicate receipts without crashing.
    if session.get(WebhookReceipt, raw_digest) is not None:
        return 0
    try:
        with session.begin_nested():
            session.add(WebhookReceipt(digest=raw_digest))
            session.flush()
    except IntegrityError:
        # Concurrent duplicate receipt — already processed
        return 0

    event_type: str = payload.get("event", "")

    # TRACKING_STOPPED — acknowledge safely, never invent a delivery
    if event_type == "TRACKING_STOPPED":
        return 0

    # Only TRACKING_UPDATED processes items; unknown events acknowledged, no mutation
    if event_type != "TRACKING_UPDATED":
        return 0

    items = _normalize_webhook_data(payload["data"])

    updated = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"webhook data item must be a dict, got {type(item).__name__!r}")
        number = item.get("number")
        if not number:
            continue
        carrier = item.get("carrier")

        # Match shipment by number; if webhook specifies a carrier, also match
        # shipments whose carrier is NULL (auto-detect, will be filled below).
        stmt = select(Shipment).where(Shipment.number == number)
        if carrier is not None:
            stmt = stmt.where(or_(Shipment.carrier == carrier, Shipment.carrier.is_(None)))

        shipment = session.execute(stmt).scalar_one_or_none()
        if shipment is None:
            continue

        # Fill NULL carrier atomically from signed webhook
        if carrier is not None:
            if shipment.carrier is None:
                shipment.carrier = carrier
                shipment.updated_at = utcnow()
                session.flush()
            elif shipment.carrier != carrier:
                # Known carrier mismatch: skip this item
                continue

        if apply_snapshot(session, shipment, item):
            updated += 1

    return updated


def process_operations(session: Session, provider, limit: int = 20) -> int:
    """Process due ProviderOperation rows with SELECT FOR UPDATE SKIP LOCKED.

    Ordering: register < stop < delete for the same shipment (chronological
    priority), allowing a shipment that is quickly archived after creation
    to still be registered before being stopped.

    Op dependency: stop waits for any pending register on the same shipment;
    delete waits for any pending register or stop.  This prevents cascaded
    failures when the earlier op is still retrying.

    Retries failed operations with exponential backoff (exponent capped to
    avoid huge integer arithmetic); sanitised error is stored on
    shipment.provider_error (never upstream body or key text).

    Unknown op kinds are recorded as failures (not silently marked done).
    Successful delete sets shipment.registered = False.
    Returns the count of operations that succeeded this call.
    """
    now = utcnow()

    rows = (
        session.execute(
            select(ProviderOperation)
            .where(ProviderOperation.done == False)  # noqa: E712
            .where(ProviderOperation.next_attempt_at <= now)
            .with_for_update(skip_locked=True)
            .order_by(_OP_PRIORITY, ProviderOperation.next_attempt_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    succeeded = 0
    for op in rows:
        shipment = session.get(Shipment, op.shipment_id)
        if shipment is None:
            # Cascade delete may have removed the shipment (concurrent scenario)
            op.done = True
            session.flush()
            continue

        try:
            if op.kind == "register":
                # Build v2.2 param string from stored additional fields (if any)
                param: str | None = None
                if shipment.carrier is not None:
                    # Detect unsupported required params BEFORE calling the provider.
                    # Silently sending empty values would cause a -18010018 reject loop.
                    unsupported = unsupported_required_params(shipment.carrier)
                    if unsupported:
                        error_msg = (
                            f"Registration blocked: carrier {shipment.carrier} requires "
                            f"parameter(s) that NovaTrack cannot supply: "
                            f"{', '.join(unsupported)}. "
                            "Update the carrier or contact support."
                        )
                        # Mark op done so the worker does not retry indefinitely.
                        # The shipment stays unregistered with an actionable error.
                        op.done = True
                        shipment.provider_error = error_msg[:200]
                        shipment.updated_at = utcnow()
                        session.flush()
                        continue
                    param = build_param_string(
                        shipment.carrier,
                        destination_country=shipment.destination_country,
                        destination_postal_code=shipment.destination_postal_code,
                        phone_number_last_4=shipment.phone_number_last_4,
                    )
                result = provider.register(shipment.number, shipment.carrier, param=param)
                # Persist detected/corrected carrier from provider response
                detected = result.get("carrier")
                if detected is not None:
                    shipment.carrier = detected
                shipment.registered = True
                shipment.provider_error = None
                shipment.updated_at = utcnow()

            elif op.kind == "stop":
                # Dependency: wait for any pending register on same shipment
                pending_reg = session.execute(
                    select(ProviderOperation).where(
                        ProviderOperation.shipment_id == op.shipment_id,
                        ProviderOperation.kind == "register",
                        ProviderOperation.done == False,  # noqa: E712
                        ProviderOperation.id != op.id,
                    )
                ).scalar_one_or_none()
                if pending_reg is not None:
                    continue  # Wait for register to complete first
                provider.stoptrack(shipment.number, shipment.carrier)
                shipment.provider_error = None
                shipment.updated_at = utcnow()

            elif op.kind == "delete":
                # Dependency: wait for any pending register or stop on same shipment
                pending_earlier = session.execute(
                    select(ProviderOperation).where(
                        ProviderOperation.shipment_id == op.shipment_id,
                        ProviderOperation.kind.in_(["register", "stop"]),
                        ProviderOperation.done == False,  # noqa: E712
                        ProviderOperation.id != op.id,
                    )
                ).scalar_one_or_none()
                if pending_earlier is not None:
                    continue  # Wait for earlier ops to complete
                provider.deletetrack(shipment.number, shipment.carrier)
                shipment.registered = False  # remote resource removed
                shipment.provider_error = None
                shipment.updated_at = utcnow()

            else:
                # Unknown op kind: record failure, do NOT mark done
                _record_retry(op, shipment, f"unknown op kind {op.kind!r}", now)
                session.flush()
                continue

            op.done = True
            session.flush()
            succeeded += 1

        except ProviderError as exc:
            if exc.terminal:
                # Permanent blocker (e.g. -18010022 date param): mark op done, no retry.
                op.done = True
                shipment.provider_error = str(exc)[:200]
                shipment.updated_at = utcnow()
                session.flush()
            else:
                _record_retry(op, shipment, str(exc), now, reset_immediately=exc.retryable_without_quota)
        except Exception:
            # Generic network failure — generic message, no upstream content
            _record_retry(op, shipment, "provider unavailable", now)

    return succeeded


def _record_retry(
    op: ProviderOperation,
    shipment: Shipment,
    error: str,
    now: datetime,
    *,
    reset_immediately: bool = False,
) -> None:
    op.attempts += 1
    op.last_error = error[:200]
    if reset_immediately:
        # Missing-param error: the op cannot make progress until the user adds
        # the missing field(s).  Set next_attempt_at to _PAUSED_INDEFINITELY so
        # the worker NEVER picks it up again.  The only way to resume is for the
        # user to supply the missing fields via POST /shipments/{id}/params (UI
        # or API), which calls update_shipment_params() and resets
        # next_attempt_at to now.
        op.next_attempt_at = _PAUSED_INDEFINITELY
    else:
        delay = _backoff_delay(op.attempts)
        op.next_attempt_at = now + timedelta(seconds=delay)
    shipment.provider_error = error[:200]
    shipment.updated_at = utcnow()
    session = Session.object_session(op)
    if session is not None:
        session.flush()


def restore_shipment(session: Session, shipment: Shipment, provider) -> bool:
    """Restore a trashed shipment back to the active bucket.

    Three registration states handled:

    1. ``registered=True`` (pending delete not yet run):
       The provider still has the number.  Cancel the pending delete, reset
       ``registered=False``, and queue a fresh register op.  The worker will
       call register; the provider returns -18019901 (already registered) which
       is treated as idempotent success.  One quota slot is NOT consumed (provider
       only charges on first acceptance, not on already-registered rejection).
       Returns True so the caller can warn the user.

    2. ``registered=False`` with a completed delete op (done=True):
       The worker deleted the number at the provider while the shipment was in
       trash.  No pending deletes to cancel.  Queue a fresh register op (costs one
       quota slot on the next worker tick).  Returns True.

    3. ``registered=False`` with a pending register op (paused/not-yet-run):
       The shipment was never confirmed registered.  Preserve the existing
       register op (it will run / un-pause once the user supplies missing params).
       Returns False.

    Returns True when a re-register op was queued (caller should warn about
    quota cost), False otherwise.
    Caller must commit.
    """
    if shipment.bucket != "trash":
        raise ValueError("Only trashed shipments can be restored")

    # Cancel pending delete ops so they don't run after the bucket changes.
    pending_deletes = (
        session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == shipment.id,
                ProviderOperation.kind == "delete",
                ProviderOperation.done == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    for op in pending_deletes:
        op.done = True
    session.flush()

    shipment.bucket = "active"
    shipment.trashed_at = None
    shipment.updated_at = utcnow()
    session.flush()

    reregister = False
    if shipment.registered:
        # State 1: registered=True — pending delete was cancelled; provider still
        # has the number.  Reset and re-register (idempotent, no quota charge).
        shipment.registered = False
        shipment.provider_error = None
        session.add(ProviderOperation(shipment_id=shipment.id, kind="register"))
        session.flush()
        reregister = True
    else:
        # State 2 vs State 3: registered=False.
        # If a pending register op exists, it will proceed on its own (State 3).
        # If no pending register op but a completed delete op exists, the provider
        # deleted the number while in trash — queue a re-register (State 2, costs quota).
        has_pending_register = (
            session.execute(
                select(ProviderOperation).where(
                    ProviderOperation.shipment_id == shipment.id,
                    ProviderOperation.kind == "register",
                    ProviderOperation.done == False,  # noqa: E712
                )
            ).scalar_one_or_none()
            is not None
        )
        if not has_pending_register:
            has_done_delete = (
                session.execute(
                    select(ProviderOperation).where(
                        ProviderOperation.shipment_id == shipment.id,
                        ProviderOperation.kind == "delete",
                        ProviderOperation.done == True,  # noqa: E712
                    )
                ).scalar_one_or_none()
                is not None
            )
            if has_done_delete:
                # Provider deleted the number while in trash; re-register costs quota.
                shipment.provider_error = None
                session.add(ProviderOperation(shipment_id=shipment.id, kind="register"))
                session.flush()
                reregister = True

    return reregister


def purge_shipment(session: Session, shipment: Shipment) -> None:
    """Hard-delete a trashed shipment, preserving provider deletion lifecycle.

    Only allowed when:
    - Shipment is in the trash bucket.
    - All provider operations are done (op.done=True) or no ops exist.
      A pending delete means the remote resource has not been cleaned up yet;
      purging before that completes would orphan the provider record.

    CASCADE removes TrackingEvent, Transition, ProviderOperation rows.
    Notification rows have no FK to shipments and are preserved.

    Raises ValueError if the shipment is not in trash or has pending ops.
    Caller must commit.
    """
    if shipment.bucket != "trash":
        raise ValueError("Only trashed shipments can be purged")

    pending_op = (
        session.execute(
            select(ProviderOperation)
            .where(
                ProviderOperation.shipment_id == shipment.id,
                ProviderOperation.done == False,  # noqa: E712
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if pending_op is not None:
        raise ValueError(
            "Cannot purge — provider deletion is still pending. "
            "Try again once the shipment has been fully deleted by the provider."
        )

    session.delete(shipment)
    session.flush()


def force_purge_unregistered_trash(session: Session, shipment: Shipment) -> None:
    """Immediately hard-delete a trashed, NEVER-registered shipment.

    This is safe because the shipment was never registered with the provider —
    there is no remote resource to orphan.  Any pending deletetrack operations
    are cancelled (marked done) before deletion to prevent spurious provider calls.

    Raises ValueError if the shipment is not in trash, or if it was ever
    registered with the provider (use restore_shipment + purge_shipment instead
    to preserve the provider lifecycle for registered shipments).

    CASCADE removes TrackingEvent, Transition, ProviderOperation rows.
    Notification rows have no FK to shipments and are preserved.
    Caller must commit.
    """
    if shipment.bucket != "trash":
        raise ValueError("Only trashed shipments can be force-purged")
    if shipment.registered:
        raise ValueError(
            "force_purge_unregistered_trash may only be used for shipments that "
            "were never registered with the provider.  Use restore_shipment or "
            "purge_shipment instead."
        )

    # Cancel any pending ops (deletetrack for an unregistered shipment is
    # idempotent — the provider would return -18019902 — so cancelling is safe).
    pending_ops = (
        session.execute(
            select(ProviderOperation).where(
                ProviderOperation.shipment_id == shipment.id,
                ProviderOperation.done == False,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    for op in pending_ops:
        op.done = True
    session.flush()

    session.delete(shipment)
    session.flush()


def is_registration_paused(session: Session, shipment: Shipment) -> bool:
    """Return True if the shipment's pending register op is paused indefinitely.

    A register op is paused when the provider rejected it for a missing/invalid
    additional parameter and next_attempt_at was set to _PAUSED_INDEFINITELY
    (year 9999).  The worker will never retry it; the user must update the
    tracking parameters via POST /shipments/{id}/params to resume.
    """
    pending_reg = session.execute(
        select(ProviderOperation).where(
            ProviderOperation.shipment_id == shipment.id,
            ProviderOperation.kind == "register",
            ProviderOperation.done == False,  # noqa: E712
        )
    ).scalar_one_or_none()
    if pending_reg is None:
        return False
    # Paused sentinel: year 9999 (>= 1000 years from now)
    if pending_reg.next_attempt_at is None:
        return False
    nat = pending_reg.next_attempt_at
    if nat.tzinfo is None:
        nat = nat.replace(tzinfo=UTC)
    return nat.year >= 9000


def housekeeping(session: Session, provider, now: datetime | None = None) -> None:
    """Periodic maintenance:

    1. Auto-archive delivered shipments older than settings.archive_days.
    2. Hard-delete trashed shipments older than settings.purge_days, **only
       after** ALL remote operations (register, stop, delete) have succeeded
       (done=True) or were never needed (TBA/never-registered, no ops queued).
    3. Notification rows are NOT cascaded — they survive shipment deletion
       to preserve the audit trail.

    Caller must commit.
    """
    if now is None:
        now = utcnow()

    settings = session.get(AppSettings, 1)
    if settings is None:
        return

    # ── Auto-archive delivered ───────────────────────────────────────────
    archive_cutoff = now - timedelta(days=settings.archive_days)
    for shipment in (
        session.execute(
            select(Shipment).where(
                Shipment.status == "delivered",
                Shipment.bucket == "active",
                Shipment.delivered_at <= archive_cutoff,
            )
        )
        .scalars()
        .all()
    ):
        archive_shipment(session, shipment, provider)

    # ── Purge old trash ──────────────────────────────────────────────────
    purge_cutoff = now - timedelta(days=settings.purge_days)
    for shipment in (
        session.execute(
            select(Shipment).where(
                Shipment.bucket == "trash",
                Shipment.trashed_at <= purge_cutoff,
            )
        )
        .scalars()
        .all()
    ):
        # Only purge if ALL remote operations have completed (done=True).
        # A pending register, stop, or delete means the remote resource may
        # still exist and must not be orphaned by a local delete.
        pending_op = (
            session.execute(
                select(ProviderOperation)
                .where(
                    ProviderOperation.shipment_id == shipment.id,
                    ProviderOperation.done == False,  # noqa: E712
                )
                .limit(1)
            )
            .scalars()
            .first()
        )
        if pending_op is not None:
            continue
        # CASCADE removes TrackingEvent, Transition, ProviderOperation.
        # Notification rows have no FK to shipments — they are preserved.
        session.delete(shipment)

    session.flush()
