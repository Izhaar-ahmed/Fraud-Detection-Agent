"""Turn signals into a calibrated fraud probability, a pattern and an episode."""
from __future__ import annotations

import math

from .signals import Evidence

KNOWN = ["card_testing", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use",
         "account_takeover", "undocumented", "none"]


def _logit(p: float) -> float:
    p = min(max(p, 0.03), 0.97)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def probability(ev: Evidence) -> tuple[float, list[str]]:
    """Model score as the prior, fired signals add log-odds. Returns (p, independent groups supporting the verdict)."""
    p0 = ev.flagged.get("model_p", 0.0)
    z = _logit(p0) + sum(s.strength for s in ev.signals if s.name != "closed_case_model")
    p = round(min(max(_sig(z), 0.02), 0.98), 2)
    fraud_side = p >= 0.5
    groups = {s.independent_group for s in ev.signals if s.fired and s.strength > 0 and s.name != "closed_case_model"}
    legit_groups = {s.independent_group for s in ev.signals if s.strength < 0}
    if p0 >= 0.8:
        groups.add("model")
    if p0 <= 0.2:
        legit_groups.add("model")
    if not any(s.fired and s.strength > 0 for s in ev.signals if s.name not in ("closed_case_model", "card_case_history")):
        legit_groups.add("no_pattern")
    return p, sorted(groups if fraud_side else legit_groups)


def sig(ev: Evidence, name: str):
    return next((s for s in ev.signals if s.name == name), None)


def fired(ev: Evidence, name: str) -> bool:
    s = sig(ev, name)
    return bool(s and s.fired)


def pattern(ev: Evidence) -> tuple[str, str]:
    f = ev.flagged
    if fired(ev, "threshold_structuring"):
        s = sig(ev, "threshold_structuring")
        ring = sig(ev, "structuring_ring")
        return "undocumented", (
            f"Threshold structuring: {s.detail} on card {ev.case['card_id']}, sized to stay under an authorization limit. "
            + (f"The same shape hits {len(ev.connected_cards)} other cards in the same four weeks, so this is one coordinated "
               "operation across many cardholders rather than a single stolen card. " if ring and ring.fired else "")
            + "Found by scanning the card window for clustered just-under-threshold online amounts, then running the same "
              "amount-band scan across every card in the graph.")
    shared = sig(ev, "shared_device")
    if shared and shared.fired and fired(ev, "proxy") and len(ev.connected_cards) >= 3:
        return "undocumented", (
            f"Device-sharing ring: one device profile ({ev.connected_devices[0] if ev.connected_devices else f.get('device_profile')}) "
            f"behind an anonymising proxy was used on {len(ev.connected_cards) + 1} unrelated cards in two weeks, a coordinated "
            "operation rather than a single stolen number. Found by traversing Txn-DeviceProfile-Txn-Card edges from the flagged "
            "transaction and filtering on proxy and model risk.")
    if fired(ev, "card_testing"):
        return "card_testing", ""
    if f["channel"] == "in_person":
        if fired(ev, "out_of_region"):
            return "out_of_region_use", ""
        by_addr = ev.baseline.get("by_addr1") or {}
        home_share = by_addr.get(f.get("addr1") or "", 0) / max(1, ev.baseline.get("total", 0) or 0)
        allowed = ("account_takeover",) if home_share >= 0.1 else ("out_of_region_use", "account_takeover")
        mem = [c["pattern"] for c in ev.prior_cases.get("closed_cases", []) if c["outcome"] == "confirmed_fraud"
               and c["pattern"] in allowed]
        if mem:
            return max(set(mem), key=mem.count), ""
        return ("out_of_region_use" if f.get("addr1") and home_share < 0.1 else "account_takeover"), ""
    if fired(ev, "account_takeover_markers"):
        return "account_takeover", ""
    if fired(ev, "new_device") or (shared and shared.fired):
        return "card_not_present_new_device", ""
    return "card_not_present_fraud", ""


def exposure(txns: list[dict]) -> float:
    return round(sum(abs(x["amt"]) for x in txns), 2)
