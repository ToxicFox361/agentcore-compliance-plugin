# Architecture patterns worth copying

Reusable shapes for agents operating inside a regulated compliance function, and the checks that
decide whether a particular implementation of one is safe to adopt.

They are described as shapes rather than as code because the property that makes each one safe is
structural: where the decision is made, what the agent is *capable* of, and what is enforced outside
the agent's control. An implementation either has that property or it does not — the framework it
was written in barely matters, and a README claiming the property is not evidence of it.

Ordered by usefulness to a compliance operations platform.

---

## Patterns at a glance

| # | Pattern | The property that makes it safe |
|---|---|---|
| 1 | Model assesses, code decides | The disposition itself is deterministic |
| 2 | Adversarial review without shared reasoning | The reviewer is not anchored to the drafter |
| 3 | Plan, approve, then execute | A human sees the plan before any tool runs |
| 4 | Segregation of duties by capability | The investigating agent structurally cannot act |
| 5 | Identity propagation across hops | The audit trail names a person, not a service |
| 6 | Parallel fan-out with error isolation | One failed item does not fail the batch |
| 7 | Approval state machine | Draft and decision are separate records |
| 8 | Programmatic evaluators first | A quality signal that does not need judgement |
| 9 | Async for long work, limits for runaway work | Legitimate slowness and loops are distinguishable |
| 10 | Cross-runtime composition | Specialists deploy and fail independently |
| 11 | Tenant isolation enforced by IAM | Isolation you can attest to, not hope for |
| 12 | Controls as declarative policy | Enforced outside agent code |
| 13 | PII-safe, tenant-tagged observability | Traces you can keep and still correlate |
| 14 | Protocol-level ask-before-acting | Confirmation the model cannot skip |

---

## Before you copy anything

Sample code — vendor samples, blog posts, internal prototypes — is written to demonstrate a
capability, not to survive an examination. Five checks, in the order they usually pay off:

1. **Read the code, not the README.** Intent described in prose and behaviour implemented in code
   diverge routinely, and the gaps are exactly the controls you care about.
2. **Find every constant that should be configuration.** An approval flag pinned to `True` for
   unattended demo operation inverts the whole control when the demo becomes a deployment.
3. **Ask what the agent is *capable* of, not what it is told.** An approval gate expressed in a
   system prompt is documentation. See `production-rules.md` §21.
4. **Look for the tenant dimension.** Most samples have a user and a session but no tenant. Adding
   one later touches keys, memory namespaces, IAM conditions and policy conditions at once.
5. **Check the tests assert something.** Print-based smoke scripts that need live infrastructure are
   common in sample code and detect nothing. So do tests that exercise a re-implemented copy of the
   handler rather than the real one.

A deployment that succeeds is not a deployment that works: a pipeline can run end to end while the
agent narrates a tool call it never made (`production-rules.md` §18).

---

## 1. Model assesses, code decides

The highest-value shape, and the one to reach for first:

```
Phase 1  Analyst agent   → structured assessment via a submit_* tool (no free-text verdict)
Phase 2  Reviewer agent  → independently re-scores, cheaper model, no write tools at all
Phase 3  Pure Python     → (assessment, confidence) → REJECT | AUTO_ROUTE | HUMAN_REVIEW
```

What earns its keep:

- **Phase 3 is deterministic.** The routing decision is code, not a model. That is precisely the
  property a compliance disposition needs — the model contributes assessment, code makes the call.
- **The reviewer holds no tools.** Least privilege *between* agents, not only at the perimeter. A
  reviewing agent has no reason to be able to act, so give it no capability to.
- **Fail-safe defaults.** Missing or malformed structured output routes to `HUMAN_REVIEW`, never to
  auto-approve. If you copy one line from this pattern, copy that one.
- **A hard threshold outside the model's control.** Above a stated value, auto-disposition is
  forbidden by policy at the Gateway (pattern 12), not by instruction.

**Check before copying:** that phase 3 really is model-free; that the threshold is enforced
somewhere the agent cannot reach; and that the two agents use separate clients with separate tool
lists, not one client with a prompt telling the reviewer to behave.

---

## 2. Adversarial review without shared reasoning

Same two-agent shape, with one decision that is worth adopting outright:

> The reviewer sees **only the drafter's structured output — never its reasoning.**

Showing a reviewer the original chain of thought anchors them to it, and the review degenerates into
agreement. Withholding it forces independent assessment. Directly applicable to a SAR draft-plus-
critic pair and to any second-line review.

The related failure is self-review — asking one agent to check its own work — which reliably
produces agreement rather than review. A separate invocation with a separate prompt and no sight of
the prior reasoning is the cheapest real control available here.

A useful companion is a **model degradation ladder**: on capacity errors, step down through model
tiers, and at the bottom rung defer the item to a queue for replay rather than guessing. For a batch
monitoring sweep that is the right failure mode — a deferred alert is recoverable, a guessed
disposition is not.

---

## 3. Plan, approve, then execute

A supervisor drafts a structured investigation plan — typed, not prose: the specialists to run, the
steps, an estimated complexity, and an `auto_execute` flag — and when the plan exceeds a complexity
threshold it **halts and surfaces the plan for approval before any specialist runs.** It then
dispatches specialists in sequence, looping through the supervisor until finished, and aggregates.

The best shape available for case investigation: an analyst reviews the AI-drafted checklist before
it executes, then sees aggregated findings. The auto-approve flag is a natural supervised-versus-
autonomous toggle per case severity.

**Check before copying:** implementations of this pattern frequently hardcode the auto-approve flag
to `True` in the deployment path so the demo runs unattended. In a compliance deployment that
inversion is the entire control. Keep it gated, make it configuration, and default it closed.

---

## 4. Segregation of duties by capability

An orchestrator that holds **no consequential capability and structurally cannot act.** Only
narrowly-scoped specialist agents can, each against its own budget or limit, and the orchestrator
can only inspect state and route.

Mapped onto compliance: an investigation agent that reads everything and may propose, plus a
separate action-capable agent — invoked only after human approval — that files, freezes or
escalates. Per-agent budgets generalise to per-case action limits.

This is the cleanest expression of segregation of duties available in an agent architecture, and it
is what `control-stack.md` Layer 1 looks like when implemented rather than asserted. The capability
split must be real: separate execution roles, separate tool lists, separate Gateways or targets.

---

## 5. Identity propagation across a multi-hop chain

A supervisor Runtime aggregates tools from several specialist Gateways and **propagates the end
user's identity through the entire chain** — client → supervisor → specialist → backend — rather
than collapsing it into a service account at the first hop.

Two things depend on this. A downstream system can enforce the individual analyst's permissions, and
the audit trail is attributable to a person rather than to a shared principal. Both are examination
questions.

The stronger form performs a real token exchange
([RFC 8693](https://datatracker.ietf.org/doc/html/rfc8693)) at each hop and **intersects** the
caller's scopes with a per-target allowance, so a profile specialist can only ever receive
`profile:*` and an accounts specialist only `accounts:*`. Short-lived tokens carrying a delegation
claim then record who delegated what to whom. AgentCore Identity provides the inbound authorizer,
the credential providers and the on-behalf-of token-exchange grant this pattern needs —
[AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html).

**Name the mechanism.** The parameter that carries a person rather than a service is
**`runtimeUserId`**, accepted by both `InvokeAgentRuntime` and `InvokeHarness` and travelling as
`X-Amzn-Bedrock-AgentCore-Runtime-User-Id`. Using it is itself an IAM-visible act: invoking on behalf
of a user requires `bedrock-agentcore:InvokeAgentRuntimeForUser` *in addition to*
`bedrock-agentcore:InvokeAgentRuntime`, so "which principals may act for a named user" becomes a
policy question with a written answer.

**Then know what the platform does not verify.** That header is enough to reach the user-scoped
credential path — AgentCore resolves it through `GetWorkloadAccessTokenForUserId` for on-behalf-of
OAuth flows. But the platform treats the user ID as **an opaque string, and does not verify it
against an authenticated end-user identity**; the binding holds only because the calling workload
passed the right value and your IAM scoping is tight. Only the JWT inbound path validates a real
end-user token, which is why AWS recommends JWT bearer authentication for production deployments and
why on-behalf-of exchange presumes an inbound user token to exchange in the first place. A runtime
supports one inbound method at a time — separate versions for different authentication types — so
this is a design-time decision with a version change behind it, not a runtime toggle.

The consequence for a compliance platform is concrete. If the audit trail must name a person, either
put a validated JWT on the inbound path, or derive `runtimeUserId` server-side from the authenticated
principal and never from a request body — the identical argument this skill makes about session IDs
(`production-rules.md` §7). AWS's own guidance supplies the detection half: monitor
`GetWorkloadAccessTokenForUserId` in CloudTrail for unexpected user IDs.

**Be precise about what Identity evidences.** Identity operations are CloudTrail-logged with token
values redacted — AWS's examples show `HIDDEN_DUE_TO_SECURITY_REASONS` where the workload access
token would be — so an exchange is recorded without the credential entering the log. Tokens are
scoped to a specific user-and-agent pair, so credentials held for one user cannot serve another
user's request, which is the binding this whole pattern exists to create. What none of it proves is
that a qualified person read a draft and approved it. That record is yours to build (pattern 7, and
`control-stack.md`).

- [Authenticate and authorize inbound requests](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)
- [Get a workload access token](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html)
- [Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)

**Check before copying:** whether the chain actually carries the user's identity or merely starts
with it. And treat any approval gate in such a sample as prompt-level unless you can point at the
code that refuses to proceed.

---

## 6. Parallel fan-out with error isolation

One task per target, gathered concurrently, exceptions returned as values rather than raised:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
```

The `return_exceptions=True` is the whole point: **one failed item does not fail the batch.** The
shape for batch alert scoring, periodic review sweeps, and parallel due-diligence lookups
(sanctions, adverse media, registry) fired concurrently per customer.

The discipline that makes it safe is what happens to the exceptions afterwards. Each failed item
needs a recorded outcome — deferred, retried, escalated — not a silent absence from the results. A
sweep that quietly covered 94% of the portfolio is a compliance failure that looks like a success.

---

## 7. Approval state machine

Event → automated checks (duplicate detection, security scan, schema validation) → findings
persisted → notification carrying approve/reject actions → `DRAFT → PENDING_APPROVAL →
APPROVED | REJECTED`.

Two distinct uses:

- **Change control for the platform itself.** A new agent, prompt or schema version passes automated
  scanning plus human sign-off before it goes live. `control-stack.md` (model risk governance) requires
  this; the state machine is how it is implemented.
- **Decision approval.** The same states map onto a SAR: auto-drafted → pending compliance-officer
  approval → filed or rejected.

**Do not hand-build the first one.** AWS Agent Registry — documented inside the AgentCore guide —
ships this state machine with the same state names: `DRAFT → PENDING_APPROVAL → APPROVED`, with
`REJECTED` reachable from pending and `DEPRECATED` terminal from any status. It also ships the parts
that take longest to build by hand:

- **Publisher and curator as distinct personas with distinct IAM permissions** — the maker-checker
  split enforced rather than described. The curator's `UpdateRegistryRecordStatus` call requires a
  `statusReason`, so the reason for a rejection is a mandatory field rather than one you remember to
  add.
- **A dual-revision rule.** Editing an approved record opens a new draft while the approved revision
  stays discoverable until a curator approves its replacement. That is the "pin a version, repoint an
  endpoint" change-control shape the skill already recommends, applied to the catalogue entry.
- **Records with structure.** `recordType` (`MCP`, `AGENT`, `SKILL`, `CUSTOM`), display name,
  description, version and tags, with endpoint and capability detail inside a descriptor that AWS
  validates against the MCP or agent protocol schema. Endpoint is not a top-level field — it lives in
  the descriptor payload.
- **EventBridge on submission and CloudTrail on the governance calls.** Record creation, submission
  and approval are management events, logged by default: who registered and who approved are
  answerable without extra work. Note the asymmetry — *discovery* and MCP invocation are **data
  events and are not logged by default**, so "who found and used this agent" needs data-event logging
  enabled for the registry resource type before it becomes an answerable question.

For the **agent inventory** `control-stack.md` requires — each agent a registered model with an owner,
purpose and risk tier — that is a better answer than a table you maintain yourself.

Three things to settle before an examinable control depends on it:

- **Auto-approval is pattern 3's inverted flag in different clothes.** Manual review is the default;
  configuring an auto-approval rule makes submission skip `PENDING_APPROVAL` and land on `APPROVED`,
  which is no gate at all. AWS does not say where auto-approval is acceptable, so make the ruling
  yourself: isolated development accounts only, and its presence in a registry that describes
  production agents is a finding.
- **It is a Preview feature in a subset of Regions.** Confirm availability, and what the Preview
  service terms mean for a regulated workload, before it carries a control you would show an examiner.
- **The namespace has already moved, with a hard cutoff.** The registry now lives under
  `agent-registry` rather than `bedrock-agentcore`; the preview namespace shuts down on AWS's
  published date (September 17, 2026 as written), after which access to it and to any data left in it
  is gone. Endpoints, IAM action prefix, service principal, ARNs, CLI, SDK client, CloudTrail event
  source, EventBridge source, CloudWatch namespace and Service Quotas code all change, a new
  `AgentRegistryFullAccess` managed policy replaces the old one (which will *not* be updated), records
  are not migrated for you, and the schema changes are breaking. The trap worth naming: the move
  applies to the registry *only* — Identity, Gateway, Runtime and Policy stay where they are — so a
  blanket search-and-replace across your IAM policies breaks workload-identity and OAuth
  credential-provider permissions, which remain under `bedrock-agentcore`.

- [AWS Agent Registry](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry.html)
- [Record lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-record-lifecycle.html)
- [Registry migration guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-faq.html)
- [Log Registry API calls with CloudTrail](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/registry-cloudtrail.html)

**Then spend the effort you saved on the half it does not cover.** Registry approves a registered
*resource*; nothing in it approves a *case decision*. A four-eyes gate on a SAR narrative or an alert
disposition is still yours to build, and that is where the design attention belongs.

The property to preserve is that the draft and the decision are **separate records with separate
authorship**, not one record whose status field advances. That is what makes the maker-checker split
reconstructable years later.

---

## 8. Programmatic evaluators first

A useful evaluation harness mixes evaluator types, and the non-LLM ones are the ones you can rely
on:

| Evaluator | What it does |
|---|---|
| PII detection | Hard-fails output containing SSN, bank/routing, card, passport or credentials above a confidence floor |
| Schema conformance | Structured output validates against the schema in force |
| Factual drift | Cited values compared against ground truth |
| Workflow contract | Actual tool-call sequence compared against a declared contract |
| LLM-as-judge (rubric) | Citation accuracy, tone, completeness — softest signal, use last |

The PII evaluator doubles as a **leak check** on any output before it reaches a user or a trace. The
workflow-contract evaluator answers "did the agent follow the required investigation steps", which
is an auditable property rather than an opinion — and it is the natural place to assert that the
tool calls a run claims to have made actually appear in its trace (`production-rules.md` §18).

AgentCore Evaluations supports online (continuous, on production traffic), on-demand and batch
evaluation over agent traces, with built-in and custom evaluators —
[AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html).
Whatever runs the evaluators, hold the fixtures in a versioned dataset: reproducibility is what makes
an evaluation result defensible on review.

---

## 9. Async for long work, limits for runaway work

Two different problems that are easy to conflate.

**Legitimate long work** — an EDD deep-dive, a monitoring sweep — needs an asynchronous shape:
accept, return a handle, process in a background thread, poll for status and results. The agent
reports `HealthyBusy` on its health endpoint so the platform keeps the session alive past the idle
timeout instead of terminating it mid-task. Keep the entrypoint non-blocking, or the health check
blocks with it and the session is reaped anyway. See
[Handle asynchronous and long running agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html).

**Runaway work** — a loop, a retry storm, a model stuck on one schema field — needs bounding:
maximum iterations, wall-clock timeout, maximum tokens. Use both mechanisms. Async without limits
turns a defect into an expensive defect; limits without async make every legitimate long task look
like one.

Validate any externally-supplied task identifier before it reaches storage or a log line.

---

## 10. Cross-runtime and federated composition

Three approaches, increasing in machinery. Start at the top.

1. **Direct runtime invocation as a tool.** A thin orchestrator invokes independently deployable
   specialist Runtimes per domain, with ARNs resolved from parameter storage rather than hardcoded.
   Simplest thing that gives specialists independent deployment and blast radius.
2. **MCP over Gateways.** Specialists exposed as tools through a Gateway, aggregated by the
   orchestrator. Note that the Gateway namespaces every tool name (`production-rules.md` §19) — with
   several targets aggregated behind one endpoint, name resolution at runtime stops being optional.
3. **A2A protocol.** Cross-framework agent-to-agent calls with per-call authorization and agent-card
   discovery. The pattern for federating across an *organisational* boundary — a vendor-operated
   screening agent composed at runtime with no shared codebase.

Only take on (3) when specialists genuinely cross an ownership boundary. Protocol machinery buys
nothing between two services you both deploy.

---

## 11. Tenant isolation enforced by IAM, not application code

The most load-bearing pattern in a multi-tenant compliance platform, and the one most often
approximated in application code:

```
Authenticated user → JWT sub → actorId, with an IAM condition on the execution role
Condition: { StringEquals: { "bedrock-agentcore:actorId": "<sub>" } }
```

The principle, stated plainly: **IAM is the authoritative boundary; application-layer actor checks
are best-effort.** That is the difference between isolation you can attest to and isolation you hope
holds — an application check is one refactor away from being skipped, an IAM condition is not.

Conventions that compose with it:

- Put the tenant in the actor identifier (`tenantA/user1`) so memory namespaces inherit the tenant
  dimension (`/facts/tenantA/user1/`) and can be scoped by IAM condition on the namespace path.
- Use per-tenant customer-managed KMS keys where the data classification warrants it; memory
  resources accept an encryption key at creation.
- Where the security bar is highest, drop the shared service role entirely and bind identity to
  short-lived per-tenant credentials — then IAM enforces the scoping with nothing left to bypass.

See [AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
for the actor/session/namespace model, and `production-rules.md` §7 for the session-ID half of this
problem — a server-derived, tenant-namespaced session ID is the other required half.

---

## 12. Compliance controls as declarative policy

Controls that must not depend on the model behaving belong outside agent code, expressed as policy
evaluated at the Gateway boundary. Two shapes are directly AML/fraud-relevant, and both are
*stateful* — they reason over what has already happened in the session, not just the current call:

1. **Cross-call provenance binding.** An argument to a consequential tool must match a value
   returned by an earlier read call within a time window — for example, a destination account that
   an earlier lookup actually returned. This blocks the agent fabricating, or being injected with,
   an identifier between two tool calls.
2. **Cumulative exposure window.** Sum an argument across a window and deny once the total crosses a
   threshold. Catches "forty small transfers add up to one large unauthorised position" — the same
   control shape as detecting structuring.

AgentCore Policy provides both: point-in-time Cedar rules over the principal, action and tool
arguments, and session-aware temporal rules with `formerly within`, `since within`, `count` and
`sum` operators.

- [Policy in AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html)
- [Temporal policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html)
- [Understanding Cedar policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html)

Adopt this layer under any transaction-monitoring or disposition agent. Two companions are worth
knowing: content guardrails can be attached as policy rules that scan free-text tool arguments
*before* the call reaches the backend — near-identical to scanning a case note or draft narrative
for PII before it is persisted
([Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html));
and a target can be configured to accept only calls that actually flowed through an approved
Gateway, which closes the bypass gap that otherwise makes all of the above optional.

**Check before trusting it:** a policy that never matches is indistinguishable from no policy.
Test every rule empirically in both directions before you rely on it — `production-rules.md` §23,
which also records the attribute-naming defect that causes this silently.

---

## 13. PII-safe, tenant-tagged observability

**The strongest control is not emitting it.** Every mechanism below is about narrowing PII that has
already reached telemetry, and each is weaker than the decision not to put it there — so make that
decision first and explicitly: which fields may leave the process at all, enforced by a deterministic
allowlist rather than by the model's cooperation or a reviewer's attention. Where the deployment policy
is that the provider's logs hold usage telemetry only, this pattern's job shrinks to defence-in-depth
for the leakage that happens anyway, which is the right size for it. `examples/log_projection.py` has
the gate; `audit-trail.md` has the profile split.

Three mechanisms, used together, are what let you keep traces at all once something does flow:

- **Redaction before export.** A span processor that strips configured attributes — prompt bodies,
  input messages — before telemetry leaves the process. Redaction after export is not redaction.
- **The log estate treated as the primary PII sink.** The mechanism teams reach for here does not
  reach as far as its name suggests. Guardrails PII masking covers what the model is sent and what it
  returns — and explicitly **not** the invocation logs: AWS states that the `input` field in
  CloudWatch Logs *always* contains the original, unmodified request regardless of guardrail
  intervention, and points at log data protection as the answer. So wherever Bedrock invocation
  logging is enabled, card numbers and national IDs land in the log estate in clear text however the
  guardrail is configured. Two edges push the same way: masking is unsupported in asynchronous
  streaming mode, and with the guardrail trace enabled the detected entity's `match` field carries
  the *original* PII value by design, so whatever persists that trace persists the PII. Masking
  narrows what a caller and a reviewer see; redaction before export and a log data-protection policy
  are what narrow the estate — complements to each other, not duplicates. `audit-trail.md` has the
  mechanisms.
- **Tenant correlation without identity leakage.** Propagate a tenant identifier and session
  identifier onto every child span via context baggage, while explicitly *not* propagating the
  user's email or name. You get per-tenant trace correlation without putting a person in the trace.
  The headers and parameters that carry this are named in "What a trace can attribute" below —
  "propagate context" is not implementable until you know which ones.

**A data-protection policy is not retroactive, so it belongs in the log group's definition.**
CloudWatch Logs masks at *ingestion*: AWS is explicit that log events ingested into the log group
before the policy existed are not masked. Turn it on after the agent has been running and every
prompt, tool argument and reasoning trace already ingested stays in the clear permanently, for the
life of the group — and in a regulated deployment the long retention that protects your evidence is
the same retention that preserves that exposure. This makes it a deployment-ordering rule rather
than a security-review one: the policy is created **with** the log group, in the same template,
before the first invocation.

Masking is not destroying the evidence, and it is worth naming the read-back path before someone
argues that it is. A principal holding `logs:Unmask` can read the originals — the `unmask` command
in Logs Insights, or `unmask=true` on `GetLogEvents` and `FilterLogEvents`. Grant it to exactly one
break-glass role and alarm on its use, which turns "who read raw customer data" into a question with
an answer. One interaction to check before it is needed: the Infrequent Access log class can *mask*
but cannot *unmask* — the `unmask` query command is unsupported there, and `GetLogEvents` and
`FilterLogEvents` are not available for IA log groups at all — so applying that cost optimisation to
a group holding masked evidence quietly removes the break-glass path. `audit-trail.md` §4 has the
policy document and the break-glass role; this pattern only decides that you need them.

- [Remove PII with sensitive information filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-sensitive-filters.html)
- [Monitor model invocation with logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html)
- [Help protect sensitive log data with masking](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html)
- [CloudWatch Logs log classes](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CloudWatch_Logs_Log_Classes.html)

AgentCore emits OpenTelemetry-compatible telemetry into CloudWatch, so the standard OTel
instrumentation hooks apply —
[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html).
Wire these before wiring a backend, not after: the first export is the one you cannot take back.

Pair it with versioned ground-truth datasets for evaluation (pattern 8) and with a phased rollout
mechanism — config-bundle A/B plus canary — which is the change-control shape a model-risk function
will ask for.

---

## 14. Protocol-level "ask before acting"

MCP elicitation lets a tool server ask the client a structured follow-up question *mid-tool-call* and
block on the answer, rather than returning and hoping something asks later. A protocol-native
confirmation gate — the right primitive for tools that must never fire unattended, such as filing or
account restriction.

Stronger than a prompt instruction because the tool, not the model, decides that confirmation is
required; and lighter than a full approval workflow for the narrow case of "confirm this specific
action before it executes." Note the client must declare the elicitation capability for it to be
offered at all, and servers should fall back safely when it is absent —
[Use elicitation with your AgentCore gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-elicitation.html).

Use it as the last mile of a human-in-the-loop design, not as a replacement for one: it confirms an
action, it does not create an approval record.

---

## What a trace can attribute, and what it cannot

Per-fact attribution is yours to build (`control-stack.md`, and `production-rules.md` §25). The reason
is structural rather than a gap someone will close in a future release, and knowing which half is
which saves a team a quarter spent trying to make spans carry evidence.

**The carriers, first.** "Propagate trace context across every agent boundary" is not implementable
until you know what to put where. The supported invoke headers are:

| Header | Carries |
|---|---|
| `X-Amzn-Trace-Id` | X-Ray context — `Root=1-…;Parent=…;Sampled=1` |
| `traceparent` | W3C context — `00-<32hex>-<16hex>-01` |
| `tracestate` | vendor state travelling with `traceparent` |
| `baggage` | your own key/values — tenant, case ID |
| `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` | the runtime session |
| `mcp-session-id` | the MCP session |

`InvokeAgentRuntime` also accepts `traceId`, `traceParent`, `traceState`, `baggage`,
`runtimeSessionId` and `runtimeUserId` as request parameters, and **returns the trace identifiers in
the response.** That return is the part worth designing around: the caller can pin the trace ID onto
the decision record at invocation time instead of reconstructing the join later from timestamps. Two
details that bite in practice — `InvokeHarness` takes the same parameters but returns an event stream
and **not** the trace headers, so on the Harness path the caller supplies the trace ID rather than
learning it; and `runtimeSessionId` has a **minimum length of 33**, which quietly rejects a 32-hex
digest as a session ID (`production-rules.md` §7).

One security caveat, the same argument this skill makes about session IDs
(`production-rules.md` §7): **a trace ID arriving from a tenant-facing API is a correlation hint,
never an identity.** A caller can inject any trace ID and any sampling decision, so derive it
server-side and treat an inbound one as untrusted input.

**What tracing genuinely gives you:**

- A real parent/child hierarchy across agent boundaries — *if* you propagate. Unpropagated, a
  request through five agents is five disconnected traces.
- Span classification via `gen_ai.operation.name`. **AgentCore itself recognises three values** —
  `invoke_agent`, `execute_tool` and `chat` — and the documented fallback for an inference span is
  `llm.request.type` = `chat`, with `openinference.span.kind` for OpenInference-instrumented
  frameworks. The wider OTel vocabulary (`create_agent`, `text_completion`, `embeddings`,
  `retrieval`, `generate_content`) is valid upstream but buys nothing here, because AgentCore will not
  classify on it. Emit the three it reads and put anything finer in your own namespace.
  `references/audit-trail.md` has the full span design.
- Session-to-trace joining via `session.id`, which the inbound
  `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header maps onto and which you propagate to child
  spans through OTEL baggage. Note that AgentCore's own `InvokeAgentRuntime` span carries `aws.*`
  names — `aws.operation.name`, `aws.resource.arn`, `aws.request_id`, `aws.agent.id`,
  `aws.endpoint.name`, `aws.account.id`, `aws.region`, `latency_ms`, `error_type` (one of `throttle`,
  `system`, `user`) — rather than `gen_ai.*`, so a query written against one vocabulary silently
  misses the other.

**What it structurally cannot do:**

- **A span is an interval; a fact is not.** Spans model work that started and ended. No span shape
  has one-per-asserted-fact cardinality, so "which specialist asserted this sentence" is not a
  question the trace model can be bent into answering.
- **Indexed annotations are capped at 50 per trace, and the quota is not adjustable.** Annotations
  are the only span fields X-Ray indexes for filter expressions; beyond 50 there is no searchability
  to rely on. And in the OTEL→X-Ray path every span attribute lands as unindexed **metadata** unless
  its key appears in the span's `aws.xray.annotations` attribute — a list of other attribute keys, not
  a collector setting. So you cannot make N facts filterable for any realistic N.
- **Size.** An X-Ray segment document is capped at 64 KB, and the log event you would use to carry
  the overflow at 1 MB. A specialist's evidence set for one case routinely exceeds both, so the trace
  can hold a pointer to the evidence but not the evidence.
- **Retention.** X-Ray traces are kept **30 days, not configurable.** Compliance retention is
  measured in years.
- **A specialist that never ran emits no span.** Absence is exactly as invisible in the trace as it
  is in the synthesiser's input (`production-rules.md` §25) — and sampling can remove a span that
  *did* run — which is why the dispatch manifest is not made redundant by good tracing. One fix worth
  adopting anyway: open the child span **at dispatch, before the call**, so a specialist that never
  returns still leaves a span with an error status. Silence becomes a record.

Two design rules follow, and both are cheap to adopt on day one and expensive to retrofit:

- **Put your own attributes in your own namespace** — `compliance.case.id`,
  `compliance.specialist.id`, `compliance.workflow.version`. Never invent a `gen_ai.*` key: a future
  convention release can redefine it underneath you and silently change what your query means, and
  nobody issues deprecation notices for a key you made up.
- **Pin the semantic-convention version and record it on the decision record.** These keys move:
  `gen_ai.prompt` and `gen_ai.completion` have been deprecated upstream in favour of
  `gen_ai.input.messages` and `gen_ai.output.messages`, and AgentCore's per-framework pages document
  several vocabularies live at once — `gen_ai.*`, `traceloop.entity.input`/`output`, OpenInference's
  `llm.input_messages.*`. A stored trace is only interpretable against the convention in force when it
  was produced, which is the same argument this skill makes for output schemas and model IDs.

And one attribute set to leave off deliberately: keep `gen_ai.input.messages` and
`gen_ai.output.messages` **off** spans in a compliance deployment. They carry customer PII verbatim
into the store you have least control over — one you cannot make immutable, cannot retain to your own
policy, and cannot key per subject when an erasure request arrives. Message content belongs on the
decision record or nowhere.

- [Configure observability and propagate context](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [InvokeAgentRuntime](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntime.html)
- [Runtime spans and metrics](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)
- [X-Ray segment documents](https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html)
  and [X-Ray quotas](https://docs.aws.amazon.com/general/latest/gr/xray.html)

---

## Anti-patterns common in sample code

These recur across agent samples regardless of provenance. Do not carry them into a compliance
platform.

**Arbitrary SQL execution as a tool.** An `execute_sql_query(sql: str)` tool running unsanitised SQL,
with a "SELECT only" restriction expressed **only in the system prompt**. No read-only role, no
parameterisation, no tenant predicate. In a compliance platform this is a cross-tenant data breach
waiting for one successful prompt injection. If you need text-to-SQL: a read-only database role,
statement parsing and allow-listing, a mandatory tenant predicate, and row-level security
underneath — in that order, all four.

**Unscoped knowledge-base retrieval.** Retrieval called with no metadata filter against a single
shared index with one access policy. Every tenant's documents are in one place with nothing
preventing cross-tenant recall. Apply a tenant metadata filter on every retrieval, or separate
indexes per tenant —
[RetrievalFilter](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_RetrievalFilter.html).

**Secrets in environment variables.** API keys passed to functions as plain environment variables
rather than as a secrets-manager reference or an AgentCore Identity credential provider.

**Auto-approve hardcoded on.** An approval flag pinned to `True` for unattended demo operation
(pattern 3).

**Prompt-level approval gates.** "Requires approval" as a sentence in a system prompt (pattern 5).
A system prompt is not an authorization control — `production-rules.md` §21.

**Tests that assert nothing.** Print-based smoke scripts requiring live infrastructure, or suites
that test a re-implemented copy of the handler rather than the deployed one, so divergence goes
undetected.

---

## What sample code will not give you

Gaps that are consistently yours to build, whatever you start from:

- **An immutable / WORM audit log.** Traces and application tables are mutable. Nothing in a typical
  sample tree gives you a tamper-evident decision record. `control-stack.md` specifies what to persist;
  the append-only store underneath it is your build.
- **A composed multi-tenant platform.** The primitives are strong and available — IAM-scoped actor
  identity, namespace conventions, per-tenant keys, tenant-tagged traces — but assembling them into a
  working multi-tenant system is not demonstrated end to end anywhere. Assembly is yours.
- **A multi-account, multi-environment pipeline.** Infrastructure samples are overwhelmingly
  single-account, single-region, with local unlocked state. See `iac-hardening.md`.
- **Case-level four-eyes approval.** Change-control approval workflows, graph interrupt/resume and
  elicitation all exist as primitives, but a maker-checker gate on a *decision record* — two named
  humans, two timestamps, one immutable outcome — is not something you will find pre-built.
- **Evidence that the agent did what it said.** Verifying a claimed action against invocation
  metrics (`production-rules.md` §18) is a check you have to write.
- **Per-fact attribution across a multi-agent merge.** Not a gap in the samples but a limit of the
  telemetry model — "What a trace can attribute" above gives the reasons, and they do not change with
  better instrumentation.

One item has left this list: the **agent inventory** is now a managed catalogue with an approval
workflow rather than a table you maintain (pattern 7), Preview caveats and namespace migration
included. Note precisely what that did *not* close — it approves a registered resource, not a case
decision, so the four-eyes gate above stays yours.

The primitives and most of the mechanics are available. The audit layer, the tenancy composition and
the attribution checks are the parts you own.
