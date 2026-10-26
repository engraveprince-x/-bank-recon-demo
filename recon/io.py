"""CSV loading with forgiving column mapping."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Union

from .models import Transaction

COLUMN_ALIASES = {
    "date": ["date", "transaction date", "posting date", "posted date", "value date", "txn date"],
    "description": ["description", "memo", "details", "narrative", "payee", "name"],
    "amount": ["amount", "amt", "value"],
    "debit": ["debit", "withdrawal", "withdrawals", "money out"],
    "credit": ["credit", "deposit", "deposits", "money in"],
    "reference": ["reference", "ref", "ref no", "reference number", "check number", "cheque number", "doc no"],
    "id": ["id", "entry id", "entry_id", "txn id", "transaction id", "line id"],
    "account": ["account", "gl account", "category"],
}

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y"]


class CSVFormatError(ValueError):
    pass


def parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise CSVFormatError(f"Unrecognised date: {raw!r}")


def parse_amount(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", "").replace("$", "").replace(" ", "")
    if not s:
        return Decimal("0")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise CSVFormatError(f"Unrecognised amount: {raw!r}") from exc
    value = -value if negative else value
    return value.quantize(Decimal("0.01"))


def _map_columns(fieldnames: Iterable[str]) -> dict[str, str]:
    normalised = {f.strip().lower().replace("_", " "): f for f in fieldnames if f}
    mapping: dict[str, str] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            alias_n = alias.replace("_", " ")
            if alias_n in normalised:
                mapping[key] = normalised[alias_n]
                break
    return mapping


def load_transactions(src: Union[str, Path, bytes], source: str) -> list[Transaction]:
    """Load bank or ledger rows from a path or raw CSV bytes."""
    if isinstance(src, bytes):
        text = src.decode("utf-8-sig")
    else:
        text = Path(src).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CSVFormatError("CSV has no header row")
    cols = _map_columns(reader.fieldnames)
    if "date" not in cols or "description" not in cols:
        raise CSVFormatError(f"{source} CSV needs at least date and description columns; got {reader.fieldnames}")
    if "amount" not in cols and not ("debit" in cols or "credit" in cols):
        raise CSVFormatError(f"{source} CSV needs an amount column or debit/credit columns")

    prefix = "B" if source == "bank" else "L"
    txns: list[Transaction] = []
    seen_ids: set[str] = set()
    for i, row in enumerate(reader, start=1):
        if not any((v or "").strip() for v in row.values()):
            continue
        if "amount" in cols:
            amount = parse_amount(row[cols["amount"]])
        else:
            credit = parse_amount(row.get(cols.get("credit", ""), "") or "")
            debit = parse_amount(row.get(cols.get("debit", ""), "") or "")
            amount = credit - abs(debit)
        txn_id = (row.get(cols["id"], "") or "").strip() if "id" in cols else ""
        if not txn_id or txn_id in seen_ids:
            txn_id = f"{prefix}{i:04d}"
        seen_ids.add(txn_id)
        txns.append(
            Transaction(
                id=txn_id,
                source=source,
                date=parse_date(row[cols["date"]]),
                description=(row[cols["description"]] or "").strip(),
                amount=amount,
                reference=(row.get(cols["reference"], "") or "").strip() if "reference" in cols else "",
                account=(row.get(cols["account"], "") or "").strip() if "account" in cols else "",
            )
        )
    return txns
