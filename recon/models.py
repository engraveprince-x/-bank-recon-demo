"""Core data structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Transaction:
    """A single bank statement line or ledger entry.

    ``amount`` is signed from the company's point of view:
    positive = money in (deposit / receipt), negative = money out (payment).
    """

    id: str
    source: str  # "bank" | "ledger"
    date: date
    description: str
    amount: Decimal
    reference: str = ""
    account: str = ""  # ledger only (GL account already posted)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": f"{self.amount:.2f}",
            "reference": self.reference,
            "account": self.account,
        }


# Match types
EXACT = "exact"
FUZZY = "fuzzy"
MANY_TO_ONE = "many_to_one"  # several bank lines -> one ledger entry
ONE_TO_MANY = "one_to_many"  # one bank line -> several ledger entries
AMOUNT_DIFF = "amount_difference"


@dataclass
class Match:
    id: str
    type: str
    bank_ids: list[str]
    ledger_ids: list[str]
    bank_total: Decimal
    ledger_total: Decimal
    confidence: float
    reason: str
    needs_review: bool = False

    @property
    def difference(self) -> Decimal:
        return self.bank_total - self.ledger_total

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "bank_ids": self.bank_ids,
            "ledger_ids": self.ledger_ids,
            "bank_total": f"{self.bank_total:.2f}",
            "ledger_total": f"{self.ledger_total:.2f}",
            "difference": f"{self.difference:.2f}",
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "needs_review": self.needs_review,
        }


@dataclass
class Duplicate:
    id: str
    source: str
    txn_id: str
    original_id: str
    amount: Decimal
    reason: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "txn_id": self.txn_id,
            "original_id": self.original_id,
            "amount": f"{self.amount:.2f}",
            "reason": self.reason,
        }


@dataclass
class Classification:
    txn_id: str
    account: str
    confidence: float
    source: str  # "rule" | "llm"
    rationale: str = ""
    note: Optional[str] = None  # e.g. why LLM fell back to rules

    def to_dict(self) -> dict:
        return {
            "txn_id": self.txn_id,
            "account": self.account,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "rationale": self.rationale,
            "note": self.note,
        }


@dataclass
class ReconResult:
    bank: dict[str, Transaction]
    ledger: dict[str, Transaction]
    matches: list[Match] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    unmatched_bank: list[str] = field(default_factory=list)
    unmatched_ledger: list[str] = field(default_factory=list)
    classifications: dict[str, Classification] = field(default_factory=dict)

    def counts(self) -> dict:
        by_type: dict[str, int] = {}
        for m in self.matches:
            by_type[m.type] = by_type.get(m.type, 0) + 1
        return {
            "bank_rows": len(self.bank),
            "ledger_rows": len(self.ledger),
            "matches": len(self.matches),
            "matches_by_type": by_type,
            "duplicates": len(self.duplicates),
            "unmatched_bank": len(self.unmatched_bank),
            "unmatched_ledger": len(self.unmatched_ledger),
        }
