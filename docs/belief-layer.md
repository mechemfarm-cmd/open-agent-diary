# Belief layer

The belief layer is an optional ranking layer over **derived graph facts**. It does not replace raw entries, and it does not make a fact automatically true.

Its purpose is to help an agent or human inspect the most supportable, useful graph knowledge while preserving source evidence and uncertainty.

## What is ranked

A candidate fact may carry computed metadata for:

- **Provenance:** how the claim entered the system and who it can be attributed to.
- **Strength and confidence:** evidence-derived support, not a human promise of truth.
- **Volatility:** observed change over time for that specific relationship.
- **Salience:** exposure/use signals that prevent repeatedly surfaced but unhelpful facts from monopolizing recall.

Bulk-imported knowledge is marked provisional. A new installation has no facts, evidence history, or usage history; an empty belief list is correct.

## Four routes, four meanings

| Route | Purpose | Side effect |
|---|---|---|
| `POST /recall_beliefs` | Deliberate agent recall block | Records that returned facts were surfaced |
| `POST /list_beliefs` | Browser/UI inspection | Read-only; does not spend attention |
| `POST /confirm_beliefs` | Human says a surfaced fact was useful | Reduces outstanding unearned exposure |
| `POST /credit_beliefs` | Explicit independent corroboration flow | Credits eligible surfaced facts |

The UI intentionally calls `/list_beliefs`, not `/recall_beliefs`: merely opening a panel must not demote facts.

## Agent use

Use `/recall_beliefs` only when relevant durable facts would materially help the current task. V1 selection is salience-based, **not question-aware**. Do not inject it automatically on every conversation turn.

After recall, use raw entry/evidence retrieval for important claims. A ranked fact is a lead with provenance, not a substitute for checking its source.

Example:

```bash
curl -sS -X POST http://127.0.0.1:8041/recall_beliefs \
  -H 'Content-Type: application/json' \
  -d '{"limit":6,"char_budget":600}'
```

## Human use

Open **Derived → Knowledge by Belief** in the browser UI to inspect the current ranking. The **Useful** button is a human signal: it says the displayed fact helped, not that every claim about it is objectively true.

## What this layer does not claim

- It is not a general reasoning engine.
- It is not an automatic truth detector.
- It is not validated as “better memory” until real use has accumulated.
- It does not turn agent-generated or inferred text into human knowledge.

For how facts are produced and corrected, see [Knowledge graph](knowledge-graph.md).