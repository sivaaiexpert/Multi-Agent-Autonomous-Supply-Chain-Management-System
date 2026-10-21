# Security and data privacy

This document describes the **technical controls that exist in the code** and the tests that exercise them. It is not a certification and not a statement that any deployment is compliant with any regulation or standard. A real deployment needs its own review, TLS termination, secret management, monitoring and (for regulated customers) an independent assessment. Threat model: [../THREAT_MODEL.md](../THREAT_MODEL.md).

## Security review: threat -> control -> test

Run all of it with `make security-test` (154 tests at the time of writing, plus `tests/test_security.py` and `tests/test_tenancy.py`).

| Threat | Control | Evidence |
|---|---|---|
| Missing / forged credentials | API keys, SHA-256 hashed at rest, constant-time lookup, expiry, revocation, disabled-user / suspended-tenant lockout | `test_every_read_endpoint_rejects_missing_or_bad_key`, `test_api_keys_are_revocable...`, `test_suspended_tenant_is_locked_out`, `test_keys_are_stored_hashed_and_never_listed` |
| Brute force | per-IP failed-auth limiter (429), per-key token bucket (429 + `Retry-After`) | `test_failed_authentication_is_rate_limited`, `test_per_key_rate_limit_returns_429_with_retry_after` |
| Broken access control / privilege escalation | server-side permission check per endpoint; role-assignment rules; SUPER_ADMIN has no data access | `test_platform_api.py` (RBAC group), `test_role_cannot_be_changed_by_the_user_it_applies_to` |
| Cross-tenant access | database per tenant; tenant derived only from the key; defence-in-depth row stamps and filters | `test_cross_tenant_workflow_is_invisible`, `test_tenant_cannot_be_chosen_by_header_or_parameter`, import-job and webhook cross-tenant tests, `tests/test_tenancy.py` |
| SQL injection | parameterised queries everywhere; user-controlled filters are enum / regex-validated at the edge; identifiers validated | `test_sql_injection_*` (query parameters, ids, imported data) |
| Path traversal | tenant ids restricted to `[a-z0-9-]`; ids / entities / report kinds validated; uploads staged under server-generated names, never the client filename | `test_path_traversal_in_identifiers_is_refused`, `test_tenant_id_cannot_be_a_path`, `test_hostile_filenames_never_become_paths` |
| SSRF | integration and subscription URLs resolved and refused when loopback / link-local / private / non-HTTP / credentialed; re-checked on every outbound delivery (DNS rebinding) | `test_ssrf_targets_are_blocked_for_outbound_subscriptions`, `test_integration_creation_rejects_ssrf_targets` |
| Webhook forgery, replay, tampering | HMAC-SHA256 over `timestamp.body`, 300 s window, unique event ids, payload hash on duplicates, 256 KB cap, indistinguishable 401s | `test_webhook_replay_outside_window_and_cross_tenant_forgery`, `tests/test_integrations.py` |
| Command injection | no shell execution anywhere on request paths; import transforms are a closed interpreter (no `eval`) | `tests/test_importer.py` |
| Prompt injection | LLMs only draft text and never decide; supplier / imported text is sanitised before it reaches a prompt; model output is schema-validated; decisions come from deterministic engines and pass the policy validator and a human gate | `test_prompt_injection_in_supplier_text_is_neutralised...`, `tests/test_llm_resilience.py`, `tests/test_policy.py` |
| Stored XSS / markup | text sanitised on import (loops until stable, handles entity-encoded markup); security headers on every response | importer tests, `test_security_headers_and_no_cors_by_default` |
| CSV / formula injection | cells starting with `= + - @` are neutralised in CSV reports | `tests/test_business_impact.py` |
| CSRF | authentication is a header key, no cookies or sessions, so browsers cannot attach credentials cross-site | `test_no_cookie_session_means_no_csrf_surface` |
| CORS | closed unless `SCM_CORS_ORIGINS` is set; exact origins only; no credentials; wildcard refused in production | `test_cors_is_closed_by_default...`, `test_cors_allows_only_configured_origins`, `test_production_refuses_wildcard_cors` |
| Resource exhaustion | 1 MB body cap (32 MB imports), `Content-Length` required, request timeout, import row / size limits, zip-bomb guard, bounded retries | `test_oversized_body_and_missing_content_length_are_refused`, importer tests |
| Information leakage | uniform error format, no stack traces or paths, identical 401 for unknown tenant / source / bad signature | `test_error_responses_do_not_leak_internals` |
| Tampering with history | audit log is append-only (SQL triggers) and hash-chained; `verify_chain` detects edits | `test_audit_trail_cannot_be_rewritten`, `tests/test_data_integrity.py` |
| Secrets exposure | integration secrets Fernet-encrypted with `SCM_SECRET_KEY`; never returned by the API; startup validation; no secrets in source (scanned by `make production-check`) | `test_secrets_are_encrypted_at_rest`, `test_secret_key_is_validated_at_startup`, `test_secrets_never_appear_in_integration_listings` |
| Unsafe model configuration | tenants choose only from a platform allow-list; no field can disable guardrails or approval | `test_model_config_cannot_disable_safety_controls` |

**Findings fixed during the review**: `GET /mappings/{entity}` accepted any string (not exploitable - it is a keyed lookup - but identifiers are now validated); earlier rounds fixed the unbounded request body, missing rate limiting, wildcard-capable CORS, and entity-encoded markup surviving sanitisation.

**Not covered / residual risk**: no external penetration test; no TLS in the application (terminate it in front); no WAF or DDoS protection; SQLite files are not encrypted at rest (use disk encryption); the platform secret key is a single key without rotation tooling; dependency vulnerabilities are not scanned automatically; the console (Streamlit) uses a configured role (`SCM_UI_ROLE`), not per-user login.

## Secrets and configuration

`.env.example` documents every variable; `.env` is git-ignored. In production the process refuses to start without a valid `SCM_SECRET_KEY` or with wildcard CORS (`secrets_store.assert_startup_ok`). `make production-check` scans the tree for credential patterns and reports configuration problems without printing secret values.

## Data privacy (technical controls that support compliance requirements)

- **Classification and inventory**: every store is classified (internal / confidential / sensitive), with purpose, whether it holds personal data, and retention - `GET /admin/privacy/inventory`.
- **Minimisation**: personal data is limited to user names / e-mails and actor ids; LLM prompts and replies are **not stored** (only token counts and latency); inbound webhook payloads are stored validated, not raw; audit payloads are redacted.
- **Retention**: `POST /admin/privacy/purge` deletes operational data past its retention (webhook events and deliveries 90 days, node runs 90 days, LLM accounting 365 days, API usage 400 days); business records and the audit log are kept until tenant deletion. The windows are defaults in `enterprise/privacy.py` and can be overridden when the purge is invoked (`retention=`); a per-tenant setting exposed through the API is FUTURE.
- **Export**: `GET /admin/privacy/export` returns a ZIP of the tenant's data as JSON, without secrets or key hashes.
- **Deletion**: `DELETE /admin/tenants/{id}?confirm={id}` revokes every key, disables every user, marks the tenant `DELETED` and removes its database, checkpoints and outbox files. The control-plane record and its lifecycle audit entries are kept; **backups are not touched** and must be deleted separately.
- **Access logging**: access denials and all configuration changes (policy, users, keys, integrations, imports, approvals) are audited with actor and time; ordinary reads are not logged.

These controls make it easier to meet obligations such as access, portability, erasure and minimisation. They do not themselves establish compliance with GDPR, SOC 2, ISO 27001 or any other framework.
