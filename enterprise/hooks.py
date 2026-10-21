"""One call that wires every enterprise extension into the workflow service (idempotent)."""
from __future__ import annotations

from . import approvals, trace


def install_all() -> None:
    approvals.install_hooks()
    trace.install_hooks()
