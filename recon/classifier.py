"""Account (GL category) suggestions for unmatched bank lines.

If ``LLM_API_KEY`` is set, an OpenAI-compatible ``/chat/completions`` endpoint is
called once per batch. Any failure (network, bad JSON, unknown account) falls
back to the keyword rules for the affected rows, and the source is recorded so
the UI can show whether a suggestion came from the LLM or from the rules.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from .models import Classification, Transaction

CHART_OF_ACCOUNTS = [
    "Sales Revenue",
    "Interest Income",
    "Bank Fees",
    "Payroll Expense",
    "Rent Expense",
    "Utilities",
    "Software & Subscriptions",
    "Office Supplies",
    "Travel & Meals",
    "Professional Fees",
    "Marketing & Advertising",
    "Insurance",
    "Taxes & Licenses",
    "Owner Contributions",
    "Suspense / Uncategorized",
]

# (account, keywords, confidence)
RULES: list[tuple[str, list[str], float]] = [
    ("Bank Fees", ["service charge", "monthly fee", "wire fee", "overdraft", "bank fee", "maintenance fee", "fx fee"], 0.9),
    ("Interest Income", ["interest"], 0.9),
    ("Payroll Expense", ["payroll", "salary", "wages"], 0.85),
    ("Rent Expense", ["rent", "lease"], 0.85),
    ("Utilities", ["electric", "power", "water", "gas co", "utility", "internet", "telecom", "mobile plan"], 0.8),
    ("Software & Subscriptions", ["software", "saas", "subscription", "cloud", "hosting", "license"], 0.8),
    ("Office Supplies", ["office", "stationery", "paper", "printer", "supply"], 0.7),
    ("Travel & Meals", ["airline", "air ", "hotel", "taxi", "ride", "coffee", "restaurant", "cafe", "diner", "meal"], 0.7),
    ("Professional Fees", ["legal", "law", "accounting", "consult", "advisory", "audit"], 0.75),
    ("Marketing & Advertising", ["ads", "advert", "marketing", "promo", "sponsor"], 0.75),
    ("Insurance", ["insurance", "insur", "premium"], 0.8),
    ("Taxes & Licenses", ["tax", "irs", "permit", "licence", "registration"], 0.8),
    ("Sales Revenue", ["customer payment", "invoice", "deposit from", "remittance", "stripe payout", "sales"], 0.7),
    ("Sales Revenue", ["deposit"], 0.5),
]


def classify_rule(txn: Transaction) -> Classification:
    text = f" {txn.description.lower()} "
    for account, keywords, conf in RULES:
        hit = next((k for k in keywords if re.search(r"\b" + re.escape(k.strip()), text)), None)
        if hit:
            if account == "Sales Revenue" and txn.amount < 0:
                continue
            return Classification(txn.id, account, conf, "rule", f"keyword '{hit.strip()}'")
    if txn.amount > 0:
        return Classification(txn.id, "Sales Revenue", 0.4, "rule", "no keyword; positive amount defaults to revenue")
    return Classification(txn.id, "Suspense / Uncategorized", 0.3, "rule", "no keyword matched")


@dataclass
class LLMConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> Optional["LLMConfig"]:
        key = os.getenv("LLM_API_KEY", "").strip()
        if not key:
            return None
        return cls(
            api_key=key,
            base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        )


SYSTEM_PROMPT = (
    "You are a bookkeeping assistant. For each bank transaction, pick the single best account "
    "from this chart of accounts: {accounts}. Reply with JSON only: "
    '{{"items": [{{"id": "...", "account": "...", "confidence": 0.0-1.0, "rationale": "short"}}]}}'
)


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


def classify_llm(txns: list[Transaction], cfg: LLMConfig, post: Optional[Callable] = None) -> dict[str, Classification]:
    """Call the LLM once for the batch. Raises on transport/parse errors."""
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(accounts=", ".join(CHART_OF_ACCOUNTS))},
            {
                "role": "user",
                "content": json.dumps(
                    [{"id": t.id, "date": t.date.isoformat(), "description": t.description, "amount": str(t.amount)} for t in txns]
                ),
            },
        ],
    }
    post = post or httpx.post
    resp = post(
        f"{cfg.base_url}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    out: dict[str, Classification] = {}
    for item in data.get("items", []):
        account = item.get("account")
        if item.get("id") and account in CHART_OF_ACCOUNTS:
            conf = float(item.get("confidence", 0.7))
            out[item["id"]] = Classification(item["id"], account, max(0.0, min(conf, 1.0)), "llm", str(item.get("rationale", ""))[:200])
    return out


def classify(txns: list[Transaction], cfg: Optional[LLMConfig] = None, post: Optional[Callable] = None) -> dict[str, Classification]:
    """Classify transactions, preferring the LLM and falling back to rules per row."""
    cfg = cfg if cfg is not None else LLMConfig.from_env()
    llm_results: dict[str, Classification] = {}
    note = None
    if cfg and txns:
        try:
            llm_results = classify_llm(txns, cfg, post=post)
        except Exception as exc:  # noqa: BLE001 - any failure means fallback
            note = f"LLM unavailable ({type(exc).__name__}); used rules"
    out = {}
    for t in txns:
        if t.id in llm_results:
            out[t.id] = llm_results[t.id]
        else:
            c = classify_rule(t)
            if cfg:
                c.note = note or "LLM returned no valid account; used rules"
            out[t.id] = c
    return out
