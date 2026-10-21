"""Stateful customer onboarding.

    create organisation -> create admin -> import data -> validate data -> configure policies -> configure suppliers
        -> run simulation -> review results -> enable production

State is persisted per tenant. Steps cannot be skipped: each one has preconditions that are re-derived from the data (an import job really exists, a policy
really was saved, a simulation really ran), so the recorded state cannot drift from reality. Production mode is the only gate to real ERP pushes, and a
tenant flagged as demo / synthetic can never be switched to it (synthetic results must not be presented as a customer's).
"""
from __future__ import annotations

import json
from datetime import datetime

from scm import audit, db, obs

from . import controlplane, policies, whatif

STEPS = ("create_organization", "create_admin", "import_data", "validate_data", "configure_policies", "configure_suppliers", "run_simulation", "review_results",
         "enable_production")
REQUIRED_IMPORTS = ("products", "suppliers", "quotes", "inventory")


class OnboardingError(Exception):
    pass


def _tenant() -> str:
    return obs.context().get("tenant_id") or "default"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    with db.connect() as c:
        r = c.execute("SELECT State_JSON FROM onboarding WHERE Tenant_ID=?", (_tenant(),)).fetchone()
    return json.loads(r[0]) if r else {"steps": {s: {"status": "PENDING"} for s in STEPS}}


def _save(state: dict) -> None:
    with db.connect(write=True) as c:
        c.execute("INSERT INTO onboarding (Tenant_ID, State_JSON, Updated_At) VALUES (?,?,?) ON CONFLICT(Tenant_ID) DO UPDATE SET State_JSON=excluded.State_JSON, "
                  "Updated_At=excluded.Updated_At", (_tenant(), json.dumps(state), _now()))


def data_quality_gate() -> dict:
    """Is the imported data good enough to plan on? Returns {ok, blocking, warnings, counts}."""
    with db.connect() as c:
        n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("inventory", "vendors", "vendor_listings", "demand_history", "supplier_deliveries")}
        no_quote = c.execute("SELECT COUNT(*) FROM inventory WHERE Item_ID NOT IN (SELECT Item_ID FROM vendor_listings WHERE Unit_Price>0)").fetchone()[0]
        bad_thr = c.execute("SELECT COUNT(*) FROM inventory WHERE Target_Stock<=Reorder_Point").fetchone()[0]
        no_hist = c.execute("SELECT COUNT(*) FROM inventory WHERE Item_ID NOT IN (SELECT DISTINCT Item_ID FROM demand_history)").fetchone()[0]
        orphan = c.execute("SELECT COUNT(*) FROM vendor_listings WHERE Vendor_ID NOT IN (SELECT Vendor_ID FROM vendors)").fetchone()[0]
    blocking, warnings = [], []
    if n["inventory"] == 0:
        blocking.append("no products/inventory imported")
    if n["vendors"] == 0:
        blocking.append("no suppliers imported")
    if n["vendor_listings"] == 0:
        blocking.append("no supplier quotes imported")
    if n["inventory"] and no_quote == n["inventory"]:
        blocking.append("no SKU has a usable supplier quote")
    if orphan:
        blocking.append(f"{orphan} quote(s) refer to a supplier that does not exist")
    if bad_thr:
        blocking.append(f"{bad_thr} SKU(s) have target stock <= reorder point")
    if n["inventory"] and no_hist:
        warnings.append(f"{no_hist} SKU(s) have no demand history: they will use the static reorder-point rule instead of a forecast")
    if n["supplier_deliveries"] == 0 and n["vendors"]:
        warnings.append("no supplier delivery history: supplier scores fall back to the reliability in the supplier master")
    if no_quote and no_quote < n["inventory"]:
        warnings.append(f"{no_quote} SKU(s) have no supplier quote and cannot be ordered")
    return {"ok": not blocking, "blocking": blocking, "warnings": warnings, "counts": n}


def _derive() -> dict[str, dict]:
    """What the data itself proves, independent of what was recorded."""
    with db.connect() as c:
        org = c.execute("SELECT COUNT(*) FROM organizations WHERE Tenant_ID=?", (_tenant(),)).fetchone()[0]
        committed = {r[0] for r in c.execute("SELECT DISTINCT Entity FROM import_jobs WHERE Tenant_ID=? AND Status='COMMITTED'", (_tenant(),))}
        pol = c.execute("SELECT Version FROM tenant_policy WHERE Tenant_ID=?", (_tenant(),)).fetchone()
        sup = c.execute("SELECT COUNT(DISTINCT Vendor_ID) FROM vendor_listings").fetchone()[0]
        sim = c.execute("SELECT COUNT(*) FROM business_metrics WHERE Tenant_ID=? AND Metric_Key='onboarding.simulation'", (_tenant(),)).fetchone()[0]
    try:
        admin = any(u["role"] in ("TENANT_ADMIN", "ADMIN") and u["status"] == "ACTIVE" for u in controlplane.get().list_users(_tenant()))
    except controlplane.ControlPlaneError:
        admin = False
    gate = data_quality_gate()
    return {"create_organization": {"ok": org > 0, "why": "an organisation record exists"}, "create_admin": {"ok": admin, "why": "an active TENANT_ADMIN user exists"},
            "import_data": {"ok": set(REQUIRED_IMPORTS) <= committed, "why": f"committed imports for {', '.join(REQUIRED_IMPORTS)}", "have": sorted(committed)},
            "validate_data": {"ok": gate["ok"], "why": "the data-quality gate passes", "gate": gate},
            "configure_policies": {"ok": pol is not None, "why": "a business policy was saved (the defaults must be confirmed explicitly by saving them)"},
            "configure_suppliers": {"ok": sup > 0, "why": "at least one supplier has a quote"},
            "run_simulation": {"ok": sim > 0, "why": "a what-if / back-test simulation was run on the imported data"}}


def create_organization(name: str, country: str | None, currency: str, actor: str) -> dict:
    if not name.strip() or len(name) > 120:
        raise OnboardingError("organisation name must be 1-120 characters")
    org_id = "org_" + obs.new_id()[:10]
    with db.connect(write=True) as c:
        c.execute("INSERT INTO organizations (Tenant_ID, Org_ID, Name, Country, Currency, Created_At) VALUES (?,?,?,?,?,?)", (_tenant(), org_id, name.strip(), country, currency.upper()[:3], _now()))
        audit.record(audit.EventType.ONBOARDING_STEP, None, actor, {"step": "create_organization", "org_id": org_id, "name": name}, conn=c)
    return {"org_id": org_id}


def run_simulation(actor: str) -> dict:
    """Runs the what-if engine on the imported data and records that it was done (the result itself is returned for the customer to review)."""
    snap = whatif.load_snapshot()
    res = whatif.compare(snap, whatif.Scenario(name="onboarding base case"))
    with db.connect(write=True) as c:
        c.execute("INSERT INTO business_metrics (Tenant_ID, Metric_Key, Period, Value, Unit, Provenance, Computed_At, Details_JSON) VALUES (?,?,?,?,?,?,?,?)",
                  (_tenant(), "onboarding.simulation", datetime.now().strftime("%Y-%m"), res["base"]["expected_cost"], "currency", "SIMULATED", _now(),
                   json.dumps({k: res["base"][k] for k in ("skus", "skus_to_order", "required_qty", "high_or_critical_risk_skus")})))
        audit.record(audit.EventType.ONBOARDING_STEP, None, actor, {"step": "run_simulation", "skus_to_order": res["base"]["skus_to_order"]}, conn=c)
    return res


def status() -> dict:
    """Recorded state reconciled with what the data proves."""
    state, derived = _load(), _derive()
    steps = []
    prev_done = True
    for s in STEPS:
        rec = state["steps"].get(s, {"status": "PENDING"})
        if s in derived:
            done = derived[s]["ok"]
        else:
            done = rec.get("status") == "DONE"
        if s == "review_results" and not derived["run_simulation"]["ok"]:
            done = False
        if s == "enable_production":
            done = _tenant_mode() == "PRODUCTION"
        steps.append({"step": s, "status": "DONE" if done else "PENDING", "blocked": not prev_done and not done, "completed_at": rec.get("completed_at"), "by": rec.get("by"),
                      "check": derived.get(s, {}).get("why"), "detail": {k: v for k, v in derived.get(s, {}).items() if k in ("gate", "have")}})
        prev_done = prev_done and done
    nxt = next((s["step"] for s in steps if s["status"] != "DONE"), None)
    return {"tenant_id": _tenant(), "mode": _tenant_mode(), "steps": steps, "next_step": nxt, "complete": nxt is None,
            "ready_for_production": all(s["status"] == "DONE" for s in steps if s["step"] != "enable_production")}


def _tenant_mode() -> str:
    try:
        return controlplane.get().get_tenant(_tenant())["mode"]
    except controlplane.NotFoundError:
        return "SANDBOX"


def confirm_policies(actor: str) -> int:
    """Explicitly accept the current (possibly default) policy so onboarding records a deliberate choice."""
    return policies.save_policy(policies.load_policy(), actor)


def acknowledge_review(actor: str, notes: str = "") -> dict:
    st = status()
    if not next(s for s in st["steps"] if s["step"] == "run_simulation")["status"] == "DONE":
        raise OnboardingError("run the simulation before reviewing its results")
    state = _load()
    state["steps"]["review_results"] = {"status": "DONE", "completed_at": _now(), "by": actor, "notes": notes[:500]}
    _save(state)
    audit.safe_record(audit.EventType.ONBOARDING_STEP, None, actor, {"step": "review_results", "notes": notes[:200]})
    return status()


def enable_production(actor: str) -> dict:
    st = status()
    if not st["ready_for_production"]:
        pending = [s["step"] for s in st["steps"] if s["status"] != "DONE" and s["step"] != "enable_production"]
        raise OnboardingError(f"production cannot be enabled yet; incomplete steps: {pending}")
    rec = controlplane.get().get_tenant(_tenant())
    if rec["is_demo"]:
        raise OnboardingError("demo / synthetic tenants cannot be switched to production: synthetic results must never be presented as a customer's")
    controlplane.get().update_tenant(_tenant(), actor, mode="PRODUCTION")
    audit.safe_record(audit.EventType.TENANT_MODE_CHANGED, None, actor, {"mode": "PRODUCTION"})
    state = _load()
    state["steps"]["enable_production"] = {"status": "DONE", "completed_at": _now(), "by": actor}
    _save(state)
    return status()
