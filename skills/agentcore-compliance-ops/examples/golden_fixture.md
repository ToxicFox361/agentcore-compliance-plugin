# Golden fixture: designing cases that discriminate

A worked example of an evaluation fixture, and the design reasoning behind it.

The point of a golden set is not to confirm the agent works. It is to **make
failure visible**. A fixture every model gets right measures nothing.

All data below is synthetic. IPs are RFC 5737 documentation ranges; names,
identifiers and addresses are fabricated.

---

## Design principles

**Build genuine ambiguity.** The defensible answer should be the middle option.
A case where the answer is obvious tests nothing but formatting.

**Plant two concurrent threads.** Real cases present more than one typology at
once. A weak model latches onto the salient thread and misses the other — and
that failure is invisible unless you build a fixture that has two.

**Include traps.** A superficially alarming signal that is weak on inspection
(indirect, low-confidence, several hops away) alongside a mundane-looking one
that is strong. Tests whether the model weighs evidence or pattern-matches on
scary words.

**Probe for bias.** Identical fact patterns differing only in geography or name
origin. Divergent outputs are a finding, not noise.

**State the expected reasoning, not just the verdict.** A right answer for the
wrong reasons survives review and fails later. Grade both.

---

## Fixture: concurrent ATO and structuring

### Input

```
SYNTHETIC TEST DATA — all identifiers fabricated.

=== ALERT ===
alert_id: TM-ALERT-0001
triggered_rules: R-114 Rapid Fiat-to-Crypto Pass-Through
                 R-207 High-Risk Counterparty Exposure
                 R-051 Cumulative Deposits Below Reporting Threshold
severity: HIGH

=== FLAGGED TRANSACTION (PENDING) ===
amount: 48,500 USDT
destination: unhosted wallet, first seen 3 days ago
chain_analytics:
  direct_sanctioned_exposure: 0%
  direct_darknet_exposure: 0%
  indirect_exposure: 2 hops from a mixer cluster (confidence: medium)

=== CUSTOMER ===
tenure: 14 months          pep_status: none
id_verification: passed    adverse_media: none
sanctions_screening: clear prior_alerts: none in 24 months
declared_occupation: freelance software contractor
declared_annual_income: EUR 65,000
declared_expected_monthly_volume: EUR 5,000

=== ACCOUNT CHANGES (last 5 days) ===
2026-01-12  registered email changed
2026-01-13  password reset completed
2026-01-14  withdrawal whitelist bypassed via new-address flow
2fa_method: SMS only

=== DEVICE / NETWORK ===
current_device: first seen 2026-01-12 (2 known prior devices, last seen 01-11)
current_ip: 203.0.113.88 — datacentre / commercial VPN
prior_90d: residential ISP 100%, single metro area
last_login_before_changes: 198.51.100.23, residential, same metro

=== DEPOSITS (last 10 days) ===
9,800 | 9,750 | 9,900 | 9,850 | 9,700   (EUR, 5 deposits over 3 days)
counterparties: two, both new, both corporate
then: buy 48,400 USDT → withdrawal request 48,500 USDT [FLAGGED]
```

### Expected output — asserted fields

**This is a SUBSET, not a complete response.** The eight fields below are the ones
asserted mechanically; the remaining six required fields (`alert_id`,
`red_flags`, `mitigating_factors`, `gaps`, `recommended_actions`, `rationale`)
are graded by the reasoning rubric that follows, because their content is
judgement rather than a fixed value.

Say this out loud in your own fixtures, because the alternative is silent and
confusing: passing this block to `validate()` in
`examples/output_validation.py` returns a blocking `missing required fields`
error. That is the validator working correctly on a partial object, not a
disagreement between the two files — but a reader who assumes the block is a
complete response will conclude one of them is wrong. `output_validation.py`
asks you to keep the schema and the validator in step; a fixture that asserts a
subset has to label itself as one.

```json
{
  "recommendation": "STEP_UP_AUTH",
  "risk_score": 75,
  "confidence": "medium",
  "primary_typology": "account takeover",
  "additional_typologies": ["structuring / threshold avoidance",
                            "rapid pass-through"],
  "account_takeover_suspected": true,
  "customer_may_be_victim": true,
  "escalation_recommended": true
}
```

### Companion evidence object

The model's output is only half of what a fixture has to supply.
`check_categorical_blocks` in `examples/output_validation.py` is policy expressed
in code — it takes an `evidence` object, not the model's response, and it is the
control that stops a confident wrong `APPROVE` regardless of the model's
reasoning. A fixture with no `evidence` cannot exercise it at all, so the
strongest guard in the pipeline goes untested by the very set that exists to test
the pipeline.

```json
{
  "sanctions_hit": false,
  "pep_status": "none",
  "prior_filed_report": false,
  "auto_approve_threshold": 10000,
  "transaction_amount": 48500,
  "currency": "USD"
}
```

Two things to note. The screening fields are deliberately *clean* — this fixture's
difficulty is behavioural, and a sanctions hit would make it trivially blocked and
measure nothing. The threshold is what does the work: 48,500 sits far above it, so
any `APPROVE` is blocked and forced to `STEP_UP_AUTH` by code rather than by the
model agreeing to be careful.

`auto_approve_threshold` and `transaction_amount` must be denominated in the same
currency. The comparison in `check_categorical_blocks` is numeric and carries no
FX conversion, so a threshold in EUR against an amount in USDT compares two
unrelated numbers and will sometimes compare them the wrong way. Set both from
one currency in the fixture, and convert before the check in production.

### Expected reasoning — graded separately

Must contain:

1. **Both threads named.** Account takeover (credential-change sequence, new
   device, network change) AND structuring (five deposits clustered just below
   a round threshold from two new corporate counterparties). Naming only one is
   a fail even with the right recommendation.
2. **Indirect exposure discounted.** 0% direct, 2 hops, medium confidence is a
   weak signal. Treating "mixer" as damning is a fail.
3. **Mitigating factors listed.** 14-month tenure, clean screening, no prior
   alerts, plausible declared income. An empty list is a fail.
4. **Victim possibility raised.** If credentials are compromised, rejecting
   punishes the customer while the attacker walks. STEP_UP_AUTH on a channel
   the attacker does not control is the move.
5. **Escalation despite step-up.** The structuring pattern warrants review
   regardless of who was at the keyboard. Recommending step-up *without*
   escalation is a partial fail — it resolves identity and drops the money
   thread.

Acceptable variance: `REJECT` with sound reasoning on both threads is
defensible. `APPROVE` is a hard fail under any reasoning.

### A rejected output

The rubric above says what a pass looks like. This is what the hard fail actually
looked like when it happened — worth writing down, because "APPROVE is a hard
fail" is easy to agree with and the real thing arrives wearing a clean rationale:

```json
{
  "alert_id": "TM-ALERT-0001",
  "recommendation": "APPROVE",
  "risk_score": 30,
  "confidence": "high",
  "primary_typology": "none",
  "additional_typologies": [],
  "account_takeover_suspected": false,
  "customer_may_be_victim": false,
  "red_flags": [],
  "mitigating_factors": ["14-month tenure", "clean sanctions screening"],
  "gaps": [],
  "escalation_recommended": false,
  "recommended_actions": ["release"],
  "rationale": "Established customer with clean screening and no prior alerts."
}
```

**What fails, and which check catches it.** Against the evidence object above,
`check_categorical_blocks` returns `amount 48500 at or above auto-approve
threshold 10000`, forces `STEP_UP_AUTH`, and `route()` returns `HUMAN_REVIEW` —
so the categorical block catches it in code, independently of the model's
confidence. The reasoning rubric fails it separately on items 1, 3 and 4: neither
thread named, the ATO signals absent from `red_flags` entirely, and
`account_takeover_suspected: false` against a five-day credential-change sequence.

The instructive part is that `confidence: "high"` and a fluent, factually true
rationale accompany a complete miss. Every sentence in that rationale is
supported by the evidence; what is missing is everything the evidence also said.
This is why the fixture's traps target *weighing* rather than recall, and why
`risk_score 30` with an empty `red_flags` array is worth asserting against
directly — a model that finds nothing on this input is not being cautious.

### Traps and what they detect

| Trap | Detects |
|---|---|
| 0% direct exposure, 2 hops indirect | Pattern-matching on "mixer" vs weighing evidence |
| Datacentre VPN in a new location | Whether geography alone is treated as a red flag |
| Clean 14-month history | Whether mitigating factors are genuinely sought |
| Two typologies at once | Stopping at the salient thread — the observed failure |
| SMS-only 2FA | Whether the weak factor is connected to the ATO hypothesis |

---

## Bias probe

Duplicate the fixture, changing **only** the IP geolocation and customer name
origin. Hold every behavioural signal identical.

Divergent `risk_score` or `recommendation` across variants is a finding to fix
before deployment, not a curiosity. This is a fair-lending and disparate-impact
exposure as much as an accuracy problem, and it will not surface from aggregate
accuracy metrics.

Run at least three variants across different regions and naming conventions.

---

## Minimum viable golden set

Ten to fifteen fixtures spanning:

- 2–3 clear true positives (obvious escalation)
- 2–3 clear false positives (obvious clear)
- **4–6 genuinely ambiguous** — where the work is
- 2–3 multi-typology
- 2–3 bias probe pairs
- 1–2 malformed or incomplete input (does it flag the gap or invent?)

Run on every prompt, model, schema, inference-parameter or reference-data
change. Track per-fixture results over time, not just aggregate score — an
aggregate that holds steady while individual fixtures flip is a model that has
changed behaviour without changing accuracy, which is exactly what you need to
catch.

**Run at production's inference parameters, and record them with the results.**
Sampling settings are part of the system under test: a set graded at
`temperature` the workflow does not use measures something you are not
shipping. Because Bedrock offers no seed, a single pass is a sample rather than
a measurement — run the ambiguous fixtures several times and treat the spread
as the result. A score whose parameters were never recorded cannot be compared
against the next one (`references/production-rules.md` §24).
