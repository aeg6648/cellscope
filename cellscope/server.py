"""FastAPI app: GET / (static UI), POST /ask (NDJSON stream of stage events), GET /usage."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .otari_client import OtariClient, UsageLedger
from .pipeline import Pipeline

STATIC = Path(__file__).resolve().parent.parent / "static"
END_USERS = ["engineer-a", "intern-b"]
app = FastAPI(title="CellScope")
ledger = UsageLedger()


class Ask(BaseModel):
    question: str
    end_user: str = "engineer-a"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/usage")
def usage():
    return {"cap_usd": ledger.cap(),
            "users": {u: {"spent_usd": ledger.spent(u), "cap_usd": ledger.cap()} for u in sorted(set(END_USERS) | set(ledger.data))}}


@app.post("/ask")
def ask(req: Ask):
    pipe = Pipeline(OtariClient(ledger=ledger))

    def stream():
        for ev in pipe.run(req.question, req.end_user):
            yield json.dumps(ev, default=str) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")
