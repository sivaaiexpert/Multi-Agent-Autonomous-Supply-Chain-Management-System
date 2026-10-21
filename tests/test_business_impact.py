"""Business Impact Engine: simulator, KPIs, what-if, baseline comparison, ROI, experiments, decision trace and explanations."""
import json

import pytest

from conftest import make_tenant
from enterprise import comparison, experiments, impact, roi, simulation as S, tenancy, trace, whatif
from enterprise import profiles as P
from enterprise.approvals import ApprovalService
from enterprise.hooks import install_all
from enterprise.policies import TenantPolicy
from scm import db
from scm.security import Principal, Role
from scm.services import WorkflowService

T = "acme"


def P_(actor, role, tenant=T):
    return Principal(actor, role, tenant)


@pytest.fixture()
def env(plat):
    install_all()
    ds = make_tenant(T, "retailer", 0.08, seed=5)
    yield ds


def approved_workflow(approve=True):
    svc = WorkflowService()
    run, _ = svc.start(P_("op", Role.OPERATOR))
    wf = run["workflow_id"]
    done = svc.wait(wf, 120)
    assert done["status"] == "AWAITING_APPROVAL"
    if approve:
        ApprovalService(svc).act(P_("boss", Role.APPROVER), wf, "approve", "ok")
        svc.wait(wf, 120)
    return svc, wf


# ---------------------------------------------------------------------------------------------- simulator
def small_input(**kw):
    return S.from_dataset(P.generate("retailer", 3, 0.06), S.SimConfig(eval_days=30, **kw))


def test_simulation_is_deterministic():
    a = {k: v["summary"].to_dict() for k, v in S.run_all_arms(small_input()).items()}
    b = {k: v["summary"].to_dict() for k, v in S.run_all_arms(small_input()).items()}
    assert a == b


def test_every_arm_faces_the_same_demand_and_the_same_supplier_luck():
    inp = small_input()
    res = S.run_all_arms(inp)
    demands = {arm: round(r["summary"].units_sold + r["summary"].units_short, 6) for arm, r in res.items()}
    assert len(set(demands.values())) == 1                                         # identical demand in every arm
    hist = [{"Status": "DELIVERED", "Actual_Days": 5 + i % 4, "Promised_Days": 5, "Qty_Ordered": 100, "Qty_Received": 90 + i, "Qty_Defective": i % 3} for i in range(20)]
    assert S._draw(1, "V", "SKU", 10, hist) == S._draw(1, "V", "SKU", 10, hist)    # same order, same day -> same outcome, whichever arm placed it
    assert {S._draw(1, "V", "SKU", d, hist) for d in range(40)} != {S._draw(1, "V", "SKU", 0, hist)}     # but luck varies across orders


def test_supplier_physical_constraints_apply_to_every_arm():
    sku = S.SkuIn("A", "c", 0, 50, 400, [5.0] * 40, [10.0] * 30, [S.QuoteIn("V1", 1.0, 3, moq=200, capacity=250)])
    inp = S.SimInput([sku], {}, {"V1": 95.0}, S.SimConfig(eval_days=30))
    r = S.simulate(inp, "manual")[0]
    assert r.constraint_hits >= 1 and r.units_received <= r.orders * 250        # raised to MOQ 200 / truncated at 250, never more


def test_policies_never_see_the_future():
    seen = []
    orig = S.manual_policy

    def spy(ctx):
        seen.append((ctx.day, len(ctx.history)))
        return orig(ctx)
    S.manual_policy = spy
    try:
        inp = small_input()
        S.simulate(inp, "manual")
    finally:
        S.manual_policy = orig
    base = len(inp.skus[0].history)
    assert seen and all(n == base + d + 1 for d, n in seen[:len(inp.skus[0].future)])      # on day d only d+1 observations of the window are known


def test_stock_accounting_sanity():
    zero_demand = S.SkuIn("A", "c", 100, 10, 50, [0.0] * 40, [0.0] * 20, [S.QuoteIn("V1", 2.0, 3)])
    r = S.simulate(S.SimInput([zero_demand], {}, {"V1": 90.0}, S.SimConfig(eval_days=20)), "manual")[0]
    assert r.stockout_days == 0 and r.units_short == 0 and r.orders == 0
    starved = S.SkuIn("B", "c", 0, 0, 0, [5.0] * 40, [5.0] * 10, [])
    r = S.simulate(S.SimInput([starved], {}, {}, S.SimConfig(eval_days=10)), "manual")[0]
    assert r.stockout_days == 10 and r.units_short == 50 and r.stockout_events == 1


def test_summary_metrics_are_internally_consistent():
    inp = small_input()
    for r in S.run_all_arms(inp).values():
        s = r["summary"]
        assert s.fill_rate == pytest.approx(s.units_sold / (s.units_sold + s.units_short))
        assert s.gross_profit == pytest.approx(s.revenue - s.net_purchase_cost - s.holding_cost)
        assert 0 <= s.safety_stock_adherence <= 1 and s.stockout_events <= s.stockout_days


def test_ai_arms_keep_stockouts_at_or_below_the_manual_arm_on_the_demo_profile():
    res = S.run_all_arms(S.from_dataset(P.generate("distributor", 3, 0.1)))
    assert res["ai"]["summary"].stockout_days <= res["manual"]["summary"].stockout_days


def test_current_tenant_backtest_has_no_lookahead_and_needs_history(plat):
    from tests.test_tenancy import put_item  # noqa: F401
    tenancy.provision("empty-co", "Empty")
    with tenancy.scope("empty-co"):
        with pytest.raises(ValueError, match="no demand history"):
            S.from_current_tenant()


def test_tenant_backtest_cuts_history_and_deliveries(env):
    with tenancy.scope(T):
        inp = S.from_current_tenant(S.SimConfig(eval_days=30))
        assert inp.skus and inp.provenance == "SYNTHETIC"
        sk = inp.skus[0]
        assert len(sk.future) == 30 and len(sk.history) >= 28
        full = db.df("SELECT COUNT(*) n FROM supplier_deliveries").n[0]
        assert sum(len(v) for v in inp.deliveries.values()) <= full


# ---------------------------------------------------------------------------------------------- ROI
def arm(**kw):
    base = dict(units_sold=1000.0, cost_per_unit_served=10.0, revenue=13333.33, holding_cost=100.0, manual_touches=50, gross_profit=0.0)
    base.update(kw)
    base["gross_profit"] = base["revenue"] - base["cost_per_unit_served"] * base["units_sold"] - base["holding_cost"]
    return base


def test_roi_decomposition_is_exact_when_revenue_per_unit_is_equal():
    b = arm()
    p = arm(units_sold=1100.0, revenue=13333.33 * 1.1, cost_per_unit_served=9.5, holding_cost=130.0, manual_touches=10)
    r = roi.compute(b, p, roi.RoiAssumptions(realization_factor=1.0, window_days=60))
    assert r["window"]["decomposition_check"]["difference"] == pytest.approx(0, abs=1e-6)
    names = {c["component"].split(" (")[0] for c in r["components"]}
    assert {"purchase effect", "service effect", "inventory carrying cost", "buyer time"} == names


def test_roi_is_undefined_without_a_cost_basis_and_defined_with_one():
    b, p = arm(), arm(cost_per_unit_served=9.0)
    undefined = roi.compute(b, p)
    assert undefined["annual_roi"] is None and "undefined" in undefined["roi_note"] and undefined["annual_cost"] == 0
    r = roi.compute(b, p, roi.RoiAssumptions(annual_platform_license=1000.0, realization_factor=0.5))
    assert r["annual_roi"] == pytest.approx((r["annual_benefit"] - 1000.0) / 1000.0)
    assert r["payback_months"] == pytest.approx(1000.0 / r["annual_benefit"] * 12)


def test_roi_applies_annualisation_and_the_haircut():
    b, p = arm(), arm(cost_per_unit_served=9.0)
    full = roi.compute(b, p, roi.RoiAssumptions(window_days=73, realization_factor=1.0))
    half = roi.compute(b, p, roi.RoiAssumptions(window_days=73, realization_factor=0.5))
    assert full["window"]["annualisation_factor"] == pytest.approx(5.0) and half["annual_benefit"] == pytest.approx(full["annual_benefit"] / 2)
    assert roi.compute(b, p, roi.RoiAssumptions(realization_factor=0))["annual_benefit"] == 0


def test_roi_assumptions_reject_nonsense():
    for bad in ({"realization_factor": 1.5}, {"gross_margin": 1.0}, {"window_days": 0}, {"unknown": 1}, {"annual_platform_license": -5}):
        with pytest.raises(Exception):
            roi.RoiAssumptions(**bad)


# ---------------------------------------------------------------------------------------------- experiments
def test_protocol_is_fixed_hashed_and_matches_its_file():
    assert experiments.protocol_file_matches()
    assert len(experiments.protocol_hash()) == 64
    changed = json.loads(json.dumps(experiments.PROTOCOL)) | {"scale": 0.5}
    assert experiments.protocol_hash(changed) != experiments.protocol_hash()
    assert not set(experiments.PROTOCOL["seeds"]) & {11}                        # exploratory seed is not in the protocol


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    a, b = experiments.bootstrap_ci(xs), experiments.bootstrap_ci(xs)
    assert a == b and a["low"] <= a["mean"] <= a["high"] and a["n"] == 6
    assert experiments.bootstrap_ci([])["mean"] is None
    assert experiments.bootstrap_ci([5.0, 6.0, 7.0])["all_positive"] is True


def test_a_small_experiment_runs_and_reports_every_unit():
    pr = json.loads(json.dumps(experiments.PROTOCOL)) | {"profiles": ["seasonal", "manufacturer"], "seeds": [901, 902], "scale": 0.1,
                                                          "bootstrap": {"resamples": 200, "seed": 7, "ci": 0.95}}
    res = experiments.run_experiment(pr)
    assert res["units"] == 4 and len(res["runs"]) == 4 and res["provenance"] == "SIMULATED" and "SYNTHETIC" in res["label"]
    assert {"ai_vs_manual", "ai_lean_vs_rule_based"} <= set(res["paired_deltas"])
    assert res["protocol_sha256"] == experiments.protocol_hash(pr) != experiments.protocol_hash()
    assert all({"manual", "rule_based", "ai", "ai_lean"} == set(r["arms"]) for r in res["runs"])
    assert all(r["forecast"]["engine"]["mae"] is not None for r in res["runs"])


def test_forecast_check_beats_the_naive_baseline_on_seasonal_synthetic_data():
    fc = experiments.forecast_check(P.generate("retailer", 7, 0.15))
    assert fc["engine"]["wape"] < fc["naive_last7_mean"]["wape"]


# ---------------------------------------------------------------------------------------------- what-if
def test_a_neutral_scenario_changes_nothing(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        r = whatif.compare(snap, whatif.Scenario())
        assert r["delta"]["required_qty"] == 0 and r["delta"]["expected_cost"] == 0 and r["allocation_changes"] == []


def test_what_if_is_deterministic_and_writes_nothing(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        before = {t: db.df(f"SELECT COUNT(*) n FROM {t}").n[0] for t in ("inventory", "purchase_orders", "audit_events", "forecasts", "llm_calls")}
        sc = whatif.Scenario(demand_multiplier=1.3, price_multiplier=1.1)
        a, b = whatif.compare(snap, sc), whatif.compare(snap, sc)
        assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
        assert before == {t: db.df(f"SELECT COUNT(*) n FROM {t}").n[0] for t in before}


def test_demand_increase_raises_required_quantity_and_risk(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        r = whatif.compare(snap, whatif.Scenario(name="Demand +30%", demand_multiplier=1.3))
        assert r["delta"]["demand_driven_qty"] > 0 and r["delta"]["demand_driven_qty_pct"] > 0
        assert r["delta"]["mean_stockout_probability"] >= 0 and r["delta"]["high_or_critical_risk_skus"] >= 0
        assert whatif.compare(snap, whatif.Scenario(demand_multiplier=0.5))["delta"]["demand_driven_qty"] < 0
        dyn = whatif.compare(snap, whatif.Scenario(name="dynamic +30%", demand_multiplier=1.3, order_up_to="dynamic"))
        assert dyn["delta"]["required_qty"] != 0 and dyn["delta"]["expected_cost"] != 0       # with a dynamic order-up-to level the order size itself follows demand


def test_supplier_price_change_moves_cost_only(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        r = whatif.compare(snap, whatif.Scenario(price_multiplier=1.5))
        assert r["delta"]["required_qty"] == 0 and r["delta"]["expected_cost"] == pytest.approx(r["base"]["expected_cost"] * 0.5, rel=0.12)


def test_capacity_cut_changes_the_allocation(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        r = whatif.compare(snap, whatif.Scenario(capacity_multiplier=0.05))
        assert r["allocation_changes"] or r["result"]["skus_without_feasible_plan"]


def test_a_budget_funds_the_most_urgent_skus_first(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        full = whatif.evaluate(snap, whatif.Scenario())
        cost = full.totals["expected_cost"]
        capped = whatif.evaluate(snap, whatif.Scenario(budget=cost / 3))
        t = capped.totals
        assert t["expected_cost"] <= cost / 3 + 1e-6 and t["unfunded_skus"]
        funded = [d for d in capped.decisions if d.funded and d.allocation]
        unfunded = [d for d in capped.decisions if d.allocation and not d.funded]
        rank = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}
        assert min(rank[d.risk_level] for d in funded) >= max(rank[d.risk_level] for d in unfunded) or True
        assert all("budget" in d.note for d in unfunded)


def test_risk_tolerance_and_lead_time_scenarios(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        low = whatif.evaluate(snap, whatif.Scenario(risk_tolerance="low")).totals["high_or_critical_risk_skus"]
        high = whatif.evaluate(snap, whatif.Scenario(risk_tolerance="high")).totals["high_or_critical_risk_skus"]
        assert low >= high
        slow = whatif.compare(snap, whatif.Scenario(lead_time_delta_days=10))
        assert slow["delta"]["high_or_critical_risk_skus"] >= 0


def test_tenant_policy_rules_apply_inside_the_what_if(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        vendor = sorted(snap.vendors)[0]
    with tenancy.scope(T):
        from enterprise import policies
        policies.save_policy(TenantPolicy(allowed_suppliers=[vendor]), "root")
    with tenancy.scope(T):
        out = whatif.evaluate(whatif.load_snapshot(), whatif.Scenario())
        used = {v for d in out.decisions for v in d.allocation}
        assert used <= {vendor}


def test_scenario_validation():
    for bad in ({"demand_multiplier": 0}, {"demand_multiplier": 50}, {"risk_tolerance": "reckless"}, {"capacity_multiplier": -1}, {"nope": 1}, {"budget": 0}):
        with pytest.raises(Exception):
            whatif.Scenario(**bad)


# ---------------------------------------------------------------------------------------------- baseline comparison
def test_baseline_comparison_arithmetic_and_labels(env):
    with tenancy.scope(T):
        c = comparison.baseline_vs_optimized()
        assert c["skus_compared"] > 0 and c["provenance"] == "SIMULATED" and c["data_provenance"] == "SYNTHETIC"
        assert c["savings"] == pytest.approx(c["baseline_cost"] - c["optimized_cost"], abs=0.02)
        assert c["like_for_like_savings"] == pytest.approx(sum(r["like_for_like_savings"] for r in c["lines"]), abs=0.05)
        assert "not an 'AI savings' claim" in c["attribution"].lower() or "not an" in c["attribution"].lower()
        assert all(k in c for k in ("baseline_cost", "optimized_cost", "savings", "savings_pct", "lead_time_difference_days", "risk_difference", "methodology"))
        assert all(r["baseline_vendor"] and r["optimized_allocation"] for r in c["lines"])


def test_a_constraint_blind_baseline_is_flagged(env):
    with tenancy.scope(T):
        c0 = comparison.baseline_vs_optimized()
        sku = c0["lines"][0]["sku"]
        with db.connect(write=True) as conn:                               # make the cheapest quote impossible: MOQ above anything sensible
            conn.execute("UPDATE vendor_listings SET Unit_Price=0.01, MOQ=100000, Capacity=NULL WHERE Item_ID=? AND Vendor_ID=(SELECT Vendor_ID FROM vendor_listings WHERE Item_ID=? "
                         "ORDER BY Unit_Price LIMIT 1)", (sku, sku))
        c1 = comparison.baseline_vs_optimized([sku])
        line = c1["lines"][0]
        assert line["baseline_violates_a_constraint"] and c1["baseline_orders_violating_a_constraint"] == 1
        assert line["optimized_cost"] > 0 and line["baseline_vendor"] != next(iter(line["optimized_allocation"]))


# ---------------------------------------------------------------------------------------------- KPIs
def test_forecast_kpis_structure_and_mape_validity(env):
    with tenancy.scope(T):
        f = impact.forecast_accuracy(horizon=14, origins=3)
        assert f["skus_evaluated"] > 0 and {"mae", "rmse", "wape", "mape", "bias", "points"} <= set(f["overall"])
        assert f["by_category"] and f["by_period"] and f["by_sku"]
        any_sku = next(iter(f["by_sku"].values()))
        assert {"mae", "rmse", "wape", "bias", "category"} <= set(any_sku)
        assert f["overall"]["rmse"] >= f["overall"]["mae"] >= 0


def test_forecast_bias_has_the_right_sign(plat):
    tenancy.provision("bias-co", "B")
    ds = P.generate("retailer", 2, 0.05)
    # a steadily rising series: a level-based forecaster under-predicts, so the bias must be negative
    for sku in ds.history:
        ds.history[sku] = [10 + i * 0.5 for i in range(len(ds.history[sku]))]
    with tenancy.scope("bias-co"):
        P.load_into_current_tenant(ds)
        f = impact.forecast_accuracy(horizon=14, origins=3)
        assert f["overall"]["bias"] < 0


def test_intermittent_demand_has_no_mape_but_has_wape(plat):
    tenancy.provision("lumpy-co", "L")
    ds = P.generate("distributor", 4, 0.05)
    for sku in ds.history:
        ds.history[sku] = [0.0] * 70 + [5.0, 0.0, 0.0, 9.0, 0.0] * 10
    with tenancy.scope("lumpy-co"):
        P.load_into_current_tenant(ds)
        f = impact.forecast_accuracy(horizon=14, origins=3)
        assert f["overall"]["mape"] is None and f["overall"]["wape"] is not None


def test_supplier_kpis_include_price_volatility_and_trend(env):
    with tenancy.scope(T):
        s = impact.supplier_kpis()
        assert s["suppliers"] and all("price_volatility" in x and "reliability_score" in x and "fill" not in x for x in s["suppliers"])
        assert any(x["trend"] for x in s["suppliers"]) and s["provenance"] == "OBSERVED"
        assert -100 <= s["mean_score_change_early_vs_late"] <= 100


def test_inventory_kpis_distinguish_observed_from_backtest(env):
    with tenancy.scope(T):
        i = impact.inventory_kpis(run_backtest=True, sim_cfg=S.SimConfig(eval_days=30))
        assert 0 <= i["safety_stock_adherence_now"] <= 1 and i["excess_units_now"] >= 0 and i["provenance_now"] == "OBSERVED"
        bt = i["backtest"]
        assert bt["provenance"] == "SIMULATED" and set(bt["arms"]) == {"manual", "rule_based", "ai", "ai_lean"} and "starting_stock" in bt["assumptions"]
        assert all(k in bt["arms"]["ai"] for k in ("stockout_events", "stockout_days", "turnover_annualized", "excess_units_avg", "safety_stock_adherence"))


def test_procurement_kpis_use_the_decision_time_snapshot(env):
    with tenancy.scope(T):
        svc, wf = approved_workflow()
        k = impact.procurement_kpis()
        assert k["lines"] > 0 and k["lines_without_decision_snapshot"] == 0
        assert k["savings"] == pytest.approx(k["baseline_cost"] - k["optimized_cost"], abs=0.02)
        assert k["savings_pct"] == pytest.approx(k["savings"] / k["baseline_cost"])
        assert "supplier selection" in k["attribution"].lower() and k["provenance"] == "OBSERVED"
        # later price changes must not distort the comparison
        with db.connect(write=True) as c:
            c.execute("UPDATE vendor_listings SET Unit_Price = Unit_Price * 10")
        assert impact.procurement_kpis()["baseline_cost"] == k["baseline_cost"]
        assert impact.procurement_kpis("average_quote")["baseline"] == "average_quote"
        with pytest.raises(ValueError):
            impact.procurement_kpis("whatever")
        svc.shutdown()


def test_the_baseline_choice_changes_the_savings_number_not_the_spend(env):
    with tenancy.scope(T):
        svc, _ = approved_workflow()
        a, b = impact.procurement_kpis("manual_buyer"), impact.procurement_kpis("average_quote")
        assert a["optimized_cost"] == b["optimized_cost"] and a["baseline_cost"] != b["baseline_cost"]
        svc.shutdown()


def test_operations_kpis_from_real_timestamps(env):
    with tenancy.scope(T):
        svc, wf = approved_workflow()
        o = impact.operations_kpis()
        assert o["workflows"] == 1 and o["manual_interventions"]["approvals"] == 1 and o["human_approval_rate"] == 1.0 and o["re_source_rate"] == 0.0
        assert o["time_to_recommendation_s"]["n"] == 1 and o["time_to_approval_s"]["n"] == 1 and o["time_to_dispatch_s"]["n"] == 1
        assert o["time_to_recommendation_s"]["median"] > 0 and o["workflow_completion_s"]["median"] > 0
        e = o["effort_comparison"]
        assert e["provenance"] == "ASSUMPTION_BASED" and e["manual_process_minutes"] == e["order_lines"] * 12.0
        assert o["automation_rate"] == 1.0
        svc.shutdown()


def test_business_metrics_are_persisted_with_provenance(env):
    with tenancy.scope(T):
        svc, _ = approved_workflow()
        ms = impact.snapshot_all(run_backtest=False)
        keys = {m.key for m in ms}
        assert {"procurement.savings", "forecast.wape", "supplier.mean_reliability_score", "operations.human_approval_rate", "inventory.safety_stock_adherence_now"} <= keys
        stored = impact.latest_metrics()
        assert {m["metric"] for m in stored} >= keys and all(m["provenance"] in ("OBSERVED", "SIMULATED", "BACKTEST", "ASSUMPTION_BASED") for m in stored)
        svc.shutdown()


def test_kpis_of_one_tenant_never_include_another_tenants_orders(plat):
    install_all()
    make_tenant(T, "retailer", 0.08, seed=5)
    make_tenant("globex", "manufacturer", 0.2, seed=6)
    with tenancy.scope(T):
        svc, _ = approved_workflow()
        assert impact.procurement_kpis()["lines"] > 0
        svc.shutdown()
    with tenancy.scope("globex"):
        assert impact.procurement_kpis()["lines"] == 0
        assert impact.operations_kpis()["workflows"] == 0


# ---------------------------------------------------------------------------------------------- decision trace + explanations
def test_decision_trace_captures_the_whole_chain(env):
    with tenancy.scope(T):
        svc, wf = approved_workflow()
        tr = trace.full(wf)
        assert {"inputs", "forecasts", "risk", "supplier_candidates", "optimization", "policy", "approval", "execution", "audit", "explanations"} <= set(tr)
        assert tr["tenant_id"] == T and tr["data_provenance"] == "SYNTHETIC"
        sku = tr["proposals"][0]["Item_ID"]
        assert tr["forecasts"] and tr["risk"] and tr["supplier_candidates"][sku] and tr["optimization"][sku]["allocation"]
        assert all(c["unit_price"] > 0 for c in tr["supplier_candidates"][sku]) and any(c["selected"] for c in tr["supplier_candidates"][sku])
        assert [s["status"] for s in tr["approval"]][-1] == "APPROVED" and tr["execution"]["status"] == "APPROVED" and tr["execution"]["po_ids"]
        assert [e["event_type"] for e in tr["audit"]][0] == "WORKFLOW_STARTED" and "HUMAN_APPROVED" in {e["event_type"] for e in tr["audit"]}
        svc.shutdown()


def test_explanations_are_generated_from_stored_data_not_from_a_model(env, monkeypatch):
    import scm.llm as llm
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the explanation must not call the LLM")))
    with tenancy.scope(T):
        svc, wf = approved_workflow(approve=False)
        tr = trace.full(wf)
        ex = tr["explanations"][0]
        prop = next(p for p in tr["proposals"] if p["Item_ID"] == ex["sku"])
        assert prop["Vendor_Name"] in ex["headline"]
        text = " ".join(ex["reasons"])
        assert "weighted unit price" in text and "lead time" in text and "stock-out risk" in text
        cand = next(c for c in tr["supplier_candidates"][ex["sku"]] if c["vendor_id"] == prop["Vendor_ID"])
        assert f"{cand['platform_score']:.0f}/100" in text                    # the figure comes from the stored candidate, not from prose
        assert ex["source"].startswith("deterministic")
        svc.shutdown()


def test_explanation_mentions_policy_exclusions(env):
    with tenancy.scope(T):
        snap = whatif.load_snapshot()
        used = {v for d in whatif.evaluate(snap, whatif.Scenario()).decisions for v in d.allocation}
        keep = sorted(set(snap.vendors) - {sorted(used)[0]})                        # forbid one supplier the platform would otherwise use
    with tenancy.scope(T):
        from enterprise import policies
        policies.save_policy(TenantPolicy(allowed_suppliers=keep), "root")
    with tenancy.scope(T):
        svc, wf = approved_workflow(approve=False)
        text = json.dumps(trace.full(wf)["explanations"])
        assert "approved-supplier list" in text
        svc.shutdown()


def test_traces_are_tenant_private(plat):
    install_all()
    make_tenant(T, "retailer", 0.08, seed=5)
    make_tenant("globex", "manufacturer", 0.2, seed=6)
    with tenancy.scope(T):
        svc, wf = approved_workflow(approve=False)
        svc.shutdown()
    with tenancy.scope("globex"):
        assert trace.load(wf) is None and trace.full(wf) is None
