from recon.matcher import reconcile
from tests.helpers import b, l


def test_many_bank_lines_to_one_ledger_entry():
    bank = [
        b("B1", "2026-03-05", "ACH QUILLMARK LEGAL PART 1 PO-7", -1500),
        b("B2", "2026-03-07", "ACH QUILLMARK LEGAL PART 2 PO-7", -1500),
        b("B3", "2026-03-09", "ACH QUILLMARK LEGAL PART 3 PO-7", -620.35),
    ]
    r = reconcile(bank, [l("L1", "2026-03-05", "Quillmark Legal - retainer", -3620.35, "PO-7")])
    assert [m.type for m in r.matches] == ["many_to_one"]
    m = r.matches[0]
    assert m.bank_ids == ["B1", "B2", "B3"] and m.ledger_ids == ["L1"]
    assert m.difference == 0
    assert r.unmatched_bank == []


def test_one_bank_deposit_to_many_invoices():
    ledger = [
        l("L1", "2026-03-09", "Invoice INV-1 - Northgate Bicycle Shop", 450.00, "INV-1"),
        l("L2", "2026-03-09", "Invoice INV-2 - Northgate Bicycle Shop", 610.40, "INV-2"),
        l("L3", "2026-03-09", "Invoice INV-3 - Northgate Bicycle Shop", 299.99, "INV-3"),
        l("L4", "2026-03-09", "Invoice INV-4 - Lumen Garden Co", 100.00, "INV-4"),
    ]
    r = reconcile([b("B1", "2026-03-10", "BATCH REMITTANCE NORTHGATE BICYCLE SHOP", 1360.39)], ledger)
    assert [m.type for m in r.matches] == ["one_to_many"]
    assert r.matches[0].ledger_ids == ["L1", "L2", "L3"]
    assert r.unmatched_ledger == ["L4"]


def test_split_requires_exact_sum():
    bank = [b("B1", "2026-03-05", "PINECREST INSURANCE", -400), b("B2", "2026-03-06", "PINECREST INSURANCE", -400)]
    r = reconcile(bank, [l("L1", "2026-03-05", "Pinecrest Insurance - premium", -800.01)])
    assert all(m.type not in ("many_to_one", "one_to_many") for m in r.matches)


def test_split_ignores_opposite_signs_and_unrelated_descriptions():
    bank = [b("B1", "2026-03-05", "PINECREST INSURANCE", -300), b("B2", "2026-03-05", "PINECREST INSURANCE", 100),
            b("B3", "2026-03-05", "TIDEWATER ELECTRIC", -100)]
    r = reconcile(bank, [l("L1", "2026-03-05", "Pinecrest Insurance - premium", -400)])
    assert r.matches == [] or all(set(m.bank_ids) != {"B1", "B3"} for m in r.matches)


def test_split_prefers_fewest_parts():
    bank = [b("B1", "2026-03-05", "ACH QUILLMARK LEGAL", -600), b("B2", "2026-03-05", "ACH QUILLMARK LEGAL", -400),
            b("B3", "2026-03-05", "ACH QUILLMARK LEGAL", -300), b("B4", "2026-03-05", "ACH QUILLMARK LEGAL", -100)]
    r = reconcile(bank, [l("L1", "2026-03-05", "Quillmark Legal retainer", -1000)])
    assert len(r.matches) == 1 and len(r.matches[0].bank_ids) == 2
