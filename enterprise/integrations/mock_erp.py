"""In-process mock ERP for development and tests. Deterministic; failure modes are injected explicitly, never random."""
from __future__ import annotations

import copy
import threading
from datetime import date

from .base import (ERPAdapter, ERPConflict, ERPDeliveryStatus, ERPInventoryItem, ERPPurchaseOrder, ERPPurchaseOrderLine, ERPSupplier, ERPTimeout, ERPUnavailable,
                   ERPValidationError)

DEFAULT_INVENTORY = [ERPInventoryItem("ERP-1001", 40, "WH-A", "Mock widget"), ERPInventoryItem("ERP-1002", 12, "WH-A", "Mock gadget"), ERPInventoryItem("ERP-1003", 150, "WH-B", "Mock bracket")]
DEFAULT_SUPPLIERS = [ERPSupplier("SUP-1", "Mock Supplier One", "one@mock.example", "US"), ERPSupplier("SUP-2", "Mock Supplier Two", "two@mock.example", "EU")]

FAILURE_MODES = (None, "unavailable", "timeout", "partial", "inventory_mismatch", "conflict_on_duplicate")


class MockERPAdapter(ERPAdapter):
    name = "mock"

    def __init__(self, inventory: list[ERPInventoryItem] | None = None, suppliers: list[ERPSupplier] | None = None, failure: str | None = None, fail_after: int = 1):
        if failure not in FAILURE_MODES:
            raise ValueError(f"failure must be one of {FAILURE_MODES}")
        self._inventory = copy.deepcopy(inventory if inventory is not None else DEFAULT_INVENTORY)
        self._suppliers = copy.deepcopy(suppliers if suppliers is not None else DEFAULT_SUPPLIERS)
        self._orders: dict[str, ERPPurchaseOrder] = {}
        self._keys: dict[str, str] = {}
        self._lock = threading.Lock()
        self.failure, self.fail_after = failure, fail_after
        self.calls: list[str] = []
        self.created_count = 0

    def _guard(self, op: str) -> None:
        self.calls.append(op)
        if self.failure == "unavailable":
            raise ERPUnavailable("mock ERP is unavailable (503)")
        if self.failure == "timeout":
            raise ERPTimeout("mock ERP timed out")

    def get_inventory(self, since: str | None = None) -> list[ERPInventoryItem]:
        self._guard("get_inventory")
        items = copy.deepcopy(self._inventory)
        if self.failure == "inventory_mismatch":
            for i in items:
                i.quantity = int(i.quantity * 0.8)                      # the ERP disagrees with the platform's books
        return items

    def get_suppliers(self) -> list[ERPSupplier]:
        self._guard("get_suppliers")
        return copy.deepcopy(self._suppliers)

    def get_purchase_orders(self, since: str | None = None) -> list[ERPPurchaseOrder]:
        self._guard("get_purchase_orders")
        return copy.deepcopy(list(self._orders.values()))

    def create_purchase_order(self, po: ERPPurchaseOrder, idempotency_key: str) -> ERPPurchaseOrder:
        self._guard("create_purchase_order")
        if not idempotency_key:
            raise ERPValidationError("an idempotency key is required")
        if not po.lines or any(line.quantity <= 0 or line.unit_price <= 0 for line in po.lines):
            raise ERPValidationError("purchase order lines must have positive quantity and price")
        with self._lock:
            if idempotency_key in self._keys:
                if self.failure == "conflict_on_duplicate":
                    raise ERPConflict("duplicate idempotency key")
                return copy.deepcopy(self._orders[self._keys[idempotency_key]])      # idempotent replay: the original order, no second one
            if self.failure == "partial" and self.created_count >= self.fail_after:
                raise ERPUnavailable("mock ERP failed part-way through the batch")
            ext = f"ERP-PO-{len(self._orders) + 1:05d}"
            stored = ERPPurchaseOrder(po.po_number, po.supplier_id, copy.deepcopy(po.lines), "CREATED", po.order_date or date.today().isoformat(), ext)
            self._orders[ext] = stored
            self._keys[idempotency_key] = ext
            self.created_count += 1
            return copy.deepcopy(stored)

    def get_delivery_status(self, po_number: str) -> ERPDeliveryStatus:
        self._guard("get_delivery_status")
        for o in self._orders.values():
            if po_number in (o.po_number, o.external_id):
                return ERPDeliveryStatus(o.po_number, "IN_TRANSIT", None, {})
        raise ERPValidationError(f"unknown purchase order {po_number!r}")

    def health(self) -> dict:
        return {"adapter": self.name, "status": "down" if self.failure in ("unavailable", "timeout") else "ok", "orders": len(self._orders)}

    @property
    def orders(self) -> list[ERPPurchaseOrder]:
        return list(self._orders.values())


def line(sku: str, qty: int, price: float) -> ERPPurchaseOrderLine:
    return ERPPurchaseOrderLine(sku, qty, price)
