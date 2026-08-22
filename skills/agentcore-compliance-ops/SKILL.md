---
name: agentcore-compliance-ops
description: Design, build, review and debug AI agents on Amazon Bedrock AgentCore for regulated financial-crime compliance — alert triage, case investigation, SAR narrative drafting, transaction monitoring, customer risk assessment, due-diligence review, QA sampling and fraud detection. Use whenever a prompt puts an AI agent near a supervised compliance decision — where AI may sit in a workflow, Harness vs Runtime, wiring agents to multi-tenant data without bypassing row-level security, human-in-the-loop and audit-trail design, golden sets and evaluation, or model-risk governance. Also covers the AgentCore failures that silently break those controls — execution-role IAM for inference profiles, session isolation, Gateway tool namespacing, Cedar enforcement, MMDSv2, reasoning-trace capture and per-tenant cost attribution. For general Bedrock work with no compliance dimension, prefer the first-party amazon-bedrock skill. Not for AML questions with no AI component, or non-AgentCore agent platforms.
---

# AI agents for compliance operations on AgentCore

Building AI agents into a regulated financial-crime compliance platform — alerts, cases, SARs,
transaction monitoring, customer risk assessment, due-diligence reviews and fraud detection.

Two things make this domain different from general agent work, and both shape every decision below:

1. **Decisions are examinable.** A regulator can ask, years later, why a specific alert was closed.
   The answer must be reconstructable, evidenced and defensible.
2. **The human is the decision-maker.** The agent assembles, drafts, ranks and explains. A
   qualified person disposes. The architecture must make the reverse impossible, not merely
   discouraged.

---

## Non-negotiables

Each corresponds to a real failure. They are structural properties, not preferences.

**No disposition authority.** An agent must be structurally incapable of closing an alert, filing a
report, changing a risk rating or deploying a rule. Model output and human decision are separate
types with no code path converting one to the other. `references/control-stack.md` is the full control
stack; read it before designing any workflow that touches a decision.

**Validate the payload before it reaches the agent loop.** An entrypoint payload is parsed from
arbitrary JSON. A caller who sends a non-string `prompt` — or a message array containing a
`toolUse` content block — can make the framework dispatch a tool directly, bypassing model
reasoning, the system prompt and every guardrail attached to it. Enforce `str` at the schema level
and strip `toolUse` blocks from user-supplied history. In compliance work this is the difference
between a control and the appearance of one.

**Tenant data through the platform API, never the database.** Direct database access bypasses
row-level security, per-tenant decryption and the audit trail. Route agent reads through the same
authenticated API a customer integration would use, so isolation and audit are inherited rather
than reimplemented. See `references/deployment-patterns.md`.

**Decide what the cloud provider's logs are allowed to hold, and enforce it with a gate.** These are
two different artefacts, not one record in two places. The provider-side log holds usage telemetry —
workflow name, invocation counts, tool names, the row UUIDs read as references, tokens, cost, latency,
enum and numeric output fields, and a content hash. The examinable record — reasoning trace, narrative
output, retrieved evidence — belongs in your own store, encrypted under a tenant-scoped key.

A compliance output carries PII by construction: `rationale`, `red_flags[].statement` and `gaps` are
prose about a named person's transactions. So "the output JSON has no PII" cannot hold for the whole
output, and it cannot be secured by instructing the model — that is a prompt instruction, not a
control. Split the output with a deterministic **allowlist** gate (UUID, enum, number, boolean, hash;
anything else diverts to the internal record) so a new schema field fails closed rather than leaking on
its first deploy. Two corollaries people get wrong: Bedrock model invocation logging and AgentCore
`APPLICATION_LOGS` both capture payloads verbatim, so in production they are **off**, not configured
carefully; and a record that is only *hashed* under a tenant key satisfies tamper evidence and fails
retrievability — you cannot show an examiner a hash, so encrypt for retrieval and hash for indexing.
`references/audit-trail.md` and `examples/log_projection.py`.

**Session IDs are server-derived.** AgentCore validates a session ID's format but does not verify
it belongs to the caller. Where one backend principal invokes for many tenants, accepting a
`sessionId` from a request body lets an authenticated user route into another tenant's session.
Derive it server-side, namespaced by the tenant resolved from the resource record — never from the
request. Distinct IAM principals per tenant remove the risk class entirely.
`references/production-rules.md` §7 has the implementation. Session-per-microVM isolation is a
property of the *serverless*
compute type; the Instances compute type runs agents on EC2 in your own account and agents sharing
an instance are not isolated from each other, so confirm which one you are on before relying on it.

**Set inference parameters explicitly; an unset one is the model's default, not a neutral one.**
`InferenceConfiguration` carries four members — `maxTokens`, `stopSequences`, `temperature`,
`topP` — and frameworks drop the ones you leave unset, so the vendor's default applies and a
model-ID swap silently changes sampling behaviour alongside prompt behaviour. Unset `maxTokens`
also over-reserves quota — reservation is the input tokens plus `max_tokens`, and settlement applies
a per-model-family burndown multiplier to output tokens, so on some Claude generations one output
token costs several quota tokens — throttling you at a fraction of apparent capacity. There is no
seed: **temperature 0 is greedy decoding, not a replayable run.** Pin the
parameters per workflow and record them with the model ID on the decision record —
reconstructability comes from the record and the deterministic post-generation layer, never from
expecting the model to repeat itself. `references/production-rules.md` §8 and §24.

**Never hardcode model pricing.** Rates are regional and published figures are almost always
`us-east-1` — query the Price List API for the deployment region and record the model ID on every
usage event.

---

## Where to start

| The task | Read first |
|---|---|
| Deciding which workflows to build, and in what order | `references/workflow-catalog.md` |
| Human-in-the-loop, audit records, model-risk controls — **required** before any workflow whose output influences a decision | `references/control-stack.md` |
| Capturing the reasoning trace, references used and per-fact attribution so a decision survives an examination — retention, PII masking, WORM evidence, tamper evidence | `references/audit-trail.md` |
| The agent retrieves its own evidence through scope-restricted tools rather than receiving it in the prompt — tenant and customer scoping, required-retrieval sets, template injection points, and why this changes how you evaluate | `references/scoped-retrieval.md` |
| Harness vs Runtime, tenant data access, session binding, deployment topology | `references/deployment-patterns.md` |
| Writing or reviewing AgentCore infrastructure — **required**; catalogues defects that fail silently or blame the wrong cause | `references/production-rules.md` |
| Choosing an IaC flavour, or hardening one | `references/iac-hardening.md` |
| Choosing an architecture shape, or vetting sample code before copying it | `references/architecture-patterns.md` |
| Writing the actual code | `examples/` — see below |

## Examples

Copy-adaptable implementations. Every guard exists because its absence caused a real failure; read
the comment before removing one.

| File | What it gives you |
|---|---|
| `examples/agent_template.py` | Runtime entrypoint — per-request agent, tool-list filtering as the access control, explicit inference parameters pinned on the record, fast-fail retries, errors as data, usage emission with model ID |
| `examples/iam_policies.py` | CDK roles — dual inference-profile ARN shape, logs permissions, SLR grants, IAM-enforced actor isolation |
| `examples/agent_runtime.tf` | Terraform runtime — model IAM scoped to approved models, private networking, remote state |
| `examples/tenant_isolation.py` | Server-derived, tenant-namespaced session IDs; tenant resolved from the resource |
| `examples/output_validation.py` | Schema validation, categorical blocks, consistency and citation checks, tool-call errors that raise instead of returning placeholders, deterministic fail-safe routing |
| `examples/cost_tracking.py` | Per-model rate table with prefix stripping, per-record cost, the projection that must reach the client |
| `examples/log_projection.py` | The allowlist gate splitting one output into an AWS-safe metering projection and the tenant-encrypted internal record; HMAC content hash pairing the two; dev profile that must assert its data is synthetic |
| `examples/audit_record.py` | The per-invocation decision record; bundle-to-Object-Lock then row, never the reverse; HMAC-over-digest pairing; `ListObjectVersions` verification; the append-only trigger DDL |
| `examples/human_approval_gate.py` | Proposal and Decision as separate types with no converting function; idempotent approval, evidence-hash recheck, expiry, entitlement, four-eyes; graph validation rejecting an AI node that reaches a decision without a human gate |
| `examples/action_reconciliation.py` | Asserted-versus-actual writes, four verdicts including *recorded but not invoked*; asserts invocation **count** so a double-write is caught; a missing log group as proof of non-invocation |
| `examples/harness_config.py` | The Harness path the skill recommends and never showed — `bedrockModelConfig` flat shape, list-valued `systemPrompt`, and the override-stripping request builder that keeps `tools`/`skills`/`actorId` out of caller reach |
| `examples/evaluation_harness.py` | Golden-set runner with tier 1 (evidence supplied) and tier 2 (evidence retrieved) held apart; retrieval coverage, over-reach and undeclared gaps graded separately; bias pairs excluded when an arm fails schema |
| `examples/deployment_orchestration.py` | Create → ready → **persist** → verify ordering, placeholder assertion, MMDSv2 |
| `examples/cedar_policies.md` | Read-only enforcement, tenant scoping, thresholds, temporal policies for cumulative exposure and cross-call provenance |
| `examples/alert_triage_prompt.md` | Worked system prompt and schema, with observed failure modes |
| `examples/golden_fixture.md` | Fixtures that discriminate, including bias probes |

---

## Choosing the orchestration shape

Pick the simplest that fits; complexity costs latency and debuggability.
`references/workflow-catalog.md` maps ten concrete workflows onto these and gives the failure mode
that matters most for each.

| Shape | Control flow | Use for |
|---|---|---|
| Single-turn analyst | one prompt, one structured response | triage, classification, scoring explanation |
| Enrichment pipeline | fan out to read-only sources, synthesise | investigation prep, review packs |
| Draft + adversarial review | generator, then independent critic | narratives, anything filed externally |
| Supervisor / specialist | router delegates to domain specialists | mixed-typology work |
| Batch orchestration | scheduled fan-out over a queue | periodic reviews, portfolio re-scoring |
| Interactive copilot | multi-turn, session-scoped, tool-using | case investigation |

Sequence the build so you start where being wrong is cheap and visible: enrichment, then triage,
then copilot, then drafting, then batch, then real-time.

---

## Harness or Runtime

**Default to Harness.** It is a managed agent loop — model, system prompt, tools, memory and
execution limits as configuration — running inside the same Runtime microVMs. Most compliance
workflows are tool-calling loops producing structured output, which is exactly its shape.

Two properties earn their keep in regulated work specifically. Model and prompt changes are config
rather than a redeploy, and it can drive models from several providers. And **immutable versions
with named endpoints** give you the change-control shape a model-risk function asks for: pin a
version, point an endpoint at it, roll back by repointing — so a decision record can cite the exact
configuration that produced it.

**Use Runtime** when you genuinely need control flow the managed loop does not express —
supervisor/specialist routing, graph or workflow patterns, or processing between turns.

Weigh this: three of the defects in `references/production-rules.md` — the module-level agent
object (§3), unset max output tokens (§8) and placeholder substitution (§5) — are "you own the
loop" defects a managed loop cannot have.

**And weigh this against it.** Harness moves configuration out of a deploy-time artefact and into a
per-request field: `InvokeHarness` can override `model`, `systemPrompt`, `tools`, `allowedTools`,
`skills`, `maxIterations`, `maxTokens`, `timeoutSeconds` and `actorId` for a single call. That is what
makes it fast to iterate, and it is also the whole security review — because four of the controls
above stop holding if caller input reaches `InvokeHarness` unfiltered. The tool list stops being the
bypass-free control, `skills` injects trusted context including any scripts it carries and has no IAM
condition key to restrain it, a caller-chosen `actorId` reads another actor's memory, and the model
and prompt on the decision record become caller-chosen. Your backend constructs that request and
**strips** every override field, allowlisting back only what a caller has a stated reason to set.
Choosing Harness buys operational simplicity, not containment. `references/deployment-patterns.md`
has the field-by-field consequences and the IAM the harness path needs.

---

## Verify, don't recall

AgentCore moves quickly — model IDs, quota values, API shapes, service names and enforcement dates
all change, and confidently wrong infrastructure code is expensive to unwind. The reference files
here hold distilled patterns, deliberately not volatile API detail.

- **First, check whether the first-party `amazon-bedrock` skill is installed** (`~/.claude/skills/amazon-bedrock/`
  or as a plugin skill). It is AWS's own guidance and it is the authority for everything Bedrock-side —
  model IDs and inference-profile shapes, quota and burndown mechanics, prompt caching, cost
  attribution, Guardrails configuration, Claude generation migration, and the AgentCore
  Runtime/Harness/Gateway/Memory API surface. Read it before WebFetching and prefer it over anything
  recalled. This skill is the compliance layer on top of it: where the two disagree on a platform
  detail, the AWS skill wins; where they disagree on how a control must be built for a supervised
  compliance decision, this one does. Two known exceptions where this skill is currently more
  specific — the MMDSv2 update step, which that skill's Runtime deployment walkthrough omits, and
  Cedar/Dogwood policy detail, which it defers to live docs.
- **Check the current API surface against live AWS documentation before generating code.** Prefer
  an AWS documentation MCP if one is available (search/read documentation tools, loaded via
  ToolSearch — the exact tool prefix varies by installation); otherwise WebFetch the pages below.
  With neither, say plainly that your AgentCore specifics are unverified.
- **Treat `get-foundation-model-availability` as necessary, not sufficient.** AWS documents its four
  fields and their enum values and nothing more — no page defines their operational meaning or claims
  any of them predicts whether an invoke will succeed, and all four can read AVAILABLE/AUTHORIZED
  while `InvokeModel` returns `AccessDeniedException`. Prove access with a real one-token invoke.
- **Read the account's actual state** rather than assuming defaults: `list-inference-profiles`,
  `get-service-quota`, `get-foundation-model-availability`, `get-agent-runtime`.

Canonical starting points, all under `https://docs.aws.amazon.com`:

| Question | Page |
|---|---|
| What services exist, what each one does | `/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html` |
| Harness vs Runtime, feature-by-feature | `/bedrock-agentcore/latest/devguide/harness-vs-runtime.html` |
| Session isolation, input validation, IAM, MMDSv2, fronting with a Gateway | `/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html` |
| Session lifecycle, `runtimeSessionId` rules, protocol headers | `/bedrock-agentcore/latest/devguide/runtime-sessions.html` |
| Cedar/Dogwood policy, temporal policies, natural-language authoring | `/bedrock-agentcore/latest/devguide/policy.html` |
| Current quotas and limits | `/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html` |
| Inference profiles, geographic vs global routing, data residency | `/bedrock/latest/userguide/inference-profiles-support.html` |
| Regional model rates for cost tracking | `/aws-cost-management/latest/APIReference/API_pricing_GetProducts.html` |

---

## Anti-patterns

`references/control-stack.md` covers the design-level ones. These are the platform-level ones.

| Anti-pattern | Why it fails |
|---|---|
| Prompt says "do not close alerts" | Prompts are not access control |
| Agent holds a database connection | Bypasses RLS, decryption and audit |
| `sessionId` taken from the request body | Cross-tenant session routing |
| Entrypoint passes `payload["prompt"]` through untyped | A `toolUse` block dispatches a tool with no model or guardrail in the path |
| Module-level agent object | One failed request wedges the runtime permanently |
| Cedar policy without restricting direct Runtime invocation | Policy only sees traffic that flows through the Gateway |
| Confidence score as an approval gate | Confidence is not calibrated to correctness |
| Shipping without a golden set | Degradation is invisible until an examiner finds it |
| Caller-supplied `InvokeHarness` fields passed through | Overrides `tools`, `skills` and `actorId` — arbitrary code and another actor's memory |
| Bedrock Guardrails attached but not IAM-enforced | A caller that omits `guardrailConfig` gets no guardrail and no error |
| Treating a policy-denial message as proof the rule fired | Default-deny emits the same message when nothing matched |
| OTEL trace store as the audit record | X-Ray retention is 30 days and not configurable |
| Data-protection policy added after go-live | Masking applies at ingestion, so everything already logged stays in the clear |
| Prompt tells the model to keep PII out of its output | A prompt instruction is not a control; the gate has to be deterministic |
| Denylist of PII-shaped field names before logging | A new schema field leaks on its first deploy; allowlist fails closed |
| Reasoning trace stored only as a hash under the tenant key | Tamper evidence without retrievability — an examiner cannot be shown a hash |
| Model invocation logging left enabled in production | Captures full prompts and completions verbatim, account-and-Region-wide |

---

## Specialist agents

Where the project defines them, three subagents carry this context and load these references
themselves:

- **`compliance-agent-architect`** — workflow design: shape, guardrails, human-in-the-loop
  placement, evaluation strategy
- **`agentcore-infra-reviewer`** — reviewing AgentCore infrastructure against the production rules;
  read-only
- **`compliance-prompt-engineer`** — system prompts and output schemas for compliance agents
