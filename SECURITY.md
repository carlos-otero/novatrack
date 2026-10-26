# Security

## Scope and support

NovaTrack v0.1.x is an initial self-hosted release, not a multi-tenant SaaS. Every OIDC-authorized user has access to the same household data and administrative settings. Restrict membership in your IdP and optionally `ALLOWED_OIDC_SUBJECTS`. API keys grant full supported shipment API access, not browser settings access; they are not read-only/scoped keys.

Do not put credentials, tracking numbers, customer information, callback authorization codes or raw provider responses into public issues. Report vulnerabilities privately to the repository owner through GitHub's private vulnerability reporting when enabled; otherwise use the owner's private contact and share a minimal, redacted reproducer.

## Trust boundaries

- The internet can access readiness, signed incoming 17TRACK webhooks and API-key-authenticated API endpoints. OIDC login and callback are necessary unauthenticated bootstrap routes.
- Browser sessions use Secure, HttpOnly, SameSite=Lax cookies. Forms, including logout and settings, require CSRF tokens. Sessions contain identity and CSRF state, never access/refresh tokens or newly generated API keys.
- The exact webhook body is authenticated before parsing with the documented v2.2 SHA256(body + '/' + API key) signature. Duplicate receipts and state transitions are handled transactionally; source signatures do not make unsigned timestamps trustworthy.
- Database, IdP, 17TRACK, webhook and Telegram credentials come from runtime environment configuration. Notification settings reference restricted `NOVATRACK_WEBHOOK_SECRET_*` variable names instead of storing credentials. API keys are shown once and stored as hashes; protect the database and backups as sensitive data nonetheless.
- Notification endpoints must be public HTTPS, without embedded credentials or query secrets. DNS answers are validated and the connection address is pinned; TLS still validates the original hostname. Redirects and private/link-local destinations are refused. Restrict worker egress at the network layer as defense in depth.
- Telegram is disabled by default and uses a separate explicit runtime switch.
- SQLAlchemy binds SQL values. Jinja escapes user/provider text. Static assets are self-hosted and CSP disallows untrusted scripts.
- Proxy forwarding headers are trusted only from explicitly configured proxy peers. Do not expose the service directly and trust `*`; isolate the proxy network. Per-process rate limiting is basic abuse control, not distributed DDoS protection.
- Provider registration and notification delivery are durable, retryable external operations. Network exactly-once delivery cannot be guaranteed; receivers must deduplicate stable event IDs. A compromised webhook recipient can see the payloads routed to it.

## Deployment requirements

1. Use TLS; do not disable secure cookies to work around an HTTP setup.
2. Keep PostgreSQL private and use a dedicated DB/user. Back up DB and secret configuration.
3. Run one web process and one worker initially; migrations are a single explicit job. Multiple replicas require shared rate limits and operational testing.
4. Use immutable versioned images, preferably digest-pinned, and update pinned dependencies after tests and release review.
5. Application containers run UID/GID 10001; use read-only filesystems, dropped capabilities and no-new-privileges. Never mount the Docker socket.
6. Do not expose raw callback URLs in access logs. The supplied web command disables Uvicorn access logging; configure equivalent redaction at your reverse proxy.
7. No real service credentials are required for unit tests. Never use actual registration endpoints in CI fixtures.

## Known limitations

No tenant isolation, local-password fallback, per-user shipment ownership, granular API-key scopes, email/order import or provider-specific extra tracking parameter editor in v0.1.0. Session-secret rotation logs users out. Revoking the OIDC grant alone does not instantly invalidate a previously issued signed session; use a short session lifetime and rotate the signing secret for emergency global revocation. A trusted app administrator can route shipment notifications to a public destination, by design.
