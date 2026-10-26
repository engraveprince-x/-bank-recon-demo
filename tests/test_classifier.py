import json

import httpx

from recon.classifier import LLMConfig, classify, classify_rule
from tests.helpers import b

TXNS = [b("B1", "2026-03-01", "MONTHLY SERVICE CHARGE", -25), b("B2", "2026-03-02", "UNKNOWN DEBIT 99812", -149.99),
        b("B3", "2026-03-03", "INTEREST PAYMENT", 4.87)]


def test_rule_classification():
    assert classify_rule(TXNS[0]).account == "Bank Fees"
    assert classify_rule(TXNS[2]).account == "Interest Income"
    c = classify_rule(TXNS[1])
    assert c.account == "Suspense / Uncategorized" and c.confidence < 0.6 and c.source == "rule"


def test_rule_keywords_use_word_boundaries():
    # "rent" must not fire inside "current"
    assert classify_rule(b("B9", "2026-03-01", "CURRENT ACCOUNT ADJ", -5)).account != "Rent Expense"


def test_no_key_falls_back_to_rules(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    out = classify(TXNS)
    assert {c.source for c in out.values()} == {"rule"}
    assert all(c.note is None for c in out.values())


def test_llm_error_falls_back_to_rules():
    def boom(*a, **k):
        raise httpx.ConnectError("offline")
    out = classify(TXNS, LLMConfig(api_key="x", base_url="http://llm.invalid/v1"), post=boom)
    assert {c.source for c in out.values()} == {"rule"}
    assert "ConnectError" in out["B1"].note


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_llm_success_and_per_row_fallback():
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"], seen["auth"], seen["model"] = url, headers["Authorization"], json["model"]
        content = '```json\n' + __import__("json").dumps({"items": [
            {"id": "B1", "account": "Bank Fees", "confidence": 0.95, "rationale": "fee"},
            {"id": "B2", "account": "Not An Account", "confidence": 0.9},  # invalid -> rule fallback
        ]}) + '\n```'
        return FakeResponse({"choices": [{"message": {"content": content}}]})

    cfg = LLMConfig(api_key="k", base_url="http://llm.local/v1", model="m")
    out = classify(TXNS, cfg, post=fake_post)
    assert seen == {"url": "http://llm.local/v1/chat/completions", "auth": "Bearer k", "model": "m"}
    assert out["B1"].source == "llm" and out["B1"].account == "Bank Fees"
    assert out["B2"].source == "rule" and out["B3"].source == "rule"


def test_llm_config_from_env(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "abc")
    monkeypatch.setenv("LLM_BASE_URL", "http://x/v1/")
    monkeypatch.setenv("LLM_MODEL", "qwen")
    cfg = LLMConfig.from_env()
    assert (cfg.base_url, cfg.model) == ("http://x/v1", "qwen")
