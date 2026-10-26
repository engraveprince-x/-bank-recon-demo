"""Human review queue, decisions and an append-only audit log."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ReconResult

CLASSIFICATION_REVIEW_BELOW = 0.6
VALID_DECISIONS = {"confirm", "reject"}


def review_items(result: ReconResult) -> list[dict]:
    """Everything a human should look at: low-confidence/odd matches, duplicates,
    and low-confidence account suggestions."""
    items: list[dict] = []
    for m in result.matches:
        if m.needs_review:
            items.append({
                "item_id": m.id,
                "kind": "match",
                "subtype": m.type,
                "confidence": m.confidence,
                "summary": f"{m.type}: bank {', '.join(m.bank_ids)} <-> ledger {', '.join(m.ledger_ids)}",
                "detail": m.reason,
                "amount": f"{m.bank_total:.2f}",
                "difference": f"{m.difference:.2f}",
            })
    for d in result.duplicates:
        items.append({
            "item_id": d.id,
            "kind": "duplicate",
            "subtype": f"{d.source}_duplicate",
            "confidence": 0.5,
            "summary": f"Possible duplicate {d.source} line {d.txn_id} (copy of {d.original_id})",
            "detail": d.reason,
            "amount": f"{d.amount:.2f}",
            "difference": "",
        })
    for txn_id, c in result.classifications.items():
        if c.confidence < CLASSIFICATION_REVIEW_BELOW:
            t = result.bank[txn_id]
            items.append({
                "item_id": f"C-{txn_id}",
                "kind": "classification",
                "subtype": c.source,
                "confidence": c.confidence,
                "summary": f"{txn_id} '{t.description}' -> {c.account}",
                "detail": c.rationale,
                "amount": f"{t.amount:.2f}",
                "difference": "",
            })
    return items


class AuditLog:
    """Append-only JSON-lines audit trail (who / when / what)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._mem: list[dict] = []
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._mem.append(json.loads(line))

    def record(self, user: str, action: str, target: str, details: Optional[dict] = None) -> dict:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "user": user,
            "action": action,
            "target": target,
            "details": details or {},
        }
        with self._lock:
            self._mem.append(entry)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self) -> list[dict]:
        return list(self._mem)


class ReviewSession:
    def __init__(self, result: ReconResult, audit: AuditLog):
        self.result = result
        self.audit = audit
        self.decisions: dict[str, dict] = {}

    def items(self) -> list[dict]:
        out = []
        for item in review_items(self.result):
            d = self.decisions.get(item["item_id"])
            item["status"] = d["decision"] if d else "pending"
            item["decided_by"] = d["user"] if d else ""
            out.append(item)
        return out

    def decide(self, item_id: str, decision: str, user: str, note: str = "") -> dict:
        if decision not in VALID_DECISIONS:
            raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}")
        user = (user or "").strip()
        if not user:
            raise ValueError("reviewer name is required")
        valid_ids = {i["item_id"] for i in review_items(self.result)}
        if item_id not in valid_ids:
            raise KeyError(item_id)
        previous = self.decisions.get(item_id, {}).get("decision", "pending")
        self.decisions[item_id] = {"decision": decision, "user": user, "note": note}
        self.audit.record(user, f"review.{decision}", item_id, {"previous": previous, "note": note})
        return self.decisions[item_id]
