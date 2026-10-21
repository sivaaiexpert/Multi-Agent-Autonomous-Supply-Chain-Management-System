"""Mock enterprise environment: ERP + supplier API + inventory API + procurement API in ONE small HTTP server, with switchable failure scenarios.

Lets the platform be demonstrated and tested without any customer's credentials. EVERYTHING HERE IS FAKE: it proves the platform's behaviour against these
scenarios, not against SAP / Oracle / NetSuite / Dynamics or any real supplier.

    python -m mocks.server --port 9101             # then POST /_scenario {"name": "supplier_failure"}

Scenarios: happy_path | supplier_failure | inventory_mismatch | api_timeout | duplicate_request | partial_failure
Auth: every route except /health needs `Authorization: Bearer <token>` (default token: mock-token).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import threading
import time
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

SCENARIOS = ("happy_path", "supplier_failure", "inventory_mismatch", "api_timeout", "duplicate_request", "partial_failure")

SEED_INVENTORY = [{"sku": "MOCK-001", "quantity": 25, "warehouse": "WH-A", "name": "Mock widget"}, {"sku": "MOCK-002", "quantity": 8, "warehouse": "WH-A", "name": "Mock gadget"},
                  {"sku": "MOCK-003", "quantity": 120, "warehouse": "WH-B", "name": "Mock bracket"}]
SEED_SUPPLIERS = [{"supplier_id": "MS-1", "name": "Mock Supplier One", "email": "one@mock.example", "region": "US"},
                  {"supplier_id": "MS-2", "name": "Mock Supplier Two", "email": "two@mock.example", "region": "EU"}]
SEED_QUOTES = {"MOCK-001": [{"supplier_id": "MS-1", "unit_price": 10.5, "lead_time_days": 5}, {"supplier_id": "MS-2", "unit_price": 11.2, "lead_time_days": 3}],
               "MOCK-002": [{"supplier_id": "MS-1", "unit_price": 25.0, "lead_time_days": 7}], "MOCK-003": [{"supplier_id": "MS-2", "unit_price": 3.4, "lead_time_days": 4}]}


class World:
    def __init__(self, token: str = "mock-token", timeout_delay: float = 3.0):
        self.token, self.timeout_delay = token, timeout_delay
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.scenario = "happy_path"
            self.inventory = copy.deepcopy(SEED_INVENTORY)
            self.orders: dict[str, dict] = {}
            self.keys: dict[str, str] = {}
            self.distinct_creates = 0
            self.requests: list[dict] = []
            self.procurement_requests: list[dict] = []


def create_app(token: str = "mock-token", timeout_delay: float = 3.0) -> FastAPI:
    app = FastAPI(title="Mock enterprise environment (FAKE)", docs_url=None, redoc_url=None)
    w = World(token, timeout_delay)
    app.state.world = w

    def auth(authorization: str | None) -> None:
        if authorization != f"Bearer {w.token}":
            raise HTTPException(status_code=401, detail="bad credentials")

    @app.middleware("http")
    async def log_and_slow(request: Request, call_next):
        w.requests.append({"method": request.method, "path": request.url.path, "ts": time.time()})
        if w.scenario == "api_timeout" and request.url.path.startswith("/erp"):
            await asyncio.sleep(w.timeout_delay)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"status": "ok", "scenario": w.scenario}

    @app.post("/_scenario")
    async def set_scenario(request: Request):
        name = (await request.json()).get("name")
        if name not in SCENARIOS:
            raise HTTPException(status_code=422, detail=f"scenario must be one of {SCENARIOS}")
        w.scenario = name
        return {"scenario": name}

    @app.post("/_reset")
    def reset():
        w.reset()
        return {"scenario": w.scenario}

    @app.get("/_state")
    def state():
        return {"scenario": w.scenario, "orders": len(w.orders), "distinct_creates": w.distinct_creates, "request_count": len(w.requests), "idempotency_keys": len(w.keys)}

    def inv_view() -> list[dict]:
        items = copy.deepcopy(w.inventory)
        if w.scenario == "inventory_mismatch":
            for i in items:
                i["quantity"] = int(i["quantity"] * 0.8)
        return items

    # ------------------------------------------------------------------ ERP
    @app.get("/erp/inventory")
    def erp_inventory(authorization: str | None = Header(None)):
        auth(authorization)
        return {"items": inv_view()}

    @app.get("/erp/suppliers")
    def erp_suppliers(authorization: str | None = Header(None)):
        auth(authorization)
        return {"suppliers": SEED_SUPPLIERS}

    @app.get("/erp/purchase-orders")
    def erp_orders(authorization: str | None = Header(None)):
        auth(authorization)
        return {"purchase_orders": list(w.orders.values())}

    @app.post("/erp/purchase-orders")
    async def erp_create(request: Request, authorization: str | None = Header(None), idempotency_key: str | None = Header(None)):
        auth(authorization)
        if not idempotency_key:
            raise HTTPException(status_code=422, detail="Idempotency-Key header required")
        body = await request.json()
        with w.lock:
            if idempotency_key in w.keys:
                if w.scenario == "duplicate_request":
                    return JSONResponse(status_code=409, content={"detail": "duplicate request", "external_id": w.keys[idempotency_key]})
                return JSONResponse(content=w.orders[w.keys[idempotency_key]], headers={"X-Idempotent-Replay": "true"})
            w.distinct_creates += 1
            if w.scenario == "partial_failure" and w.distinct_creates % 2 == 0:
                raise HTTPException(status_code=500, detail="simulated failure part-way through a batch")
            if not body.get("lines") or any(float(x.get("quantity", 0)) <= 0 or float(x.get("unit_price", 0)) <= 0 for x in body["lines"]):
                raise HTTPException(status_code=422, detail="invalid lines")
            ext = f"ERP-PO-{len(w.orders) + 1:05d}"
            order = {**body, "external_id": ext, "status": "CREATED", "order_date": body.get("order_date") or date.today().isoformat()}
            w.orders[ext] = order
            w.keys[idempotency_key] = ext
            return JSONResponse(status_code=201, content=order)

    @app.get("/erp/purchase-orders/{po}/delivery")
    def erp_delivery(po: str, authorization: str | None = Header(None)):
        auth(authorization)
        for o in w.orders.values():
            if po in (o["po_number"], o["external_id"]):
                return {"po_number": o["po_number"], "status": "IN_TRANSIT", "expected_date": None, "received": {}}
        raise HTTPException(status_code=404, detail="unknown purchase order")

    # ------------------------------------------------------------------ supplier API
    def supplier_guard(authorization: str | None) -> None:
        auth(authorization)
        if w.scenario == "supplier_failure":
            raise HTTPException(status_code=503, detail="supplier API unavailable")

    @app.get("/supplier/quotes")
    def supplier_quotes(sku: str, authorization: str | None = Header(None)):
        supplier_guard(authorization)
        return {"sku": sku, "quotes": SEED_QUOTES.get(sku, [])}

    @app.get("/supplier/availability")
    def supplier_availability(sku: str, authorization: str | None = Header(None)):
        supplier_guard(authorization)
        return {"sku": sku, "available": 1000 if sku in SEED_QUOTES else 0}

    # ------------------------------------------------------------------ inventory API (a different shape of the same facts)
    @app.get("/inventory/levels")
    def inventory_levels(authorization: str | None = Header(None)):
        auth(authorization)
        return {"levels": [{"sku": i["sku"], "onHand": i["quantity"], "location": i["warehouse"]} for i in inv_view()]}

    # ------------------------------------------------------------------ procurement API
    @app.post("/procurement/requests")
    async def procurement_request(request: Request, authorization: str | None = Header(None)):
        auth(authorization)
        body = await request.json()
        rid = f"PR-{len(w.procurement_requests) + 1:04d}"
        w.procurement_requests.append({"id": rid, **body})
        return JSONResponse(status_code=201, content={"id": rid, "status": "RECEIVED"})

    return app


app = create_app()


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9101)
    a = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
