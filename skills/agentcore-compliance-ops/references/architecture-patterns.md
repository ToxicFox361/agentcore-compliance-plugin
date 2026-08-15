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
is what `guardrails.md` Layer 1 looks like when implemented rather than asserted. The capability
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
  scanning plus human sign-off before it goes live. `guardrails.md` (model risk governance) requires
  this; the state machine is how it is implemented.
- **Decision approval.** The same states map onto a SAR: auto-drafted → pending compliance-officer
  approval → filed or rejected.

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

Three mechanisms, used together, are what let you keep traces at all in a platform handling customer
data:

- **Redaction before export.** A span processor that strips configured attributes — prompt bodies,
  input messages — before telemetry leaves the process. Redaction after export is not redaction.
- **Anonymisation in the data path.** Guardrails masking PII in prompts and responses, plus log
  data-protection policies masking PII in application logs.
- **Tenant correlation without identity leakage.** Propagate a tenant identifier and session
  identifier onto every child span via context baggage, while explicitly *not* propagating the
  user's email or name. You get per-tenant trace correlation without putting a person in the trace.

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
  sample tree gives you a tamper-evident decision record. `guardrails.md` specifies what to persist;
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

The primitives and most of the mechanics are available. The audit layer, the tenancy composition and
the attribution checks are the parts you own.
