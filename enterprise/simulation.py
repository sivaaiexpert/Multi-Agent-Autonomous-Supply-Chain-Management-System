"""Inventory replay simulator: the common ground for baseline-vs-optimised comparison, business KPIs and the experiment protocol.

Same inputs for every policy arm (`ARMS`):
  * identical future demand (known to the simulator, hidden from the policies until it happens),
  * identical random supplier outcomes via common random numbers: the delay / fill / defect outcome of an order is a deterministic function of
    (seed, supplier, sku, order day) drawn from that supplier's EMPIRICAL delivery history, so the arms face the same luck,
  * identical physical constraints: suppliers raise orders to their MOQ and cap them at their capacity (a constraint-blind buyer pays for that).
Policies see only the past: demand observed so far and deliveries dated before the evaluation window (no look-ahead).

Arms:  manual     - the buyer's habit: legacy reorder-point rule, lowest sticker price, no constraint awareness
       rule_based - same trigger and quantity, but the cheapest supplier that satisfies MOQ / capacity / reliability floor
       ai         - forecast + risk engine decide WHEN and HOW MUCH; the constrained optimiser decides WHO (the same engines the workflow uses)

Everything computed here is SIMULATED. It says nothing about a customer's real results unless it was run on that customer's own history, and even
then it is a back-test under stated assumptions (see `SimConfig`).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, timedelta
from statistics import mean

from scm import db
from scm.forecast import DemandForecaster
from scm.optimizer import SupplierOption, SupplierOptimizer
from scm.risk import RiskLevel, assess
from scm.settings import get_settings
from scm.suppliers import SupplierMetrics, compute_metrics

ARMS = ("manual", "rule_based", "ai", "ai_lean")


@dataclass(frozen=True)
class SimConfig:
    eval_days: int = 60
    refit_every: int = 7                     # AI arm re-fits its forecasts weekly
    pack: int = 5
    seed: int = 1
    holding_rate_annual: float = 0.20        # ASSUMPTION: carrying cost as a share of inventory value per year
    gross_margin: float = 0.25               # ASSUMPTION: margin lost on every unit of unmet demand
    start_stock: str = "as_given"            # as_given | cover:<days> (re-derive starting stock from demand)
    min_reliability: float = 60.0            # rule-based arm's supplier floor


@dataclass
class QuoteIn:
    vendor: str
    price: float
    lead: int
    moq: int = 1
    capacity: int | None = None
    region: str = "GLOBAL"


@dataclass
class SkuIn:
    sku: str
    category: str
    stock0: int
    rop: int
    target: int
    history: list[float]                     # demand observed BEFORE the evaluation window
    future: list[float]                      # demand during the window (hidden until it happens)
    quotes: list[QuoteIn]


@dataclass
class SimInput:
    skus: list[SkuIn]
    deliveries: dict[str, list[dict]]        # vendor -> past delivery records (rows shaped like supplier_deliveries)
    prior_reliability: dict[str, float]      # vendor master reliability estimate
    config: SimConfig = field(default_factory=SimConfig)
    provenance: str = "SYNTHETIC"
    label: str = ""


@dataclass
class Order:
    vendor: str
    qty: int


@dataclass
class SkuResult:
    sku: str
    category: str
    arm: str
    purchase_cost: float = 0.0
    units_ordered: int = 0
    units_received: int = 0
    orders: int = 0
    order_days: set = field(default_factory=set)
    demand_total: float = 0.0
    units_sold: float = 0.0
    units_short: float = 0.0
    stockout_events: int = 0
    stockout_days: int = 0
    inv_sum: float = 0.0
    excess_sum: float = 0.0
    safety_ok_days: int = 0
    lead_qty: float = 0.0
    late_orders: int = 0
    constraint_hits: int = 0                 # orders the supplier had to raise (MOQ) or truncate (capacity)
    days: int = 0
    avg_price: float = 0.0
    start_stock: int = 0
    end_stock: int = 0
    in_transit_good: int = 0


# ------------------------------------------------------------------------------------------------- supplier outcome (common random numbers)
def _draw(seed: int, vendor: str, sku: str, day: int, history: list[dict]) -> tuple[int, float, float]:
    """(delay_days, fill_ratio, defect_ratio) of an order, identical for every arm that places the same order on the same day."""
    if not history:
        return 0, 1.0, 0.0
    rec = history[random.Random(f"{seed}|{vendor}|{sku}|{day}").randrange(len(history))]
    if rec["Status"] == "CANCELLED" or rec.get("Actual_Days") is None:
        return 0, 0.0, 0.0
    ordered = max(1, rec["Qty_Ordered"])
    recv = min(1.0, rec["Qty_Received"] / ordered)
    return max(0, rec["Actual_Days"] - rec["Promised_Days"]), recv, (rec["Qty_Defective"] / rec["Qty_Received"] if rec["Qty_Received"] else 0.0)


def _pack_up(q: float, pack: int) -> int:
    return int(math.ceil(max(0.0, q) / pack) * pack)


# ------------------------------------------------------------------------------------------------- policies
class _Ctx:
    def __init__(self, sku: SkuIn, day: int, stock: int, on_order: int, history: list[float], metrics: dict[str, SupplierMetrics], cfg: SimConfig):
        self.sku, self.day, self.stock, self.on_order, self.history, self.metrics, self.cfg = sku, day, stock, on_order, history, metrics, cfg

    @property
    def position(self) -> int:
        return self.stock + self.on_order


def _legacy_trigger(ctx: _Ctx) -> int:
    """The customer's static rule: when stock position <= reorder point, order up to target."""
    return _pack_up(ctx.sku.target - ctx.position, ctx.cfg.pack) if ctx.position <= ctx.sku.rop and ctx.sku.target > ctx.position else 0


def manual_policy(ctx: _Ctx) -> list[Order]:
    qty = _legacy_trigger(ctx)
    if qty <= 0 or not ctx.sku.quotes:
        return []
    q = min(ctx.sku.quotes, key=lambda x: (x.price, x.lead, x.vendor))        # lowest sticker price, nothing else considered
    return [Order(q.vendor, qty)]


def rule_policy(ctx: _Ctx) -> list[Order]:
    qty = _legacy_trigger(ctx)
    if qty <= 0 or not ctx.sku.quotes:
        return []
    ok = [q for q in ctx.sku.quotes if q.moq <= qty and (q.capacity is None or q.capacity >= qty)
          and (ctx.metrics[q.vendor].reliability_score if q.vendor in ctx.metrics else 90.0) >= ctx.cfg.min_reliability]
    pool = ok or ctx.sku.quotes
    q = min(pool, key=lambda x: (x.price, x.lead, x.vendor))
    return [Order(q.vendor, qty)]


class AiPolicy:
    def __init__(self, cfg: SimConfig, lean: bool = False):
        self.cfg = cfg
        self.risk_cfg = get_settings().risk.model_copy(update={"order_up_to": "dynamic"}) if lean else get_settings().risk
        self.forecaster = DemandForecaster()
        self.optimizer = SupplierOptimizer()
        self._fc: dict[str, tuple[int, object]] = {}

    def __call__(self, ctx: _Ctx) -> list[Order]:
        sku = ctx.sku
        cached = self._fc.get(sku.sku)
        if cached is None or ctx.day - cached[0] >= self.cfg.refit_every:
            cached = (ctx.day, self.forecaster.forecast(sku.sku, ctx.history, get_settings().forecast.horizon_days))
            self._fc[sku.sku] = cached
        fc = cached[1]
        leads = [q.lead for q in sku.quotes] or [get_settings().risk.default_lead_time_days]
        lead_std = _lead_std(ctx.metrics, [q.vendor for q in sku.quotes])
        ra = assess(sku.sku, ctx.stock, fc, mean(leads), sku.target, sku.rop, ctx.on_order, lead_std, self.risk_cfg)           # type: ignore[arg-type]
        if ra.risk_level.rank < RiskLevel.MEDIUM.rank or ra.recommended_order_quantity <= 0:
            return []
        options = [SupplierOption(q.vendor, q.vendor, q.price, q.lead, reliability_of(ctx.metrics, q.vendor), quality_of(ctx.metrics, q.vendor),
                                  q.moq, q.capacity, q.region) for q in sku.quotes]
        plan = self.optimizer.optimize(ra.recommended_order_quantity, options, ra.days_until_stockout)
        return [Order(v, n) for v, n in plan.allocation.items()] if plan.allocation else []


def reliability_of(metrics: dict[str, SupplierMetrics], vendor: str, default: float = 90.0) -> float:
    m = metrics.get(vendor)
    return float(m.reliability_score) if m is not None and m.reliability_score is not None else default


def quality_of(metrics: dict[str, SupplierMetrics], vendor: str) -> float:
    m = metrics.get(vendor)
    return float(m.quality_rate) if m is not None and m.quality_rate is not None else 1.0


def _lead_std(metrics: dict[str, SupplierMetrics], vendors: list[str]) -> float:
    vs = [float(var) for v in vendors if v in metrics for var in [metrics[v].lead_time_variance] if var]
    return math.sqrt(mean(vs)) if vs else 0.0


# ------------------------------------------------------------------------------------------------- the replay
def simulate(inp: SimInput, arm: str) -> list[SkuResult]:
    cfg = inp.config
    metrics = {v: compute_metrics(v, d, inp.prior_reliability.get(v, 90.0)) for v, d in inp.deliveries.items()}
    for v, prior in inp.prior_reliability.items():
        metrics.setdefault(v, compute_metrics(v, [], prior))
    policies: dict[str, Callable[[_Ctx], list[Order]]] = {"manual": manual_policy, "rule_based": rule_policy, "ai": AiPolicy(cfg), "ai_lean": AiPolicy(cfg, lean=True)}
    policy = policies[arm]
    forecaster = DemandForecaster()
    results = []
    for s in inp.skus:
        r = SkuResult(s.sku, s.category, arm, days=len(s.future))
        r.avg_price = mean(q.price for q in s.quotes) if s.quotes else 0.0
        stock = s.stock0
        if cfg.start_stock.startswith("cover:") and s.history:
            stock = int(mean(s.history[-28:]) * float(cfg.start_stock.split(":")[1]))
        r.start_stock = stock
        history = list(s.history)
        if history:                                                    # safety stock per the risk engine, identical formula for every arm
            ss = assess(s.sku, stock, forecaster.forecast(s.sku, history, 30), mean(q.lead for q in s.quotes) if s.quotes else 7, s.target, s.rop).safety_stock
        else:
            ss = 0
        pipeline: list[tuple[int, int, int]] = []                       # (arrival day, good units, planned units)
        prev_short = False
        for d, demand in enumerate(s.future):
            arrived = [p for p in pipeline if p[0] <= d]
            pipeline = [p for p in pipeline if p[0] > d]
            stock += sum(p[1] for p in arrived)
            sold = min(stock, demand)
            short = demand - sold
            stock = int(round(stock - sold))
            r.demand_total += demand
            r.units_sold += sold
            r.units_short += short
            if short > 1e-9:
                r.stockout_days += 1
                r.stockout_events += 0 if prev_short else 1
            prev_short = short > 1e-9
            r.inv_sum += stock
            r.excess_sum += max(0, stock - s.target)
            r.safety_ok_days += stock >= ss
            history.append(demand)                                      # observed at the end of the day, usable from tomorrow
            ctx = _Ctx(s, d, stock, sum(p[2] for p in pipeline), history, metrics, cfg)
            for o in policy(ctx):
                q = next((x for x in s.quotes if x.vendor == o.vendor), None)
                if q is None or o.qty <= 0:
                    continue
                eff = max(o.qty, q.moq)                                 # supplier raises to MOQ ...
                if q.capacity is not None and eff > q.capacity:         # ... and cannot ship more than its capacity
                    eff = q.capacity
                r.constraint_hits += eff != o.qty
                delay, fill, defect = _draw(cfg.seed, o.vendor, s.sku, d, inp.deliveries.get(o.vendor, []))
                received = int(eff * fill)
                good = int(received * (1 - defect))
                pipeline.append((d + q.lead + delay, good, o.qty))
                r.orders += 1
                r.order_days.add(d)
                r.units_ordered += o.qty
                r.units_received += received
                r.purchase_cost += received * q.price                   # you pay for what arrives, defective or not
                r.lead_qty += q.lead * o.qty
                r.late_orders += delay > 0
        r.end_stock = stock
        r.in_transit_good = sum(p[1] for p in pipeline)
        results.append(r)
    return results


@dataclass
class ArmSummary:
    arm: str
    purchase_cost: float
    net_purchase_cost: float                  # cost of goods CONSUMED: purchases - value of stock and in-transit units left at the end + value of the starting stock
    holding_cost: float
    stockout_cost: float
    total_cost: float
    units_sold: float
    cost_per_unit_served: float | None        # net purchase cost / units sold: isolates the supplier-price effect from the volume effect
    revenue: float                            # ASSUMPTION: selling price = cost / (1 - gross_margin)
    gross_profit: float                       # revenue - net purchase cost - holding cost: the like-for-like comparison when service levels differ
    units_ordered: int
    orders: int
    stockout_events: int
    stockout_days: int
    units_short: float
    fill_rate: float                          # demand served from stock / demand
    avg_inventory_units: float
    avg_inventory_value: float
    turnover_annualized: float | None
    excess_units_avg: float
    excess_value_avg: float
    safety_stock_adherence: float
    avg_lead_time_days: float | None
    late_order_rate: float | None
    constraint_hits: int
    manual_touches: int
    skus: int
    days: int

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def summarize(results: list[SkuResult], cfg: SimConfig, arm: str) -> ArmSummary:
    n_days = max((r.days for r in results), default=0)
    sold = sum(r.units_sold for r in results)
    demand = sum(r.demand_total for r in results)
    avg_units = sum(r.inv_sum for r in results) / max(1, n_days)
    avg_value = sum(r.inv_sum / max(1, r.days) * r.avg_price for r in results)
    holding = avg_value * cfg.holding_rate_annual * n_days / 365
    margin_per_unit = cfg.gross_margin / (1 - cfg.gross_margin)                                # selling price = cost / (1 - margin)
    stockout_cost = sum(r.units_short * r.avg_price * margin_per_unit for r in results)       # lost margin on unmet demand
    purchase = sum(r.purchase_cost for r in results)
    net = purchase - sum((r.end_stock + r.in_transit_good - r.start_stock) * r.avg_price for r in results)
    orders = sum(r.orders for r in results)
    batches = len({d for r in results for d in r.order_days})
    touches = {"manual": orders, "rule_based": batches, "ai": batches, "ai_lean": batches}[arm]
    value_sold = sum(r.units_sold * r.avg_price for r in results)
    qty = sum(r.units_ordered for r in results)
    revenue = sum(r.units_sold * r.avg_price / (1 - cfg.gross_margin) for r in results)
    return ArmSummary(arm, purchase, net, holding, stockout_cost, net + holding + stockout_cost, sold, (net / sold) if sold else None, revenue,
                      revenue - net - holding, qty, orders,
                      sum(r.stockout_events for r in results), sum(r.stockout_days for r in results), sum(r.units_short for r in results),
                      sold / demand if demand else 1.0, avg_units, avg_value, (value_sold / avg_value * 365 / max(1, n_days)) if avg_value else None,
                      sum(r.excess_sum for r in results) / max(1, n_days), sum(r.excess_sum / max(1, r.days) * r.avg_price for r in results),
                      sum(r.safety_ok_days for r in results) / max(1, sum(r.days for r in results)),
                      (sum(r.lead_qty for r in results) / qty) if qty else None, (sum(r.late_orders for r in results) / orders) if orders else None,
                      sum(r.constraint_hits for r in results), touches, len(results), n_days)


def run_all_arms(inp: SimInput, arms: tuple[str, ...] = ARMS) -> dict:
    """{arm: {summary: ArmSummary, per_sku: [SkuResult]}}"""
    out = {}
    for a in arms:
        res = simulate(inp, a)
        out[a] = {"summary": summarize(res, inp.config, a), "per_sku": res}
    return out


# ------------------------------------------------------------------------------------------------- builders
def from_dataset(ds, cfg: SimConfig | None = None) -> SimInput:
    """Synthetic dataset: the generator's hidden future demand is the evaluation window; deliveries are the platform-visible history."""
    p = ds.profile
    cfg = cfg or SimConfig(eval_days=p.eval_days)
    by_sku: dict[str, list[QuoteIn]] = {}
    region = {s["Vendor_ID"]: s["Region"] for s in ds.suppliers}
    for q in ds.quotes:
        by_sku.setdefault(q["Item_ID"], []).append(QuoteIn(q["Vendor_ID"], q["Unit_Price"], q["Lead_Time_Days"], q["MOQ"], q["Capacity"], region[q["Vendor_ID"]]))
    deliveries: dict[str, list[dict]] = {}
    for d in ds.deliveries:
        deliveries.setdefault(d["Vendor_ID"], []).append({"Status": d["Status"], "Actual_Days": d["Actual_Days"], "Promised_Days": d["Promised_Days"], "Qty_Ordered": d["Qty_Ordered"],
                                                          "Qty_Received": d["Qty_Received"], "Qty_Defective": d["Qty_Defective"], "Unit_Price": d["Unit_Price"], "Item_ID": d["Item_ID"]})
    skus = [SkuIn(s["Item_ID"], s["Category"], s["Stock_Level"], s["Reorder_Point"], s["Target_Stock"], ds.history[s["Item_ID"]],
                  ds.truth["future_demand"][s["Item_ID"]][:cfg.eval_days], by_sku.get(s["Item_ID"], [])) for s in ds.skus]
    return SimInput(skus, deliveries, {s["Vendor_ID"]: s["Reliability"] for s in ds.suppliers}, cfg, ds.provenance, p.name)


def from_current_tenant(cfg: SimConfig | None = None) -> SimInput:
    """BACK-TEST on the current tenant's own history: the last `eval_days` of recorded demand play the future, everything earlier is the past.
    Starting stock is today's stock level (an assumption: stock history is not part of the import), deliveries after the cut-off are ignored."""
    cfg = cfg or SimConfig()
    with db.connect() as c:
        inv = [dict(r) for r in c.execute("SELECT * FROM inventory ORDER BY Item_ID")]
        hist: dict[str, list[tuple[str, float]]] = {}
        for item, day, qty in c.execute("SELECT Item_ID, Day, Quantity FROM demand_history ORDER BY Item_ID, Day"):
            hist.setdefault(item, []).append((day, qty))
        listings = [dict(r) for r in c.execute("SELECT l.*, v.Region FROM vendor_listings l JOIN vendors v USING(Vendor_ID)")]
        vendors = {r[0]: r[1] for r in c.execute("SELECT Vendor_ID, Reliability FROM vendors")}
        deliveries_all = [dict(r) for r in c.execute("SELECT * FROM supplier_deliveries")]
    last_day = max((h[-1][0] for h in hist.values() if h), default=None)
    if last_day is None:
        raise ValueError("no demand history: import historical demand first")
    cut = (date.fromisoformat(last_day) - timedelta(days=cfg.eval_days)).isoformat()
    by_sku: dict[str, list[QuoteIn]] = {}
    for q in listings:
        by_sku.setdefault(q["Item_ID"], []).append(QuoteIn(q["Vendor_ID"], q["Unit_Price"], q["Lead_Time_Days"], q["MOQ"], q["Capacity"], q["Region"]))
    skus = []
    for r in inv:
        series = hist.get(r["Item_ID"], [])
        past = [q for d, q in series if d <= cut]
        future = [q for d, q in series if d > cut]
        if len(past) < 28 or len(future) < max(7, cfg.eval_days // 2) or not by_sku.get(r["Item_ID"]):
            continue
        skus.append(SkuIn(r["Item_ID"], r["Category"] or "", max(0, r["Stock_Level"]), r["Reorder_Point"], r["Target_Stock"], past, future[:cfg.eval_days], by_sku[r["Item_ID"]]))
    deliveries: dict[str, list[dict]] = {}
    for d in deliveries_all:
        if d["Ordered_At"] <= cut:                                     # no look-ahead
            deliveries.setdefault(d["Vendor_ID"], []).append(d)
    with db.connect() as c:
        row = c.execute("SELECT Value FROM app_state WHERE Key='data_provenance'").fetchone()
    return SimInput(skus, deliveries, vendors, cfg, row[0] if row else "CUSTOMER_DATA", "back-test on tenant history")
