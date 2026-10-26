"""Carrier catalogue helpers.

Loads the bundled carrier list and additional-parameters index from the
package's ``data/`` directory.  These are offline copies of the official
17TRACK JSON feeds:

  https://res.17track.net/asset/carrier/info/apicarrier.all.json
  https://res.17track.net/asset/carrier/info/additional_parameters.json

All lookups are O(1) after the first call (module-level caches).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

_DATA_DIR = Path(__file__).parent / "data"
_STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Carrier list (slim: key + name)
# ---------------------------------------------------------------------------


class CarrierEntry(TypedDict):
    key: int
    name: str


_CARRIERS: list[CarrierEntry] | None = None
_CARRIER_BY_KEY: dict[int, CarrierEntry] | None = None


def _load_carriers() -> list[CarrierEntry]:
    global _CARRIERS, _CARRIER_BY_KEY
    if _CARRIERS is not None:
        return _CARRIERS
    path = _STATIC_DIR / "carriers.json"
    with path.open(encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)
    _CARRIERS = [{"key": c["key"], "name": c["name"]} for c in raw]
    _CARRIER_BY_KEY = {c["key"]: c for c in _CARRIERS}
    return _CARRIERS


def get_carrier(code: int) -> CarrierEntry | None:
    """Return carrier entry for *code*, or None if unknown."""
    _load_carriers()
    assert _CARRIER_BY_KEY is not None
    return _CARRIER_BY_KEY.get(code)


def search_carriers(query: str, limit: int = 20) -> list[CarrierEntry]:
    """Return up to *limit* carriers whose name or key contains *query*."""
    carriers = _load_carriers()
    q = query.strip().lower()
    if not q:
        return carriers[:limit]
    results: list[CarrierEntry] = []
    for c in carriers:
        if q in c["name"].lower() or q == str(c["key"]):
            results.append(c)
            if len(results) >= limit:
                break
    return results


# ---------------------------------------------------------------------------
# Additional parameters index
# ---------------------------------------------------------------------------


class ParamDef(TypedDict):
    paramKey: str
    description: str
    options: dict | None
    sample: str
    require: bool


_ADDITIONAL_PARAMS: dict[int, list[ParamDef]] | None = None

# Subset of paramKeys we can supply from our DB columns.
# Any required paramKey NOT in this set is unsupported; registration must be
# blocked with an actionable message rather than silently sending empty values.
_OUR_PARAM_KEYS = frozenset({"destination_country", "postal_code", "phone_number_last_4"})


def _load_additional_params() -> dict[int, list[ParamDef]]:
    global _ADDITIONAL_PARAMS
    if _ADDITIONAL_PARAMS is not None:
        return _ADDITIONAL_PARAMS
    path = _DATA_DIR / "additional_parameters.json"
    with path.open(encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)
    _ADDITIONAL_PARAMS = {
        int(entry["key"]): [
            {
                "paramKey": p["paramKey"],
                "description": p.get("description", ""),
                "options": p.get("options"),
                "sample": p.get("sample", ""),
                "require": bool(p.get("require", False)),
            }
            for p in entry.get("parameters", [])
        ]
        for entry in raw
    }
    return _ADDITIONAL_PARAMS


def get_additional_params(carrier: int) -> list[ParamDef]:
    """Return the additional-parameter definitions for *carrier*, or []."""
    return _load_additional_params().get(carrier, [])


def required_param_keys(carrier: int) -> list[str]:
    """Return list of paramKeys that are required for *carrier*."""
    return [p["paramKey"] for p in get_additional_params(carrier) if p["require"]]


def build_param_string(
    carrier: int,
    *,
    destination_country: str | None = None,
    destination_postal_code: str | None = None,
    phone_number_last_4: str | None = None,
) -> str | None:
    """Build the v2.2 ``param`` string for *carrier* given our known fields.

    The v2.2 ``param`` field is a semicolon-separated list of values, one
    per entry in ``additional_parameters.json``, in the declared order.
    Only our three supported paramKeys are handled; unknown paramKeys produce
    an empty string for that position (which may cause a -18010018 if required).

    Returns None when the carrier has no additional parameters.
    """
    params = get_additional_params(carrier)
    if not params:
        return None

    # Map paramKey → value we can supply
    value_map: dict[str, str] = {}
    if destination_country:
        value_map["destination_country"] = destination_country.strip().upper()[:2]
    if destination_postal_code:
        value_map["postal_code"] = destination_postal_code.strip()
    if phone_number_last_4:
        value_map["phone_number_last_4"] = phone_number_last_4.strip()

    parts: list[str] = []
    for p in params:
        parts.append(value_map.get(p["paramKey"], ""))

    param_str = ";".join(parts)
    # Return None if everything is empty (no useful data to send)
    return param_str if any(parts) else None


def missing_required_params(
    carrier: int,
    *,
    destination_country: str | None = None,
    destination_postal_code: str | None = None,
    phone_number_last_4: str | None = None,
) -> list[str]:
    """Return list of required paramKeys that are missing for *carrier*.

    Only considers paramKeys that NovaTrack supports (see ``_OUR_PARAM_KEYS``).
    Unsupported paramKeys are reported separately by ``unsupported_required_params``.
    """
    value_map: dict[str, str | None] = {
        "destination_country": destination_country,
        "postal_code": destination_postal_code,
        "phone_number_last_4": phone_number_last_4,
    }
    missing = []
    for p in get_additional_params(carrier):
        if p["require"] and p["paramKey"] in _OUR_PARAM_KEYS and not value_map.get(p["paramKey"]):
            missing.append(p["paramKey"])
    return missing


def unsupported_required_params(carrier: int) -> list[str]:
    """Return required paramKeys for *carrier* that NovaTrack cannot supply.

    If this list is non-empty, registration must be blocked — silently sending
    empty values for required fields would cause a -18010018 loop without ever
    succeeding.  The caller should surface an actionable error to the user.
    """
    return [
        p["paramKey"]
        for p in get_additional_params(carrier)
        if p["require"] and p["paramKey"] not in _OUR_PARAM_KEYS
    ]
