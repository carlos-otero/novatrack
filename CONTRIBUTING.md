# Contributing

NovaTrack is a small self-hosted, single-household tracking service. Keep changes generic: no personal domain names, API credentials, real tracking fixtures, proprietary logos or deployment-specific assumptions.

## Local setup

```sh
uv sync --frozen --python 3.13
uv run pytest -q
uv run ruff check .
```

Write a failing behavioral test first, implement the smallest complete slice, then run the focused test and the full suite. Use `httpx.MockTransport` or a local test IdP/receiver; never spend real registration quota. Test the real caller-to-adapter path rather than duplicating normalization/signature formulas in test helper implementations.

Runtime environment is explicit; use a disposable PostgreSQL 16 DB for migration/integration tests. Add versioned Alembic migrations for model changes. Run `alembic upgrade head` and `alembic check` with a secret-free or disposable `DATABASE_URL`. Do not use `create_all` on production startup.

Changes to authentication, CSRF, proxy trust, webhook signing, outbound destinations or retry idempotency require regression tests for rejection cases. Do not relax HTTPS/SSRF requirements for convenience. Keep the worker optional channels off by default, logs free of secrets and the UI usable without external scripts/CDNs.

Before a PR, include the purpose, tests actually executed, migrations, backward-compatibility notes and any unverified behavior. Discuss major features in an issue first. Keep README, environment examples and CHANGELOG aligned with production source. All contributions are under the [MIT license](LICENSE).
