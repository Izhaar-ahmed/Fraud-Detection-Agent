# Social posts

## LinkedIn

We built a fraud investigation agent on @TigerGraphDB for the Hacker House Goa 2026 Agentic Fraud Investigation track, in one evening.

It takes a card alert, investigates it in a TigerGraph graph of 590,742 transactions and 5,565 closed cases through the TigerGraph MCP server, and recommends next actions under the bank's fraud policy, with the approval route for each.

What we found:
- The bank's risk score was inverted where it mattered. A model trained only on the closed cases separates confirmed fraud from cleared false alarms at ROC AUC 0.88; the bank score gets 0.05 on the same split.
- One case scored 0.05 by the bank was a device-sharing ring: one Samsung SM-G935F profile behind an anonymous proxy across 24 cards in two weeks. It was only visible two hops out in the graph.
- Generic device profiles such as a default Windows browser connect unrelated people and need filtering.

Write-up: [blog link]
Demo: [demo link]

#TigerGraph

## X

Built a fraud investigation agent on @TigerGraphDB at Hacker House Goa: 590k transactions, GSQL over MCP, case memory in the graph. The bank's risk score missed a 24-card device ring that sat two hops out. Write-up: [blog link] Demo: [demo link]
