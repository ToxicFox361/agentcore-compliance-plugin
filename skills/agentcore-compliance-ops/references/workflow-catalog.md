# Workflow catalog: AI agents in compliance operations

Candidate workflows for a financial-crime compliance operations platform — alert, case and
SAR management, transaction monitoring, customer risk assessment, ongoing and enhanced due
diligence, and fraud detection.

Each entry states the orchestration shape, why an agent beats a rules engine there, the
human-in-the-loop position, and the failure mode that matters most. Ordered roughly by
implementation difficulty.

**A note on framing.** Compliance work is not a domain where "the model decides". Almost every
workflow below is *decision support*: the agent assembles, drafts, ranks or explains, and a
qualified human disposes. The regulatory obligation stays with the human, so the design goal
is to make their judgement faster and better-evidenced — not to remove it.

---

## Orchestration shapes

Six shapes cover nearly all of these. Pick the simplest that fits.

| Shape | Control flow | Fits |
|---|---|---|
| **Single-turn analyst** | one prompt, one structured response | triage, classification, scoring explanation |
| **Enrichment pipeline** | fan out to N read-only sources, then synthesize | investigation prep, ODD refresh |
| **Draft + adversarial review** | generator agent, then independent critic | SAR narratives, anything filed externally |
| **Supervisor / specialist** | router delegates to domain specialists | mixed-typology cases, multi-product firms |
| **Batch orchestration** | scheduled fan-out over a work queue | periodic reviews, portfolio re-scoring |
| **Interactive copilot** | multi-turn, session-scoped, tool-using | case investigation, analyst Q&A |

---

## 1. Alert triage and disposition recommendation

**Shape:** single-turn analyst.
**Difficulty:** low-to-moderate. **Value:** high — the highest-volume judgement workflow, and the
one with the clearest evaluation signal, which is why it is the usual first agent to reach
production.

Agent receives a monitoring alert with customer KYC, account profile, device and network
intelligence, and transaction history. Returns a structured disposition: recommendation,
risk score, typology, red flags, mitigating factors, recommended actions, and a rationale.

**Why an agent:** rules engines fire on thresholds but cannot weigh a *combination* of weak
signals against mitigating context. The judgement "five deposits just under the reporting
threshold from two new counterparties, but a 14-month clean history and a plausible income
source" is exactly the synthesis a rules engine cannot express and an analyst does in their head.

**Human position:** agent recommends, analyst disposes. Never auto-close.

**Design notes:**
- Force structured output with an explicit schema. Free-text dispositions are unreviewable and
  unmeasurable.
- Require `mitigating_factors` as a schema field. An alert write-up with none is usually an
  incomplete review, and mandating the field surfaces one-sided reasoning.
- Separate the *identity* question from the *activity* question. Account takeover and money
  laundering share signals but demand opposite responses — one protects the customer, the other
  investigates them.
- Prompt explicitly against geographic bias. Location correlates with nothing on its own, and a
  model that treats a country as a red flag is both bad analysis and a fair-lending problem.

**Failure mode to watch:** confident recommendations that miss a whole typology thread. A
repeatedly observed pattern: a smaller model identifies account takeover correctly and entirely
misses concurrent structuring in the same fixture — the answer it gives is right, and incomplete,
and reads as complete. Evaluate on *reasoning coverage*, not just the verdict.

---

## 2. Alert enrichment and context assembly

**Shape:** enrichment pipeline.
**Difficulty:** low. **Value:** high — often the biggest time saving available.

Before a human opens an alert, an agent gathers everything they would have looked up: customer
profile and risk rating, related parties, prior alerts and their dispositions, recent
transaction patterns, sanctions and adverse-media status, open cases on the same subject.

**Why an agent:** this is not judgement, it is fetching from six systems and writing a summary.
Analysts routinely spend more time assembling context than deciding on it.

**Human position:** none required. Output is context, not conclusion — a good first automation
because a wrong summary is visible and cheap.

**Design notes:** every fetch goes through the platform's authenticated API so tenant isolation
and audit logging apply (see `deployment-patterns.md`). Cache per alert; enrichment is
read-heavy and re-run often. Cite sources inline so the analyst can verify rather than trust.

---

## 3. Case investigation copilot

**Shape:** interactive copilot, session-scoped.

An analyst works a case conversationally: *"show me every counterparty this customer paid more
than once", "what changed on this account before the first alert", "summarise what we know
about this entity"*. The agent holds case context across turns and calls read tools.

**Why an agent:** investigation is inherently exploratory. The next question depends on the last
answer, which is precisely what a fixed screen cannot serve.

**Human position:** human drives throughout; the agent never writes to the case.

**Design notes:**
- One session per analyst per case. Session isolation is the security boundary — see
  `production-rules.md` §7 on deriving session IDs server-side.
- Read-only tools only. Case mutations go through the platform's normal audited paths.
- Persist a transcript to the case record. In a regulated setting, what the analyst was told
  during an investigation is itself evidence.

---

## 4. SAR narrative drafting with adversarial review

**Shape:** draft + adversarial review (two agents, distinct roles).

One agent drafts a suspicious activity report narrative from the case record. A second,
prompted to *find deficiencies rather than approve*, checks it: unsupported assertions, missing
required elements, the five Ws, facts not evidenced in the case file, conclusory language.
The human sees the draft **and** the critique.

**Why an agent:** narrative writing is genuinely time-consuming and highly templated. It is
also the highest-risk output in the workflow, which is why it gets a second pair of eyes.

**Human position:** mandatory review and sign-off. This is a regulatory filing — never auto-file,
and make sure the UI cannot be mistaken for one that does.

**Design notes:**
- The reviewer must be a *separate invocation with an adversarial prompt*. Asking one agent to
  self-check produces agreement, not review.
- Ground every factual claim in a case artefact and cite it. An unciteable sentence should be
  flagged, not silently emitted.
- Keep the draft, the critique, and the human's edits. The delta between draft and filed version
  is valuable quality signal and likely examinable.

**Failure mode:** fluent narratives that assert facts the case does not support. Fluency reads
as confidence to a reviewer under time pressure — which is exactly why the critic exists.

---

## 5. Customer risk assessment explanation and challenge

**Shape:** single-turn analyst, two variants.

*Explain:* turn a CRA score into plain-language reasoning — which factors drove it, by how
much, and what would change it.

*Challenge:* given the customer's full profile, argue the score is wrong. Surface factors the
model missed or overweighted.

**Why an agent:** scoring stays deterministic and auditable in the rules engine, where it
belongs. The agent explains and stress-tests, which is where scoring models are weakest and
where regulators increasingly ask questions.

**Human position:** explanation is advisory; any score override follows the existing approval
path unchanged.

**Design notes:** the agent must never mutate the score — determinism is the point of a CRA
engine. Feed it the factor weights, not just the total, or the explanation is confabulated.
The challenge variant is a good periodic model-validation input.

---

## 6. Periodic ODD / EDD review orchestration

**Shape:** batch orchestration with per-item pipelines.

A scheduled job selects customers due for review, then for each one runs an agent that assembles
the review pack: profile changes since last review, transaction behaviour versus expected,
adverse media, sanctions re-screen, source-of-funds consistency. It drafts findings and, where
the picture is unchanged and low-risk, proposes a no-change conclusion.

**Why an agent:** periodic review is high-volume and mostly confirms nothing has changed. The
scarce resource is reviewer attention, and it should go to the exceptions.

**Human position:** approval required. Risk-tiered — low-risk no-change reviews can be
lighter-touch, elevated risk always gets full human review.

**Design notes:**
- Fan out per customer with bounded concurrency; respect model quota (`production-rules.md` §8, §10)
  *and* the AgentCore session creation rate, which is a per-account quota shared across endpoints —
  see `deployment-patterns.md`. A fan-out sized against model quota alone will still throttle.
- Drive the schedule with [EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html)
  and a queue rather than a single long-running job, so an individual customer's review can fail,
  retry and land in a [dead-letter queue](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
  without stalling the batch. In a regulated setting a silently dropped periodic review is a
  missed obligation, so the DLQ is a compliance control, not just operational hygiene — alarm on
  depth and reconcile it against the population that was due.
- Make "nothing changed" a first-class, well-evidenced output. Reviewers must be able to trust it
  or the automation is worthless.
- Route anything the agent is uncertain about to full review rather than letting it guess.
  Calibrate the threshold conservatively; an agent that never escalates is not confident, it is
  miscalibrated.

---

## 7. Real-time fraud decisioning support

**Shape:** single-turn analyst under a hard latency budget.

At transaction or login time, assess whether to allow, block, or step up authentication, using
device, network, behavioural and historical signals.

**Why an agent:** step-up decisions balance friction against loss, and the right call depends on
context a threshold cannot capture.

**Human position:** none in the moment — latency forbids it. Human review happens after the fact,
on sampled and disputed decisions.

**Design notes:**
- **This is the workflow where latency is a correctness constraint.** Budget end to end and fail
  safe to a deterministic rule when the budget is exceeded. See `production-rules.md` §11.
- A blocked legitimate customer is a real cost. Prefer step-up over block wherever it is
  defensible; step-up is recoverable, a block is a support call.
- Keep a deterministic fallback path that works when the model is unavailable. Model outage must
  not mean payment outage.

**Honest assessment:** the hardest workflow here and the last one to attempt. Sub-second budgets,
availability requirements and adversarial counterparties. Build the batch workflows first.

---

## 8. Transaction monitoring rule tuning and backtesting

**Shape:** interactive copilot plus batch evaluation.

An agent proposes rule threshold changes, then explains the expected effect: how many current
alerts the change would suppress, what historical true positives it would have missed, which
segments are affected.

**Why an agent:** rule tuning is currently expert-and-spreadsheet work. The agent handles the
counterfactual analysis and explains the trade-off in reviewable terms.

**Human position:** mandatory approval. Rule changes alter the firm's detection surface and are
directly examinable.

**Design notes:** the agent must not deploy rules. It proposes; the existing change-control path
approves. Ground every claim in an actual backtest over historical data — an unbacktested
threshold recommendation is worse than none, because it looks quantitative.

---

## 9. Quality assurance sampling and consistency review

**Shape:** batch orchestration.

Sample closed alerts and cases; assess whether the disposition is supported by the evidence,
whether the rationale is adequate, and whether similar fact patterns received consistent
outcomes across analysts.

**Why an agent:** QA sampling is under-resourced almost everywhere, and consistency across
analysts is nearly impossible to assess manually at volume.

**Human position:** QA lead reviews flagged items. The agent narrows the field.

**Design notes:** report *inconsistency* rather than declaring correctness — "these two similar
cases were dispositioned differently" is actionable and defensible in a way that "this decision
was wrong" is not. This workflow also produces the best training signal for improving the triage
prompts in §1.

---

## 10. Multi-typology case routing

**Shape:** supervisor / specialist.

A supervisor agent classifies incoming work by typology — structuring, trade-based, fraud,
sanctions nexus, terrorist financing — and delegates to a specialist agent with the relevant
domain prompt, tools and reference material.

**Why an agent:** typology expertise does not generalise. A prompt tuned for trade-based
laundering is materially different from one for account takeover, and one prompt covering both
does neither well.

**Human position:** as per the underlying workflow the specialist performs.

**Design notes:** the classic failure is a supervisor that answers instead of delegating —
constrain it to routing only. Handle multi-typology cases explicitly; real cases frequently
present two threads at once, and the §1 failure mode (missing a concurrent typology) is exactly
what this shape is meant to fix. Only adopt this once single-agent workflows are working; the
added indirection costs latency and debuggability.

---

## Sequencing

Build in roughly this order:

1. **Enrichment (§2)** — no judgement, immediate time saving, safe place to learn the platform.
2. **Alert triage (§1)** — highest volume, clearest evaluation signal.
3. **Case copilot (§3)** — builds on the same read tools.
4. **CRA explanation (§5) and SAR drafting (§4)** — high value; drafting needs the review
   discipline established first.
5. **Batch reviews (§6)** and **QA (§9)** — need reliable single-item workflows underneath.
6. **Rule tuning (§8)**, **routing (§10)**, **real-time fraud (§7)** — hardest, most constrained.

The pattern: start where being wrong is cheap and visible, and earn the right to workflows where
being wrong is expensive or invisible.

---

## Cross-cutting requirements

Applies to every workflow above.

**Explainability.** Every output carries a rationale traceable to inputs. "The model said so" is
not a disposition an examiner will accept.

**Auditability.** Persist inputs, outputs, model ID and version, and the human decision that
followed. Model version matters — a disposition made under one model is not evidence about
another.

**Verified wiring, not reported status.** Before a workflow goes live, prove it does what the
design claims by exercising it, in both directions — the permitted path succeeds, the forbidden
path is refused. Resources reporting `READY`, `ACTIVE` or `ENFORCE` prove only that they loaded.
This matters more here than in ordinary software because these workflows are defined as much by
what the agent *cannot* do as by what it does: "the agent cannot close an alert" is a claim you
will eventually have to evidence, and the only evidence is a recorded attempt that failed. Two
specifics worth building in from the start: an agent's narrative that an action succeeded is never
evidence the action occurred, and the absence of a CloudWatch log group for a tool's Lambda is
unambiguous proof it was never invoked. See `deployment-patterns.md` and `production-rules.md` §18.

**Evaluation.** Golden fixtures with known-correct answers, run on every prompt or model change.
Grade the reasoning, not just the verdict; a right answer for wrong reasons is worse than a wrong
one, because it survives review.

**Human authority.** No agent closes an alert, files a report, changes a risk rating, or deploys
a rule. The tooling should make that structurally impossible, not merely discouraged.

**Model choice per workflow.** Enrichment and summarisation run fine on a small, cheap model.
Triage and narrative drafting need a stronger one. Benchmark per workflow on identical fixtures —
observed differences are large and not intuitable from benchmarks.

**Tenant isolation.** Every data access is scoped to one tenant and travels the platform's
authenticated, audited path. See `deployment-patterns.md`.
