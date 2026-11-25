# Sentinel — Security

> Status: Phase 4 (detection). Updated as each phase adds attack surface; finalised in Phase 10.
> Sentinel is an educational simulation using synthetic data. It holds no real customer, card or banking data.

## Reporting

This is a portfolio project. Please report security issues privately to the repository owner rather than opening a public issue.

## Controls implemented (Phase 2)

| Area | Control | Where |
|---|---|---|
| Passwords | Argon2id (pwdlib defaults), 12-char minimum, must not contain the email name, 128-char input cap | `core/security.py`, `auth/service.py` |
| User enumeration | Unknown email and wrong password return the same message; a dummy Argon2 verify equalises timing | `AuthService.login` |
| Brute force | Account locks for 15 min after 5 consecutive failures; `/auth/login` and `/auth/refresh` rate-limited (10/min per client IP) | `auth/service.py`, `core/rate_limit.py` |
| Access tokens | HS256 JWT, 15-min lifetime, `iss`/`aud`/`exp`/`type` validated, `alg=none` rejected. Held in memory by the client, never in a cookie | `core/security.py` |
| Refresh tokens | 48-byte random opaque token in an `HttpOnly; SameSite=Strict; Secure` cookie scoped to `/api/v1/auth`. Only a SHA-256 hash is stored. Rotated on every use; replaying a rotated token revokes the whole token family | `auth/service.py` |
| Session revocation | Logout, password change and deactivation revoke refresh tokens. The user is reloaded on every request, so deactivation and role changes apply immediately | `auth/dependencies.py` |
| Authorisation | Permission checks enforced server-side by `RequirePermission`. A test discovers every route, fails if a route has no permission (unless explicitly public), and checks the full route-by-role matrix | `tests/integration/test_permission_matrix.py` |
| Separation of duties | Admins cannot adjudicate alerts/cases; analysts cannot change rules, scoring or models; auditors are read-only; admins cannot demote or deactivate themselves | `core/permissions.py`, `users_service.py` |
| Concurrent edits | Optimistic locking (`version`) — stale writes get 409, never a silent overwrite | `users_service.py` |
| Audit trail | Every security event and admin change is written in the same transaction as the change. `UPDATE`, `DELETE` and `TRUNCATE` on `audit_log` are rejected by a database trigger. IP addresses are stored only as keyed hashes | migration `0001`, `audit/service.py` |
| Logging | JSON logs; keys containing password/token/secret/cookie/authorization/card data are redacted recursively; query strings are not logged | `core/logging.py`, `core/middleware.py` |
| Errors | One error envelope; validation errors never echo submitted values; no stack traces to clients | `core/errors.py` |
| HTTP headers | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, COOP | `core/middleware.py` |
| CORS | Explicit origin allowlist with credentials; no wildcard | `main.py` |
| Configuration | Secrets from environment only; `.env` git-ignored; app refuses to start in production with default/short JWT secret, default salt or insecure cookies; API docs disabled in production | `core/config.py` |
| Containers | Multi-stage image, non-root user, no dev dependencies; ports bound to `127.0.0.1` | `backend/Dockerfile`, `docker-compose.yml` |

## Controls implemented (Phase 3 — ingestion)

| Area | Control | Where |
|---|---|---|
| Card / personal data | Files with card, bank-credential, personal-data or protected-attribute columns are rejected whole; REST records with such fields are rejected and the values dropped. Text values that look like card numbers (13–19 digits, card prefix, valid Luhn) are rejected | `ingestion/validation.py`, `csv_reader.py` |
| Rejected-row storage | Only known fields are kept; `device_id` and card-like values are replaced with `[REDACTED]`; values truncated to 256 chars | `validation.redact_raw_row` |
| Ground-truth isolation | Label columns (`is_fraud`, `typology`, …) are ignored and never stored | `csv_reader.py` |
| Upload limits | Body read only after the permission check; `Content-Length` checked and the stream counted against `SENTINEL_IMPORT_MAX_BYTES` (413); row, column and cell-size limits; content type allow-list (415) | `ingestion/router.py`, `csv_reader.py` |
| Parsing | UTF-8 only, NUL bytes rejected, strict CSV quoting, header names normalised | `csv_reader.py` |
| Data minimisation | Raw uploads are deleted after processing; the batch keeps only the SHA-256. Device ids are stored as keyed hashes | `ingestion/service.py` |
| Duplicate safety | `UNIQUE (source_system, external_id)` + `ON CONFLICT DO NOTHING` — concurrent imports cannot double-insert | `transactions/service.py` |
| Immutability | `UPDATE`/`DELETE`/`TRUNCATE` on `transactions` rejected by trigger | migration `0002` |
| CSV export | Report cells starting with `= + - @` are prefixed with `'` (spreadsheet formula injection) | `ingestion/router.py` |
| Rate limiting | `POST /imports` and `POST /transactions` limited to 30/min per client | `core/rate_limit.py` |
| Job errors | Database errors are recorded as exception type + SQLSTATE only, because driver messages can echo row values | `jobs/service.describe_error` |
| Filenames | Display-only; path components and control characters stripped; never used to touch the filesystem | `ingestion/service.safe_filename` |
| Audit | `import.uploaded` (user), `import.completed` / `import.rejected` / `import.failed` (system or user) with counts — never row contents | `ingestion/service.py` |

## Controls implemented (Phase 4 — detection)

| Area | Control | Where |
|---|---|---|
| No code injection via configuration | Admins tune typed, bounded parameters (Pydantic, extra keys refused); rule logic exists only in reviewed code | `detection/rules.py`, `detection/engine.py` |
| Separation of duties | Only `rules:configure` (admins) can change rules; analysts can re-score (`detection:run`) but not tune detection; auditors read only | `rules/router.py`, `scoring/router.py` |
| Reproducibility | Rule versions, rule sets, assessments and rule hits are append-only (trigger); each assessment records the rule set, scoring config and feature values it used | migration `0003` |
| Concurrent edits | `expected_version` → 409 on stale edits; a transaction-level advisory lock serialises rule-set snapshots | `rules/service.py` |
| Audit | `rule.version_created` (before/after diff + reason) and `detection.run_requested` | `rules/service.py`, `scoring/service.py` |
| Wording | Explanations describe unusual activity; a test asserts none contain "fraud" | `tests/unit/test_rules.py` |

## Known limitations / planned work

| Item | Plan |
|---|---|
| The application's database user owns the tables, so it could technically drop the audit trigger | Phase 10: separate migration role (DDL) from a runtime role with only `SELECT`/`INSERT` on append-only tables |
| Rate limiting uses in-memory storage and the direct client IP | Fine for one API process. Behind a proxy, enable trusted proxy headers; for several processes, move limits to shared storage |
| Concurrent refreshes from two browser tabs can trip reuse detection and log the user out | Acceptable for now; a short grace window for the immediately previous token may be added in Phase 9 |
| No MFA | Out of scope for the simulation; documented as a production requirement |
| Demo accounts share one dev-only password from `.env.example` | Seed command refuses to run when `SENTINEL_ENV=production` |
| `starlette.testclient` warns that `httpx` support is deprecated in favour of `httpx2` | Test-only dependency; revisit when pinning versions in Phase 10 |
| JSON request bodies (e.g. `POST /transactions`) have no byte limit in the app; only the record count is limited | Phase 10: request-size limit at a reverse proxy or ASGI middleware |
| Uploaded files sit in Postgres (`import_files`) until the worker processes them | Acceptable at simulation scale; deleted after processing |
| Card-number detection can reject purely numeric 13–19-digit ids that happen to pass Luhn | Deliberately conservative; documented in DATA_DICTIONARY.md |

## Data protection

- No real personal data, card numbers (PAN/CVV) or bank credentials are collected. Ingestion rejects such fields and card-like values (see the Phase 3 table above).
- Device identifiers and IPs are stored as keyed hashes (HMAC-SHA256 with `SENTINEL_HASH_SALT`) because only equality checks are needed.
