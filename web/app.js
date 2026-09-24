"use strict";

const S = { cases: [], filter: "all", sel: null, detail: null, tab: "timeline", running: false, graph: {} };
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v) => (v == null ? "" : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const prob = (v) => (v == null ? "" : Number(v).toFixed(2));
const human = (s) => String(s || "").replace(/_/g, " ");
const TRIG = { risk_score: "Risk score", customer_report: "Customer report", analyst_request: "Analyst request" };
const TRIG_SHORT = { risk_score: "Risk score", customer_report: "Customer", analyst_request: "Analyst" };

function badge(v) {
  if (!v) return `<span class="badge b-none">Not run</span>`;
  return `<span class="badge b-${esc(v)}">${esc(v)}</span>`;
}
function meter(p) {
  if (p == null) return "";
  const cls = p >= 0.7 ? "hi" : p <= 0.3 ? "lo" : "mid";
  return `<span class="meter"><span class="bar"><span class="fill ${cls}" style="width:${Math.round(p * 100)}%"></span></span><span class="tnum">${prob(p)}</span></span>`;
}
async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

/* ------------------------------------------------------------ health */
async function loadHealth() {
  try {
    const h = await getJSON("/api/health");
    $("#health").innerHTML =
      `<span><span class="dot ok"></span>Backend <b>${esc(h.backend)}</b></span>` +
      `<span>Store <b>${h.store_loaded ? "loaded" : "idle"}</b></span>` +
      `<span>Tool calls <b class="tnum">${h.tool_calls}</b></span>` +
      `<span>LLM <b>${h.llm_available ? "available" : "not configured"}</b></span>`;
  } catch (e) {
    $("#health").innerHTML = `<span><span class="dot"></span>Server unreachable</span>`;
  }
}

/* ------------------------------------------------------------ queue */
async function loadCases() {
  S.cases = await getJSON("/api/cases");
  renderQueue();
}
function renderQueue() {
  const rows = S.cases.filter((c) => S.filter === "all" || c.verdict === S.filter);
  $("#queue-count").textContent = `${rows.length} of ${S.cases.length}`;
  $("#queue-body").innerHTML = rows.map((c) => `
    <tr data-id="${esc(c.case_id)}" class="${c.case_id === S.sel ? "sel" : ""}">
      <td class="id">${esc(c.case_id)}</td>
      <td class="tnum">${esc((c.opened_at || "").slice(5, 16))}</td>
      <td class="trig" title="${esc(TRIG[c.trigger_type])}">${esc(TRIG_SHORT[c.trigger_type] || c.trigger_type)}</td>
      <td class="card">${esc(c.card_id)}</td>
      <td>${badge(c.verdict)}</td>
      <td class="num" title="${esc(prob(c.probability))}">${pct(c.probability)}</td>
      <td class="num">${money(c.exposure)}</td>
    </tr>`).join("");
}
$("#queue-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (tr) selectCase(tr.dataset.id);
});
$("#filters").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  S.filter = b.dataset.f;
  document.querySelectorAll("#filters button").forEach((x) => x.classList.toggle("on", x === b));
  renderQueue();
});

/* ------------------------------------------------------------ detail */
async function selectCase(id) {
  if (S.running) return;
  S.sel = id;
  renderQueue();
  history.replaceState(null, "", `#${id}${S.tab !== "timeline" ? "/" + S.tab : ""}`);
  S.detail = await getJSON(`/api/cases/${id}`);
  renderDetail();
}

function renderDetail() {
  const d = S.detail, row = d.row, a = d.answer || {}, c = a.case || {};
  const steps = (d.trace && d.trace.steps) || [];
  const nba = a.next_best_actions || {};
  const ev = c.evidence || [];
  $("#detail").innerHTML = `
    <div class="case-head">
      <div class="ch-row1">
        <span class="ch-id">${esc(row.case_id)}</span>
        ${badge(c.verdict)}
        <span class="ch-sub">${esc(TRIG[row.trigger_type] || row.trigger_type)} &middot; opened <span class="tnum">${esc(row.opened_at)}</span></span>
        <div class="ch-actions">
          <span class="run-status" id="run-status"></span>
          <button class="btn btn-primary" id="run-btn">Re-run investigation</button>
        </div>
      </div>
      <div class="ch-trigger">${esc(row.trigger_text)}</div>
      <div class="facts">
        <div class="fact"><div class="k">Card</div><div class="v mono">${esc(row.card_id)}</div></div>
        <div class="fact"><div class="k">Customer</div><div class="v mono">${esc(row.customer_id)}</div></div>
        <div class="fact"><div class="k">Flagged txn</div><div class="v mono">${esc(row.flagged_txn_id)}</div></div>
        <div class="fact"><div class="k">Status ${tip(FIELD_TIPS.status)}</div><div class="v">${esc(human(c.status) || "Not run")}</div></div>
        <div class="fact"><div class="k">Pattern ${tip(FIELD_TIPS.pattern)}</div><div class="v" title="${esc((PATTERN_WORDS[c.pattern] || human(c.pattern) || "") + (c.pattern ? " (" + c.pattern + ")" : ""))}">${esc(PATTERN_WORDS[c.pattern] || human(c.pattern) || "")}</div></div>
        <div class="fact"><div class="k">Fraud prob. ${tip(FIELD_TIPS.prob)}</div><div class="v">${meter(c.fraud_probability)}</div></div>
        <div class="fact"><div class="k">Exposure ${tip(FIELD_TIPS.exposure)}</div><div class="v tnum">${money(c.exposure_usd)}</div></div>
        <div class="fact"><div class="k">SAR ${tip(FIELD_TIPS.sar)}</div><div class="v">${a.sar ? (a.sar.file ? "File" : "Not required") : ""}</div></div>
        <div class="fact"><div class="k">Graph case ${tip(FIELD_TIPS.graph)}</div><div class="v mono">${esc(c.graph_case_id || "")}</div></div>
        <div class="fact"><div class="k">Tool calls ${tip(FIELD_TIPS.tools)}</div><div class="v tnum">${a.tool_calls != null ? `${a.tool_calls} calls, ${a.latency_s}s` : ""}</div></div>
      </div>
    </div>
    ${caseBrief(d)}
    <nav class="tabs" id="tabs">
      ${tabBtn("timeline", "Investigation timeline", steps.length)}
      ${tabBtn("nba", "Next best action")}
      ${tabBtn("evidence", "Evidence", ev.length)}
      ${tabBtn("network", "Network")}
      ${tabBtn("sar", "SAR")}
      ${tabBtn("memory", "Case memory", (c.similar_prior_cases || []).length)}
    </nav>
    <div class="pane" id="pane"></div>`;
  $("#run-btn").addEventListener("click", () => runCase(row.case_id));
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (b) { S.tab = b.dataset.tab; history.replaceState(null, "", `#${row.case_id}/${S.tab}`); renderDetail(); }
  });
  renderPane();
}
const tabBtn = (id, label, n) =>
  `<button data-tab="${id}" class="${S.tab === id ? "on" : ""}">${label}${n != null ? `<span class="cnt" id="cnt-${id}">${n}</span>` : ""}</button>`;

function renderPane() {
  const fn = { timeline: paneTimeline, nba: paneNBA, evidence: paneEvidence, network: paneNetwork, sar: paneSAR, memory: paneMemory }[S.tab];
  $("#pane").innerHTML = fn();
  if (S.tab === "network") drawNetwork();
}

/* ------------------------------------------------------------ timeline */
function stepHTML(s, prev, isNew) {
  let extra = "";
  if (s.probability != null) {
    const p = prev != null && prev !== s.probability ? `${prob(prev)} &rarr; ${prob(s.probability)}` : prob(s.probability);
    extra += `<div><span class="prob">P ${p}</span></div>`;
  }
  if (s.actions) extra += `<div class="acts">${s.actions.map((x) => `<span class="chip act" title="${esc(actWord(x.action))}; ${esc(ROUTE_TIPS[x.route] || x.route)}">${esc(x.action)} <span class="muted">${esc(x.route)}</span></span>`).join("")}</div>`;
  if (s.similar) extra += `<div class="sub">Similar: ${s.similar.map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>`;
  if (s.docs) extra += `<div class="sub">Policy: ${s.docs.map(esc).join("; ")}</div>`;
  if (s.critique) extra += `<div class="sub">Critique: ${esc(s.critique)}</div>`;
  if (s.signal) extra += `<div class="sub">Signal <span class="mono">${esc(s.signal)}</span>, strength ${esc(s.strength)}</div>`;
  return `<li class="tl${isNew ? " new" : ""}">
    <span class="n">${s.n}</span>
    <span class="kind k-${esc(s.kind)}" title="${esc(KIND_TIPS[s.kind] || "")}">${esc(String(s.kind).toUpperCase())}</span>
    <div><div class="txt">${esc(s.text)}</div>${extra}</div>
    <span class="t">${Number(s.t).toFixed(2)}s</span></li>`;
}
function stepsHTML(steps) {
  let prev = null;
  return steps.map((s) => { const h = stepHTML(s, prev); if (s.probability != null) prev = s.probability; return h; }).join("");
}
function paneTimeline() {
  const d = S.detail, a = d.answer || {}, c = a.case || {};
  const steps = (d.trace && d.trace.steps) || [];
  const tools = (d.trace && d.trace.tool_trace) || [];
  return `
    <div class="section"><h3>Investigation timeline <span class="muted"><span id="steps-count">${steps.length}</span> steps${a.latency_s != null ? ` &middot; ${a.tool_calls} tool calls &middot; ${a.latency_s}s` : ""}</span></h3>
      <ol class="timeline" id="timeline">${steps.length ? stepsHTML(steps) : `<li class="none-note">No trace recorded. Run the investigation.</li>`}</ol></div>
    ${a.stop_reason ? `<div class="section"><h3>Stop reason</h3><div class="body">${esc(a.stop_reason)}</div></div>` : ""}
    ${tools.length ? `<div class="section"><h3>Graph tool calls <span class="muted">last ${tools.length}</span></h3>
      <table class="grid"><thead><tr><th>Query</th><th>Parameters</th><th class="num">Results</th><th class="num">ms</th><th>Backend</th></tr></thead><tbody>
      ${tools.slice(-15).map((t) => `<tr><td class="mono">${esc(t.name)}</td><td class="ref">${esc(JSON.stringify(t.params))}</td><td class="num">${esc(t.n_results)}</td><td class="num">${esc(t.ms)}</td><td>${esc(t.backend)}</td></tr>`).join("")}
      </tbody></table></div>` : ""}`;
}

/* ------------------------------------------------------------ NBA */
function actTable(title, acts) {
  return `<div class="section"><h3>${title} <span class="muted">${(acts || []).length} actions</span></h3>
    <table class="grid"><thead><tr><th>Action</th><th class="route-c">Route</th><th>Reason</th></tr></thead><tbody>
    ${(acts || []).map((x) => `<tr><td class="act w">${esc(actWord(x.action))}<div class="code">${esc(x.action)}</div></td><td class="route-c">${routeBadge(x.route)}</td><td>${esc(x.reason)}</td></tr>`).join("")}
    </tbody></table></div>`;
}
function paneNBA() {
  const a = S.detail.answer || {}, n = a.next_best_actions || {};
  const reqs = a.evidence_requests || [];
  return `
    <div class="explain-note">Initial = what the agent recommended before asking for more evidence. Final = after the (simulated) reply.</div>
    <div class="changed"><div class="k">What changed</div><div>${esc(n.what_changed || "nothing")}</div></div>
    <div class="nba">${actTable("Initial recommendation", n.initial)}${actTable("Final recommendation", n.final)}</div>
    <div class="section"><h3>Evidence requests <span class="muted">${reqs.length}</span></h3>
    ${reqs.length ? `<table class="grid"><thead><tr><th>Type</th><th class="num">After step</th><th>Assumed response <span class="muted" style="text-transform:none;letter-spacing:0">(simulated per policy section 5)</span></th></tr></thead><tbody>
      ${reqs.map((r) => `<tr><td class="act">${esc(human(r.type))}</td><td class="num">${esc(r.asked_after_step)}</td><td>${esc(r.assumed_response)}</td></tr>`).join("")}
      </tbody></table>` : `<div class="none-note">No evidence was requested; the initial recommendation stands.</div>`}</div>`;
}

/* ------------------------------------------------------------ evidence */
function paneEvidence() {
  const ev = (S.detail.answer || {}).case?.evidence || [];
  return `<div class="section"><h3>Evidence <span class="muted">${ev.length} items</span></h3>
    <table class="grid"><thead><tr><th style="width:40%">Claim</th><th style="width:90px">Source</th><th style="width:250px">Reference</th><th>Entities</th></tr></thead><tbody>
    ${ev.map((e) => `<tr><td>${esc(e.claim)}</td><td class="src">${esc(e.source)}</td><td class="ref">${esc(e.ref)}</td>
      <td>${(e.entity_ids || []).slice(0, 10).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}${(e.entity_ids || []).length > 10 ? `<span class="muted"> +${e.entity_ids.length - 10} more</span>` : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

/* ------------------------------------------------------------ SAR */
function paneSAR() {
  const s = (S.detail.answer || {}).sar;
  if (!s) return `<div class="none-note">No answer on file.</div>`;
  const kv = (k, v) => `<div class="k">${k}</div><div>${v}</div>`;
  return `<div class="section"><h3>Suspicious activity report</h3>
    <div class="sar-grid">
      ${kv("File report", s.file ? `<span class="badge b-fraud">Yes</span>` : `<span class="badge b-none">No</span>`)}
      ${kv("Reason", esc(s.reason))}
      ${kv("Total amount", `<span class="tnum">${money(s.total_amount_usd)}</span>`)}
      ${kv("Activity dates", esc((s.activity_dates || []).join(" to ")))}
      ${kv("Subjects", (s.subjects || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("") || `<span class="muted">None</span>`)}
    </div>
    ${s.narrative ? `<div class="doc"><h4>Narrative</h4>${esc(s.narrative)}</div>` : `<div class="none-note">No narrative: report not required.</div>`}
  </div>`;
}

/* ------------------------------------------------------------ memory */
function paneMemory() {
  const c = (S.detail.answer || {}).case || {};
  const steps = (S.detail.trace && S.detail.trace.steps) || [];
  const mem = steps.find((s) => s.kind === "memory");
  const simClaim = (c.evidence || []).find((e) => (e.ref || "").startsWith("query:similar_cases"));
  const ids = c.similar_prior_cases || [];
  return `<div class="section"><h3>Similar prior cases <span class="muted">cited in the answer</span></h3>
    <table class="grid"><thead><tr><th>Case</th><th>Outcome / pattern</th></tr></thead><tbody>
    ${ids.map((id) => { const m = simClaim && simClaim.claim.match(new RegExp(id + " \\(([^)]*)\\)")); return `<tr><td class="mono">${esc(id)}</td><td>${esc(m ? human(m[1]) : "")}</td></tr>`; }).join("") || `<tr><td colspan="2" class="none-note">None</td></tr>`}
    </tbody></table></div>
    ${mem ? `<div class="section"><h3>Retrieval step</h3><div class="body">${esc(mem.text)}
      <div style="margin-top:8px">${(mem.similar || []).map((x) => `<span class="chip">${esc(x)}</span>`).join("")}</div>
      <div class="muted" style="margin-top:6px">Policy passages: ${(mem.docs || []).map(esc).join("; ")}</div></div></div>` : ""}
    ${simClaim ? `<div class="section"><h3>Vector search result</h3><div class="body">${esc(simClaim.claim)}</div></div>` : ""}`;
}

/* ------------------------------------------------------------ network */
function paneNetwork() {
  return `<div class="section"><h3>Network <span class="muted" id="net-meta">Loading subgraph</span></h3>
    <div class="net-wrap" id="net"></div>
    <div class="legend">
      <span><i style="background:#eff4ff;border-color:#1d4ed8"></i>Card under review</span>
      <span><i style="background:#fef2f2;border-color:#b91c1c"></i>Flagged or affected transaction</span>
      <span><i style="background:#fff;border-color:#9ca3af"></i>Customer, device, other cards</span>
      <span><i style="background:#fefce8;border-color:#a16207"></i>Connected card in the case</span>
      <span><i style="background:#f3f4f6;border-color:#6b7280"></i>Case and similar closed cases</span>
    </div></div>`;
}
async function drawNetwork() {
  const id = S.sel;
  let g = S.graph[id];
  if (!g) {
    try { g = S.graph[id] = await getJSON(`/api/cases/${id}/graph`); }
    catch (e) { $("#net-meta").innerHTML = `<span class="err">Could not load subgraph: ${esc(e.message)}</span>`; return; }
  }
  if (S.sel !== id || S.tab !== "network") return;
  $("#net").innerHTML = networkSVG(g);
  const nCards = g.nodes.filter((n) => n.kind === "card").length - 1;
  $("#net-meta").textContent = `${g.nodes.length} nodes, ${g.edges.length} edges` + (g.device_cards_total ? `; device used by ${g.device_cards_total} cards in 14 days, ${nCards} shown` : "");
}
function trunc(s, n) { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function networkSVG(g) {
  const RH = 50, top = 40;
  const row = S.detail.row;
  const by = (k) => g.nodes.filter((n) => n.kind === k);
  const txns = by("txn"), dev = by("device")[0], cust = by("customer")[0];
  const card = g.nodes.find((n) => n.id === row.card_id);
  const others = by("card").filter((n) => n.id !== row.card_id);
  const gcase = by("case")[0], closed = by("closed_case");
  const pos = {};
  const COLS = { txn: 95, card: 320, dev: 545, grid: 718 };
  txns.forEach((n, i) => (pos[n.id] = { x: COLS.txn, y: top + 30 + i * RH, w: 176, h: 38 }));
  const gridCols = others.length > 10 ? 3 : others.length > 5 ? 2 : 1;
  const GW = 100, rowsGrid = Math.ceil(others.length / gridCols);
  others.forEach((n, i) => (pos[n.id] = { x: COLS.grid + (i % gridCols) * (GW + 10), y: top + 30 + Math.floor(i / gridCols) * 40, w: GW, h: 28 }));
  const midRows = Math.max(txns.length, rowsGrid * 0.8, 4);
  const cy = top + 30 + ((midRows - 1) * RH) / 2;
  if (card) pos[card.id] = { x: COLS.card, y: cy, w: 150, h: 40 };
  if (cust) pos[cust.id] = { x: COLS.card, y: cy - 90, w: 130, h: 30 };
  if (gcase) pos[gcase.id] = { x: COLS.card, y: cy + 90, w: 150, h: 30 };
  closed.forEach((n, i) => (pos[n.id] = { x: COLS.card + (i - (closed.length - 1) / 2) * 104, y: cy + 150, w: 94, h: 26 }));
  if (dev) pos[dev.id] = { x: COLS.dev, y: cy, w: 196, h: 52 };
  const W = others.length ? COLS.grid + (gridCols - 1) * (GW + 10) + GW / 2 + 16 : (dev ? COLS.dev + 120 : COLS.card + 200);
  const H = Math.max(...Object.values(pos).map((p) => p.y + p.h / 2), 200) + 30;

  const edge = (e) => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return "";
    let x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
    if (Math.abs(x1 - x2) > 40) { const s = x2 > x1 ? 1 : -1; x1 += s * a.w / 2; x2 -= s * b.w / 2; }
    else { const s = y2 > y1 ? 1 : -1; y1 += s * a.h / 2; y2 -= s * b.h / 2; }
    const mx = (x1 + x2) / 2;
    const d = Math.abs(x1 - x2) > 40 ? `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}` : `M${x1},${y1} L${x2},${y2}`;
    const dash = e.label === "FROM_DEVICE" ? ' stroke-dasharray="4 3"' : "";
    return `<path d="${d}" fill="none" stroke="#cbd5e1" stroke-width="1"${dash}/>`;
  };
  const box = (n, fill, stroke, lines, bold) => {
    const p = pos[n.id]; if (!p) return "";
    const x = p.x - p.w / 2, y = p.y - p.h / 2;
    const ls = lines.filter(Boolean);
    const t = ls.map((l, i) => `<text x="${p.x}" y="${p.y + (i - (ls.length - 1) / 2) * 14 + 4}" text-anchor="middle" font-size="${i === 0 ? 12 : 11}" ${i === 0 && bold ? 'font-weight="600"' : ""} fill="${i === 0 ? "#111827" : "#6b7280"}" ${i === 0 ? 'font-family="IBM Plex Mono, monospace"' : ""}>${esc(l)}</text>`).join("");
    return `<g><title>${esc(n.label)}</title><rect x="${x}" y="${y}" width="${p.w}" height="${p.h}" rx="4" fill="${fill}" stroke="${stroke}"/>${t}</g>`;
  };
  const colHead = (x, label) => `<text x="${x}" y="${top - 12}" text-anchor="middle" font-size="11" fill="#6b7280" letter-spacing="0.5">${label}</text>`;
  let out = g.edges.map(edge).join("");
  if (cust) out += box(cust, "#fff", "#9ca3af", [cust.label, "customer"]);
  if (card) out += box(card, "#eff4ff", "#1d4ed8", [card.label, "card under review"], true);
  if (gcase) out += box(gcase, "#f3f4f6", "#6b7280", [gcase.label]);
  closed.forEach((n) => (out += box(n, "#f3f4f6", "#9ca3af", [n.label])));
  txns.forEach((n) => (out += box(n, n.flagged || n.affected ? "#fef2f2" : "#fff", n.flagged || n.affected ? "#b91c1c" : "#9ca3af",
    [n.label, `${money(n.amount)} · ${(n.ts || "").slice(5, 16)}${n.flagged ? " · flagged" : ""}`])));
  if (dev) out += box(dev, "#fff", "#6b7280", [trunc(dev.label.split("|")[0].trim(), 26), trunc(dev.label.split("|").slice(1).join(" · ").trim(), 32), `device profile · ${dev.n_cards} cards`]);
  others.forEach((n) => (out += box(n, n.connected ? "#fefce8" : "#fff", n.connected ? "#a16207" : "#d1d5db", [n.label])));
  out += colHead(COLS.txn, "TRANSACTIONS") + colHead(COLS.card, "CARD") + (dev ? colHead(COLS.dev, "DEVICE") : "")
    + (others.length ? colHead(COLS.grid + ((gridCols - 1) * (GW + 10)) / 2, "OTHER CARDS ON DEVICE") : "");
  return `<svg style="max-width:${W}px" viewBox="0 0 ${W} ${H}" font-family="IBM Plex Sans, sans-serif">${out}</svg>`;
}

/* ------------------------------------------------------------ live run */
async function runCase(id) {
  if (S.running) return;
  S.running = true;
  S.tab = "timeline";
  delete S.graph[id];
  S.detail.trace = { steps: [] };
  renderDetail();
  const btn = $("#run-btn"), status = $("#run-status"), tl = $("#timeline");
  btn.disabled = true; btn.textContent = "Running 0s";
  const t0 = Date.now();
  const tick = setInterval(() => { btn.textContent = `Running ${Math.floor((Date.now() - t0) / 1000)}s`; }, 1000);
  const setCounts = () => {
    const n = String(S.detail.trace.steps.length);
    const a = $("#cnt-timeline"), b = $("#steps-count");
    if (a) a.textContent = n;
    if (b) b.textContent = n;
  };
  tl.innerHTML = "";
  let prev = null;
  const onEvent = (ev, data) => {
    if (ev === "status") status.textContent = data.text;
    else if (ev === "step") {
      S.detail.trace.steps.push(data);
      tl.insertAdjacentHTML("beforeend", stepHTML(data, prev, true));
      if (data.probability != null) prev = data.probability;
      setCounts();
      status.textContent = `Step ${data.n}: ${data.kind}`;
    } else if (ev === "done") {
      status.textContent = `Completed in ${data.latency_s}s, ${data.tool_calls} tool calls`;
      applyRecord(id, data);
    }
    else if (ev === "error") status.innerHTML = `<span class="err">${esc(data.text)}</span>`;
  };
  try {
    const r = await fetch(`/api/cases/${id}/run`, { method: "POST" });
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = "message", data = "";
        chunk.split("\n").forEach((l) => { if (l.startsWith("event: ")) ev = l.slice(7); else if (l.startsWith("data: ")) data += l.slice(6); });
        if (data) onEvent(ev, JSON.parse(data));
      }
    }
  } catch (e) {
    status.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
  clearInterval(tick);
  S.running = false;
  const msg = status.textContent;
  await Promise.all([loadCases(), loadHealth()]);
  S.detail = await getJSON(`/api/cases/${id}`);
  renderDetail();
  $("#run-status").textContent = msg;
}

/* Update the queue row from a finished record before the server refetch lands. */
function applyRecord(id, rec) {
  const c = rec.case || {}, row = S.cases.find((x) => x.case_id === id);
  if (row) Object.assign(row, { verdict: c.verdict, status: c.status, pattern: c.pattern, probability: c.fraud_probability,
    exposure: c.exposure_usd, sar_file: (rec.sar || {}).file, has_answer: true });
  renderQueue();
}

/* ------------------------------------------------------------ boot */
$("#help-link").addEventListener("click", (e) => { e.preventDefault(); openHelp(); });
(async function boot() {
  loadHealth();
  setInterval(loadHealth, 15000);
  await loadCases();
  const [id, tab] = location.hash.slice(1).split("/");
  if (tab) S.tab = tab;
  const first = S.cases.find((c) => c.case_id === id) || S.cases[0];
  if (first) selectCase(first.case_id);
})();
