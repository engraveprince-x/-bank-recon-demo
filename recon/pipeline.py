"""Glue: load -> reconcile -> classify unmatched bank lines."""
from __future__ import annotations

from typing import Optional

from .classifier import LLMConfig, classify
from .io import load_transactions
from .matcher import MatchConfig, reconcile
from .models import ReconResult


def run(bank_src, ledger_src, config: Optional[MatchConfig] = None,
        llm: Optional[LLMConfig] = None, post=None) -> ReconResult:
    bank = load_transactions(bank_src, "bank")
    ledger = load_transactions(ledger_src, "ledger")
    result = reconcile(bank, ledger, config)
    unmatched = [result.bank[i] for i in result.unmatched_bank]
    result.classifications = classify(unmatched, llm, post=post)
    return result
