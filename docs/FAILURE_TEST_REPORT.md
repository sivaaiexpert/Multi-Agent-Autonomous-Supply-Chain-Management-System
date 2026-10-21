# Controlled failure test report

Generated 2026-10-02 08:53:29 by `make chaos-test` (`python -m evaluation.failure_suite`). Data: SYNTHETIC retailer tenants, mock ERP and local fault injection.

**12 of 12 scenarios passed.** Times are wall-clock milliseconds measured on the test machine; 'detection' is fault -> failure recorded/visible, 'recovery' is fault removed -> correct end state. They are not SLA figures.

| Scenario | Fault | Detected | Detect ms | Recovered | Recover ms | Integrity | No duplicates | State correct |
|---|---|---|---:|---|---:|---|---|---|
| llm_unavailable | LLM endpoint unreachable (connection refused) | yes | - | yes | 2434.6 | yes | yes | yes |
| db_unavailable | tenant database write-locked by another connection | yes | 1118.4 | yes | 13.4 | yes | yes | yes |
| erp_unavailable | ERP unavailable (every call raises a retryable error) | yes | 294.4 | yes | 137.3 | yes | yes | yes |
| erp_timeout | ERP calls time out | yes | 218.8 | yes | 102.7 | yes | yes | yes |
| partial_dispatch | ERP fails after accepting 2 purchase orders | yes | 236.8 | yes | 50.2 | yes | yes | yes |
| supplier_unavailable | supplier S03 withdraws every quote | yes | 321.6 | yes | 321.6 | yes | yes | yes |
| duplicate_webhook_events | same signed webhook delivered 5 times, then re-sent with a different body under the same event id | yes | 68.0 | yes | 0.0 | yes | yes | yes |
| worker_crash | worker process killed mid-workflow | yes | - | yes | 132.0 | yes | yes | yes |
| invalid_data_import | corrupt customer upload (type errors, duplicate ids, negatives, blank id, injected markup) | yes | 31.3 | yes | 0.0 | yes | yes | yes |
| inventory_mismatch | ERP on-hand differs from the platform's inventory | yes | 3.1 | yes | 0.0 | yes | yes | yes |
| outbound_endpoint_down | customer webhook receiver unreachable | yes | 12296.2 | yes | 27.4 | yes | yes | yes |
| disaster_recovery | tenant data destroyed after a backup; then a corrupted backup is offered | yes | - | yes | 65.8 | yes | yes | yes |

## Evidence per scenario

### llm_unavailable

- Expected: workflow still reaches the human gate with the same decision as the deterministic path
- Evidence: `{"status": "AWAITING_APPROVAL", "decisions_equal_deterministic_reference": true, "llm_metrics": {"request_count": 1, "failure_count": 1, "timeout_count": 1, "fallback_count": 1, "retry_count": 0, "breaker_rejections": 0, "tokens_in": 0, "tokens_out": 0, "by_model": {}, "p50_ms": null, "p95_ms": null, "p99_ms": null}}`

### db_unavailable

- Expected: writes fail with a clear error after the busy timeout; nothing half-written; works once released
- Evidence: `{"error": "OperationalError: database is locked", "busy_timeout_used_s": 1, "orders_before": 0, "orders_after": 0}`

### erp_unavailable

- Expected: local approval stands; pushes FAILED with reason; retry sends each PO to the ERP exactly once
- Evidence: `{"approval_stage": "approved", "pos_sent_locally": 4, "failed_pushes_during_outage": 4, "erp_orders_after_retry": 4}`

### erp_timeout

- Expected: recorded as failed/retryable, no duplicate ERP orders after retry
- Evidence: `{"errors": ["ERPTimeout: mock ERP timed out"], "erp_orders": 4}`

### partial_dispatch

- Expected: 2 SENT, remainder FAILED and recorded; retry sends only the remainder
- Evidence: `{"sent_during_fault": 2, "failed_during_fault": 2, "erp_orders_final": 4}`

### supplier_unavailable

- Expected: no order addressed to it; a valid plan from the remaining suppliers (or an explicit escalation)
- Evidence: `{"victim": "S03", "suppliers_used": ["S01", "S02", "S04"], "proposals": 4}`

### duplicate_webhook_events

- Expected: applied once, 4 acknowledged as duplicates, tampered re-send rejected (409)
- Evidence: `{"results": ["processed", "duplicate", "duplicate", "duplicate", "duplicate"], "changed_body": "409:event_id_reuse", "event_rows": 1, "stock_after": 321}`

### worker_crash

- Expected: state left RUNNING, a restarted service resumes it from the durable checkpoint, no duplicate orders, recover() idempotent
- Evidence: `{"recover": {"resumed": ["8644e892b3704cd78e0393b8b6a3b992"], "at_gate": [], "failed": []}, "second_recover": {"resumed": [], "at_gate": [], "failed": []}, "purchase_orders": 4, "proposals": 4}`

### invalid_data_import

- Expected: problems reported per row; with allow_partial=False the commit is blocked and nothing is written
- Evidence: `{"rows_before": 10, "rows_after_commit_attempt": 10, "commit_blocked": true, "report_status": "BLOCKED", "rejected_rows": 4, "issues_sample": ["name is empty after removing markup (got '<script>alert(1)</script>')", "reorder_point must be a number (got 'five')", "reorder_point must be >= 0 (got -3)", "sku_id is required"], "injected_markup_rows": 0}`

### inventory_mismatch

- Expected: mismatches listed with both values; no data modified by reconciliation
- Evidence: `{"mismatches_found": 10, "stock_sum_before": 12636.0, "stock_sum_after": 12636.0}`

### outbound_endpoint_down

- Expected: bounded retries with backoff, dead-lettered with a reason, nothing dropped; replay delivers exactly once when the receiver is back
- Evidence: `{"deliveries": 1, "dead_lettered": 1, "attempts_each": [6], "error": "URLError: <urlopen error [WinError 10061] No connection could be made because th", "after_replay": ["DELIVERED"]}`

### disaster_recovery

- Expected: restore reproduces row counts and a valid audit chain; a corrupted backup is refused
- Evidence: `{"measured_restore_ms": 65.8, "backup_seconds": 0.067, "rows_in_backup": 4, "audit_events_verified": 18, "corrupted_backup_refused": true, "scope": "measured on this machine with a small synthetic tenant; RTO for real data volumes must be measured on the target system. RPO is the backup interval you schedule (not measured here)"}`

## What this does not show

- Faults are injected in one process on one machine; real networks, disks and multi-node failures are not exercised.
- The ERP is the in-repo mock (or the mock HTTP server), not a real ERP.
