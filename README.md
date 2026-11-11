# SecretRadar / Samlab Admission

Active source: `_integration/samlab-admission-20261111/`.

| Directory | Purpose |
| --- | --- |
| `sentinel/` | FastAPI security service, administrator-only Vue UI, roster import and signed admission API |
| `sub2api/` | Samlab business platform source and independent admission worker |
| `archive/` | Existing archive service and admission enforcement |
| `plugins/` | Existing workspace plugin and admission enforcement |

The integration is an isolated candidate, not an approved production release.
Synthetic integration tests do not constitute production acceptance. Existing
business platform administrator creation and password reset are preserved.

## Local Security Service

Use Python 3.12. From `sentinel/`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

The security service currently accepts loopback access only. Do not expose it
by removing those checks or trusting arbitrary proxy headers. Remote access
requires a separately reviewed authentication and trusted-proxy boundary.

For tests, install `sentinel/requirements-dev.txt`, then run from `sentinel/`:

```powershell
python -m unittest discover -s tests -p "test_*.py"
python tests/verify_admission_browser.py
```

Browser tests require Playwright and an available Microsoft Edge installation.
They use disposable databases, not the administrator's runtime database.

`isolated.compose.yml`, `setup_isolated.py` and `verify_isolated.py` target an
explicit isolated test workspace. Review their environment-specific paths
before running. They are not production deployment commands.

## Configuration and Data

Inject service signing keys and database credentials using protected server
configuration. Never commit real passwords, API tokens, invitation tokens,
private keys, `.env` files, databases, runtime logs or backups. Environment
examples are templates only. Do not use synthetic test credentials in production.

Runtime data, dependency directories, local packages and test outputs are
intentionally excluded. The original portable release and backups remain local.

Preserve existing source licenses in the included companion projects.

Hardcoded third-party OAuth client credentials are not distributed in this fork.
To use those existing OAuth features, supply the authorized client configuration
through `ANTIGRAVITY_OAUTH_CLIENT_ID`, `ANTIGRAVITY_OAUTH_CLIENT_SECRET`,
`GEMINI_CLI_OAUTH_CLIENT_ID` and `GEMINI_CLI_OAUTH_CLIENT_SECRET`.
Custom Gemini OAuth clients configured through the existing settings remain
supported. Do not paste client secrets into source files.

Rule test fixtures tagged `!synthetic` generate deterministic, unissued values
locally. They are not live keys and are never validated against provider APIs.
