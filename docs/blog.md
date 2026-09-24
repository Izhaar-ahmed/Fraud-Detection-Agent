# Investigating card fraud with a graph, a policy engine and a small model

*TigerGraph Hacker House Goa 2026, Agentic Fraud Investigation track*

The task gave us six months of IEEE-CIS card transactions (590,742 rows), 5,565 closed investigations from July to October, a written fraud policy, and 20 open alerts from November and December. For each alert the agent decides whether it is fraud, which pattern, how far it goes, and what the bank should do next with the approval route, before and after any evidence it asks for. Each case is written back into the graph so the next investigation can find it.

We built it in one evening. Below: the architecture, how TigerGraph is used, two cases with their real numbers, and what we would change.

## Architecture

There are four parts.

1. **Data preparation** (`prep.py`). `transactions.csv` has a `customer_id` but no card ID. We recover card IDs from the closed cases and the case pack, which name the card for each listed transaction, and assign the rest by the customer's card attribute tuple. The same step trains a LightGBM model on the closed cases only: 4,665 confirmed fraud and 900 cleared, plus 120k unlabeled July to October transactions as negatives.
2. **The graph** (`graph/schema.gsql`, `graph_load.py`). `Customer`, `PayCard` (the starter workspace already defines a global `Card` type), `Txn`, `DeviceProfile`, `EmailDomain`, `BillingRegion` and `ClosedCase`, plus `DocChunk` for policy text and `InvestigationCase` for the agent's own cases. 590,742 `Txn` vertices and about 3.1M vertices plus edges in total, with 12 installed GSQL queries.
3. **The agent** (`agent.py`, `signals.py`, `assess.py`, `policy.py`). A fixed state machine: trigger, open case, gather, extract signals, retrieve memory, assess, recommend, request evidence, reassess, recommend again, explain, write back.
4. **The LLM** (`llm.py`). Gemini Flash first, Groq as fallback, deterministic templates if neither answers. It writes the analyst summary, the SAR narrative and a short critique. It never sets a probability or picks an action.

An analyst case workbench (`python -m fraudagent.server`) streams each investigation live, next to the evidence, the network view and the SAR.

## How TigerGraph is used

**Schema.** Every type is local to `FraudGraph`, because the shared Savanna workspace already defines global names such as `Email` and `Address`. `Txn` carries `card_id`, `customer_id` and `device_id` as plain attributes next to the `MADE` and `FROM_DEVICE` edges, so a query can group per card or per device without a second hop. Closed cases connect to transactions (`CC_INVOLVES`), their own card (`CC_ON_CARD`) and linked cards (`CC_CONNECTED`).

**Queries.** The agent's per-case reads are `card_window`, `card_baseline`, `card_cases`, `device_neighbors`, `region_activity` and `email_neighbors`. `device_neighbors` is the one that decides the network cases: from one `DeviceProfile` it walks `FROM_DEVICE` to transactions in a 14-day window, then `MADE` to cards, tallies per-card amount, maximum model score, proxy flags and device status, and then follows `CC_ON_CARD` and `CC_CONNECTED` to the closed cases on those cards. `device_ring` and `card_community` cover ring discovery: all devices above a card count, or a bounded walk from one card that skips hub devices.

**MCP as the tool transport.** The agent calls the graph through `tigergraph-mcp` 1.0.3 over stdio. Reads go through `tigergraph__run_installed_query`; case write-back uses `add_node` and `add_edges`. `tg_mcp.py` is a small synchronous client around the MCP session. The same `GraphStore` interface has a pyTigerGraph fallback and a pandas mirror with identical return shapes, which is how the tests run offline.

**Vector similarity inside GSQL.** Policy and pattern sections plus our regulatory notes are chunked into `DocChunk` vertices. Every `ClosedCase` gets an embedding of its outcome, pattern and analyst notes. `similar_cases` and `doc_search` compute cosine similarity in GSQL with accumulators over `LIST<DOUBLE>` attributes and keep the top k in a `HeapAccum`. `similar_cases` scans `ClosedCase` and `InvestigationCase` in the same query, so past closed cases and this run's own cases come back ranked together. The embeddings are 256-dimensional feature-hashing vectors over word unigrams and bigrams: deterministic, no model download, identical on every machine.

**Case memory write-back.** Every investigation is written twice: once as an open `InvestigationCase` when the alert arrives, and again when it stops, with edges to its card, its affected transactions, the connected cards, the device profile and the three most similar closed cases (with the score on the edge). `run_cases` processes the pack in `opened_at` order, so an earlier case is retrievable by the next one. In the run below, HHG-014 retrieved `CASE-HHG-011` and `CASE-HHG-019` as prior investigations.

## Agentic behaviour

**Signals and probability.** `signals.py` extracts twelve signals: card testing, threshold structuring, a cross-card structuring ring, repeated amounts, new device, anonymising proxy, unusual amount or product, out-of-region use, account takeover markers, recurring charge, shared device and prior case history. Each carries a log-odds strength, the query that produced it, the entity IDs it rests on and an evidence group (`sequence`, `device`, `network`, `behaviour`, `region`, `identity`, `memory`, `customer`, `model`). Our model's score is the prior and the fired signals add to it.

**Policy as code.** `policy.py` holds the action catalogue, computes the route from the action and exposure (for example `BLOCK_CARD` is L1 up to $2,500 and L2 above), applies the case-versus-report rule from section 3a, and refuses to execute anything that is not `auto`.

**Before and after.** When the verdict is uncertain, the initial recommendation follows R1: verify or step up before any block, open a case, escalate under R8 if exposure or conflicting network evidence calls for it. The agent then requests `customer_validation` or `step_up_auth`. Replies are not in the dataset, so a simulator answers from the graph evidence and the assumption is written into `evidence_requests`. The agent reassesses and recommends again, and `what_changed` states the difference.

**Stopping.** Section 6 says stop at 0.85 or above, or 0.15 or below, with two independent pieces of evidence. We count evidence groups, not signals, so two facts from the same card window do not count twice. When that holds, the agent stops without an evidence request and says why in `stop_reason`.

## Worked example: HHG-014, a device ring the score missed

An analyst asked about transaction 3478561, $74.96 online on card C13487-K1 on 2016-11-22. The bank risk score was 0.05 and our model scored it 0.11. On the card alone, there is nothing here.

`device_neighbors` on device DPbd0e6b7c4f (`SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080`) returned 24 cards in 14 days, 23 of them behind `IP_PROXY:ANONYMOUS`, and 47 confirmed closed cases on those cards. The profile is specific (a model string, not "Windows"), so it passes the filter. Probability rose to 0.66 on two groups, device and network.

0.66 is below R1's 0.70 bar, so the initial actions were `STEP_UP_AUTH`, `VERIFY_WITH_CUSTOMER`, `CREATE_CASE`, `MONITOR_CARD` and `ESCALATE_TO_ANALYST`, all auto. The simulated step-up failed and the cardholder denied the activity. Probability went to 0.92 and the final actions became `BLOCK_CARD` (L1, exposure $74.96), `CREATE_CASE`, `FILE_REPORT` (L2, under R6 and R9), `MONITOR_CONNECTED_CARDS` for the 23 other cards, and `ESCALATE_TO_ANALYST`. The pattern is `undocumented`: one proxied device profile across 24 unrelated cards is a coordinated operation, which none of the five known patterns describe. The same Samsung SM-G935F, Chrome for Android, anonymous-proxy profile appears in the closed cases the analysts could not classify. Eight tool calls.

## Worked example: HHG-006, threshold structuring

Customer C07297 wrote "I never made this $482.12 purchase" about transaction 3476682. Bank score 0.25, our model 0.13.

`card_window` over 72 hours found four online purchases on card C07297-K1 between 20:00 and 20:30 on 2016-11-21: $478.95, $456.96, $488.04 and $482.12, all just under $500, total $1,906.07. `card_baseline` put the flagged amount at 4.1 times the card's $116.73 average. Vector search returned CC-3907, CC-3841 and CC-3748, all confirmed `undocumented`, whose notes read "four online purchases within forty minutes, each just under $500".

The device was `Trident/7.0 | Windows 7 | ie 11.0 for desktop | 1920x1080`, used by 43 cards in the window with no other high-risk card on it. The specificity filter marked it generic and it contributed nothing, which is correct: that is a default desktop profile.

The card alone does not show how far this goes, so the agent took a second hop: `amount_band_scan` scans every online transaction in the graph for $440 to $500 over the surrounding four weeks and groups them by card. 13 other cards show the same shape (three or more such purchases inside an hour): 51 transactions, $24,262.56. That turns a single dispute into a coordinated operation. The cardholder's denial is direct evidence under R2 and probability reached 0.98, so no further request was needed and initial equals final: `BLOCK_CARD` (L1, $1,906.07 is under $2,500), `CREATE_CASE`, `FILE_REPORT` (L2: exposure above $1,000, coordinated and undocumented), `MONITOR_CONNECTED_CARDS` for the 13 cards under R6, and `ESCALATE_TO_ANALYST` under R9. Both device profiles on the four purchases (a Windows 7 desktop and an iPhone behind a transparent proxy) are recorded on the case.

## What we learned

**The bank risk score is inverted where it matters.** On the October hold-out, our model reaches ROC AUC 0.96 overall and 0.88 separating confirmed fraud from cleared false alarms. The bank `risk_score` scores 0.05 on that same split: its high scores were exactly the false alarms. HHG-010 shows it in the pack: risk score 0.90 on a $1,000.03 purchase, our model 0.01. The agent does not trust either score alone: with a shared device, a new-device flag and $1,000 exposed, it stays uncertain, the simulated cardholder never answers the step-up, and R4 and R8 put the card under monitoring, decline pending authorizations and send it to an analyst. HHG-014 is the reverse.

**The neighbourhood matters more than the card.** HHG-014 cannot be solved from C13487-K1's own history, only by asking what other cards did on the same device that fortnight: a two-hop query from the transaction.

**Shared generic device profiles are noise.** Without a filter, "same device as 43 other cards" looks damning in HHG-006 and would have linked unrelated customers into a fake ring. We now require a specific profile and a card count of 60 or fewer before a shared device counts as a link.

**Lexical embeddings retrieve lexical matches.** HHG-006 found its structuring precedents because the notes share phrases. HHG-014's top three similar cases were ordinary new-device fraud cases, not the SM-G935F undocumented cases, because our query text did not share their words.

## What we would improve

- Swap hashed vectors for a sentence embedding model and TigerGraph's native vector search.
- Compute exposure at ring level, so HHG-014 reports the connected cards' amounts and not only $74.96.
- Run `device_ring` over the exam period on a schedule to raise ring alerts before a customer reports.
- Segment customers into cardholders, since one customer ID aggregates many cards and per-card baselines are noisy.
- Check calibration against held-out closed cases.
