"""Tests for novatrack/providers — no real network calls, no API keys."""
import hashlib
import inspect

import httpx
import pytest

from novatrack.providers import ProviderError
from novatrack.providers.track17 import (
    _ALREADY_REGISTERED,
    _ALREADY_STOPPED,
    _NOT_REGISTERED,
    Track17Provider,
    normalize_status,
    verify_signature,
)

# ─────────────────────────────────────────────────────────────
# normalize_status
# ─────────────────────────────────────────────────────────────

def test_normalize_pending_for_not_found():
    assert normalize_status("NotFound") == "pending"


def test_normalize_untrackable_for_invalid_code_sub_status():
    assert normalize_status("NotFound", "NotFound_InvalidCode") == "untrackable"


def test_normalize_info_received():
    assert normalize_status("InfoReceived") == "info_received"


def test_normalize_in_transit():
    assert normalize_status("InTransit") == "in_transit"


def test_normalize_delivered():
    assert normalize_status("Delivered") == "delivered"


def test_normalize_exception():
    assert normalize_status("Exception") == "exception"


def test_normalize_expired():
    assert normalize_status("Expired") == "expired"


def test_normalize_out_for_delivery():
    assert normalize_status("OutForDelivery") == "out_for_delivery"


def test_normalize_available_for_pickup_is_out_for_delivery():
    assert normalize_status("AvailableForPickup") == "out_for_delivery"


def test_normalize_undelivered_is_exception():
    assert normalize_status("Undelivered") == "exception"


def test_normalize_customs_processing_sub_status():
    assert normalize_status("InTransit", "CustomsProcessing") == "customs"


def test_normalize_requiring_information_sub_status():
    assert normalize_status("InTransit", "RequiringInformation") == "customs"


def test_normalize_customs_released_to_in_transit():
    assert normalize_status("InTransit", "CustomsReleased") == "in_transit"


def test_normalize_delivery_failure_to_exception():
    assert normalize_status("InTransit", "DeliveryFailure") == "exception"


def test_normalize_available_for_pickup_as_sub_status():
    assert normalize_status("InTransit", "AvailableForPickup") == "out_for_delivery"


def test_normalize_intransit_full_sub_status_string():
    """17track v2.2 often sends sub_status as 'ParentStatus_Detail'."""
    assert normalize_status("InTransit", "InTransit_CustomsProcessing") == "customs"


def test_normalize_unknown_status_returns_untrackable():
    assert normalize_status("CompletelyMadeUpStatus") == "untrackable"


def test_normalize_empty_sub_status_uses_main_status():
    assert normalize_status("InTransit", "") == "in_transit"


def test_normalize_delivery_failure_main_status_no_sub_status():
    """DeliveryFailure as main status (no sub_status) must map to exception.

    This was broken — DeliveryFailure was missing from _STATUS_MAP, causing
    it to fall through to 'untrackable'.
    """
    assert normalize_status("DeliveryFailure") == "exception"


def test_normalize_delivery_failure_with_sub_status():
    """DeliveryFailure_NoBody sub_status also maps to exception."""
    assert normalize_status("DeliveryFailure", "DeliveryFailure_NoBody") == "exception"


# ─────────────────────────────────────────────────────────────
# Exact error code constants (authoritative v2.2 docs)
# ─────────────────────────────────────────────────────────────

def test_already_registered_code_is_18019901():
    """-18019901 = 'Tracking number already registered' per v2.2 spec.

    Previous code wrongly used -18019904 ('Only stopped numbers can be re-tracked').
    """
    assert _ALREADY_REGISTERED == -18019901


def test_already_stopped_code_is_18019906():
    """-18019906 = 'Only numbers being tracked can be stopped' per v2.2 spec.

    Previous code wrongly used -18019910 ('Carrier Code incorrect').
    """
    assert _ALREADY_STOPPED == -18019906


def test_not_registered_code_is_18019902():
    assert _NOT_REGISTERED == -18019902


def test_retrack_only_code_18019904_is_not_already_registered():
    """Guard: -18019904 means re-track is not allowed (not already-registered)."""
    assert _ALREADY_REGISTERED != -18019904


# ─────────────────────────────────────────────────────────────
# verify_signature
# ─────────────────────────────────────────────────────────────

def test_verify_signature_correct():
    body = b'{"event":"TRACKING_UPDATED"}'
    key = "mywebhooksecret"
    sign = hashlib.sha256(body + b"/" + key.encode()).hexdigest()
    assert verify_signature(body, sign, key) is True


def test_verify_signature_wrong_sign_returns_false():
    body = b'{"event":"TRACKING_UPDATED"}'
    key = "mywebhooksecret"
    assert verify_signature(body, "badhash", key) is False


def test_verify_signature_wrong_key_returns_false():
    body = b'{"event":"TRACKING_UPDATED"}'
    key = "mywebhooksecret"
    sign = hashlib.sha256(body + b"/" + key.encode()).hexdigest()
    assert verify_signature(body, sign, "differentkey") is False


def test_verify_signature_empty_body():
    body = b""
    key = "mykey"
    sign = hashlib.sha256(b"/" + key.encode()).hexdigest()
    assert verify_signature(body, sign, key) is True


def test_verify_signature_uses_constant_time_compare():
    """Must use hmac.compare_digest, not plain ==, to prevent timing attacks."""
    src = inspect.getsource(verify_signature)
    assert "compare_digest" in src


def test_verify_signature_algorithm_is_sha256_body_slash_key():
    """Algorithm: SHA256(raw_body + b'/' + key.encode()).hexdigest()."""
    body = b"rawbody"
    key = "k"
    expected = hashlib.sha256(body + b"/" + key.encode()).hexdigest()
    assert verify_signature(body, expected, key) is True
    # v1 (body alone, no '/key') must NOT verify
    wrong_v1 = hashlib.sha256(body).hexdigest()
    assert verify_signature(body, wrong_v1, key) is False


# ─────────────────────────────────────────────────────────────
# MockTransport helpers
# ─────────────────────────────────────────────────────────────

def _make_transport(responses: list[dict]):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        assert queue, f"unexpected extra request to {request.url}"
        return httpx.Response(200, json=queue.pop(0))

    return httpx.MockTransport(handler)


def _provider(responses: list[dict]) -> Track17Provider:
    return Track17Provider(api_key="testkey", client=httpx.Client(transport=_make_transport(responses)))


def _ok(accepted=None, rejected=None, **extra_data):
    return {"code": 0, "data": {"accepted": accepted or [], "rejected": rejected or [], **extra_data}}


# ─────────────────────────────────────────────────────────────
# register
# ─────────────────────────────────────────────────────────────

def test_register_returns_accepted_item():
    p = _provider([_ok(accepted=[{"number": "TRACK123", "carrier": 100}])])
    result = p.register("TRACK123", 100)
    assert result["number"] == "TRACK123"
    assert result["carrier"] == 100


def test_register_without_carrier():
    p = _provider([_ok(accepted=[{"number": "TRACK123"}])])
    result = p.register("TRACK123")
    assert result["number"] == "TRACK123"


def test_register_already_registered_is_idempotent():
    """Rejection with -18019901 must be treated as idempotent success.

    Previously -18019904 was used (wrong: that code means 'retrack only' not 'already registered').
    """
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019901, "message": "already registered"}}])])
    result = p.register("TRACK123")
    assert result["number"] == "TRACK123"


def test_register_already_registered_recovers_carrier_from_rejected():
    """When idempotent, carrier present in rejected entry must be returned."""
    rej = {"number": "TRACK123", "carrier": 3011, "error": {"code": -18019901, "message": "registered"}}
    p = _provider([_ok(rejected=[rej])])
    result = p.register("TRACK123")
    assert result["carrier"] == 3011


def test_register_other_rejection_raises_provider_error():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -99, "message": "bad"}}])])
    with pytest.raises(ProviderError):
        p.register("TRACK123")


def test_register_nonzero_envelope_code_raises_provider_error():
    p = _provider([{"code": -1, "message": "Internal error"}])
    with pytest.raises(ProviderError):
        p.register("TRACK123")


def test_register_http_500_raises_sanitized_provider_error():
    def handler(req):
        return httpx.Response(500, text="Internal Server Error with api_key details")

    p = Track17Provider(api_key="supersecret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as exc:
        p.register("TRACK123")
    assert "supersecret" not in str(exc.value)


def test_register_error_never_contains_api_key():
    p = _provider([{"code": -42, "data": {}}])
    with pytest.raises(ProviderError) as exc:
        p.register("TRACK123")
    assert "testkey" not in str(exc.value)


def test_register_posts_to_correct_endpoint():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["header"] = req.headers.get("17token")
        return httpx.Response(200, json=_ok(accepted=[{"number": "T1"}]))

    p = Track17Provider(api_key="k1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    p.register("T1")
    assert captured["url"] == "https://api.17track.net/track/v2.2/register"
    assert captured["header"] == "k1"


def test_post_data_errors_field_raises_provider_error():
    """When data contains 'errors' but no accepted/rejected, raise ProviderError safely."""
    resp = {"code": 0, "data": {"errors": [{"code": -18010013, "message": "Invalid submitted data."}]}}
    p = _provider([resp])
    with pytest.raises(ProviderError):
        p.register("TRACK123")


def test_post_missing_data_dict_raises_provider_error():
    """code=0 but data is not a dict → ProviderError."""
    p = _provider([{"code": 0, "data": None}])
    with pytest.raises(ProviderError):
        p.register("TRACK123")


# ─────────────────────────────────────────────────────────────
# gettrackinfo  — uses exact v2.2 track_info shape
# ─────────────────────────────────────────────────────────────

SAMPLE_TRACK_INFO = {
    "latest_status": {"status": "InTransit", "sub_status": "InTransit_Other", "sub_status_descr": None},
    "latest_event": {
        "time_iso": "2024-01-15T10:00:00+00:00",
        "time_utc": "2024-01-15T10:00:00Z",
        "time_raw": {"date": "2024-01-15", "time": "10:00:00", "timezone": "+00:00"},
        "description": "Arrived at sorting facility",
        "location": "NY",
        "stage": "InTransit",
        "sub_status": "InTransit_Other",
    },
    "time_metrics": {
        "estimated_delivery_date": {"source": "17TRACK", "from": "2024-01-17", "to": "2024-01-18"},
    },
    "tracking": {
        "providers_hash": 12345,
        "providers": [
            {
                "provider": {"key": 100, "name": "TestCarrier"},
                "events": [
                    {
                        "time_iso": "2024-01-15T10:00:00+00:00",
                        "time_utc": "2024-01-15T10:00:00Z",
                        "description": "Arrived at sorting facility",
                        "location": "NY",
                        "stage": "InTransit",
                        "sub_status": "InTransit_Other",
                    }
                ],
            }
        ],
    },
}


def test_gettrackinfo_returns_accepted_item_with_track_info():
    """v2.2: accepted items use 'track_info' key, not 'track'."""
    p = _provider([_ok(accepted=[{"number": "TRACK123", "carrier": 100, "track_info": SAMPLE_TRACK_INFO}])])
    result = p.gettrackinfo("TRACK123", 100)
    assert result["number"] == "TRACK123"
    assert "track_info" in result
    assert result["track_info"]["latest_status"]["status"] == "InTransit"


def test_gettrackinfo_rejection_raises_provider_error():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019902, "message": "not registered"}}])])
    with pytest.raises(ProviderError):
        p.gettrackinfo("TRACK123")


def test_gettrackinfo_nonzero_code_raises_provider_error():
    p = _provider([{"code": -1}])
    with pytest.raises(ProviderError):
        p.gettrackinfo("TRACK123")


# ─────────────────────────────────────────────────────────────
# stoptrack
# ─────────────────────────────────────────────────────────────

def test_stoptrack_returns_accepted_item():
    p = _provider([_ok(accepted=[{"number": "TRACK123"}])])
    assert p.stoptrack("TRACK123")["number"] == "TRACK123"


def test_stoptrack_already_stopped_is_idempotent():
    """-18019906 ('only tracked numbers can be stopped') → idempotent OK.

    Previously -18019910 was used (wrong: that code means 'Carrier Code incorrect').
    """
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019906, "message": "only tracked numbers"}}])])
    assert p.stoptrack("TRACK123")["number"] == "TRACK123"


def test_stoptrack_not_registered_is_idempotent():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019902, "message": "not registered"}}])])
    assert p.stoptrack("TRACK123")["number"] == "TRACK123"


def test_stoptrack_other_rejection_raises():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -999, "message": "bad"}}])])
    with pytest.raises(ProviderError):
        p.stoptrack("TRACK123")


def test_stoptrack_carrier_incorrect_raises_not_idempotent():
    """-18019910 ('Carrier Code incorrect') must raise ProviderError, not be idempotent."""
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019910, "message": "carrier incorrect"}}])])
    with pytest.raises(ProviderError):
        p.stoptrack("TRACK123")


# ─────────────────────────────────────────────────────────────
# deletetrack
# ─────────────────────────────────────────────────────────────

def test_deletetrack_returns_accepted_item():
    p = _provider([_ok(accepted=[{"number": "TRACK123"}])])
    assert p.deletetrack("TRACK123")["number"] == "TRACK123"


def test_deletetrack_not_registered_is_idempotent():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -18019902, "message": "not registered"}}])])
    assert p.deletetrack("TRACK123")["number"] == "TRACK123"


def test_deletetrack_other_rejection_raises():
    p = _provider([_ok(rejected=[{"number": "TRACK123", "error": {"code": -999, "message": "bad"}}])])
    with pytest.raises(ProviderError):
        p.deletetrack("TRACK123")


# ─────────────────────────────────────────────────────────────
# getquota
# ─────────────────────────────────────────────────────────────

def test_getquota_returns_quota_data():
    p = _provider([_ok(total=1000, used=100, remain=900)])
    result = p.getquota()
    assert result["remain"] == 900


def test_getquota_nonzero_code_raises():
    p = _provider([{"code": -1}])
    with pytest.raises(ProviderError):
        p.getquota()


# ─────────────────────────────────────────────────────────────
# Protocol structural compliance
# ─────────────────────────────────────────────────────────────

def test_track17provider_has_all_protocol_methods():
    required = {"register", "gettrackinfo", "stoptrack", "deletetrack", "getquota"}
    p = Track17Provider(api_key="x")
    for m in required:
        assert callable(getattr(p, m, None)), f"Track17Provider missing '{m}'"


def test_track17provider_has_getquota_not_get_quota():
    """Method must be 'getquota', not 'get_quota'."""
    p = Track17Provider(api_key="x")
    assert callable(getattr(p, "getquota", None))
    assert not callable(getattr(p, "get_quota", None))
