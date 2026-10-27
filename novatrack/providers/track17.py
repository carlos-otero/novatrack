"""17TRACK v2.2 provider adapter.

Endpoint: POST https://api.17track.net/track/v2.2/{method}
Auth header: 17token
Response envelope: {"code": 0, "data": {"accepted": [...], "rejected": [...]}}

Error codes — v2.2 authoritative source: https://api.17track.net/en/doc

changecarrier error codes (verified against official docs 2026-10-27):
  -18019801  Multiple carriers registered — specify carrier_old to disambiguate
  -18019802  carrier_new value is incorrect
  -18019803  New carrier code cannot be same as current one
  -18019804  carrier_new or final_carrier_new must be specified
  -18019805  Tracking number not registered for carrier_old, or carrier_old incorrect
  -18019806  Number is stopped — retrack before changing carrier
  -18019807  Carrier change limit exceeded (max 5 per tracking number)
  -18019808  Tracking result not returned yet — wait before changing
  -18019809  Duplicate registration info already exists

Additional-parameter error codes (register):
  -18010018  Destination country (alpha-2) + postal code required
  -18010019  Destination postal code required
  -18010020  Phone number (last 4 digits) required
  -18010022  Shipping date required (unsupported by NovaTrack — terminal)

IMPORTANT: -18019807 is limit-exceeded, NOT same-carrier.  It must NEVER be
           treated as idempotent success.  Doing so silently ignores exhausted
           change slots and causes data corruption.
"""

from __future__ import annotations

import hashlib
import hmac

import httpx

from novatrack.providers import ProviderError

_BASE = "https://api.17track.net/track/v2.2"

# ── Error codes whose rejection is semantically idempotent ──────────────────
_ALREADY_REGISTERED = -18019901  # "Tracking number already registered"
_NOT_REGISTERED = -18019902  # "Tracking number is not registered yet"
_ALREADY_STOPPED = -18019906  # "Only numbers being tracked can be stopped"

# ── changecarrier error codes (verified against official API docs) ────────────
_CC_MULTIPLE_CARRIERS = -18019801   # Multiple carriers — specify carrier_old
_CC_INVALID_NEW_CARRIER = -18019802  # carrier_new value is incorrect
_CC_SAME_AS_CURRENT = -18019803     # New carrier same as current
_CC_MISSING_NEW = -18019804         # carrier_new / final_carrier_new not specified
_CC_OLD_NOT_REGISTERED = -18019805  # carrier_old not registered / incorrect
_CC_STOPPED = -18019806             # Number stopped — retrack first
_CC_LIMIT_EXCEEDED = -18019807      # Change limit (max 5) reached — NOT idempotent
_CC_WAIT_RESULTS = -18019808        # Tracking result pending — wait before changing
_CC_DUPLICATE_INFO = -18019809      # Duplicate registration info

# Human-readable messages for changecarrier rejection codes
_CC_MESSAGES: dict[int, str] = {
    _CC_MULTIPLE_CARRIERS: (
        "Multiple carriers are registered for this tracking number — "
        "specify carrier_old to identify which one to replace"
    ),
    _CC_INVALID_NEW_CARRIER: "The new carrier code is incorrect",
    _CC_SAME_AS_CURRENT: "The new carrier code cannot be the same as the current carrier",
    _CC_MISSING_NEW: "carrier_new or final_carrier_new must be specified",
    _CC_OLD_NOT_REGISTERED: (
        "The tracking number is not registered for the specified carrier, "
        "or carrier_old is incorrect"
    ),
    _CC_STOPPED: (
        "Carrier cannot be changed for stopped shipments — "
        "retrack the shipment before changing carrier"
    ),
    _CC_LIMIT_EXCEEDED: (
        "Carrier change limit reached — this tracking number has already been "
        "changed 5 times (maximum)"
    ),
    _CC_WAIT_RESULTS: (
        "Tracking result has not been returned yet after the last "
        "registration or modification — wait for results before changing carrier"
    ),
    _CC_DUPLICATE_INFO: (
        "This carrier registration already exists for the tracking number "
        "and cannot be changed to duplicate information"
    ),
}

# ── Additional-parameter error codes (register) ──────────────────────────────
_MISSING_COUNTRY_POSTAL = -18010018  # Destination country + postal code required
_MISSING_POSTAL = -18010019          # Destination postal code required
_MISSING_PHONE = -18010020           # Phone number (last 4) required
_MISSING_DATE = -18010022            # Shipping date required (unsupported by NovaTrack)

# Codes that the user CAN fix by supplying missing fields in the UI (pause indefinitely).
# -18010022 (shipping date) is NOT retryable — NovaTrack cannot supply ship_date
# and the op must be marked done=True (terminal) so the worker never retries it.
_PARAM_RETRYABLE_CODES = frozenset({_MISSING_COUNTRY_POSTAL, _MISSING_POSTAL, _MISSING_PHONE})

# Accurate user-facing messages per missing-param code:
_PARAM_MESSAGES: dict[int, str] = {
    _MISSING_COUNTRY_POSTAL: (
        "Registration requires the destination country (ISO alpha-2) and "
        "postal code for this carrier. Please add the missing field(s) and save."
    ),
    _MISSING_POSTAL: (
        "Registration requires the destination postal code for this carrier. "
        "Please add the postal code and save."
    ),
    _MISSING_PHONE: (
        "Registration requires the last 4 digits of the recipient's phone "
        "number for this carrier. Please add the phone field and save."
    ),
    _MISSING_DATE: (
        "This carrier requires a shipping date parameter that NovaTrack cannot "
        "currently supply. Update the carrier or contact support."
    ),
}

# ── Canonical status mapping ─────────────────────────────────────────────────
_STATUS_MAP: dict[str, str] = {
    "notfound": "pending",
    "notfound_invalidcode": "untrackable",
    "inforeceived": "info_received",
    "intransit": "in_transit",
    "outfordelivery": "out_for_delivery",
    "availableforpickup": "out_for_delivery",
    "deliveryfailure": "exception",  # main status DeliveryFailure → exception
    "delivered": "delivered",
    "exception": "exception",
    "expired": "expired",
    "undelivered": "exception",
    "returning": "exception",
}


def normalize_status(status: str, sub_status: str | None = None) -> str:
    """Map 17track status/sub_status to one of 9 canonical NovaTrack states.

    Canonical states: pending | info_received | in_transit | customs |
                      out_for_delivery | delivered | exception | expired | untrackable
    """
    if sub_status:
        sub = sub_status.lower()
        # Invalid tracking number
        if "invalidcode" in sub:
            return "untrackable"
        # Customs branch — must check before generic InTransit fall-through
        if "customsprocessing" in sub or "requiringinformation" in sub:
            return "customs"
        if "customsreleased" in sub:
            return "in_transit"
        # Delivery state overrides
        if "deliveryfailure" in sub:
            return "exception"
        if "availableforpickup" in sub:
            return "out_for_delivery"
    return _STATUS_MAP.get(status.lower(), "untrackable")


def verify_signature(raw_body: bytes, sign: str, key: str) -> bool:
    """Verify 17track webhook signature (v2.2 algorithm only).

    Algorithm: SHA256(raw_body + b'/' + key.encode()).hexdigest()
    Uses hmac.compare_digest for constant-time comparison.
    Never use the v1 body-only algorithm.
    """
    expected = hashlib.sha256(raw_body + b"/" + key.encode()).hexdigest()
    return hmac.compare_digest(expected, sign)


class Track17Provider:
    """Synchronous 17TRACK v2.2 adapter.

    api_key is kept private and never appears in logs or exception messages.
    client may be injected (e.g. httpx.MockTransport in tests).
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=30.0)

    def __del__(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass

    # ── Internal plumbing ───────────────────────────────────────────────────

    def _post(self, method: str, body: list) -> dict:
        """POST to 17track, return parsed data dict. Never leaks key or body."""
        url = f"{_BASE}/{method}"
        try:
            resp = self._client.post(
                url,
                headers={"17token": self._api_key, "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            raise ProviderError("upstream HTTP error")
        except httpx.RequestError:
            raise ProviderError("upstream connection error")

        try:
            payload = resp.json()
        except Exception:
            raise ProviderError("upstream response not parseable")

        if not isinstance(payload, dict):
            raise ProviderError("upstream response is not a JSON object")

        code = payload.get("code")
        if code != 0:
            raise ProviderError(f"provider returned error code {code}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderError("provider response missing data dict")

        # Detect schema error path: data has 'errors' but no accepted/rejected
        if "errors" in data and "accepted" not in data and "rejected" not in data:
            err_list = data.get("errors") or []
            codes = [str(e.get("code", "?")) for e in err_list if isinstance(e, dict)]
            raise ProviderError(f"provider returned schema error(s): {', '.join(codes)}")

        return data

    def _find_accepted(self, data: dict, number: str) -> dict | None:
        for item in data.get("accepted") or []:
            if isinstance(item, dict) and item.get("number") == number:
                return item
        return None

    def _find_rejected(self, data: dict, number: str) -> dict | None:
        for item in data.get("rejected") or []:
            if isinstance(item, dict) and item.get("number") == number:
                return item
        return None

    # ── Public API ──────────────────────────────────────────────────────────

    def register(
        self,
        number: str,
        carrier: int | None = None,
        *,
        param: str | None = None,
    ) -> dict:
        """Register a tracking number. Idempotent if already registered.

        param: v2.2 additional-parameters string (semicolon-separated values
               in the order declared by additional_parameters.json for this
               carrier).  Pass None when no additional params are needed.
        """
        body: dict = {"number": number}
        if carrier is not None:
            body["carrier"] = carrier
        if param is not None:
            body["param"] = param
        data = self._post("register", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            if err_code == _ALREADY_REGISTERED:
                # Recover carrier from rejected entry if provider supplied it
                detected = rej.get("carrier")
                effective_carrier = detected if detected is not None else carrier
                return {"number": number, "carrier": effective_carrier}

            # Missing-param codes the user can fix via the UI params form.
            # These are paused indefinitely (retryable_without_quota=True).
            if err_code in _PARAM_RETRYABLE_CODES:
                msg = _PARAM_MESSAGES.get(
                    int(err_code) if isinstance(err_code, int) else 0,
                    "Missing required tracking parameter — please update the fields and retry.",
                )
                raise ProviderError(msg, retryable_without_quota=True)

            # Shipping date required — NovaTrack cannot supply ship_date; terminal.
            if err_code == _MISSING_DATE:
                raise ProviderError(_PARAM_MESSAGES[_MISSING_DATE], terminal=True)

            raise ProviderError(f"register rejected (code {err_code})")

        raise ProviderError("register: number absent from accepted and rejected")

    def gettrackinfo(self, number: str, carrier: int | None = None) -> dict:
        """Fetch tracking snapshot. Returns the full accepted item dict."""
        body: dict = {"number": number}
        if carrier is not None:
            body["carrier"] = carrier
        data = self._post("gettrackinfo", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            raise ProviderError(f"gettrackinfo rejected (code {err_code})")

        raise ProviderError("gettrackinfo: number absent from response")

    def stoptrack(self, number: str, carrier: int | None = None) -> dict:
        """Stop tracking. Idempotent if already stopped or not registered."""
        body: dict = {"number": number}
        if carrier is not None:
            body["carrier"] = carrier
        data = self._post("stoptrack", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            if err_code in (_ALREADY_STOPPED, _NOT_REGISTERED):
                return {"number": number}
            raise ProviderError(f"stoptrack rejected (code {err_code})")

        raise ProviderError("stoptrack: number absent from response")

    def deletetrack(self, number: str, carrier: int | None = None) -> dict:
        """Delete a tracked number. Idempotent if not registered."""
        body: dict = {"number": number}
        if carrier is not None:
            body["carrier"] = carrier
        data = self._post("deletetrack", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            if err_code == _NOT_REGISTERED:
                return {"number": number}
            raise ProviderError(f"deletetrack rejected (code {err_code})")

        raise ProviderError("deletetrack: number absent from response")

    def getquota(self) -> dict:
        """Return account quota. The data dict is returned as-is."""
        return self._post("getquota", [])

    def changecarrier(
        self,
        number: str,
        carrier_new: int,
        carrier_old: int | None = None,
    ) -> dict:
        """Change the carrier for a registered tracking number.

        carrier_old is optional; pass it when the number has multiple assigned
        carriers to disambiguate which one to replace (-18019801).

        Raises ProviderError with a human-readable message on all rejections,
        including the full -18019801..-18019809 range.

        IMPORTANT: -18019807 is carrier-change LIMIT EXCEEDED, not "same carrier".
        It is NEVER treated as idempotent success; it always raises ProviderError.
        Costs one shipment quota on acceptance.
        """
        body: dict = {"number": number, "carrier_new": carrier_new}
        if carrier_old is not None:
            body["carrier_old"] = carrier_old
        data = self._post("changecarrier", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            msg = (
                _CC_MESSAGES.get(int(err_code), f"changecarrier rejected (code {err_code})")
                if isinstance(err_code, int)
                else f"changecarrier rejected (code {err_code})"
            )
            raise ProviderError(msg)

        raise ProviderError("changecarrier: number absent from accepted and rejected")
