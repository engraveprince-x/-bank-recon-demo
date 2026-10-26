import json
from pathlib import Path

from recon.matcher import MatchConfig, reconcile, similarity
from recon.pipeline import run
from tests.helpers import b, l

ROOT = Path(__file__).resolve().parents[1]


def types(result):
    return sorted(m.type for m in result.matches)


def test_exact_match_requires_amount_reference_and_window():
    r = reconcile(
        [b("B1", "2026-03-02", "ACH QUILLMARK LEGAL PO-1", -500, "PO-1")],
        [l("L1", "2026-03-01", "Quillmark Legal retainer", -500, "PO-1")],
    )
    assert types(r) == ["exact"]
    assert r.matches[0].confidence == 1.0 and not r.matches[0].needs_review


def test_exact_match_reference_embedded_in_bank_description():
    r = reconcile([b("B1", "2026-03-02", "WIRE IN INV-77 BLUEPEAK", 120)], [l("L1", "2026-03-02", "Customer payment", 120, "INV-77")])
    assert types(r) == ["exact"]


def test_outside_date_window_is_not_matched():
    r = reconcile([b("B1", "2026-03-20", "Unrelated", -500, "PO-1")], [l("L1", "2026-03-01", "Other", -500, "PO-1")])
    assert r.matches == []
    assert r.unmatched_bank == ["B1"] and r.unmatched_ledger == ["L1"]


def test_fuzzy_match_on_amount_and_description():
    r = reconcile(
        [b("B1", "2026-03-04", "POS PURCHASE HARBOR LANE COFFEE #2231", -42.10)],
        [l("L1", "2026-03-02", "Harbor Lane Coffee - client meeting", -42.10)],
    )
    assert types(r) == ["fuzzy"]
    assert 0.8 < r.matches[0].confidence < 1.0


def test_fuzzy_rejects_dissimilar_description():
    r = reconcile([b("B1", "2026-03-02", "TIDEWATER ELECTRIC", -42.10)], [l("L1", "2026-03-02", "Brightpath Ads campaign", -42.10)])
    assert r.matches == []


def test_similarity_merchant_prefix():
    assert similarity("SPARROWLINE AIR TICKET 76143", "Sparrowline Airlines - conference travel") >= 80
    assert similarity("", "anything") == 0


def test_greedy_assignment_never_reuses_a_row():
    bank = [b("B1", "2026-03-02", "Bluefin Office Supply", -99.5), b("B2", "2026-03-03", "Bluefin Office Supply", -99.5)]
    ledger = [l("L1", "2026-03-02", "Bluefin Office Supply - paper", -99.5)]
    r = reconcile(bank, ledger)
    used = [i for m in r.matches for i in m.bank_ids]
    assert len(used) == len(set(used)) == 1
    assert len(r.unmatched_bank) == 1


def test_duplicates_are_flagged_and_held_back():
    bank = [b("B1", "2026-03-02", "COBALTCLOUD HOSTING", -60, "PO-9"), b("B2", "2026-03-02", "COBALTCLOUD HOSTING", -60, "PO-9")]
    r = reconcile(bank, [l("L1", "2026-03-02", "Cobaltcloud hosting", -60, "PO-9")])
    assert [d.txn_id for d in r.duplicates] == ["B2"]
    assert types(r) == ["exact"] and r.matches[0].bank_ids == ["B1"]


def test_amount_difference_with_same_reference_goes_to_review():
    r = reconcile([b("B1", "2026-03-05", "WIRE IN SILVERBIRCH INV-5", 4235, "INV-5")], [l("L1", "2026-03-04", "Customer payment", 4250, "INV-5")])
    assert types(r) == ["amount_difference"]
    m = r.matches[0]
    assert str(m.difference) == "-15.00" and m.needs_review


def test_amount_difference_beyond_tolerance_is_unmatched():
    r = reconcile([b("B1", "2026-03-05", "X INV-5", 1000, "INV-5")], [l("L1", "2026-03-04", "Y", 4250, "INV-5")],
                  MatchConfig(amount_tolerance_pct=10))
    assert r.matches == []


def test_sample_data_matches_planted_ground_truth():
    expected = json.loads((ROOT / "data/sample/expected.json").read_text())
    r = run(ROOT / "data/sample/bank_statement.csv", ROOT / "data/sample/ledger.csv")
    c = r.counts()
    assert c["bank_rows"] == expected["bank_rows"] and c["ledger_rows"] == expected["ledger_rows"]
    for t in ["exact", "fuzzy", "many_to_one", "one_to_many", "amount_difference"]:
        assert c["matches_by_type"].get(t, 0) == expected[t], t
    assert sum(d.source == "bank" for d in r.duplicates) == expected["bank_duplicates"]
    assert sum(d.source == "ledger" for d in r.duplicates) == expected["ledger_duplicates"]
    assert c["unmatched_bank"] == expected["unmatched_bank"]
    assert c["unmatched_ledger"] == expected["unmatched_ledger"]
