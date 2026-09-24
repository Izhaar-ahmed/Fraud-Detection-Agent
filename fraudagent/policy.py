"""Fraud Policy v1.0 as code: action catalogue, approval routing, rules R1-R10,
the case-vs-report decision (3a) and the stopping rule (6).

The agent may only execute `auto` actions; L1/L2 actions are recommended and
wait for a human. `execute()` enforces that.
"""
from __future__ import annotations

AUTO = {"ALLOW_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
        "STEP_UP_AUTH", "GENERATE_REPORT", "CREATE_CASE", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"}
ALL_ACTIONS = AUTO | {"DECLINE_TRANSACTION", "BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"}
ORDER = ["DECLINE_TRANSACTION", "STEP_UP_AUTH", "VERIFY_WITH_CUSTOMER", "BLOCK_CARD", "BLOCK_ALL_CARDS", "CREATE_CASE",
         "FILE_REPORT", "MONITOR_CONNECTED_CARDS", "MONITOR_CARD", "WARN_CUSTOMER", "ESCALATE_TO_ANALYST",
         "ALLOW_TRANSACTION", "CLOSE_NO_FRAUD", "GENERATE_REPORT"]


def route(action: str, exposure: float) -> str:
    if action in AUTO:
        return "auto"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    return "L2"  # BLOCK_ALL_CARDS, FILE_REPORT


def act(action: str, exposure: float, reason: str) -> dict:
    assert action in ALL_ACTIONS, action
    return {"action": action, "route": route(action, exposure), "reason": reason}


def ordered(actions: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in sorted(actions, key=lambda a: ORDER.index(a["action"])):
        if a["action"] not in seen:
            seen.add(a["action"])
            out.append(a)
    return out


def sar_required(verdict: str, prob: float, exposure: float, shared_origin: bool, undocumented: bool) -> tuple[bool, str]:
    """Section 3a: file when fraud is confirmed or strongly suspected AND one of the aggravating conditions holds."""
    suspected = verdict == "fraud" or prob >= 0.85
    if not suspected:
        return False, "3a: fraud not confirmed or strongly suspected, so no report; the internal case holds the record"
    why = []
    if exposure > 1000:
        why.append(f"exposure ${exposure:,.2f} exceeds $1,000")
    if shared_origin:
        why.append("activity connects to a shared device profile or other cards' fraud (R6)")
    if undocumented:
        why.append("pattern is coordinated or undocumented (R9)")
    if why:
        return True, "3a/R2: fraud confirmed and " + "; ".join(why)
    return False, f"3a: fraud confirmed but exposure ${exposure:,.2f} is under $1,000 with no shared origin or undocumented pattern, so case only"


def should_stop(prob: float, n_independent: int, settled_by_response: bool) -> tuple[bool, str]:
    if settled_by_response:
        return True, "verification response settles the question"
    if prob >= 0.85 and n_independent >= 2:
        return True, f"probability {prob:.2f} >= 0.85 with {n_independent} independent evidence groups"
    if prob <= 0.15 and n_independent >= 2:
        return True, f"probability {prob:.2f} <= 0.15 with {n_independent} independent evidence groups"
    return False, "uncertainty remains"


class PermissionError_(Exception):
    pass


def execute(action: dict, executor) -> dict:
    """Execute an action only when policy routes it `auto`; otherwise queue it for approval."""
    if action["route"] != "auto":
        return {"action": action["action"], "status": f"pending_{action['route']}_approval"}
    return executor(action)
