---
name: agentcore-compliance-ops
description: Design, build, review and debug AI agents on Amazon Bedrock AgentCore for regulated financial-crime compliance operations — alert triage, case investigation, SAR narrative drafting, transaction monitoring, customer risk assessment, ongoing and enhanced due diligence, QA sampling and fraud detection. Use whenever a prompt puts an AI agent near a supervised compliance decision — where AI may sit in a workflow, Harness vs Runtime, wiring agents to multi-tenant data without bypassing row-level security, human-in-the-loop and audit-record design, golden sets and evaluation, or model-risk governance. Also covers AgentCore platform problems outside compliance — execution-role IAM for inference profiles, service-linked roles, runtimeSessionId derivation and session isolation, Gateway targets and Cedar policy controls, quotas and throttling, MMDSv2, and per-tenant cost attribution. Not needed for AML questions with no AI component, or for agent platforms other than AgentCore.
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
types with no code path converting one to the other. `references/guardrails.md` is the full control
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

**Session IDs are server-derived.** AgentCore validates a session ID's format but does not verify
it belongs to the caller. Where one backend principal invokes for many tenants, accepting a
`sessionId` from a request body lets an authenticated user route into another tenant's session.
Derive it server-side, namespaced by the tenant resolved from the resource record — never from the
request. Distinct IAM principals per tenant remove the risk class entirely.
`references/production-rules.md` §7 has the implementation. Session-per-microVM isolation is a
property of the *serverless*
compute type; the Instances compute type runs agents on EC2 in your own account and agents sharing
an instance are not isolated from each other, so confirm which one you are on before relying on it.

**Set max output tokens explicitly, and never hardcode model pricing.** Quota is reserved as
`input_tokens + max_tokens`; unset means the model's maximum, and you throttle at a fraction of
apparent capacity. Rates are regional and published figures are almost always `us-east-1` — query
the Price List API for the deployment region and record the model ID on every usage event.

---

## Where to start

| The task | Read first |
|---|---|
| Deciding which workflows to build, and in what order | `references/workflow-catalog.md` |
| Human-in-the-loop, audit records, model-risk controls — **required** before any workflow whose output influences a decision | `references/guardrails.md` |
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
| `examples/agent_template.py` | Runtime entrypoint — per-request agent, tool-list filtering as the access control, `max_tokens`, fast-fail retries, errors as data, usage emission with model ID |
| `examples/iam_policies.py` | CDK roles — dual inference-profile ARN shape, logs permissions, SLR grants, IAM-enforced actor isolation |
| `examples/agent_runtime.tf` | Terraform runtime — model IAM scoped to approved models, private networking, remote state |
| `examples/tenant_isolation.py` | Server-derived, tenant-namespaced session IDs; tenant resolved from the resource |
| `examples/output_validation.py` | Schema validation, categorical blocks, consistency and citation checks, tool-call errors that raise instead of returning placeholders, deterministic fail-safe routing |
| `examples/cost_tracking.py` | Per-model rate table with prefix stripping, per-record cost, the projection that must reach the client |
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

---

## Verify, don't recall

AgentCore moves quickly — model IDs, quota values, API shapes, service names and enforcement dates
all change, and confidently wrong infrastructure code is expensive to unwind. The reference files
here hold distilled patterns, deliberately not volatile API detail.

- **Check the current API surface against live AWS documentation before generating code.** Prefer
  an AWS documentation MCP if one is available (search/read documentation tools, loaded via
  ToolSearch — the exact tool prefix varies by installation); otherwise WebFetch the pages below.
  With neither, say plainly that your AgentCore specifics are unverified.
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

`references/guardrails.md` covers the design-level ones. These are the platform-level ones.

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

---

## Specialist agents

Where the project defines them, three subagents carry this context and load these references
themselves:

- **`compliance-agent-architect`** — workflow design: shape, guardrails, human-in-the-loop
  placement, evaluation strategy
- **`agentcore-infra-reviewer`** — reviewing AgentCore infrastructure against the production rules;
  read-only
- **`compliance-prompt-engineer`** — system prompts and output schemas for compliance agents
