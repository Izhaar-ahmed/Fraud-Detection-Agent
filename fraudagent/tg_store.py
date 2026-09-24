"""TigerGraphStore: GraphStore backed by the installed FraudGraph queries.

Transports: "mcp" spawns the tigergraph-mcp server and calls
tigergraph__run_installed_query / add_node / add_nodes / add_edges;
"pytg" calls pyTigerGraph directly; "auto" prefers mcp. Results are
normalised to the LocalGraphStore shapes documented in graphstore.py.
"""
from __future__ import annotations

import json
from typing import Iterable

import numpy as np

from .graph_load import tg_settings
from .graphstore import (_NUM_TXN, TXN_FIELDS, GraphStore, _case_links, _case_vertex_attrs, _epoch_to_str,
                         _record_case_id, _top_cosine)



def _norm_attrs(attrs: dict) -> dict:
    """'cc_on.@via_cards' / '@via_cards' / 'id' -> 'via_cards' / 'id'."""
    return {k.split(".")[-1].lstrip("@"): v for k, v in attrs.items()}


def _vertex_list(result: list, key: str) -> list[dict]:
    for block in result or []:
        if isinstance(block, dict) and key in block:
            rows = []
            for v in block[key] or []:
                a = _norm_attrs(v.get("attributes", {}))
                a.setdefault("id", v.get("v_id"))
                rows.append(a)
            return rows
    return []


def _value(result: list, key: str, default=None):
    for block in result or []:
        if isinstance(block, dict) and key in block:
            return block[key]
    return default


def _clean_set(v) -> list[str]:
    return sorted({str(x) for x in (v or []) if str(x)})


class TigerGraphStore(GraphStore):
    """Installed-query backend. transport: "mcp", "pytg" or "auto" (mcp if available)."""

    def __init__(self, transport: str = "auto", settings: dict | None = None, mcp_command: str = "tigergraph-mcp"):
        super().__init__()
        self.settings = settings or tg_settings()
        self._mcp = None
        self._conn = None
        self._emb_cache: dict | None = None
        self._vector_fallback = False
        if transport in ("mcp", "auto"):
            try:
                from .tg_mcp import McpClient
                env = {"TG_HOST": self.settings["host"], "TG_SECRET": self.settings["secret"],
                       "TG_GRAPHNAME": self.settings["graph"], "TG_TGCLOUD": "true"}
                self._mcp = McpClient(env, command=mcp_command)
            except Exception:
                if transport == "mcp":
                    raise
        self.transport = "mcp" if self._mcp else "pytg"
        self.backend = f"tigergraph-{self.transport}"

    # ---- transport
    def _pytg(self):
        if self._conn is None:
            from .graph_load import connect
            self._conn = connect(self.settings, quiet=True)
        return self._conn

    def run(self, query: str, params: dict) -> list:
        if self._mcp:
            data = self._mcp.call("tigergraph__run_installed_query", {"query_name": query, "params": params})
            return data.get("result") or []
        return self._pytg().runInstalledQuery(query, params)

    def _upsert_vertices(self, vtype: str, rows: list[tuple[str, dict]], chunk: int = 200) -> int:
        n = 0
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            if self._mcp:
                self._mcp.call("tigergraph__add_nodes", {"vertex_type": vtype, "vertex_id": "id",
                                                          "vertices": [{"id": vid, **a} for vid, a in part]})
            else:
                self._pytg().upsertVertices(vtype, part)
            n += len(part)
        return n

    def _upsert_edges(self, src_t: str, etype: str, tgt_t: str, rows: list[tuple[str, str, dict]]) -> int:
        if not rows:
            return 0
        if self._mcp:
            self._mcp.call("tigergraph__add_edges", {"edge_type": etype, "edges": [
                {"source_type": src_t, "source_id": s, "target_type": tgt_t, "target_id": t, **a}
                for s, t, a in rows]})
        else:
            self._pytg().upsertEdges(src_t, etype, tgt_t, rows)
        return len(rows)

    def close(self) -> None:
        if self._mcp:
            self._mcp.close()
            self._mcp = None

    # ---- queries
    @staticmethod
    def _txn(a: dict) -> dict:
        d = {f: a.get(f, "") for f in TXN_FIELDS}
        d["proxy"] = a.get("ip_proxy", a.get("proxy", ""))  # PROXY is a GSQL reserved word, so the attribute is ip_proxy
        d["txn_id"] = str(a.get("id", ""))
        for f in _NUM_TXN:
            d[f] = float(d[f] or 0.0)
        return d

    @staticmethod
    def _card_row(a: dict, extra: Iterable[str] = ()) -> dict:
        d = {"card_id": a.get("id"), "customer_id": a.get("customer_id", ""), "n": int(a.get("n", 0)),
             "amt": round(float(a.get("amt", 0.0)), 2), "max_p": float(a.get("max_p", 0.0)),
             "txn_ids": sorted(str(x) for x in a.get("txn_ids", []))}
        if "first_epoch" in a:
            d["first_ts"], d["last_ts"] = _epoch_to_str(a["first_epoch"]), _epoch_to_str(a["last_epoch"])
        for key in extra:
            d[key] = _clean_set(a.get(key))
        return d

    @staticmethod
    def _sort_cards(cards: list[dict]) -> list[dict]:
        return sorted(cards, key=lambda d: (-d["max_p"], -d["n"], d["card_id"]))

    def _card_window(self, card_id, t_from, t_to):
        r = self.run("card_window", {"card_id": card_id, "t_from": t_from, "t_to": t_to})
        cards = _vertex_list(r, "cards")
        card = None
        if cards:
            c = cards[0]
            card = {"card_id": c.get("id"), "customer_id": c.get("customer_id", ""),
                    "network": c.get("network", ""), "card_type": c.get("card_type", "")}
        txns = sorted((self._txn(a) for a in _vertex_list(r, "txns")), key=lambda d: (d["ts"], d["txn_id"]))
        return {"card": card, "txns": txns}

    def _card_baseline(self, card_id, t_before):
        r = self.run("card_baseline", {"card_id": card_id, "t_before": t_before})
        total = int(_value(r, "total", 0) or 0)
        m = lambda k: {str(a): int(b) for a, b in (_value(r, k, {}) or {}).items()}  # noqa: E731
        return {"card_id": card_id, "t_before": t_before, "total": total, "online": int(_value(r, "online", 0) or 0),
                "by_addr1": m("by_addr1"), "by_product": m("by_product"), "by_device": m("by_device"),
                "by_p_email": m("by_p_email"), "by_r_email": m("by_r_email"),
                "amt_avg": round(float(_value(r, "amt_sum", 0.0)) / total, 2) if total else 0.0,
                "amt_max": float(_value(r, "amt_max", 0.0)) if total else 0.0,
                "first_ts": _epoch_to_str(_value(r, "first_epoch")) if total else "",
                "last_ts": _epoch_to_str(_value(r, "last_epoch")) if total else ""}

    def _device_neighbors(self, device_id, t_from, t_to):
        r = self.run("device_neighbors", {"device_id": device_id, "t_from": t_from, "t_to": t_to})
        devs = _vertex_list(r, "devs")
        cards = self._sort_cards([self._card_row(a, ("proxies", "statuses")) for a in _vertex_list(r, "cards")])
        closed = []
        for key, rel in (("cc_on", "on_card"), ("cc_conn", "connected")):
            for a in _vertex_list(r, key):
                closed.append({"case_id": a.get("id"), "relation": rel, "outcome": a.get("outcome", ""),
                               "pattern": a.get("pattern", ""), "card_id": a.get("card_id", ""),
                               "exposure": float(a.get("exposure", 0.0)), "via_cards": _clean_set(a.get("via_cards"))})
        closed.sort(key=lambda h: (h["case_id"], h["relation"]))
        return {"device": {"device_id": devs[0]["id"], "label": devs[0].get("label", "")} if devs else None,
                "n_txns": sum(c["n"] for c in cards), "n_cards": len(cards), "cards": cards, "closed_cases": closed}

    def _amount_band_scan(self, t_from, t_to, lo, hi):
        r = self.run("amount_band_scan", {"t_from": t_from, "t_to": t_to, "lo": lo, "hi": hi})
        rows = [{"txn_id": str(x.get("txn_id", "")), "card_id": x.get("card_id", ""), "ts": _epoch_to_str(x.get("epoch")),
                 "amt": float(x.get("amt", 0.0)), "device_id": x.get("device_id", ""), "model_p": float(x.get("model_p", 0.0))}
                for x in (_value(r, "rows", []) or [])]
        return {"rows": sorted(rows, key=lambda r: (r["card_id"], r["ts"]))}

    def _region_activity(self, addr1, t_from, t_to, min_p):
        r = self.run("region_activity", {"region": addr1, "t_from": t_from, "t_to": t_to, "min_p": min_p})
        cards = self._sort_cards([self._card_row(a) for a in _vertex_list(r, "cards")])
        return {"addr1": addr1, "n_all": int(_value(r, "n_all", 0) or 0),
                "n_cards_all": int(_value(r, "n_cards_all", 0) or 0), "cards": cards}

    def _email_neighbors(self, domain, t_from, t_to, min_p):
        r = self.run("email_neighbors", {"dom": domain, "t_from": t_from, "t_to": t_to, "min_p": min_p})
        return {"domain": domain,
                "cards": self._sort_cards([self._card_row(a, ("roles",)) for a in _vertex_list(r, "cards")])}

    def _card_cases(self, card_id):
        r = self.run("card_cases", {"card_id": card_id})
        closed = []
        for key, rel in (("cc_on", "on_card"), ("cc_conn", "connected")):
            for a in _vertex_list(r, key):
                closed.append({"case_id": a.get("id"), "customer_id": a.get("customer_id", ""),
                               "card_id": a.get("card_id", ""), "opened_at": a.get("opened_at", ""),
                               "closed_at": a.get("closed_at", ""), "outcome": a.get("outcome", ""),
                               "pattern": a.get("pattern", ""), "first_fraud_txn_id": a.get("first_fraud_txn_id", ""),
                               "exposure": float(a.get("exposure", 0.0)), "n_txns": int(a.get("n_txns", 0)),
                               "actions": a.get("actions", ""), "report_filed": bool(a.get("report_filed", False)),
                               "notes": a.get("notes", ""), "txn_ids": sorted(_clean_set(a.get("txn_ids"))),
                               "relation": rel})
        inv = []
        for key, rel in (("ic_on", "on_card"), ("ic_conn", "connected")):
            for a in _vertex_list(r, key):
                try:
                    rec = json.loads(a.get("record") or "{}")
                except ValueError:
                    rec = {"raw": a.get("record")}
                inv.append({"case_id": a.get("id"), "relation": rel, "case_ref": a.get("case_ref", ""),
                            "status": a.get("status", ""), "verdict": a.get("verdict", ""),
                            "probability": float(a.get("probability", 0.0)), "pattern": a.get("pattern", ""),
                            "exposure": float(a.get("exposure", 0.0)), "summary": a.get("summary", ""),
                            "opened_at": a.get("opened_at", ""), "updated_at": a.get("updated_at", ""),
                            "record": rec, "txn_ids": sorted(_clean_set(a.get("txn_ids")))})
        return {"card_id": card_id, "closed_cases": closed, "investigation_cases": inv}

    def _embeddings(self, refresh: bool = False) -> dict:
        if self._emb_cache is None or refresh:
            r = self.run("embeddings_dump", {})
            self._emb_cache = {k: _vertex_list(r, k) for k in ("cc", "ic", "docs")}
        return self._emb_cache

    def _similar_cases(self, qvec, k, pattern_filter):
        if not self._vector_fallback:
            try:
                r = self.run("similar_cases", {"qvec": qvec, "k": k, "pattern_filter": pattern_filter})
                return {"results": [{"case_id": h.get("case_id"), "kind": h.get("kind"), "outcome": h.get("outcome"),
                                     "pattern": h.get("pattern"), "exposure": float(h.get("exposure", 0.0)),
                                     "notes": h.get("notes", ""), "score": round(float(h.get("score", 0.0)), 6)}
                                    for h in sorted(_value(r, "results", []) or [],
                                                    key=lambda h: -float(h.get("score", 0.0)))]}
            except Exception:
                self._vector_fallback = True
        e = self._embeddings(refresh=True)
        items = [({"case_id": a["id"], "kind": "closed", "outcome": a.get("outcome", ""), "pattern": a.get("pattern", ""),
                   "exposure": float(a.get("exposure", 0.0)), "notes": a.get("notes", "")}, a.get("embedding") or [])
                 for a in e["cc"]]
        items += [({"case_id": a["id"], "kind": "investigation", "outcome": a.get("verdict", ""),
                    "pattern": a.get("pattern", ""), "exposure": float(a.get("exposure", 0.0)),
                    "notes": a.get("summary", "")}, a.get("embedding") or []) for a in e["ic"]]
        items = [it for it in items if len(it[1]) == len(qvec)
                 and (not pattern_filter or it[0]["pattern"] == pattern_filter)]
        mat = np.array([it[1] for it in items], dtype=float) if items else np.zeros((0, len(qvec)))
        q = np.asarray(qvec, dtype=float)
        return {"results": [{**items[i][0], "score": round(s, 6)} for i, s in _top_cosine(q, mat, k)]}

    def _doc_search(self, qvec, k):
        if not self._vector_fallback:
            try:
                r = self.run("doc_search", {"qvec": qvec, "k": k})
                return {"results": [{"chunk_id": h.get("chunk_id"), "source": h.get("src", ""),
                                     "section": h.get("section", ""), "text": h.get("text", ""),
                                     "score": round(float(h.get("score", 0.0)), 6)}
                                    for h in sorted(_value(r, "results", []) or [],
                                                    key=lambda h: -float(h.get("score", 0.0)))]}
            except Exception:
                self._vector_fallback = True
        docs = [d for d in self._embeddings()["docs"] if len(d.get("embedding") or []) == len(qvec)]
        mat = np.array([d["embedding"] for d in docs], dtype=float) if docs else np.zeros((0, len(qvec)))
        q = np.asarray(qvec, dtype=float)
        return {"results": [{"chunk_id": docs[i]["id"], "source": docs[i].get("src", ""),
                             "section": docs[i].get("section", ""), "text": docs[i].get("text", ""),
                             "score": round(s, 6)} for i, s in _top_cosine(q, mat, k)]}

    def _device_ring(self, t_from, t_to, min_cards):
        r = self.run("device_ring", {"t_from": t_from, "t_to": t_to, "min_cards": min_cards})
        out = []
        for a in _vertex_list(r, "ring"):
            n = int(a.get("n_txns", 0))
            out.append({"device_id": a.get("id"), "label": a.get("label", ""), "n_cards": len(_clean_set(a.get("cards"))),
                        "n_txns": n, "amt": round(float(a.get("amt", 0.0)), 2), "max_p": float(a.get("max_p", 0.0)),
                        "avg_p": round(float(a.get("sum_p", 0.0)) / n, 6) if n else 0.0,
                        "n_anon_proxy": int(a.get("n_anon", 0)), "n_new_status": int(a.get("n_new", 0)),
                        "cards": _clean_set(a.get("cards"))})
        out.sort(key=lambda d: (-d["n_txns"], d["device_id"]))
        return {"devices": out}

    def _card_community(self, card_id, t_from, t_to, max_hops, max_device_txns):
        r = self.run("card_community", {"card_id": card_id, "t_from": t_from, "t_to": t_to,
                                        "max_hops": max_hops, "max_device_txns": max_device_txns})
        links = sorted([s.split("|", 1) for s in (_value(r, "links", []) or []) if "|" in s])
        skipped = set(_clean_set(_value(r, "skipped_devices", [])))
        # links to hub devices are kept, matching LocalGraphStore
        return {"seed": card_id, "cards": {str(k): int(v) for k, v in (_value(r, "cards", {}) or {}).items()},
                "devices": {str(k): int(v) for k, v in (_value(r, "devices", {}) or {}).items()},
                "links": links, "skipped_devices": sorted(skipped), "rounds": int(_value(r, "rounds", 0) or 0)}

    # ---- writes
    def _write_case(self, record, embedding):
        cid = _record_case_id(record)
        attrs = _case_vertex_attrs(record, embedding)
        if self._mcp:
            self._mcp.call("tigergraph__add_node", {"vertex_type": "InvestigationCase", "vertex_id": cid,
                                                     "attributes": attrs})
        else:
            self._pytg().upsertVertex("InvestigationCase", cid, attrs)
        links = _case_links(record)
        n = self._upsert_edges("InvestigationCase", "IC_INVOLVES", "Txn", [(cid, t, {}) for t in links["txns"]])
        n += self._upsert_edges("InvestigationCase", "IC_ON_CARD", "PayCard", [(cid, c, {}) for c in links["cards"]])
        n += self._upsert_edges("InvestigationCase", "IC_CONNECTED", "PayCard", [(cid, c, {}) for c in links["connected"]])
        n += self._upsert_edges("InvestigationCase", "IC_DEVICE", "DeviceProfile", [(cid, d, {}) for d in links["devices"]])
        n += self._upsert_edges("InvestigationCase", "IC_SIMILAR", "ClosedCase",
                                [(cid, s, {"score": sc}) for s, sc in links["similar"]])
        self._emb_cache = None
        return {"case_id": cid, "edges": n}

    def _upsert_cc_emb(self, emb):
        n = self._upsert_vertices("ClosedCase", [(cid, {"embedding": v}) for cid, v in emb.items()])
        self._emb_cache = None
        return {"n": n}

    def _upsert_docs(self, chunks):
        rows = [(c["id"], {"src": c["source"], "section": c["section"], "text": c["text"],
                           "embedding": c["embedding"]}) for c in chunks]
        n = self._upsert_vertices("DocChunk", rows, chunk=100)
        self._emb_cache = None
        return {"n": n}
