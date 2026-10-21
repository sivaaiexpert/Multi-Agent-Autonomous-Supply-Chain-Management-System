"""Data privacy controls: what is stored and why, retention, export, deletion.

These are TECHNICAL CONTROLS THAT SUPPORT compliance requirements (data minimisation, retention limits, access / portability, erasure, auditability).
Using them does not by itself make a deployment compliant with GDPR, India's DPDP Act or any other law: that needs a legal assessment of the whole
processing activity, contracts and organisational measures that software cannot provide.
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import datetime, timedelta
from typing import Any

from scm import audit, db, obs

from . import controlplane, tenancy

PUBLIC, INTERNAL, CONFIDENTIAL, SENSITIVE = "public", "internal", "confidential", "sensitive"

# store / table -> (classification, purpose, personal data?)
CLASSIFICATION: dict[str, tuple[str, str, str]] = {
    "control.tenants": (INTERNAL, "tenant routing, plan and mode", "no"),
    "control.users": (CONFIDENTIAL, "authentication and role assignment", "yes: name and e-mail of platform users"),
    "control.api_keys": (SENSITIVE, "authentication (SHA-256 hash only, key itself is never stored)", "indirect: key id bound to a user"),
    "control.platform_audit": (INTERNAL, "tenant / user / key lifecycle audit", "yes: actor ids"),
    "control.api_usage": (INTERNAL, "usage metering per day and route group", "no"),
    "inventory": (CONFIDENTIAL, "stock levels and thresholds", "no"),
    "vendors": (CONFIDENTIAL, "supplier master data", "business contact e-mail of suppliers"),
    "vendor_listings": (CONFIDENTIAL, "supplier quotes, MOQ, capacity", "no"),
    "demand_history": (CONFIDENTIAL, "historical demand for forecasting", "no"),
    "purchase_orders": (CONFIDENTIAL, "orders and their status", "no"),
    "supplier_deliveries": (CONFIDENTIAL, "supplier performance history", "no"),
    "forecasts": (INTERNAL, "forecast results", "no"),
    "audit_events": (CONFIDENTIAL, "immutable record of decisions and actions", "yes: actor ids; payloads are redacted"),
    "decision_traces": (CONFIDENTIAL, "decision chain for explainability", "no"),
    "approvals": (CONFIDENTIAL, "approval chain", "yes: approver ids"),
    "secrets": (SENSITIVE, "encrypted integration credentials / signing secrets", "no"),
    "integrations": (CONFIDENTIAL, "integration configuration (no secrets)", "no"),
    "webhook_events_in": (CONFIDENTIAL, "inbound event log (validated payload only)", "no"),
    "outbound_deliveries": (CONFIDENTIAL, "outbound event queue and delivery status", "no"),
    "llm_calls": (INTERNAL, "token usage / latency / cost accounting (prompts and replies are NOT stored)", "no"),
    "node_runs": (INTERNAL, "workflow step timing", "no"),
    "business_metrics": (INTERNAL, "computed KPIs with provenance", "no"),
    "usage_events": (INTERNAL, "usage metering", "no"),
}
DEFAULT_RETENTION_DAYS = {"node_runs": 90, "llm_calls": 365, "domain_events": 30, "webhook_events_in": 90, "outbound_deliveries": 90, "api_usage": 400}
EXPORT_TABLES = ("inventory", "vendors", "vendor_listings", "demand_history", "purchase_orders", "supplier_deliveries", "forecasts", "audit_events", "decision_traces", "approvals",
                 "integrations", "webhook_events_in", "outbound_deliveries", "llm_calls", "node_runs", "business_metrics", "usage_events", "import_jobs", "tenant_policy",
                 "tenant_policy_history", "tenant_models", "field_mappings", "id_mappings", "organizations", "warehouses", "inventory_by_warehouse", "onboarding")


def data_inventory() -> list[dict]:
    return [{"store": k, "classification": v[0], "purpose": v[1], "personal_data": v[2], "retention": DEFAULT_RETENTION_DAYS.get(k.split(".")[-1], "until tenant deletion / export")}
            for k, v in CLASSIFICATION.items()]


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def export_tenant(actor: str) -> bytes:
    """A ZIP of the tenant's own data as JSON (portability / access request). Secrets' ciphertext and key hashes are deliberately NOT included."""
    buf = io.BytesIO()
    manifest: dict[str, Any] = {"tenant_id": _tenant(), "exported_at": datetime.now().isoformat(timespec="seconds"), "tables": {}}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, db.connect() as c:
        for t in EXPORT_TABLES:
            try:
                rows = [dict(r) for r in c.execute(f"SELECT * FROM {t}")]
            except Exception:
                continue
            z.writestr(f"{t}.json", json.dumps(rows, default=str, indent=1))
            manifest["tables"][t] = len(rows)
        try:
            cp = controlplane.get()
            z.writestr("control_users.json", json.dumps(cp.list_users(_tenant()), default=str, indent=1))
            z.writestr("control_api_keys.json", json.dumps(cp.list_keys(_tenant()), default=str, indent=1))       # metadata only: hashes are not listed
        except controlplane.ControlPlaneError:
            pass
        z.writestr("MANIFEST.json", json.dumps(manifest, indent=1))
    audit.safe_record(audit.EventType.DATA_EXPORTED, None, actor, {"tables": len(manifest["tables"]), "rows": sum(manifest["tables"].values())})
    return buf.getvalue()


def purge_expired(actor: str, retention: dict[str, int] | None = None, now: datetime | None = None) -> dict:
    """Delete operational data past its retention period. Business records and the audit log are never purged here."""
    ret, now = {**DEFAULT_RETENTION_DAYS, **(retention or {})}, now or datetime.now()
    cut = lambda k: (now - timedelta(days=ret[k])).isoformat(timespec="seconds")
    out: dict[str, int] = {}
    with db.connect(write=True) as c:
        out["node_runs"] = c.execute("DELETE FROM node_runs WHERE Tenant_ID=? AND Started_At<?", (_tenant(), cut("node_runs"))).rowcount
        out["llm_calls"] = c.execute("DELETE FROM llm_calls WHERE Tenant_ID=? AND Ts<?", (_tenant(), cut("llm_calls"))).rowcount
        out["domain_events"] = c.execute("DELETE FROM domain_events WHERE Tenant_ID=? AND Published=1 AND Ts<?", (_tenant(), cut("domain_events"))).rowcount
        out["webhook_events_in"] = c.execute("DELETE FROM webhook_events_in WHERE Tenant_ID=? AND Status='PROCESSED' AND Received_At<?", (_tenant(), cut("webhook_events_in"))).rowcount
        out["outbound_deliveries"] = c.execute("DELETE FROM outbound_deliveries WHERE Tenant_ID=? AND Status='DELIVERED' AND Created_At<?", (_tenant(), cut("outbound_deliveries"))).rowcount
        audit.record(audit.EventType.DATA_PURGED, None, actor, {"retention_days": ret, "deleted": out}, conn=c)
    return out


def delete_tenant(tenant_id: str, actor: str, confirm: str) -> dict:
    """Erase a tenant: all keys revoked, record marked DELETED, the tenant directory (database, checkpoints, outbox) removed. `confirm` must equal the tenant id."""
    if confirm != tenant_id:
        raise ValueError("confirmation does not match the tenant id")
    cp = controlplane.get()
    cp.get_tenant(tenant_id)
    path = tenancy.tenant_dir(tenant_id)
    with cp.connect(write=True) as c:
        c.execute("UPDATE api_keys SET revoked_at=COALESCE(revoked_at, ?) WHERE tenant_id=?", (datetime.now().isoformat(timespec="seconds"), tenant_id))
        c.execute("UPDATE users SET status='DISABLED' WHERE tenant_id=?", (tenant_id,))
        c.execute("UPDATE tenants SET status='DELETED' WHERE tenant_id=?", (tenant_id,))
        cp.audit(tenant_id, actor, "TENANT_DELETED", {"directory_removed": True}, c)
    existed = path.exists()
    if existed:
        shutil.rmtree(path, ignore_errors=True)
    tenancy.forget_workspaces()
    return {"tenant_id": tenant_id, "deleted": True, "files_removed": existed, "note": "the platform audit trail keeps the lifecycle event; backups are not touched"}
