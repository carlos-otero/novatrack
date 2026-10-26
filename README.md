# NovaTrack

Self-hosted parcel tracking for your own deliveries. A small, mobile-friendly web app with OpenID Connect login, a REST API for agents and automation, and signed outbound notifications.

**Status:** v0.1.1. Single-household, not multi-tenant; see [SECURITY.md](SECURITY.md) for limits.

- **Private by design:** no local passwords, no registration, OIDC-only browser access.
- **Bring your tracking provider:** a replaceable adapter, initially [17TRACK API v2.2](https://api.17track.net/en/doc).
- **One registration, webhook updates:** durable background operations with visible errors and retries.
- **Your own infrastructure:** Python 3.13, FastAPI/Jinja, SQLAlchemy, Alembic, PostgreSQL 16. No SPA, CDN or analytics.
- **Notifications:** generic HTTPS webhooks with HMAC signatures, a delivery audit and exponential retries. Telegram is optional and off by default.
- **Automation:** multiple revocable, hashed API keys; authenticated OpenAPI schema.
- **Portable containers:** ARM64 and AMD64; non-root, read-only filesystem supported.

## Screenshots

### Sign in

![NovaTrack sign-in screen with the SSO button](docs/screenshots/login.png)

### Shipments

![Active shipments page showing the empty state](docs/screenshots/shipments.png)

### Add a shipment

![Track New Shipment dialog with optional carrier, label, store and notes](docs/screenshots/add-shipment.png)

The shipment form image is cropped to the dialog; it does not show its position
within the page. Desktop dialogs are centered; on mobile they open at the bottom.

## What it tracks

Add a tracking number, optional numeric carrier code, a friendly name, store and notes. 17TRACK usually detects the carrier. Correos, SEUR, DHL, Ecoscooting, GLS, Nacex and Cainiao may be supported; consult the [current carrier directory](https://res.17track.net/asset/carrier/info/apicarrier.all.json) rather than guessing numeric codes. Coverage and extra carrier requirements are controlled by the provider.

Active, Archive and Trash views keep the list manageable. Shipment detail retains a deduplicated event history and displays an estimated delivery range when supplied. TBA-prefixed Amazon Logistics numbers are local-only, `untrackable`, consume no provider quota and can be manually marked delivered.

Normalized statuses: `pending`, `info_received`, `in_transit`, `customs`, `out_for_delivery`, `delivered`, `exception`, `expired`, `untrackable`. Provider `AvailableForPickup` maps to `out_for_delivery`; the original event description retains the pickup instruction. Customs-released events map back to `in_transit`. Delivered is terminal, including manual delivery, so a delayed webhook cannot undo it.

## Prerequisites

- A PostgreSQL 16 database and least-privilege application user.
- An OIDC provider such as Authentik, Keycloak or another standards-compliant issuer.
- A 17TRACK API account/key and configured webhook.
- HTTPS reverse proxy and a canonical public application URL.

This is a **single-household application, not a multi-tenant service**: every authorized OIDC user sees the same shipments, settings and API keys. Restrict the app at the identity provider and optionally set `ALLOWED_OIDC_SUBJECTS`.

## Quick start (standalone Docker Compose)

```sh
cp .env.example .env
# Fill credentials, canonical HTTPS URLs, proxy trust and POSTGRES_PASSWORD.
# DATABASE_URL must reference the postgres service and match that password.
docker compose -f docker-compose.example.yml up --build -d
```

The example builds from source. It starts PostgreSQL, runs the one-shot `migrate` service, then web and worker. Publish **only web port 8000** behind your HTTPS reverse proxy. The default host binding is `127.0.0.1:8000`; do not expose the app or database directly to the internet. Secure session cookies require HTTPS even in development.

Default image commands:

```sh
docker run --rm --env-file .env IMAGE migrate # alembic upgrade head
docker run --env-file .env IMAGE web
docker run --env-file .env IMAGE worker
```

Do not run several migration jobs simultaneously. Start with **one web process and one worker**. The worker must stay running for registrations, provider stop/delete, retention, quota checks and outgoing notifications. No scheduler is hidden inside the web process.

### Shared database / Dokploy

Use `docker-compose.dokploy.yml` as a starting point. Set `NOVATRACK_IMAGE` to the exact versioned registry image. Configure the runtime `.env` through Dokploy Environment Settings; it is not a file in the public repository. The example attaches all services to the external `dokploy-network`, uses the existing database and exposes web port 8000 to Traefik without publishing host ports. The `migrate` container must exit successfully before web/worker start.

Set the app's domain at the proxy. Infra owns DNS, TLS, the database/user, IdP application and deployment; this repository does not provision them.

## Environment variables

All runtime secrets come from environment variables. The application does not automatically load a local `.env`; Compose injects it. Never commit a populated file.

| Variable | Required | Purpose / default |
|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE`; URL-encode password characters |
| `OIDC_ISSUER` | Yes | Exact HTTPS issuer from discovery; no query/fragment |
| `OIDC_CLIENT_ID` | Yes | Confidential OIDC client |
| `OIDC_CLIENT_SECRET` | Yes | OIDC client secret |
| `APP_BASE_URL` | Yes | Canonical HTTPS app origin, e.g. `https://tracking.example.com` |
| `SECRET_KEY` | Yes | At least 32 random bytes for signed browser sessions |
| `TRACK17_API_KEY` | Yes | 17TRACK API key; also verifies incoming webhook signatures |
| `TZ` | No | Display timezone; `UTC` (e.g. `Atlantic/Canary`) |
| `LOG_LEVEL` | No | `INFO` |
| `TRUSTED_PROXY_IPS` | No | Uvicorn trusted immediate proxy addresses/CIDRs; `127.0.0.1` |
| `ALLOWED_OIDC_SUBJECTS` | No | Comma-separated permitted `sub` claim values; otherwise IdP policy controls access |
| `WORKER_POLL_SECONDS` | No | Worker tick interval, `5` (1–300) |
| `NOVATRACK_WEBHOOK_SECRET_<NAME>` | For each webhook channel | Receiver-specific HMAC secret; UI stores the **variable name**, never the value |
| `TELEGRAM_ENABLED` | No | `false`; an enabled UI channel and this explicit switch are both necessary |
| `TELEGRAM_BOT_TOKEN` | Telegram only | Bot credential |
| `TELEGRAM_CHAT_ID` | Telegram only | Destination chat |
| `TELEGRAM_THREAD_ID` | No | Optional topic/thread ID; omit rather than set an empty string |
| `POSTGRES_PASSWORD` | Standalone Compose only | Database bootstrap password, must match `DATABASE_URL` |
| `NOVATRACK_IMAGE` | Dokploy Compose only | Exact registry image/tag, preferably pinned by digest |

Generate a session secret locally with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`. Rotating `SECRET_KEY` invalidates existing browser sessions. Runtime application secrets must not be used as outbound webhook secrets.

### OIDC configuration

Create a **confidential** OIDC client with authorization-code flow, PKCE S256 and scopes `openid profile email`. Register this exact redirect URI:

```text
https://tracking.example.com/auth/callback
```

Replace the origin with `APP_BASE_URL`. Discovery is read from `OIDC_ISSUER/.well-known/openid-configuration`. NovaTrack validates state, nonce, token signature and issuer. `/auth/login` and `/auth/callback` are the necessary unauthenticated login bootstrap endpoints; they never expose application data. All other browser routes, including static assets and unknown paths, require the signed session. Logout is CSRF-protected POST.

### Proxy / Cloudflare

Uvicorn only honors forwarded client and scheme headers from `TRUSTED_PROXY_IPS`. Set this to the real immediate Traefik/reverse-proxy IP or isolated subnet, not `*` on a reachable network. Do not copy arbitrary incoming `X-Forwarded-For` into application decisions. Canonical OIDC callback URLs come from `APP_BASE_URL`, not the incoming Host header. Configure Cloudflare not to challenge the signed 17TRACK webhook endpoint and not to cache API/session responses.

## Tracking and incoming webhook

1. Configure 17TRACK to deliver **API v2.2** webhook messages to:
   `https://tracking.example.com/webhooks/17track` (HTTP POST).
2. On creation the app commits a shipment and a durable registration operation together. The worker calls `/track/v2.2/register`, stores the detected carrier and retries transient failures. UI/API creation does not falsely claim immediate provider success.
3. 17TRACK pushes updates. NovaTrack checks the `sign` header against **SHA256 of the exact raw HTTP body + `/` + `TRACK17_API_KEY`**, in constant time, before parsing. It does not use the legacy v1 body-sign algorithm or reserialize the JSON.
4. Webhook receipt, event history, state transition and notification outbox commit atomically. Retries and identical transitions do not create new notices. Out-of-order snapshots cannot move the shipment backward by event timestamp, and delivery is terminal.
5. Manual refresh calls `/gettrackinfo` using the same update path. Archive queues `/stoptrack`; Trash queues `/deletetrack`. Worker retention archives delivered shipments and purges old trash only after provider cleanup succeeds.

Registration consumes quota. Deleting and re-registering may consume another shipment. **The former 100-free-shipments/month policy was withdrawn effective January 7, 2026**, according to the current [official documentation](https://api.17track.net/en/doc). NovaTrack reads `/getquota`, shows the account's actual remaining allowance and sends an 80%-used alert; it does not assume any free renewal. Quota is checked automatically and can be refreshed from Settings. Tests never spend real registration quota.

## Outgoing notifications

In Settings add an HTTPS webhook channel, select `status_changed`, `delivered`, `exception` and/or `quota_low`, and enter an environment variable name such as `NOVATRACK_WEBHOOK_SECRET_HOME`. Configure its random value in the **worker environment** and share it with the receiver. Selecting overlapping events still produces one delivery per transition/channel.

Contract (historical `X-Parcels-*` names retained for integration compatibility):

```text
X-Parcels-Timestamp: <Unix seconds>
X-Parcels-Signature: sha256=<hex HMAC-SHA256>
X-Parcels-Event-Id: <stable notification UUID>
Content-Type: application/json
```

Signed bytes are `timestamp + "." + exact_request_body`. The receiver should check HMAC in constant time, accept only a short timestamp window and durably deduplicate the event ID **before** applying effects. Retries use the same event ID. The application guarantees one queued notice per transition, **not impossible network exactly-once delivery**: a crash after receiver acceptance but before sender commit can cause a retry. Delivery attempts and safe status/error categories are retained in Settings; failures retry with bounded exponential backoff.

Only public HTTPS destinations are allowed. Credentials, secret-bearing query strings, redirects and private/local/link-local DNS targets are rejected. Delivery resolves and validates public addresses, then pins the connection address while preserving TLS hostname validation to prevent DNS rebinding. Internal destinations require a public HTTPS relay; there is intentionally no unsafe “allow all private networks” switch.

Telegram is implemented as a separate channel but disabled by default. Set `TELEGRAM_ENABLED=true` plus bot/chat environment variables and explicitly enable its UI channel to opt in. No bot token is stored in the database.

## REST API

Create a key in authenticated Settings. Its raw value is displayed **once**; the database stores only a SHA256 hash and non-secret prefix. Name keys by consumer, issue several and revoke independently.

Use `Authorization: Bearer <key>` or `X-API-Key: <key>`. Browser sessions alone do not authenticate API calls.

```text
POST /api/v1/shipments           create (number, carrier?, name?, store?, notes?)
GET  /api/v1/shipments           list active shipments, paginated
GET  /api/v1/shipments/{id}      shipment detail
POST /api/v1/shipments/{id}/archive
GET  /api/openapi.json          OpenAPI schema (API key required)
GET  /api/docs                  self-hosted API reference (API key required)
```

`/healthz` is public readiness and checks the database; it returns 503 when unavailable. API and incoming webhook have basic in-process rate limits; keep one web process and add reverse-proxy rate limiting for multi-replica deployments. Body sizes are bounded. Security headers, secure HttpOnly SameSite cookies and CSRF-protected browser forms are applied centrally. Application logs deliberately omit HTTP access URLs to avoid leaking OIDC callback codes or Telegram credential URLs.

## Development and tests

```sh
uv sync --frozen --python 3.13
uv run pytest -q
uv run ruff check .
# Migrations, against your disposable database:
DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head
DATABASE_URL=postgresql+psycopg://... uv run alembic check
# Start with explicit runtime environment and an HTTPS reverse proxy:
uv run uvicorn novatrack.app:create_app --factory --no-access-log
uv run python -m novatrack.worker
```

Unit tests use isolated SQLite and simulated HTTP responses, never real tracking numbers. PostgreSQL tests/migration gates use a disposable PostgreSQL 16 instance. SQLite is not a supported production deployment. Changes to the schema require an Alembic migration, not `create_all` in application startup.

## Backups, upgrades and limitations

Back up PostgreSQL plus the external secret manager/environment configuration. No persistent application volume is required. Before upgrade, take a DB backup, review `CHANGELOG.md`, pin the new image, run one migration job and restart both web and worker. A schema downgrade may delete data; restoring the backup is the safe rollback path.

17TRACK may require additional carrier-specific parameters or have missing/unavailable delivery estimates; these are provider limitations, not invented progress or fake ETA values. This initial version has no email/order import, multi-tenancy, local accounts or live carrier scraping. External tracking and notification services receive the shipment data required for their operation.

See [SECURITY.md](SECURITY.md) for threat boundaries and reporting. Contributions are welcome under [MIT](LICENSE).
