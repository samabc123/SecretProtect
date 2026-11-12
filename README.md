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
The existing rule self-test baseline has four failing fixtures:
`openrouter-api-key`, `github-pat`, `dingtalk-webhook` and
`generic-env-assignment`. Publication cleanup preserves that baseline; this
revision does not claim complete detection coverage or production integration.

## Permanent Roster

Roster CSV files require `username` / `用户名`, `email` / `邮箱` and `id` / `ID`
columns, in any order. ID is the existing Samlab user ID (a positive signed
64-bit integer), not the security service's member ID. Headers such as
`用户id`, `用户 ID`, `user_id`, `UID` and `平台ID` are also accepted; matching
ignores case, whitespace, underscores, hyphens and full-width forms.
Multiple ID columns and unrelated identifiers such as `group_id` or
`request_id` are rejected instead of guessed. Passwords, student
numbers and expiry columns are rejected. Imported IDs are displayed in the
whitelist but do not verify mailbox ownership, provision accounts or confirm
remote account status.

Reimport can fill a missing platform ID on an unlinked legacy roster entry,
without adding another person. A fresh preview explicitly marks deleted or
revoked people as `restore`; confirmation restores their permanent whitelist
with the same member ID, email and platform account mapping. Missing IDs can
only be filled on legacy roster entries without a linked registration workflow.
Restoration retains deletion audits, writes a restoration audit, increments the
policy version and awaits platform synchronization instead of claiming remote
access is already restored. A preview made before deletion cannot silently
restore access. It cannot replace an existing account mapping or reassign a
deleted member's ID to another email. Same-file
mapping conflicts block the entire batch; exact duplicates are skipped.
Only a matching signed platform administrator delivery can associate an
imported account with the service workflow. New registration cannot claim an
existing imported account by matching its email.

Whitelist membership is permanent until explicitly removed. Approval, account
creation, login and API access remain distinct operations. Session expiry,
administrator account disabling and the worker's short authorization lease are
unchanged.

Whitelist listings default to descending numeric platform account ID, with
unlinked entries last; the backend applies this ordering before its 500-person
limit. Sorting and exports retain exact 64-bit IDs. Administrators can also
add a person manually using username, email and existing platform ID. Checking
the form creates a short-lived administrator-owned preview; confirmation uses
the same transactional, idempotent conflict and restoration checks as CSV.
New manual records have a distinct `manual` source and audit trail, and do not
create security administrators or claim a remote account has been provisioned.
The source filter no longer offers direct administrator creation or invitations;
historical records and the signed platform compatibility contract are retained.

Administrators can select the displayed rows and delete up to 500 members in
one atomic operation. Single and batch deletion share the same logic, require
confirmation and a reason, preserve audit records and queue account/session/API
authorization invalidation. Repeated delivery is idempotent.

Before first startup of this revision, back up the admission database. An
audited, transactional migration clears future membership expiry for active
members. Previously expired or revoked members are not restored; their state
is merged into logical deletion. Historical columns and audit records remain
for recovery. The renewal and separate revocation endpoints are removed.
Rollback requires the pre-upgrade database backup as well as the old code
revision; reverting code alone does not restore former expiry values.
