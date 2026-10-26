"""Security primitives: API key generation/verification, webhook signature,
rate limiting, and security response headers.

Nothing here does I/O or depends on siblings — import-safe at any time.
"""

import hashlib
import hmac
import secrets
import time
from threading import Lock

from fastapi import Request

# ---------------------------------------------------------------------------
# API key primitives
# ---------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, sha256_digest).  Store only the digest."""
    raw = secrets.token_hex(32)  # 64-hex chars = 256 bits entropy
    return raw, hash_api_key(raw)


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest of a raw API key string."""
    return hashlib.sha256(key.encode()).hexdigest()


def get_api_key_from_request(request: Request) -> str | None:
    """Extract raw key from 'Bearer <token>' or 'X-API-Key: <token>' header."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
        return tok if tok else None
    tok = request.headers.get("X-API-Key", "").strip()
    return tok if tok else None


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------


def verify_webhook_signature(raw_body: bytes, sign: str, key: str) -> bool:
    """SHA256(raw_body + b'/' + key.encode()).hexdigest(), constant-time compare.

    Matches the 17TRACK v2 webhook signing scheme documented at
    https://api.17track.net/en/doc (and mirrored in providers.track17.verify_signature).
    """
    expected = hashlib.sha256(raw_body + b"/" + key.encode()).hexdigest()
    return hmac.compare_digest(expected, sign)


# ---------------------------------------------------------------------------
# In-memory rate limiter
# ---------------------------------------------------------------------------

_RATE_LIMITER_MAX_KEYS = 4096  # bounded memory


class RateLimiter:
    """Sliding-window, in-memory per-key rate limiter.

    Thread-safe; bounded at MAX_KEYS entries to prevent memory exhaustion.
    Keys are typically hashed credential digests or hashed IP addresses —
    never raw secrets or plaintext IPs.
    """

    def __init__(self, limit: int, window: int) -> None:
        self._limit = limit
        self._window = window  # seconds
        self._data: dict[str, list[float]] = {}
        self._lock = Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            # Evict oldest entry when at capacity
            if key not in self._data and len(self._data) >= _RATE_LIMITER_MAX_KEYS:
                oldest = next(iter(self._data))
                del self._data[oldest]

            hits = [t for t in self._data.get(key, []) if t > cutoff]
            if len(hits) >= self._limit:
                self._data[key] = hits
                return False
            hits.append(now)
            self._data[key] = hits
            return True


# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}
