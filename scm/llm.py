"""Ollama / LangChain access with hard timeouts, tolerant JSON parsing and QA fault injection."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from types import SimpleNamespace

from langchain_core.messages import HumanMessage, SystemMessage

from . import obs
from .llm_router import METRICS, ModelRouter, call_with_policy
from .schemas import validate_output
from .settings import get_settings

_POOL = ThreadPoolExecutor(max_workers=6)

_GOOD = '{"summary": "Stock risk is elevated.", "insights": {}, "notes": {}, "intro": "Please process the order below.", "closing": "Thank you."}'
# key -> (label, payload | ("raise", exception))   "none" = no fault
MUTATIONS: dict[str, tuple[str, object]] = {
    "none": ("No fault (real model)", None),
    "truncated_json": ("Broken JSON — truncated mid-string",
                       '{"summary": "Stock risk is elevated across 3 SKUs", "insights": {"SKU-1005": "Critical short'),
    "empty": ("Empty string", ""),
    "whitespace": ("Whitespace only", "   \n\t  "),
    "missing_fields": ("Valid JSON — required fields missing", '{"unexpected": true, "data": [1, 2, 3]}'),
    "wrong_types": ("Valid JSON — wrong field types",
                    '{"summary": 42, "insights": ["not", "a", "dict"], "notes": "string not dict", "intro": ["list"], "closing": null}'),
    "json_array": ("JSON array instead of object", '["summary", "insights"]'),
    "prose": ("Prose, no JSON at all", "Sure! I'd be happy to help. Overall the risk looks high, but I can't format this as JSON."),
    "fenced_ok": ("Recoverable — JSON inside ```fence``` + chatter", f"Here is the analysis:\n```json\n{_GOOD}\n```\nHope this helps!"),
    "html_injection": ("Valid JSON containing HTML / markdown / $ symbols",
                       json.dumps({"summary": '<script>alert(1)</script> **bold** $100$ $$x$$ <img src=x onerror=alert(2)>',
                                   "insights": {}, "notes": {}, "intro": "<b>Dear</b> vendor $5 {{x}}", "closing": "Bye"})),
    "oversized": ("Valid JSON with a 6,000-character summary",
                  json.dumps({"summary": "LONGTEXT" * 750, "insights": {}, "notes": {}, "intro": "Please process. " * 40, "closing": "Thanks"})),
    "connection_error": ("Raises ConnectionError (Ollama down)", ("raise", ConnectionError("Simulated: connection refused"))),
}
RECOVERABLE = {"fenced_ok", "html_injection", "oversized", "truncated_json"}  # parser / regex repair should accept these
FORMAT_BAD = {"empty", "whitespace", "missing_fields", "wrong_types", "json_array", "prose"}  # unrecoverable -> safe mock + warning
FORMAT_WARNING = "System Warning: LLM Format Exception Handled"
CUSTOM = "custom"          # QA sandbox: the tester pastes any raw model reply (kept out of MUTATIONS so the automated matrix is unchanged)
CUSTOM_LABEL = "Custom payload (paste your own raw reply)"
_LOCAL = threading.local()


def last_raw() -> str:
    """Raw text of the most recent model reply on this thread (used by agents for field-level salvage)."""
    return getattr(_LOCAL, "raw", "") or ""


ORDER_FIELDS = ("selected_vendor", "recommended_order_quantity", "unit_price_usd")


def salvage_order(text) -> dict | None:
    """Regex Extraction Engine for order-style replies. Pulls selected_vendor / recommended_order_quantity / unit_price_usd out of
    corrupt or truncated text. Returns {"found": {...}, "missing": [...]} or None when none of the fields appear at all."""
    if not isinstance(text, str) or not text.strip():
        return None
    found: dict = {}
    m = re.search(r'"selected_vendor"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m and m.group(1).strip():
        found["selected_vendor"] = m.group(1).strip()
    for key, cast in (("recommended_order_quantity", int), ("unit_price_usd", float)):
        m = re.search(r'"%s"\s*:\s*"?(-?\d+(?:\.\d+)?)' % key, text)
        if m:
            try:
                v = cast(float(m.group(1)))
            except (ValueError, OverflowError):
                continue
            if v > 0:
                found[key] = v
    if not found:
        return None
    return {"found": found, "missing": [k for k in ORDER_FIELDS if k not in found]}


@lru_cache(maxsize=8)
def _chat(model: str, base_url: str, timeout: int):
    try:  # preferred, maintained integration
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model, base_url=base_url, temperature=0.2, format="json",
                          num_predict=160, keep_alive="30m", client_kwargs={"timeout": timeout})
    except ImportError:  # works with older LangChain stacks
        from langchain_community.chat_models import ChatOllama  # type: ignore[no-redef]
        return ChatOllama(model=model, base_url=base_url, temperature=0.2, format="json",
                          num_predict=160, timeout=timeout)


def parse_json(text: object) -> dict | None:
    """Parse model output into a dict, tolerating prose / code fences around the JSON."""
    if not isinstance(text, str):
        return None
    for candidate in (text.strip(), (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def salvage(text, required: dict) -> dict | None:
    """Last-chance recovery from corrupted model output: close truncated JSON, then pull string fields out with regexes.
    Returns a dict containing every required field, or None when nothing trustworthy can be recovered."""
    if not isinstance(text, str) or not text.strip() or any(t is not str for t in required.values()):
        return None
    body = re.sub(r"```(?:json)?", "", text).strip()
    start = body.find("{")
    if start >= 0:
        cand = body[start:]
        for tail in ("", '"}', "}", '"}}', '"]}', '"}]}', '"}}}'):
            try:
                obj = json.loads(cand + tail)
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(obj, dict) and all(isinstance(obj.get(k), str) and obj[k].strip() for k in required):
                return obj
    out = {}
    for k in required:
        m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % re.escape(k), body)   # tolerates an unterminated string
        if not m or not m.group(1).strip():
            return None
        out[k] = m.group(1).replace('\\"', '"').replace("\\n", " ").strip()
    return out


def usage_stats(reply, prompt: str, wall_ms: float) -> dict:
    """Token + latency accounting. Uses Ollama's own counters when present, else estimates (~4 chars/token)."""
    meta = getattr(reply, "response_metadata", None) or {}
    um = getattr(reply, "usage_metadata", None) or {}
    text = getattr(reply, "content", "") or ""
    eval_ns = meta.get("eval_duration")
    return {"in": int(um.get("input_tokens") or meta.get("prompt_eval_count") or max(1, len(prompt) // 4)),
            "out": int(um.get("output_tokens") or meta.get("eval_count") or max(1, len(text) // 4)),
            "wall_ms": round(wall_ms), "eval_ms": round(eval_ns / 1e6) if eval_ns else round(wall_ms),
            "measured": bool(eval_ns)}


def _invoke(model: str, cfg: dict, messages, fault: str, delay: float, custom: str, timeout: float):
    """One attempt: optional simulated latency, then either a fabricated (mutated) reply or the real model."""
    if delay:
        time.sleep(delay)
    if fault != "none":
        payload = custom if fault == CUSTOM else MUTATIONS[fault][1]
        if isinstance(payload, tuple):
            raise payload[1]
        return SimpleNamespace(content=payload, response_metadata={}, usage_metadata={})
    return _chat(model, cfg["llm_base_url"], max(1, int(timeout))).invoke(messages)


def last_ignored() -> list[str]:
    """Decision-style fields the model emitted on this thread's last call (they were discarded, never read)."""
    return list(getattr(_LOCAL, "ignored", []) or [])


def last_call() -> dict:
    """Resilience facts about this thread's last call: model used, retries, fallback, error (for the audit trail)."""
    return dict(getattr(_LOCAL, "call", {}) or {})


def ask_json(cfg: dict, system: str, user: str, required: dict | list, schema=None) -> tuple[dict | None, str, dict | None]:
    """Returns (data, note, stats). data is None on any failure; note says why (shown in the trace).

    `required` maps field -> expected type (a plain list of names means "any type"). `schema` (a pydantic model from scm.schemas) adds strict
    validation + sanitisation of the advisory text; decision-style fields are discarded. Resilience: model routing, bounded retries with
    exponential backoff, per-model circuit breakers, one shared deadline (see llm_router).
    Fault hooks (QA panel): cfg['fault_llm'] swaps the model reply for a corrupt string, cfg['fault_latency'] adds delay inside the call.
    """
    fault, delay = cfg.get("fault_llm", "none"), float(cfg.get("fault_latency") or 0)
    _LOCAL.raw, _LOCAL.ignored, _LOCAL.call = "", [], {}
    if not cfg.get("llm_enabled", True) and fault == "none":
        return None, "LLM disabled (rule-based mode)", None
    required = {k: object for k in required} if isinstance(required, list) else required
    lcfg = get_settings().llm
    timeout = int(cfg.get("llm_timeout", lcfg.timeout_seconds))
    msgs = [SystemMessage(content=system), HumanMessage(content=user)]
    custom = str(cfg.get("fault_payload") or "")
    router = ModelRouter(lcfg)
    models = [f"fault:{fault}"] if fault != "none" else router.route(cfg.get("llm_task", "simple"), cfg.get("llm_model"))
    retries = int(cfg.get("llm_retries", lcfg.retries))
    out = call_with_policy(lambda m, remaining: _invoke(m, cfg, msgs, fault, delay, custom, remaining), models, timeout, retries,
                           float(cfg.get("llm_backoff_base", lcfg.backoff_base_seconds)), lcfg.backoff_max_seconds, lcfg)
    agent = obs.context().get("agent") or "llm"
    _LOCAL.call = {"model": out.model, "retries": out.retries, "fallback": out.used_fallback or out.reply is None, "error": out.error}
    if out.reply is None:
        obs.record_llm_call(agent, models[0], "timeout" if out.timed_out else "error", out.latency_ms, 0, 0, out.retries, True)
        if out.timed_out:
            return None, f"LLM timed out after {timeout}s", None
        return None, f"LLM unavailable ({out.error})", None
    reply = out.reply
    stats = usage_stats(reply, system + user, out.latency_ms)
    METRICS.observe(out.model or models[0], out.latency_ms, True, stats["in"], stats["out"])
    text = getattr(reply, "content", reply)
    _LOCAL.raw = text if isinstance(text, str) else ""
    data, problem = parse_json(text), None
    if data is None:
        problem = "malformed JSON"
    else:
        missing = [k for k in required if k not in data]
        wrong = [k for k, t in required.items() if k in data and not isinstance(data[k], t)]
        if missing:
            problem = f"missing keys: {', '.join(missing)}"
        elif wrong:
            problem = f"wrong type for: {', '.join(wrong)}"
    if schema is not None and problem is None and data is not None:
        model, issues, ignored = validate_output(schema, data)
        _LOCAL.ignored = ignored
        if model is None:
            problem = "schema validation failed: " + "; ".join(issues)
        else:
            data = {**data, **model.model_dump()}
    if problem:
        rec = salvage(text, required)                      # 2nd chance: repair / regex
        if rec is not None and schema is not None:
            model, _issues, ignored = validate_output(schema, rec)
            rec = {**rec, **model.model_dump()} if model is not None else None
            _LOCAL.ignored = ignored
        if rec is not None:
            obs.record_llm_call(agent, out.model or models[0], "repaired", out.latency_ms, stats["in"], stats["out"], out.retries, out.used_fallback)
            return rec, "llm (regex-recovered)", stats
        obs.record_llm_call(agent, out.model or models[0], "invalid_output", out.latency_ms, stats["in"], stats["out"], out.retries, True)
        return None, f"{FORMAT_WARNING} ({problem})", stats   # 3rd: caller falls back to its safe mock layout
    obs.record_llm_call(agent, out.model or models[0], "ok", out.latency_ms, stats["in"], stats["out"], out.retries, out.used_fallback)
    return data, "llm", stats


_WARMED: set = set()


def warm(base_url: str, model: str, keep: str = "30m") -> None:
    """Load the model into Ollama's memory in a background thread, so the first pipeline run does not pay the ~15-20s cold start."""
    key = (base_url, model)
    if key in _WARMED:
        return
    _WARMED.add(key)

    def _go():
        try:
            req = urllib.request.Request(base_url.rstrip("/") + "/api/generate", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"model": model, "prompt": "", "keep_alive": keep, "stream": False}).encode())
            urllib.request.urlopen(req, timeout=180).read()
        except Exception:
            _WARMED.discard(key)  # retry on a later run if Ollama was not ready

    threading.Thread(target=_go, daemon=True).start()


def health(base_url: str, model: str) -> tuple[str, str]:
    """('online'|'no_model'|'offline', message) via Ollama's /api/tags."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=2) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return "offline", "Ollama not reachable"
    if any(n == model or n.split(":")[0] == model.split(":")[0] and ":" not in model for n in names):
        return "online", f"{model} ready"
    return "no_model", f"'{model}' not pulled (ollama pull {model})"
