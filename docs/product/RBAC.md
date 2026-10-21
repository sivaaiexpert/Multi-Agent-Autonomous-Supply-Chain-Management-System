# Roles and access control

Authorisation is enforced **on the server** for every endpoint (the console hides controls as a convenience, never as the control). A request is authenticated to a key -> user -> (tenant, role); the handler checks a named permission, then enters that tenant's scope. Denials are audited (tenant audit trail, or the platform audit for anonymous attempts).

## Roles

| Role | Purpose | Permissions |
|---|---|---|
| SUPER_ADMIN | Platform operator. Manages tenants **only**. Cannot read any tenant's business data. | `manage_tenants` |
| TENANT_ADMIN | Customer administrator | all tenant permissions: `read`, `read_audit`, `run_workflow`, `approve`, `edit_procurement`, `import_data`, `run_simulation`, `view_reports`, `view_trace`, `view_usage`, `manage_users`, `manage_policies`, `manage_integrations`, `configure` |
| OPERATOR | Runs workflows and imports | `read`, `read_audit`, `run_workflow`, `import_data`, `run_simulation`, `view_reports` |
| APPROVER | Decides on proposals | `read`, `read_audit`, `approve`, `edit_procurement`, `view_reports`, `view_trace` |
| ANALYST | Read-only analytics | `read`, `read_audit`, `run_simulation`, `view_reports`, `view_trace`, `view_usage` |
| VIEWER | Read-only | `read`, `read_audit` |

`ADMIN` is a legacy alias of TENANT_ADMIN kept for the original environment-variable keyring.

Approval authority is separate from permissions: OPERATOR level 1, APPROVER level 2, TENANT_ADMIN level 3. A step in an approval chain names a required level; a lower level cannot act on it.

## Rules enforced

- **Tenant isolation**: the tenant comes only from the authenticated key. Each tenant has its own database; rows are additionally stamped and filtered by `Tenant_ID`.
- **No privilege escalation**: a user can only create users whose role is in `assignable_roles(own role)`; only a SUPER_ADMIN creates TENANT_ADMINs; SUPER_ADMIN users exist only in the platform tenant (bootstrapped from the environment keyring); a user cannot disable themselves.
- **Segregation of duties**: with `distinct_approvers` (default) the same person cannot approve two steps of one chain.
- **Keys**: shown once, stored as SHA-256 only, revocable, optional expiry, last-used tracked; suspended tenants and disabled users stop authenticating immediately.
- **Policy and model configuration** are validated against platform bounds; there is no field that disables guardrails, approval or policy validation.

## Tests

`tests/test_platform_api.py` and `tests/test_security_review.py` (run `make security-test`) cover: missing / invalid keys on every read endpoint; viewer cannot write; non-admin roles cannot use admin endpoints; tenant admin cannot create a SUPER_ADMIN, list tenants or delete another tenant; SUPER_ADMIN gets 403 on tenant data; tenant cannot be selected by header or parameter; cross-tenant workflow, approval, trace, import-job and webhook access; revoked key and suspended tenant lockout; mass-assignment of `tenant_id` on policy / model config rejected.
