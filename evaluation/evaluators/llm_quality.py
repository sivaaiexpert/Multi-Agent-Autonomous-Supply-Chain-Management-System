"""LLM quality on a REAL local model (Ollama): structured-output validity, groundedness, instruction following, hallucination, consistency, latency.

This is deliberately a small live sample (a CPU-only llama3.2 takes tens of seconds per call). The 100+ scenario benchmark uses a scripted
model; this evaluator is where actual model behaviour is measured. TTFT is not measured: calls are non-streaming, so only total latency exists.
"""
from __future__ import annotations

import re
import tempfile
import time
import urllib.request
from pathlib import Path

from evaluation.metrics.stats import latency_summary, rate
from scm import db, seed
from scm.settings import get_settings

SKU = re.compile(r"SKU-[A-Za-z0-9\-]+")


def ollama_available(base_url: str, model: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3) as r:
            return model.split(":")[0] in r.read().decode()
    except Exception:
        return False


def _grounded(text: str, valid_skus: set[str], valid_vendors: list[str]) -> tuple[bool, list[str]]:
    """A reply is grounded when every SKU id it names exists. Vendor names (when mentioned) must be real ones."""
    problems = [s for s in SKU.findall(text) if s not in valid_skus]
    return not problems, problems


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def run(n_workflows: int = 3, model: str | None = None, base_url: str | None = None, timeout: int = 120) -> dict:
    cfg = get_settings().llm
    model, base_url = model or cfg.primary_model, base_url or cfg.base_url
    if not ollama_available(base_url, model):
        return {"available": False, "reason": f"Ollama model {model!r} not reachable at {base_url}"}
    import scm.graph as g
    from scm.graph import build_graph, initial_state

    calls: list[dict] = []
    real = g.ask_json

    def spy(c, system, user, required, schema=None):
        t0 = time.perf_counter()
        data, note, stats = real(c, system, user, required, schema)
        calls.append({"agent": "Analytics" if "risk analyst" in system else "Procurement" if "procurement" in system else "Vendor Comms", "user": user,
                      "data": data, "note": note, "ms": (time.perf_counter() - t0) * 1000, "stats": stats})
        return data, note, stats
    g.ask_json = spy
    workflow_ms: list[float] = []
    tmp = Path(tempfile.mkdtemp(prefix="scm_live_"))
    try:
        with db.use_path(tmp / "live.db", tmp / "out"):
            db.init_db()
            seed.ensure_demo_data()
            valid_skus = set(db.df("SELECT Item_ID FROM inventory").Item_ID)
            valid_vendors = list(db.df("SELECT Vendor_Name FROM vendors").Vendor_Name)
            for i in range(n_workflows):
                graph = build_graph()
                config = {"configurable": {"thread_id": f"live{i}", "llm_enabled": True, "llm_model": model, "llm_base_url": base_url, "llm_timeout": timeout,
                                           "llm_emails": True}}
                t0 = time.perf_counter()
                for _ in graph.stream(initial_state(), config, stream_mode="updates"):
                    pass
                workflow_ms.append((time.perf_counter() - t0) * 1000)
            llm_rows = db.df("SELECT * FROM llm_calls")
    finally:
        g.ask_json = real
    ok = [c for c in calls if c["data"] is not None]
    texts = [" ".join(str(v) for v in c["data"].values() if isinstance(v, str)) for c in ok]
    grounded = [_grounded(t, valid_skus, valid_vendors) for t in texts]
    follow = [(_sentences(str(c["data"].get("summary") or c["data"].get("intro") or "")) <= 4) for c in ok]
    # consistency: group replies by (agent, prompt); a group is consistent when all its replies are valid and name the same SKUs
    groups: dict[tuple, list[dict]] = {}
    for c in calls:
        groups.setdefault((c["agent"], c["user"]), []).append(c)
    pairs = [v for v in groups.values() if len(v) > 1]
    consistent = [all(x["data"] is not None for x in grp) and len({frozenset(SKU.findall(str(x["data"]))) for x in grp}) == 1 for grp in pairs]
    return {"available": True, "model": model, "workflows": n_workflows, "llm_calls": len(calls),
            "structured_output_validity": rate(len(ok), len(calls)),
            "groundedness": rate(sum(1 for gd, _ in grounded if gd), len(grounded)),
            "hallucination_rate": rate(sum(1 for gd, _ in grounded if not gd), len(grounded)),
            "hallucinated_examples": [p for gd, p in grounded if not gd][:5],
            "instruction_following_rate": rate(sum(follow), len(follow)),
            "consistency": rate(sum(consistent), len(consistent)) if consistent else None, "consistency_pairs": len(consistent),
            "call_latency": latency_summary([c["ms"] for c in calls]), "workflow_latency": latency_summary(workflow_ms),
            "tokens_in": int(llm_rows.Tokens_In.sum()) if len(llm_rows) else 0, "tokens_out": int(llm_rows.Tokens_Out.sum()) if len(llm_rows) else 0,
            "cloud_equivalent_cost_usd": float(llm_rows.Cost_USD.sum()) if len(llm_rows) else 0.0,
            "ttft": "not measured (non-streaming calls)"}
