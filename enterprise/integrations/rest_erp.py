"""Generic REST/JSON ERP adapter.

Contract (what an ERP, or a thin facade in front of one, must expose):

    GET  {base}/erp/inventory                  -> {"items": [{"sku", "quantity", "warehouse", "name"}]}
    GET  {base}/erp/suppliers                  -> {"suppliers": [{"supplier_id", "name", "email", "region"}]}
    GET  {base}/erp/purchase-orders            -> {"purchase_orders": [{"po_number", "supplier_id", "lines": [...], "status", "order_date", "external_id"}]}
    POST {base}/erp/purchase-orders            (header Idempotency-Key)  -> the created (or, on replay, the original) purchase order
    GET  {base}/erp/purchase-orders/{po}/delivery -> {"po_number", "status", "expected_date", "received": {sku: qty}}

STATUS: SIMULATED. It has been exercised against the bundled mock server only. It is NOT an SAP / Oracle / NetSuite / Dynamics integration.
Safety: SSRF-checked URL (re-checked before every call), bounded timeouts, bounded retries with exponential backoff on 5xx / timeouts only, the API key is sent in a
header and never logged or put in an error message.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from scm.security import UnsafeUrl, assert_public_url

from .base import (ERPAdapter, ERPConflict, ERPDeliveryStatus, ERPError, ERPInventoryItem, ERPPurchaseOrder, ERPPurchaseOrderLine, ERPSupplier, ERPTimeout, ERPUnavailable,
                   ERPValidationError)


class RestERPAdapter(ERPAdapter):
    name = "rest"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 5.0, retries: int = 2, backoff: float = 0.2, sleep=time.sleep):
        self.base_url, self.api_key, self.timeout, self.retries, self.backoff, self._sleep = base_url.rstrip("/"), api_key, timeout, retries, backoff, sleep
        assert_public_url(self.base_url)

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: dict | None = None, idem: str | None = None) -> dict:
        url = self.base_url + path
        try:
            assert_public_url(url)                                      # DNS may have changed since configuration
        except UnsafeUrl as exc:
            raise ERPValidationError(f"blocked URL: {exc}") from exc
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if idem:
            headers["Idempotency-Key"] = idem
        data = json.dumps(body).encode() if body is not None else None
        last: ERPError | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    raise ERPConflict("the ERP reports a conflict (duplicate request)") from exc
                if 400 <= exc.code < 500:
                    raise ERPValidationError(f"the ERP rejected the request (HTTP {exc.code})") from exc
                last = ERPUnavailable(f"the ERP failed (HTTP {exc.code})")
            except TimeoutError as exc:
                last = ERPTimeout(f"the ERP did not answer within {self.timeout:g}s")
                last.__cause__ = exc
            except urllib.error.URLError as exc:
                last = ERPTimeout("the ERP did not answer in time") if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason) else ERPUnavailable("the ERP is unreachable")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ERPValidationError("the ERP returned an unreadable response") from exc
            if attempt < self.retries:
                self._sleep(self.backoff * (2 ** attempt))
        assert last is not None
        raise last

    # ------------------------------------------------------------------ contract
    def get_inventory(self, since: str | None = None) -> list[ERPInventoryItem]:
        d = self._request("GET", "/erp/inventory" + (f"?since={since}" if since else ""))
        return [ERPInventoryItem(i["sku"], int(i["quantity"]), i.get("warehouse"), i.get("name")) for i in d.get("items", [])]

    def get_suppliers(self) -> list[ERPSupplier]:
        return [ERPSupplier(s["supplier_id"], s["name"], s.get("email"), s.get("region")) for s in self._request("GET", "/erp/suppliers").get("suppliers", [])]

    @staticmethod
    def _po(d: dict) -> ERPPurchaseOrder:
        return ERPPurchaseOrder(d["po_number"], d["supplier_id"], [ERPPurchaseOrderLine(x["sku"], int(x["quantity"]), float(x["unit_price"])) for x in d.get("lines", [])],
                                d.get("status", "CREATED"), d.get("order_date"), d.get("external_id"))

    def get_purchase_orders(self, since: str | None = None) -> list[ERPPurchaseOrder]:
        return [self._po(p) for p in self._request("GET", "/erp/purchase-orders").get("purchase_orders", [])]

    def create_purchase_order(self, po: ERPPurchaseOrder, idempotency_key: str) -> ERPPurchaseOrder:
        if not idempotency_key:
            raise ERPValidationError("an idempotency key is required")
        try:
            return self._po(self._request("POST", "/erp/purchase-orders", po.to_dict(), idem=idempotency_key))
        except ERPConflict:                                              # the ERP says it already has this request: fetch the original instead of failing
            for existing in self.get_purchase_orders():
                if existing.po_number == po.po_number:
                    return existing
            raise

    def get_delivery_status(self, po_number: str) -> ERPDeliveryStatus:
        d = self._request("GET", f"/erp/purchase-orders/{po_number}/delivery")
        return ERPDeliveryStatus(d["po_number"], d["status"], d.get("expected_date"), {k: int(v) for k, v in d.get("received", {}).items()})

    def health(self) -> dict:
        try:
            self._request("GET", "/health")
            return {"adapter": self.name, "status": "ok"}
        except ERPError as exc:
            return {"adapter": self.name, "status": "down", "error": str(exc)}
