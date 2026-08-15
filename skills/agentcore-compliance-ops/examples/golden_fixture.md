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

### Expected output

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

Run on every prompt, model, schema or reference-data change. Track per-fixture
results over time, not just aggregate score — an aggregate that holds steady
while individual fixtures flip is a model that has changed behaviour without
changing accuracy, which is exactly what you need to catch.
