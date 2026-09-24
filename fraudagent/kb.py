"""Build the GraphRAG knowledge base: document chunks and closed-case embeddings, written into the store.

Usage: python -m fraudagent.kb [--backend local|mcp|pytg]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from .embed import embed  # noqa: E402
from .graphstore import open_store  # noqa: E402


def chunks() -> list[dict]:
    out = []
    for p in sorted((REPO / "docs" / "kb").glob("*.md")):
        text = p.read_text()
        for sec in re.split(r"\n## ", text)[1:]:
            title, _, body = sec.partition("\n")
            out.append({"id": f"{p.stem}:{title[:40]}", "source": p.name, "section": title.strip(), "text": body.strip(),
                        "embedding": embed(title + " " + body)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="local")
    a = ap.parse_args()
    store = open_store(a.backend)
    docs = chunks()
    print("doc chunks", store.upsert_doc_chunks(docs))
    cc = pd.read_parquet(REPO / "data" / "derived" / "closed_cases.parquet")
    embs = {r.case_id: embed(f"{r.outcome} {r.pattern} {r.analyst_notes}") for r in cc.itertuples()}
    print("closed case embeddings", store.upsert_closed_case_embeddings(embs))
    store.close()


if __name__ == "__main__":
    main()
