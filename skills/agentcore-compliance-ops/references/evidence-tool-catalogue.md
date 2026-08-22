# The evidence tool catalogue

Which tools an alert triage agent actually needs, what each response must carry, and the order to
call them in so the token bill stays sane without losing the audit trail.

`scoped-retrieval.md` establishes the principles this builds on and does not restate them:
scope lives in the session and never in a tool parameter (§1), a read has **three** answers and not
two (§3), "did not look" and "looked and found nothing" must be distinguishable in the record (§4),
every result needs a denominator and "aggregated" is a third state (§4b), and the catalogue itself
is a least-privilege control (§9).

This file is the concrete layer: the tools, their coverage fields, and the call sequence.

---

## 1. The coverage block, field by field

`scoped-retrieval.md` §4b establishes *that* every result needs a denominator. This is the field
set that delivers it, and the reason each field exists.

```python
"coverage": {
  "rows_total":       212,          # everything this source holds IN SCOPE
  "rows_in_period":     0,          # ... matching the narrowing asked for
  "rows_returned":      0,          # ... actually returned
  "basis":         "transactions",  # WHAT is being counted
  "first_seen":  "2024-03-02",      # scope-wide earliest — NOT of what returned
  "last_seen":   "2026-08-10",      # scope-wide latest  — NOT of what returned
  "period_requested": {"from": "2019-01-01", "to": "2019-12-31"},
  "truncated": false
}
```

Read that: **zero returned, 212 exist, and the customer's history starts after the window asked
for.** The agent asked about 2019; the relationship began in 2024. Without these fields this is an
empty list and the agent concludes the customer has no transactions. With them it can see it asked
the wrong question and re-ask. That is the whole mechanism.

**`rows_total`** — what exists in scope, ignoring every filter. This is the field that makes an
empty result interpretable. `rows_total: 0` and `rows_total: 47, rows_in_period: 0` are entirely
different findings, and without it both are `[]`.

**`basis`** — names the unit. "rows" is wrong for half of any real schema: KYC counts *versions*,
due diligence counts *reviews*, accounts counts *accounts*, screening counts *screening events*. A
count without its unit invites the reader to compare two different things, and the reader is a
model that will.

**`first_seen` / `last_seen`** — **scope-wide extremes, deliberately NOT the extremes of what was
returned.** The most commonly inverted field pair, and inverting it destroys the mechanism: if they
describe the returned rows they are redundant with `data`, and they can no longer tell the caller
that the window it asked for lies outside the data that exists.

Where the sort key is not date-ordered, emit them `null` **with a note naming the key the table is
actually ordered by**. Never omit them silently — an absent field reads as "no data"; a null with a
reason reads as "not answerable from this table's key". This matters for KYC especially, where
`version` and `verified_at` order differently and sorting by the wrong one silently returns a
superseded record.

**`period_requested`** — always present; `{}` when nothing was narrowed. `{}` is the complete and
honest record for a tool that takes no filter. Omitting the key makes "asked for everything" and
"the field was never written" identical, and an `EMPTY` that does not record its own subject is
unfalsifiable.

**`truncated`** plus the arithmetic **in words**:

```
"40 of 212 matching transactions were NOT returned (cap reached); this evidence
 set is incomplete and a disposition should treat it as a declared gap"
```

State it rather than leave it to subtraction. A model reads what it was given as complete unless
told otherwise, and the subtraction is exactly the step it will not perform.

**A floor flag** — where a full census would cost too many pages, stop and mark the total a floor.
A capped count presented as a total is a lie the model cannot detect.

### The row-level index

For high-cardinality sources, carry a compact index of what exists so the agent can drill without
re-reading the table:

```python
"index": {
   "by_month":       {"2026-06": 41, "2026-07": 88, "2026-08": 83},
   "by_type":        {"CRYPTO_WITHDRAW": 200, "CRYPTO_PURCHASE": 58},
   "counterparties": ["0xZZ-...", "..."]
}
```

The index answers "what is in here" for a fraction of the tokens of the rows themselves, and it is
what turns a summary call into an actionable one. **Keep it out of the sealed record** — it runs to
several KB, the record is written once and read by a human, and the envelope already answered the
question at the time. Seal the counts and flags, not the index verbatim.

---

## 2. The catalogue

Grouped by *why the agent calls it*. The tiers drive both prompt guidance and cost.

### Tier 0 — the bundle

| Tool | Returns |
|---|---|
| `get_case_context` | alert, rule logic, customer profile, accounts, latest KYC, due diligence, prior alerts, screening — as **sections** in one envelope |

Collapses the always-called reads into one round trip. Large token and latency win.

**It changes the response shape, and that has silently disabled audit sealing in production.** Read
`production-rules.md` §52 before adding one.

### Tier 1 — always called

| Tool | Source | Note |
|---|---|---|
| `get_alert` | Alerts | The alert under triage; bound to it by the interceptor. |
| `get_rule_logic` | Rules | The rule **and its version** — the logic that fired may not be current (`scoped-retrieval.md` §7). |
| `get_customer_profile` | Customers | Must include the **stated baseline**: purpose of account, employment, expected activity. Without a declared baseline, "unusual" is unfalsifiable and the agent will cite a thin profile as a mitigating factor. |
| `get_accounts` | Accounts | Currency per account. Never merge currency units. |
| `get_kyc_record` | KYC | Latest version **plus `versions_total`**. A KYC read that silently returns v1 of 4 is wrong in the way that matters. |
| `get_due_diligence` | DueDiligence | Reviews with **per-field verification status**. "Declared but not verified" is a different fact from "verified", and collapsing them removes the gap a reviewer needs to see. |
| `get_prior_alerts` | Alerts (by customer) | Pattern of behaviour. |

### Tier 1 — transactions, deliberately two tools

| Tool | Purpose |
|---|---|
| `get_transaction_summary` | Aggregates over the window: totals, counts, by-type, by-counterparty, by-month, wallet census. Returns **no row ids**. |
| `get_transaction_history` | The rows, filtered and capped. |

The single biggest token saver available. A 212-transaction alert costs a few hundred tokens as a
summary and thousands as rows. Summarise first, drill only where the summary shows something.

**The audit consequence must be handled explicitly**, and it is the "aggregated" state of
`scoped-retrieval.md` §4b: a summary reads every row and returns none, so a naive ledger records
"Transactions: read" with zero record ids. Counting summarised rows as *read* hides that no
individual row was examined; counting them as *unread* reported 212 of 212 outstanding on a run
that had summarised all of them. Neither is honest — carry the third state.

### Tier 2 — case-dependent

| Tool | Source | Called when |
|---|---|---|
| `get_chain_analytics` | ChainAnalytics | Crypto. Attribution, designation, **hop distance, confidence, staleness** — a four-hop, low-confidence, nineteen-month-stale designation is a very different fact from a direct one, and the agent cannot weigh it if the tool returns only the label. |
| `get_kyt_address_exposure` | KytExposure | Crypto. Per-address exposure by category. |
| `get_kyt_user_exposure` | KytExposure | Crypto. Customer-level aggregate. |
| `get_device_intel` | DeviceIntel | ATO and fraud. Device continuity is often the strongest false-positive signal available. |
| `get_name_screening_history` | NameScreening | Sanctions/PEP. **Screened and clear is a finding; never screened is a gap** — the tool must distinguish them. |
| `get_screening_matches` | ScreeningMatches | Match quality, list type, transliteration, secondary identifiers. |
| `get_trade_history` | Trades | Conversions: pair, rate, venue, both legs. |
| `get_trade` | Trades | Drill-down on one `trade_id` already seen on a transaction row. |

### Worth adding — same pattern, not yet built here

| Tool | Source | Why triage needs it |
|---|---|---|
| `get_support_tickets` | Support/CRM | A customer who **reported** the transaction is a victim, not a subject. Contact history is what separates APP fraud from complicity — and its absence is evidence too, so `EMPTY` here is meaningful rather than neutral. |
| `get_case_history` | Cases | Prior investigations and outcomes. A previously-cleared identical pattern changes the disposition. |
| `get_filing_history` | Filings/SARs | Prior filings on this customer; continuing-activity obligations depend on it. |
| `get_relationships` | Parties/graph | Shared devices, addresses, counterparties. Mule networks are invisible one customer at a time. |
| `get_rule_performance` | Analytics | This rule's historical false-positive rate. A rule running at 95% FP deserves different scepticism, and stating it beats the model guessing. |
| `get_watchlist_status` | Internal lists | Internal monitoring flags, distinct from external screening. |

Each obeys the same contract: three states, mandatory coverage, scope injected server-side, one
action on one table, and registration in the model-visible list, the interceptor's binding sets
(§54) and the metering allow-list.

---

## 3. Sequencing — how to spend tokens

```
1. get_case_context                     → one call, the always-needed set
2. get_transaction_summary              → shape of activity, no rows
3. drill only where the summary points:
      get_transaction_history(filtered) → the slice that matters
      get_chain_analytics(wallets)      → from the summary's wallet census
      get_trade(trade_id)               → ids already in hand
4. tier 2 by typology
```

- **Summary before rows, always.** Never open with an unfiltered history call.
- **Census, then targeted reads.** One call returning the wallet census beats six blind per-wallet
  probes — and the census is what reveals that six exist.
- **Drill with ids you already hold**, never guessed ones.
- **Cap the tool loop**, sized from the required set plus headroom for case-dependent calls. An
  agent that exhausts its budget mid-retrieval writes a disposition on a partial evidence set —
  the failure the cap exists to bound, not to cause.
- **Do not re-read what the bundle returned.** This needs enforcement rather than instruction:
  expand the bundle into the ledger so the deterministic sweep sees those sources as read.

### What token pressure must not buy

Three shortcuts are tempting and all three are audit failures:

1. Dropping `coverage` to shorten responses — the field set that makes empties interpretable.
2. Returning rows without `record_ids` — a reviewer can no longer verify anything.
3. Letting a summary stand in for itemised review on a case that gets actioned automatically.

---

## 4. What the runtime must record

The tools are half the audit trail; the other half is what the runtime records about their use.
`audit-trail.md` specifies the record itself. Two obligations belong here because they are
properties of the tool layer:

**The ledger is built from the transcript, not from the model's account of itself.** A model asked
what it called will sometimes narrate a call it never made (`production-rules.md` §18). Entries
expanded out of a bundle carry a marker, so "how many calls" and "how many sources" stay separable.

**A deterministic sweep runs after the model**, calling every required source it never attempted,
through the same scope-injected tools. A model that gets nothing from one tool tends to stop
pulling that thread: observed, `get_transaction_history` returned `EMPTY`, so the model had no
destination wallet to pass to `get_chain_analytics` — on the one alert whose entire question was
how much weight a stale, low-confidence, four-hop designation deserved. One empty result silently
removed the decisive source from the review. A prompt instruction cannot guarantee this
(`production-rules.md` §21). An `EMPTY` counts as read; only a source never attempted is swept.

**If a review UI exists, it must query every source the agent can.** A source the agent reads and
the UI does not is data used in a decision that no reviewer can see, and nothing reports it as an
error. Observed here: trade tools were called on nine alerts, and the review UI never queried the
Trades table — so reviewers saw the two transaction legs of a conversion and not the record that
made them a pair.

---

## 5. Worked example — an empty that is not empty

```json
{
  "status": "EMPTY",
  "source": "Transactions",
  "as_of": "2026-08-22T09:01:34Z",
  "record_ids": [],
  "coverage": {
    "rows_total": 212,
    "rows_in_period": 0,
    "rows_returned": 0,
    "basis": "transactions",
    "first_seen": "2024-03-02",
    "last_seen": "2026-08-10",
    "period_requested": {"from": "2019-01-01", "to": "2019-12-31"},
    "truncated": false
  },
  "reason": "queried successfully; no transactions fall in the period requested. This customer's history begins 2024-03-02 — the period asked for precedes it."
}
```

With a bare `[]` this alert closes on a false premise and the sealed record asserts a source was
read that in substance never was.

---

## Checklist for a new evidence tool

1. Three states; `UNAVAILABLE` never collapsed into `EMPTY`.
2. `coverage` required — keyword-only, no default, so a tool that forgets it fails on its first
   call rather than shipping under-instrumented.
3. `first_seen`/`last_seen` **scope-wide**, or null with the sort key named.
4. `basis` names the unit.
5. `period_requested` present, `{}` when unfiltered.
6. `record_ids` sufficient for a reviewer to pull the same rows.
7. Truncation stated in words.
8. One action, one table, scope from the session (`scoped-retrieval.md` §1).
9. Registered in the model-visible tool list, the interceptor binding sets (§54), and the metering
   allow-list.
10. Exercised end to end once — confirming a sealed record actually landed, not that the call
    returned 200 (§52).
