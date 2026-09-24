"""The investigation agent.

A fixed state machine over the policy's investigation flow:

  trigger -> open case -> gather evidence (graph) -> retrieve memory (GraphRAG)
  -> assess -> recommend (initial) -> [request evidence -> simulate reply
  -> reassess -> recommend (final)] -> explain -> write case to graph

Graph analysis, probability and policy decisions are deterministic, so every
recommendation traces back to a query result and a rule number. The LLM reads
the assembled evidence packet (graph facts, retrieved policy text, similar past
cases) and writes the analyst summary, the SAR narrative and a critique; its
critique is recorded in the trace but cannot override policy.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from . import assess as A
from . import policy as P
from .embed import embed
from .llm import LLM
from .signals import Evidence, expand, extract, gather

FMT = "%Y-%m-%d %H:%M:%S"


class Investigation:
    def __init__(self, store, case_row: dict, flag_ts: str, llm: LLM | None = None, on_step=None):
        self.store = store
        self.llm = llm or LLM()
        self.row = case_row
        self.case = dict(case_id=case_row["case_id"], card_id=case_row["card_id"], customer_id=case_row["customer_id"],
                         flagged_txn_id=str(case_row["flagged_txn_id"]), trigger_type=case_row["trigger_type"],
                         trigger_text=case_row["trigger_text"], opened_at=case_row["opened_at"], flag_ts=flag_ts)
        self.steps: list[dict] = []
        self.on_step = on_step
        self.graph_case_id = "CASE-" + case_row["case_id"]

    # ------------------------------------------------------------------ trace
    def log(self, kind: str, text: str, **data) -> None:
        step = {"n": len(self.steps) + 1, "kind": kind, "text": text, "t": round(time.time() - self.t0, 2),
                "tool_calls": self.store.tool_calls, **data}
        self.steps.append(step)
        if self.on_step:
            self.on_step(step)

    # ------------------------------------------------------------------ memory
    def retrieve(self, ev: Evidence) -> tuple[list[dict], list[dict]]:
        f = ev.flagged
        q = (f"{f['channel']} {f['product']} ${f['amt']:.0f} "
             + " ".join(s.detail for s in ev.signals if s.fired and s.name != "closed_case_model"))
        res = self.store.similar_cases(embed(q), k=12)["results"]
        closed = [r for r in res if r.get("kind", "closed") == "closed"][:5]
        mine = [r for r in res if r.get("kind") == "investigation" and r["case_id"] != self.graph_case_id][:2]
        docs = self.store.doc_search(embed(q + " " + self.case["trigger_type"]), k=4)["results"]
        return closed + mine, docs

    # ------------------------------------------------------------------ decide
    def recommend(self, ev: Evidence, prob: float, groups: list[str], pat: str, stage: str,
                  response: str | None = None) -> tuple[list[dict], str]:
        f = ev.flagged
        epi = ev.episode
        exp = A.exposure(epi)
        shared = (A.fired(ev, "shared_device") or A.fired(ev, "structuring_ring")) and bool(ev.connected_cards)
        undocumented = pat == "undocumented"
        trig = self.case["trigger_type"]
        acts: list[dict] = []
        recurring = A.fired(ev, "recurring_charge")

        if response == "confirmed":
            acts.append(P.act("CLOSE_NO_FRAUD", 0, "R3: cardholder confirmed the transaction; confirmation noted in the case file"))
            if trig != "customer_report":
                acts.append(P.act("ALLOW_TRANSACTION", 0, "R3: confirmed legitimate"))
            acts.append(P.act("CREATE_CASE", 0, "3a: a case is opened whenever evidence is requested or a charge is disputed; closed as legitimate"))
            if recurring:
                acts.append(P.act("WARN_CUSTOMER", 0, "R7: remind the cardholder of the recurring charge they disputed"))
            return P.ordered(acts), "legitimate"

        if recurring and trig == "customer_report" and response is None:
            acts += [P.act("CREATE_CASE", 0, "R7 and 3a: disputed charge matches the cardholder's own recurring pattern"),
                     P.act("VERIFY_WITH_CUSTOMER", 0, "R7: confirm the recurring merchant with the cardholder; do not block"),
                     P.act("WARN_CUSTOMER", 0, "R7: recurring charge reminder")]
            return P.ordered(acts), "uncertain"

        if response == "no_reply":
            acts += [P.act("MONITOR_CARD", exp, "R4: no reply within 24 hours; raise monitoring for 72h"),
                     P.act("DECLINE_TRANSACTION", exp, "R4: decline pending authorizations while unverified"),
                     P.act("CREATE_CASE", exp, "3a: evidence was requested; case stays open")]
            if exp > 500 or A.fired(ev, "shared_device"):
                acts.append(P.act("ESCALATE_TO_ANALYST", exp, "R4 and R8: no reply, uncertain verdict, exposure above $500 or conflicting network evidence"))
            return P.ordered(acts), "uncertain"

        denial = trig == "customer_report" and not recurring
        fraud = response == "denied" or denial or (prob >= 0.85 and len(groups) >= 2)
        legit = response is None and prob <= 0.15 and len(groups) >= 2

        if fraud:
            why = "R2: cardholder denied the transaction" if response == "denied" or trig == "customer_report" else \
                f"6 and R1: probability {prob:.2f} >= 0.85 on {len(groups)} independent evidence groups ({', '.join(groups)}), so no verification is needed before blocking"
            if pat == "card_testing":
                acts.append(P.act("DECLINE_TRANSACTION", exp, "R5: card-testing sequence; decline pending authorizations"))
                big = max(x["amt"] for x in epi)
                if big <= 100:
                    acts.append(P.act("STEP_UP_AUTH", exp, "R5: no purchase over $100 has cleared, require step-up"))
            acts.append(P.act("BLOCK_CARD", exp, f"{why}; exposure ${exp:,.2f} {'<= $2,500 (L1)' if exp <= 2500 else '> $2,500 (L2)'}"))
            acts.append(P.act("CREATE_CASE", exp, "R2 and 3a: fraud case with evidence attached, written to the graph"))
            sar, sar_why = P.sar_required("fraud", max(prob, 0.85), exp, shared, undocumented)
            if sar:
                acts.append(P.act("FILE_REPORT", exp, sar_why))
            if shared:
                what = (f"share device profile {ev.connected_devices[0]}" if A.fired(ev, "shared_device") else
                        "show the same just-under-threshold purchase shape in the same window")
                acts.append(P.act("MONITOR_CONNECTED_CARDS", exp, f"R6: {len(ev.connected_cards)} other cards {what}"))
            if undocumented:
                acts.append(P.act("ESCALATE_TO_ANALYST", exp, "R9: undocumented, coordinated pattern described in the case"))
            return P.ordered(acts), "fraud"

        if legit:
            acts.append(P.act("CLOSE_NO_FRAUD", 0, f"R3 and 6: probability {prob:.2f} <= 0.15 on {len(groups)} independent groups ({', '.join(groups)})"))
            if trig == "customer_report":
                acts.append(P.act("CREATE_CASE", 0, "3a: every customer dispute opens a case, closed as legitimate"))
            else:
                acts.append(P.act("ALLOW_TRANSACTION", 0, "6: evidence does not support fraud; let the flagged transaction stand"))
            return P.ordered(acts), "legitimate"

        # uncertain: gather more evidence before any block (R1), open a case (3a)
        acts.append(P.act("CREATE_CASE", exp, f"3a: probability {prob:.2f} and evidence is being requested"))
        online_new = f["channel"] == "online" and (A.fired(ev, "new_device") or A.fired(ev, "proxy"))
        if trig == "customer_report":
            acts.append(P.act("VERIFY_WITH_CUSTOMER", exp, "R1: dispute rests on the customer statement; confirm details (merchant, device) before blocking"))
            if online_new:
                acts.append(P.act("STEP_UP_AUTH", exp, "R1: new device or proxy on the disputed purchase; hold further online activity behind OTP"))
        else:
            if online_new:
                acts.append(P.act("STEP_UP_AUTH", exp, f"R1: probability {prob:.2f} < 0.70 with a device signal; confirm the cardholder via OTP before any block"))
            acts.append(P.act("VERIFY_WITH_CUSTOMER", exp, f"R1: probability {prob:.2f}; verify before blocking"))
            if prob >= 0.5:
                acts.append(P.act("MONITOR_CARD", exp, "R1: raise monitoring for 72h while verification is pending"))
        if (exp > 500 and prob >= 0.3) or A.fired(ev, "shared_device"):
            acts.append(P.act("ESCALATE_TO_ANALYST", exp, "R8: uncertain verdict with exposure above $500 or conflicting network evidence"))
        return P.ordered(acts), "uncertain"

    def simulate_response(self, ev: Evidence, prob: float) -> tuple[str, str, str]:
        """Customer and step-up replies are not in the dataset (policy section 5). The simulator answers the way the
        graph evidence implies: a denial when the assessed probability is at least 0.5, a confirmation otherwise."""
        f = ev.flagged
        trig = self.case["trigger_type"]
        kind = "customer_validation"
        if trig != "customer_report" and f["channel"] == "online" and (A.fired(ev, "new_device") or A.fired(ev, "proxy")):
            kind = "step_up_auth"
        if A.fired(ev, "recurring_charge"):
            return kind, "confirmed", (f"Shown the {len([1 for _ in ev.recurring])} earlier ${f['amt']:.2f} charges, the cardholder recognises "
                                       "the recurring merchant and withdraws the dispute")
        exp = A.exposure(ev.episode)
        if trig != "customer_report" and 0.3 <= prob < 0.6 and (exp > 500 or A.fired(ev, "proxy") and not A.fired(ev, "shared_device")):
            return kind, "no_reply", ("No reply within 24 hours; " + ("the one-time passcode was never entered" if kind == "step_up_auth"
                                                                       else "the cardholder did not answer the verification request"))
        if prob >= 0.5:
            txt = ("Cardholder states they did not make the purchase and still holds the card" if kind == "customer_validation" else
                   "Step-up challenge failed: the one-time passcode was not completed and the cardholder denies the activity")
            return kind, "denied", txt
        if trig == "customer_report":
            return kind, "confirmed", ("Shown the merchant, device and time, the cardholder recognises the purchase "
                                       "(made by a household member / forgotten order) and withdraws the dispute")
        txt = ("Cardholder confirms the purchase" if kind == "customer_validation" else
               "Step-up challenge passed: cardholder completed the one-time passcode and confirms the purchase")
        return kind, "confirmed", txt

    # ------------------------------------------------------------------ explain
    def explain(self, ev: Evidence, record: dict, sim: list[dict], docs: list[dict]) -> None:
        c = record["case"]
        if not self.llm.available:
            self.log("explain", "No LLM key configured: deterministic template used for summary and SAR narrative")
            return
        packet = {
            "alert": {k: self.case[k] for k in ("case_id", "trigger_type", "trigger_text", "card_id", "customer_id")},
            "flagged_txn": {k: ev.flagged.get(k) for k in ("txn_id", "ts", "amt", "product", "channel", "addr1", "device_profile", "device_status", "proxy", "risk_score", "model_p")},
            "evidence": c["evidence"], "verdict": c["verdict"], "probability": c["fraud_probability"], "pattern": c["pattern"],
            "affected_txn_ids": c["affected_txn_ids"], "exposure_usd": c["exposure_usd"], "connected_cards": c["connected_card_ids"],
            "similar_prior_cases": [{k: s.get(k) for k in ("case_id", "outcome", "pattern", "notes")} for s in sim[:4]],
            "policy_passages": [{"section": d["section"], "text": d["text"][:500]} for d in docs[:4]],
            "next_best_actions": record["next_best_actions"], "evidence_requests": record["evidence_requests"],
            "sar_required": record["sar"]["file"],
        }
        system = ("You are a senior card-fraud investigator at a bank. You receive graph evidence already computed by "
                  "TigerGraph queries, retrieved policy passages and similar closed cases. Do not invent IDs, amounts or dates: use only "
                  "values in the packet. Actions with route L1 or L2 are recommendations awaiting human approval: write 'recommended, pending L1 "
                  "(team lead) approval' or 'pending L2 (fraud manager) approval', never that they were carried out. Customer and "
                  "step-up replies in evidence_requests are simulated assumptions; say 'the assumed reply' when you mention them. "
                  "Plain, factual analyst English, no em dashes. Return JSON with keys: summary (2 to 5 "
                  "sentences), sar_narrative (6 to 12 sentences covering who, what, when, where, how, why suspicious; empty string if "
                  "sar_required is false), critique (one or two sentences: anything in the evidence that conflicts with the verdict), "
                  "pattern_description (2 to 3 sentences, only if pattern is undocumented, else empty).")
        out = self.llm.complete_json(system, json.dumps(packet, default=str), purpose="explain")
        if not out:
            self.log("explain", "LLM unavailable or quota exhausted: deterministic template kept")
            return
        if out.get("summary"):
            c["summary"] = out["summary"].strip()
        if record["sar"]["file"] and len(out.get("sar_narrative", "")) > 200:
            record["sar"]["narrative"] = out["sar_narrative"].strip()
        if c["pattern"] == "undocumented" and len(out.get("pattern_description", "")) > 80:
            c["pattern_description"] = out["pattern_description"].strip()
        self.log("explain", "LLM synthesised summary and narrative from the evidence packet",
                 critique=out.get("critique", ""))

    # ------------------------------------------------------------------ run
    def run(self) -> dict:
        self.t0 = time.time()
        calls0 = self.store.tool_calls
        tok0 = self.llm.tokens
        cs = self.case
        self.log("trigger", f"{cs['trigger_type']} alert on {cs['card_id']}: {cs['trigger_text']}")
        self.store.write_case({"case_id": self.graph_case_id, "case_ref": cs["case_id"], "status": "open", "verdict": "uncertain",
                               "probability": 0.0, "pattern": "none", "exposure": 0.0, "summary": cs["trigger_text"],
                               "opened_at": cs["opened_at"], "updated_at": cs["opened_at"], "card_id": cs["card_id"],
                               "txn_ids": [cs["flagged_txn_id"]]}, embed(cs["trigger_text"]))
        self.log("case", f"Opened case {self.graph_case_id} in the graph (status open)", status="open")

        ev = gather(self.store, cs, self.log)
        extract(ev)
        expand(self.store, ev, self.log)
        for s in ev.signals:
            if s.fired or s.name in ("closed_case_model", "shared_device"):
                self.log("signal", s.detail, signal=s.name, fired=s.fired, strength=s.strength)

        sim, docs = self.retrieve(ev)
        own = [c["case_id"] for c in ev.prior_cases.get("closed_cases", [])]
        self.log("memory", f"Retrieved {len(sim)} similar cases by vector search and {len(own)} closed cases on this card; "
                           f"{len(docs)} policy passages", similar=[s["case_id"] for s in sim[:5]],
                 docs=[d["section"] for d in docs])

        prob, groups = A.probability(ev)
        pat, pat_desc = A.pattern(ev)
        denial = cs["trigger_type"] == "customer_report" and not A.fired(ev, "recurring_charge")
        if denial:
            before = prob
            prob = round(min(0.98, 1 - (1 - prob) * 0.35), 2)
            groups = sorted(set(groups) | {"customer"})
            self.log("assess", f"Cardholder's denial is direct evidence (R2): probability {before:.2f} -> {prob:.2f}", probability=prob)
        self.log("assess", f"Fraud probability {prob:.2f}; independent groups: {', '.join(groups) or 'none'}; "
                           f"leading pattern {pat}", probability=prob)
        initial, verdict = self.recommend(ev, prob, groups, pat, "initial")
        self.log("recommend", "Initial next best action: " + ", ".join(f"{a['action']} ({a['route']})" for a in initial),
                 actions=initial, stage="initial")

        requests, final, response = [], initial, None
        stop, stop_reason = P.should_stop(prob, len(groups), False)
        if verdict == "uncertain":
            kind, response, assumed = self.simulate_response(ev, prob)
            requests.append({"type": kind, "asked_after_step": len(self.steps), "assumed_response": assumed})
            self.log("request", f"Requested {kind}; simulated reply: {assumed}", request=kind)
            before = prob
            prob = 0.92 if response == "denied" else (0.05 if response == "confirmed" else prob)
            if response == "no_reply":
                stop_note = "No reply within 24 hours (R4): card monitored, pending authorizations declined, case escalated; the verdict stays uncertain."
            if response == "denied":
                groups = sorted(set(groups) | {"customer"})
            self.log("assess", f"Probability {before:.2f} -> {prob:.2f} after the reply", probability=prob)
            final, verdict = self.recommend(ev, prob, groups, pat, "final", response=response)
            self.log("recommend", "Final next best action: " + ", ".join(f"{a['action']} ({a['route']})" for a in final),
                     actions=final, stage="final")
            stop_reason = (stop_note if response == "no_reply" else
                           f"Verification reply ({response}) settles the question; further graph steps would not change the actions.")
        elif denial:
            stop_reason = ("The cardholder's denial settles the verdict under R2 and the graph evidence has been collected; "
                           "further steps would not change the actions.")
        else:
            stop_reason = stop_reason[0].upper() + stop_reason[1:] + "; no evidence request is needed and further steps would not change the decision."

        if verdict == "legitimate":
            affected, pat_out, pat_desc = [], "none", ""
        else:
            affected = [x["txn_id"] for x in ev.episode]
            pat_out = pat
        epi = [x for x in ev.episode if x["txn_id"] in affected]
        exp = A.exposure(epi)
        status = {"fraud": "closed_fraud", "legitimate": "closed_legitimate"}.get(verdict, "escalated")
        if verdict == "uncertain" and not any(a["action"] == "ESCALATE_TO_ANALYST" for a in final):
            status = "open"
        sar_file = any(a["action"] == "FILE_REPORT" for a in final)
        connected = ev.connected_cards if verdict != "legitimate" else []
        devices = ev.connected_devices if verdict != "legitimate" else []

        evidence = []
        for s in ev.signals:
            if s.fired or s.name in ("closed_case_model", "shared_device", "card_case_history"):
                evidence.append({"claim": s.detail, "source": "graph" if not s.ref.startswith(("model", "trigger")) else
                                 ("customer" if s.ref.startswith("trigger") else "graph"),
                                 "ref": s.ref, "entity_ids": [e for e in s.entity_ids if e][:25]})
        sim_closed = [s for s in sim if s.get("kind", "closed") == "closed"]
        sim_mine = [s for s in sim if s.get("kind") == "investigation"]
        if sim_closed:
            evidence.append({"claim": "Most similar closed cases by vector search: " + "; ".join(
                f"{s['case_id']} ({s['outcome']}, {s['pattern']})" for s in sim_closed[:3])
                + ("; earlier investigations this run: " + ", ".join(f"{s['case_id']} ({s['outcome']}, {s['pattern']})" for s in sim_mine) if sim_mine else ""),
                "source": "graph", "ref": "query:similar_cases(k=12)", "entity_ids": [s["case_id"] for s in sim_closed[:3]]})
        for d in docs[:2]:
            evidence.append({"claim": f"Policy retrieved: {d['section']}", "source": "document", "ref": f"doc:{d['source']}#{d['section']}", "entity_ids": []})
        for i, r in enumerate(requests, 1):
            evidence.append({"claim": r["assumed_response"], "source": "customer", "ref": f"evidence_request:{i}", "entity_ids": []})

        similar_ids = list(dict.fromkeys([c for c in own if c][-3:] + [s["case_id"] for s in sim[:3] if s.get("kind", "closed") == "closed"]))
        f = ev.flagged
        summary = self._template_summary(ev, verdict, prob, pat_out, exp, final, response)
        what_changed = "nothing"
        if requests:
            what_changed = (f"The simulated {requests[0]['type']} reply ({response}) moved probability to {prob:.2f}. "
                            + ("Verification replaced by a block and a fraud case." if response == "denied" else
                               "Verification replaced by closing the alert as legitimate."))
        dates = sorted(x["ts"][:10] for x in epi)
        record = {
            "case_id": cs["case_id"],
            "case": {
                "status": status, "verdict": verdict, "fraud_probability": prob, "pattern": pat_out,
                "pattern_description": pat_desc if pat_out == "undocumented" else "",
                "affected_txn_ids": affected, "first_suspicious_txn_id": affected[0] if affected else "",
                "connected_card_ids": connected, "connected_device_profiles": devices, "exposure_usd": exp,
                "evidence": evidence, "similar_prior_cases": similar_ids, "summary": summary,
                "written_to_graph": True, "graph_case_id": self.graph_case_id,
            },
            "evidence_requests": requests,
            "next_best_actions": {"initial": initial, "final": final, "what_changed": what_changed},
            "sar": {"file": sar_file,
                    "reason": next((a["reason"] for a in final if a["action"] == "FILE_REPORT"),
                                   P.sar_required(verdict, prob, exp, bool(connected), pat_out == "undocumented")[1]),
                    "narrative": self._template_sar(ev, epi, exp, pat_out, connected, devices, response) if sar_file else "",
                    "subjects": ([cs["customer_id"], cs["card_id"]] + connected + ([f["device_id"]] if f.get("device_id") and devices else [])) if sar_file else [],
                    "total_amount_usd": exp if sar_file else 0,
                    "activity_dates": [dates[0], dates[-1]] if sar_file and dates else []},
            "stop_reason": stop_reason,
        }
        self.explain(ev, record, sim, docs)
        self.store.write_case({"case_id": self.graph_case_id, "case_ref": cs["case_id"], "status": status, "verdict": verdict,
                               "probability": prob, "pattern": pat_out, "exposure": exp, "summary": record["case"]["summary"],
                               "opened_at": cs["opened_at"], "updated_at": cs["opened_at"], "card_id": cs["card_id"],
                               "txn_ids": affected or [cs["flagged_txn_id"]], "connected_card_ids": connected,
                               "device_ids": [f["device_id"]] if devices and f.get("device_id") else [],
                               "similar_cases": [{"case_id": s["case_id"], "score": s["score"]} for s in sim[:3] if s.get("kind", "closed") == "closed"],
                               "record": record}, embed(record["case"]["summary"] + " " + pat_out))
        self.log("case", f"Case {self.graph_case_id} updated in the graph: {status}, {verdict}, pattern {pat_out}", status=status)
        record["tool_calls"] = self.store.tool_calls - calls0
        record["tokens"] = self.llm.tokens - tok0
        record["latency_s"] = round(time.time() - self.t0, 2)
        record["_trace"] = self.steps
        return record

    # ------------------------------------------------------------------ templates
    def _template_summary(self, ev, verdict, prob, pat, exp, final, response) -> str:
        f = ev.flagged
        fired = [s.detail.split(";")[0] for s in ev.signals if s.fired and s.strength > 0 and s.name != "closed_case_model"][:2]
        neg = [s.detail.split(";")[0] for s in ev.signals if s.strength < 0][:1]
        head = (f"{self.case['trigger_type'].replace('_', ' ').capitalize()} alert on {self.case['card_id']} for "
                f"${f['amt']:.2f} ({f['channel']}, product {f['product']}) on {f['ts'][:10]}. ")
        body = (f"Model score from closed cases {f['model_p']:.2f}. " + (" ".join(s.rstrip('.') + "." for s in fired) + " " if fired else "")
                + (" ".join(s.rstrip('.') + "." for s in neg) + " " if neg and verdict != "fraud" else ""))
        tail = {"fraud": f"Concluded {pat.replace('_', ' ')} with probability {prob:.2f}; exposure ${exp:,.2f}.",
                "legitimate": f"Concluded legitimate (probability {prob:.2f})" + (" after the cardholder confirmed." if response == "confirmed" else "."),
                "uncertain": f"Verdict uncertain at {prob:.2f}; routed to an analyst."}[verdict]
        return head + body + tail

    def _template_sar(self, ev, epi, exp, pat, connected, devices, response) -> str:
        cs, f = self.case, ev.flagged
        d0, d1 = epi[0]["ts"], epi[-1]["ts"]
        lines = [f"Between {d0} and {d1}, card {cs['card_id']} held by customer {cs['customer_id']} was used for {len(epi)} "
                 f"{'online' if f['channel'] == 'online' else 'card-present'} transaction(s) totaling ${exp:,.2f} "
                 f"({', '.join(x['txn_id'] + ' $' + format(x['amt'], '.2f') for x in epi[:6])}).",
                 f"The activity was identified from a {cs['trigger_type'].replace('_', ' ')} alert on transaction {cs['flagged_txn_id']} opened {cs['opened_at']}."]
        if f.get("device_profile"):
            lines.append(f"The flagged transaction came from device profile '{f['device_profile']}'"
                         + (f", marked {f['device_status']} for this account" if f.get("device_status") else "")
                         + (f", behind {f['proxy']}" if f.get("proxy") else "") + ".")
        if f.get("addr1"):
            lines.append(f"Billing region code {f['addr1']}; channel {f['channel']}; product code {f['product']}.")
        for s in ev.signals:
            if s.fired and s.strength > 0 and s.name not in ("closed_case_model", "customer_dispute", "card_case_history",
                                                              "structuring_ring", "shared_device", "amount_product_unusual"):
                lines.append(s.detail.rstrip(".") + ".")
        if connected and pat == "undocumented" and A.fired(ev, "structuring_ring"):
            lines.append(f"The same just-under-threshold shape appears on {len(connected)} other cards in the same four weeks "
                         f"(${ev.ring_total:,.2f} in total), including {', '.join(connected[:8])}, which indicates one actor working many cards.")
        elif connected:
            lines.append(f"The same device profile links this activity to {len(connected)} other cards: {', '.join(connected[:12])}"
                         + (" and others." if len(connected) > 12 else "."))
        if response == "denied" or cs["trigger_type"] == "customer_report":
            lines.append("The cardholder stated they did not authorise the transactions.")
        lines.append({"undocumented": "The activity does not match a documented typology and appears coordinated, which is why it is reported.",
                      "card_testing": "The sequence is consistent with testing a stolen card number before use."}.get(
            pat, f"The activity is consistent with {pat.replace('_', ' ')}."))
        lines.append(f"Total suspicious amount ${exp:,.2f}. The card has been recommended for blocking and reissue"
                     + ("; connected cards are under monitoring." if connected else "."))
        return " ".join(lines)
