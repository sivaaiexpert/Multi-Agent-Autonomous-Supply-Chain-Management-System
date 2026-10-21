# Customer onboarding

Onboarding is a stateful process per tenant. Each step's status is **derived from the tenant's real data** (not a checkbox), so it cannot say "done" while the data is missing. `GET /api/v1/admin/onboarding` returns the steps, the next step and whether production can be enabled.

| # | Step | Completed when | Endpoint |
|---|---|---|---|
| 1 | `create_organization` | an organisation record exists | `POST /api/v1/admin/onboarding/organization` |
| 2 | `create_admin` | the tenant has an active TENANT_ADMIN user | created with the tenant (`POST /api/v1/admin/tenants`) |
| 3 | `import_data` | committed imports exist for products, suppliers, quotes and inventory | `POST /api/v1/imports/preview` + `/commit` |
| 4 | `validate_data` | the data-quality gate has no blocking issue (orphan quotes, negative stock, missing history ...) | automatic |
| 5 | `configure_policies` | a business policy was saved (defaults count only once explicitly saved/confirmed) | `PUT /api/v1/admin/policy`, `POST /admin/onboarding/confirm-policies` |
| 6 | `configure_suppliers` | at least one supplier has a quote | automatic |
| 7 | `run_simulation` | a back-test on the tenant's own history has run | `POST /api/v1/admin/onboarding/simulation` |
| 8 | `review_results` | a human acknowledged the results (with notes) | `POST /api/v1/admin/onboarding/review` |
| 9 | `enable_production` | steps 1-8 are complete | `POST /api/v1/admin/onboarding/production` |

**Modes.** A new tenant is created in `SANDBOX`: workflows run, but ERP pushes are allowed only to the mock ERP. Production mode is refused until every earlier step is complete, and **demo tenants can never be promoted**.

## Typical sequence

1. A platform operator (SUPER_ADMIN) creates the tenant and receives the first administrator's API key once: `POST /api/v1/admin/tenants`.
2. The administrator creates users (`POST /admin/users`) and keys (`POST /admin/users/{id}/keys`). Roles: [RBAC.md](RBAC.md).
3. Import data. For each file: optionally `POST /mappings/suggest` with the file's column names, save the mapping (`PUT /mappings/{entity}`), `POST /imports/preview`, read the data-quality report, fix the source file or mapping, then `POST /imports/{job}/commit`. Customer identifiers can be translated with `PUT /mappings/ids/sku` and `/supplier`.
4. Set the business policy (limits, thresholds, approval tiers). The policy is validated against platform bounds and versioned with history.
5. Run the simulation, review results, enable production.

## Importing data

Entities: `products`, `inventory`, `suppliers`, `quotes`, `demand`, `purchase_orders`, `supplier_performance`. Formats: CSV, JSON, XLSX (limits: 25 MB, 300,000 rows; spreadsheet archives are checked against decompression bombs).

What the importer does, in order: read the file as text -> apply the column mapping and the closed set of transforms (trim, default, value map, date format; no code execution) -> validate types, ranges, enums, dates, duplicates in the file and against existing data, referential integrity (unknown SKU / supplier) and entity rules (for example target stock above reorder point) -> produce a **data-quality report** (counts, rejected rows with reasons, null rates, unmapped columns, missing fields) -> stage the validated records as a **preview** -> **commit** in one transaction with an audit event. Nothing reaches business tables before commit; a failed or blocked commit changes nothing. `allow_partial=false` blocks the commit if any row is rejected; `max_reject_rate` blocks it when the file is probably the wrong one.

**Rollback.** A commit is one transaction; to undo a committed import, restore the pre-import backup (see [OPERATIONS.md](OPERATIONS.md)). Row-level import rollback after commit is FUTURE.

Text fields are sanitised (markup stripped, control characters removed) and spreadsheet formula prefixes are neutralised in exports.

## Data provenance

Imported data is marked `CUSTOMER_DATA`; generated data is `SYNTHETIC`. Every report and KPI shows which one it was computed on. A tenant loaded with generated data is labelled **DEMO / SYNTHETIC DATA** in the console and in reports.
