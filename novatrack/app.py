"""FastAPI application factory for NovaTrack.

``create_app(settings, session_factory, provider)`` is the single entry point.
All dependencies are injected; siblings (tracking, notifications) are imported
directly — no lazy ImportError guards or _HAS stubs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from novatrack import notifications, tracking
from novatrack.auth import (
    create_oauth,
    generate_csrf_token,
    get_current_user,
    oidc_exchange,
    oidc_login,
    validate_csrf,
)
from novatrack.carriers import search_carriers
from novatrack.models import (
    ApiKey,
    AppSettings,
    NotificationChannel,
    Shipment,
    TrackingEvent,
)
from novatrack.providers import ProviderError
from novatrack.security import (
    SECURITY_HEADERS,
    RateLimiter,
    generate_api_key,
    get_api_key_from_request,
    hash_api_key,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"

_MAX_WEBHOOK_BYTES = 1024 * 1024  # 1 MiB
_DEFAULT_API_RATE_LIMIT = 120  # per minute per valid key
_DEFAULT_INVALID_KEY_LIMIT = 30  # per minute per source IP (brute-force guard)
_DEFAULT_WEBHOOK_RATE_LIMIT = 60  # per minute per source IP

_NUMBER_RE = re.compile(r"^[A-Za-z0-9\-]{5,50}$")
_SECRET_ENV_RE = re.compile(r"^NOVATRACK_WEBHOOK_SECRET_[A-Z0-9_]+$")
_VALID_CHANNEL_KINDS = {"webhook", "telegram"}
_VALID_EVENTS = {"status_changed", "delivered", "exception", "quota_low"}
_PG_INT_MAX = 2**31 - 1  # 2 147 483 647


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateShipmentRequest(BaseModel):
    number: str = Field(..., description="Tracking number (5-50 alphanumeric/hyphen)")
    carrier: int | None = Field(None, gt=0, le=_PG_INT_MAX, description="17TRACK numeric carrier code")
    name: str = Field("", max_length=200, description="Human label")
    store: str = Field("", max_length=200, description="Store or marketplace")
    notes: str = Field("", max_length=2000, description="Free-form notes")
    destination_country: str | None = Field(
        None, min_length=2, max_length=2, description="Destination country ISO alpha-2"
    )
    destination_postal_code: str | None = Field(None, max_length=20, description="Destination postal code")
    phone_number_last_4: str | None = Field(
        None, max_length=10, description="Last 4 digits of recipient phone"
    )

    @field_validator("number")
    @classmethod
    def validate_number(cls, v: str) -> str:
        if not _NUMBER_RE.match(v):
            raise ValueError("Tracking number must be 5-50 alphanumeric/hyphen characters")
        return v  # preserve exact case - no normalisation

    @field_validator("destination_country")
    @classmethod
    def validate_destination_country(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not v.isalpha() or len(v) != 2:
            raise ValueError("destination_country must be a 2-letter ISO alpha-2 code")
        return v

    @field_validator("phone_number_last_4")
    @classmethod
    def validate_phone_last_4(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not v.isdigit():
            raise ValueError("phone_number_last_4 must contain only digits")
        return v or None


class ChangeCarrierRequest(BaseModel):
    carrier_new: int = Field(..., gt=0, le=_PG_INT_MAX, description="New 17TRACK numeric carrier code")


class UpdateShipmentParamsRequest(BaseModel):
    """Partial update: update additional tracking params and trigger a retry."""

    destination_country: str | None = Field(
        None, min_length=2, max_length=2, description="Destination country ISO alpha-2"
    )
    destination_postal_code: str | None = Field(None, max_length=20, description="Destination postal code")
    phone_number_last_4: str | None = Field(
        None, max_length=10, description="Last 4 digits of recipient phone"
    )

    @field_validator("destination_country")
    @classmethod
    def validate_destination_country(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not v.isalpha() or len(v) != 2:
            raise ValueError("destination_country must be a 2-letter ISO alpha-2 code")
        return v

    @field_validator("phone_number_last_4")
    @classmethod
    def validate_phone_last_4(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not v.isdigit():
            raise ValueError("phone_number_last_4 must contain only digits")
        return v or None


class ShipmentResponse(BaseModel):
    id: str
    number: str
    carrier: int | None
    name: str
    store: str
    notes: str
    status: str
    bucket: str
    registered: bool
    eta: dict | None
    created_at: str
    updated_at: str
    provider_error: str | None
    destination_country: str | None
    destination_postal_code: str | None
    phone_number_last_4: str | None

    @classmethod
    def from_orm(cls, s: Shipment) -> "ShipmentResponse":
        return cls(
            id=s.id or "",
            number=s.number or "",
            carrier=s.carrier,
            name=s.name or "",
            store=s.store or "",
            notes=s.notes or "",
            status=s.status or "pending",
            bucket=s.bucket or "active",
            registered=bool(s.registered),
            eta=s.eta,
            created_at=s.created_at.isoformat() if s.created_at else "",
            updated_at=s.updated_at.isoformat() if s.updated_at else "",
            provider_error=s.provider_error,
            destination_country=s.destination_country,
            destination_postal_code=s.destination_postal_code,
            phone_number_last_4=s.phone_number_last_4,
        )


# ---------------------------------------------------------------------------
# Middleware: security headers
# ---------------------------------------------------------------------------


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        return response


# ---------------------------------------------------------------------------
# Middleware: auth enforcement
# ---------------------------------------------------------------------------

# /static/auth.css must be public so the unauthenticated login page loads its CSS.
# ALL other /static/* assets require a session.
_AUTH_EXEMPT_EXACT = frozenset({"/healthz", "/auth/login", "/auth/callback", "/static/auth.css"})
_API_PREFIX = "/api/"
_WEBHOOK_PATH = "/webhooks/17track"


class AuthEnforcementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in _AUTH_EXEMPT_EXACT:
            return await call_next(request)

        # Webhook - signature checked inside the handler
        if path == _WEBHOOK_PATH and request.method == "POST":
            return await call_next(request)

        # API routes - API-key dependency on router/catch-all handles auth
        if path.startswith(_API_PREFIX):
            return await call_next(request)

        # Everything else (including /static/*) requires an OIDC session
        user = request.session.get("user")
        if not user or not user.get("sub"):
            accept = request.headers.get("Accept", "")
            if "application/json" in accept:
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            return RedirectResponse(url="/auth/login", status_code=302)

        return await call_next(request)


# ---------------------------------------------------------------------------
# Session / DB helpers
# ---------------------------------------------------------------------------


def _db_session(request: Request):
    sf = request.app.state.session_factory
    with sf() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


# ---------------------------------------------------------------------------
# API key dependency
# ---------------------------------------------------------------------------


def _require_api_key(request: Request, db: Session = Depends(_db_session)):
    """Validate API key with dual rate-limiting.

    - Per valid-key bucket: prevents hammering with one known key.
    - Per source-IP for invalid/absent keys: prevents key brute-forcing
      across many different key values.
    """
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
    invalid_limiter: RateLimiter = request.app.state.invalid_key_limiter

    raw = get_api_key_from_request(request)
    if not raw:
        if not invalid_limiter.is_allowed(ip_hash):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    # Per-valid-key rate-limit keyed on digest prefix (constant-time-safe)
    digest = hash_api_key(raw)
    limiter: RateLimiter = request.app.state.api_limiter
    if not limiter.is_allowed(digest[:16]):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )

    key_row: ApiKey | None = (
        db.query(ApiKey)
        .filter(
            ApiKey.digest == digest,
            ApiKey.revoked_at.is_(None),
        )
        .first()
    )
    if not key_row:
        invalid_limiter.is_allowed(ip_hash)  # consume one invalid-IP token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    return key_row


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_app(
    settings=None,
    session_factory=None,
    provider=None,
) -> FastAPI:
    """Application factory.  All three arguments are optional for convenience;
    production must supply all three via environment or explicit injection."""

    if settings is None:
        from novatrack.config import Settings

        settings = Settings()  # type: ignore[call-arg]

    if session_factory is None:
        from novatrack.db import make_engine, make_session_factory

        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)

    if provider is None:
        from novatrack.providers.track17 import Track17Provider

        provider = Track17Provider(api_key=settings.track17_api_key.get_secret_value())

    # Per-app OAuth instance - never a module-level singleton
    oauth = create_oauth(settings)

    app = FastAPI(
        title="NovaTrack",
        version="0.1.3",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.provider = provider
    app.state.oauth = oauth  # test seam: app.state.oauth.create_client("oidc")
    app.state.api_limiter = RateLimiter(_DEFAULT_API_RATE_LIMIT, 60)
    app.state.invalid_key_limiter = RateLimiter(_DEFAULT_INVALID_KEY_LIMIT, 60)
    app.state.webhook_limiter = RateLimiter(_DEFAULT_WEBHOOK_RATE_LIMIT, 60)

    # ----------------------------------------------------------------
    # Middleware (last-added = outermost)
    # ----------------------------------------------------------------
    app.add_middleware(AuthEnforcementMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value(),
        session_cookie="novatrack_session",
        same_site="lax",
        https_only=True,
        max_age=8 * 3600,  # 8 hours, not 14 days
    )
    app.add_middleware(SecurityHeadersMiddleware)

    # ----------------------------------------------------------------
    # Static files (/static/auth.css is public; all others need session)
    # ----------------------------------------------------------------
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----------------------------------------------------------------
    # Templates
    # ----------------------------------------------------------------
    templates: Jinja2Templates | None = None
    if _TEMPLATES_DIR.exists():
        templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    def _render(name: str, request: Request, ctx: dict | None = None) -> HTMLResponse:
        if templates is None:
            return HTMLResponse(f"<pre>Template not found: {name}</pre>", status_code=200)
        return templates.TemplateResponse(request, name, ctx or {})

    def _csrf_ctx(request: Request) -> dict:
        token = request.session.get("csrf") or generate_csrf_token()
        request.session["csrf"] = token
        return {"csrf_token": token}

    def _full_settings_ctx(sf) -> dict:
        """Load full settings page DB context (channels, api_keys, app_cfg)."""
        with sf() as s:
            app_cfg = s.get(AppSettings, 1)
            channels = s.query(NotificationChannel).order_by(NotificationChannel.name).all()
            api_keys = s.query(ApiKey).filter(ApiKey.revoked_at.is_(None)).all()
        return {"app_cfg": app_cfg, "channels": channels, "api_keys": api_keys}

    def _telegram_available(app_settings) -> bool:
        return bool(
            app_settings.telegram_enabled
            and app_settings.telegram_bot_token is not None
            and app_settings.telegram_chat_id is not None
        )

    # ----------------------------------------------------------------
    # Health check
    # ----------------------------------------------------------------

    @app.get("/healthz", tags=["ops"])
    def healthz(request: Request) -> JSONResponse:
        try:
            with request.app.state.session_factory() as s:
                s.execute(__import__("sqlalchemy").text("SELECT 1"))
            return JSONResponse({"status": "ok"})
        except Exception:
            logger.error("Health check DB failure")
            return JSONResponse(
                {"status": "error", "detail": "Database unavailable"},
                status_code=503,
            )

    # ----------------------------------------------------------------
    # OIDC auth routes
    # ----------------------------------------------------------------

    @app.get("/auth/login")
    async def auth_login(request: Request):
        # Error redirect from callback - show login page instead of looping
        if request.query_params.get("error"):
            return _render("login.html", request, {})
        redirect_uri = settings.app_base_url + "/auth/callback"
        try:
            return await oidc_login(request, redirect_uri)
        except Exception:
            logger.warning("OIDC login initiation failed")
            return _render(
                "login.html",
                request,
                {"service_error": "Authentication service unavailable. Please try again."},
            )

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        if request.query_params.get("error"):
            return RedirectResponse("/auth/login?error=1", status_code=302)
        try:
            token = await oidc_exchange(request)
        except Exception:
            logger.warning("OIDC callback exchange failed")
            return RedirectResponse("/auth/login?error=1", status_code=302)

        userinfo = token.get("userinfo") or {}
        sub = userinfo.get("sub", "")
        if not sub:
            return RedirectResponse("/auth/login?error=1", status_code=302)

        allowed_raw: str = settings.allowed_oidc_subjects or ""
        if allowed_raw.strip():
            allowed = {s.strip() for s in allowed_raw.split(",") if s.strip()}
            if sub not in allowed:
                logger.warning("OIDC subject not in allowlist")
                return RedirectResponse("/auth/login?error=unauthorized", status_code=302)

        csrf_tok = generate_csrf_token()
        request.session.clear()
        request.session["user"] = {"sub": sub, "name": userinfo.get("name", sub)}
        request.session["csrf"] = csrf_tok
        return RedirectResponse("/", status_code=302)

    @app.post("/auth/logout")
    async def auth_logout(request: Request):
        form = await request.form()
        try:
            validate_csrf(request, form.get("_csrf"))
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        request.session.clear()
        return RedirectResponse("/auth/login", status_code=302)

    # ----------------------------------------------------------------
    # Webhook: 17TRACK
    # Official v2.2 incoming signature header name is "sign" (lowercase).
    # ----------------------------------------------------------------

    @app.post("/webhooks/17track", tags=["webhooks"])
    async def webhook_17track(request: Request) -> JSONResponse:
        # 1. Rate-limit BEFORE buffering body to prevent memory-exhaustion
        client_ip = request.client.host if request.client else "unknown"
        ip_key = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
        wh_limiter: RateLimiter = request.app.state.webhook_limiter
        if not wh_limiter.is_allowed(ip_key):
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)

        # 2. Buffer body with hard cap
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_WEBHOOK_BYTES:
                return JSONResponse({"detail": "Payload too large"}, status_code=413)
            chunks.append(chunk)
        raw_body = b"".join(chunks)

        # 3. Verify HMAC-SHA256 on exact raw bytes.
        #    Official 17TRACK v2.2 header is "sign".
        sig_header = request.headers.get("sign", "")
        if not sig_header:
            return JSONResponse({"detail": "Missing signature"}, status_code=400)
        track17_key = settings.track17_api_key.get_secret_value()
        if not verify_webhook_signature(raw_body, sig_header, track17_key):
            return JSONResponse({"detail": "Invalid signature"}, status_code=400)

        # 4. Decode UTF-8; reject malformed byte sequences before JSON parse
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse({"detail": "Malformed encoding"}, status_code=400)

        # 5. Parse JSON
        try:
            payload: Any = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "Payload must be a JSON object"}, status_code=400)

        # 6. Transactional apply
        raw_digest = hashlib.sha256(raw_body).hexdigest()
        applied = 0
        try:
            sf = request.app.state.session_factory
            with sf() as session:
                applied = tracking.apply_webhook(session, payload, raw_digest)
                session.commit()
        except ValueError:
            return JSONResponse({"detail": "Malformed webhook payload"}, status_code=400)
        except Exception:
            logger.error("Webhook processing failed - DB unavailable")
            return JSONResponse(
                {"detail": "Service temporarily unavailable. Please retry."},
                status_code=503,
            )

        return JSONResponse({"status": "ok", "applied": applied})

    # ----------------------------------------------------------------
    # API router (/api/v1/*)
    # ----------------------------------------------------------------
    from fastapi import APIRouter

    api = APIRouter(prefix="/api/v1", tags=["api"])

    @api.get("/shipments")
    def list_shipments(
        request: Request,
        page: int = 1,
        per_page: int = 50,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        page = max(1, page)
        per_page = min(max(1, per_page), 200)
        offset = (page - 1) * per_page
        sf = request.app.state.session_factory
        with sf() as s:
            total = s.query(Shipment).filter(Shipment.bucket == "active").count()
            rows = (
                s.query(Shipment)
                .filter(Shipment.bucket == "active")
                .order_by(Shipment.updated_at.desc())
                .offset(offset)
                .limit(per_page)
                .all()
            )
        return JSONResponse(
            {
                "items": [ShipmentResponse.from_orm(r).model_dump() for r in rows],
                "total": total,
                "page": page,
                "per_page": per_page,
            }
        )

    @api.post("/shipments", status_code=201)
    def create_shipment_api(
        body: CreateShipmentRequest,
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        sf = request.app.state.session_factory
        prov = request.app.state.provider
        with sf() as session:
            try:
                ship = tracking.create_shipment(
                    session,
                    prov,
                    number=body.number,
                    carrier=body.carrier,
                    name=body.name,
                    store=body.store,
                    notes=body.notes,
                    destination_country=body.destination_country,
                    destination_postal_code=body.destination_postal_code,
                    phone_number_last_4=body.phone_number_last_4,
                )
                session.commit()
                try:
                    session.refresh(ship)
                except Exception:
                    pass
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(ShipmentResponse.from_orm(ship).model_dump(), status_code=201)

    @api.get("/shipments/{shipment_id}")
    def get_shipment_api(
        shipment_id: str,
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        sf = request.app.state.session_factory
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            events = (
                s.query(TrackingEvent)
                .filter(TrackingEvent.shipment_id == shipment_id)
                .order_by(TrackingEvent.occurred_at.desc())
                .limit(200)
                .all()
            )
            events_data = [
                {
                    "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                    "description": e.description,
                    "location": e.location,
                    "provider": e.provider,
                }
                for e in events
            ]
        data = ShipmentResponse.from_orm(ship).model_dump()
        data["events"] = events_data
        return JSONResponse(data)

    @api.get("/carriers")
    def list_carriers_api(
        q: str = "",
        limit: int = 40,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        """Search the offline carrier catalogue (returns key + name)."""
        limit = min(max(1, limit), 200)
        results = search_carriers(q, limit=limit)
        return JSONResponse({"items": results, "count": len(results)})

    @api.post("/shipments/{shipment_id}/carrier", status_code=200)
    def change_carrier_api(
        shipment_id: str,
        body: ChangeCarrierRequest,
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        """Change the carrier for a registered shipment (costs one quota)."""
        sf = request.app.state.session_factory
        prov = request.app.state.provider
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            if not ship.registered:
                raise HTTPException(
                    status_code=400,
                    detail="Shipment must be registered before changing carrier",
                )
            try:
                tracking.change_carrier_shipment(s, ship, prov, body.carrier_new)
                s.commit()
                try:
                    s.refresh(ship)
                except Exception:
                    pass
            except ProviderError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(ShipmentResponse.from_orm(ship).model_dump())

    @api.post("/shipments/{shipment_id}/params", status_code=200)
    def update_shipment_params_api(
        shipment_id: str,
        body: UpdateShipmentParamsRequest,
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        """Update additional tracking parameters and trigger a registration retry.

        Sets ``destination_country``, ``destination_postal_code``, and/or
        ``phone_number_last_4`` on the shipment, then resets any pending
        register operation's ``next_attempt_at`` to now so the worker retries
        immediately on the next tick.

        Use this endpoint after a registration fails with -18010018/-18010019
        (missing or invalid additional parameter).
        """
        sf = request.app.state.session_factory
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            tracking.update_shipment_params(
                s,
                ship,
                destination_country=body.destination_country,
                destination_postal_code=body.destination_postal_code,
                phone_number_last_4=body.phone_number_last_4,
            )
            s.commit()
            try:
                s.refresh(ship)
            except Exception:
                pass
        return JSONResponse(ShipmentResponse.from_orm(ship).model_dump())

    @api.post("/shipments/{shipment_id}/archive", status_code=200)
    def archive_shipment_api(
        shipment_id: str,
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> JSONResponse:
        sf = request.app.state.session_factory
        prov = request.app.state.provider
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            tracking.archive_shipment(s, ship, prov)
            s.commit()
            return JSONResponse({"status": "archived", "id": ship.id})

    # ----------------------------------------------------------------
    # API docs (self-hosted, no external CDN)
    # ----------------------------------------------------------------

    @app.get("/api/openapi.json", include_in_schema=False)
    def api_openapi(
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ):
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    def api_docs(
        request: Request,
        _key: ApiKey = Depends(_require_api_key),
    ) -> HTMLResponse:
        return HTMLResponse(content=_SELF_HOSTED_DOCS_HTML, media_type="text/html")

    app.include_router(api)

    # Catch-all: any /api/* path not matched above still demands a valid key
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    def api_catch_all(
        path: str,
        _key: ApiKey = Depends(_require_api_key),
    ):
        raise HTTPException(status_code=404, detail="Unknown API endpoint")

    # ----------------------------------------------------------------
    # Custom OpenAPI with ApiKey security definitions
    # ----------------------------------------------------------------

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(
            {
                "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                "BearerToken": {"type": "http", "scheme": "bearer"},
            }
        )
        security_block = [{"ApiKeyHeader": []}, {"BearerToken": []}]
        for path_str, path_item in schema.get("paths", {}).items():
            if path_str.startswith("/api/v1/"):
                for method_data in path_item.values():
                    if isinstance(method_data, dict):
                        method_data["security"] = security_block
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]

    # ----------------------------------------------------------------
    # UI routes (session-authenticated; covered by middleware)
    # ----------------------------------------------------------------

    @app.get("/")
    def index(request: Request) -> HTMLResponse:
        user = get_current_user(request)
        sf = request.app.state.session_factory
        with sf() as s:
            shipments = (
                s.query(Shipment)
                .filter(Shipment.bucket == "active")
                .order_by(Shipment.updated_at.desc())
                .limit(200)
                .all()
            )
        return _render(
            "index.html",
            request,
            {
                "user": user,
                "shipments": shipments,
                **_csrf_ctx(request),
            },
        )

    @app.post("/shipments")
    async def create_shipment_ui(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        number = str(form.get("number", "")).strip()
        carrier_raw = str(form.get("carrier", "")).strip()
        name = str(form.get("name", "")).strip()[:200]
        store_val = str(form.get("store", "")).strip()[:200]
        notes_val = str(form.get("notes", "")).strip()[:2000]

        sf = request.app.state.session_factory

        # Validate destination_country: must be exactly 2 alpha chars (not silently truncated)
        dest_country_raw = str(form.get("destination_country", "")).strip().upper()
        if dest_country_raw and (len(dest_country_raw) != 2 or not dest_country_raw.isalpha()):
            with sf() as s:
                shipments = (
                    s.query(Shipment)
                    .filter(Shipment.bucket == "active")
                    .order_by(Shipment.updated_at.desc())
                    .limit(200)
                    .all()
                )
            return _render(
                "index.html",
                request,
                {
                    "user": get_current_user(request),
                    "shipments": shipments,
                    "form_error": "Destination country must be a 2-letter ISO alpha-2 code (e.g. ES, NL).",
                    **_csrf_ctx(request),
                },
            )
        dest_country: str | None = dest_country_raw or None

        dest_postal = str(form.get("destination_postal_code", "")).strip()[:20] or None

        # Validate phone_number_last_4: must be digits only (not silently truncated)
        phone_last4_raw = str(form.get("phone_number_last_4", "")).strip()
        if phone_last4_raw and not phone_last4_raw.isdigit():
            with sf() as s:
                shipments = (
                    s.query(Shipment)
                    .filter(Shipment.bucket == "active")
                    .order_by(Shipment.updated_at.desc())
                    .limit(200)
                    .all()
                )
            return _render(
                "index.html",
                request,
                {
                    "user": get_current_user(request),
                    "shipments": shipments,
                    "form_error": "Phone (last 4) must contain only digits.",
                    **_csrf_ctx(request),
                },
            )
        phone_last4: str | None = phone_last4_raw[:10] or None

        carrier: int | None = None
        if carrier_raw and carrier_raw.isdigit():
            carrier_int = int(carrier_raw)
            if 1 <= carrier_int <= _PG_INT_MAX:
                carrier = carrier_int

        if not number or not _NUMBER_RE.match(number):
            with sf() as s:
                shipments = (
                    s.query(Shipment)
                    .filter(Shipment.bucket == "active")
                    .order_by(Shipment.updated_at.desc())
                    .limit(200)
                    .all()
                )
            return _render(
                "index.html",
                request,
                {
                    "user": get_current_user(request),
                    "shipments": shipments,
                    "form_error": "Invalid tracking number - 5-50 alphanumeric/hyphen characters.",
                    **_csrf_ctx(request),
                },
            )

        prov = request.app.state.provider
        try:
            with sf() as session:
                tracking.create_shipment(
                    session,
                    prov,
                    number=number,
                    carrier=carrier,
                    name=name,
                    store=store_val,
                    notes=notes_val,
                    destination_country=dest_country,
                    destination_postal_code=dest_postal,
                    phone_number_last_4=phone_last4,
                )
                session.commit()
        except ValueError as exc:
            err_str = str(exc)
            link_shipment_id: str | None = None
            link_shipment_bucket: str | None = None

            if "already exists" in err_str:
                # Look up existing shipment for contextual handling + link
                from sqlalchemy import select as _sel

                from novatrack.models import Shipment as _Shipment
                with sf() as _s:
                    _existing = _s.execute(
                        _sel(_Shipment).where(_Shipment.number == number)
                    ).scalar_one_or_none()
                if _existing is not None:
                    if _existing.bucket == "trash" and not _existing.registered:
                        # Safe to purge (never registered → no provider record).
                        # Auto-purge then recreate transparently.
                        try:
                            with sf() as _s2:
                                _to_purge = _s2.get(_Shipment, _existing.id)
                                if _to_purge:
                                    tracking.force_purge_unregistered_trash(_s2, _to_purge)
                                    _s2.commit()
                            # Recreate with the new submitted data
                            with sf() as _s3:
                                tracking.create_shipment(
                                    _s3,
                                    prov,
                                    number=number,
                                    carrier=carrier,
                                    name=name,
                                    store=store_val,
                                    notes=notes_val,
                                    destination_country=dest_country,
                                    destination_postal_code=dest_postal,
                                    phone_number_last_4=phone_last4,
                                )
                                _s3.commit()
                            return RedirectResponse("/", status_code=303)
                        except ValueError:
                            pass  # Fall through to show error if something went wrong
                    else:
                        link_shipment_id = _existing.id
                        link_shipment_bucket = _existing.bucket
                        if _existing.bucket == "trash":
                            err_str = (
                                "This tracking number is already in Trash (registered with carrier). "
                                "Restore it to re-activate tracking (history preserved), or purge first."
                            )
                        elif _existing.bucket == "archive":
                            err_str = "This tracking number is already archived."
                        else:
                            err_str = "This tracking number is already being tracked."

            with sf() as s:
                shipments = (
                    s.query(Shipment)
                    .filter(Shipment.bucket == "active")
                    .order_by(Shipment.updated_at.desc())
                    .limit(200)
                    .all()
                )
            return _render(
                "index.html",
                request,
                {
                    "user": get_current_user(request),
                    "shipments": shipments,
                    "form_error": err_str,
                    "form_error_shipment_id": link_shipment_id,
                    "form_error_bucket": link_shipment_bucket,
                    **_csrf_ctx(request),
                },
            )

        return RedirectResponse("/", status_code=303)

    @app.get("/archive")
    def archive_view(request: Request) -> HTMLResponse:
        user = get_current_user(request)
        sf = request.app.state.session_factory
        with sf() as s:
            shipments = (
                s.query(Shipment)
                .filter(Shipment.bucket == "archive")
                .order_by(Shipment.archived_at.desc())
                .limit(200)
                .all()
            )
        return _render(
            "archive.html",
            request,
            {
                "user": user,
                "shipments": shipments,
                **_csrf_ctx(request),
            },
        )

    @app.get("/trash")
    def trash_view(request: Request) -> HTMLResponse:
        user = get_current_user(request)
        sf = request.app.state.session_factory
        with sf() as s:
            shipments = (
                s.query(Shipment)
                .filter(Shipment.bucket == "trash")
                .order_by(Shipment.trashed_at.desc())
                .limit(200)
                .all()
            )
        return _render(
            "trash.html",
            request,
            {
                "user": user,
                "shipments": shipments,
                **_csrf_ctx(request),
            },
        )

    @app.get("/shipments/{shipment_id}")
    def shipment_detail(shipment_id: str, request: Request) -> HTMLResponse:
        user = get_current_user(request)
        sf = request.app.state.session_factory
        tz_name = request.app.state.settings.tz
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            events = (
                s.query(TrackingEvent)
                .filter(TrackingEvent.shipment_id == shipment_id)
                .order_by(TrackingEvent.occurred_at.desc())
                .limit(500)
                .all()
            )
            reg_paused = tracking.is_registration_paused(s, ship)

        # Flash messages via query param
        _FLASH_MESSAGES: dict[str, str] = {
            "not_registered": (
                "Refresh skipped — shipment is not yet registered with the carrier."
            ),
            "carrier_changed": "Carrier updated successfully.",
            "carrier_error": "Carrier change failed — see the error detail below.",
            "invalid_carrier": "Invalid carrier code — please enter a positive integer.",
            "params_updated": ("Tracking parameters updated. Registration will be retried automatically."),
            "invalid_country": "Destination country must be a 2-letter ISO alpha-2 code (e.g. ES, NL).",
            "invalid_phone": "Phone (last 4) must contain only digits.",
            "restored": (
                "Shipment restored from Trash. "
                "Re-registration has been queued and will happen automatically (uses one quota slot)."
            ),
            "restored_unregistered": "Shipment restored from Trash. Registration will proceed automatically.",
            "purge_blocked": (
                "Cannot purge — provider deletion is still pending. "
                "Try again once the shipment has been fully deleted by the provider."
            ),
        }
        flash_msg = _FLASH_MESSAGES.get(request.query_params.get("msg", ""))

        return _render(
            "shipment.html",
            request,
            {
                "user": user,
                "shipment": ship,
                "events": events,
                "tz": tz,
                "flash_msg": flash_msg,
                "registration_paused": reg_paused,
                **_csrf_ctx(request),
            },
        )

    @app.post("/shipments/{shipment_id}/refresh")
    async def refresh_shipment_ui(shipment_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        prov = request.app.state.provider

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            # Refuse to refresh TBA/untrackable
            if ship.status == "untrackable":
                raise HTTPException(status_code=400, detail="Cannot refresh untrackable shipment")
            if not ship.registered:
                # HTML: redirect with informational message instead of 400
                return RedirectResponse(
                    f"/shipments/{shipment_id}?msg=not_registered",
                    status_code=303,
                )
            try:
                tracking.refresh_shipment(s, ship, prov)
                s.commit()
            except ProviderError as exc:
                ship.provider_error = str(exc)[:200]
                s.commit()

        return RedirectResponse(f"/shipments/{shipment_id}", status_code=303)

    @app.post("/shipments/{shipment_id}/params")
    async def update_shipment_params_ui(shipment_id: str, request: Request) -> Response:
        """Update additional tracking params and trigger a registration retry."""
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        # Validate destination_country: must be exactly 2 alpha chars
        dest_country_raw = str(form.get("destination_country", "")).strip().upper()
        if dest_country_raw and (len(dest_country_raw) != 2 or not dest_country_raw.isalpha()):
            return RedirectResponse(f"/shipments/{shipment_id}?msg=invalid_country", status_code=303)
        dest_country: str | None = dest_country_raw or None

        dest_postal: str | None = str(form.get("destination_postal_code", "")).strip()[:20] or None

        # Validate phone_number_last_4: digits only
        phone_last4_raw = str(form.get("phone_number_last_4", "")).strip()
        if phone_last4_raw and not phone_last4_raw.isdigit():
            return RedirectResponse(f"/shipments/{shipment_id}?msg=invalid_phone", status_code=303)
        phone_last4: str | None = phone_last4_raw[:10] or None

        sf = request.app.state.session_factory
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            tracking.update_shipment_params(
                s,
                ship,
                destination_country=dest_country,
                destination_postal_code=dest_postal,
                phone_number_last_4=phone_last4,
            )
            s.commit()

        return RedirectResponse(f"/shipments/{shipment_id}?msg=params_updated", status_code=303)

    @app.post("/shipments/{shipment_id}/carrier")
    async def change_carrier_ui(shipment_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        carrier_raw = str(form.get("carrier", "")).strip()

        carrier_int: int | None = None
        if carrier_raw and carrier_raw.isdigit():
            v = int(carrier_raw)
            if 1 <= v <= _PG_INT_MAX:
                carrier_int = v

        if carrier_int is None:
            return RedirectResponse(
                f"/shipments/{shipment_id}?msg=invalid_carrier",
                status_code=303,
            )

        sf = request.app.state.session_factory
        prov = request.app.state.provider
        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            if not ship.registered:
                return RedirectResponse(
                    f"/shipments/{shipment_id}?msg=not_registered",
                    status_code=303,
                )
            try:
                tracking.change_carrier_shipment(s, ship, prov, carrier_int)
                s.commit()
            except ProviderError as exc:
                ship.provider_error = str(exc)[:200]
                s.commit()
                return RedirectResponse(
                    f"/shipments/{shipment_id}?msg=carrier_error",
                    status_code=303,
                )
        return RedirectResponse(f"/shipments/{shipment_id}?msg=carrier_changed", status_code=303)

    @app.post("/shipments/{shipment_id}/archive")
    async def archive_shipment_ui(shipment_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        prov = request.app.state.provider

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            tracking.archive_shipment(s, ship, prov)
            s.commit()

        return RedirectResponse("/", status_code=303)

    @app.post("/shipments/{shipment_id}/trash")
    async def trash_shipment_ui(shipment_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        prov = request.app.state.provider

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            tracking.trash_shipment(s, ship, prov)
            s.commit()

        return RedirectResponse("/", status_code=303)

    @app.post("/shipments/{shipment_id}/restore")
    async def restore_shipment_ui(shipment_id: str, request: Request) -> Response:
        """Restore a trashed shipment back to active bucket."""
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        prov = request.app.state.provider

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            try:
                reregistered = tracking.restore_shipment(s, ship, prov)
                s.commit()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        msg = "restored" if reregistered else "restored_unregistered"
        return RedirectResponse(f"/shipments/{shipment_id}?msg={msg}", status_code=303)

    @app.post("/shipments/{shipment_id}/purge")
    async def purge_shipment_ui(shipment_id: str, request: Request) -> Response:
        """Hard-delete a trashed shipment (only when no pending provider ops)."""
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            try:
                tracking.purge_shipment(s, ship)
                s.commit()
            except ValueError:
                # Pending ops — redirect back with informational message
                return RedirectResponse(
                    f"/shipments/{shipment_id}?msg=purge_blocked",
                    status_code=303,
                )

        return RedirectResponse("/trash", status_code=303)

    @app.post("/shipments/{shipment_id}/deliver")
    async def deliver_shipment_ui(shipment_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory

        with sf() as s:
            ship = s.get(Shipment, shipment_id)
            if not ship:
                raise HTTPException(status_code=404, detail="Shipment not found")
            if ship.status != "untrackable" or ship.bucket != "active":
                raise HTTPException(
                    status_code=400,
                    detail="Manual delivery only allowed for active untrackable shipments",
                )
            tracking.manual_deliver(s, ship)
            s.commit()

        return RedirectResponse(f"/shipments/{shipment_id}", status_code=303)

    # ----------------------------------------------------------------
    # Settings
    # ----------------------------------------------------------------

    @app.get("/settings")
    def settings_view(request: Request) -> HTMLResponse:
        user = get_current_user(request)
        app_settings = request.app.state.settings
        sf = request.app.state.session_factory

        with sf() as s:
            app_cfg = s.get(AppSettings, 1)
            channels = s.query(NotificationChannel).order_by(NotificationChannel.name).all()
            api_keys = s.query(ApiKey).filter(ApiKey.revoked_at.is_(None)).all()

        return _render(
            "settings.html",
            request,
            {
                "user": user,
                "app_cfg": app_cfg,
                "channels": channels,
                "api_keys": api_keys,
                "telegram_available": _telegram_available(app_settings),
                **_csrf_ctx(request),
            },
        )

    @app.post("/settings/retention")
    async def settings_retention(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        try:
            archive_days = int(form.get("archive_days", 7))
            purge_days = int(form.get("purge_days", 30))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Days must be integers")

        if not (1 <= archive_days <= 3650):
            raise HTTPException(status_code=400, detail="archive_days must be between 1 and 3650")
        if not (1 <= purge_days <= 3650):
            raise HTTPException(status_code=400, detail="purge_days must be between 1 and 3650")

        sf = request.app.state.session_factory
        with sf() as s:
            cfg = s.get(AppSettings, 1)
            if cfg is None:
                cfg = AppSettings(id=1)
                s.add(cfg)
            cfg.archive_days = archive_days
            cfg.purge_days = purge_days
            s.commit()

        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/quota")
    async def settings_quota(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        sf = request.app.state.session_factory
        prov = request.app.state.provider
        app_settings = request.app.state.settings

        try:
            with sf() as s:
                notifications.check_quota(s, prov)
                s.commit()
        except Exception:
            logger.error("Quota check failed")
            return _render(
                "settings.html",
                request,
                {
                    "user": get_current_user(request),
                    "quota_error": "Quota check failed - check provider configuration.",
                    "telegram_available": _telegram_available(app_settings),
                    **_full_settings_ctx(sf),
                    **_csrf_ctx(request),
                },
            )

        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/api-keys")
    async def create_api_key_route(request: Request) -> HTMLResponse:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        key_name = str(form.get("name", "")).strip()[:100] or "API Key"
        raw, digest = generate_api_key()
        prefix = raw[:8]
        sf = request.app.state.session_factory
        with sf() as s:
            k = ApiKey(name=key_name, digest=digest, prefix=prefix)
            s.add(k)
            s.commit()
        app_settings = request.app.state.settings
        # Return full settings context so no sections appear blank
        return _render(
            "settings.html",
            request,
            {
                "user": get_current_user(request),
                "new_api_key": raw,
                "new_api_key_name": key_name,
                "telegram_available": _telegram_available(app_settings),
                **_full_settings_ctx(sf),
                **_csrf_ctx(request),
            },
        )

    @app.post("/settings/api-keys/{key_id}/revoke")
    async def revoke_api_key_route(key_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        with sf() as s:
            k = s.get(ApiKey, key_id)
            if k and k.revoked_at is None:
                k.revoked_at = datetime.now(UTC)
            s.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/channels")
    async def create_channel_route(request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        name = str(form.get("name", "")).strip()[:100]
        kind = str(form.get("kind", "webhook")).strip()
        url = str(form.get("url", "")).strip()
        secret_env = str(form.get("secret_env", "")).strip()[:100]
        events_raw = form.getlist("events") if hasattr(form, "getlist") else []
        events = [e for e in events_raw if e in _VALID_EVENTS]

        if not name:
            raise HTTPException(status_code=400, detail="Channel name must not be empty")
        if kind not in _VALID_CHANNEL_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"kind must be one of: {', '.join(sorted(_VALID_CHANNEL_KINDS))}",
            )
        if not events:
            raise HTTPException(status_code=400, detail="At least one event must be selected")

        if kind == "telegram":
            if url:
                raise HTTPException(status_code=400, detail="Telegram channels do not use webhook URLs")
            if secret_env:
                raise HTTPException(
                    status_code=400,
                    detail="Telegram channels do not use webhook secrets",
                )
            app_settings = request.app.state.settings
            if not _telegram_available(app_settings):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Telegram not configured - set TELEGRAM_ENABLED, "
                        "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
                    ),
                )
        else:  # webhook
            if not url:
                raise HTTPException(status_code=400, detail="Webhook channels require a URL")
            if secret_env and not _SECRET_ENV_RE.match(secret_env):
                raise HTTPException(
                    status_code=400,
                    detail="secret_env must match NOVATRACK_WEBHOOK_SECRET_[A-Z0-9_]+",
                )
            try:
                url = notifications.validate_webhook_url(url)
            except (ValueError, Exception) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        sf = request.app.state.session_factory
        with sf() as s:
            ch = NotificationChannel(
                name=name,
                kind=kind,
                url=url or None,
                secret_env=secret_env or None,
                events=events,
            )
            s.add(ch)
            s.commit()

        return RedirectResponse("/settings", status_code=303)

    @app.get("/settings/channels/{ch_id}/edit")
    def edit_channel_form(ch_id: str, request: Request) -> HTMLResponse:
        user = get_current_user(request)
        sf = request.app.state.session_factory
        with sf() as s:
            ch = s.get(NotificationChannel, ch_id)
        if not ch:
            raise HTTPException(status_code=404, detail="Channel not found")
        return _render(
            "settings_channel_edit.html",
            request,
            {
                "user": user,
                "channel": ch,
                "valid_events": sorted(_VALID_EVENTS),
                **_csrf_ctx(request),
            },
        )

    @app.post("/settings/channels/{ch_id}/edit")
    async def edit_channel_save(ch_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))

        url = str(form.get("url", "")).strip()
        secret_env = str(form.get("secret_env", "")).strip()[:100]
        events_raw = form.getlist("events") if hasattr(form, "getlist") else []
        events = [e for e in events_raw if e in _VALID_EVENTS]

        if not events:
            raise HTTPException(status_code=400, detail="At least one event must be selected")

        sf = request.app.state.session_factory
        with sf() as s:
            ch = s.get(NotificationChannel, ch_id)
            if not ch:
                raise HTTPException(status_code=404, detail="Channel not found")

            if ch.kind == "webhook":
                if not url:
                    raise HTTPException(status_code=400, detail="Webhook channels require a URL")
                if secret_env and not _SECRET_ENV_RE.match(secret_env):
                    raise HTTPException(
                        status_code=400,
                        detail="secret_env must match NOVATRACK_WEBHOOK_SECRET_[A-Z0-9_]+",
                    )
                try:
                    url = notifications.validate_webhook_url(url)
                except (ValueError, Exception) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                ch.url = url
                ch.secret_env = secret_env or None

            ch.events = events
            s.commit()

        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/channels/{ch_id}/toggle")
    async def toggle_channel_route(ch_id: str, request: Request) -> Response:
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        with sf() as s:
            ch = s.get(NotificationChannel, ch_id)
            if ch:
                ch.enabled = not ch.enabled
            s.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/channels/{ch_id}/delete")
    async def delete_channel_route(ch_id: str, request: Request) -> Response:
        """Soft-disable channel to preserve Delivery audit history.

        Hard delete would cascade-delete Delivery/DeliveryAttempt rows.
        Soft-disable (enabled=False) keeps the audit log intact.
        """
        form = await request.form()
        validate_csrf(request, form.get("_csrf"))
        sf = request.app.state.session_factory
        with sf() as s:
            ch = s.get(NotificationChannel, ch_id)
            if ch:
                ch.enabled = False
            s.commit()
        return RedirectResponse("/settings", status_code=303)

    return app


# ---------------------------------------------------------------------------
# Self-hosted minimal API docs HTML (no external CDN, no inline styles)
# ---------------------------------------------------------------------------
_SELF_HOSTED_DOCS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NovaTrack API Docs</title>

</head>
<body>
<div class="main">
<h1>NovaTrack API</h1>
<p>Authenticate with <code>X-API-Key: &lt;your-key&gt;</code> or
<code>Authorization: Bearer &lt;your-key&gt;</code> on every request.</p>
<p><a href="/api/openapi.json">OpenAPI schema (JSON)</a></p>
<p>Endpoints:
  <code>GET /api/v1/shipments</code> &middot;
  <code>POST /api/v1/shipments</code> &middot;
  <code>GET /api/v1/shipments/{id}</code> &middot;
  <code>POST /api/v1/shipments/{id}/params</code> &middot;
  <code>POST /api/v1/shipments/{id}/archive</code> &middot;
  <code>POST /api/v1/shipments/{id}/carrier</code> &middot;
  <code>GET /api/v1/carriers</code>
</p>
</div>
</body>
</html>"""
