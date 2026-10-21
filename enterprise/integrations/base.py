"""ERP integration contract.

The platform core knows only this interface. Adding SAP / Oracle / Dynamics / NetSuite / a custom ERP means writing ONE class that implements `ERPAdapter`;
nothing in the workflow, the importer or the API changes. Implementation status of every adapter is declared in `ADAPTER_STATUS` and surfaced in the docs:
only `mock` and `rest` exist, and `rest` has been exercised against the bundled mock server only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


class ERPError(Exception):
    """Base error. `retryable` tells callers whether trying again can help."""
    retryable = False


class ERPTimeout(ERPError):
    retryable = True


class ERPUnavailable(ERPError):
    retryable = True


class ERPConflict(ERPError):
    retryable = False


class ERPValidationError(ERPError):
    retryable = False


@dataclass
class ERPInventoryItem:
    sku: str
    quantity: int
    warehouse: str | None = None
    name: str | None = None


@dataclass
class ERPSupplier:
    supplier_id: str
    name: str
    email: str | None = None
    region: str | None = None


@dataclass
class ERPPurchaseOrderLine:
    sku: str
    quantity: int
    unit_price: float


@dataclass
class ERPPurchaseOrder:
    po_number: str
    supplier_id: str
    lines: list[ERPPurchaseOrderLine]
    status: str = "CREATED"
    order_date: str | None = None
    external_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def total(self) -> float:
        return round(sum(line.quantity * line.unit_price for line in self.lines), 2)


@dataclass
class ERPDeliveryStatus:
    po_number: str
    status: str                                   # CREATED | IN_TRANSIT | PARTIALLY_RECEIVED | RECEIVED | CANCELLED
    expected_date: str | None = None
    received: dict[str, int] = field(default_factory=dict)


class ERPAdapter(ABC):
    name = "abstract"

    @abstractmethod
    def get_inventory(self, since: str | None = None) -> list[ERPInventoryItem]: ...

    @abstractmethod
    def get_suppliers(self) -> list[ERPSupplier]: ...

    @abstractmethod
    def get_purchase_orders(self, since: str | None = None) -> list[ERPPurchaseOrder]: ...

    @abstractmethod
    def create_purchase_order(self, po: ERPPurchaseOrder, idempotency_key: str) -> ERPPurchaseOrder:
        """MUST be idempotent: the same idempotency_key returns the original order and never creates a second one."""

    @abstractmethod
    def get_delivery_status(self, po_number: str) -> ERPDeliveryStatus: ...

    def health(self) -> dict:
        return {"adapter": self.name, "status": "unknown"}


# What actually exists. Keep this honest: documentation and the API read it.
ADAPTER_STATUS: dict[str, dict[str, str | None]] = {
    "mock": {"status": "IMPLEMENTED", "validated_against": "unit + integration tests (in-process)", "note": "deterministic, with failure injection"},
    "rest": {"status": "SIMULATED", "validated_against": "the bundled mock HTTP server (mocks/server.py)", "note": "generic JSON/REST contract; not tested against any real ERP"},
    "sap": {"status": "ADAPTER_READY", "validated_against": None, "note": "implement ERPAdapter; no SAP code exists"},
    "oracle": {"status": "ADAPTER_READY", "validated_against": None, "note": "implement ERPAdapter; no Oracle code exists"},
    "dynamics": {"status": "ADAPTER_READY", "validated_against": None, "note": "implement ERPAdapter; no Microsoft Dynamics code exists"},
    "netsuite": {"status": "ADAPTER_READY", "validated_against": None, "note": "implement ERPAdapter; only an outbound payload SHAPE for NetSuite exists (scm.ingest), not an integration"},
}
