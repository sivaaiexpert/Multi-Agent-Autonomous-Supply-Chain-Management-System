"""Platform endpoints: forecasts, risks, procurement, approvals, imports, integrations, webhooks, simulations, business impact, reports, usage, dashboards, admin.

Every handler: (1) checks the role's permission, (2) enters the caller's tenant scope, (3) calls the platform module. Nothing here contains business logic.
"""
import base64
import binascii
from typing import Annotated, Literal

from fastapi import Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from enterprise import mapping as colmap
from enterprise import (approvals, comparison, controlplane, dashboards, impact, importer, onboarding, outbound, policies, privacy, reports, roi, tenancy, trace, usage, webhooks, whatif)
from enterprise.integrations import ADAPTER_STATUS, dispatch, registry, sync
from scm import audit, db, forecast
from scm.dashboard import inventory_risk
from scm.security import Perm, PermissionDenied, Principal, Role, valid_id
from scm.services import InvalidRequest

from .common import API_PREFIX, error, rid_of

P = API_PREFIX


# ---------------------------------------------------------------------------------------------- request models
class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    filename: str = Field("upload.csv", max_length=200)
    format: Literal["csv", "json", "xlsx"] | None = None
    content_base64: str | None = None
    records: list[dict] | None = Field(None, max_length=50_000)
    mapping: colmap.FieldMapping | None = None
    allow_partial: bool = True
    max_reject_rate: float = Field(0.5, ge=0, le=1)
    unknown_sku: Literal["reject", "create"] = "reject"
    unmapped_ids: Literal["pass", "reject"] = "pass"


class SuggestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity: str
    columns: list[str] = Field(max_length=200)


class IdMapBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairs: dict[str, str] = Field(max_length=20_000)


class IntegrationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["erp_mock", "erp_rest", "webhook_in"]
    name: str = Field(max_length=120)
    config: dict = Field(default_factory=dict)
    secret: str | None = Field(None, max_length=500)


class SubscriptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=500)
    event_types: list[str] = Field(max_length=20)


class RoiBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assumptions: roi.RoiAssumptions = roi.RoiAssumptions()
    baseline_arm: Literal["manual", "rule_based"] = "manual"
    platform_arm: Literal["ai", "ai_lean"] = "ai_lean"


class TenantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    name: str = Field(max_length=120)
    plan: Literal["pilot", "professional", "enterprise"] = "pilot"
    is_demo: bool = False
    admin_user_id: str = Field(max_length=80)
    admin_email: str = Field(max_length=200)


class TenantPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["pilot", "professional", "enterprise"] | None = None
    status: Literal["ACTIVE", "SUSPENDED"] | None = None
    name: str | None = Field(None, max_length=120)


class UserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=80)
    email: str = Field(max_length=200)
    display_name: str = Field(max_length=120)
    role: Role


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field("", max_length=80)
    ttl_days: int | None = Field(None, ge=1, le=3650)


class OrgBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=120)
    country: str | None = Field(None, max_length=60)
    currency: str = Field("USD", pattern="^[A-Za-z]{3}$")


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = Field("", max_length=500)


def register(app, ctx, Auth, Page) -> None:
    S = ctx.scoped

    # ================================================================== forecasts / risks / procurement
    @app.get(f"{P}/forecasts", tags=["forecasts"], summary="Latest forecast per SKU")
    def forecasts(who: Auth, pg: Page):
        who.require(Perm.READ)
        with S(who):
            rows = forecast.latest_forecasts(1000)
            return {"items": rows[pg[1]:pg[1] + pg[0]], "total": len(rows), "limit": pg[0], "offset": pg[1]}

    @app.get(f"{P}/risks", tags=["risks"], summary="Current stock-out risk per SKU (deterministic engine)")
    def risks(who: Auth, pg: Page, level: str | None = Query(None, pattern="^(LOW|MEDIUM|HIGH|CRITICAL)$")):
        who.require(Perm.READ)
        with S(who):
            rows = [r for r in inventory_risk() if not level or r["risk_level"] == level]
            return {"items": rows[pg[1]:pg[1] + pg[0]], "total": len(rows), "limit": pg[0], "offset": pg[1]}

    @app.get(f"{P}/procurement/orders", tags=["procurement"])
    def orders(who: Auth, pg: Page, status: str | None = Query(None, pattern="^[A-Z_]{3,20}$")):
        who.require(Perm.READ)
        with S(who), db.connect() as c:
            where, params = ("WHERE Status=?", [status]) if status else ("", [])
            total = c.execute(f"SELECT COUNT(*) FROM purchase_orders {where}", params).fetchone()[0]
            rows = [dict(r) for r in c.execute(f"SELECT PO_ID, Item_ID, Vendor_ID, Quantity, Unit_Price, Status, Timestamp, Workflow_ID FROM purchase_orders {where} "
                                               "ORDER BY Timestamp DESC, PO_ID DESC LIMIT ? OFFSET ?", (*params, pg[0], pg[1]))]
            return {"items": rows, "total": total, "limit": pg[0], "offset": pg[1]}

    @app.get(f"{P}/procurement/comparison", tags=["procurement"], summary="Baseline vs optimised decision on current data (methodology included)")
    def procurement_comparison(who: Auth):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return comparison.baseline_vs_optimized()

    @app.get(P + "/workflows/{workflow_id}/trace", tags=["procurement"], summary="Complete decision chain for one procurement decision")
    def decision_trace(workflow_id: str, who: Auth):
        who.require(Perm.VIEW_TRACE)
        if not valid_id(workflow_id):
            raise InvalidRequest("malformed workflow id")
        with S(who):
            tr = trace.full(workflow_id)
            if tr is None:
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "no decision trace for that workflow", "request_id": None}})
            return tr

    @app.get(P + "/workflows/{workflow_id}/explanation", tags=["procurement"], summary="Human-readable reasons, generated from stored data only")
    def explanation(workflow_id: str, who: Auth):
        who.require(Perm.READ)
        if not valid_id(workflow_id):
            raise InvalidRequest("malformed workflow id")
        with S(who):
            tr = trace.load(workflow_id)
            if tr is None:
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "no decision trace for that workflow", "request_id": None}})
            return {"workflow_id": workflow_id, "explanations": trace.explain_all(tr), "data_provenance": tr["data_provenance"]}

    # ================================================================== approvals
    @app.get(f"{P}/approvals", tags=["approvals"], summary="Workflows whose current approval step the caller may action")
    def approvals_inbox(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return {"items": approvals.ApprovalService(ctx.service(who.tenant_id)).inbox(who)}

    @app.get(P + "/approvals/{workflow_id}", tags=["approvals"])
    def approval_steps(workflow_id: str, who: Auth):
        who.require(Perm.READ)
        with S(who):
            return {"workflow_id": workflow_id, "steps": approvals.ApprovalService(ctx.service(who.tenant_id)).steps(workflow_id, include_superseded=True)}

    @app.post(f"{P}/approvals/process-overdue", tags=["approvals"], summary="Escalate overdue steps and reject timed-out workflows")
    def approvals_overdue(who: Auth):
        who.require(Perm.MANAGE_POLICIES)
        with S(who):
            return approvals.ApprovalService(ctx.service(who.tenant_id)).process_overdue()

    # ================================================================== imports and mapping
    def _entity(entity: str) -> str:
        if entity not in colmap.ENTITIES:
            raise InvalidRequest(f"entity must be one of {list(colmap.ENTITIES)}")
        return entity

    def _content(body: ImportBody) -> tuple[bytes, str, str | None]:
        if (body.content_base64 is None) == (body.records is None):
            raise InvalidRequest("provide exactly one of content_base64 or records")
        if body.records is not None:
            import json
            return json.dumps({"records": body.records}).encode(), body.filename if body.filename.endswith(".json") else "records.json", "json"
        try:
            return base64.b64decode(body.content_base64 or "", validate=True), body.filename, body.format
        except (binascii.Error, ValueError) as exc:
            raise InvalidRequest("content_base64 is not valid base64") from exc

    @app.post(f"{P}/imports/preview", tags=["imports"], summary="Validate an upload and return a data-quality report (writes nothing to business tables)")
    def import_preview(body: ImportBody, who: Auth):
        who.require(Perm.IMPORT_DATA)
        content, name, fmt = _content(body)
        opts = importer.ImportOptions(body.allow_partial, body.max_reject_rate, body.unknown_sku, body.unmapped_ids)
        with S(who):
            try:
                return importer.preview(body.entity, content, name, who.actor, body.mapping, opts, fmt)
            except importer.ImportError_ as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.post(P + "/imports/{job_id}/commit", tags=["imports"], summary="Atomically commit a previewed import")
    def import_commit(job_id: str, who: Auth):
        who.require(Perm.IMPORT_DATA)
        with S(who):
            try:
                return importer.commit(job_id, who.actor)
            except importer.ImportBlocked as exc:
                return JSONResponse(status_code=409, content={"error": {"code": "import_blocked", "message": str(exc), "request_id": None}})
            except importer.ImportError_ as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.get(f"{P}/imports", tags=["imports"])
    def import_list(who: Auth, pg: Page):
        who.require(Perm.READ)
        with S(who):
            return {"items": importer.list_jobs(pg[0], pg[1]), "limit": pg[0], "offset": pg[1]}

    @app.get(P + "/imports/{job_id}", tags=["imports"])
    def import_get(job_id: str, who: Auth):
        who.require(Perm.READ)
        with S(who):
            j = importer.get_job(job_id)
            if j is None:
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "import job not found", "request_id": None}})
            return j

    @app.post(f"{P}/mappings/suggest", tags=["imports"], summary="Suggest a column mapping for a customer's headers")
    def mapping_suggest(body: SuggestBody, who: Auth):
        who.require(Perm.IMPORT_DATA)
        try:
            return colmap.suggest_mapping(body.entity, body.columns)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc

    @app.get(P + "/mappings/{entity}", tags=["imports"])
    def mapping_get(entity: str, who: Auth):
        who.require(Perm.READ)
        _entity(entity)
        with S(who):
            m = colmap.load_mapping(entity)
            return m.model_dump() if m else {"columns": {}, "transforms": {}}

    @app.put(P + "/mappings/{entity}", tags=["imports"])
    def mapping_put(entity: str, body: colmap.FieldMapping, who: Auth):
        who.require(Perm.IMPORT_DATA)
        _entity(entity)
        with S(who):
            try:
                colmap.save_mapping(entity, body, who.actor)
            except ValueError as exc:
                raise InvalidRequest(str(exc)) from exc
            return {"saved": True, "entity": entity}

    @app.put(P + "/mappings/ids/{kind}", tags=["imports"], summary="Customer id -> internal id (kind: sku | supplier)")
    def id_mappings_put(kind: Literal["sku", "supplier"], body: IdMapBody, who: Auth):
        who.require(Perm.IMPORT_DATA)
        with S(who):
            return {"stored": colmap.save_id_mappings(kind, body.pairs, who.actor)}

    # ================================================================== integrations
    @app.get(f"{P}/integrations/adapters", tags=["integrations"], summary="Which ERP adapters exist and how far they were validated")
    def adapters(who: Auth):
        who.require(Perm.READ)
        return ADAPTER_STATUS

    @app.get(f"{P}/integrations", tags=["integrations"])
    def integrations_list(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return {"items": registry.list_all()}

    @app.post(f"{P}/integrations", status_code=201, tags=["integrations"], summary="Create an integration (secrets are encrypted and never returned)")
    def integrations_create(body: IntegrationBody, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                return registry.create(body.kind, body.name, body.config, body.secret, who.actor)
            except registry.IntegrationError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.delete(P + "/integrations/{integration_id}", tags=["integrations"])
    def integrations_delete(integration_id: str, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                registry.delete(integration_id, who.actor)
            except registry.IntegrationError as exc:
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": str(exc), "request_id": None}})
            return {"deleted": True}

    @app.post(P + "/integrations/{integration_id}/sync/{entity}", tags=["integrations"], summary="Pull inventory / suppliers / purchase_orders from the ERP through the validated importer")
    def integrations_sync(integration_id: str, entity: Literal["inventory", "suppliers", "purchase_orders"], who: Auth, apply: bool = True):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                adapter = registry.adapter_for(integration_id)
                fn = {"inventory": sync.sync_inventory, "suppliers": sync.sync_suppliers, "purchase_orders": sync.sync_purchase_orders}[entity]
                return fn(adapter, who.actor, apply)
            except registry.IntegrationError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.get(P + "/integrations/{integration_id}/reconcile", tags=["integrations"], summary="Compare ERP stock with the platform's books (writes nothing)")
    def integrations_reconcile(integration_id: str, who: Auth, tolerance_units: int = Query(0, ge=0)):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                return sync.reconcile_inventory(registry.adapter_for(integration_id), tolerance_units)
            except registry.IntegrationError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.post(P + "/integrations/{integration_id}/push/{workflow_id}", tags=["integrations"], summary="(Re)push an approved workflow's purchase orders to the ERP")
    def integrations_push(integration_id: str, workflow_id: str, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                return dispatch.push_workflow(workflow_id, integration_id, who.actor)
            except registry.IntegrationError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.get(f"{P}/integrations/dispatches", tags=["integrations"])
    def integrations_dispatches(who: Auth, workflow_id: str | None = None):
        who.require(Perm.READ)
        with S(who):
            return {"items": dispatch.status(workflow_id)}

    @app.get(f"{P}/events/subscriptions", tags=["events"])
    def subs_list(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return {"items": outbound.list_subscriptions(), "event_types": list(outbound.EVENT_TYPES)}

    @app.post(f"{P}/events/subscriptions", status_code=201, tags=["events"], summary="Subscribe a URL to outbound events (signing secret shown once)")
    def subs_create(body: SubscriptionBody, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                return outbound.subscribe(body.url, body.event_types, who.actor)
            except ValueError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.delete(P + "/events/subscriptions/{sub_id}", tags=["events"])
    def subs_delete(sub_id: str, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                outbound.unsubscribe(sub_id, who.actor)
            except ValueError as exc:
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": str(exc), "request_id": None}})
            return {"unsubscribed": True}

    @app.get(f"{P}/events/deliveries", tags=["events"], summary="Outbound delivery status, retries and failures")
    def deliveries(who: Auth, pg: Page, status: str | None = Query(None, pattern="^(PENDING|DELIVERED|FAILED|DEAD)$")):
        who.require(Perm.READ)
        with S(who):
            return {"items": outbound.deliveries(status, pg[0], pg[1]), "limit": pg[0], "offset": pg[1]}

    @app.post(P + "/events/deliveries/{delivery_id}/replay", tags=["events"])
    def deliveries_replay(delivery_id: int, who: Auth):
        who.require(Perm.MANAGE_INTEGRATIONS)
        with S(who):
            try:
                outbound.replay(delivery_id, who.actor)
            except ValueError as exc:
                raise InvalidRequest(str(exc)) from exc
            return {"replayed": True}

    @app.get(f"{P}/webhooks/events", tags=["webhooks"], summary="Inbound webhook event log")
    def webhook_log(who: Auth, pg: Page, status: str | None = Query(None, pattern="^(RECEIVED|PROCESSED|FAILED|DEAD)$")):
        who.require(Perm.READ)
        with S(who):
            return {"items": webhooks.events(status, pg[0], pg[1]), "limit": pg[0], "offset": pg[1]}

    @app.post(P + "/webhooks/inbound/{tenant_id}/{source}", tags=["webhooks"], summary="Receive a signed event from an external system (no API key: HMAC signature)")
    async def webhook_inbound(tenant_id: str, source: str, request: Request):
        rid = rid_of(request)
        ip = request.client.host if request.client else "?"
        ok, wait = ctx.limiter.allow(f"wh:{ip}:{tenant_id}")
        if not ok:
            return JSONResponse(status_code=429, content={"error": {"code": "rate_limited", "message": "too many requests", "request_id": rid}}, headers={"Retry-After": str(int(wait) + 1)})
        body = await request.body()
        headers = dict(request.headers)

        def work() -> JSONResponse:
            try:
                with tenancy.scope(tenant_id):
                    res = webhooks.receive(source, body, headers)
                return JSONResponse(status_code=200 if res["status"] in ("processed", "duplicate") else 202, content=res)
            except webhooks.WebhookError as exc:
                return error(exc.status, exc.code, str(exc), rid)
            except (controlplane.ControlPlaneError, ValueError):                 # unknown / suspended / malformed tenant: indistinguishable from a bad signature
                return error(401, "unauthorized", "signature verification failed", rid)
        return await run_in_threadpool(work)

    # ================================================================== simulation and impact
    @app.post(f"{P}/simulations/what-if", tags=["simulations"], summary="Deterministic what-if: demand, stock, supplier price / capacity / lead time, budget, risk tolerance")
    def what_if(body: whatif.Scenario, who: Auth):
        who.require(Perm.RUN_SIMULATION)
        with S(who):
            return whatif.compare(whatif.load_snapshot(), body)

    @app.post(f"{P}/simulations/backtest", tags=["simulations"], summary="Replay the tenant's own history through manual / rule-based / AI policy arms")
    def backtest(who: Auth, eval_days: int = Query(60, ge=14, le=180)):
        who.require(Perm.RUN_SIMULATION)
        with S(who):
            return impact.inventory_kpis(True, __import__("enterprise.simulation", fromlist=["x"]).SimConfig(eval_days=eval_days))

    @app.get(f"{P}/impact/procurement", tags=["impact"])
    def impact_procurement(who: Auth, baseline: Literal["manual_buyer", "average_quote", "last_paid"] = "manual_buyer"):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return impact.procurement_kpis(baseline)

    @app.get(f"{P}/impact/forecast", tags=["impact"])
    def impact_forecast(who: Auth, horizon: int = Query(14, ge=1, le=60), origins: int = Query(4, ge=1, le=12)):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return impact.forecast_accuracy(horizon, origins)

    @app.get(f"{P}/impact/suppliers", tags=["impact"])
    def impact_suppliers(who: Auth):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return impact.supplier_kpis()

    @app.get(f"{P}/impact/operations", tags=["impact"])
    def impact_operations(who: Auth, manual_minutes_per_line: float = Query(12.0, ge=0, le=600)):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return impact.operations_kpis(manual_minutes_per_line)

    @app.post(f"{P}/impact/snapshot", tags=["impact"], summary="Compute and store the headline KPIs")
    def impact_snapshot(who: Auth):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            return {"metrics": [m.to_dict() for m in impact.snapshot_all()]}

    @app.post(f"{P}/impact/roi", tags=["impact"], summary="ROI with explicit assumptions (undefined when no cost basis is given)")
    def impact_roi(body: RoiBody, who: Auth):
        who.require(Perm.VIEW_REPORTS)
        with S(who):
            from enterprise import simulation as sim
            inv = impact.inventory_kpis(True, sim.SimConfig(eval_days=body.assumptions.window_days, gross_margin=body.assumptions.gross_margin))
            bt = inv.get("backtest", {})
            if "arms" not in bt:
                raise InvalidRequest(bt.get("unavailable", "no back-test available: import historical demand first"))
            return roi.compute(bt["arms"][body.baseline_arm], bt["arms"][body.platform_arm], body.assumptions, bt["provenance"], impact.data_provenance())

    # ================================================================== reports, usage, dashboards
    @app.get(P + "/reports/{kind}", tags=["reports"], summary="Download a report (csv | json | md | html)")
    def report(kind: Literal["monthly_procurement", "supplier_performance", "forecast_accuracy", "inventory_risk", "savings", "ai_usage", "audit"], who: Auth,
               format: Literal["csv", "json", "md", "html"] = "csv", period: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
        who.require(Perm.VIEW_REPORTS)
        if kind == "audit":
            who.require(Perm.READ_AUDIT)
        with S(who):
            rep = reports.generate(kind, period)
            body, media = reports.render(rep, format)
            ext = {"md": "md"}.get(format, format)
            return Response(content=body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{kind}-{rep.meta["period"]}.{ext}"'})

    @app.get(f"{P}/usage", tags=["usage"], summary="Metered usage, plan quotas and cost (billing-ready, no prices)")
    def usage_get(who: Auth):
        who.require(Perm.VIEW_USAGE)
        with S(who):
            return usage.usage_report()

    @app.get(P + "/usage/workflows/{workflow_id}", tags=["usage"])
    def usage_workflow(workflow_id: str, who: Auth):
        who.require(Perm.VIEW_USAGE)
        if not valid_id(workflow_id):
            raise InvalidRequest("malformed workflow id")
        with S(who):
            return usage.workflow_cost(workflow_id)

    @app.get(P + "/dashboards/{name}", tags=["dashboards"])
    def dashboard(name: Literal["system", "ai", "business", "tenant"], who: Auth):
        who.require(Perm.VIEW_USAGE if name in ("system", "tenant") else Perm.READ)
        with S(who):
            return {"system": dashboards.system, "ai": dashboards.ai, "business": dashboards.business, "tenant": dashboards.tenant}[name]()

    # ================================================================== administration
    def cp() -> controlplane.ControlPlane:
        return controlplane.get()

    @app.post(f"{P}/admin/tenants", status_code=201, tags=["admin"], summary="Create a customer tenant + its first administrator (platform operators only)")
    def tenant_create(body: TenantBody, who: Auth):
        who.require(Perm.MANAGE_TENANTS)
        rec = tenancy.provision(body.tenant_id, body.name, body.plan, "SANDBOX", body.is_demo, who.actor)
        cp().create_user(body.tenant_id, body.admin_user_id, body.admin_email, "Tenant administrator", Role.TENANT_ADMIN, who)
        key, key_rec = cp().issue_key(body.tenant_id, body.admin_user_id, "initial administrator key", issued_by=who)
        return {"tenant": rec, "admin_api_key_shown_once": key, "key": key_rec}

    @app.get(f"{P}/admin/tenants", tags=["admin"])
    def tenant_list(who: Auth):
        who.require(Perm.MANAGE_TENANTS)
        return {"items": cp().list_tenants()}

    @app.patch(P + "/admin/tenants/{tenant_id}", tags=["admin"])
    def tenant_patch(tenant_id: str, body: TenantPatch, who: Auth):
        who.require(Perm.MANAGE_TENANTS)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        return cp().update_tenant(tenant_id, who.actor, **fields)

    @app.delete(P + "/admin/tenants/{tenant_id}", tags=["admin"], summary="Erase a tenant (confirm must equal the tenant id)")
    def tenant_delete(tenant_id: str, who: Auth, confirm: str = Query(...)):
        who.require(Perm.MANAGE_TENANTS)
        return privacy.delete_tenant(tenant_id, who.actor, confirm)

    @app.get(f"{P}/admin/users", tags=["admin"])
    def users_list(who: Auth):
        who.require(Perm.MANAGE_USERS)
        return {"items": cp().list_users(who.tenant_id)}

    @app.post(f"{P}/admin/users", status_code=201, tags=["admin"])
    def users_create(body: UserBody, who: Auth):
        who.require(Perm.MANAGE_USERS)
        return cp().create_user(who.tenant_id, body.user_id, body.email, body.display_name, body.role, who)

    @app.put(P + "/admin/users/{user_id}/status", tags=["admin"])
    def users_status(user_id: str, who: Auth, status: Literal["ACTIVE", "DISABLED"] = Query(...)):
        who.require(Perm.MANAGE_USERS)
        if user_id == who.actor and status == "DISABLED":
            raise InvalidRequest("you cannot disable your own account")
        cp().set_user_status(who.tenant_id, user_id, status, who.actor)
        return {"user_id": user_id, "status": status}

    @app.post(P + "/admin/users/{user_id}/keys", status_code=201, tags=["admin"], summary="Issue an API key (shown once; only its hash is stored)")
    def keys_issue(user_id: str, body: KeyBody, who: Auth):
        who.require(Perm.MANAGE_USERS)
        key, rec = cp().issue_key(who.tenant_id, user_id, body.label, body.ttl_days, who)
        return {"api_key_shown_once": key, "key": rec}

    @app.get(f"{P}/admin/keys", tags=["admin"])
    def keys_list(who: Auth):
        who.require(Perm.MANAGE_USERS)
        return {"items": cp().list_keys(who.tenant_id)}

    @app.delete(P + "/admin/keys/{key_id}", tags=["admin"])
    def keys_revoke(key_id: str, who: Auth):
        who.require(Perm.MANAGE_USERS)
        cp().revoke_key(who.tenant_id, key_id, who.actor)
        return {"revoked": True}

    @app.get(f"{P}/admin/policy", tags=["admin"], summary="The tenant's business policy (limits, thresholds, approval tiers)")
    def policy_get(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return {"version": policies.policy_version(), "policy": policies.load_policy().model_dump()}

    @app.put(f"{P}/admin/policy", tags=["admin"])
    def policy_put(body: policies.TenantPolicy, who: Auth):
        who.require(Perm.MANAGE_POLICIES)
        with S(who):
            return {"version": policies.save_policy(body, who.actor)}

    @app.get(f"{P}/admin/policy/history", tags=["admin"])
    def policy_history(who: Auth):
        who.require(Perm.MANAGE_POLICIES)
        with S(who):
            return {"items": policies.policy_history()}

    @app.get(f"{P}/admin/model-config", tags=["admin"])
    def model_get(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return policies.load_model_config().model_dump()

    @app.put(f"{P}/admin/model-config", tags=["admin"], summary="Choose approved models; safety and authorisation controls cannot be configured here")
    def model_put(body: policies.ModelConfig, who: Auth):
        who.require(Perm.MANAGE_POLICIES)
        with S(who):
            policies.save_model_config(body, who.actor)
            return body.model_dump()

    @app.get(f"{P}/admin/onboarding", tags=["onboarding"])
    def onboarding_status(who: Auth):
        who.require(Perm.READ)
        with S(who):
            return onboarding.status()

    @app.post(f"{P}/admin/onboarding/organization", status_code=201, tags=["onboarding"])
    def onboarding_org(body: OrgBody, who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            return onboarding.create_organization(body.name, body.country, body.currency, who.actor)

    @app.post(f"{P}/admin/onboarding/confirm-policies", tags=["onboarding"])
    def onboarding_policies(who: Auth):
        who.require(Perm.MANAGE_POLICIES)
        with S(who):
            return {"version": onboarding.confirm_policies(who.actor)}

    @app.post(f"{P}/admin/onboarding/simulation", tags=["onboarding"])
    def onboarding_sim(who: Auth):
        who.require(Perm.RUN_SIMULATION)
        with S(who):
            return onboarding.run_simulation(who.actor)

    @app.post(f"{P}/admin/onboarding/review", tags=["onboarding"])
    def onboarding_review(body: ReviewBody, who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            try:
                return onboarding.acknowledge_review(who.actor, body.notes)
            except onboarding.OnboardingError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.post(f"{P}/admin/onboarding/production", tags=["onboarding"], summary="Enable production mode (only after every step is complete; never for demo tenants)")
    def onboarding_production(who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            try:
                return onboarding.enable_production(who.actor)
            except onboarding.OnboardingError as exc:
                raise InvalidRequest(str(exc)) from exc

    @app.get(f"{P}/admin/privacy/inventory", tags=["privacy"], summary="What data is stored, its classification, purpose and retention")
    def privacy_inventory(who: Auth):
        who.require(Perm.CONFIGURE)
        return {"items": privacy.data_inventory(), "notice": "technical controls that support compliance requirements; they do not by themselves make a deployment compliant"}

    @app.get(f"{P}/admin/privacy/export", tags=["privacy"], summary="Export the tenant's data (ZIP of JSON; no secrets, no key hashes)")
    def privacy_export(who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            return Response(content=privacy.export_tenant(who.actor), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{who.tenant_id}-export.zip"'})

    @app.post(f"{P}/admin/privacy/purge", tags=["privacy"], summary="Delete operational data past its retention period (business data and audit are kept)")
    def privacy_purge(who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            return privacy.purge_expired(who.actor)

    @app.post(f"{P}/admin/maintenance/run", tags=["admin"], summary="Run the periodic jobs for the caller's tenant now")
    def maintenance_run(who: Auth):
        who.require(Perm.CONFIGURE)
        with S(who):
            return {"outbound": outbound.deliver_due(), "webhooks": webhooks.process_pending(), "approvals": approvals.ApprovalService(ctx.service(who.tenant_id)).process_overdue()}

    _ = (PermissionDenied, Principal, audit, Annotated)
