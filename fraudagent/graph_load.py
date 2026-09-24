"""Create, load and inspect the FraudGraph TigerGraph graph.

Usage (from the repo root):
    python -m fraudagent.graph_load schema    # run graph/schema.gsql
    python -m fraudagent.graph_load load      # upsert all vertices and edges
    python -m fraudagent.graph_load queries   # create + install graph/queries/*.gsql
    python -m fraudagent.graph_load stats     # vertex and edge counts
    python -m fraudagent.graph_load all       # schema, load, queries, stats
    python -m fraudagent.graph_load reset --yes   # DROP GRAPH (local types go with it)

Options for load: --only Txn,MADE (subset of vertex/edge types),
--chunk 5000, --workers 4.

Connection settings come from .env at the repo root (or the process env):
TG_HOST, TG_SECRET, TG_GRAPHNAME (fallback TG_GRAPH, default FraudGraph).
Input data: DATA_DIR/derived/txn.parquet and closed_cases.parquet
(DATA_DIR defaults to ./data).
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("DATA_DIR", REPO / "data"))
DERIVED = DATA / "derived"
GRAPH_DIR = REPO / "graph"
QUERY_DIR = GRAPH_DIR / "queries"
DEFAULT_GRAPH = "FraudGraph"
NULL_DT = "1970-01-01 00:00:00"
DT_FMT = "%Y-%m-%d %H:%M:%S"

# Query files in install order. install_all.gsql is not in this list.
QUERY_NAMES = [
    "card_window", "card_baseline", "device_neighbors", "region_activity",
    "email_neighbors", "card_cases", "similar_cases", "doc_search",
    "embeddings_dump", "device_ring", "card_community", "amount_band_scan",
]

VERTEX_TYPES = ["Customer", "PayCard", "Txn", "DeviceProfile", "EmailDomain", "BillingRegion", "ClosedCase"]
EDGE_TYPES = ["HAS_CARD", "MADE", "FROM_DEVICE", "PURCHASER_EMAIL", "RECIPIENT_EMAIL", "BILLED_IN",
              "NEXT", "CC_INVOLVES", "CC_ON_CARD", "CC_CONNECTED"]


# ---------------------------------------------------------------- settings

def load_env() -> None:
    """Load REPO/.env into os.environ without overriding values already set."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO / ".env", override=False)


def tg_settings() -> dict:
    load_env()
    host = os.environ.get("TG_HOST", "").strip()
    secret = os.environ.get("TG_SECRET", "").strip()
    graph = (os.environ.get("TG_GRAPHNAME") or os.environ.get("TG_GRAPH") or DEFAULT_GRAPH).strip()
    if host and not host.startswith("http"):
        host = "https://" + host
    return {"host": host.rstrip("/"), "secret": secret, "graph": graph}


def connect(settings: dict | None = None, quiet: bool = False):
    """Return an authenticated pyTigerGraph TigerGraphConnection."""
    import pyTigerGraph as tg

    s = settings or tg_settings()
    if not s["host"] or not s["secret"]:
        sys.exit("TG_HOST or TG_SECRET missing (set them in .env at the repo root)")
    conn = tg.TigerGraphConnection(host=s["host"], graphname=s["graph"], gsqlSecret=s["secret"], tgCloud=True)
    try:
        conn.getToken(s["secret"])
    except Exception as exc:  # the graph may not exist yet during `schema`
        if not quiet:
            print(f"[warn] getToken failed ({type(exc).__name__}: {str(exc)[:200]}); continuing with secret auth")
    return conn


def _with_graph(text: str, graph: str) -> str:
    """Rename the graph in a GSQL script when TG_GRAPHNAME is not FraudGraph."""
    return text if graph == DEFAULT_GRAPH else re.sub(r"\bFraudGraph\b", graph, text)


def _gsql_failed(out: str) -> bool:
    low = out.lower()
    return any(w in low for w in ("error", "failed", "fails", "exception", "not exist", "conflict"))


# ------------------------------------------------------------------ schema

def cmd_schema(conn, settings: dict) -> None:
    graph = settings["graph"]
    listing = str(conn.gsql("ls"))
    global_part = listing.split("Graphs:")[0]
    global_types = set(re.findall(r"-\s+(?:VERTEX|(?:UN)?DIRECTED EDGE)\s+(\w+)", global_part))
    ours = set(VERTEX_TYPES + ["InvestigationCase", "DocChunk"] + EDGE_TYPES
               + ["IC_INVOLVES", "IC_ON_CARD", "IC_CONNECTED", "IC_DEVICE", "IC_SIMILAR"])
    clash = sorted(ours & global_types)
    if re.search(rf"Graph\s+{re.escape(graph)}\s*\(", listing):
        print(f"graph {graph} already exists; skipping schema (use `reset --yes` to drop it first)")
        return
    if clash:
        sys.exit(f"type names already defined in the global schema: {clash}. Rename them in graph/schema.gsql "
                 "and graph/queries/*.gsql before running schema.")
    script = _with_graph((GRAPH_DIR / "schema.gsql").read_text(), graph)
    print(f"creating graph {graph} ...")
    out = str(conn.gsql(script))
    print(out)
    if _gsql_failed(out):
        sys.exit("schema script reported an error (see output above)")


def cmd_reset(conn, settings: dict, yes: bool) -> None:
    if not yes:
        sys.exit("reset drops the graph and all its data; pass --yes to confirm")
    print(conn.gsql(f"DROP GRAPH {settings['graph']}"))


# ------------------------------------------------------------ frame prep

def _s(series: pd.Series) -> list:
    """String column to a list with NaN as ''."""
    return series.astype(object).where(series.notna(), "").astype(str).tolist()


def _f(series: pd.Series) -> list:
    """Numeric column to a list of floats with NaN as 0.0."""
    return pd.to_numeric(series, errors="coerce").fillna(0.0).astype(float).tolist()


def _dt(series: pd.Series) -> list:
    ts = pd.to_datetime(series, errors="coerce")
    return ts.dt.strftime(DT_FMT).where(ts.notna(), NULL_DT).tolist()


def _sv(value) -> str:
    """Scalar to str with None/NaN as ''."""
    return "" if value is None or (isinstance(value, float) and math.isnan(value)) else str(value)


def _split_ids(value) -> list[str]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    return [p.strip() for p in str(value).split("|") if p.strip()]


def _num_id(value) -> str:
    """Closed-case txn ids arrive as float or str; normalise to '3000120'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    try:
        return str(int(float(value)))
    except ValueError:
        return str(value)


def read_txn() -> pd.DataFrame:
    return pd.read_parquet(DERIVED / "txn.parquet")


def read_closed_cases() -> pd.DataFrame:
    return pd.read_parquet(DERIVED / "closed_cases.parquet")


def vertex_payloads(tx: pd.DataFrame, cc: pd.DataFrame) -> dict[str, Callable[[], list]]:
    """Map vertex type -> builder returning [(id, attrs), ...]."""

    def customers():
        return [(c, {}) for c in sorted(tx["customer_id"].dropna().unique())]

    def cards():
        g = tx.groupby("card_id", sort=True).agg(customer_id=("customer_id", "first"),
                                                  network=("network", "first"),
                                                  card_type=("card_type", "first")).reset_index()
        return [(cid, {"customer_id": cu, "network": nw, "card_type": ct})
                for cid, cu, nw, ct in zip(g["card_id"], _s(g["customer_id"]), _s(g["network"]), _s(g["card_type"]))]

    def txns():
        cols = {
            "ts": _dt(tx["ts"]), "amt": _f(tx["amt"]), "product": _s(tx["product"]),
            "channel": _s(tx["channel"]), "addr1": _s(tx["addr1"]), "p_email": _s(tx["p_email"]),
            "r_email": _s(tx["r_email"]), "risk_score": _f(tx["risk_score"]), "model_p": _f(tx["model_p"]),
            "device_status": _s(tx["device_status"]), "ip_proxy": _s(tx["proxy"]), "m4": _s(tx["m4"]),
            "device_profile": _s(tx["device_profile"]), "device_type": _s(tx["device_type"]),
            "dist1": _f(tx["dist1"]), "card_id": _s(tx["card_id"]), "customer_id": _s(tx["customer_id"]),
            "device_id": _s(tx["device_id"]),
        }
        ids = _s(tx["txn_id"])
        keys = list(cols)
        return [(ids[i], {k: cols[k][i] for k in keys}) for i in range(len(ids))]

    def devices():
        d = tx.loc[tx["device_id"].notna(), ["device_id", "device_profile"]].drop_duplicates("device_id")
        return [(i, {"label": lab}) for i, lab in zip(_s(d["device_id"]), _s(d["device_profile"]))]

    def emails():
        doms = pd.concat([tx["p_email"], tx["r_email"]]).dropna().astype(str)
        return [(d, {}) for d in sorted(set(doms) - {""})]

    def regions():
        return [(a, {}) for a in sorted(set(_s(tx["addr1"])) - {""})]

    def closed():
        out = []
        opened, closed_at = _dt(cc["opened_at"]), _dt(cc["closed_at"])
        for i, r in enumerate(cc.itertuples(index=False)):
            out.append((str(r.case_id), {
                "customer_id": _sv(r.customer_id), "card_id": _sv(r.card_id),
                "opened_at": opened[i], "closed_at": closed_at[i],
                "outcome": _sv(r.outcome), "pattern": _sv(r.pattern),
                "first_fraud_txn_id": _num_id(r.first_fraud_txn_id),
                "exposure": float(0.0 if pd.isna(r.exposure_usd) else r.exposure_usd),
                "n_txns": int(0 if pd.isna(r.n_txns) else r.n_txns),
                "actions": _sv(r.actions_taken),
                "report_filed": str(r.report_filed).strip().lower() == "yes",
                "notes": _sv(r.analyst_notes),
            }))
        return out

    return {"Customer": customers, "PayCard": cards, "Txn": txns, "DeviceProfile": devices,
            "EmailDomain": emails, "BillingRegion": regions, "ClosedCase": closed}


def edge_payloads(tx: pd.DataFrame, cc: pd.DataFrame) -> dict[str, tuple[str, str, Callable[[], list]]]:
    """Map edge type -> (source type, target type, builder returning [(src, tgt, attrs), ...])."""
    ids = _s(tx["txn_id"])

    def pairs(src: list, tgt: list) -> list:
        return [(a, b, {}) for a, b in zip(src, tgt) if a and b]

    def has_card():
        d = tx[["customer_id", "card_id"]].drop_duplicates()
        return pairs(_s(d["customer_id"]), _s(d["card_id"]))

    def next_edges():
        o = tx[["card_id", "txn_id", "ts"]].sort_values(["card_id", "ts", "txn_id"], kind="mergesort")
        same = o["card_id"].eq(o["card_id"].shift(-1))
        gap = (o["ts"].shift(-1) - o["ts"]).dt.total_seconds()
        src, tgt, g = _s(o["txn_id"]), _s(o["txn_id"].shift(-1)), gap.fillna(0.0).tolist()
        return [(src[i], tgt[i], {"gap_s": float(g[i])}) for i, ok in enumerate(same.tolist()) if ok]

    known = set(ids)

    def cc_involves():
        out = []
        for cid, tids in zip(cc["case_id"].astype(str), cc["txn_ids"]):
            out += [(cid, t, {}) for t in (_num_id(x) for x in _split_ids(tids)) if t in known]
        return out

    def cc_on_card():
        return pairs(cc["case_id"].astype(str).tolist(), _s(cc["card_id"]))

    def cc_connected():
        out = []
        for cid, cards in zip(cc["case_id"].astype(str), cc["connected_card_ids"]):
            out += [(cid, c, {}) for c in _split_ids(cards)]
        return out

    return {
        "HAS_CARD": ("Customer", "PayCard", has_card),
        "MADE": ("PayCard", "Txn", lambda: pairs(_s(tx["card_id"]), ids)),
        "FROM_DEVICE": ("Txn", "DeviceProfile", lambda: pairs(ids, _s(tx["device_id"]))),
        "PURCHASER_EMAIL": ("Txn", "EmailDomain", lambda: pairs(ids, _s(tx["p_email"]))),
        "RECIPIENT_EMAIL": ("Txn", "EmailDomain", lambda: pairs(ids, _s(tx["r_email"]))),
        "BILLED_IN": ("Txn", "BillingRegion", lambda: pairs(ids, _s(tx["addr1"]))),
        "NEXT": ("Txn", "Txn", next_edges),
        "CC_INVOLVES": ("ClosedCase", "Txn", cc_involves),
        "CC_ON_CARD": ("ClosedCase", "PayCard", cc_on_card),
        "CC_CONNECTED": ("ClosedCase", "PayCard", cc_connected),
    }


# ------------------------------------------------------------------ upsert

class Uploader:
    """Chunked, retried, multi-threaded REST++ upserts (one connection per thread)."""

    def __init__(self, settings: dict, workers: int = 4, chunk: int = 5000, retries: int = 5):
        self.settings, self.workers, self.chunk, self.retries = settings, workers, chunk, retries
        self._local = threading.local()

    def _conn(self):
        if getattr(self._local, "conn", None) is None:
            self._local.conn = connect(self.settings, quiet=True)
        return self._local.conn

    def _retry(self, fn: Callable[[], int]) -> int:
        for attempt in range(1, self.retries + 1):
            try:
                return int(fn() or 0)
            except Exception as exc:
                if attempt == self.retries:
                    raise
                wait = min(30, 2 ** attempt)
                print(f"  [retry {attempt}] {type(exc).__name__}: {str(exc)[:160]} (sleep {wait}s)")
                time.sleep(wait)
                self._local.conn = None
        return 0

    def _run(self, label: str, items: list, send: Callable[[list], int]) -> int:
        if not items:
            print(f"{label}: nothing to load")
            return 0
        chunks = [items[i:i + self.chunk] for i in range(0, len(items), self.chunk)]
        t0, done, accepted = time.time(), 0, 0
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = [pool.submit(self._retry, lambda c=c: send(c)) for c in chunks]
            for fut in as_completed(futs):
                accepted += fut.result()
                done += 1
                if done == len(chunks) or done % max(1, len(chunks) // 10) == 0:
                    print(f"  {label}: {done}/{len(chunks)} chunks, {accepted:,} accepted, {time.time() - t0:.0f}s")
        print(f"{label}: {accepted:,}/{len(items):,} accepted in {time.time() - t0:.0f}s")
        return accepted

    def vertices(self, vtype: str, items: list) -> int:
        return self._run(vtype, items, lambda c: self._conn().upsertVertices(vtype, c))

    def edges(self, src_type: str, etype: str, tgt_type: str, items: list) -> int:
        return self._run(etype, items, lambda c: self._conn().upsertEdges(src_type, etype, tgt_type, c))


def cmd_load(settings: dict, only: Iterable[str] | None, chunk: int, workers: int) -> None:
    only_set = {o.strip() for o in only} if only else None
    print("reading parquet ...")
    tx, cc = read_txn(), read_closed_cases()
    up = Uploader(settings, workers=workers, chunk=chunk)
    for vtype, build in vertex_payloads(tx, cc).items():
        if only_set is None or vtype in only_set:
            up.vertices(vtype, build())
    for etype, (src, tgt, build) in edge_payloads(tx, cc).items():
        if only_set is None or etype in only_set:
            up.edges(src, etype, tgt, build())


# ----------------------------------------------------------------- queries

def cmd_queries(conn, settings: dict) -> None:
    graph = settings["graph"]
    created, failed = [], []
    for name in QUERY_NAMES:
        text = _with_graph((QUERY_DIR / f"{name}.gsql").read_text(), graph)
        out = str(conn.gsql(f"USE GRAPH {graph}\n{text}"))
        ok = "successfully created" in out.lower() or not _gsql_failed(out)
        (created if ok else failed).append(name)
        print(f"[{'ok' if ok else 'FAIL'}] create {name}")
        if not ok:
            print(out)
    if created:
        print(f"installing {len(created)} queries (this can take several minutes on Savanna) ...")
        out = str(conn.gsql(f"USE GRAPH {graph}\nINSTALL QUERY {', '.join(created)}"))
        print(out[-3000:])
    if failed:
        print(f"queries that failed to create: {failed}")
        if set(failed) & {"similar_cases", "doc_search"}:
            print("TigerGraphStore falls back to embeddings_dump + client-side cosine for vector search.")


def cmd_stats(conn) -> None:
    v = conn.getVertexCount("*")
    e = conn.getEdgeCount("*")
    print("vertices:")
    for k in sorted(v):
        print(f"  {k:<20} {v[k]:>10,}")
    print("edges:")
    for k in sorted(e):
        print(f"  {k:<20} {e[k]:>10,}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m fraudagent.graph_load")
    ap.add_argument("command", choices=["schema", "load", "queries", "stats", "all", "reset"])
    ap.add_argument("--only", default="", help="comma-separated vertex/edge types for load")
    ap.add_argument("--chunk", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--yes", action="store_true", help="confirm reset")
    a = ap.parse_args(argv)

    settings = tg_settings()
    only = [o for o in a.only.split(",") if o] or None
    if a.command in ("schema", "all"):
        cmd_schema(connect(settings), settings)
    if a.command in ("load", "all"):
        cmd_load(settings, only, a.chunk, a.workers)
    if a.command in ("queries", "all"):
        cmd_queries(connect(settings), settings)
    if a.command in ("stats", "all"):
        cmd_stats(connect(settings))
    if a.command == "reset":
        cmd_reset(connect(settings), settings, a.yes)


if __name__ == "__main__":
    main()
