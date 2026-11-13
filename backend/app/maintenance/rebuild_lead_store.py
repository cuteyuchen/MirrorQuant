"""Backup, capture, apply and roll back a complete BAPI history rebuild.

Run from backend with the same MQ configuration as the application. This module
does not route trades, initialize the database or dispatch notifications.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.core.database import get_session_factory
from app.models.domain import LeadSource, ProxyNode
from app.services.engine.serialization import execution_lock
from app.services.lead_collector.binance_bapi import BinanceBapiCollector, expand_slim_order, slim_order
from app.services.proxy.manager import ProxyManager

logger = logging.getLogger("mirrorquant.rebuild_lead_store")
FIELDS = ("orders_store_json", "snapshot_json", "closed_cache_json", "seen_order_keys_json",
          "last_sync_at", "status", "last_error")


class RebuildError(RuntimeError):
    """Operator-safe validation errors; messages contain no remote error payloads."""


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def state_of(lead) -> dict:
    state = {key: getattr(lead, key) for key in FIELDS}
    if state["last_sync_at"] is not None:
        state["last_sync_at"] = state["last_sync_at"].replace(tzinfo=timezone.utc).isoformat()
    return state


def write_artifact(directory: Path, label: str, data: dict) -> Path:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"lead-{data['lead_id']}-{label}-{stamp}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(encoded(data))
        stream.flush()
        os.fsync(stream.fileno())
    parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    logger.info("Lead %s %s artifact=%s", data["lead_id"], label, path)
    return path


async def fetch_closed_history(collector, portfolio_id: str, proxy_url) -> list:
    rows, total, seen = [], None, set()
    for page in range(1, 101):
        data = await collector.fetch_position_history(portfolio_id, page_number=page, page_size=100,
                                                      proxy_url=proxy_url)
        batch = data.get("list")
        reported = data.get("total")
        if not isinstance(batch, list) or not isinstance(reported, int) or reported < 0:
            raise RebuildError("Malformed complete position history")
        if total is not None and total != reported:
            raise RebuildError("Position history changed during capture")
        total = reported
        for row in batch:
            if not isinstance(row, dict) or row.get("positionId") is None or not row.get("symbol") or row.get("side") not in ("LONG", "SHORT"):
                raise RebuildError("Malformed closed position identity")
            opened, closed = row.get("opened"), row.get("closed")
            if not isinstance(opened, int) or not isinstance(closed, int) or not 0 < opened <= closed:
                raise RebuildError("Malformed closed position timestamps")
            key = (str(row.get("positionId")), row.get("opened"))
            if key in seen:
                raise RebuildError("Overlapping position history pages")
            seen.add(key)
            rows.append(row)
        if len(rows) == total:
            return rows
        if len(rows) > total or len(batch) < 100:
            raise RebuildError("Truncated complete position history")
    raise RebuildError("Complete position history exceeds page limit")


async def prepare(db, lead_id: int, directory: Path, collector=None) -> Path:
    """Back up before any network I/O; produce a frozen, reviewable rebuild bundle."""
    lead = await db.get(LeadSource, lead_id, populate_existing=True)
    if lead is None or lead.platform != "binance_bapi":
        raise RebuildError("Binance lead not found")
    before = state_of(lead)
    backup = write_artifact(directory, "backup", {"version": 1, "lead_id": lead_id, "before": before})
    proxies = list((await db.scalars(select(ProxyNode).order_by(ProxyNode.priority))).all())
    urls = ProxyManager.ordered_proxy_urls(proxies, preferred_proxy_id=lead.proxy_id, exchange="binance")
    collector = collector or BinanceBapiCollector()
    for proxy_url in urls:
        try:
            closed = await fetch_closed_history(collector, lead.portfolio_id, proxy_url)
            store = {}
            snapshot = await collector.sync_snapshot(lead.portfolio_id, store, [], proxy_url=proxy_url,
                                                     full_sweep=True, with_analysis=False, closed_history=closed)
            if not snapshot["meta"]["closedHistoryComplete"] or len(snapshot["closed"]) != len(closed):
                raise RebuildError("Incomplete closed position reconciliation")
            break
        except Exception as exc:
            logger.error("Lead %s rebuild capture failed errorType=%s backup=%s",
                         lead_id, type(exc).__name__, backup)
    else:
        raise RebuildError("Complete history capture failed; no database writes")
    # A captured observation can be reviewed later. Only a fresh normal sync may
    # authorize financial routing after application of the bundle.
    snapshot["openPositionsMeta"].update(authoritative=False, complete=False, asOf=None)
    snapshot["meta"]["rebuildDigest"] = hashlib.sha256(encoded({"store": store, "closed": closed}).encode()).hexdigest()
    seen = sorted(set(json.loads(before["seen_order_keys_json"] or "[]")) | set(store))
    after = {**before, "orders_store_json": encoded(store), "snapshot_json": encoded(snapshot),
             "closed_cache_json": encoded(snapshot["closed"]), "seen_order_keys_json": encoded(seen),
             "last_sync_at": datetime.fromtimestamp(snapshot["meta"]["generatedAt"] / 1000, timezone.utc).isoformat(),
             "status": "idle", "last_error": None}
    return write_artifact(directory, "bundle", {"version": 1, "lead_id": lead_id, "backup": str(backup),
                                               "before": before, "after": after})


def decoded_state(state: dict) -> dict:
    result = dict(state)
    if result.get("last_sync_at") is not None:
        result["last_sync_at"] = datetime.fromisoformat(result["last_sync_at"])
    return result


async def replace_state(db, lead, before: dict, after: dict) -> None:
    # The process lock also guards normal publishing. The conditional update
    # additionally detects writers that do not use that lock.
    conditions = [LeadSource.id == lead.id]
    for key, value in decoded_state(before).items():
        conditions.append(getattr(LeadSource, key) == value)
    result = await db.execute(update(LeadSource).where(*conditions).values(**decoded_state(after)))
    if result.rowcount != 1:
        await db.rollback()
        raise RebuildError("Lead changed concurrently; database left untouched")
    await db.commit()


async def apply_bundle(db, bundle_path: Path, directory: Path) -> Path | None:
    bundle = json.loads(bundle_path.read_text())
    lead = await db.get(LeadSource, bundle["lead_id"], populate_existing=True)
    if lead is None:
        raise RebuildError("Lead not found")
    current = state_of(lead)
    after = dict(bundle["after"])
    current_snap = json.loads(current["snapshot_json"] or "{}")
    incoming_snap = json.loads(after["snapshot_json"])
    if current["orders_store_json"] == after["orders_store_json"] and (
        (current_snap.get("meta") or {}).get("rebuildDigest") == incoming_snap["meta"]["rebuildDigest"]
    ):
        logger.info("Lead %s rebuild already applied; unchanged", lead.id)
        return None
    # Failed polls can update error metadata between prepare and apply. Accept
    # those changes only if the underlying successful snapshot/store is unchanged.
    original = bundle["before"]
    original_snap = json.loads(original["snapshot_json"] or "{}")
    for snap in (current_snap, original_snap):
        meta = snap.get("openPositionsMeta") or {}
        for key in ("authoritative", "complete", "lastFailureStartedAtMs", "consecutiveSyncFailures",
                    "firstSyncFailureAtMs", "syncFailureNotified"):
            meta.pop(key, None)
        if not meta:
            snap.pop("openPositionsMeta", None)
    if any(current[key] != original[key] for key in ("orders_store_json", "closed_cache_json", "seen_order_keys_json", "last_sync_at")) or current_snap != original_snap:
        raise RebuildError("Successful source data changed since prepare; prepare a new bundle")
    # Validate the frozen records again, including non-finite/non-positive values.
    for key, record in json.loads(after["orders_store_json"]).items():
        if slim_order(expand_slim_order(record))["h"] != key or record.get("h") != key:
            raise RebuildError("Invalid rebuilt source identity")
    incoming_snap["meta"]["storeRebuiltAtMs"] = int(time.time() * 1000)
    after["snapshot_json"] = encoded(incoming_snap)
    backup = write_artifact(directory, "rollback", {"version": 1, "lead_id": lead.id,
                                                   "before": current, "after": after})
    await replace_state(db, lead, current, after)
    logger.info("Lead %s rebuild committed orders=%s rollback=%s", lead.id,
                len(json.loads(after["orders_store_json"])), backup)
    return backup


async def rollback(db, backup_path: Path, directory: Path | None = None, *, allow_newer: bool = False) -> None:
    backup = json.loads(backup_path.read_text())
    if "after" not in backup:
        raise RebuildError("Use the rollback artifact logged by apply")
    lead = await db.get(LeadSource, backup["lead_id"], populate_existing=True)
    if lead is None:
        raise RebuildError("Lead not found")
    current = state_of(lead)
    if decoded_state(current) == decoded_state(backup["before"]):
        logger.info("Lead %s rollback already applied; unchanged", lead.id)
        return
    if decoded_state(current) != decoded_state(backup["after"]):
        if not allow_newer:
            raise RebuildError("Lead changed after rebuild; rollback refused to overwrite newer data")
        if directory is None:
            raise RebuildError("Overwriting newer data requires a backup directory")
        rescue = write_artifact(directory, "rollback-rescue", {"version": 1, "lead_id": lead.id,
                                                               "before": current, "after": backup["before"]})
        logger.info("Lead %s newer data backed up before rollback artifact=%s", lead.id, rescue)
    await replace_state(db, lead, current, backup["before"])
    logger.info("Lead %s rollback committed artifact=%s", lead.id, backup_path.resolve())


async def run(args) -> None:
    async with execution_lock():
        async with get_session_factory()() as db:
            if args.command in ("prepare", "rebuild"):
                bundle = await prepare(db, args.lead_id, args.backup_dir)
                if args.command == "rebuild":
                    await apply_bundle(db, bundle, args.backup_dir)
            elif args.command == "apply":
                await apply_bundle(db, args.bundle, args.backup_dir)
            else:
                await rollback(db, args.backup, args.backup_dir, allow_newer=args.allow_newer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "rebuild", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--backup-dir", type=Path, required=True)
        if name == "apply":
            command.add_argument("--bundle", type=Path, required=True)
        else:
            command.add_argument("--lead-id", type=int, required=True)
    undo = commands.add_parser("rollback")
    undo.add_argument("--backup", type=Path, required=True)
    undo.add_argument("--backup-dir", type=Path, required=True)
    undo.add_argument("--allow-newer", action="store_true",
                      help="Back up and overwrite source state updated after rebuild")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        if isinstance(exc, RebuildError):
            logger.error("Maintenance refused: %s", exc)
        else:
            # Never print exception text: network/database errors may include secrets.
            logger.error("Maintenance failed errorType=%s; consult artifact logs", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
