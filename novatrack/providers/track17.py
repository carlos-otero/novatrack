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
"""
from __future__ import annotations

import hashlib
import hmac

import httpx

from novatrack.providers import ProviderError

_BASE = "https://api.17track.net/track/v2.2"

# ── Error codes whose rejection is semantically idempotent ──────────────────
_ALREADY_REGISTERED = -18019901   # "Tracking number already registered"
_NOT_REGISTERED = -18019902       # "Tracking number is not registered yet"
_ALREADY_STOPPED = -18019906      # "Only numbers being tracked can be stopped"

# ── Canonical status mapping ─────────────────────────────────────────────────
_STATUS_MAP: dict[str, str] = {
    "notfound": "pending",
    "notfound_invalidcode": "untrackable",
    "inforeceived": "info_received",
    "intransit": "in_transit",
    "outfordelivery": "out_for_delivery",
    "availableforpickup": "out_for_delivery",
    "deliveryfailure": "exception",   # main status DeliveryFailure → exception
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
            codes = [
                str(e.get("code", "?"))
                for e in err_list
                if isinstance(e, dict)
            ]
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

    def register(self, number: str, carrier: int | None = None) -> dict:
        """Register a tracking number. Idempotent if already registered."""
        body: dict = {"number": number}
        if carrier is not None:
            body["carrier"] = carrier
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
