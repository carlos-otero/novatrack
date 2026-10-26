"""OIDC authentication helpers: session, CSRF, per-app OAuth factory.

Per-app OAuth instance
----------------------
``create_oauth(settings)`` returns a *fresh* Authlib OAuth object registered
with the OIDC provider.  The instance is stored in ``app.state.oauth`` so
that tests can inject a MockTransport via::

    app.state.oauth.create_client("oidc").client_kwargs["transport"] = t

``oidc_login`` and ``oidc_exchange`` always read from
``request.app.state.oauth`` — never from a module-level singleton — so
different test fixtures each get isolated, non-stale config.
"""

from __future__ import annotations

import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

_OIDC_NAME = "oidc"


def create_oauth(settings) -> OAuth:
    """Create and register a fresh per-app OIDC client.

    Called once per ``create_app()`` invocation; the result is stored in
    ``app.state.oauth``.  Never shares state across app instances.
    """
    oauth = OAuth()
    oauth.register(
        name=_OIDC_NAME,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        server_metadata_url=settings.oidc_issuer + "/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile",
            "code_challenge_method": "S256",
        },
    )
    return oauth


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> dict | None:
    """Return the session user dict {sub, name} or None if not authenticated."""
    user = request.session.get("user")
    if user and isinstance(user, dict) and user.get("sub"):
        return user
    return None


def generate_csrf_token() -> str:
    return secrets.token_hex(32)


def validate_csrf(request: Request, form_token: str | None) -> None:
    """Raise HTTP 403 if the CSRF token from the form does not match the session."""
    session_csrf = request.session.get("csrf")
    if not session_csrf or not form_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )
    if not secrets.compare_digest(str(session_csrf), str(form_token)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )


# ---------------------------------------------------------------------------
# Login / callback helpers
# ---------------------------------------------------------------------------


async def oidc_login(request: Request, redirect_uri: str) -> RedirectResponse:
    """Initiate PKCE+S256 OIDC authorisation redirect via the per-app OAuth."""
    oauth: OAuth = request.app.state.oauth
    return await oauth.oidc.authorize_redirect(request, redirect_uri)  # type: ignore[attr-defined]


async def oidc_exchange(request: Request) -> dict:
    """Exchange authorisation code; returns token dict with 'userinfo' key.

    Uses ``request.app.state.oauth`` so the test seam
    ``app.state.oauth.create_client('oidc').client_kwargs['transport']``
    intercepts both discovery and token-endpoint traffic.

    Raises an exception (caught by the route handler) on any failure.
    """
    oauth: OAuth = request.app.state.oauth
    return await oauth.oidc.authorize_access_token(request)  # type: ignore[attr-defined]
