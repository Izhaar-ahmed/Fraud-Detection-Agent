"""Run the agent over case_pack.csv and write cases/<case_id>.json plus traces.

Usage: python -m fraudagent.run_cases [--backend local|mcp|pytg] [--only HHG-001,HHG-002]
Before the first run: python -m fraudagent.kb (embeds documents and closed cases into the store).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from .agent import Investigation  # noqa: E402
from .graphstore import open_store  # noqa: E402
from .llm import LLM  # noqa: E402

DATA = REPO / "data"


def flag_times(store, rows) -> dict:
    t = pd.read_parquet(DATA / "derived" / "txn.parquet", columns=["txn_id", "ts"]).set_index("txn_id").ts
    return {str(r["flagged_txn_id"]): str(t[str(r["flagged_txn_id"])]) for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="local")
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(REPO / "cases"))
    a = ap.parse_args()
    store = open_store(a.backend)
    rows = pd.read_csv(DATA / "case_pack.csv").to_dict("records")
    if a.only:
        rows = [r for r in rows if r["case_id"] in a.only.split(",")]
    ft = flag_times(store, rows)
    out = Path(a.out)
    (out / "_trace").mkdir(parents=True, exist_ok=True)
    llm = LLM()
    for r in sorted(rows, key=lambda r: r["opened_at"]):  # chronological, so earlier cases become memory for later ones
        rec = Investigation(store, r, ft[str(r["flagged_txn_id"])], llm).run()
        trace = rec.pop("_trace")
        (out / f"{r['case_id']}.json").write_text(json.dumps(rec, indent=2, default=str))
        (out / "_trace" / f"{r['case_id']}.json").write_text(json.dumps({"steps": trace, "tool_trace": store.trace[-60:]}, indent=2, default=str))
        c = rec["case"]
        print(f"{r['case_id']} {c['verdict']:<10} p={c['fraud_probability']:.2f} {c['pattern']:<28} exp=${c['exposure_usd']:>9,.2f} "
              f"sar={rec['sar']['file']!s:<5} final={[x['action'] for x in rec['next_best_actions']['final']]}")
    store.close()


if __name__ == "__main__":
    main()
