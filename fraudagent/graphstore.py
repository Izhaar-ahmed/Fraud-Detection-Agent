"""Graph backends for the fraud-investigation agent.

GraphStore defines one method per installed GSQL query (graph/queries) plus
case/embedding writes. Every method returns a JSON-able dict with the same
shape in both implementations:

* LocalGraphStore: pandas over DATA_DIR/derived/*.parquet, used for tests and
  offline runs. Written investigation cases persist to case_memory.json;
  closed-case embeddings to closed_case_embeddings.npz; doc chunks to
  doc_chunks.json (all in the store directory, default DATA_DIR/derived).
* TigerGraphStore (tg_store.py, re-exported here): calls the installed queries, over the
  tigergraph-mcp server (transport="mcp") or pyTigerGraph (transport="pytg").

Conventions: datetimes are "YYYY-MM-DD HH:MM:SS" strings; windows are
inclusive (t_from <= ts <= t_to); card_baseline counts ts < t_before.
Missing strings are "" and missing numbers 0.0, matching the loaded graph.
Every call increments `tool_calls` and appends {name, params, ms,
n_results, backend} to `trace`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .graph_load import DERIVED, DT_FMT, NULL_DT, _num_id, _split_ids

TXN_FIELDS = ["txn_id", "ts", "amt", "product", "channel", "addr1", "p_email", "r_email", "risk_score",
              "model_p", "device_status", "proxy", "m4", "device_profile", "device_type", "dist1",
              "card_id", "customer_id", "device_id"]
_STR_TXN = ["product", "channel", "addr1", "p_email", "r_email", "device_status", "proxy", "m4",
            "device_profile", "device_type", "card_id", "customer_id", "device_id"]
_NUM_TXN = ["amt", "risk_score", "model_p", "dist1"]
ANON_PROXY = "IP_PROXY:ANONYMOUS"
MAX_ROUNDS = 4


def _now() -> str:
    return datetime.now(timezone.utc).strftime(DT_FMT)


def _epoch_to_str(v: Any) -> str:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime(DT_FMT)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _summarise_params(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) > 8:
            out[k] = f"<vector len={len(v)}>"
        elif isinstance(v, dict) and k == "record":
            out[k] = {"case_id": v.get("case_id") or v.get("id")}
        else:
            out[k] = v
    return out


def _count(result: Any) -> int:
    if isinstance(result, dict):
        for key in ("txns", "cards", "results", "devices", "closed_cases", "chunks"):
            v = result.get(key)
            if isinstance(v, (list, dict)):
                return len(v)
        if "total" in result:
            return int(result["total"])
        if "n" in result:
            return int(result["n"])
    return 1 if result else 0


def _vec(v: Iterable[float]) -> list[float]:
    return [float(x) for x in v]


def _record_case_id(record: dict) -> str:
    cid = record.get("case_id") or record.get("id")
    if not cid:
        raise ValueError("record needs case_id")
    return str(cid)


def _case_links(record: dict) -> dict[str, list]:
    """Edge targets named by an investigation record (see write_case)."""
    def ids(key):
        v = record.get(key) or []
        return [str(x) for x in (v if isinstance(v, (list, tuple)) else _split_ids(v)) if str(x)]

    card = record.get("card_id")
    sims = []
    for s in record.get("similar_cases") or []:
        if isinstance(s, dict) and (s.get("case_id") or s.get("id")):
            sims.append((str(s.get("case_id") or s.get("id")), float(s.get("score") or 0.0)))
        elif isinstance(s, str):
            sims.append((s, 0.0))
    return {"txns": ids("txn_ids"), "cards": [str(card)] if card else [], "connected": ids("connected_card_ids"),
            "devices": ids("device_ids"), "similar": [s for s in sims if s[0].startswith("CC-")]}


def _case_vertex_attrs(record: dict, embedding: Iterable[float] | None) -> dict:
    attrs = {
        "case_ref": str(record.get("case_ref") or ""), "status": str(record.get("status") or ""),
        "verdict": str(record.get("verdict") or ""), "probability": float(record.get("probability") or 0.0),
        "pattern": str(record.get("pattern") or ""), "exposure": float(record.get("exposure") or 0.0),
        "summary": str(record.get("summary") or ""),
        "opened_at": str(record.get("opened_at") or _now()), "updated_at": str(record.get("updated_at") or _now()),
        "record": json.dumps(record, default=str),
    }
    if embedding is not None:
        attrs["embedding"] = _vec(embedding)
    return attrs


def _top_cosine(q: np.ndarray, mat: np.ndarray, k: int) -> list[tuple[int, float]]:
    if mat.size == 0 or q.size == 0 or mat.shape[1] != q.size:
        return []
    qn = np.linalg.norm(q)
    norms = np.linalg.norm(mat, axis=1)
    ok = (norms > 0) & (qn > 0)
    scores = np.full(mat.shape[0], -np.inf)
    scores[ok] = (mat[ok] @ q) / (norms[ok] * qn)
    order = np.argsort(-scores, kind="stable")[:k]
    return [(int(i), float(scores[i])) for i in order if np.isfinite(scores[i])]


class GraphStore:
    """Interface. Subclasses implement the _impl methods; public calls are traced."""

    backend = "abstract"

    def __init__(self) -> None:
        self.tool_calls = 0
        self.trace: list[dict] = []

    def _traced(self, name: str, params: dict, fn):
        t0 = time.perf_counter()
        result = fn()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.tool_calls += 1
        self.trace.append({"name": name, "params": _summarise_params(params), "ms": ms,
                           "n_results": _count(result), "backend": self.backend})
        return result

    # ---- reads
    def card_window(self, card_id: str, t_from: str, t_to: str) -> dict:
        p = {"card_id": card_id, "t_from": t_from, "t_to": t_to}
        return self._traced("card_window", p, lambda: self._card_window(**p))

    def card_baseline(self, card_id: str, t_before: str) -> dict:
        p = {"card_id": card_id, "t_before": t_before}
        return self._traced("card_baseline", p, lambda: self._card_baseline(**p))

    def device_neighbors(self, device_id: str, t_from: str, t_to: str) -> dict:
        p = {"device_id": device_id, "t_from": t_from, "t_to": t_to}
        return self._traced("device_neighbors", p, lambda: self._device_neighbors(**p))

    def region_activity(self, addr1: str, t_from: str, t_to: str, min_p: float = 0.5) -> dict:
        p = {"addr1": str(addr1), "t_from": t_from, "t_to": t_to, "min_p": float(min_p)}
        return self._traced("region_activity", p, lambda: self._region_activity(**p))

    def email_neighbors(self, domain: str, t_from: str, t_to: str, min_p: float = 0.5) -> dict:
        p = {"domain": domain, "t_from": t_from, "t_to": t_to, "min_p": float(min_p)}
        return self._traced("email_neighbors", p, lambda: self._email_neighbors(**p))

    def card_cases(self, card_id: str) -> dict:
        p = {"card_id": card_id}
        return self._traced("card_cases", p, lambda: self._card_cases(**p))

    def similar_cases(self, qvec: Iterable[float], k: int = 5, pattern_filter: str = "") -> dict:
        p = {"qvec": _vec(qvec), "k": max(1, int(k)), "pattern_filter": pattern_filter or ""}
        return self._traced("similar_cases", p, lambda: self._similar_cases(**p))

    def doc_search(self, qvec: Iterable[float], k: int = 5) -> dict:
        p = {"qvec": _vec(qvec), "k": max(1, int(k))}
        return self._traced("doc_search", p, lambda: self._doc_search(**p))

    def device_ring(self, t_from: str, t_to: str, min_cards: int = 3) -> dict:
        p = {"t_from": t_from, "t_to": t_to, "min_cards": int(min_cards)}
        return self._traced("device_ring", p, lambda: self._device_ring(**p))

    def card_community(self, card_id: str, t_from: str, t_to: str, max_hops: int = 2,
                       max_device_txns: int = 200) -> dict:
        p = {"card_id": card_id, "t_from": t_from, "t_to": t_to,
             "max_hops": min(MAX_ROUNDS, max(1, int(max_hops))), "max_device_txns": int(max_device_txns)}
        return self._traced("card_community", p, lambda: self._card_community(**p))

    def amount_band_scan(self, t_from: str, t_to: str, lo: float, hi: float) -> dict:
        p = {"t_from": t_from, "t_to": t_to, "lo": float(lo), "hi": float(hi)}
        return self._traced("amount_band_scan", p, lambda: self._amount_band_scan(**p))

    # ---- writes
    def write_case(self, record: dict, embedding: Iterable[float] | None = None) -> dict:
        p = {"record": record, "embedding": _vec(embedding) if embedding is not None else None}
        return self._traced("write_case", p, lambda: self._write_case(record, p["embedding"]))

    def upsert_closed_case_embeddings(self, embeddings: dict[str, Iterable[float]]) -> dict:
        p = {"n": len(embeddings)}
        emb = {str(k): _vec(v) for k, v in embeddings.items()}
        return self._traced("upsert_closed_case_embeddings", p, lambda: self._upsert_cc_emb(emb))

    def upsert_doc_chunks(self, chunks: list[dict]) -> dict:
        p = {"n": len(chunks)}
        clean = [{"id": str(c["id"]), "source": str(c.get("source", "")), "section": str(c.get("section", "")),
                  "text": str(c.get("text", "")), "embedding": _vec(c.get("embedding") or [])} for c in chunks]
        return self._traced("upsert_doc_chunks", p, lambda: self._upsert_docs(clean))

    def close(self) -> None:
        pass


# =================================================================== local

class LocalGraphStore(GraphStore):
    backend = "local"

    def __init__(self, derived_dir: str | Path | None = None, store_dir: str | Path | None = None,
                 persist: bool = True):
        super().__init__()
        self.derived = Path(derived_dir) if derived_dir else DERIVED
        self.store_dir = Path(store_dir) if store_dir else self.derived
        self.persist = persist
        self._load_txn()
        self._load_closed()
        self.cases: dict[str, dict] = {}          # case_id -> {"record", "embedding"}
        self.cc_emb: dict[str, list[float]] = {}  # closed case id -> embedding
        self.docs: dict[str, dict] = {}           # chunk id -> chunk
        self._load_memory()

    # ---- data
    def _load_txn(self) -> None:
        raw = pd.read_parquet(self.derived / "txn.parquet")
        tx = pd.DataFrame({"txn_id": raw["txn_id"].astype(str)})
        tx["ts_dt"] = pd.to_datetime(raw["ts"])
        for c in _STR_TXN:
            col = raw[c].astype(object)
            tx[c] = col.where(col.notna(), "").astype(str)
        for c in _NUM_TXN:
            tx[c] = pd.to_numeric(raw[c], errors="coerce").fillna(0.0).astype(float)
        tx["network"] = raw["network"].astype(object).where(raw["network"].notna(), "").astype(str)
        tx["card_type"] = raw["card_type"].astype(object).where(raw["card_type"].notna(), "").astype(str)
        tx = tx.sort_values(["ts_dt", "txn_id"], kind="mergesort").reset_index(drop=True)
        self.tx = tx
        self.ts_ns = tx["ts_dt"].to_numpy().astype("datetime64[ns]").astype(np.int64)
        self.by_card = self._index("card_id")
        self.by_device = self._index("device_id")
        self.by_addr1 = self._index("addr1")
        self.by_p_email = self._index("p_email")
        self.by_r_email = self._index("r_email")
        dev = tx[tx["device_id"] != ""]
        self.device_label = dict(zip(dev["device_id"], dev["device_profile"]))
        self.device_degree = {k: len(v) for k, v in self.by_device.items()}
        firsts = tx.drop_duplicates("card_id").set_index("card_id")
        self.card_info = {cid: {"card_id": cid, "customer_id": r.customer_id, "network": r.network,
                                "card_type": r.card_type} for cid, r in firsts.iterrows()}

    def _index(self, col: str) -> dict[str, np.ndarray]:
        """value -> ascending row positions in self.tx (rows sorted by ts); '' is skipped."""
        sub = self.tx[self.tx[col] != ""]
        pos = sub.index.to_numpy()
        return {k: pos[v] for k, v in sub.groupby(col, sort=False).indices.items()}

    def _load_closed(self) -> None:
        cc = pd.read_parquet(self.derived / "closed_cases.parquet")
        self.cc: dict[str, dict] = {}
        self.cc_on: dict[str, list[str]] = {}
        self.cc_conn: dict[str, list[str]] = {}
        for r in cc.itertuples(index=False):
            cid = str(r.case_id)
            txn_ids = [_num_id(x) for x in _split_ids(r.txn_ids)]
            self.cc[cid] = {
                "case_id": cid, "customer_id": str(r.customer_id), "card_id": str(r.card_id),
                "opened_at": str(pd.Timestamp(r.opened_at).strftime(DT_FMT)) if pd.notna(r.opened_at) else NULL_DT,
                "closed_at": str(pd.Timestamp(r.closed_at).strftime(DT_FMT)) if pd.notna(r.closed_at) else NULL_DT,
                "outcome": str(r.outcome), "pattern": str(r.pattern),
                "first_fraud_txn_id": _num_id(r.first_fraud_txn_id),
                "exposure": float(r.exposure_usd) if pd.notna(r.exposure_usd) else 0.0,
                "n_txns": int(r.n_txns) if pd.notna(r.n_txns) else 0,
                "actions": "" if pd.isna(r.actions_taken) else str(r.actions_taken),
                "report_filed": str(r.report_filed).strip().lower() == "yes",
                "notes": "" if pd.isna(r.analyst_notes) else str(r.analyst_notes),
                "txn_ids": txn_ids,
            }
            self.cc_on.setdefault(str(r.card_id), []).append(cid)
            for c in _split_ids(r.connected_card_ids):
                self.cc_conn.setdefault(c, []).append(cid)

    # ---- persistence
    @property
    def _case_path(self) -> Path:
        return self.store_dir / "case_memory.json"

    @property
    def _emb_path(self) -> Path:
        return self.store_dir / "closed_case_embeddings.npz"

    @property
    def _doc_path(self) -> Path:
        return self.store_dir / "doc_chunks.json"

    def _load_memory(self) -> None:
        if not self.persist:
            return
        if self._case_path.exists():
            self.cases = json.loads(self._case_path.read_text()).get("cases", {})
        if self._emb_path.exists():
            z = np.load(self._emb_path, allow_pickle=False)
            self.cc_emb = {str(i): row.tolist() for i, row in zip(z["ids"], z["emb"])}
        if self._doc_path.exists():
            self.docs = {c["id"]: c for c in json.loads(self._doc_path.read_text())}

    def _save(self, what: str) -> None:
        if not self.persist:
            return
        self.store_dir.mkdir(parents=True, exist_ok=True)
        if what == "cases":
            self._case_path.write_text(json.dumps({"cases": self.cases}, default=str))
        elif what == "emb" and self.cc_emb:
            dims = {len(v) for v in self.cc_emb.values()}
            if len(dims) == 1:
                ids = np.array(list(self.cc_emb), dtype=str)
                np.savez_compressed(self._emb_path, ids=ids, emb=np.array(list(self.cc_emb.values()), dtype=float))
        elif what == "docs":
            self._doc_path.write_text(json.dumps(list(self.docs.values())))

    # ---- helpers
    def _window(self, idx: np.ndarray | None, t_from: str | None, t_to: str | None) -> np.ndarray:
        if idx is None or len(idx) == 0:
            return np.array([], dtype=int)
        ts = self.ts_ns[idx]
        m = np.ones(len(idx), dtype=bool)
        if t_from:
            m &= ts >= pd.Timestamp(t_from).value
        if t_to:
            m &= ts <= pd.Timestamp(t_to).value
        return idx[m]

    def _rows(self, idx: np.ndarray) -> pd.DataFrame:
        return self.tx.iloc[idx]

    def _txn_dicts(self, idx: np.ndarray) -> list[dict]:
        sub = self._rows(idx).copy()
        sub["ts"] = sub["ts_dt"].dt.strftime(DT_FMT)
        return sub[TXN_FIELDS].to_dict("records")

    def _per_card(self, sub: pd.DataFrame, extra: dict | None = None) -> list[dict]:
        out = []
        for cid, g in sub.groupby("card_id", sort=False):
            info = self.card_info.get(cid, {})
            d = {"card_id": cid, "customer_id": info.get("customer_id", ""), "n": int(len(g)),
                 "amt": round(float(g["amt"].sum()), 2), "max_p": float(g["model_p"].max()),
                 "txn_ids": sorted(g["txn_id"]),
                 "first_ts": g["ts_dt"].min().strftime(DT_FMT), "last_ts": g["ts_dt"].max().strftime(DT_FMT)}
            if extra:
                for key, fn in extra.items():
                    d[key] = fn(g)
            out.append(d)
        out.sort(key=lambda d: (-d["max_p"], -d["n"], d["card_id"]))
        return out

    # ---- queries
    def _card_window(self, card_id, t_from, t_to):
        idx = self._window(self.by_card.get(card_id), t_from, t_to)
        return {"card": self.card_info.get(card_id), "txns": self._txn_dicts(idx)}

    def _card_baseline(self, card_id, t_before):
        idx = self.by_card.get(card_id)
        if idx is not None:
            idx = idx[self.ts_ns[idx] < pd.Timestamp(t_before).value]
        sub = self._rows(idx if idx is not None else np.array([], dtype=int))
        n = len(sub)

        def counts(col):
            return {str(k): int(v) for k, v in sub[col].value_counts().items()}

        return {"card_id": card_id, "t_before": t_before, "total": n,
                "online": int((sub["channel"] == "online").sum()),
                "by_addr1": counts("addr1"), "by_product": counts("product"), "by_device": counts("device_id"),
                "by_p_email": counts("p_email"), "by_r_email": counts("r_email"),
                "amt_avg": round(float(sub["amt"].mean()), 2) if n else 0.0,
                "amt_max": float(sub["amt"].max()) if n else 0.0,
                "first_ts": sub["ts_dt"].min().strftime(DT_FMT) if n else "",
                "last_ts": sub["ts_dt"].max().strftime(DT_FMT) if n else ""}

    def _closed_touching(self, cards: Iterable[str]) -> list[dict]:
        hits: dict[tuple, dict] = {}
        for c in cards:
            for rel, index in (("on_card", self.cc_on), ("connected", self.cc_conn)):
                for cid in index.get(c, []):
                    cc = self.cc[cid]
                    h = hits.setdefault((cid, rel), {"case_id": cid, "relation": rel, "outcome": cc["outcome"],
                                                     "pattern": cc["pattern"], "card_id": cc["card_id"],
                                                     "exposure": cc["exposure"], "via_cards": []})
                    h["via_cards"].append(c)
        return sorted(hits.values(), key=lambda h: (h["case_id"], h["relation"]))

    def _device_neighbors(self, device_id, t_from, t_to):
        idx = self._window(self.by_device.get(device_id), t_from, t_to)
        sub = self._rows(idx)
        uniq = lambda col: (lambda g: sorted(set(g[col]) - {""}))  # noqa: E731
        cards = self._per_card(sub, {"proxies": uniq("proxy"), "statuses": uniq("device_status")})
        dev = {"device_id": device_id, "label": self.device_label[device_id]} if device_id in self.device_label else None
        return {"device": dev, "n_txns": int(len(sub)), "n_cards": len(cards), "cards": cards,
                "closed_cases": self._closed_touching(c["card_id"] for c in cards)}

    def _amount_band_scan(self, t_from, t_to, lo, hi):
        d = self._rows(self._window(np.arange(len(self.ts_ns)), t_from, t_to))
        d = d[(d["channel"] == "online") & (d["amt"] >= lo) & (d["amt"] < hi)]
        rows = [{"txn_id": r.txn_id, "card_id": r.card_id, "ts": r.ts_dt.strftime(DT_FMT), "amt": float(r.amt),
                 "device_id": r.device_id or "", "model_p": float(r.model_p)} for r in d.itertuples()]
        return {"rows": sorted(rows, key=lambda r: (r["card_id"], r["ts"]))}

    def _region_activity(self, addr1, t_from, t_to, min_p):
        idx = self._window(self.by_addr1.get(str(addr1)), t_from, t_to)
        sub = self._rows(idx)
        hot = sub[sub["model_p"] >= min_p]
        return {"addr1": str(addr1), "n_all": int(len(sub)), "n_cards_all": int(sub["card_id"].nunique()),
                "cards": self._per_card(hot)}

    def _email_neighbors(self, domain, t_from, t_to, min_p):
        ip = self._window(self.by_p_email.get(domain), t_from, t_to)
        ir = self._window(self.by_r_email.get(domain), t_from, t_to)
        idx = np.union1d(ip, ir)
        sub = self._rows(idx)
        sub = sub[sub["model_p"] >= min_p]

        def roles(g):
            r = set()
            if (g["p_email"] == domain).any():
                r.add("purchaser")
            if (g["r_email"] == domain).any():
                r.add("recipient")
            return sorted(r)

        return {"domain": domain, "cards": self._per_card(sub, {"roles": roles})}

    def _ic_view(self, cid: str, relation: str) -> dict:
        rec = self.cases[cid]["record"]
        a = _case_vertex_attrs(rec, None)
        links = _case_links(rec)
        return {"case_id": cid, "relation": relation, "case_ref": a["case_ref"], "status": a["status"],
                "verdict": a["verdict"], "probability": a["probability"], "pattern": a["pattern"],
                "exposure": a["exposure"], "summary": a["summary"], "opened_at": a["opened_at"],
                "updated_at": a["updated_at"], "record": rec, "txn_ids": links["txns"]}

    def _card_cases(self, card_id):
        closed = []
        for rel, index in (("on_card", self.cc_on), ("connected", self.cc_conn)):
            for cid in index.get(card_id, []):
                closed.append({**self.cc[cid], "relation": rel})
        inv = []
        for cid, entry in self.cases.items():
            links = _case_links(entry["record"])
            if card_id in links["cards"]:
                inv.append(self._ic_view(cid, "on_card"))
            if card_id in links["connected"]:
                inv.append(self._ic_view(cid, "connected"))
        return {"card_id": card_id, "closed_cases": closed, "investigation_cases": inv}

    def _similar_cases(self, qvec, k, pattern_filter):
        items = []
        for cid, emb in self.cc_emb.items():
            cc = self.cc.get(cid)
            if cc and (not pattern_filter or cc["pattern"] == pattern_filter):
                items.append(({"case_id": cid, "kind": "closed", "outcome": cc["outcome"], "pattern": cc["pattern"],
                               "exposure": cc["exposure"], "notes": cc["notes"]}, emb))
        for cid, entry in self.cases.items():
            rec, emb = entry["record"], entry.get("embedding")
            if emb and (not pattern_filter or str(rec.get("pattern") or "") == pattern_filter):
                items.append(({"case_id": cid, "kind": "investigation", "outcome": str(rec.get("verdict") or ""),
                               "pattern": str(rec.get("pattern") or ""), "exposure": float(rec.get("exposure") or 0.0),
                               "notes": str(rec.get("summary") or "")}, emb))
        q = np.asarray(qvec, dtype=float)
        items = [it for it in items if len(it[1]) == q.size]
        mat = np.array([it[1] for it in items], dtype=float) if items else np.zeros((0, q.size))
        return {"results": [{**items[i][0], "score": round(s, 6)} for i, s in _top_cosine(q, mat, k)]}

    def _doc_search(self, qvec, k):
        q = np.asarray(qvec, dtype=float)
        docs = [d for d in self.docs.values() if len(d["embedding"]) == q.size]
        mat = np.array([d["embedding"] for d in docs], dtype=float) if docs else np.zeros((0, q.size))
        return {"results": [{"chunk_id": docs[i]["id"], "source": docs[i]["source"], "section": docs[i]["section"],
                             "text": docs[i]["text"], "score": round(s, 6)} for i, s in _top_cosine(q, mat, k)]}

    def _device_ring(self, t_from, t_to, min_cards):
        lo, hi = pd.Timestamp(t_from).value, pd.Timestamp(t_to).value
        m = (self.ts_ns >= lo) & (self.ts_ns <= hi)
        sub = self.tx[m & (self.tx["device_id"] != "").to_numpy()]
        out = []
        for dev, g in sub.groupby("device_id", sort=False):
            cards = sorted(set(g["card_id"]))
            if len(cards) < min_cards:
                continue
            out.append({"device_id": dev, "label": self.device_label.get(dev, ""), "n_cards": len(cards),
                        "n_txns": int(len(g)), "amt": round(float(g["amt"].sum()), 2),
                        "max_p": float(g["model_p"].max()), "avg_p": round(float(g["model_p"].mean()), 6),
                        "n_anon_proxy": int((g["proxy"] == ANON_PROXY).sum()),
                        "n_new_status": int((g["device_status"] == "New").sum()), "cards": cards})
        out.sort(key=lambda d: (-d["n_txns"], d["device_id"]))
        return {"devices": out[:200]}

    def _card_community(self, card_id, t_from, t_to, max_hops, max_device_txns):
        card_hop, dev_hop = {card_id: 0}, {}
        links, skipped, seen_dev = set(), set(), set()
        frontier, rounds = [card_id] if card_id in self.by_card else [], 0
        while frontier and rounds < max_hops:
            rounds += 1
            idx = np.concatenate([self._window(self.by_card.get(c), t_from, t_to) for c in frontier])
            sub = self._rows(idx)
            sub = sub[sub["device_id"] != ""]
            new_devs = []
            for c, d in sub[["card_id", "device_id"]].drop_duplicates().itertuples(index=False):
                if d in seen_dev:
                    continue
                links.add((c, d))
                new_devs.append(d)
            new_devs = sorted(set(new_devs))
            seen_dev.update(new_devs)
            expand = []
            for d in new_devs:
                if self.device_degree.get(d, 0) > max_device_txns:
                    skipped.add(d)
                else:
                    dev_hop[d] = rounds
                    expand.append(d)
            nxt = set()
            if expand:
                idx2 = np.concatenate([self._window(self.by_device.get(d), t_from, t_to) for d in expand])
                for c, d in self._rows(idx2)[["card_id", "device_id"]].drop_duplicates().itertuples(index=False):
                    if c in card_hop:
                        continue
                    links.add((c, d))
                    nxt.add(c)
            for c in nxt:
                card_hop[c] = rounds
            frontier = sorted(nxt)
        return {"seed": card_id, "cards": card_hop, "devices": dev_hop,
                "links": sorted([list(x) for x in links]), "skipped_devices": sorted(skipped), "rounds": rounds}

    # ---- writes
    def _write_case(self, record, embedding):
        cid = _record_case_id(record)
        prev = self.cases.get(cid, {})
        self.cases[cid] = {"record": record, "embedding": embedding if embedding is not None else prev.get("embedding")}
        self._save("cases")
        links = _case_links(record)
        return {"case_id": cid, "edges": sum(len(v) for v in links.values())}

    def _upsert_cc_emb(self, emb):
        self.cc_emb.update(emb)
        self._save("emb")
        return {"n": len(emb)}

    def _upsert_docs(self, chunks):
        for c in chunks:
            self.docs[c["id"]] = c
        self._save("docs")
        return {"n": len(chunks)}


def __getattr__(name: str):
    """TigerGraphStore lives in tg_store.py; `from fraudagent.graphstore import TigerGraphStore` works."""
    if name == "TigerGraphStore":
        from .tg_store import TigerGraphStore
        return TigerGraphStore
    raise AttributeError(name)


def open_store(kind: str = "local", **kw) -> GraphStore:
    """kind: "local", "tigergraph" (auto transport), "mcp" or "pytg"."""
    from .tg_store import TigerGraphStore

    if kind == "local":
        return LocalGraphStore(**kw)
    if kind in ("tigergraph", "tg"):
        return TigerGraphStore(transport="auto", **kw)
    return TigerGraphStore(transport=kind, **kw)
