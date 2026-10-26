"""Generate a fictitious bank statement + ledger for one month.

All company, vendor and customer names are invented. The script deliberately
plants the problems the matcher must find and writes the ground truth to
``expected.json`` so the tests can check the engine against it.

    python scripts/generate_sample_data.py            # writes data/sample/
"""
from __future__ import annotations

import csv
import json
import random
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

SEED = 20260301
START = date(2026, 3, 1)

# (ledger description, bank description variant, sign, min, max)
COUNTERPARTIES = [
    ("Bluefin Office Supply - toner and paper", "POS PURCHASE BLUEFIN OFFICE SUPPLY", -1, 40, 400),
    ("Harbor Lane Coffee - client meeting", "HARBOR LANE COFFEE #2231", -1, 8, 90),
    ("Cobaltcloud Hosting - monthly plan", "COBALTCLOUD HOSTING SUBSCRIPTION", -1, 50, 600),
    ("Quillmark Legal LLP - retainer", "ACH QUILLMARK LEGAL", -1, 500, 3000),
    ("Tidewater Electric - utility bill", "TIDEWATER ELECTRIC AUTOPAY", -1, 120, 480),
    ("Sparrowline Airlines - conference travel", "SPARROWLINE AIR TICKET", -1, 180, 900),
    ("Pinecrest Insurance - premium", "PINECREST INSURANCE PREMIUM", -1, 200, 800),
    ("Brightpath Ads - campaign", "BRIGHTPATH ADS MARKETING", -1, 100, 1500),
    ("Orchard Row Catering - team lunch", "ORCHARD ROW CATERING", -1, 60, 350),
    ("Customer payment - Bluepeak Outfitters", "DEPOSIT FROM BLUEPEAK OUTFITTERS", 1, 400, 6000),
    ("Customer payment - Lumen Garden Co", "ACH CREDIT LUMEN GARDEN CO", 1, 300, 5000),
    ("Customer payment - Redwood Kite Works", "REMITTANCE REDWOOD KITE WORKS", 1, 250, 4000),
    ("Customer payment - Silverbirch Studio", "DEPOSIT FROM SILVERBIRCH STUDIO", 1, 500, 7000),
    ("Customer payment - Northgate Bicycle Shop", "ACH CREDIT NORTHGATE BICYCLE", 1, 200, 3500),
]

# bank lines with no ledger entry (should be classified)
BANK_ONLY = [
    ("MONTHLY SERVICE CHARGE", -25.00),
    ("WIRE FEE OUTGOING", -30.00),
    ("INTEREST PAYMENT", 4.87),
    ("FX FEE INTL CARD", -3.12),
    ("PAYROLL RUN 03/15 PAYROLL SVC", -8420.55),
    ("STATE TAX PAYMENT ONLINE", -612.00),
    ("RIDE SHARE TRIP 7781", -23.40),
    ("SQ *GREENLEAF MARKET", -46.19),
    ("UNKNOWN DEBIT 99812", -149.99),
    ("MOBILE DEPOSIT", 275.00),
]

# ledger entries with no bank line yet
LEDGER_ONLY = [
    ("Check #1043 - Quillmark Legal LLP", -1250.00, "CHK-1043"),
    ("Check #1044 - Tidewater Electric", -312.40, "CHK-1044"),
    ("Deposit in transit - Lumen Garden Co", 2140.00, "DEP-0331"),
    ("Accrued rent - Maplewood Properties", -2800.00, "RENT-03"),
    ("Petty cash replenishment", -200.00, "PC-03"),
]


def money(rng, lo, hi) -> Decimal:
    return Decimal(str(round(rng.uniform(lo, hi), 2))).quantize(Decimal("0.01"))


def generate(out_dir: Path, n_pairs: int = 110) -> dict:
    rng = random.Random(SEED)
    bank, ledger = [], []
    expected = {"exact": 0, "fuzzy": 0, "many_to_one": 0, "one_to_many": 0, "amount_difference": 0,
                "bank_duplicates": 0, "ledger_duplicates": 0,
                "unmatched_bank": len(BANK_ONLY), "unmatched_ledger": len(LEDGER_ONLY)}
    used_amounts: set[Decimal] = set()
    ref_no = 2000

    def uniq_amount(lo, hi):
        while True:
            a = money(rng, lo, hi)
            if a not in used_amounts:
                used_amounts.add(a)
                return a

    def add_bank(d, desc, amt, ref=""):
        bank.append({"date": d.isoformat(), "description": desc, "amount": f"{amt:.2f}", "reference": ref})

    def add_ledger(d, desc, amt, ref, account=""):
        ledger.append({"entry_id": f"JE-{len(ledger) + 1:04d}", "date": d.isoformat(), "description": desc,
                       "amount": f"{amt:.2f}", "reference": ref, "account": account})

    # 1) ordinary pairs: ~60% exact (reference on both), ~40% fuzzy (bank lacks reference)
    for _ in range(n_pairs):
        ldesc, bdesc, sign, lo, hi = rng.choice(COUNTERPARTIES)
        amt = uniq_amount(lo, hi) * sign
        ldate = START + timedelta(days=rng.randint(0, 27))
        lag = rng.randint(0, 3)
        ref_no += 1
        ref = f"{'INV' if sign > 0 else 'PO'}-{ref_no}"
        add_ledger(ldate, ldesc, amt, ref)
        if rng.random() < 0.6:
            add_bank(ldate + timedelta(days=lag), f"{bdesc} {ref}", amt, ref)
            expected["exact"] += 1
        else:
            add_bank(ldate + timedelta(days=lag), f"{bdesc} {rng.randint(10000, 99999)}", amt)
            expected["fuzzy"] += 1

    # 2) amount differences: same reference, bank amount slightly different
    for desc, bdesc, base, delta in [
        ("Customer payment - Silverbirch Studio", "WIRE IN SILVERBIRCH STUDIO", 4250.00, -15.00),  # receiving bank fee
        ("Cobaltcloud Hosting - annual add-on", "COBALTCLOUD HOSTING", -480.00, -12.48),         # FX conversion
        ("Customer payment - Redwood Kite Works", "REMITTANCE REDWOOD KITE WORKS", 1875.50, -1.50),
        ("Brightpath Ads - campaign", "BRIGHTPATH ADS", -720.00, 7.20),                          # partial credit
    ]:
        ref_no += 1
        ref = f"{'INV' if base > 0 else 'PO'}-{ref_no}"
        d = START + timedelta(days=rng.randint(2, 26))
        add_ledger(d, desc, Decimal(str(base)), ref)
        add_bank(d + timedelta(days=1), f"{bdesc} {ref}", Decimal(str(base + delta)), ref)
        expected["amount_difference"] += 1

    # 3) split payments: one invoice paid in instalments (many bank -> one ledger)
    for desc, bdesc, parts in [
        ("Quillmark Legal LLP - March retainer", "ACH QUILLMARK LEGAL", [-1500.00, -1500.00, -620.35]),
        ("Customer payment - Bluepeak Outfitters", "DEPOSIT FROM BLUEPEAK OUTFITTERS", [3000.00, 2411.20]),
        ("Pinecrest Insurance - quarterly premium", "PINECREST INSURANCE", [-410.10, -410.10, -410.13]),
    ]:
        ref_no += 1
        ref = f"{'INV' if parts[0] > 0 else 'PO'}-{ref_no}"
        d = START + timedelta(days=rng.randint(3, 20))
        add_ledger(d, desc, Decimal(str(round(sum(parts), 2))), ref)
        for k, p in enumerate(parts):
            add_bank(d + timedelta(days=2 * k), f"{bdesc} PART {k + 1} {ref}", Decimal(str(p)), "")
        expected["many_to_one"] += 1

    # 4) one deposit settling several invoices (one bank -> many ledger)
    for cust, invoices in [("Lumen Garden Co", [880.00, 1215.75]), ("Northgate Bicycle Shop", [450.00, 610.40, 299.99])]:
        d = START + timedelta(days=rng.randint(5, 22))
        for amt in invoices:
            ref_no += 1
            add_ledger(d - timedelta(days=1), f"Invoice INV-{ref_no} - {cust}", Decimal(str(amt)), f"INV-{ref_no}")
        add_bank(d, f"BATCH REMITTANCE {cust.upper()}", Decimal(str(round(sum(invoices), 2))), "")
        expected["one_to_many"] += 1

    # 5) bank-only and ledger-only lines
    for desc, amt in BANK_ONLY:
        add_bank(START + timedelta(days=rng.randint(0, 30)), desc, Decimal(str(amt)))
    for desc, amt, ref in LEDGER_ONLY:
        add_ledger(START + timedelta(days=rng.randint(26, 30)), desc, Decimal(str(amt)), ref)

    # 6) duplicates: the bank charged three lines twice, and one bill was posted twice
    for row in rng.sample([r for r in bank if r["amount"].startswith("-") and r["reference"]], 3):
        bank.append(dict(row))
        expected["bank_duplicates"] += 1
    ledger_dup = dict(rng.choice([r for r in ledger if r["reference"].startswith("PO-")]))
    ledger_dup["entry_id"] = f"JE-{len(ledger) + 1:04d}"
    ledger.append(ledger_dup)
    expected["ledger_duplicates"] += 1

    bank.sort(key=lambda r: r["date"])
    ledger.sort(key=lambda r: r["date"])
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in [("bank_statement.csv", bank), ("ledger.csv", ledger)]:
        with (out_dir / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    expected["bank_rows"] = len(bank)
    expected["ledger_rows"] = len(ledger)
    (out_dir / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    return expected


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "data" / "sample"
    print(json.dumps(generate(target), indent=2))
