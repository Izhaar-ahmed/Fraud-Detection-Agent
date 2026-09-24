# Demo video script (about 4 minutes)

Presenter: Izhaar. Read the voice-over lines as written; the "Screen" column says what to show.

Before recording:
- Savanna workspace running, `FraudGraph` loaded, queries installed.
- `.env` filled in (keep the terminal scrolled past it; never show `TG_SECRET` or API keys on screen).
- Workbench running: `FRAUD_BACKEND=mcp python -m fraudagent.server`, open http://127.0.0.1:8000.
- A terminal at the repo root with the venv active.
- Savanna graph explorer open on `FraudGraph`, with device `DPbd0e6b7c4f` searched in advance.

---

### 0:00 to 0:20 Intro

**Screen:** Workbench case queue with all 20 cases.

**Voice-over:**
"This is our fraud investigation agent for the TigerGraph Hacker House Goa track. It takes an alert, investigates it in TigerGraph through the TigerGraph MCP server, and recommends what the bank should do under the fraud policy, with the approval route for each action. These are the 20 cases in the pack."

### 0:20 to 0:50 Architecture in one breath

**Screen:** README architecture diagram, then back to the workbench.

**Voice-over:**
"The graph holds 590,742 transactions, the cards, devices, email domains, billing regions and 5,565 closed cases, about 3.1 million vertices and edges. The agent reads it through 12 installed GSQL queries over MCP. The decisions are deterministic: a model trained on the closed cases, graph signals, and the policy written as code. The LLM only writes the summary and the report narrative."

### 0:50 to 1:50 HHG-014: the device ring

**Screen:** Click HHG-014 in the queue and run it. Show the investigation timeline streaming in.

**Voice-over:**
"HHG-014 is an analyst request. Transaction 3478561, $74.96 online. The bank's risk score is 0.05 and our own model says 0.11. On the card alone, there's nothing.

The agent pulls the device neighbourhood. This Samsung SM-G935F profile, Chrome for Android, was used by 24 cards in two weeks, 23 of them behind an anonymous proxy, with 47 confirmed closed cases on those cards."

**Screen:** Network view for HHG-014.

**Voice-over:**
"Probability goes to 0.66. That's below the policy's 0.70 bar for a block, so rule R1 applies: the initial action is step-up authentication and verification, a case, and escalation."

**Screen:** Initial versus final next best action panel.

**Voice-over:**
"Customer replies aren't in the dataset, so we simulate them and record the assumption. The step-up fails. Probability moves to 0.92, and the final actions change: block the card, which needs a team lead, L1. File a report, which needs a fraud manager, L2. And monitor the 23 connected cards. The pattern is undocumented, a device-sharing ring, and the agent describes it in its own words."

### 1:50 to 2:20 The ring in Savanna

**Screen:** Savanna graph explorer. Start from DeviceProfile `DPbd0e6b7c4f`, expand `FROM_DEVICE` to Txn, then `MADE` to Card. Let the fan-out of cards render.

**Voice-over:**
"Here's the same ring in the Savanna explorer. One device profile, out to its transactions, out to the cards. That two-hop walk is what the device_neighbors query does for the agent, and it's what the bank's score couldn't see."

### 2:20 to 3:00 HHG-006: threshold structuring

**Screen:** Back to the workbench. Open HHG-006. Show the evidence table, then the SAR panel.

**Voice-over:**
"HHG-006 is a customer dispute over $482.12. The agent finds four online purchases in thirty minutes on the same card, $478.95, $456.96, $488.04 and $482.12, all just under $500. $1,906.07 in total. Vector search inside GSQL returns three closed cases with exactly this shape, all undocumented fraud.

The device here is a generic Windows 7, Internet Explorer profile shared by 43 cards, and the agent ignores it. Generic profiles connect unrelated people.

Then it asks the graph a second question: does this shape appear on other cards? One scan over every transaction finds thirteen other cards with the same pattern in the same four weeks, $24,262 in total. The denial settles it under R2 at 0.98. Block at L1, report at L2, and all thirteen cards go under monitoring."

### 3:00 to 3:30 Case memory

**Screen:** Case memory panel for HHG-014, showing earlier investigations retrieved.

**Voice-over:**
"Every investigation is written back to TigerGraph as an InvestigationCase vertex with edges to its card, transactions, device and similar closed cases. Later cases retrieve it. HHG-014 pulled up our own earlier cases HHG-011 and HHG-019."

### 3:30 to 3:55 Full run in the terminal

**Screen:** Terminal. Run:

```bash
python -m fraudagent.run_cases --backend mcp
```

Let the 20 lines print.

**Voice-over:**
"And the full pack from the command line. One line per case: verdict, probability, pattern, exposure, whether a report is filed, and the final actions. Nine fraud, eleven legitimate, four reports. Half the pack being legitimate matters: an agent that blocks everything scores badly."

### 3:55 to 4:15 Close

**Screen:** Workbench queue, or the README results table.

**Voice-over:**
"What we learned: the bank's high risk scores were mostly the false alarms, the answer often sits on other cards in the graph, and generic devices need filtering out. Code, answer files and the write-up are linked below. Thanks for watching."
