"""Matching engine.

Pipeline (each stage only sees rows still unmatched by earlier stages):

1. Duplicate detection  - same date, amount, normalised description and reference
                          inside one source; the later copy is flagged and held back.
2. Exact match          - equal amount, same reference, dates within ``date_window`` days.
3. Split match          - many bank lines summing exactly to one ledger entry
                          (e.g. an invoice paid in instalments) and the reverse
                          (one bank deposit settling several invoices).
4. Fuzzy match          - equal amount, dates within ``fuzzy_date_window`` days and
                          description similarity above ``fuzzy_threshold``.
5. Amount difference    - same reference (or near-identical description) and dates in
                          window, but amounts differ within ``amount_tolerance_pct``.

Candidate pairs in stages 2, 4 and 5 are scored and assigned greedily from the
highest score, so one row can never be used twice.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from rapidfuzz import fuzz

from .models import (
    AMOUNT_DIFF,
    EXACT,
    FUZZY,
    MANY_TO_ONE,
    ONE_TO_MANY,
    Duplicate,
    Match,
    ReconResult,
    Transaction,
)

_NOISE = re.compile(r"\b(pos|ach|debit|credit|card|purchase|payment|pmt|trf|transfer|online|ref|no)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_DIGITS = re.compile(r"\b\d{4,}\b")


def normalise(text: str) -> str:
    t = _NON_ALNUM.sub(" ", text.lower())
    t = _DIGITS.sub(" ", t)
    t = _NOISE.sub(" ", t)
    return " ".join(t.split())


def similarity(a: str, b: str) -> float:
    """Description similarity in [0, 100]."""
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    score = float(fuzz.token_set_ratio(na, nb))
    # Bank feeds usually start with the merchant name ("SPARROWLINE AIR TICKET" vs
    # "Sparrowline Airlines - travel"): a shared distinctive first word is a strong hint.
    fa, fb = na.split()[0], nb.split()[0]
    if fa == fb and len(fa) >= 5:
        score = max(score, 80.0)
    return score


def _ref(t: Transaction) -> str:
    return re.sub(r"[^A-Z0-9]", "", t.reference.upper())


def _refs_match(a: Transaction, b: Transaction) -> bool:
    ra, rb = _ref(a), _ref(b)
    if ra and rb and ra == rb:
        return True
    # bank descriptions often embed the reference ("WIRE INV-2041 ACME")
    if rb and rb in re.sub(r"[^A-Z0-9]", "", a.description.upper()):
        return True
    if ra and ra in re.sub(r"[^A-Z0-9]", "", b.description.upper()):
        return True
    return False


@dataclass
class MatchConfig:
    date_window: int = 3
    fuzzy_date_window: int = 5
    split_date_window: int = 10
    fuzzy_threshold: float = 70.0
    review_threshold: float = 0.85
    amount_tolerance_pct: float = 10.0
    max_split_parts: int = 4
    max_split_candidates: int = 14
    split_min_similarity: float = 45.0


class Reconciler:
    def __init__(self, config: Optional[MatchConfig] = None):
        self.cfg = config or MatchConfig()

    # ------------------------------------------------------------------ public
    def reconcile(self, bank: Iterable[Transaction], ledger: Iterable[Transaction]) -> ReconResult:
        bank_map = {t.id: t for t in bank}
        ledger_map = {t.id: t for t in ledger}
        result = ReconResult(bank=bank_map, ledger=ledger_map)
        self._match_seq = 0

        dup_ids = self._find_duplicates(bank_map, result) | self._find_duplicates(ledger_map, result)
        open_bank = {i for i in bank_map if i not in dup_ids}
        open_ledger = {i for i in ledger_map if i not in dup_ids}

        self._exact(result, open_bank, open_ledger)
        self._splits(result, open_bank, open_ledger)
        self._fuzzy(result, open_bank, open_ledger)
        self._amount_diff(result, open_bank, open_ledger)

        order_b = list(bank_map)
        order_l = list(ledger_map)
        result.unmatched_bank = sorted(open_bank, key=order_b.index)
        result.unmatched_ledger = sorted(open_ledger, key=order_l.index)
        result.matches.sort(key=lambda m: (min(bank_map[b].date for b in m.bank_ids), m.id))
        return result

    # ---------------------------------------------------------------- helpers
    def _new_match(self, result, mtype, bank_ids, ledger_ids, confidence, reason) -> Match:
        self._match_seq += 1
        b_total = sum((result.bank[i].amount for i in bank_ids), Decimal("0"))
        l_total = sum((result.ledger[i].amount for i in ledger_ids), Decimal("0"))
        needs_review = confidence < self.cfg.review_threshold or mtype == AMOUNT_DIFF
        m = Match(
            id=f"M{self._match_seq:04d}",
            type=mtype,
            bank_ids=list(bank_ids),
            ledger_ids=list(ledger_ids),
            bank_total=b_total,
            ledger_total=l_total,
            confidence=confidence,
            reason=reason,
            needs_review=needs_review,
        )
        result.matches.append(m)
        return m

    def _find_duplicates(self, txns: dict[str, Transaction], result: ReconResult) -> set[str]:
        seen: dict[tuple, str] = {}
        dups: set[str] = set()
        for t in txns.values():
            key = (t.date, t.amount, normalise(t.description), _ref(t))
            if key in seen:
                dups.add(t.id)
                result.duplicates.append(
                    Duplicate(
                        id=f"D{len(result.duplicates) + 1:04d}",
                        source=t.source,
                        txn_id=t.id,
                        original_id=seen[key],
                        amount=t.amount,
                        reason="Same date, amount, description and reference as " + seen[key],
                    )
                )
            else:
                seen[key] = t.id
        return dups

    @staticmethod
    def _greedy(candidates: list[tuple[float, str, str, str]], open_bank: set, open_ledger: set):
        """candidates: (score, bank_id, ledger_id, reason) -> yields accepted ones."""
        for score, b, l, reason in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
            if b in open_bank and l in open_ledger:
                open_bank.discard(b)
                open_ledger.discard(l)
                yield score, b, l, reason

    # ---------------------------------------------------------------- stages
    def _exact(self, result, open_bank, open_ledger):
        cands = []
        for b in open_bank:
            tb = result.bank[b]
            for l in open_ledger:
                tl = result.ledger[l]
                if tb.amount != tl.amount:
                    continue
                gap = abs((tb.date - tl.date).days)
                if gap > self.cfg.date_window or not _refs_match(tb, tl):
                    continue
                score = 1.0 - gap * 0.01
                cands.append((score, b, l, f"amount + reference, {gap}d apart"))
        for score, b, l, reason in self._greedy(cands, open_bank, open_ledger):
            self._new_match(result, EXACT, [b], [l], 1.0, reason)

    def _splits(self, result, open_bank, open_ledger):
        # many bank lines -> one ledger entry
        for l in sorted(open_ledger):
            tl = result.ledger[l]
            combo = self._find_combo(tl, [result.bank[b] for b in open_bank])
            if combo:
                ids, conf, reason = combo
                open_ledger.discard(l)
                open_bank.difference_update(ids)
                self._new_match(result, MANY_TO_ONE, ids, [l], conf, reason)
        # one bank line -> many ledger entries
        for b in sorted(open_bank):
            tb = result.bank[b]
            combo = self._find_combo(tb, [result.ledger[l] for l in open_ledger])
            if combo:
                ids, conf, reason = combo
                open_bank.discard(b)
                open_ledger.difference_update(ids)
                self._new_match(result, ONE_TO_MANY, [b], ids, conf, reason)

    def _find_combo(self, target: Transaction, pool: list[Transaction]):
        sign = 1 if target.amount > 0 else -1
        cands = []
        for t in pool:
            if (t.amount > 0) != (sign > 0) or abs(t.amount) >= abs(target.amount):
                continue
            gap = abs((t.date - target.date).days)
            if gap > self.cfg.split_date_window:
                continue
            sim = 100.0 if _refs_match(t, target) else similarity(t.description, target.description)
            if sim < self.cfg.split_min_similarity:
                continue
            cands.append((sim, gap, t))
        cands.sort(key=lambda c: (-c[0], c[1], c[2].id))
        cands = cands[: self.cfg.max_split_candidates]
        best = None
        for size in range(2, self.cfg.max_split_parts + 1):
            for combo in itertools.combinations(cands, size):
                if sum(c[2].amount for c in combo) != target.amount:
                    continue
                avg_sim = sum(c[0] for c in combo) / size
                if best is None or avg_sim > best[0]:
                    best = (avg_sim, combo)
            if best:
                break  # prefer the smallest number of parts
        if not best:
            return None
        avg_sim, combo = best
        ids = sorted(c[2].id for c in combo)
        all_ref = all(c[0] == 100.0 for c in combo)
        conf = 0.9 if all_ref else round(0.5 + 0.4 * avg_sim / 100, 3)
        reason = f"{len(ids)} parts sum to {target.amount:.2f}" + (" (reference match)" if all_ref else f" (avg similarity {avg_sim:.0f})")
        return ids, conf, reason

    def _fuzzy(self, result, open_bank, open_ledger):
        cands = []
        for b in open_bank:
            tb = result.bank[b]
            for l in open_ledger:
                tl = result.ledger[l]
                if tb.amount != tl.amount:
                    continue
                gap = abs((tb.date - tl.date).days)
                if gap > self.cfg.fuzzy_date_window:
                    continue
                sim = similarity(tb.description, tl.description)
                if sim < self.cfg.fuzzy_threshold:
                    continue
                score = (sim / 100) * (1 - 0.03 * gap)
                cands.append((score, b, l, f"amount equal, description similarity {sim:.0f}, {gap}d apart"))
        for score, b, l, reason in self._greedy(cands, open_bank, open_ledger):
            self._new_match(result, FUZZY, [b], [l], round(score, 3), reason)

    def _amount_diff(self, result, open_bank, open_ledger):
        cands = []
        tol = Decimal(str(self.cfg.amount_tolerance_pct)) / 100
        for b in open_bank:
            tb = result.bank[b]
            for l in open_ledger:
                tl = result.ledger[l]
                if (tb.amount > 0) != (tl.amount > 0) or tb.amount == tl.amount:
                    continue
                gap = abs((tb.date - tl.date).days)
                if gap > self.cfg.fuzzy_date_window:
                    continue
                diff = abs(tb.amount - tl.amount)
                if diff > abs(tl.amount) * tol:
                    continue
                if _refs_match(tb, tl):
                    score, why = 0.8, "same reference"
                else:
                    sim = similarity(tb.description, tl.description)
                    if sim < 90:
                        continue
                    score, why = 0.6, f"description similarity {sim:.0f}"
                cands.append((score - float(diff / abs(tl.amount)), b, l, f"{why}, amounts differ by {tb.amount - tl.amount:.2f}"))
        for score, b, l, reason in self._greedy(cands, open_bank, open_ledger):
            self._new_match(result, AMOUNT_DIFF, [b], [l], round(max(score, 0.1), 3), reason)


def reconcile(bank, ledger, config: Optional[MatchConfig] = None) -> ReconResult:
    return Reconciler(config).reconcile(bank, ledger)
