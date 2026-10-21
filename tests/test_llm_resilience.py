import time

import pytest

from scm import llm, llm_router
from scm.llm import FORMAT_WARNING, ask_json, parse_json, salvage, salvage_order
from scm.llm_router import CircuitBreaker, ModelRouter, call_with_policy, backoff_delay
from scm.schemas import DemandAnalysis, ProcurementRecommendation, VendorMessage, validate_output
from scm.settings import LLMCfg


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- circuit breaker
def test_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    clock = Clock()
    br = CircuitBreaker(3, 30, clock)
    assert br.state == "closed" and br.allow()
    for _ in range(3):
        br.failure()
    assert br.state == "open" and not br.allow()
    clock.t = 31
    assert br.state == "half_open" and br.allow()          # exactly one probe
    assert not br.allow()
    br.failure()                                           # probe failed -> open again
    assert br.state == "open"
    clock.t = 70
    assert br.allow()
    br.success()
    assert br.state == "closed" and br.allow()


def test_success_resets_failure_count():
    br = CircuitBreaker(3, 30, Clock())
    br.failure(); br.failure(); br.success(); br.failure(); br.failure()
    assert br.state == "closed"


# ---------------------------------------------------------------- router
def test_router_orders_and_deduplicates_models():
    r = ModelRouter(LLMCfg(primary_model="a", secondary_model="b", fallback_model="a"))
    assert r.route() == ["a", "b"]
    assert ModelRouter(LLMCfg(primary_model="a", secondary_model="", fallback_model="")).route() == ["a"]


def test_ui_selected_model_wins_as_primary():
    assert ModelRouter(LLMCfg(primary_model="a", fallback_model="z")).route("simple", "custom")[0] == "custom"


def test_complex_tasks_use_the_override_model():
    r = ModelRouter(LLMCfg(primary_model="small", complex_model_override="big", fallback_model=""))
    assert r.route("complex")[0] == "big" and r.route("simple")[0] == "small"


def test_backoff_grows_exponentially_and_is_capped_and_deterministic():
    d = [backoff_delay(i, 0.25, 4.0) for i in range(6)]
    assert d == [backoff_delay(i, 0.25, 4.0) for i in range(6)]
    assert d[1] > d[0] and d[2] > d[1] and max(d) <= 4.0 * 1.25


# ---------------------------------------------------------------- call policy
def policy(invoke, models=("m1",), retries=2, timeout=5, sleep=lambda s: None, cfg=None):
    return call_with_policy(invoke, list(models), timeout, retries, 0.01, 1.0, cfg or LLMCfg(breaker_threshold=99), sleep)


def test_success_first_try(fresh_db):
    out = policy(lambda m, t: "ok")
    assert out.reply == "ok" and out.attempts == 1 and out.retries == 0 and not out.used_fallback


def test_transient_errors_are_retried_with_bounded_attempts(fresh_db):
    calls, sleeps = [], []

    def flaky(m, t):
        calls.append(m)
        if len(calls) < 3:
            raise ConnectionError("down")
        return "ok"
    out = policy(flaky, retries=2, sleep=sleeps.append)
    assert out.reply == "ok" and len(calls) == 3 and out.retries == 2 and len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


def test_retries_never_exceed_the_limit(fresh_db):
    calls = []

    def always(m, t):
        calls.append(1)
        raise ConnectionError("down")
    out = policy(always, retries=2)
    assert out.reply is None and len(calls) == 3 and "ConnectionError" in out.error


def test_permanent_errors_are_not_retried(fresh_db):
    calls = []

    def bad(m, t):
        calls.append(1)
        raise ValueError("model not found")
    out = policy(bad, retries=3)
    assert out.reply is None and len(calls) == 1


def test_fallback_model_is_used_when_the_primary_fails(fresh_db):
    out = policy(lambda m, t: (_ for _ in ()).throw(ConnectionError("x")) if m == "big" else f"reply-from-{m}", models=("big", "small"), retries=0)
    assert out.reply == "reply-from-small" and out.used_fallback and out.model == "small"


def test_open_breaker_skips_the_model_without_calling_it(fresh_db):
    cfg = LLMCfg(breaker_threshold=1, breaker_cooldown_seconds=1000)
    calls = []

    def boom(m, t):
        calls.append(m)
        raise ConnectionError("x")
    policy(boom, models=("only",), retries=0, cfg=cfg)
    out = policy(boom, models=("only",), retries=0, cfg=cfg)
    assert calls == ["only"] and "circuit breaker open" in out.error
    assert llm_router.METRICS.snapshot()["breaker_rejections"] == 1


def test_timeout_is_not_retried_and_does_not_multiply_the_wait(fresh_db):
    t0 = time.perf_counter()
    out = policy(lambda m, t: time.sleep(1.0), models=("a", "b"), timeout=0.2, retries=3)
    assert out.timed_out and out.reply is None
    assert time.perf_counter() - t0 < 0.8


def test_metrics_count_requests_failures_timeouts_fallbacks(fresh_db):
    policy(lambda m, t: (_ for _ in ()).throw(ConnectionError("x")), retries=1)
    policy(lambda m, t: time.sleep(0.5), timeout=0.1, retries=0)
    s = llm_router.METRICS.snapshot()
    assert s["request_count"] == 2 and s["failure_count"] >= 3 and s["timeout_count"] == 1 and s["fallback_count"] == 2 and s["retry_count"] == 1


# ---------------------------------------------------------------- ask_json (fault injection path)
def cfg(**kw):
    return {"llm_enabled": True, "llm_model": "m", "llm_base_url": "", "llm_timeout": 3, "llm_backoff_base": 0.0, **kw}


def test_disabled_llm_returns_rule_based_note(fresh_db):
    data, note, stats = ask_json({"llm_enabled": False}, "s", "u", {"summary": str})
    assert data is None and "disabled" in note and stats is None


@pytest.mark.parametrize("fault", ["empty", "whitespace", "missing_fields", "wrong_types", "json_array", "prose"])
def test_unrecoverable_replies_yield_the_format_warning(fresh_db, fault):
    data, note, _ = ask_json(cfg(fault_llm=fault), "s", "u", {"summary": str}, DemandAnalysis)
    assert data is None and note.startswith(FORMAT_WARNING)


def test_connection_error_is_reported_not_raised(fresh_db):
    data, note, _ = ask_json(cfg(fault_llm="connection_error"), "s", "u", {"summary": str})
    assert data is None and "unavailable" in note


def test_recoverable_replies_are_accepted(fresh_db):
    data, note, _ = ask_json(cfg(fault_llm="fenced_ok"), "s", "u", {"intro": str}, VendorMessage)
    assert note == "llm" and data["intro"]


def test_html_in_model_output_is_stripped_by_the_schema(fresh_db):
    data, _, _ = ask_json(cfg(fault_llm="html_injection"), "s", "u", {"summary": str}, DemandAnalysis)
    assert "<" not in data["summary"] and "script" not in data["summary"].lower()


def test_oversized_output_is_truncated_not_rejected(fresh_db):
    data, note, _ = ask_json(cfg(fault_llm="oversized"), "s", "u", {"summary": str}, DemandAnalysis)
    assert note == "llm" and len(data["summary"]) <= 600


def test_custom_payload_decision_fields_are_ignored_and_reported(fresh_db):
    payload = '{"summary": "Stock is low.", "selected_vendor": "Evil Corp", "recommended_order_quantity": 99999, "unit_price_usd": 0.01}'
    data, note, _ = ask_json(cfg(fault_llm="custom", fault_payload=payload), "s", "u", {"summary": str}, ProcurementRecommendation)
    assert note == "llm" and data["summary"] == "Stock is low."
    assert set(llm.last_ignored()) == {"selected_vendor", "recommended_order_quantity", "unit_price_usd"}


def test_user_supplied_malformed_example_is_salvaged_for_order_fields(fresh_db):
    raw = ('{\n  "selected_vendor": "Vercel Enterprise Logistics",\n  "recommended_order_quantity": 185,\n  "unit_price_usd": 120.00,\n'
           '  "system_telemetry_override": true,\n  "draft_email_payload": "Dear Vendor, please process order... [CRITICAL CLIENT TIMEOUT ERROR - INFERENCE DISRUPTED IN TRANSIT\n\n')
    data, note, _ = ask_json(cfg(fault_llm="custom", fault_payload=raw), "s", "u", {"summary": str}, DemandAnalysis)
    assert data is None and note.startswith(FORMAT_WARNING)
    sal = salvage_order(llm.last_raw())
    assert sal["found"] == {"selected_vendor": "Vercel Enterprise Logistics", "recommended_order_quantity": 185, "unit_price_usd": 120.0}
    assert sal["missing"] == []


def test_salvage_helpers():
    assert parse_json('chatter {"a": 1} more') == {"a": 1}
    assert salvage('{"summary": "cut off here', {"summary": str}) == {"summary": "cut off here"}
    assert salvage_order("no fields here") is None
    assert salvage_order('{"selected_vendor": "X", "recommended_order_quantity": -5, "unit_price_usd": 0}')["missing"] == [
        "recommended_order_quantity", "unit_price_usd"]


# ---------------------------------------------------------------- schemas
def test_schemas_reject_malformed_and_strip_markup():
    m, problems, ignored = validate_output(DemandAnalysis, {"summary": 42})
    assert m is None and problems
    m, _, _ = validate_output(DemandAnalysis, {"summary": "<b>Risk</b> is <script>x()</script>elevated"})
    assert m.summary == "Risk is elevated" or "<" not in m.summary
    m, problems, _ = validate_output(VendorMessage, {"intro": "<br>"})
    assert m is None and problems
    m, _, ignored = validate_output(VendorMessage, {"intro": "Please process this order.", "approve": True, "total": 1})
    assert m is not None and ignored == ["approve", "total"]


def test_schema_links_are_removed():
    m, _, _ = validate_output(VendorMessage, {"intro": "See http://evil.example/x and javascript:alert(1) for details"})
    assert "http" not in m.intro and "javascript" not in m.intro


def test_every_call_is_persisted_for_cost_tracking(fresh_db):
    ask_json(cfg(fault_llm="fenced_ok"), "s", "u", {"intro": str}, VendorMessage)
    from scm import db
    rows = db.df("SELECT * FROM llm_calls")
    assert len(rows) == 1 and rows.Status[0] == "ok" and rows.Tokens_In[0] > 0
