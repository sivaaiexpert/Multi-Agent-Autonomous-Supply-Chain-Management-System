"""Synthetic customer profiles and a deterministic dataset generator.

EVERYTHING produced here is SYNTHETIC. It exists to demonstrate and validate the platform without real customer data; results computed from it are
SIMULATED results and are labelled so everywhere (`DATA_PROVENANCE`). The generator separates what the platform can SEE (quotes, noisy prior
reliability, delivery history, demand history) from the hidden TRUTH used only by the simulator (true reliability, true lead-time delays, future demand).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from scm import db

DATA_PROVENANCE = "SYNTHETIC"
DEMO_LABEL = "DEMO / SYNTHETIC DATA: not customer data, results are simulated"


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    description: str
    n_skus: int
    n_suppliers: int
    history_days: int = 120
    eval_days: int = 60                      # future days used by the simulator
    base_demand: tuple[float, float] = (5, 40)
    weekly_amp: tuple[float, float] = (0.05, 0.3)
    trend_per_day: tuple[float, float] = (-0.001, 0.004)
    noise: tuple[float, float] = (0.3, 0.8)
    season_amp: float = 0.0                  # slow (period) seasonality: drives the "seasonal business" surge
    season_period: float = 90.0
    zero_prob: tuple[float, float] = (0.0, 0.0)   # intermittent demand
    lead_time: tuple[int, int] = (3, 12)
    price: tuple[float, float] = (5, 120)
    price_spread: float = 0.12               # supplier price dispersion around the SKU reference price
    reliability: tuple[float, float] = (82, 98)
    quality: tuple[float, float] = (0.93, 0.995)
    moq_choices: tuple[int, ...] = (1, 10, 25)
    capacity_choices: tuple[int | None, ...] = (None, 300, 800)
    listings_per_sku: tuple[int, int] = (3, 5)
    categories: tuple[str, ...] = ("General",)
    cover_days: tuple[float, float] = (4, 40)       # starting stock expressed in days of cover
    rop_cover_days_extra: float = 7.0               # the customer's legacy reorder rule: mean demand x (avg lead + this)
    target_days: float = 30.0
    monthly_budget: float | None = None
    risk_tolerance: str = "medium"
    weekly_orders_rate: float = 1.0


PROFILES: dict[str, Profile] = {
    "retailer": Profile("retailer", "Customer A - high-volume retailer", "Many fast-moving SKUs, short lead times, strong weekly pattern, many suppliers",
                        n_skus=120, n_suppliers=12, base_demand=(15, 90), weekly_amp=(0.15, 0.4), lead_time=(2, 9), reliability=(85, 98),
                        categories=("Grocery", "Household", "Electronics", "Apparel"), monthly_budget=2_000_000, risk_tolerance="low"),
    "manufacturer": Profile("manufacturer", "Customer B - manufacturing company", "Steadier component demand with a trend, long lead times, large MOQs, fewer suppliers",
                            n_skus=60, n_suppliers=8, base_demand=(4, 30), weekly_amp=(0.0, 0.1), trend_per_day=(0.0, 0.006), noise=(0.2, 0.5), lead_time=(14, 45),
                            reliability=(72, 95), moq_choices=(25, 50, 100, 250), capacity_choices=(500, 1500, None), price=(2, 400),
                            categories=("Raw materials", "Components", "Packaging"), cover_days=(10, 70), monthly_budget=900_000, risk_tolerance="medium"),
    "distributor": Profile("distributor", "Customer C - distributor", "Very wide catalogue, many intermittent tail SKUs, wide price differences between suppliers",
                           n_skus=200, n_suppliers=20, base_demand=(0.5, 25), noise=(0.5, 1.2), zero_prob=(0.0, 0.6), lead_time=(4, 21), price_spread=0.25,
                           reliability=(70, 97), categories=("Tools", "Fasteners", "Electrical", "Plumbing", "Safety"), monthly_budget=1_400_000, risk_tolerance="medium"),
    "seasonal": Profile("seasonal", "Customer D - seasonal business", "Strong slow seasonality with a demand ramp inside the planning window",
                        n_skus=80, n_suppliers=10, base_demand=(6, 45), season_amp=0.6, season_period=100, weekly_amp=(0.05, 0.2), lead_time=(5, 18),
                        reliability=(78, 96), categories=("Seasonal goods", "Gifts", "Outdoor"), monthly_budget=700_000, risk_tolerance="high"),
}


@dataclass
class DemandModel:
    base: float
    weekly_amp: float
    trend: float
    noise: float
    season_amp: float
    season_period: float
    phase: float
    phase2: float
    zero_prob: float

    def level(self, day: int) -> float:
        lvl = self.base * (1 + self.weekly_amp * math.sin(2 * math.pi * (day % 7) / 7 + self.phase)) * (1 + self.trend * day)
        if self.season_amp:
            lvl *= 1 + self.season_amp * math.sin(2 * math.pi * day / self.season_period + self.phase2)
        return max(lvl, 0.0)

    def draw(self, day: int, rng: random.Random) -> float:
        if self.zero_prob and rng.random() < self.zero_prob:
            return 0.0
        lvl = self.level(day) / max(1e-9, (1 - self.zero_prob))
        return max(0.0, rng.gauss(lvl, math.sqrt(lvl) * self.noise))


@dataclass
class Dataset:
    profile: Profile
    seed: int
    as_of: date                                       # the day AFTER the last observed demand
    skus: list[dict] = field(default_factory=list)       # Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock, ref_price
    suppliers: list[dict] = field(default_factory=list)  # Vendor_ID, Vendor_Name, Contact_Email, Reliability(prior), Region
    quotes: list[dict] = field(default_factory=list)     # Vendor_ID, Item_ID, Unit_Price, Lead_Time_Days, MOQ, Capacity
    history: dict[str, list[float]] = field(default_factory=dict)
    deliveries: list[dict] = field(default_factory=list)
    truth: dict = field(default_factory=dict)            # HIDDEN from the platform: demand models, true reliability / quality, future demand
    provenance: str = DATA_PROVENANCE


def _u(rng: random.Random, pair: tuple[float, float]) -> float:
    return rng.uniform(pair[0], pair[1])


def generate(profile: Profile | str, seed: int = 1, scale: float = 1.0, as_of: date | None = None) -> Dataset:
    """Deterministic for a given (profile, seed, scale). `scale` shrinks the catalogue for fast tests (e.g. 0.1)."""
    p = PROFILES[profile] if isinstance(profile, str) else profile
    rng = random.Random(f"{p.key}-{seed}")
    as_of = as_of or date(2026, 1, 1)
    n_skus, n_sup = max(3, round(p.n_skus * scale)), max(3, round(p.n_suppliers * max(scale, 0.3)))
    ds = Dataset(p, seed, as_of)
    regions = ("US", "EU", "APAC")
    true_rel, true_q = {}, {}
    for i in range(n_sup):
        vid = f"S{i + 1:02d}"
        true_rel[vid] = rng.uniform(*p.reliability)
        true_q[vid] = rng.uniform(*p.quality)
        prior = max(40.0, min(99.0, true_rel[vid] + rng.gauss(0, 6)))            # what the vendor master says: a noisy estimate of the truth
        ds.suppliers.append({"Vendor_ID": vid, "Vendor_Name": f"{p.key.title()} Supplier {i + 1}", "Contact_Email": f"orders@{p.key}-s{i + 1}.example",
                             "Reliability": round(prior, 1), "Region": regions[i % 3]})
    models: dict[str, DemandModel] = {}
    total_days = p.history_days + p.eval_days
    for i in range(n_skus):
        sku = f"{p.key[:3].upper()}-{i + 1:04d}"
        m = DemandModel(base=_u(rng, p.base_demand), weekly_amp=_u(rng, p.weekly_amp), trend=_u(rng, p.trend_per_day), noise=_u(rng, p.noise),
                        season_amp=p.season_amp, season_period=p.season_period * rng.uniform(0.9, 1.1), phase=rng.uniform(0, 6.28), phase2=rng.uniform(-0.6, 0.6),
                        zero_prob=_u(rng, p.zero_prob) if p.zero_prob[1] > 0 else 0.0)
        models[sku] = m
        series = [round(m.draw(d, rng), 2) for d in range(total_days)]
        ds.history[sku] = series[:p.history_days]
        ds.truth.setdefault("future_demand", {})[sku] = series[p.history_days:]
        ref = round(rng.uniform(*p.price), 2)
        chosen = rng.sample(ds.suppliers, k=min(len(ds.suppliers), rng.randint(*p.listings_per_sku)))
        leads = []
        for s in chosen:
            lead = rng.randint(*p.lead_time)
            leads.append(lead)
            ds.quotes.append({"Vendor_ID": s["Vendor_ID"], "Item_ID": sku, "Unit_Price": round(ref * rng.uniform(1 - p.price_spread, 1 + p.price_spread), 2),
                              "Lead_Time_Days": lead, "MOQ": rng.choice(p.moq_choices), "Capacity": rng.choice(p.capacity_choices)})
        mean = sum(ds.history[sku][-28:]) / 28
        avg_lead = sum(leads) / len(leads)
        rop = max(1, round(mean * (avg_lead + p.rop_cover_days_extra)))                # the customer's legacy static rule
        target = max(rop + 5, round(mean * (avg_lead + p.target_days)))
        stock = max(0, round(mean * _u(rng, p.cover_days)))
        ds.skus.append({"Item_ID": sku, "Item_Name": f"{p.key.title()} item {i + 1}", "Category": p.categories[i % len(p.categories)], "Stock_Level": stock,
                        "Reorder_Point": rop, "Target_Stock": target, "ref_price": ref})
    # delivery history the platform can LEARN from: drawn from the TRUE reliability / quality, not from the noisy prior
    for s in ds.suppliers:
        vid = s["Vendor_ID"]
        mine = [q for q in ds.quotes if q["Vendor_ID"] == vid]
        for _ in range(35 if mine else 0):
            q = rng.choice(mine)
            ordered = rng.choice([40, 80, 150, 300])
            on_time = rng.random() < true_rel[vid] / 100
            recv = ordered if rng.random() < true_rel[vid] / 100 else int(ordered * rng.uniform(0.75, 0.98))
            at = (as_of - timedelta(days=rng.randint(5, 170))).isoformat()
            if rng.random() < 0.03 * (1 - true_rel[vid] / 100) * 10:
                ds.deliveries.append({"Vendor_ID": vid, "Item_ID": q["Item_ID"], "Ordered_At": at, "Promised_Days": q["Lead_Time_Days"], "Actual_Days": None,
                                      "Qty_Ordered": ordered, "Qty_Received": 0, "Qty_Defective": 0, "Unit_Price": q["Unit_Price"], "Status": "CANCELLED"})
            else:
                ds.deliveries.append({"Vendor_ID": vid, "Item_ID": q["Item_ID"], "Ordered_At": at, "Promised_Days": q["Lead_Time_Days"],
                                      "Actual_Days": q["Lead_Time_Days"] + (0 if on_time else rng.randint(1, 6)), "Qty_Ordered": ordered, "Qty_Received": recv,
                                      "Qty_Defective": int(recv * (1 - true_q[vid]) * rng.uniform(0.2, 1.0)), "Unit_Price": round(q["Unit_Price"] * rng.uniform(0.92, 1.08), 2),
                                      "Status": "DELIVERED"})
    ds.truth.update(models=models, reliability=true_rel, quality=true_q)
    return ds


def load_into_current_tenant(ds: Dataset, replace: bool = True) -> dict:
    """Write the dataset into the CURRENT tenant's database (call inside `tenancy.scope`). Replaces existing master data when `replace`."""
    with db.connect(write=True) as c:
        if replace:
            for t in ("demand_history", "supplier_deliveries", "supplier_metrics", "vendor_listings", "purchase_requests", "purchase_orders", "forecasts",
                      "inventory_by_warehouse", "vendors", "inventory"):
                c.execute(f"DELETE FROM {t}")
        c.executemany("INSERT INTO inventory (Item_ID, Item_Name, Category, Stock_Level, Reorder_Point, Target_Stock) VALUES (?,?,?,?,?,?)",
                      [(s["Item_ID"], s["Item_Name"], s["Category"], s["Stock_Level"], s["Reorder_Point"], s["Target_Stock"]) for s in ds.skus])
        c.executemany("INSERT INTO vendors (Vendor_ID, Vendor_Name, Base_Price, Contact_Email, Reliability, Region) VALUES (?,?,?,?,?,?)",
                      [(v["Vendor_ID"], v["Vendor_Name"], 1.0, v["Contact_Email"], v["Reliability"], v["Region"]) for v in ds.suppliers])
        c.executemany("INSERT INTO vendor_listings (Vendor_ID, Item_ID, Unit_Price, Lead_Time_Days, MOQ, Capacity) VALUES (?,?,?,?,?,?)",
                      [(q["Vendor_ID"], q["Item_ID"], q["Unit_Price"], q["Lead_Time_Days"], q["MOQ"], q["Capacity"]) for q in ds.quotes])
        rows = []
        for sku, series in ds.history.items():
            for i, qty in enumerate(series):
                rows.append((sku, (ds.as_of - timedelta(days=len(series) - i)).isoformat(), qty))
        c.executemany("INSERT INTO demand_history (Item_ID, Day, Quantity) VALUES (?,?,?)", rows)
        c.executemany("INSERT INTO supplier_deliveries (Vendor_ID, Item_ID, Ordered_At, Promised_Days, Actual_Days, Qty_Ordered, Qty_Received, Qty_Defective, "
                      "Unit_Price, Status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      [(d["Vendor_ID"], d["Item_ID"], d["Ordered_At"], d["Promised_Days"], d["Actual_Days"], d["Qty_Ordered"], d["Qty_Received"], d["Qty_Defective"],
                        d["Unit_Price"], d["Status"]) for d in ds.deliveries])
        c.execute("INSERT INTO app_state (Key, Value) VALUES ('data_provenance', ?) ON CONFLICT(Key) DO UPDATE SET Value=excluded.Value", (ds.provenance,))
    from scm import suppliers
    suppliers.invalidate_cache()
    return {"skus": len(ds.skus), "suppliers": len(ds.suppliers), "quotes": len(ds.quotes), "history_rows": len(rows), "deliveries": len(ds.deliveries),
            "provenance": ds.provenance}
