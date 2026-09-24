"""Analyst web UI backend (Casework).

Usage: python -m fraudagent.server
Env: FRAUD_BACKEND = local (default) | mcp | pytg; HOST / PORT override 127.0.0.1:8000.
Serves web/ and a JSON API over case_pack.csv, the answer files in cases/ and the graph store.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from .agent import Investigation  # noqa: E402
from .graphstore import open_store  # noqa: E402
from .llm import LLM  # noqa: E402
from .run_cases import flag_times  # noqa: E402

DATA = REPO / "data"
CASES = REPO / "cases"
WEB = REPO / "web"
BACKEND = os.environ.get("FRAUD_BACKEND", "local")
FMT = "%Y-%m-%d %H:%M:%S"

app = FastAPI(title="Casework")

_store = None
_store_lock = threading.Lock()   # guards store creation
_query_lock = threading.Lock()   # serialises store queries and investigations
_flag_ts: dict[str, str] | None = None
_llm = LLM()


def store():
    global _store
    with _store_lock:
        if _store is None:
            _store = open_store(BACKEND)
        return _store


def case_rows() -> list[dict]:
    df = pd.read_csv(DATA / "case_pack.csv", dtype={"flagged_txn_id": str})
    df = df.where(pd.notna(df), None)
    return df.to_dict("records")


def case_row(case_id: str) -> dict:
    row = next((r for r in case_rows() if r["case_id"] == case_id), None)
    if row is None:
        raise HTTPException(404, f"unknown case {case_id}")
    return row


def flag_ts(row: dict) -> str:
    global _flag_ts
    if _flag_ts is None:
        _flag_ts = flag_times(None, case_rows())
    return _flag_ts[str(row["flagged_txn_id"])]


def read_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def parse_amount(text: str) -> float | None:
    m = re.search(r"\$([0-9][0-9,]*\.?[0-9]*)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def shift(ts: str, **delta) -> str:
    return (datetime.strptime(ts, FMT) + timedelta(**delta)).strftime(FMT)


# ---------------------------------------------------------------- API
@app.get("/api/health")
def health() -> dict:
    return {"backend": BACKEND, "store_loaded": _store is not None,
            "tool_calls": _store.tool_calls if _store is not None else 0,
            "llm_available": _llm.available}


@app.get("/api/cases")
def list_cases() -> list[dict]:
    out = []
    for r in case_rows():
        ans = read_json(CASES / f"{r['case_id']}.json")
        c = (ans or {}).get("case", {})
        out.append({
            "case_id": r["case_id"], "opened_at": r["opened_at"], "trigger_type": r["trigger_type"],
            "trigger_text": r["trigger_text"], "card_id": r["card_id"], "customer_id": r["customer_id"],
            "flagged_txn_id": r["flagged_txn_id"], "amount": parse_amount(r["trigger_text"]),
            "risk_score": r["risk_score"], "verdict": c.get("verdict"), "status": c.get("status"),
            "pattern": c.get("pattern"), "probability": c.get("fraud_probability"),
            "exposure": c.get("exposure_usd"), "sar_file": (ans or {}).get("sar", {}).get("file"),
            "has_answer": ans is not None,
        })
    return out


@app.get("/api/cases/{case_id}")
def get_case(case_id: str) -> dict:
    row = case_row(case_id)
    return {"row": row, "answer": read_json(CASES / f"{case_id}.json"),
            "trace": read_json(CASES / "_trace" / f"{case_id}.json")}


@app.get("/api/cases/{case_id}/graph")
def case_graph(case_id: str) -> dict:
    row = case_row(case_id)
    ans = read_json(CASES / f"{case_id}.json") or {}
    c = ans.get("case", {})
    ts = flag_ts(row)
    st = store()
    with _query_lock:
        win = st.card_window(row["card_id"], shift(ts, days=-3), shift(ts, days=3))["txns"]
        flagged = next((t for t in win if t["txn_id"] == str(row["flagged_txn_id"])), {})
        dev = {}
        if flagged.get("device_id"):
            dev = st.device_neighbors(flagged["device_id"], shift(ts, days=-7), shift(ts, days=7))
    by_id = {t["txn_id"]: t for t in win}
    affected = set(c.get("affected_txn_ids", []))
    nodes, edges = [], []

    def node(nid, kind, label, **extra):
        nodes.append({"id": nid, "kind": kind, "label": label, **extra})

    node(row["customer_id"], "customer", row["customer_id"])
    node(row["card_id"], "card", row["card_id"])
    edges.append({"source": row["customer_id"], "target": row["card_id"], "label": "HOLDS"})
    txn_ids = [str(row["flagged_txn_id"])] + [t for t in c.get("affected_txn_ids", []) if t != str(row["flagged_txn_id"])]
    for tid in txn_ids[:12]:
        t = by_id.get(tid, {})
        node(tid, "txn", tid, amount=t.get("amt"), ts=t.get("ts"), channel=t.get("channel"),
             flagged=tid == str(row["flagged_txn_id"]), affected=tid in affected, model_p=t.get("model_p"))
        edges.append({"source": row["card_id"], "target": tid, "label": "PAID"})
    d = dev.get("device") if dev else None
    if d:
        node(d["device_id"], "device", d["label"], n_cards=dev.get("n_cards"))
        edges.append({"source": str(row["flagged_txn_id"]), "target": d["device_id"], "label": "FROM_DEVICE"})
        connected = set(c.get("connected_card_ids", []))
        others = [x for x in dev.get("cards", []) if x["card_id"] != row["card_id"]]
        others.sort(key=lambda x: (x["card_id"] not in connected, -x["max_p"]))
        for x in others[:15]:
            node(x["card_id"], "card", x["card_id"], n=x["n"], max_p=x["max_p"], connected=x["card_id"] in connected)
            edges.append({"source": d["device_id"], "target": x["card_id"], "label": "USED_BY"})
    for sid in c.get("similar_prior_cases", [])[:5]:
        node(sid, "closed_case", sid)
        edges.append({"source": c.get("graph_case_id", case_id), "target": sid, "label": "SIMILAR_TO"})
    if c.get("graph_case_id"):
        node(c["graph_case_id"], "case", c["graph_case_id"], verdict=c.get("verdict"))
        edges.append({"source": c["graph_case_id"], "target": row["card_id"], "label": "ABOUT"})
    return {"nodes": nodes, "edges": edges, "device_cards_total": (dev or {}).get("n_cards", 0)}


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/cases/{case_id}/run")
def run_case(case_id: str):
    row = case_row(case_id)
    q: queue.Queue = queue.Queue()

    def work():
        try:
            q.put(("status", {"text": "Loading graph store" if _store is None else "Store ready"}))
            ts = flag_ts(row)
            st = store()
            with _query_lock:
                q.put(("status", {"text": "Investigation started"}))
                rec = Investigation(st, row, ts, _llm, on_step=lambda s: q.put(("step", s))).run()
                trace = rec.pop("_trace")
                (CASES / "_trace").mkdir(parents=True, exist_ok=True)
                (CASES / f"{case_id}.json").write_text(json.dumps(rec, indent=2, default=str))
                (CASES / "_trace" / f"{case_id}.json").write_text(
                    json.dumps({"steps": trace, "tool_trace": st.trace[-60:]}, indent=2, default=str))
            q.put(("done", rec))
        except Exception as e:  # surfaced to the client as an error event
            q.put(("error", {"text": f"{type(e).__name__}: {e}"}))
        q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is None:
                return
            yield _sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/", StaticFiles(directory=WEB), name="web")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
