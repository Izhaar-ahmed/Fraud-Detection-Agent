"""LocalGraphStore tests on the derived parquet data, plus parsers used by TigerGraphStore.

Run from the repo root: python -m pytest -q tests/test_graphstore.py
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from fraudagent import graph_load as gl
from fraudagent.graphstore import LocalGraphStore
from fraudagent.tg_store import TigerGraphStore, _vertex_list, _value
from fraudagent.tg_mcp import parse_tool_text

NOV = ("2016-11-01 00:00:00", "2016-11-30 23:59:59")
DEVICE_3478561 = "DPbd0e6b7c4f"  # SM-G935F, anonymous proxy


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return LocalGraphStore(store_dir=tmp_path_factory.mktemp("store"))


def test_card_window_is_json_and_sorted(store):
    r = store.card_window("C04570-K1", "2016-11-11 00:00:00", "2016-11-11 23:59:59")
    json.dumps(r)
    assert r["card"]["customer_id"] == "C04570"
    ts = [t["ts"] for t in r["txns"]]
    assert len(ts) == 3 and ts == sorted(ts)
    assert {t["device_id"] for t in r["txns"]} == {"DPcaf8508d25"}
    assert all(t["addr1"] == "204" for t in r["txns"])


def test_card_window_unknown_card(store):
    assert store.card_window("NOPE", *NOV) == {"card": None, "txns": []}


def test_card_baseline_counts(store):
    b = store.card_baseline("C04570-K1", "2016-11-11 00:00:00")
    json.dumps(b)
    assert b["total"] == sum(b["by_product"].values()) == sum(b["by_addr1"].values()) > 0
    assert 0 <= b["online"] <= b["total"]
    assert b["last_ts"] < "2016-11-11 00:00:00" and b["first_ts"] <= b["last_ts"]
    assert b["amt_max"] >= b["amt_avg"] > 0
    empty = store.card_baseline("C04570-K1", "2000-01-01 00:00:00")
    assert empty["total"] == 0 and empty["first_ts"] == ""


def test_device_neighbors_many_cards(store):
    r = store.device_neighbors(DEVICE_3478561, *NOV)
    json.dumps(r)
    assert r["device"]["label"].startswith("SM-G935F")
    assert r["n_cards"] >= 20 and r["n_txns"] == sum(c["n"] for c in r["cards"])
    assert any("IP_PROXY:ANONYMOUS" in c["proxies"] for c in r["cards"])
    assert all("" not in c["proxies"] for c in r["cards"])
    cards = {c["card_id"] for c in r["cards"]}
    assert "C13487-K1" in cards
    assert all(set(h["via_cards"]) <= cards for h in r["closed_cases"])


def test_region_and_email(store):
    r = store.region_activity("204", *NOV, min_p=0.5)
    assert r["n_all"] >= sum(c["n"] for c in r["cards"]) and r["cards"]
    assert all(c["max_p"] >= 0.5 for c in r["cards"])
    e = store.email_neighbors("gmail.com", "2016-11-01 00:00:00", "2016-11-02 00:00:00", min_p=0.8)
    assert e["cards"] and all(set(c["roles"]) <= {"purchaser", "recipient"} for c in e["cards"])


def test_card_cases_closed(store):
    r = store.card_cases("C04570-K1")
    ids = [c["case_id"] for c in r["closed_cases"]]
    assert "CC-1383" in ids
    c = r["closed_cases"][ids.index("CC-1383")]
    assert c["relation"] == "on_card" and isinstance(c["report_filed"], bool) and c["txn_ids"]


def test_device_ring(store):
    r = store.device_ring(*NOV, min_cards=5)
    assert r["devices"] and len(r["devices"]) <= 200
    assert all(d["n_cards"] >= 5 and d["n_cards"] == len(d["cards"]) for d in r["devices"])
    assert any(d["device_id"] == DEVICE_3478561 for d in r["devices"])


def test_card_community_bounded(store):
    r1 = store.card_community("C13487-K1", *NOV, max_hops=1)
    r2 = store.card_community("C13487-K1", *NOV, max_hops=2)
    assert r1["cards"]["C13487-K1"] == 0 and DEVICE_3478561 in r1["devices"]
    assert len(r2["cards"]) >= len(r1["cards"]) > 1
    assert max(r2["cards"].values()) <= 2 and r2["rounds"] <= 2
    assert not set(r2["skipped_devices"]) & set(r2["devices"])
    assert store.card_community("C13487-K1", *NOV, max_hops=99)["rounds"] <= 4


def test_vectors_and_case_memory(store, tmp_path):
    rng = np.random.default_rng(0)
    ids = ["CC-0001", "CC-0002", "CC-1383"]
    emb = {cid: rng.normal(size=8).tolist() for cid in ids}
    store.upsert_closed_case_embeddings(emb)
    hits = store.similar_cases(emb["CC-0002"], k=2)["results"]
    assert hits[0]["case_id"] == "CC-0002" and hits[0]["score"] == pytest.approx(1.0)
    only = store.similar_cases(emb["CC-0002"], k=5, pattern_filter="none")["results"]
    assert [h["case_id"] for h in only] == ["CC-1383"]

    rec = {"case_id": "IC-TEST-1", "case_ref": "CASE-1", "status": "open", "verdict": "likely_fraud",
           "probability": 0.9, "pattern": "account_takeover", "exposure": 250.0, "summary": "test",
           "card_id": "C04570-K1", "txn_ids": ["3450436"], "connected_card_ids": ["C13487-K1"],
           "device_ids": ["DPcaf8508d25"], "similar_cases": [{"case_id": "CC-1383", "score": 0.8}]}
    w = store.write_case(rec, emb["CC-0001"])
    assert w == {"case_id": "IC-TEST-1", "edges": 5}
    cc = store.card_cases("C13487-K1")["investigation_cases"]
    assert cc and cc[0]["relation"] == "connected" and cc[0]["record"]["case_ref"] == "CASE-1"
    top = store.similar_cases(emb["CC-0001"], k=2)["results"]
    assert {h["case_id"] for h in top} == {"CC-0001", "IC-TEST-1"}

    store.upsert_doc_chunks([{"id": "D1", "source": "policy", "section": "s1", "text": "a", "embedding": [1, 0]},
                             {"id": "D2", "source": "policy", "section": "s2", "text": "b", "embedding": [0, 1]}])
    assert store.doc_search([0.1, 0.9], k=1)["results"][0]["chunk_id"] == "D2"

    reopened = LocalGraphStore(store_dir=store.store_dir)
    assert "IC-TEST-1" in reopened.cases and set(ids) <= set(reopened.cc_emb) and "D1" in reopened.docs


def test_trace(store):
    n = store.tool_calls
    store.similar_cases([0.0] * 64, k=3)
    assert store.tool_calls == n + 1
    t = store.trace[-1]
    assert t["name"] == "similar_cases" and t["params"]["qvec"] == "<vector len=64>" and "ms" in t


# ---- TigerGraph parsing (no network)

def test_parse_mcp_reply():
    from tigergraph_mcp.response_formatter import format_success

    result = [{"cards": [{"v_id": "C1", "v_type": "Card", "attributes": {"id": "C1", "@n": 2}}]}]
    text = format_success("run_installed_query", "ok", data={"query_name": "q", "result": result})[0].text
    env = parse_tool_text(text)
    assert env["success"] and env["data"]["result"] == result


def test_tg_result_normalisation():
    r = [{"cc_on": [{"v_id": "CC-1", "v_type": "ClosedCase",
                     "attributes": {"cc_on.id": "CC-1", "cc_on.outcome": "cleared", "cc_on.@via_cards": ["C1"]}}]},
         {"total": 3, "by_addr1": {"204": 3}}]
    assert _vertex_list(r, "cc_on") == [{"id": "CC-1", "outcome": "cleared", "via_cards": ["C1"]}]
    assert _value(r, "total") == 3 and _value(r, "missing", 0) == 0


def test_tg_store_shapes_with_stub(store):
    """TigerGraphStore turns GSQL-shaped output into the LocalGraphStore shape."""
    tg = TigerGraphStore.__new__(TigerGraphStore)
    tg.tool_calls, tg.trace, tg.backend = 0, [], "stub"
    tg._vector_fallback, tg._emb_cache = False, None
    local = store.device_neighbors(DEVICE_3478561, *NOV)

    def fake_run(query, params):
        assert query == "device_neighbors" and params["device_id"] == DEVICE_3478561
        cards = [{"v_id": c["card_id"], "v_type": "Card", "attributes": {
            "id": c["card_id"], "customer_id": c["customer_id"], "@n": c["n"], "@amt": c["amt"],
            "@max_p": c["max_p"], "@proxies": c["proxies"] + [""], "@statuses": c["statuses"],
            "@txn_ids": c["txn_ids"], "@first_epoch": _epoch(c["first_ts"]), "@last_epoch": _epoch(c["last_ts"])}}
            for c in local["cards"]]
        return [{"devs": [{"v_id": DEVICE_3478561, "attributes": {"id": DEVICE_3478561,
                                                                 "label": local["device"]["label"]}}]},
                {"cards": cards}, {"cc_on": []}, {"cc_conn": []}]

    tg.run = fake_run
    out = tg.device_neighbors(DEVICE_3478561, *NOV)
    assert out["cards"] == local["cards"] and out["device"] == local["device"]
    assert set(out) == set(local)


def _epoch(ts: str) -> int:
    from datetime import datetime, timezone
    return int(datetime.strptime(ts, gl.DT_FMT).replace(tzinfo=timezone.utc).timestamp())


# ---- loader payloads (no network)

def test_loader_payloads_small():
    tx = gl.read_txn().head(2000)
    cc = gl.read_closed_cases().head(50)
    v = {k: b() for k, b in gl.vertex_payloads(tx, cc).items()}
    json.dumps(v["Txn"][:10]), json.dumps(v["ClosedCase"][:10])
    assert v["Txn"][0][1]["ts"].count(":") == 2 and isinstance(v["ClosedCase"][0][1]["report_filed"], bool)
    e = {k: (s, t, b()) for k, (s, t, b) in gl.edge_payloads(tx, cc).items()}
    assert all(a["gap_s"] >= 0 for _, _, a in e["NEXT"][2])
    assert len(e["MADE"][2]) == len(tx)


class _FakeConn:
    def __init__(self):
        self.calls = []

    def upsertVertices(self, vtype, rows):
        self.calls.append((vtype, len(rows)))
        return len(rows)


def test_uploader_chunks(monkeypatch):
    fake = _FakeConn()
    monkeypatch.setattr(gl, "connect", lambda *a, **k: fake)
    up = gl.Uploader({"host": "h", "secret": "s", "graph": "g"}, workers=2, chunk=3)
    assert up.vertices("Customer", [(str(i), {}) for i in range(10)]) == 10
    assert sorted(n for _, n in fake.calls) == [1, 3, 3, 3]
