"""FastAPI web app: upload CSVs, view results, review exceptions, export."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from recon.classifier import LLMConfig
from recon.io import CSVFormatError
from recon.pipeline import run
from recon.report import build_summary, export_results_csv, export_summary_csv, export_xlsx
from recon.review import AuditLog, ReviewSession

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "data" / "sample"
STATIC = Path(__file__).parent / "static"


class Decision(BaseModel):
    decision: str
    user: str
    note: str = ""


def create_app(audit_path: Optional[Path] = None) -> FastAPI:
    app = FastAPI(title="Bank Reconciliation Assistant", version="0.1.0")
    audit = AuditLog(audit_path if audit_path is not None else Path(os.getenv("RECON_AUDIT_LOG", ROOT / "data" / "audit_log.jsonl")))
    state: dict = {"session": None, "label": ""}
    lock = threading.Lock()

    def _load(bank, ledger, label: str, user: str):
        try:
            result = run(bank, ledger)
        except CSVFormatError as exc:
            raise HTTPException(400, str(exc))
        with lock:
            state["session"] = ReviewSession(result, audit)
            state["label"] = label
        audit.record(user or "system", "reconcile.run", label, result.counts())
        return _state()

    def _session() -> ReviewSession:
        if state["session"] is None:
            raise HTTPException(409, "No reconciliation loaded yet")
        return state["session"]

    def _state() -> dict:
        s: Optional[ReviewSession] = state["session"]
        base = {"loaded": s is not None, "llm_enabled": LLMConfig.from_env() is not None, "audit": audit.entries()[-200:][::-1]}
        if s is None:
            return base
        r = s.result
        txn = lambda src, i: (r.bank if src == "bank" else r.ledger)[i].to_dict()
        return {
            **base,
            "label": state["label"],
            "counts": r.counts(),
            "summary": build_summary(r, s.decisions),
            "matches": [{**m.to_dict(), "bank": [txn("bank", i) for i in m.bank_ids],
                         "ledger": [txn("ledger", i) for i in m.ledger_ids],
                         "status": s.decisions.get(m.id, {}).get("decision", "pending" if m.needs_review else "auto")}
                        for m in r.matches],
            "duplicates": [{**d.to_dict(), "txn": txn(d.source, d.txn_id)} for d in r.duplicates],
            "unmatched_bank": [{**txn("bank", i), "classification": r.classifications[i].to_dict() if i in r.classifications else None}
                               for i in r.unmatched_bank],
            "unmatched_ledger": [txn("ledger", i) for i in r.unmatched_ledger],
            "review": s.items(),
        }

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/state")
    def get_state():
        return _state()

    @app.post("/api/sample")
    def load_sample(user: str = Form("demo")):
        return _load(SAMPLE_DIR / "bank_statement.csv", SAMPLE_DIR / "ledger.csv", "sample data (fictitious)", user)

    @app.post("/api/upload")
    async def upload(bank: UploadFile = File(...), ledger: UploadFile = File(...), user: str = Form("demo")):
        return _load(await bank.read(), await ledger.read(), f"{bank.filename} + {ledger.filename}", user)

    @app.post("/api/review/{item_id}")
    def review(item_id: str, body: Decision):
        s = _session()
        try:
            s.decide(item_id, body.decision, body.user, body.note)
        except KeyError:
            raise HTTPException(404, f"Unknown review item {item_id}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return _state()

    @app.get("/api/export/{name}")
    def export(name: str):
        s = _session()
        if name == "results.csv":
            return Response(export_results_csv(s.result, s.decisions), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=reconciliation_results.csv"})
        if name == "summary.csv":
            return Response(export_summary_csv(s.result, s.decisions), media_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=reconciliation_summary.csv"})
        if name == "report.xlsx":
            data = export_xlsx(s.result, s.decisions, s.items(), audit.entries())
            return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": "attachment; filename=reconciliation_report.xlsx"})
        raise HTTPException(404, "Unknown export")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
