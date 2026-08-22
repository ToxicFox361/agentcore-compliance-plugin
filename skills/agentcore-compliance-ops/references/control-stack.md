# Guardrails for AI in regulated compliance decisions

Controls for AI agents operating inside a supervised financial-crime compliance function.
These are not general LLM safety advice. They exist because a compliance decision is
examinable: a regulator can ask, years later, why a particular alert was closed, and the
answer has to be reconstructable and defensible.

The organising principle: **an agent proposes, a human disposes, and the architecture makes
the reverse impossible.**

> **Name collision, worth clearing up before you read on.** This file is the *control stack* — the
> layered structural controls you build around an agent that sits near a supervised decision. It is
> not about Amazon Bedrock Guardrails, the AWS feature. Layer 5a says where that feature fits in
> this stack and how to stop it being optional; its configuration mechanics — integration modes,
> filter types, `BLOCK` vs `ANONYMIZE`, versioning, KMS, permissions — live in the global
> `amazon-bedrock` skill's `amazon-bedrock/references/guardrails.md`. Same filename, different subject: this one is
> what you build, that one is what you configure.

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

## Layer 5a — Bedrock Guardrails, and making them non-optional

The one control in this stack you *configure* rather than build, and it sits usefully: PII
detection and content filtering on the model call itself, evaluated before a response reaches your
Layer 5 validation. Configuration mechanics are in the global `amazon-bedrock` skill's
`references/control-stack.md`. What follows is what this domain adds on top.

**Applied the ordinary way, it is exactly the kind of control this file argues against.** A caller
that omits `guardrailConfig` from a `Converse` call gets no guardrail *and no error* — AWS's own
words are that guardrails are then "trivially bypassable". That is the same failure shape as the
prompt saying "do not close alerts": a control that holds only while every caller chooses to invoke
it. Make it structural, in increasing order of strength:

1. An IAM `Deny` on `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, conditioned
   on `bedrock:GuardrailIdentifier` not matching your pinned guardrail ARN.
2. Account-level `PutEnforcedGuardrailConfiguration`.
3. AWS Organizations Amazon Bedrock policies, at org level.

The latter two are stronger because they do not depend on every role's policy being written
correctly. Option 1 fails silently the day someone adds a role and forgets the condition, and
nothing about the working system looks different.

Record it accordingly: **"the guardrail was configured" and "the guardrail could not be bypassed"
are different claims, and an examiner asks the second.** Evidence for the second is the enforcement
configuration *plus* a call that was refused for lacking the guardrail — the same both-directions
discipline as `examples/cedar_policies.md` §8.

**Pin a numbered version.** `DRAFT` is mutable and can change without notice. A decision record
citing a guardrail must cite a version for the same reason it cites a model ID and a prompt
version: a stored decision is only interpretable against the configuration in force when it was
made.

**Encrypt the guardrail configuration with a customer-managed KMS key.** AWS-managed keys do not
satisfy customer-managed-encryption requirements in most frameworks — and the denied-topic list
plus custom regex patterns *describe your detection surface*, which for an AML firm is sensitive in
its own right: it tells a reader what you look for.

### Three traps that matter more in this domain than elsewhere

- **`guardContent` narrows what is evaluated, and the narrowing is filter-type-dependent.** Wrap
  only the user's input and content filters, denied topics, PII filters and contextual grounding
  all skip tool results and retrieved context — AWS calls this "a false sense of security". In a
  monitoring workflow the hostile text does not arrive as user input; it arrives in a transaction
  memo through a tool result, which is precisely the channel Layer 4 is about. Worse, **word filters
  still evaluate everything**, so partial coverage passes the obvious test and looks like full
  coverage. Wrap every untrusted region — including the system prompt, which is never evaluated
  unless it carries its own block.
- **Async streaming does not mask PII at all**, and delivers policy-violating content to the reader
  before the guardrail can intervene. Use `sync` stream processing, or keep PII out of the streamed
  path.
- **`trace: "enabled"` returns the original unmasked content that triggered the filter.** Keep it
  off in production, and while it is on treat the whole response — logs included — as PII.

### What Guardrails does not do, and why this is Layer 5a rather than Layer 1

Masking applies to what flows through the API — the input prompt and the model's response both. What
it does **not** reach is your log estate: where Bedrock model invocation logging is enabled, the
`input` field written to CloudWatch Logs holds the original, unmodified request regardless of any
guardrail intervention. Nor does the guardrail see what your own tools returned to your own code.

So the precise claim is that Guardrails reduces what a *reader of the API* sees, not what your
storage holds — log encryption, retention limits and redaction-before-write remain yours to arrange
(see the audit record section below, and `references/audit-trail.md` for the mechanisms). A control
that shapes the API channel cannot be the control the whole stack rests on.

State this carefully in a control narrative. "PII is masked" invites the reading that PII is absent,
and the log estate is the counter-example an examiner will find.

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

Control failures need a queue, and it can be the one you already have.
`BatchImportFindings` (ASFF) is the generally available API for an application to push its own
findings into Security Hub; `BatchImportFindingsV2` (OCSF) is gated on Security Hub Extended program
membership, so confirm eligibility before designing around it. Security Hub CSPM also ships a
standard named `AI Best Practices` — resolve its ARN and version with
`aws securityhub describe-standards` rather than hardcoding, since both move. Worth doing if you
want failures like "a synthesis shipped with an incomplete specialist set" or "a Cedar policy is
sitting in `LOG_ONLY`" to land in the same triaged queue as infrastructure findings, rather than on a
bespoke dashboard nobody has open.

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
| Reasoning trace, where the model emits one | The first thing an examiner asking *how was this concluded* wants to read — but see below |
| Tool calls made, with results | What the run actually did, as distinct from what it narrated |
| Validation result | Which checks ran, what passed |
| Reference-data version | Which typology definitions applied |
| Human decision, actor, timestamp | The decision of record |
| Disagreement rationale, when overridden | Quality signal and examiner evidence |

**A reasoning trace is testimony, not causation.** The model's reasoning text is a distinct artefact
from its answer and worth persisting on its own. But it is model-generated narration of a process,
not a verified causal account of one: it can be post-hoc rationalisation that does not reflect what
actually drove the output. That is the same epistemics as an agent reporting an action it never took
(`production-rules.md` §18) — plausible prose is not evidence of the thing it describes. Record it
as evidence of *what the model said its reasoning was*, never as proof of why the output occurred.
The consequence for control design: **a reasoning trace discharges neither citation grounding nor
action grounding** (Layer 5). Claims still verify against the supplied evidence set, actions still
verify against tool results and in-window invocation counts, no matter how convincingly the trace
accounts for them. Traces are also bulky and carry PII verbatim — keep the text in the immutable
object store and the content hash in the structured row (see the storage split below).

Persist to an append-only store. Compliance audit trails should be tamper-evident — a database
trigger rejecting `UPDATE`/`DELETE` for every role is stronger than a revoked grant, because a
future migration can silently re-add a grant but cannot silently remove a trigger.

### The record is two artefacts, split by a gate

**The record that answers an examiner and the record that sits in the cloud provider's logs are
different artefacts with different contents.** Treating them as one record stored in two places is
the mistake, because the two have incompatible requirements: the examinable record must hold the
reasoning and the narrative verbatim, and the provider-side log must hold no narrative at all. So
the design question is not "how much of the record can we redact before logging it" but "which two
artefacts are we producing".

The split has to be **deterministic and structural — an allowlist gate, not the model's
cooperation.** A compliance output carries PII by construction: `rationale`, `red_flags[].statement`
and `gaps` are prose about a named person's transactions, so "the output contains no PII" cannot hold
for the whole output. Nor can it be secured by telling the model to keep PII out of the structured
fields; that is a prompt instruction, and by the argument running through Layers 2, 3 and 5 an
instruction is a request with no enforcement and no evidence of having operated. An allowlist also
fails in the right direction on change: a schema field nobody has classified yet is diverted, where a
denylist of PII field names leaks it on the deploy that introduces it.

**AWS-side holds identifiers, counts, usage and a content hash** — the workflow invoked, the tenant,
customer, alert and run identifiers, the row UUIDs read as references, tool names called, token
counts, cost, latency, and the enum and numeric output fields, plus counts of narrative items rather
than the items. **The firm's own encrypted store holds the reasoning trace, the narrative output and
the retrieved evidence**, under a tenant-scoped key.

**The pairing between the two halves is an HMAC content hash**, carried on the metering row and on
the bundle, so neither half can be swapped for another run's without detection. Keyed rather than a
plain digest, because an unkeyed hash of a small guessable value is a lookup oracle for that value —
and because a verification artefact should share the subject's shredding lifecycle rather than outlive
the evidence it checks. Note also what a hash does not do: it gives tamper evidence and not
retrievability, so a record that is only hashed satisfies the integrity obligation and fails the
production obligation. You cannot show an examiner a hash. Hash for pairing, encrypt for retrieval,
always both.

`examples/log_projection.py` implements the gate — the allowlist with its type predicates, the
diversion of anything that fails one, the last-resort sweep before emission, and the deployment
profile whose permissive path must positively assert that its data is synthetic. `audit-trail.md`
carries the mechanism detail: the storage split, retention, the provider-side logging settings this
rule forces off, and the key-granularity decisions that follow.

One consequence worth stating because it is routinely missed: **UUIDs are pseudonymous, not
anonymous.** With the mapping table in the firm's own store a customer UUID still resolves to a
person, which puts the metering store inside the privacy perimeter and in scope for erasure.

### Case-level records for multi-agent workflows

A per-invocation record is necessary and not sufficient. Where several specialists run concurrently
on one subject — network and link analysis, customer history, document parsing, OSINT — a
synthesiser writes the case up, and a QA agent verifies that write-up against the data, the
examinable unit is the **case**, not any single invocation.

- **Parent record over child records.** One case-level record aggregating N specialist runs, each
  child keeping its own per-invocation record from the table above. The join key is the trace ID.
- **Propagate W3C Trace Context across every agent boundary** — this is what makes the hierarchy
  real rather than nominal. Propagated, a request through five agents is one trace with five spans;
  unpropagated it is five disconnected traces, and correlation degrades to matching timestamps,
  which produces wrong answers exactly when concurrent requests overlap — the normal condition for
  a fan-out. AWS states this directly:
  [AGENTOPS05-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html).
- **Per-fact attribution.** Every fact reaching the synthesiser carries four things: which
  specialist asserted it, the tool result or source it came from, the confidence, and the retrieval
  timestamp. Without it the synthesiser can silently manufacture a link between two specialists'
  outputs that neither asserted, and the QA agent has nothing to check against — it can only re-read
  the prose. Per-fact attribution is what makes Layer 5 citation grounding hold across a multi-agent
  merge rather than only within one invocation.
- **A partial evidence set is recorded as partial.** The case record states which specialists ran,
  which failed, and that the synthesis was over an incomplete set. A synthesis over three of four
  specialists presented as complete is the same failure class as the sweep that quietly covered 94%
  of the portfolio (`architecture-patterns.md` pattern 6) — a compliance failure that looks like a
  success.
- **Split the storage by shape.** Structured, queryable rows go to a relational store with a
  `BEFORE UPDATE OR DELETE` trigger that raises, for the reason given above. The bulky immutable
  bundle — raw specialist outputs, reasoning traces, any captured screenshots — goes to **S3 Object
  Lock**, which is WORM. Record a content hash of each bundle object in the structured row, so the
  pair is verifiable and neither half can be swapped unnoticed. AWS recommends the same split —
  Object Lock for compliance-critical records, tiered retention rather than one blanket policy, and
  PII redacted *before* write, naming PII inside reasoning traces as an anti-pattern outright:
  [AGENTOPS05-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html).
  Which Object Lock *mode* is a legal decision rather than a default — see below.
- **The DynamoDB fallback is the weaker form, and it matters why.** An IAM deny on
  `UpdateItem`/`DeleteItem` approximates append-only, but there are no triggers, so the control lives
  entirely in policy that a later change can re-grant. Its compensating control is CloudTrail
  **data** events on item-level operations — and those are **off by default**. An unenabled
  compensating control is not a weaker control, it is *no* control, and it is the kind of gap that
  reads as fine in a design document and is discovered on the first evidence request.

The OTEL trace store cannot be the case record, for a reason beyond the retention and schema
arguments in `deployment-patterns.md` (observability as audit evidence): AWS's own guidance
recommends **sampling** — 100% of error traces plus a configurable percentage of successful ones —
and separate retention tiers for operational, compliance and debug telemetry
([AGENTOPS05-BP01](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html)).
Sampled telemetry is not an audit trail.

### S3 Object Lock: the parameters, and the mode decision

**Enable it before you need it — but retrofitting is possible, and the folklore that it isn't makes
teams abandon a remediation they could still perform.** Object Lock requires versioning, and it must
be enabled before you *lock* any objects. That is a weaker precondition than the common reading:
Object Lock can be turned on for an existing bucket, and existing object versions can then be
protected individually with `PutObjectRetention` or in bulk with S3 Batch Operations. So a platform
that has been writing evidence to an unlocked bucket for a year is not out of options — it has a
backfill job, and the backfill is worth doing precisely because the unprotected window is the part an
examiner will ask about.

Treat it as a day-one decision anyway. Retrofitting protects the versions that still exist; it cannot
protect what was already deleted, and it leaves a documented gap between first write and first lock.

```bash
aws s3api put-bucket-versioning \
  --bucket <bucket> \
  --versioning-configuration Status=Enabled

aws s3api put-object-lock-configuration \
  --bucket <bucket> \
  --object-lock-configuration \
    '{"ObjectLockEnabled":"Enabled","Rule":{"DefaultRetention":{"Mode":"GOVERNANCE","Years":7}}}'
```

- `Mode` is `GOVERNANCE` or `COMPLIANCE`.
- The period is `Days` **or** `Years` — never both.
- Per-object overrides go on `PutObject` as `x-amz-object-lock-mode` and
  `x-amz-object-lock-retain-until-date`, or afterwards via `PutObjectRetention`. An explicit
  per-object setting overrides the bucket default, so treat the default as a floor for objects
  written without one, not as a statement about every object present.

**The mode choice is a legal decision, not a default.** They are not "strict" and "less strict" —
they answer different questions.

In `COMPLIANCE` mode WORM protection cannot be removed by any user, including the account root, and
retention can be extended but never shortened. Say the consequence out loud before choosing it:
**for the whole retention period an erasure request cannot be honoured**, except by crypto-shredding
the key, which takes the surrounding records with it. Use it only where the record-keeping
obligation is documented as overriding the erasure right, and where a named person owns that
determination.

`GOVERNANCE` mode is usually the defensible default for PII-bearing evidence — paired with
`s3:BypassGovernanceRetention` and an explicit `x-amz-bypass-governance-retention:true` header, held
by one break-glass role, under a two-person rule, with an alarm on use. That yields an auditable
erasure path that ordinary principals do not have, which is the property you actually want, rather
than an absolute one you cannot operate.

Three further facts decide whether the control behaves as designed:

- **S3 Lifecycle configurations cannot bypass governance retention.** A locked object version cannot
  be quietly expired by a lifecycle rule, so a retention policy and a cost-cleanup rule cannot
  silently contradict each other.
- **Legal hold (`s3:PutObjectLegalHold`) is the primitive for an indefinite hold** — a filed report
  under review, a litigation hold. No fixed duration, independent of retention mode, and it survives
  retention expiry. But anyone holding the permission can remove it, so it is never a substitute for
  a retention period; it sits on top of one.
- **A plain `DELETE` writes a delete marker over an intact protected version** — hidden, not gone.
  Decide explicitly whether your evidence-retrieval procedure lists current objects or lists
  versions. One that lists current objects will report "no record" for a record that is sitting
  there, protected, and to an examiner that is a worse answer than either the truth or a real gap.

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

### The inventory does not have to be a spreadsheet

Inventory, independent validation and change control are requirements about *state transitions on a
record* — not about a document format. `architecture-patterns.md` builds that approval state machine
by hand. AgentCore **Registry** ships it as a managed service, with the identical state names:

```
DRAFT → PENDING_APPROVAL → APPROVED
              ↓
           REJECTED                   DEPRECATED (terminal)
```

**The governance mode is the control, not a setting.** Under **manual approval** a record is not
discoverable until it is approved — that is the maker-checker gate, enforced by the service instead
of by convention. **Auto-approve** removes the gate, and belongs only in isolated development
accounts.

What that buys over maintaining a parallel system:

- Records carry type, description, endpoint, tags and capability metadata — enough to hold owner,
  purpose and risk tier without a second inventory that drifts from the first.
- CloudTrail records who registered and who approved. That is evidence; a spreadsheet cell is an
  assertion.
- Resource types cover agents, MCP servers and agent skills, so tools and retrieved reference
  material are inventoried on the same terms as the agents that use them.

Three caveats to state before an examinable control rests on it:

- It is a **Preview** feature. Confirm Region availability, and confirm what Preview terms mean for a
  regulated workload.
- The registry namespace is **migrating from `bedrock-agentcore` to `agent-registry`**, which changes
  endpoints, IAM policy resources, and SDK and CLI usage.
- An IAM policy written against the old namespace **fails closed** — the safe direction, but not a
  free one. It surfaces as an authorisation error in a governance path, at whatever moment the
  migration lands.

API mechanics are in the global `amazon-bedrock` skill's
`amazon-bedrock/references/agentcore-registry-evaluations.md`.

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

### The seam is a routing field, and the record has to be able to express it

"Auto-close the lowest-risk category" is not one decision, and a record that carries only a
recommendation cannot express which one is meant. A rejection alone spans at least three different
actions with different consequences for the customer:

| route | what happens |
|---|---|
| `REJECT_TO_L2` | block, open an investigation case |
| `REJECT_AUTO` | block the transaction **and** close the alert |
| `REJECT_ALERT_ONLY` | close the alert; the customer keeps transacting; re-alert on recurrence |
| `STEP_UP_THEN_APPROVE` | challenge in-app; release automatically on success |
| `STEP_UP_THEN_L2` | challenge in-app; human review on failure |
| `APPROVE_AUTO` | release |

Three properties this field needs, and each has a failure mode behind it:

- **Derived, never model-emitted.** A model that names its own consequence has become the
  disposition. Compute the route from the recommendation, the flags, the confidence and the
  retrieval completeness, and record the reason beside it.
- **Fail-safe ordering.** Evaluate every gate that can route to a human *before* the recommendation
  is consulted, so no combination of model output can route around an unestablished fact. An
  automatic release resting on an unread remainder is not a finding.
- **Some routes must be unreachable by construction.** Blocking a transaction is meaningless
  against a retrospective alert whose activity has already settled. Keep the value, refuse it in
  the derivation, and record why. A route that encodes risk appetite — close the alert, let them
  continue — should be exposed and never *inferred* from one alert's evidence.

### Agreement is a record, not a rate

"Sustained analyst agreement" implies a number, and a number is not enough to open a seam with. The
disagreement that matters is rarely a different verdict on the same reasoning; it is the analyst
seeing something the agent did not, or vice versa. Capture per (case, configuration):

- would you have reached the same recommendation, **and the same route**
- what did the agent **miss** that you found
- what did it find that **you** would have missed
- was its retrieval complete enough to support the call

The fourth is mechanical if the envelopes carry a coverage denominator: show the reviewer, per
source, what exists for that customer versus what the agent actually read
(`references/scoped-retrieval.md` §4b). Bind the stored verdict to a snapshot of what the reviewer
saw — recommendation, risk, route, unread remainder — or the agreement describes a decision nobody
can reconstruct.

Two observations from doing this on a real deployment. The first review produced a **fixture
defect**, a **model weakness** and a **presentation defect** at once: financial history that could
not have occurred (`production-rules.md` §48), the agent citing a six-field review as a mitigating
factor rather than declaring its thinness a gap, and a field the analyst misread that the agent read
correctly. And agent and analyst were **complementary rather than one dominating** — each found
things the other did not, which is an argument for the seam being narrow and for sampling to
continue after it opens.

---

## Anti-patterns

| Anti-pattern | Why it fails |
|---|---|
| Prompt says "do not close alerts" | Prompts are not access control — filter the tool list instead |
| Agent writes directly to the case record | No separation between proposal and decision |
| Failed tool call returned as a placeholder value | A failure that reads like data ships silently |
| Agent narrative accepted as evidence of an action | Verify the record *and* the in-window invocation count |
| Reasoning trace treated as proof of why an output occurred | It is the model's account of its reasoning, not a causal record |
| Multi-agent synthesis with no per-fact attribution | The synthesiser can assert a link no specialist made, and the verifier cannot catch it |
| Partial specialist set synthesised as if complete | An incomplete evidence base presented as a finished case |
| Policy assumed effective because it deployed | Loaded and enforcing is not the same as matching — test both directions |
| Guardrail attached per call via `guardrailConfig` | A caller that omits it gets no guardrail and no error — enforce with IAM or account/org policy |
| Object Lock `COMPLIANCE` mode picked as the default | For the full retention period no erasure request can be honoured — that is a legal decision, not a setting |
| Append-only claimed on DynamoDB without CloudTrail data events enabled | The compensating control is off by default, so there is no control at all |
| Self-review ("check your work") | Produces agreement, not review — use a separate adversarial invocation |
| Semantic search over customer PII, early on | Unexplainable recall plus a leakage surface |
| Fine-tuning typologies into weights | Destroys version pinning and explainability |
| Confidence score as an approval gate | Confidence is not calibrated to correctness |
| Temperature 0 treated as a reproducibility guarantee | Greedy decoding narrows variance; with no seed it does not replay a run |
| One prompt for all typologies | Dilutes every one; miss rate is invisible |
| Shipping without a golden set | No way to detect degradation until an examiner does |
