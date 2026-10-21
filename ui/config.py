"""Tunable settings and shared constants (edit here)."""
# ============================ EDITABLE CONFIG ============================
OLLAMA_MODEL = "llama3.2"                    # or "llama3.2:1b" for faster CPU runs
OLLAMA_BASE_URL = "http://localhost:11434"
LLM_TIMEOUT_SECONDS = 60                     # per LLM call; on timeout the agent falls back to rules
# Comparison pricing used for the "Estimated Cloud Savings" metric (USD per 1M tokens, GPT-4o list price —
# adjust if pricing changes). Local inference is treated as $0 marginal cost (hardware already owned).
GPT4O_INPUT_PER_M = 2.50
GPT4O_OUTPUT_PER_M = 10.00
# Marketing content (edit freely). Images are hot-linked from the Unsplash CDN; swap any URL for your own assets.
IMG_ORCHESTRATION = "https://images.unsplash.com/photo-1485827404703-89b55fcc595e?auto=format&fit=crop&w=1200&q=80"   # automated robotics
IMG_INTEGRATION = "https://images.unsplash.com/photo-1451187580459-43490279c0fa?auto=format&fit=crop&w=1200&q=80"     # enterprise network
IMG_METRICS = "https://images.unsplash.com/photo-1551288049-bebda4e38f71?auto=format&fit=crop&w=1200&q=80"            # cloud metrics
PRICE_INTEGRATION = ("Scoped per engagement", "Fixed price after a discovery call")
PRICE_CONSULT = ("From $20,000 / month", "Monthly retainer")

STEPS = {  # node key -> (label, tooltip)
    "supervisor": ("Supervisor", "Routes state between agents and decides when to stop or pause"),
    "demand": ("Demand", "Forecasts demand per SKU (best model chosen by backtest)"),
    "analytics": ("Risk", "Stock-out probability, days of cover and reorder quantity from the deterministic risk engine"),
    "supplier_intel": ("Suppliers", "Scores supplier reliability from delivery history"),
    "procurement": ("Optimiser", "Splits each order across suppliers under MOQ, capacity, quality and lead-time constraints"),
    "policy_check": ("Policy", "Deterministic validator: blocks impossible or unsafe lines before a human sees them"),
    "vendor_comm": ("Vendor Comms", "Drafts purchase-order emails and ERP payloads"),
    "vendor_dispatch": ("Human Gate · Dispatch", "Graph is paused here until a human approves or denies"),
}
AGENT_KEY = {"Supervisor": "supervisor", "Demand": "demand", "Analytics": "analytics", "Supplier Intel": "supplier_intel",
             "Procurement": "procurement", "Policy": "policy_check", "Vendor Comms": "vendor_comm"}
DOWNSTREAM = ("supplier_intel", "procurement", "policy_check", "vendor_comm", "vendor_dispatch")   # skipped when nothing needs ordering
PROGRESS = (("demand", 0.0, .1), ("analytics", .1, .25), ("supplier_intel", .25, .35), ("procurement", .35, .5),
            ("policy_check", .5, .6), ("vendor_comm", .6, .75), ("vendor_dispatch", .75, 1.0))
LEVELS = {"info": "INFO", "success": "OK", "warn": "WARN", "error": "ERROR"}
