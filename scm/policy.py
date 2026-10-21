"""Deterministic business-policy validator. The last automated gate before a human sees a proposal.

Everything is re-derived from the database (the source of truth), never trusted from the proposal itself: a tampered, hallucinated or
stale proposal (wrong price, unknown vendor, impossible quantity...) is BLOCKED here regardless of where it came from.
"""
from __future__ import annotations

import functools
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import db
from .settings import ApprovalCfg, get_settings

BLOCK, WARN = "block", "warn"


@dataclass
class Violation:
    code: str
    severity: str
    message: str
    item_id: str | None = None
    vendor_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyReport:
    violations: list[Violation] = field(default_factory=list)
    blocked: set[tuple[str, str]] = field(default_factory=set)       # (item_id, vendor_id) pairs that must not proceed
    requires_executive_review: bool = False
    requires_elevated_review: bool = False

    @property
    def ok(self) -> bool:
        return not self.blocked

    def for_line(self, item_id: str, vendor_id: str) -> list[Violation]:
        return [v for v in self.violations if v.item_id == item_id and v.vendor_id == vendor_id]


class PolicyValidator:
    def __init__(self, cfg: ApprovalCfg | None = None, budget_per_item: float | None = None):
        self.cfg = cfg or get_settings().approval
        self.budget_per_item = budget_per_item

    @staticmethod
    def _add(rep: PolicyReport, item: str, vid: str, code: str, sev: str, msg: str) -> None:
        rep.violations.append(Violation(code, sev, msg, item, vid))
        if sev == BLOCK:
            rep.blocked.add((item, vid))

    def validate(self, proposals: list[dict]) -> PolicyReport:
        rep, seen = PolicyReport(), set()
        rules = get_settings().rules
        running_total: float = 0.0
        spend_by_category: dict[str, float] = {}
        with db.connect() as c:
            month = datetime.now().strftime("%Y-%m")
            spent = {r[0]: float(r[1]) for r in c.execute(
                "SELECT i.Category, COALESCE(SUM(p.Quantity*p.Unit_Price),0) FROM purchase_orders p JOIN inventory i USING(Item_ID) "
                "WHERE p.Status='SENT' AND substr(p.Timestamp,1,7)=? GROUP BY i.Category", (month,))} if rules.budget_per_category_monthly else {}
            for p in proposals:
                item, vid = str(p.get("Item_ID", "")), str(p.get("Vendor_ID", ""))
                key = (item, vid)

                add = functools.partial(self._add, rep, item, vid)

                if key in seen:
                    add("DUPLICATE_LINE", BLOCK, f"duplicate proposal for {item} / {vid}")
                    continue
                seen.add(key)
                inv = c.execute("SELECT Target_Stock FROM inventory WHERE Item_ID=?", (item,)).fetchone()
                if inv is None:
                    add("UNKNOWN_SKU", BLOCK, f"SKU {item!r} does not exist")
                    continue
                ven = c.execute("SELECT Reliability FROM vendors WHERE Vendor_ID=?", (vid,)).fetchone()
                if ven is None:
                    add("UNKNOWN_VENDOR", BLOCK, f"vendor {vid!r} does not exist")
                    continue
                quote = c.execute("SELECT Unit_Price, Lead_Time_Days, MOQ, Capacity FROM vendor_listings WHERE Vendor_ID=? AND Item_ID=?",
                                  (vid, item)).fetchone()
                if quote is None:
                    add("NO_LISTING", BLOCK, f"{vid} has no listing for {item}")
                    continue
                qty, price = p.get("qty"), p.get("unit_price")
                if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty != qty or qty <= 0 or qty != int(qty):
                    add("BAD_QUANTITY", BLOCK, f"quantity must be a positive whole number (got {qty!r})")
                    continue
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price != price or price <= 0:
                    add("BAD_PRICE", BLOCK, f"unit price must be positive (got {price!r})")
                    continue
                qty = int(qty)
                if price > self.cfg.max_unit_price:
                    add("PRICE_ABSURD", BLOCK, f"unit price {price} exceeds the {self.cfg.max_unit_price:g} sanity limit")
                catalogue = float(quote["Unit_Price"])
                if catalogue > 0 and abs(price - catalogue) / catalogue > self.cfg.max_price_deviation:
                    add("PRICE_MISMATCH", BLOCK, f"price {price:.2f} deviates more than {self.cfg.max_price_deviation:.0%} from the "
                                                 f"catalogue quote {catalogue:.2f}")
                target = max(int(inv["Target_Stock"]), 1)
                if qty > self.cfg.max_qty_multiple_of_target * target:
                    add("QTY_EXCEEDS_LIMIT", BLOCK, f"quantity {qty} exceeds {self.cfg.max_qty_multiple_of_target:g}x the target stock ({target})")
                if qty < int(quote["MOQ"] or 1):
                    add("MOQ_VIOLATION", BLOCK, f"quantity {qty} is below the supplier minimum order quantity {quote['MOQ']}")
                if quote["Capacity"] is not None and qty > int(quote["Capacity"]):
                    add("CAPACITY_EXCEEDED", BLOCK, f"quantity {qty} exceeds the supplier capacity {quote['Capacity']}")
                if self.budget_per_item is not None and qty * price > self.budget_per_item:
                    add("BUDGET_EXCEEDED", BLOCK, f"line value {qty * price:,.2f} exceeds the budget {self.budget_per_item:,.2f}")
                if int(quote["Lead_Time_Days"]) < 0:
                    add("BAD_LEAD_TIME", BLOCK, "negative lead time in the catalogue")
                if p.get("optimizer_status") == "fallback":
                    add("OPTIMIZER_FALLBACK", WARN, "the optimiser failed; the deterministic fallback supplier was used")
                conf = p.get("forecast_confidence")
                if isinstance(conf, (int, float)) and conf < 0.3:
                    add("LOW_FORECAST_CONFIDENCE", WARN, f"forecast confidence is only {conf:.0%}")
                days = p.get("days_until_stockout")
                if isinstance(days, (int, float)) and p.get("lead_time") is not None and p["lead_time"] > days:
                    add("LEAD_TIME_EXCEEDS_COVER", WARN, f"lead time {p['lead_time']}d exceeds the {days:.1f}d of stock cover")
                total = qty * price
                # ---- tenant-configured business rules (see enterprise.policies)
                if rules.allowed_vendors and vid not in rules.allowed_vendors:
                    add("ALLOWED_SUPPLIER", BLOCK, f"supplier {vid} is not on the tenant's approved-supplier list")
                if rules.max_lead_time_days is not None and int(quote["Lead_Time_Days"]) > rules.max_lead_time_days:
                    add("LEAD_TIME_POLICY", BLOCK, f"lead time {quote['Lead_Time_Days']}d exceeds the policy maximum {rules.max_lead_time_days:g}d")
                if rules.max_order_quantity is not None and qty > rules.max_order_quantity:
                    add("ORDER_QUANTITY_POLICY", BLOCK, f"quantity {qty} exceeds the policy maximum {rules.max_order_quantity}")
                if rules.max_line_amount is not None and total > rules.max_line_amount:
                    add("LINE_AMOUNT_POLICY", BLOCK, f"line value {total:,.2f} exceeds the policy maximum {rules.max_line_amount:,.2f}")
                if rules.max_batch_amount is not None and running_total + total > rules.max_batch_amount and key not in rep.blocked:
                    add("BATCH_AMOUNT_POLICY", BLOCK, f"deferred: adding {total:,.2f} would exceed the batch limit {rules.max_batch_amount:,.2f}")
                cat = c.execute("SELECT Category FROM inventory WHERE Item_ID=?", (item,)).fetchone()[0] if rules.budget_per_category_monthly else None
                if cat in rules.budget_per_category_monthly:
                    used = spent.get(cat, 0.0) + spend_by_category.get(cat, 0.0)
                    if used + total > rules.budget_per_category_monthly[cat] and key not in rep.blocked:
                        add("CATEGORY_BUDGET_POLICY", BLOCK, f"monthly budget for {cat} ({rules.budget_per_category_monthly[cat]:,.2f}) would be exceeded "
                                                           f"(spent {used:,.2f})")
                if key not in rep.blocked:
                    running_total += total
                    if cat is not None:
                        spend_by_category[cat] = spend_by_category.get(cat, 0.0) + total
                if total > self.cfg.executive_review_total:
                    rep.requires_executive_review = True
                    add("EXECUTIVE_REVIEW", WARN, f"line value ${total:,.0f} exceeds the executive review threshold")
                elif total > self.cfg.elevated_review_total:
                    rep.requires_elevated_review = True
                    add("ELEVATED_REVIEW", WARN, f"line value ${total:,.0f} exceeds the elevated review threshold")
        return rep
