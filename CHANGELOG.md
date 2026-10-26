# Changelog

All notable changes are documented here. Versions use SemVer.

## 0.1.1 — 2026-10-26

### Fixed
- Center the Track New Shipment dialog on desktop despite the global margin reset.
- Keep the mobile dialog anchored to the bottom by clearing the browser's default top inset.
- Add regression checks for dialog styles and shared modal classes.

### Documentation
- Add sign-in, shipment list and cropped shipment form screenshots to the README.

### Upgrade notes
- No database schema changes or new Alembic migrations. The schema remains at revision `0001`.

## 0.1.0 — 2026-10-26

### Added
- Mobile-friendly server-rendered shipment lists and event detail; Active, Archive and Trash.
- Replaceable tracking adapter with 17TRACK API v2.2, signed idempotent webhooks and durable provider operations.
- Normalized shipment state transitions, retained/deduplicated events and optional ETA.
- Local-only TBA Amazon Logistics numbers and manual delivery.
- Transactional notification outbox, HMAC-SHA256 webhook delivery, bounded retries and delivery audit.
- Optional Telegram channel, disabled by default; quota monitoring against actual provider allowance.
- Configurable delivered retention and trash purge.
- OIDC-only web authentication; API keys stored hashed, with independent revocation and authenticated OpenAPI.
- CSRF, secure cookies, security headers, rate limits, bounded webhook requests and restricted proxy trust.
- PostgreSQL/Alembic schema, locked Python dependencies and version/digest-pinned non-root multi-architecture container definition.
- Standalone and shared-Dokploy Compose examples; MIT license and security guidance.

### Notes
- Initial household application, not multi-tenant.
- Old 100-free-trackings/month assumptions are intentionally not encoded; current provider quota is authoritative.
- Container image: `linux/amd64` and `linux/arm64`, non-root, pinned base and dependencies.
