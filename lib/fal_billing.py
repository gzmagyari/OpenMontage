"""Fal actuals: what fal REALLY billed, reconciled over the tool ledger at settlement.

Estimates exist for live display and budget guarding; the bill a customer's margin is
applied to must be the provider's own numbers. Fal's billing-events API returns one
event per charged generation (request_id -> cost_total), so a job whose tool calls
recorded their request ids can swap every estimate for the real charge before the job
settles. Items whose ids are missing or unmatched keep their estimates - conservative,
and visible in the stats. The 2026-11-09 audit that motivated this found the estimate
table 2.4x under reality (a pricing-API "per unit" read as per second, and a missed
768P resolution multiplier).
"""

from __future__ import annotations

from typing import Any, Iterable

from lib.job_ledger import CostItem

FAL_API = "https://api.fal.ai/v1"
ADMIN_KEY_ENV = "OPENMONTAGE_FAL_ADMIN_KEY"


def admin_key() -> str:
    import os

    return (os.environ.get(ADMIN_KEY_ENV) or "").strip()


def fetch_events(request_ids: Iterable[str], since_iso: str, key: str,
                 timeout: float = 20.0) -> dict[str, float]:
    """request_id -> summed cost_total, for OUR request ids only (never a window sweep:
    the fal account is shared with live products, and their events are not ours)."""
    import requests

    ids = [str(r) for r in request_ids if r]
    out: dict[str, float] = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        params: list[tuple[str, str]] = [("start", since_iso), ("limit", "1000")]
        params += [("request_id", rid) for rid in chunk]
        resp = requests.get(f"{FAL_API}/models/billing-events", params=params,
                            headers={"Authorization": f"Key {key}"}, timeout=timeout)
        resp.raise_for_status()
        for ev in (resp.json().get("billing_events") or []):
            rid = str(ev.get("request_id") or "")
            if rid:
                out[rid] = round(out.get(rid, 0.0) + float(ev.get("cost_total") or 0.0), 6)
    return out


def reconcile(items: list[CostItem], events: dict[str, float]) -> tuple[list[CostItem], dict[str, Any]]:
    """Replace each item's estimate with the summed billed cost of its request ids.
    Only items whose EVERY id has a billing event flip to actual; anything else stays
    an estimate (a partially billed composite would under-state the real charge)."""
    out: list[CostItem] = []
    stats = {"matched": 0, "unmatched": 0, "no_ids": 0,
             "estimated_usd": 0.0, "actual_usd": 0.0, "drift_usd": 0.0}
    for item in items:
        rids = [r for r in item.request_ids if r]
        if not rids:
            stats["no_ids"] += 1
            out.append(item)
            continue
        if all(rid in events for rid in rids):
            actual = round(sum(events[rid] for rid in rids), 4)
            stats["matched"] += 1
            stats["estimated_usd"] = round(stats["estimated_usd"] + item.usd, 4)
            stats["actual_usd"] = round(stats["actual_usd"] + actual, 4)
            stats["drift_usd"] = round(stats["drift_usd"] + actual - item.usd, 4)
            out.append(CostItem(item.item, actual, item.provider, item.request_ids, estimated=False))
        else:
            stats["unmatched"] += 1
            out.append(item)
    return out, stats
