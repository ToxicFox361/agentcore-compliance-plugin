# Guardrails for AI in regulated compliance decisions

Controls for AI agents operating inside a supervised financial-crime compliance function.
These are not general LLM safety advice. They exist because a compliance decision is
examinable: a regulator can ask, years later, why a particular alert was closed, and the
answer has to be reconstructable and defensible.

The organising principle: **an agent proposes, a human disposes, and the architecture makes
the reverse impossible.**

---

## Layer 1 — No disposition authority (primary control)

The agent must be structurally incapable of closing an alert, filing a report, changing a risk
rating, or deploying a rule. Not "discouraged by prompt" — incapable.

Implementation:
- The model's output and the human's decision are **separate types with no code path that
  converts one into the other.** A model produces a *proposal*; a human authors a *decision*
  under their own identity. There is no function that promotes the former to the latter.
- Agent tool access is read-only. Mutations travel the platform's normal audited write paths,
  triggered by a human action.
- **Enforce that read-only property in the tool list the model is offered, never in the prompt.**
  A model can only call a tool it has been given, so filter write tools out of the list at client
  construction; where a deterministic layer must perform the write, let it call the tool directly
  through its own unfiltered client. Tool filters apply when tools are listed for the model, not to
  direct invocation, which is exactly what makes that split work. An instruction like "do not call
  `create_case`" has been observed being ignored — and, because the sanctioned path was broken at
  the time, that off-script call was the only thing writing records at all
  (`production-rules.md` §21). Stronger still, put a policy engine in front of the tool boundary so
  enforcement lives outside agent code entirely — and verify it empirically in both directions,
  because a policy whose condition never matches is indistinguishable from no policy
  (`production-rules.md` §23).
- Where an agent participates in an automated workflow graph, validation must reject any graph
  where an AI node reaches a decisioning node without an intervening mandatory human-review node.
  Enforce in graph validation, not convention.

Why this is first: every other control is defence in depth. If this one holds, the worst case is
a bad draft that a human rejects. If it fails, nothing downstream saves you.

---

## Layer 2 — Bounded assertion types

Constrain what kinds of claim the model is permitted to make. A useful bounded set:

- **Observation** — a fact drawn from supplied evidence. *"Five deposits between 9,700 and 9,900
  occurred across three days."*
- **Consistency note** — how evidence relates to a known typology or the customer's own baseline.
  *"This pattern is consistent with threshold-avoidance structuring."*
- **Gap** — something absent that a reviewer should know is absent. *"Source of funds for the new
  counterparty is not documented."*

Explicitly excluded: assertions of **intent** and **legal conclusions**. "The customer was
laundering money" is not an evidence-supported statement, it is a determination reserved for
humans and ultimately courts. The distinction is not pedantry — it is the difference between a
defensible work product and one that prejudices an investigation.

Encode this in the system prompt *and* in the output schema, so violations are detectable rather
than merely discouraged.

---

## Layer 3 — Strict typed output, no silent repair

Every agent output conforms to an explicit schema, validated deterministically after generation.

- On validation failure: **bounded retry, then flag for human.** Never silently repair, never
  coerce a malformed response into a valid-looking one. A response the model could not produce
  correctly is a signal, not an inconvenience.
- Required fields should include the ones that surface poor reasoning: mitigating factors,
  confidence, explicit gaps. An assessment that lists no mitigating factors is usually incomplete,
  and mandating the field makes one-sidedness visible.
- Schemas are versioned alongside prompts. A stored output is only interpretable against the
  schema in force when it was produced.

---

## Layer 4 — Untrusted input containment

Customer-supplied and counterparty-supplied text — transaction references, names, memo fields,
uploaded documents, adverse-media extracts — is **hostile input**. Assume some of it is written
by the subject of the investigation.

- Never place untrusted text in the instruction channel. It goes in a clearly delimited data
  region with explicit framing.
- Instruct the model that content inside the data region is evidence to analyse, never
  instructions to follow.
- Validate outputs for signs of injection success: recommendations inconsistent with the evidence,
  sudden tone shifts, references to instructions not in the system prompt.

This is not theoretical. A transaction memo field is attacker-controlled, reaches your model, and
in a monitoring context the attacker has direct motive to suppress an alert.

---

## Layer 5 — Deterministic post-generation validation

After the model responds, before a human sees it, apply rules the model cannot override.

- **Citation grounding.** Every factual claim must trace to a supplied evidence item. Flag
  anything unciteable rather than displaying it as fact.
- **Action grounding.** Every *action* the output asserts must trace to a tool result, and the tool
  result must be corroborated by evidence that the tool actually ran. An agent narrating a write it
  never performed is not hypothetical — it has been observed, with a second reviewing agent scoring
  the fabricated narrative highly. Two checks, in order: the record exists in the system of record,
  and the tool's invocation count inside the run's time window is what it should be. Existence
  alone is not attribution — a concurrent session or an off-script model call can produce the
  record — and an in-window count of two where one was expected means two writers. Never let a
  failed tool call become a placeholder value that reads like data; raise, so the failure surfaces
  where it happened. See `production-rules.md` §18, §20 and §22.
- **Categorical blocks.** Defined trigger conditions must block a clear/low-risk recommendation
  regardless of model confidence — for example a sanctions hit, a prior filed report on the same
  subject, or a threshold breach. These are policy decisions expressed in code, not prompt
  suggestions.
- **Consistency checks.** A recommendation inconsistent with its own stated red flags is a defect;
  catch it mechanically.

Model confidence is not evidence. Treat a high-confidence clear on a categorically-blocked fact
pattern as a defect in the pipeline, not a judgement to respect.

---

## Layer 6 — Continuous monitoring

Ship the measurement with the feature.

- **Golden set.** Held-out fixtures with known-correct answers, re-run on every prompt, model or
  schema change. Grade the *reasoning*, not just the verdict — a right answer for the wrong
  reasons survives review and fails later.
- **Analyst agreement rate.** Track how often humans accept the recommendation. Falling agreement
  means degradation. **Agreement approaching 100% is also a warning** — it usually means automation
  bias, not excellence. Humans who stop genuinely reviewing are a control failure that looks like
  success.
- **Mandatory disagreement rationale.** When a human overrides, require a reason. This is the
  single highest-value quality signal available, and the primary input for prompt improvement.
- **Drift monitoring.** Distribution of recommendations over time. A sudden shift in the
  clear-versus-escalate ratio needs explaining before it needs accepting.

---

## Reference knowledge: retrieve, don't bake in

Typology definitions, red-flag indicators, regulatory guidance and internal policy are
**versioned, governed data retrieved deterministically at inference time.**

- Do not fine-tune this knowledge into weights. You lose the ability to say which version of a
  typology definition informed a decision — and that is exactly what an examiner asks.
- Prefer deterministic retrieval (explicit lookup by identifier) over semantic or vector search in
  early versions. Semantic recall is hard to explain, and embedding customer PII creates a
  leakage surface with no compensating control.
- Version the reference corpus and pin the version on every decision record.

---

## Audit record: what to persist per invocation

Store enough to reconstruct the decision exactly, months later:

| Field | Why |
|---|---|
| Model ID **and** version | A decision under one model is not evidence about another |
| Inference parameters as sent (`maxTokens`, `temperature` or `topP`, `stopSequences`) | The same model at different settings is a different system — and with no seed available, the record is the only reconstruction (`production-rules.md` §24) |
| Prompt/template version | Prompts change; decisions must pin the one in force |
| Full evidence set supplied | The reconstruction is meaningless without its inputs |
| Raw model output | Before any post-processing |
| Tool calls made, with results | What the run actually did, as distinct from what it narrated |
| Validation result | Which checks ran, what passed |
| Reference-data version | Which typology definitions applied |
| Human decision, actor, timestamp | The decision of record |
| Disagreement rationale, when overridden | Quality signal and examiner evidence |

Persist to an append-only store. Compliance audit trails should be tamper-evident — a database
trigger rejecting `UPDATE`/`DELETE` for every role is stronger than a revoked grant, because a
future migration can silently re-add a grant but cannot silently remove a trigger.

---

## Model risk governance

Once AI influences regulated decisions, it falls under model risk management:

- **Inventory.** Each agent is a registered model with an owner, purpose and risk tier.
- **Independent validation.** Reviewed by someone other than the team that built it. Second line,
  not the builders.
- **Scheduled revalidation.** Models and prompts drift; providers update models underneath you.
- **Bias assessment before launch.** Particularly for factors correlating with geography,
  nationality or ethnicity. A model that treats a country as a risk factor creates disparate-impact
  exposure — and is usually also just wrong, since the real signal is the behaviour, not the place.
- **Change control.** Prompt changes are production changes. Review, version, and be able to roll
  back.

---

## The graduation question

Eventually someone asks whether the agent can auto-close the lowest-risk category. The answer is
not permanently no — but the path matters.

- Name the seam in advance, narrowly. One outcome type, lowest stakes, explicit criteria.
- Gate on **measured** reliability against the golden set and sustained analyst agreement, not on
  intuition that it seems good now.
- Require standing quality sampling after it opens. Automation without sampling is unmonitored
  risk.
- Open it with a fresh, documented, reviewed decision — never a feature flag flipped quietly.

Design the seam early even if you never open it. Retrofitting a safe graduation path into a system
built without one means rebuilding the control structure.

---

## Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Prompt says "do not close alerts" | Prompts are not access control — filter the tool list instead |
| Agent writes directly to the case record | No separation between proposal and decision |
| Failed tool call returned as a placeholder value | A failure that reads like data ships silently |
| Agent narrative accepted as evidence of an action | Verify the record *and* the in-window invocation count |
| Policy assumed effective because it deployed | Loaded and enforcing is not the same as matching — test both directions |
| Self-review ("check your work") | Produces agreement, not review — use a separate adversarial invocation |
| Semantic search over customer PII, early on | Unexplainable recall plus a leakage surface |
| Fine-tuning typologies into weights | Destroys version pinning and explainability |
| Confidence score as an approval gate | Confidence is not calibrated to correctness |
| Temperature 0 treated as a reproducibility guarantee | Greedy decoding narrows variance; with no seed it does not replay a run |
| One prompt for all typologies | Dilutes every one; miss rate is invisible |
| Shipping without a golden set | No way to detect degradation until an examiner does |
