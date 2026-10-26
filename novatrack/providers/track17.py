"""17TRACK v2.2 provider adapter.

Endpoint: POST https://api.17track.net/track/v2.2/{method}
Auth header: 17token
Response envelope: {"code": 0, "data": {"accepted": [...], "rejected": [...]}}

Error codes (17track API v2.2, authoritative source):
  -18019901  already registered   (register → idempotent OK)
  -18019902  not registered       (stoptrack, deletetrack → idempotent OK)
  -18019906  only tracked numbers can be stopped (already stopped → idempotent OK)

  Note: -18019904 = "Only stopped numbers can be re-tracked" (retrack, NOT register)
        -18019910 = "Carrier Code incorrect" (NOT already-stopped)

changecarrier error codes (v2.2):
  -18019801  Not registered — only registered tracking numbers can have carrier changed
  -18019802  Number is being processed — retry later
  -18019803  carrier_new cannot be null
  -18019804  Carrier code does not exist
  -18019805  Tracking number format does not match the specified carrier
  -18019806  Carrier can only be changed for active (Tracking) numbers
  -18019807  Already using this carrier (idempotent)
  -18019808  Carrier change limit exceeded (max 5 changes per number)

Additional-parameter error codes (register):
  -18010018  Missing required additional parameter (e.g. postal_code)
  -18010019  Additional parameter format invalid
  -18010020  Additional parameter value invalid
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

# ── changecarrier error codes ────────────────────────────────────────────────
_CC_NOT_REGISTERED = -18019801  # Number must be registered first
_CC_PROCESSING = -18019802  # Number is currently being processed
_CC_NULL_CARRIER = -18019803  # carrier_new cannot be null
_CC_INVALID_CARRIER = -18019804  # Carrier code does not exist
_CC_FORMAT_MISMATCH = -18019805  # Number format incompatible with carrier
_CC_NOT_ACTIVE = -18019806  # Number not in active tracking state
_CC_SAME_CARRIER = -18019807  # Already using this carrier (idempotent)
_CC_LIMIT_EXCEEDED = -18019808  # Max 5 carrier changes reached

# Human-readable messages for changecarrier rejection codes
_CC_MESSAGES: dict[int, str] = {
    _CC_NOT_REGISTERED: "Shipment must be registered with 17TRACK before changing carrier",
    _CC_PROCESSING: "Shipment is currently being processed — please retry shortly",
    _CC_NULL_CARRIER: "New carrier code cannot be empty",
    _CC_INVALID_CARRIER: "Carrier code does not exist",
    _CC_FORMAT_MISMATCH: "Tracking number format does not match the specified carrier",
    _CC_NOT_ACTIVE: "Carrier can only be changed for actively-tracked shipments",
    _CC_SAME_CARRIER: "Shipment is already using this carrier",
    _CC_LIMIT_EXCEEDED: "Carrier has already been changed 5 times (maximum reached)",
}

# ── Additional-parameter error codes (register) ──────────────────────────────
_MISSING_PARAM = -18010018  # Missing required additional parameter
_INVALID_PARAM_FORMAT = -18010019  # Additional parameter format invalid
_INVALID_PARAM_VALUE = -18010020  # Additional parameter value invalid

# Human-readable prompts for missing-parameter rejections
_PARAM_MISSING_PROMPT = (
    "Registration requires additional information for this carrier — "
    "please add the missing field(s) (postal code, destination country, "
    "or last 4 digits of phone) and the shipment will be re-registered."
)

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
            if err_code in (_MISSING_PARAM, _INVALID_PARAM_FORMAT, _INVALID_PARAM_VALUE):
                raise ProviderError(_PARAM_MISSING_PROMPT, retryable_without_quota=True)
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
        carriers to disambiguate which one to replace.

        Raises ProviderError with a human-readable message on all rejections,
        including the -18019801..-18019808 range.
        -18019807 (same carrier) is treated as idempotent success.
        Costs one shipment quota on acceptance.
        """
        body: dict = {"number": number, "carrier": carrier_new}
        if carrier_old is not None:
            body["carrier_old"] = carrier_old
        data = self._post("changecarrier", [body])

        if acc := self._find_accepted(data, number):
            return acc

        rej = self._find_rejected(data, number)
        if rej is not None:
            err_code = (rej.get("error") or {}).get("code")
            if err_code == _CC_SAME_CARRIER:
                # Idempotent: already using this carrier
                return {"number": number, "carrier": carrier_new}
            msg = (
                _CC_MESSAGES.get(int(err_code), f"changecarrier rejected (code {err_code})")
                if isinstance(err_code, int)
                else f"changecarrier rejected (code {err_code})"
            )
            raise ProviderError(msg)

        raise ProviderError("changecarrier: number absent from accepted and rejected")
