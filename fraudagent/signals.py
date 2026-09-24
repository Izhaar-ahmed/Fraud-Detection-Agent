"""Evidence gathering and signal extraction for one alert.

Every graph read goes through the GraphStore, so the same code runs against
TigerGraph (MCP or pyTigerGraph) and the local pandas store. Signals are
plain dicts: name, fired (bool), strength (log-odds contribution), detail,
entity_ids and the query that produced them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean

FMT = "%Y-%m-%d %H:%M:%S"
HUB_DEVICE_CARDS = 60          # device profiles shared by more cards than this are generic (e.g. "Windows | chrome 66")
SUSPICIOUS_PROXIES = ("IP_PROXY:ANONYMOUS", "IP_PROXY:HIDDEN")
THRESHOLDS = (100.0, 250.0, 500.0, 1000.0, 2000.0)


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:19], FMT)


def _fmt(d: datetime) -> str:
    return d.strftime(FMT)


@dataclass
class Signal:
    name: str
    fired: bool
    strength: float
    detail: str
    entity_ids: list = field(default_factory=list)
    ref: str = ""
    independent_group: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Evidence:
    case: dict
    flagged: dict
    card: dict | None
    baseline: dict
    window: list
    prior_cases: dict
    device: dict | None = None
    region: dict | None = None
    email: dict | None = None
    community: dict | None = None
    recurring: list = field(default_factory=list)
    ring_total: float = 0.0
    signals: list = field(default_factory=list)
    episode: list = field(default_factory=list)
    connected_cards: list = field(default_factory=list)
    connected_devices: list = field(default_factory=list)


def gather(store, case: dict, log) -> Evidence:
    """Pull the card, its history, the alert window and its graph neighbourhood."""
    t = _dt(case["flag_ts"])
    card_id = case["card_id"]
    log("gather", f"Card history and 72h window around flagged transaction {case['flagged_txn_id']}")
    win = store.card_window(card_id, _fmt(t - timedelta(days=3)), _fmt(t + timedelta(days=3)))
    txns = win["txns"]
    flagged = next((x for x in txns if x["txn_id"] == str(case["flagged_txn_id"])), None)
    if flagged is None:
        wide = store.card_window(card_id, _fmt(t - timedelta(days=30)), _fmt(t + timedelta(days=30)))
        flagged = next(x for x in wide["txns"] if x["txn_id"] == str(case["flagged_txn_id"]))
    ft = _dt(flagged["ts"])
    base = store.card_baseline(card_id, flagged["ts"])
    prior = store.card_cases(card_id)
    ev = Evidence(case=case, flagged=flagged, card=win.get("card"), baseline=base, window=txns, prior_cases=prior)
    if case["trigger_type"] == "customer_report":
        log("gather", "120-day card history to test for a recurring charge at the disputed amount (R7)")
        hist = store.card_window(card_id, _fmt(ft - timedelta(days=120)), _fmt(ft - timedelta(seconds=1)))["txns"]
        ev.recurring = [x for x in hist if abs(x["amt"] - flagged["amt"]) <= 0.011 and x["product"] == flagged["product"]]
    if flagged.get("device_id"):
        log("gather", f"Device profile neighbourhood for {flagged['device_id']} over 14 days")
        ev.device = store.device_neighbors(flagged["device_id"], _fmt(ft - timedelta(days=7)), _fmt(ft + timedelta(days=7)))
    if flagged.get("channel") == "in_person" and flagged.get("addr1"):
        log("gather", f"Billing region {flagged['addr1']} activity over 10 days")
        ev.region = store.region_activity(flagged["addr1"], _fmt(ft - timedelta(days=5)), _fmt(ft + timedelta(days=5)), 0.5)
    if flagged.get("r_email") and flagged["r_email"] not in ("gmail.com", "yahoo.com", "hotmail.com", "anonymous.com"):
        ev.email = store.email_neighbors(flagged["r_email"], _fmt(ft - timedelta(days=3)), _fmt(ft + timedelta(days=3)), 0.7)
    return ev


# ---------------------------------------------------------------- signals

GENERIC_DEVICE_INFO = {"?", "Windows", "iOS Device", "MacOS", "Trident/7.0", "Linux", "rv:11.0"}


def specific_profile(label: str) -> bool:
    """A device profile identifies a device only when the model or both OS and screen are known."""
    parts = [p.strip() for p in (label or "").split("|")]
    if len(parts) < 4:
        return False
    info, os_, _, screen = parts[:4]
    return (info not in GENERIC_DEVICE_INFO and not info.startswith("rv:")) or (os_ != "?" and screen != "?")


def _near(a: dict, b: dict, hours: float) -> bool:
    return abs((_dt(a["ts"]) - _dt(b["ts"])).total_seconds()) <= hours * 3600


def extract(ev: Evidence) -> None:
    f, base, W = ev.flagged, ev.baseline, ev.window
    S = ev.signals
    card = ev.case["card_id"]
    prior_n = base.get("total", 0) or 0
    avg = base.get("amt_avg") or 0.0
    ref_win = f"query:card_window(card_id={card}, +-72h)"
    ref_base = f"query:card_baseline(card_id={card}, before={f['ts'][:10]})"

    # 1. transaction-level model trained on closed cases
    p = f.get("model_p", 0.0)
    S.append(Signal("closed_case_model", True, 0.0,
                    f"Fraud model trained on 5,565 closed cases scores the flagged transaction {p:.2f} "
                    f"(bank risk_score {f.get('risk_score', 0):.2f})", [f["txn_id"]], "model:closed_case_lgbm", "model"))

    online = [x for x in W if x["channel"] == "online"]
    # 2. card testing: >=3 small online auths within 1h followed by a larger purchase
    small = [x for x in online if x["amt"] < 10]
    ct = []
    for big in online:
        if big["amt"] < 20:
            continue
        pre = [s for s in small if 0 <= (_dt(big["ts"]) - _dt(s["ts"])).total_seconds() <= 3600 * 2
               and ((s.get("device_id") and s.get("device_id") == big.get("device_id"))
                    or (s.get("p_email") and s.get("p_email") == big.get("p_email") and s.get("device_profile") == big.get("device_profile")))]
        if len(pre) >= 3:
            burst = [s for s in pre if (_dt(pre[-1]["ts"]) - _dt(s["ts"])).total_seconds() <= 3600]
            if len(burst) >= 3:
                ct = burst + [big]
                break
    S.append(Signal("card_testing", bool(ct), 3.0 if ct else 0.0,
                    f"{len(ct) - 1} online authorizations under $10 within an hour, then ${ct[-1]['amt']:.2f}" if ct else
                    "No run of small online authorizations before a larger purchase", [x["txn_id"] for x in ct], ref_win, "sequence"))

    # 3. threshold structuring: >=3 online purchases within 60 minutes just under a round threshold
    struct = []
    for T in THRESHOLDS:
        cand = [x for x in online if 0.88 * T <= x["amt"] < T and _near(x, f, 2)]
        cand.sort(key=lambda x: x["ts"])
        for i in range(len(cand)):
            grp = [y for y in cand[i:] if (_dt(y["ts"]) - _dt(cand[i]["ts"])).total_seconds() <= 3600]
            if len(grp) >= 3 and any(y["txn_id"] == f["txn_id"] for y in grp) and len(grp) > len(struct):
                struct = grp
                thr = T
    S.append(Signal("threshold_structuring", bool(struct), 3.5 if struct else 0.0,
                    f"{len(struct)} online purchases within an hour, each just under ${thr:,.0f} "
                    f"(${min(x['amt'] for x in struct):.2f} to ${max(x['amt'] for x in struct):.2f})" if struct else
                    "No cluster of just-under-threshold amounts", [x["txn_id"] for x in struct], ref_win, "sequence"))

    # 4. repeated near-identical online amounts in a short window (automation)
    rep = [x for x in online if abs(x["amt"] - f["amt"]) <= 0.02 * max(f["amt"], 1) and _near(x, f, 3)]
    fired = f["channel"] == "online" and len(rep) >= 3 and not struct
    S.append(Signal("repeated_amounts", fired, 1.0 if fired else 0.0,
                    f"{len(rep)} online charges within 3 hours at about ${f['amt']:.2f}" if fired else "No repeated same-amount charges",
                    [x["txn_id"] for x in rep] if fired else [], ref_win, "sequence"))

    # 5. new device / proxy
    dev_seen = (base.get("by_device") or {}).get(f.get("device_id") or "", 0)
    new_dev = bool(f.get("device_id")) and dev_seen == 0 and f.get("device_status") == "New"
    S.append(Signal("new_device", new_dev, 0.8 if new_dev else (-0.4 if dev_seen >= 3 else 0.0),
                    f"Device {f.get('device_profile')} marked New and never seen on this card before" if new_dev else
                    (f"Device seen {dev_seen} times on this card before" if f.get("device_id") else "In-person, no device record"),
                    [f.get("device_id")] if f.get("device_id") else [], ref_base, "device"))
    prox = f.get("proxy") in SUSPICIOUS_PROXIES
    S.append(Signal("proxy", prox, 0.7 if prox else 0.0,
                    f"Connection behind {f.get('proxy')}" if prox else "No anonymising proxy", [f["txn_id"]] if prox else [], ref_win, "device"))

    # 6. amount / product out of pattern for the card
    by_prod = base.get("by_product") or {}
    odd_amt = prior_n >= 5 and avg > 0 and f["amt"] > 3 * avg and f["amt"] > 150
    odd_prod = prior_n >= 5 and by_prod.get(f["product"], 0) == 0
    S.append(Signal("amount_product_unusual", odd_amt or odd_prod, 0.4 * odd_amt + 0.4 * odd_prod,
                    "; ".join(filter(None, [f"amount ${f['amt']:.2f} is {f['amt'] / avg:.1f}x the card average ${avg:.2f}" if odd_amt else "",
                                            f"product code {f['product']} never used on this card" if odd_prod else ""])) or
                    "Amount and product consistent with card history", [f["txn_id"]], ref_base, "behaviour"))

    # 7. out-of-region card-present use while home activity continues
    reg = f.get("addr1") or ""
    by_addr = base.get("by_addr1") or {}
    if f["channel"] == "in_person" and reg and prior_n >= 5:
        seen = by_addr.get(reg, 0)
        home = max(by_addr, key=by_addr.get) if by_addr else ""
        same_reg = [x for x in W if x.get("addr1") == reg and x["channel"] == "in_person"]
        days = {x["ts"][:10] for x in same_reg}
        home_active = [x for x in W if x.get("addr1") and x["addr1"] != reg and by_addr.get(x["addr1"], 0) >= 3 and _near(x, f, 24)]
        new_reg = seen == 0
        trip = new_reg and len(days) >= 3 and not home_active
        s = 1.2 if new_reg and home_active else (-1.2 if trip else (0.3 if new_reg else -0.3))
        S.append(Signal("out_of_region", new_reg and not trip, s,
                        (f"Card-present purchase in region {reg}, where the card has no prior history, while it was used in "
                         f"home regions {sorted({x['addr1'] for x in home_active})[:3]} within 24h") if new_reg and home_active else
                        (f"Purchases in new region {reg} on {len(days)} separate days with no home activity: consistent with travel" if trip else
                         (f"First purchase in region {reg}; no concurrent home activity" if new_reg else
                          f"Region {reg} used {seen} times before on this card")),
                        [f["txn_id"]] + [x["txn_id"] for x in home_active[:3]], ref_base, "region"))
        ev._home = home

    # 8. account takeover style: mixed channel + device/email change + match flags
    if prior_n >= 5:
        emails = base.get("by_p_email") or {}
        new_email = bool(f.get("p_email")) and emails.get(f["p_email"], 0) == 0
        mixed = len({x["channel"] for x in W if _near(x, f, 24)}) == 2
        m_anom = f.get("m4") == "M2"
        ato = new_email and mixed and (new_dev or m_anom)
        S.append(Signal("account_takeover_markers", ato, 1.0 if ato else 0.0,
                        f"New purchaser email {f['p_email']}, mixed-channel use within 24h, {'new device' if new_dev else 'match flag M4=M2'}" if ato else
                        "No credential-change markers", [f["txn_id"]] if ato else [], ref_base, "identity"))

    # 9. disputed but recurring (R7): same amount and product charged repeatedly across months
    rec = ev.recurring or []
    months = {x["ts"][:7] for x in rec}
    span = (_dt(f["ts"]) - _dt(rec[0]["ts"])).days if rec else 0
    is_rec = len(rec) >= 5 and len(months) >= 2 and span >= 45 and len(rec) <= span / 3
    S.append(Signal("recurring_charge", is_rec, -3.0 if is_rec else 0.0,
                    f"The disputed ${f['amt']:.2f} {f['product']} charge matches the card's own recurring charge: the same amount to the cent appears {len(rec)} times in the previous {span} days ({', '.join(sorted(months))})" if is_rec else
                    "No recurring charge at this amount", [x["txn_id"] for x in rec][-6:], f"query:card_window(card_id={card}, -120d)", "behaviour"))
    if ev.case["trigger_type"] == "customer_report":
        S.append(Signal("customer_dispute", True, 1.0,
                        f"Cardholder states they did not make the ${f['amt']:.2f} purchase", [f["txn_id"]], "trigger:customer_report", "customer"))

    # 10. shared device profile with other suspicious cards
    if ev.device and ev.device.get("device"):
        cards = [c for c in ev.device["cards"] if c["card_id"] != card]
        n_cards = ev.device.get("n_cards", 0)
        hub = n_cards > HUB_DEVICE_CARDS or not specific_profile(ev.device["device"]["label"])
        prox_cards = [c for c in cards if any(p in SUSPICIOUS_PROXIES for p in c.get("proxies", []))]
        bad = [c for c in cards if c["max_p"] >= 0.8]
        cc_fraud = [c for c in ev.device.get("closed_cases", []) if c["outcome"] == "confirmed_fraud" and c.get("card_id") != card]
        if prox:
            bad = [c for c in bad if c in prox_cards] or bad
        strong = (not hub and len(bad) >= 2) or (prox and len(prox_cards) >= 3)
        lab = ev.device["device"]["label"]
        detail = (f"Device profile '{lab}' used by {n_cards} cards in 14 days; {len(bad)} other cards have high-risk transactions on it"
                  + (f", {len(prox_cards)} behind an anonymising proxy" if prox_cards else "")
                  + (f"; {len(cc_fraud)} confirmed closed cases on those cards" if cc_fraud else ""))
        if hub:
            detail += " (generic or widely shared profile, so the link is weak)"
        S.append(Signal("shared_device", strong, 2.0 if strong else 0.0, detail,
                        [f["device_id"]] + [c["card_id"] for c in (prox_cards if prox and len(prox_cards) >= 3 else bad)][:25],
                        f"query:device_neighbors(device_id={f['device_id']}, +-7d)", "network"))
        if strong:
            ev.connected_cards = sorted({c["card_id"] for c in (prox_cards if prox and len(prox_cards) >= 3 else bad)})
            ev.connected_devices = [lab]

    # 11. prior outcomes on this card (case memory)
    cl = ev.prior_cases.get("closed_cases", [])
    conf = [c for c in cl if c["outcome"] == "confirmed_fraud"]
    clr = [c for c in cl if c["outcome"] == "cleared"]
    if cl:
        S.append(Signal("card_case_history", bool(conf), 0.0,
                        f"{len(conf)} confirmed and {len(clr)} cleared prior cases on this card"
                        + (f"; last cleared reason: {clr[-1]['notes'][:110]}" if clr else ""),
                        [c["case_id"] for c in cl][-6:], f"query:card_cases(card_id={card})", "memory"))

    # episode: flagged + linked suspicious transactions on the same card within 48h
    ep = {f["txn_id"]: f}
    for grp in (ct, struct, rep if fired else []):
        for x in grp:
            ep[x["txn_id"]] = x
    for x in W:
        if x["model_p"] >= 0.8 and _near(x, f, 48) and x["channel"] == f["channel"]:
            ep[x["txn_id"]] = x
    ev.episode = sorted(ep.values(), key=lambda x: x["ts"])


def expand(store, ev: Evidence, log) -> None:
    """Second hop for sequence patterns: look for the same shape on other cards (R6/R9)."""
    st = next((s for s in ev.signals if s.name == "threshold_structuring" and s.fired), None)
    if not st:
        return
    amts = [x["amt"] for x in ev.window if x["txn_id"] in st.entity_ids]
    thr = min(T for T in THRESHOLDS if T > max(amts))
    ft = _dt(ev.flagged["ts"])
    log("gather", f"Scanning all cards for online purchases between ${0.88 * thr:,.0f} and ${thr:,.0f} within 14 days (R9 ring check)")
    rows = store.amount_band_scan(_fmt(ft - timedelta(days=14)), _fmt(ft + timedelta(days=14)), 0.88 * thr, thr)["rows"]
    by_card: dict[str, list] = {}
    for r in rows:
        by_card.setdefault(r["card_id"], []).append(r)
    ring, total, n_tx = [], 0.0, 0
    for card, rs in by_card.items():
        if card == ev.case["card_id"]:
            continue
        rs.sort(key=lambda r: r["ts"])
        for i in range(len(rs)):
            grp = [y for y in rs[i:] if (_dt(y["ts"]) - _dt(rs[i]["ts"])).total_seconds() <= 3600]
            if len(grp) >= 3:
                ring.append(card)
                total += sum(y["amt"] for y in grp)
                n_tx += len(grp)
                break
    devs = sorted({x["device_profile"] for x in ev.window if x["txn_id"] in st.entity_ids and x.get("device_profile")})
    ev.connected_devices = devs
    if ring:
        ev.connected_cards = sorted(ring)
        ev.ring_total = round(total, 2)
        ev.signals.append(Signal("structuring_ring", True, 1.5,
                                 f"The same shape (3 or more online purchases within an hour just under ${thr:,.0f}) appears on "
                                 f"{len(ring)} other cards in the surrounding 4 weeks: {n_tx} transactions, ${total:,.2f}",
                                 sorted(ring)[:25], f"query:amount_band_scan(lo={0.88 * thr:.0f}, hi={thr:.0f}, +-14d)", "network"))
