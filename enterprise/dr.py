"""Backup / restore / recovery.

    backup_tenant -> consistent online copy of the tenant's databases (SQLite backup API) + a MANIFEST with sha256, row counts and the audit-chain tip
    restore_tenant -> verifies every checksum, restores, migrates, then PROVES integrity (row counts equal the manifest, audit hash chain valid)

RPO is whatever the backup schedule is (this module makes backups, it does not schedule them). RTO is measured by `restore_tenant` (see `restore_seconds`);
the measured value applies to the data volume it was measured on, not to yours.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from scm import audit, db, migrations

from . import controlplane, tenancy

COUNT_TABLES = ("inventory", "vendors", "vendor_listings", "demand_history", "purchase_orders", "supplier_deliveries", "audit_events", "workflow_runs", "approvals",
                "decision_traces", "import_jobs", "integrations", "secrets", "outbound_deliveries", "webhook_events_in")


class DRError(Exception):
    pass


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)                                        # consistent snapshot even while the database is in use
    finally:
        d.close()
        s.close()


def _counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for t in COUNT_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.DatabaseError:
            continue
    return out


def backup_tenant(tenant_id: str, dest_root: Path | str, actor: str = "system:backup") -> dict:
    t0 = time.perf_counter()
    db_path, _, ckpt = tenancy.tenant_paths(tenant_id)
    tenancy.ensure_workspace(tenant_id)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    dest = Path(dest_root) / f"{tenant_id}-{stamp}"
    files = {"engine.db": db_path}
    if ckpt.exists():
        files["checkpoints.db"] = ckpt
    for name, src in files.items():
        _copy_db(src, dest / name)
    with tenancy.scope(tenant_id, enforce_active=False):
        chain = audit.verify_chain()
        with db.connect() as c:
            counts = _counts(c)
            tip = c.execute("SELECT Hash FROM audit_events ORDER BY Event_ID DESC LIMIT 1").fetchone()
            version = migrations.current_version(c)
        audit.safe_record(audit.EventType.BACKUP_CREATED, None, actor, {"dir": dest.name})
    manifest = {"tenant_id": tenant_id, "created_at": datetime.now().isoformat(timespec="seconds"), "schema_version": version,
                "files": {n: {"sha256": _sha(dest / n), "bytes": (dest / n).stat().st_size} for n in files}, "row_counts": counts,
                "audit": {"chain_ok": chain["ok"], "events": chain["checked"], "tip_hash": tip[0] if tip else None}, "backup_seconds": round(time.perf_counter() - t0, 3)}
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"path": str(dest), **manifest}


def backup_control(dest_root: Path | str) -> Path:
    dest = Path(dest_root) / f"control-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.db"
    _copy_db(controlplane.control_db_path(), dest)
    return dest


def verify_backup(backup_dir: Path | str) -> dict:
    d = Path(backup_dir)
    try:
        m = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DRError("MANIFEST.json missing or unreadable") from exc
    bad = [n for n, meta in m["files"].items() if not (d / n).exists() or _sha(d / n) != meta["sha256"]]
    return {"ok": not bad, "corrupt_or_missing": bad, "manifest": m}


def restore_tenant(backup_dir: Path | str, tenant_id: str | None = None, overwrite: bool = False, actor: str = "system:restore") -> dict:
    """Restore into the tenant's workspace and PROVE the result. Raises DRError (and leaves the live data untouched) if verification fails."""
    t0 = time.perf_counter()
    d = Path(backup_dir)
    v = verify_backup(d)
    if not v["ok"]:
        raise DRError(f"backup is corrupt or incomplete: {v['corrupt_or_missing']}")
    m = v["manifest"]
    tid = tenant_id or m["tenant_id"]
    target_db, _, target_ckpt = tenancy.tenant_paths(tid)
    if target_db.exists() and not overwrite:
        raise DRError("the tenant already has a database: pass overwrite=True to replace it")
    stage = target_db.parent / ".restore-staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    try:
        for name in m["files"]:
            shutil.copy2(d / name, stage / name)
        with db.use_path(stage / "engine.db", None):       # verify the staged copy BEFORE it replaces anything
            with db.connect(write=True) as c:
                migrations.up(c)
            counts_ok = {}
            with db.connect() as c:
                counts = _counts(c)
            for t, n in m["row_counts"].items():
                counts_ok[t] = counts.get(t) == n
            chain = audit.verify_chain()
        mismatched = [t for t, ok in counts_ok.items() if not ok]
        if mismatched or not chain["ok"]:
            raise DRError(f"restore verification failed (row counts differ: {mismatched}; audit chain ok: {chain['ok']})")
        target_db.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(str(target_db) + suffix).unlink(missing_ok=True)
        shutil.move(str(stage / "engine.db"), target_db)
        if (stage / "checkpoints.db").exists():
            Path(target_ckpt).unlink(missing_ok=True)
            shutil.move(str(stage / "checkpoints.db"), target_ckpt)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    tenancy.forget_workspaces()
    with tenancy.scope(tid, enforce_active=False):
        audit.safe_record(audit.EventType.BACKUP_RESTORED, None, actor, {"from": d.name, "events": chain["checked"]})
    return {"tenant_id": tid, "restored_from": str(d), "restore_seconds": round(time.perf_counter() - t0, 3), "row_counts_match": True, "audit_chain_ok": True,
            "audit_events": chain["checked"]}
