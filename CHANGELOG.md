# Changelog

All notable changes are documented here. Versions use SemVer.

## 0.1.3 — 2026-10-27

### Fixed
- **changecarrier payload key corrected:** provider call now sends `carrier_new` (not `carrier`) and optional `carrier_old` per the official 17TRACK v2.2/v2.4 spec.  The v0.1.2 key was wrong; the API silently rejected all change-carrier requests.
- **changecarrier error codes corrected** (all nine codes were mislabelled in v0.1.2):
  - -18019801: Multiple carriers registered — specify `carrier_old`
  - -18019802: `carrier_new` value is incorrect
  - -18019803: New carrier cannot be the same as current
  - -18019804: `carrier_new` or `final_carrier_new` must be specified
  - -18019805: Tracking number not registered for `carrier_old` / `carrier_old` incorrect
  - -18019806: Stopped number — retrack before changing carrier
  - -18019807: Change limit reached (max 5) — previously mis-treated as idempotent "same carrier" success
  - -18019808: Tracking result not yet returned — wait before changing
  - -18019809: Duplicate registration info (new)
- **-18019807 never treated as success:** the previous release incorrectly returned an accepted result when the provider rejected with -18019807 (limit exceeded).  This is now correctly raised as `ProviderError`.
- **Register error codes 18/19/20/22 accurate messages:**
  - -18010018: Country + postal code required (was generic prompt)
  - -18010019: Postal code required
  - -18010020: Phone number last 4 required
  - -18010022: Shipping date required — NovaTrack cannot supply this; marks op `done=True` permanently (no retry loop)
- **`ProviderError.terminal` flag:** new flag causes `process_operations` to mark the op `done=True` instead of retrying with backoff (used for -18010022).
- **Trash re-add — unregistered:** adding a tracking number that's already in Trash AND was never registered automatically purges the old row (all related rows removed) and creates a fresh shipment with the new submitted data.  No user action needed.  (`force_purge_unregistered_trash` cancels any pending delete ops before deletion, since the provider was never called.)
- **Trash re-add — registered:** adding a number that's in Trash and registered shows a contextual error with a direct link to restore or purge the existing shipment — no silent failure.
- **Archive/active duplicate:** adding a number already tracked shows "already archived" / "already being tracked" with a direct link to the existing shipment.
- **`POST /shipments/{id}/restore`:** restores a trashed shipment back to active.  Cancels pending delete ops.  If the shipment was registered, queues a re-register op (costs one quota, user is warned).
- **`POST /shipments/{id}/purge`:** hard-deletes a trashed shipment immediately if no pending provider ops exist.  Returns actionable error if deletion is still pending (preserves provider lifecycle).
- **Paused registration prominent notice:** shipment detail page now shows a yellow banner at the top (above the header) when registration is paused indefinitely.  The banner shows the actual provider error message and links directly to the `#update-params` form.  The "Registration pending" hint distinguishes paused vs. actively-pending.  New `is_registration_paused()` helper in `tracking.py`; `registration_paused` boolean passed to `shipment.html` context.
- **Refresh unregistered flash message:** no longer says "check back shortly" when registration may be paused indefinitely.  Now directs the user to check the error below and update tracking parameters.
- **Provider error alert position:** moved above the shipment header so it is visible on first paint without scrolling on both desktop and mobile.
- **ETA section:** empty `{}` dict no longer renders the ETA section.  Null/missing `from`/`to` fields display an em dash (—) instead of blank.
- **Restore/Purge buttons** in shipment detail for trash-bucket shipments.
- **Params form anchor** (`#update-params`) linked from the provider error alert for one-click navigation.
- **`restore_shipment` State 2 fix:** when a shipment's delete op already ran (provider deleted it while in trash, `registered=False`), restoring now correctly queues a re-register op (costs quota).  Previously, only `registered=True` cases triggered re-registration; a shipment whose delete had already completed would return to active permanently unregistered with no recovery path.  New test: `test_restore_shipment_after_delete_op_completed_requeues_register`.  Also adds `test_restore_shipment_paused_register_preserved` verifying the paused-register case does not duplicate ops.
- **CSS `alert-warn` / `alert-info` missing:** the `style.css` file lacked definitions for `.alert-warn` (used by the paused-registration banner) and `.alert-info` (used for flash messages).  Both classes now render correctly (amber/yellow for warn, neutral surface for info).  Two new stylesheet contract tests guard against regression.

### Upgrade notes
- No database schema changes.  Migration remains at revision `0002`.
- API clients calling `POST /api/v1/shipments/{id}/carrier` must now send `{"carrier_new": N}` instead of `{"carrier": N}`.

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
