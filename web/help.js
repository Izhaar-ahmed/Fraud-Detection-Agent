"use strict";
/* Plain-English labels, tooltips, the case brief and the glossary drawer.
   Loaded before app.js; relies only on the globals defined here. */

const PATTERN_WORDS = {
  card_testing: "Card testing",
  card_not_present_fraud: "Card-not-present fraud",
  card_not_present_new_device: "Card-not-present fraud from a new device",
  out_of_region_use: "Out-of-region use",
  account_takeover: "Account takeover",
  undocumented: "New (undocumented) pattern",
  none: "No fraud",
};
const ACTION_WORDS = {
  DECLINE_TRANSACTION: "Decline the transaction",
  STEP_UP_AUTH: "Challenge the cardholder with a one-time passcode",
  VERIFY_WITH_CUSTOMER: "Ask the customer to confirm the activity",
  BLOCK_CARD: "Block and reissue the card",
  BLOCK_ALL_CARDS: "Block every card on the customer",
  CREATE_CASE: "Open an internal investigation case",
  FILE_REPORT: "File a suspicious activity report",
  MONITOR_CONNECTED_CARDS: "Watch the connected cards",
  MONITOR_CARD: "Watch this card",
  WARN_CUSTOMER: "Warn the customer",
  ESCALATE_TO_ANALYST: "Hand over to a human analyst",
  ALLOW_TRANSACTION: "Allow the transaction",
  CLOSE_NO_FRAUD: "Close the case as not fraud",
  GENERATE_REPORT: "Generate an internal report",
};
const ROUTE_WORDS = { auto: "Done automatically", L1: "Needs team lead approval", L2: "Needs fraud manager approval" };
const ROUTE_TIPS = {
  auto: "auto: low-risk action the agent may carry out itself",
  L1: "L1: the agent recommends it; a team lead must approve before it happens",
  L2: "L2: the agent recommends it; a fraud manager must approve before it happens",
};
const VERDICT_WORDS = { fraud: "Fraud", legitimate: "Legitimate", uncertain: "Uncertain" };
const TRIGGER_WORDS = {
  risk_score: "The bank's risk model flagged",
  customer_report: "The customer reported",
  analyst_request: "An analyst asked us to review",
};
const REQUEST_WORDS = {
  customer_validation: "Ask the customer to confirm",
  step_up_auth: "One-time passcode challenge",
  analyst_info: "Ask an analyst for information",
};
const KIND_TIPS = {
  trigger: "The alert that started this investigation.",
  case: "The agent opened or updated the case record in the graph.",
  gather: "The agent ran a graph query to collect facts about the card, device or customer.",
  signal: "A single risk indicator found in the data, with its strength.",
  memory: "The agent looked up similar closed cases and policy passages to reuse past decisions.",
  assess: "The agent combined the signals into a verdict and a fraud probability.",
  recommend: "The agent proposed actions, each with its approval route under the policy.",
  request: "The agent asked for more evidence; the reply is simulated per policy section 5.",
  explain: "The agent wrote the plain-language explanation and stop reason.",
};
const FIELD_TIPS = {
  status: "Where the case stands: open (waiting on evidence), escalated (with a human), closed as fraud, or closed as legitimate.",
  pattern: "The known fraud pattern this matches, 'undocumented' for a new pattern, or 'none'.",
  exposure: "Total dollar amount of the transactions believed to be part of this fraud.",
  sar: "Suspicious activity report: a regulatory filing, required only when policy calls for it.",
  graph: "ID of the case record written into the graph, so future investigations can find it (case memory).",
  tools: "Number of graph and retrieval queries the agent ran, and how long the run took.",
  prob: "How likely the flagged activity is fraud, from 0% to 100%.",
};

const actWord = (a) => ACTION_WORDS[a] || human(a);
const tip = (text) => `<span class="qm" title="${esc(text)}" aria-label="${esc(text)}">?</span>`;
const pct = (v) => (v == null ? "" : Math.round(Number(v) * 100) + "%");
const routeBadge = (r) => `<span class="route ${esc(r)}" title="${esc(ROUTE_TIPS[r] || "")}">${esc(r)}</span>`;

/* "for $74.96 (online, product C) on 2016-11-22" from the summary; row as fallback */
function incidentFacts(row, c) {
  const m = String(c.summary || "").match(/for \$([\d,.]+) \(([^,)]+)[^)]*\) on (\d{4}-\d{2}-\d{2})/);
  if (m) return { amount: "$" + m[1], channel: m[2].replace(/_/g, "-"), date: m[3] };
  const a = String(row.trigger_text || "").match(/\$[\d,.]+\d/);
  return { amount: a ? a[0] : "", channel: "", date: String(row.opened_at || "").slice(0, 10) };
}

function caseBrief(d) {
  const row = d.row, a = d.answer, c = (a && a.case) || {};
  if (!a) return `<div class="brief"><div class="brief-row"><div class="bk">Case brief</div><div>Not investigated yet. Run the investigation to get a brief.</div></div></div>`;
  const f = incidentFacts(row, c);
  const what = `${TRIGGER_WORDS[row.trigger_type] || human(row.trigger_type)} a ${f.amount || "transaction"}${f.channel ? " " + f.channel : ""} payment on card <span class="mono">${esc(row.card_id)}</span>${f.date ? " on " + esc(f.date) : ""}.`;
  const concl = `<b>${esc(VERDICT_WORDS[c.verdict] || human(c.verdict))}, ${pct(c.fraud_probability)} likely</b>: ${esc(PATTERN_WORDS[c.pattern] || human(c.pattern))}` +
    (c.pattern === "undocumented" && c.pattern_description ? ` <span class="muted">(${esc(c.pattern_description.split(":")[0])})</span>` : "") + ".";
  const claims = (c.evidence || []).filter((e) => !String(e.claim).startsWith("Policy retrieved")).slice(0, 3);
  const why = c.summary ? esc(c.summary) : claims.map((e) => esc(e.claim)).join("; ");
  const acts = (a.next_best_actions || {}).final || [];
  const next = acts.map((x) => `<li><span>${esc(actWord(x.action))}</span> <span class="who r-${esc(x.route)}" title="${esc(ROUTE_TIPS[x.route] || "")}">${esc(ROUTE_WORDS[x.route] || x.route)}</span></li>`).join("");
  const reqs = a.evidence_requests || [];
  const reqLine = reqs.length ? `<div class="brief-req"><b>Evidence requested:</b> ${reqs.map((r) => esc(REQUEST_WORDS[r.type] || human(r.type))).join("; ")}. <b>Assumed reply</b> <span class="muted">(simulated per policy section 5)</span>: ${reqs.map((r) => esc(r.assumed_response)).join(" ")}</div>` : "";
  return `<div class="brief">
    <div class="brief-row"><div class="bk">What happened</div><div>${what}</div></div>
    <div class="brief-row"><div class="bk">What we concluded</div><div>${concl}</div></div>
    <div class="brief-row"><div class="bk">Why</div><div>${why}</div></div>
    <div class="brief-row"><div class="bk">What happens next</div><div><ul class="next">${next || "<li>No action recommended.</li>"}</ul>${reqLine}</div></div>
  </div>`;
}

/* ------------------------------------------------------------ glossary drawer */
const GLOSSARY = [
  ["Triggers", "Every case starts from one alert: the bank's risk score flagged a transaction, the customer reported something, or an analyst asked for a review."],
  ["Verdict", "The agent's conclusion: fraud, legitimate, or uncertain when the evidence is not strong enough either way."],
  ["Fraud probability", "How likely the flagged activity is fraud, from 0% to 100%. The queue shows a rounded percentage; the case detail shows two decimals (0.92 = 92%)."],
  ["Exposure", "The total dollar amount of all transactions the agent believes belong to the same fraud episode."],
  ["Patterns", "Known fraud patterns from the policy: card testing, card-not-present fraud, card-not-present fraud from a new device, out-of-region use, account takeover. 'Undocumented' means a new pattern the agent describes in its own words; 'none' means no fraud."],
  ["Routes", "Who acts on a recommendation. auto: the agent does it itself (low risk). L1: a team lead must approve. L2: a fraud manager must approve. The agent never executes L1 or L2 actions on its own."],
  ["SAR", "Suspicious activity report: the regulatory filing. Filed only when the policy calls for it, and written to stand on its own for the regulator."],
  ["Graph case and case memory", "Each investigation is written into the graph as a case record. Later investigations search past cases (and 5,000+ closed cases) for similar ones and reuse what was decided."],
  ["What the agent does", "1. Reads the trigger. 2. Opens a case. 3. Gathers facts with graph queries (card history, device, region, linked cards). 4. Scores risk signals. 5. Retrieves similar past cases and policy passages. 6. Assesses a verdict and probability. 7. Recommends actions with approval routes. 8. Asks for more evidence if needed, then re-assesses. 9. Explains and stops."],
  ["Initial vs final", "Initial is what the agent recommended before asking for more evidence. Final is after the reply came back."],
  ["Customer replies are simulated", "This demo has no real customers. When the agent asks for a passcode challenge or a confirmation, the reply is simulated per policy section 5 and labelled as such."],
];
function openHelp() {
  let el = document.getElementById("help");
  if (!el) {
    el = document.createElement("div");
    el.id = "help";
    el.className = "help-backdrop";
    el.innerHTML = `<aside class="help" role="dialog" aria-modal="true" aria-labelledby="help-title">
      <div class="help-head"><h2 id="help-title">How to read this</h2><button class="btn" id="help-close">Close</button></div>
      <dl class="help-body">${GLOSSARY.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></aside>`;
    document.body.appendChild(el);
    el.addEventListener("click", (e) => { if (e.target === el || e.target.id === "help-close") closeHelp(); });
  }
  el.classList.add("open");
  document.getElementById("help-close").focus();
}
function closeHelp() { const el = document.getElementById("help"); if (el) el.classList.remove("open"); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelp(); });
