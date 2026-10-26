# Changelog

All notable changes are documented here. Versions use SemVer.

## 0.1.2 — 2026-10-26

### Added
- **Carrier catalogue search:** offline copy of the official 17TRACK carrier list (`apicarrier.all.json`, 3 558 entries) bundled as a static JSON file; searchable autocomplete in the Add Shipment and Change Carrier dialogs, with manual numeric code entry as a fallback.
- **Change carrier:** new `POST /shipments/{id}/carrier` UI route and `POST /api/v1/shipments/{id}/carrier` API endpoint calling the 17TRACK v2.2 `changecarrier` method. All eight rejection codes (-18019801…-18019808) produce actionable error messages. A cost-warning banner ("⚠ Changing the carrier will consume one tracking quota slot") appears in the UI dialog. `change_carrier_shipment` now guards against unregistered shipments at the business-logic layer (raises `ValueError`).
- **Optional additional tracking parameters:** `destination_country` (ISO alpha-2), `destination_postal_code`, and `phone_number_last_4` stored per shipment and sent as a v2.2 `param` string on registration. Supported through the Add Shipment form, the API (`POST /api/v1/shipments`), and the `additional_parameters.json` catalogue.
- **Update-params endpoint:** new `POST /shipments/{id}/params` UI route and `POST /api/v1/shipments/{id}/params` API endpoint. Updates the three stored param fields on an existing shipment and **resets any paused register operation's `next_attempt_at` to now**, triggering an immediate retry on the next worker tick. This is the correct path to resume a registration that was paused for missing fields.
- **Missing-param pause (not a retry loop):** when the provider rejects registration with -18010018/-18010019/-18010020 (missing or invalid additional parameter), the register operation is **paused indefinitely** (`next_attempt_at` is set to the sentinel year 9999) rather than retried on every worker tick or after a fixed delay. The worker will never auto-resume; the user must supply the missing fields via the update-params endpoint to trigger an immediate retry. Normal transient errors (network failures, etc.) continue to use exponential backoff as before.
- **Unsupported required carrier params blocked explicitly:** carriers that require additional parameters that NovaTrack cannot currently supply (e.g. `phone_number`, `cpf_or_cnpj`, `company_id`, `ship_date` — 8 carriers in the bundled catalogue) are detected before calling the provider. The register operation is marked `done=True` with an actionable error message (`provider_error`) instead of silently sending empty values and looping on -18010018 errors. New function `novatrack.carriers.unsupported_required_params(carrier)` performs this check.
- **Server-side UI validation:** `destination_country` and `phone_number_last_4` are now properly validated in all HTML form routes (Add Shipment, Update Params). Invalid values produce a form error instead of being silently truncated to pass the field length limit.
- **Offline additional-parameters catalogue:** `additional_parameters.json` bundled under `novatrack/data/`; `novatrack/carriers.py` exposes `get_carrier`, `search_carriers`, `get_additional_params`, `build_param_string`, `missing_required_params`, `unsupported_required_params`.
- **Unregistered-shipment HTML redirect:** `POST /shipments/{id}/refresh` for an unregistered shipment now redirects with `?msg=not_registered` and shows an informational message instead of returning HTTP 400. The Refresh button is hidden in the template when the shipment is not yet registered. API clients still receive JSON 400.
- **Flash messages** on the shipment detail page via `?msg=` query parameter (not_registered, carrier_changed, carrier_error, invalid_carrier, params_updated, invalid_country, invalid_phone).
- **`registered=True` audit on accepted registration:** `process_operations` sets `shipment.registered = True` (and persists the auto-detected carrier) on every accepted register response, including the idempotent `-18019901` path.
- Alembic migration `0002`: adds `destination_country` (String(2)), `destination_postal_code` (String(20)), `phone_number_last_4` (String(10)) columns to `shipments`.
- **Docs HTML:** fixed unclosed `<code>` tags in the self-hosted API docs page.

### Changed
- `ShipmentResponse` and API `POST /api/v1/shipments` include the three new optional fields.
- `novatrack/providers/__init__.py`: `ProviderError` gains `retryable_without_quota` flag; `TrackingProvider` protocol adds `changecarrier` and `param` to `register`.
- `missing_required_params` now only reports supported param keys (those in `_OUR_PARAM_KEYS`); unsupported required keys are reported separately by `unsupported_required_params`.
- App version bumped to `0.1.2`.

### Upgrade notes
- Run `alembic upgrade head` (or `novatrack migrate`) — migration `0002` adds three nullable columns; downgrade with `alembic downgrade 0001`.
- Shipments already stuck in a missing-param retry loop will continue retrying until the next worker tick after `next_attempt_at` lapses; no manual intervention is needed, but you can accelerate them by submitting new params via the UI or `POST /api/v1/shipments/{id}/params`.

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
