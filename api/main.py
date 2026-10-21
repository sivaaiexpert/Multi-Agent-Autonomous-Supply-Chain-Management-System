"""HTTP API (FastAPI): multi-tenant, versioned under /api/v1.

    uvicorn api.main:app --host 0.0.0.0 --port 8000        docs: /docs   OpenAPI: /openapi.json

Thin layer: authentication, tenant scoping, validation, error mapping. All logic lives in `scm` (engine) and `enterprise` (platform).
Auth: `X-API-Key: <key>` or `Authorization: Bearer <key>`. Keys are issued per user by a tenant admin (control plane, SHA-256 hashed at rest); legacy
SCM_API_KEYS entries map to the `default` tenant. The tenant of a request is ALWAYS the authenticated key's tenant: no header or parameter can select another.
"""
import asyncio
import contextlib
import os
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from enterprise import approvals, controlplane, dashboards, hooks, maintenance, outbound, secrets_store, tenancy
from enterprise.integrations import dispatch
from scm import audit, db, forecast, migrations, obs, suppliers
from scm.llm_router import METRICS
from scm.security import Perm, PermissionDenied, Principal, UnsafeUrl, valid_id
from scm.services import Conflict, InvalidRequest, NotFound, WorkflowService

from . import routes_platform
from .common import API_PREFIX, MAX_PAGE, error, page, rid_of, route_group
from .context import AppContext

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("SCM_API_TIMEOUT", "30"))
MAX_BODY_DEFAULT = 1 * 1024 * 1024
MAX_BODY_IMPORT = 32 * 1024 * 1024


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field("", max_length=500)
    edits: dict[str, dict[str, int]] | None = Field(None, description="approve only: {po_id: {'quantity': int}}")


def create_app(service: WorkflowService | None = None, keyring: dict[str, Principal] | None = None, background: bool | None = None) -> FastAPI:
    ctx = AppContext(service, keyring)
    maint = maintenance.Background(float(os.environ.get("SCM_MAINTENANCE_INTERVAL", "30")), lambda tid: ctx.service(tid))
    run_background = background if background is not None else os.environ.get("SCM_BACKGROUND", "0") == "1"

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        secrets_store.assert_startup_ok()               # production refuses to start without its platform secret
        db.init_db()                                    # idempotent: creates the schema on a new database and applies pending migrations
        controlplane.get().ensure_default_tenant()
        hooks.install_all()
        dispatch.install_hooks()
        outbound.install_event_bridge()
        if run_background:
            maint.start()
        yield
        maint.stop()
        controlplane.flush_api_usage()
        ctx.shutdown()

    app = FastAPI(title="Autonomous Supply Chain Platform API", version="2.0.0", docs_url="/docs", redoc_url=None, lifespan=lifespan,
                  description="Multi-tenant procurement and supply-chain operations platform. LLMs are advisory; deterministic engines and humans decide.")
    app.state.ctx = ctx
    origins = [o.strip() for o in os.environ.get("SCM_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:                                          # CORS is OFF unless explicitly configured; never with credentials
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["X-API-Key", "Authorization", "Content-Type", "Idempotency-Key"], allow_credentials=False)

    # ---------------------------------------------------------------- middleware / errors
    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = request.headers.get("x-request-id") or obs.new_id()
        if not valid_id(rid):
            rid = obs.new_id()
        request.state.request_id = rid
        token = obs.bind(request_id=rid, trace_id=rid)
        t0 = time.perf_counter()
        try:
            if request.method in ("POST", "PUT", "PATCH"):
                cl = request.headers.get("content-length")
                limit = MAX_BODY_IMPORT if request.url.path.startswith(f"{API_PREFIX}/imports") else MAX_BODY_DEFAULT
                if cl is None:
                    response = error(411, "length_required", "Content-Length is required", rid)
                elif not cl.isdigit() or int(cl) > limit:
                    response = error(413, "payload_too_large", f"request body larger than {limit // (1024 * 1024)} MB", rid)
                else:
                    response = await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
            else:
                response = await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            response = error(504, "timeout", f"request exceeded {REQUEST_TIMEOUT_SECONDS:g}s", rid)
        finally:
            obs.unbind(token)
        ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        tenant = getattr(request.state, "tenant", "-")
        dashboards.API_METRICS.record(route_group(request.url.path), response.status_code, ms, tenant)
        if tenant != "-":
            controlplane.buffer_api_call(tenant, route_group(request.url.path), ms, response.status_code >= 500)
        obs.log_event("http", method=request.method, path=request.url.path, status=response.status_code, duration_ms=round(ms, 1), request_id=rid, tenant=tenant)
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        resp = error(exc.status_code, "http_error", str(exc.detail), rid_of(request))
        for k, v in (exc.headers or {}).items():
            resp.headers[k] = v
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        return error(422, "validation_error", msg, rid_of(request))

    @app.exception_handler(PermissionDenied)
    async def _denied(request: Request, exc: PermissionDenied):
        ctx.deny(request, str(exc))
        return error(403, "forbidden", str(exc), rid_of(request))

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return error(404, "not_found", str(exc), rid_of(request))

    @app.exception_handler(controlplane.NotFoundError)
    async def _cp_nf(request: Request, exc: controlplane.NotFoundError):
        return error(404, "not_found", str(exc), rid_of(request))

    @app.exception_handler(Conflict)
    async def _conflict(request: Request, exc: Conflict):
        return error(409, "conflict", str(exc), rid_of(request))

    @app.exception_handler(controlplane.ConflictError)
    async def _cp_conflict(request: Request, exc: controlplane.ConflictError):
        return error(409, "conflict", str(exc), rid_of(request))

    @app.exception_handler(InvalidRequest)
    async def _invalid(request: Request, exc: InvalidRequest):
        return error(422, "invalid_request", str(exc), rid_of(request))

    @app.exception_handler(UnsafeUrl)
    async def _unsafe(request: Request, exc: UnsafeUrl):
        return error(422, "unsafe_url", str(exc), rid_of(request))

    @app.exception_handler(controlplane.ControlPlaneError)
    async def _cp_err(request: Request, exc: controlplane.ControlPlaneError):
        return error(422, "invalid_request", str(exc), rid_of(request))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        obs.log_event("unhandled error", level=40, error=f"{type(exc).__name__}: {exc}")
        return error(500, "internal_error", "internal server error", rid_of(request))      # never leak internals

    Auth = Annotated[Principal, Depends(ctx.principal)]
    Page = Annotated[tuple[int, int], Depends(page)]

    # ---------------------------------------------------------------- public
    @app.get("/health", tags=["ops"], summary="Liveness")
    def health():
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"], summary="Readiness: database, migrations, secrets")
    def ready():
        checks: dict = {}
        try:
            with tenancy.scope("default"), db.connect() as c:
                checks["database"] = "ok"
                checks["migrations"] = "ok" if migrations.current_version(c) >= migrations.LATEST else "behind"
            checks["control_plane"] = "ok" if controlplane.get().list_tenants() is not None else "error"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
        secret_checks = secrets_store.validate_startup()
        failed = [r["name"] for r in secret_checks if r["status"] == "FAIL"]
        checks["secrets"] = "ok" if not failed else "FAIL: " + "; ".join(failed)
        checks["api_keys"] = "configured" if ctx.keyring() or controlplane.get().has_active_keys() else "none configured (all protected endpoints will refuse)"
        ok = checks.get("database") == "ok" and checks.get("migrations") == "ok" and checks["secrets"] == "ok"
        return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "not_ready", "checks": checks})

    # ---------------------------------------------------------------- workflows
    @app.post(f"{API_PREFIX}/workflows", status_code=202, tags=["workflows"], summary="Start a procurement workflow (runs in a worker; poll the returned id)")
    def create_workflow(who: Auth, idempotency_key: Annotated[str | None, Header()] = None):
        who.require(Perm.RUN_WORKFLOW)
        with ctx.scoped(who):
            from enterprise import usage
            usage.check_quota("workflows_per_month")
            run, created = ctx.service(who.tenant_id).start(who, idempotency_key)
            return JSONResponse(status_code=202 if created else 200, content=run, headers={"Location": f"{API_PREFIX}/workflows/{run['workflow_id']}"})

    @app.get(f"{API_PREFIX}/workflows", tags=["workflows"])
    def list_workflows(who: Auth, pg: Page, status: str | None = Query(None, pattern="^[A-Z_]{3,20}$")):
        who.require(Perm.READ)
        with ctx.scoped(who):
            items, total = ctx.service(who.tenant_id).list(pg[0], pg[1], status)
            return {"items": items, "total": total, "limit": pg[0], "offset": pg[1]}

    @app.get(API_PREFIX + "/workflows/{workflow_id}", tags=["workflows"])
    def get_workflow(workflow_id: str, who: Auth):
        who.require(Perm.READ)
        with ctx.scoped(who):
            return ctx.service(who.tenant_id).get(workflow_id)

    def _decide(workflow_id: str, who: Principal, action: str, body: DecisionBody):
        with ctx.scoped(who):
            run = approvals.ApprovalService(ctx.service(who.tenant_id)).act(who, workflow_id, action, body.reason, body.edits if action == "approve" else None)
            return JSONResponse(status_code=202, content=run)

    @app.post(API_PREFIX + "/workflows/{workflow_id}/approve", status_code=202, tags=["workflows"], summary="Approve (one step of the tenant's approval chain)")
    def approve(workflow_id: str, who: Auth, body: DecisionBody = DecisionBody()):
        return _decide(workflow_id, who, "approve", body)

    @app.post(API_PREFIX + "/workflows/{workflow_id}/reject", status_code=202, tags=["workflows"])
    def reject(workflow_id: str, who: Auth, body: DecisionBody = DecisionBody()):
        return _decide(workflow_id, who, "reject", body)

    @app.post(API_PREFIX + "/workflows/{workflow_id}/resource", status_code=202, tags=["workflows"])
    def resource(workflow_id: str, who: Auth, body: DecisionBody = DecisionBody()):
        return _decide(workflow_id, who, "resource", body)

    # ---------------------------------------------------------------- read models
    @app.get(f"{API_PREFIX}/inventory", tags=["inventory"])
    def inventory(who: Auth, pg: Page, low_only: bool = False):
        who.require(Perm.READ)
        with ctx.scoped(who):
            where = "WHERE Stock_Level < Reorder_Point" if low_only else ""
            with db.connect() as c:
                total = c.execute(f"SELECT COUNT(*) FROM inventory {where}").fetchone()[0]
                rows = [dict(r) for r in c.execute(f"SELECT * FROM inventory {where} ORDER BY Item_ID LIMIT ? OFFSET ?", pg)]
            fc = {f["Item_ID"]: f for f in forecast.latest_forecasts(MAX_PAGE * 5)}
            for r in rows:
                f = fc.get(r["Item_ID"])
                r["forecast"] = None if not f else {"method": f["Method"], "expected_demand": f["Expected_Demand"], "confidence": f["Confidence"], "horizon_days": f["Horizon_Days"]}
            return {"items": rows, "total": total, "limit": pg[0], "offset": pg[1]}

    @app.get(f"{API_PREFIX}/suppliers", tags=["suppliers"])
    def list_suppliers(who: Auth, pg: Page):
        who.require(Perm.READ)
        with ctx.scoped(who):
            m = suppliers.all_metrics()
            with db.connect() as c:
                vendors = [dict(r) for r in c.execute("SELECT Vendor_ID, Vendor_Name, Region FROM vendors ORDER BY Vendor_ID")]
            items = [{**v, "metrics": m[v["Vendor_ID"]].to_dict()} for v in vendors if v["Vendor_ID"] in m]
            return {"items": items[pg[1]:pg[1] + pg[0]], "total": len(items), "limit": pg[0], "offset": pg[1]}

    @app.get(f"{API_PREFIX}/metrics", tags=["observability"], summary="Workflow, LLM and node latency metrics for the caller's tenant")
    def metrics(who: Auth):
        who.require(Perm.READ)
        with ctx.scoped(who):
            tid = who.tenant_id
            with db.connect() as c:
                runs = {r[0]: r[1] for r in c.execute("SELECT Status, COUNT(*) FROM workflow_runs WHERE Tenant_ID=? GROUP BY Status", (tid,))}
                llm = c.execute("SELECT COUNT(*), COALESCE(SUM(Tokens_In),0), COALESCE(SUM(Tokens_Out),0), COALESCE(SUM(Cost_USD),0), COALESCE(SUM(Fallback),0), COALESCE(SUM(Retries),0) "
                                "FROM llm_calls WHERE Tenant_ID=?", (tid,)).fetchone()
            done = runs.get("COMPLETED", 0) + runs.get("AWAITING_APPROVAL", 0)
            finished = done + runs.get("FAILED", 0)
            return {"workflows": {"by_status": runs, "success_rate": (done / finished) if finished else None},
                    "llm": {"calls": llm[0], "tokens_in": llm[1], "tokens_out": llm[2], "cloud_equivalent_cost_usd": round(llm[3], 6), "fallbacks": llm[4], "retries": llm[5],
                            "process": METRICS.snapshot()}, "nodes": obs.node_latency_summary()}

    @app.get(f"{API_PREFIX}/audit", tags=["audit"])
    def audit_log(who: Auth, pg: Page, workflow_id: str | None = None, event_type: str | None = Query(None, pattern="^[A-Z_]{3,40}$"), verify: bool = False):
        who.require(Perm.READ_AUDIT)
        if workflow_id is not None and not valid_id(workflow_id):
            raise InvalidRequest("malformed workflow_id")
        with ctx.scoped(who):
            body = {"items": audit.events(workflow_id, event_type, pg[0], pg[1]), "total": audit.count(workflow_id), "limit": pg[0], "offset": pg[1]}
            if verify:
                body["chain"] = audit.verify_chain()
            return body

    routes_platform.register(app, ctx, Auth, Page)
    return app


app = create_app()
