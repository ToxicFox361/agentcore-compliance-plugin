# Deploying AgentCore into a multi-tenant compliance platform

How to host agents, how to give them tenant data safely, and where the isolation boundaries sit.

All limits and quotes below are from AWS documentation as of the last revision, and every link was
checked to resolve. AgentCore moves quickly — **verify against live documentation before generating
code**, and treat any specific number here as a starting hypothesis to confirm against
[Quotas for Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
and the account itself.

**Contents** — read the section you need rather than the whole file.

| Section | Answers |
|---|---|
| [The core rule for data access](#the-core-rule-for-data-access) | How agents reach tenant data; which Gateway target type to pick |
| [Hosting: Harness or Runtime](#hosting-harness-or-runtime) | Which to choose; the Runtime contract; container vs direct code deployment |
| [Session and tenant binding](#session-and-tenant-binding) | Who owns the session-to-tenant mapping, and session lifecycle |
| [Close the Gateway bypass](#close-the-gateway-bypass) | Stopping direct Runtime invocation; command-execution APIs; payload validation |
| [Credentials](#credentials) | Token Vault, user-delegated vs autonomous, identity propagation via interceptors |
| [Policy as a deterministic control](#policy-as-a-deterministic-control) | Cedar controls that survive prompt injection — and proving they work |
| [Memory](#memory) | Tenant namespacing, and whether long-term memory is defensible at all |
| [Observability as audit evidence](#observability-as-audit-evidence) | What traces and CloudTrail do and do not prove |
| [Evaluations](#evaluations) | Built-in, LLM-as-judge and code-based evaluators |
| [Deployment topology](#deployment-topology) | Accounts, Regions, environments, per-tenant vs shared |
| [Infrastructure as code](#infrastructure-as-code) | Current CDK surface and packaging choices — see `iac-hardening.md` for the full audit |
| [Limits that shape design](#limits-that-shape-design) | Quotas worth designing around, and the `/ping` trap |
| [MMDSv2 enforcement](#mmdsv2-enforcement) | Now in effect — what to assert |
| [Post-deploy verification](#post-deploy-verification) | Why `READY`/`ENFORCE` proves nothing, and what to check instead |
| [Recommended starting architecture](#recommended-starting-architecture) | The whole thing on one page |

---

## The core rule for data access

> **Agents reach tenant data through the platform's authenticated API, never through direct
> database access.**

A compliance platform typically enforces tenant isolation with database row-level security, and
often encrypts PII per tenant with tenant-specific keys. Both properties are enforced in the
request path. An agent with a database connection bypasses:

- Row-level security scoped to the tenant session
- Per-tenant decryption (a raw `SELECT` returns ciphertext, not usable data)
- The audit trail that records who read what
- The API's own authorization and rate limiting

Routing agent reads through the same authenticated API a customer integration would use means
tenant isolation, encryption, and audit are inherited rather than reimplemented — and there is
one enforcement path to review rather than two.

**Mechanism:** AgentCore Gateway with a target pointed at the platform's public API, plus an
outbound credential provider. Gateway converts the target into MCP tools the agent calls.

```
Agent ──► Gateway ──► [target] ──► Platform API ──► RLS + per-tenant decryption ──► DB
             │
             └── Policy (Cedar) evaluates every tool call
```

**Choosing a target type.** The set of supported targets has grown — Lambda, OpenAPI, Smithy, MCP
server, and an AgentCore Runtime target, with more appearing in the CDK L2 than in the older
guides. Check
[Create an AgentCore gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-create.html)
and
[AgentCore Runtime targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-http-runtime.html)
for the current list before designing around one. The trade-offs that matter for a compliance
platform are stable even as the list moves:

| Target | Credentials | What you are choosing |
|---|---|---|
| **Lambda** | Gateway IAM role | You write and maintain tool definitions — and get a natural place to shape requests, enforce filters, and redact responses before the model ever sees them |
| **MCP server** | OAuth, API key | You already have (or will run) an MCP server; tool surface lives there, not in Gateway config |
| **OpenAPI** | OAuth, API key — no IAM | Least code: upload the spec. But no request shaping, and the tool surface is whatever the spec exposes, which is usually wider than an agent should have |
| **Smithy** | OAuth, API key | Same trade-off as OpenAPI, for Smithy-modelled services |

For regulated work the **Lambda target** is usually right, and the reason is not convenience: a
spec-driven target exposes the API's surface, whereas a Lambda target exposes *only the read
operations you deliberately wrote*. The gap between those two is the blast radius of a prompt
injection. Budget for writing and maintaining those tool definitions — that cost is the control.

---

## Hosting: Harness or Runtime

AWS publishes a feature-by-feature grid at
[AgentCore harness vs. Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html);
read it rather than trusting the summary below, which will drift. The shape of the decision:

| | Harness | Runtime |
|---|---|---|
| Orchestration loop | Managed (Strands-based) | Yours, any framework |
| Model / tools / prompt / skills | Config, no redeploy — including switching provider mid-session | Code, rebuild + redeploy |
| Memory, Gateway, Browser, Code Interpreter, outbound Identity, observability, streaming | Config | SDK calls you write |
| Execution limits (`maxIterations`, `timeoutSeconds`, `maxTokens`, idle and lifetime) | Config | You implement them |
| Choice of agent framework, bidirectional streaming, hooks, non-agent-loop patterns (graph/workflow) | **Not supported** | Supported |

Harness runs *inside* Runtime — same microVMs, same isolation. It is worth being precise that this
is an abstraction, not an extra security layer: per
[Harness security and access controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html),
the trust boundary is unchanged (IAM or JWT plus microVM isolation), and validating harness input
is still your job. Choosing Harness buys operational simplicity, not additional containment.

**Recommendation:** start with **Harness** for the workflows in `workflow-catalog.md` §1–3, §5, §6, §9.
They are tool-calling loops with a structured output, which is exactly Harness's shape, and model
or prompt changes become config rather than a deploy. Move to **Runtime** where you genuinely need
custom control flow — supervisor/specialist routing (§10), or pre/post-processing between turns.
Note that "graph/workflow style, non-agent-loop patterns" is an explicit ❌ for Harness, so a
LangGraph-style state machine forces Runtime.

Three of the defects catalogued in `production-rules.md` — the module-level agent object (§3),
unset max output tokens (§8), and placeholder substitution (§5) — are "you own the loop" defects
that a managed loop cannot have. Weigh that. Harness's `maxTokens` and `maxIterations` are the
config-level answer to §8 and to runaway loops; see
[harness observability and cost controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html).

**Runtime contract if you do own the loop** — from the
[HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html):
ARM64 container, host `0.0.0.0`, port **8080**, `POST /invocations` (JSON or SSE), `GET /ping`,
and an optional `/ws` for bidirectional streaming. MCP and A2A are separate protocol contracts
with their own shapes.

**Two ways to package it.** Container images are no longer the only option —
[direct code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html)
ships a zip to S3 with no Docker at all. The trade-off worth knowing for regulated work is who
patches what: with direct code deploy AWS patches the language runtime (but *not* past its end of
support — see
[supported language runtimes and deprecation policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html)),
whereas with a container image rebuilding on a current base image is yours. Package size limits
differ substantially between the two; check the quotas page before assuming your dependency tree
fits.

---

## Session and tenant binding

The microVM is the isolation boundary — one per `runtimeSessionId`, own CPU, memory, filesystem,
sanitised on termination. But:

> "AgentCore does not enforce session-to-user mappings — your client backend should maintain the
> relationship between users and their session IDs."
> — [runtime-sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)

> "Treat the `runtimeSessionId` as a server-side value derived from the authenticated end user —
> never accept it directly from untrusted client input."
> — [runtime-instances-security](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html)

AgentCore isolates the **compute**; your backend owns the **authorization mapping** from session
to tenant and user. Derive session IDs server-side, namespaced by the tenant resolved from the
resource record — see `production-rules.md` §7 for the implementation.

Same warning applies to the user-identity path: the
`X-Amzn-Bedrock-AgentCore-Runtime-User-Id` header is treated as *an opaque string without IdP
verification*. Docs recommend JWT-based identification in production, and explicitly denying
`GetWorkloadAccessTokenForUserId` and `InvokeAgentRuntimeForUser` in IAM where a JWT is always
available.

**Session granularity:** one session per (tenant, user, work item). Sharing a session across users
of a tenant leaks conversation state between analysts; sharing across tenants is a breach.

Two mechanics that catch people out. `runtimeSessionId` has a **minimum length** (33 characters at
last check — a UUID satisfies it), so a tenant-namespaced derivation scheme needs enough entropy to
clear it. And sessions end on their own: idle timeout terminates the microVM, maximum lifetime caps
it outright, and a request reusing the same ID afterwards silently gets a *new* execution
environment rather than an error. Design for that — nothing in the session is durable. See
[Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html).

### The Instances compute type breaks the one-microVM-per-session assumption

Everything above assumes the default **microVMs** compute type, where the isolation boundary and
the session are the same object. AgentCore also offers an **Instances** compute type, where agents
run on EC2 managed instances inside *your* account and VPC, provisioned through a **capacity
provider**. It buys 14-day sessions, GPUs, persistent EBS volumes across session stops, and your
own Savings Plans — and it moves the isolation boundary.

The line that matters for a multi-tenant compliance platform:

> "**Agents on an instance are not isolated from each other** — Multiple agents can run on the same
> instance and share its filesystem. Agents run on the instance either in containers or, for
> directly deployed agents, as processes directly on the instance — neither provides a security
> boundary between workloads on the same instance. All agents that share an instance must be
> mutually trusted."
> — [runtime-instances-security](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html)

Read that against the microVM model, where a session is a sanitised-on-termination VM with its own
filesystem and no path to another customer's workload. On Instances, a container is a packaging
format, not a boundary. Two agents landed on the same instance can read each other's files.

**The session is still the isolation unit — it just contains more.** A session is identified by
*capacity provider plus session ID* and maps 1:1 to an EC2 instance, and invoking a second runtime
that shares the capacity provider with the same `runtimeSessionId` deliberately lands that agent on
the same instance. So the tenant-binding rule from this section does not merely still apply, it
carries more weight: a session ID collision across tenants is no longer a leaked conversation, it
is two tenants' agents on one filesystem. Agents you intend to keep isolated must not share a
session, and in a compliance platform that means never co-locating tenants on an instance —
derive the session ID server-side, namespaced by tenant, exactly as above.

Two limits that shape the design:

- **20 agents per capacity provider session**, and this one is **not adjustable**. It caps how far
  a multi-agent collaboration on a shared instance can go before it needs a second session.
- **Your own account quotas apply on top of AgentCore's.** Because the instances are provisioned in
  your account, AgentCore consumes your EC2 (running instance count, `RunInstances`/`CreateFleet`
  request rates), EBS (volume counts, `CreateVolume`/`AttachVolume` rates), VPC (network
  interfaces) and EC2 Auto Scaling quotas. A fan-out that fits comfortably inside the AgentCore
  limits can still fail on an EC2 request-rate quota nobody thought to check. Capacity providers
  per account is 1,000, also not adjustable.

Unless you specifically need long sessions, GPUs or agents sharing a filesystem, the default
microVM compute type gives a stronger isolation story for free — and you cannot change the compute
type after a runtime is created. See
[Instances](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-how-it-works.html)
and the
[quotas page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html).

---

## Close the Gateway bypass

Policy, guardrails and interceptors are enforced **at the Gateway**, not the Runtime:

> "These controls only protect you if all traffic actually flows through the gateway. If a caller
> can reach the runtime directly, it bypasses the gateway's policies, guardrails, and interceptors
> entirely."
> — [runtime-security-best-practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html)

Restrict the Runtime so only the Gateway can invoke it — a resource-based policy for IAM (SigV4)
runtimes, or `allowedWorkloadConfiguration` on the authorizer for OAuth ones. A Cedar policy
protecting nothing is worse than none, because it produces false assurance in a control review.

Two details that decide whether this actually holds:

- **Both the runtime and the endpoint are evaluated.** For `InvokeAgentRuntime` and friends, AWS
  checks policies on the agent runtime *and* the agent endpoint, and both must allow. A
  resource-based policy on only one of them leaves the other open. See
  [Resource-based policies for AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/resource-based-policies.html).
- **There are command-execution APIs, and they bypass the model entirely.**
  `InvokeAgentRuntimeCommand` (one-shot) and `InvokeAgentRuntimeCommandShell` (interactive PTY over
  WebSocket) run commands inside the microVM with full access to its filesystem and credentials —
  no prompt, no Cedar policy, no guardrail in the path. AWS's own guidance is that not everyone who
  may invoke an agent should be able to execute commands on it. In a regulated deployment, deny both
  actions outright unless you have a named reason, and alarm on them if you don't.
  See [Execute shell commands in AgentCore Runtime sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html)
  and [Security best practices for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html).

**Also validate the payload, not just the caller.** AWS documents a concrete bypass: the `prompt`
field is parsed from arbitrary JSON, and if a caller sends structured content blocks — particularly
`toolUse` blocks — some frameworks dispatch the named tool immediately, *without model reasoning,
system prompt, or guardrails*. Enforce that the prompt is a string and strip `toolUse` blocks from
any caller-supplied message history. A guardrail that the request routes around is not a control.

---

## Credentials

Outbound credentials live in AgentCore Identity's **Token Vault**, backed by a Secrets Manager
secret in your account. You can bring your own secret (own KMS key, own rotation) — same Region
only, no cross-Region references. See
[AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
and
[Understanding credentials management](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-credentials-management.html).

At call time, Gateway obtains a workload access token bound to **agent workload identity + user
ID**, then exchanges it for the actual credential. **The agent code never sees the raw
credential** — which also means a prompt-injected agent cannot exfiltrate one.

Two outbound modes:
- **Autonomous** (client credentials) — agent acts as a service principal. Right for batch and
  enrichment workloads.
- **User-delegated** (authorization code) — agent acts with the end user's consent. Tokens are
  cached per user, so one user's token is unreachable while serving another.

For compliance work, user-delegated is usually correct for interactive workflows: the audit trail
then shows the agent acted *as* a named analyst, within that analyst's permissions, rather than as
an omnipotent service account.

### Propagate identity, don't filter results

The strongest data-access pattern goes further than scoping a shared credential — it forwards the
*end user's own identity* to the downstream system so that system enforces its native access
controls.

Mechanism: a **Gateway REQUEST interceptor Lambda** (`interceptorConfigurations`, with
`passRequestHeaders` set to true) receives every tool call, decodes the inbound JWT, performs an
**RFC 8693 OAuth token exchange** (`grant_type=urn:ietf:params:oauth:grant-type:token-exchange`)
against the downstream platform, and rewrites the `Authorization` header before forwarding.

Why this matters here: the downstream platform then applies **the individual analyst's**
permissions — RLS, per-tenant decryption, object-level ACLs — rather than a shared service
principal's. Tenant isolation is enforced by the system that owns the data, using the identity of
the person who actually asked. Nothing depends on the agent behaving correctly.

The alternative, a single service-principal credential for all users, means the agent can reach
any tenant's data and only application logic stops it. Prefer per-user propagation wherever the
downstream system can accept it, and treat the shared-credential form as a fallback you have to
justify.

Four constraints from
[Using interceptors with Gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html)
that shape the design:

- **One REQUEST and one RESPONSE interceptor per gateway, maximum, and Lambda only.** If token
  exchange consumes the REQUEST slot, every other pre-flight concern has to live in the same
  function. Plan that function as a small pipeline rather than assuming you can add another later.
- **`passRequestHeaders` defaults to false, and turning it on hands your interceptor the raw
  `Authorization` header.** AWS flags this explicitly: verify the function does not log what it
  now receives. In a compliance context an interceptor that logs bearer tokens is a credential
  disclosure that will pass every functional test.
- **Interceptors must be idempotent** — the gateway retries them on failure or timeout. A token
  exchange with a side effect (audit write, counter increment) will double-fire.
- **Scope the gateway execution role to the specific interceptor function ARNs**, not
  `lambda:InvokeFunction` on `*`.

Interceptors are also the right place for response redaction — stripping PII before it reaches the
model or the trace. Configuration details are at
[interceptor configuration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-configuration.html).

**Caveat worth knowing:** any code inside the microVM can read execution-role credentials from the
instance metadata endpoint. Scope the execution role to the minimum. Assume that anything the role
can do, a successfully injected agent can do.

---

## Policy as a deterministic control

Cedar policies evaluate every tool call at the Gateway, outside the agent's execution boundary:

```
permit(principal, action == AgentCore::Action::"...", resource == AgentCore::Gateway::"...")
when { context.input.amount <= 1000000 };
```

Default-deny in `ENFORCE` mode; `forbid` wins. Also available in `LOG_ONLY` mode — deploy there
first and read the logs before enforcing. Reference:
[Policy in AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html),
[Understanding Cedar policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html),
[example policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies.html).
Bedrock Guardrails attach through the same engine —
[guardrails in policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-guardrails-in-policies.html) —
so screening and authorization share one enforcement point.

For compliance platforms this is how you express controls that must not depend on prompt
compliance:
- Agents may call read tools only; write tools are forbidden outright
- A tool call carrying a tenant ID other than the session's is denied
- Tools touching filed-report data require an elevated principal

**Temporal policies** evaluate an action against the sequence of prior actions in the session,
catching patterns no single call reveals — an agent reading an unusual breadth of customer records
within one session, for instance.

Policy is a deterministic control that survives prompt injection, model change and framework
change. Prefer it over prompt instructions for anything that actually matters.

**Sizing.** Policies are individually small and the generated Cedar schema is bounded — the schema
grows with the *complexity of tool input parameters*, not just tool count, so a wide platform API
wrapped as many richly-typed tools can hit the ceiling before you expect it. If it does, split
gateways across separate policy engines rather than simplifying the policies. Current figures are
on the [quotas page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html);
verify them against your account rather than planning from a number written here.

> **`ENFORCE` means the policy is loaded, not that it is working.**
>
> A policy engine reporting `ENFORCE` tells you the control plane accepted your Cedar. It says
> nothing about whether the policy denies what you think it denies — a typo'd action name, a
> resource ARN that matches nothing, or a condition on an attribute the request never carries all
> produce a policy that is enthusiastically enforced and completely inert.
>
> Verify functionally, in both directions, against the deployed gateway: **make a call that should
> succeed and confirm it succeeds, then make a call that should be denied and confirm it is
> denied.** Only the second half proves anything, and only the first half proves you have not
> simply broken the agent. Run both as a post-deploy gate, not a one-off during development —
> a later policy edit can silently invert either result.

---

## Memory

Scoped by **actor** (the user or agent/user pair), **session**, and **namespace** for long-term
memories. Extraction behaviour depends on which strategy you configure — semantic, summarization,
user preference, episodic, or custom; see
[Memory strategies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html).

For a multi-tenant compliance platform:
- Namespace long-term memory by tenant at minimum. Never let a namespace span tenants.
- Consider whether long-term memory is appropriate at all. Extracted "facts" about a customer that
  persist across sessions are an unversioned, unauditable influence on later decisions — hard to
  defend when an examiner asks why a decision was made.
- Short-term (within-session) memory is uncontroversial and sufficient for most workflows here.
- There are caps on memory resources per Region *and* on strategies per memory resource. The
  per-resource strategy cap is the one that surprises people, because it constrains how many
  distinct extraction behaviours one memory can carry. Check both on the quotas page before
  designing a per-tenant or per-workflow memory layout.

Long-term extraction is **asynchronous** — memories are not immediately retrievable after an event.
Do not write a post-deploy check that asserts a memory is readable immediately after writing it;
it will be flaky for reasons that have nothing to do with your deployment.

---

## Observability as audit evidence

Built on OpenTelemetry, surfaced in CloudWatch GenAI Observability: agent metrics, session views,
and traces with per-span tool parameters, latency and token usage. Automatic when hosted on
Runtime — no OTEL libraries to add. It does require a **one-time per-account** enablement of
[CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html)
before spans and traces are visible at all, which is the usual reason a correctly instrumented
agent appears to emit nothing. Overview:
[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html);
setup: [get started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html).

Traces are operational telemetry — **not** a compliance audit record. Retention, schema and
availability are not under your control. Persist your own decision record (see `guardrails.md`) to
an append-only store. Use traces to debug; use your own records to answer examiners.

Three things to get right in a regulated deployment:

- **Redirecting OTLP to a third-party backend is a move, not a copy.** Runtime-hosted agents are
  auto-instrumented to CloudWatch; pointing the exporter at a vendor is what stops traces arriving
  there. Note also that the ADOT *Collector* is explicitly unsupported for agent observability —
  the sanctioned paths are the ADOT SDK or the Lambda layer — which rules out the collector as the
  obvious place to fan out to two destinations. If CloudWatch is your system of record, confirm it
  still receives spans after any vendor integration, rather than assuming both get a copy. If the
  motivation is central visibility across accounts,
  [cross-account observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-cross-account.html)
  solves that without leaving CloudWatch.
- **Traces carry raw prompts and tool arguments verbatim.** In a compliance context those payloads
  contain customer PII, so your trace backend inherits the data-protection obligations of the
  platform — including whichever third-party vendor you just exported to. AWS lists filtering
  sensitive data out of observability attributes and payloads as a best practice; it is not on by
  default, and instrumenting redaction is
  [configuration you write](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html).
- **CloudTrail does record the data plane — but only the call, not the content.** `InvokeAgentRuntime`,
  `InvokeAgentRuntimeCommand`, `InvokeAgentRuntimeCommandShell` and the control-plane operations are
  logged with caller identity, timestamp, source IP and response status. That answers *who invoked
  which agent, when*. It does not answer *what they asked*, which lands in the agent's CloudWatch
  log group. Correlate the two by request ID if you need the whole picture, and set metric filters
  and alarms on the command-execution APIs. Do not assume either is enabled and retained to your
  policy by default.

Session correlation is available via OTel baggage (`session.id`) and framework trace attributes,
so spans can be filtered per session — useful, but it is the floor, not an audit trail.

**Exception worth noting:** the Browser tool's
[session recording](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-session-recording.html)
captures DOM changes, actions, console and network events to your own S3 bucket, replayable with a
timeline. Where an agent gathers evidence from a system that has no API, that recording *is* a
defensible record of exactly what it saw and did.

---

## Evaluations

[AgentCore Evaluations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluations.html)
scores OTEL traces. Three evaluator kinds are worth distinguishing, because they are not
interchangeable for compliance work:

- **Built-in evaluators** — pre-configured LLM-as-judge with AWS's prompt templates and scoring
  scales. The catalogue grows; check
  [Evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/evaluators.html)
  rather than assuming a fixed count.
- **Custom LLM-as-judge** — your model, instructions and scoring schema. Right for judgement-shaped
  criteria: did the assessment name mitigating factors, is the reasoning coherent.
- **[Custom code-based evaluators](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)** —
  your own Lambda, deterministic. Right for anything that must be *reproducible under examination*:
  schema conformance, every claim carrying a citation, no forbidden assertion type. Prefer these
  over a judge wherever the check can be expressed as code, because a judge's score is itself a
  model output and inherits every reliability question you are trying to answer.

Runs are on-demand, batch, or
[**online**](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/online-evaluations.html) —
sampling live production traffic continuously. Where you have known-correct answers,
[ground-truth evaluation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/ground-truth-evaluations.html)
compares against them directly.

Map this onto the monitoring in `guardrails.md`: on-demand runs against the golden set on every
prompt change; online evaluation as the continuous drift signal. Evaluation throughput is itself
quota-bound (tokens per minute, spans per evaluation, evaluators per configuration), so a
continuously-sampling online configuration over high alert volume needs a sampling rate chosen
against those limits rather than set to "everything".

---

## Deployment topology

**Account separation.** Agent infrastructure in its own AWS account, separate from the platform's
data plane. The agent account holds no tenant data; it reaches data only through the platform API
across an authenticated boundary. This makes the trust boundary an account boundary — the easiest
kind to audit.

**Region.** Deploy in the region where tenant data resides — and confirm AgentCore is actually
available there, since its
[supported Regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
are a subset of Bedrock's. Use geographic inference profiles (`eu.`, `us.`) rather than `global.`
where data residency is a commitment: geographic profiles keep inference within the geography,
global profiles route anywhere. See
[cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
and the [supported Regions and models for inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html) —
which profiles exist for which models changes often enough that this is a lookup, not a memory.

**Environment separation.** Separate accounts per environment. Note that a brand-new AWS account
can have **zero** Bedrock quota until verified (`production-rules.md` §10) — provision
environment accounts before you need them, and consider a
[Service Quotas request template](https://docs.aws.amazon.com/servicequotas/latest/userguide/organization-templates.html)
on the organization so new accounts inherit quota automatically.

**Per-tenant vs shared agents.** Shared runtimes with session-level isolation is the default and
is what the microVM model is designed for. Reach for per-tenant runtimes only where a specific
requirement demands it — a tenant-specific model, a contractual isolation commitment, or a
regulator that requires it. Per-tenant runtimes multiply deployment and version-management cost
quickly.

For the strongest multi-tenant isolation, use **distinct IAM principals per tenant** to invoke:
IAM then enforces scoping rather than your application code, which removes the shared-principal
session-routing risk entirely.

---

## Infrastructure as code

`iac-hardening.md` covers flavour choice and the defect list in full. Three points belong here
because they shape the deployment topology rather than the code:

**The CDK story has changed — check before reaching for the old workarounds.** AgentCore L2
constructs now live in stable `aws-cdk-lib` under
[`aws_bedrockagentcore`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)
(`Runtime`, `RuntimeEndpoint`, `Gateway`, `GatewayTarget`, `Memory`, `BrowserCustom`,
`CodeInterpreterCustom`, `OnlineEvaluation`), rather than only in an alpha package or as raw L1
`Cfn*` resources. Guidance written against the alpha module — or against the era when CDK could not
package an agent at all — is now misleading. If you inherit a stack built around a CodeBuild
project and a polling custom resource purely to produce an image, that scaffolding is very likely
replaceable; check the current `AgentRuntimeArtifact` factory methods before maintaining it.

**Packaging is a real choice now, not a formality.** The artifact can come from a local Dockerfile
asset, an existing ECR image, a code asset packaged for direct code deployment, or a zip already in
S3. Direct code deployment removes Docker from the deploy path entirely, which removes the ARM64
cross-build, the privileged build environment, and the image-scanning step along with it. It also
moves language-runtime patching to AWS. For a compliance platform that is usually the better
default; reach for a container image when you need system-level dependencies, a specific base
image, or a package larger than the direct-deploy size limit.

**Multi-account and multi-environment pipelines are yours to build.** Public examples are
overwhelmingly single-account, single-Region, with variability supplied through environment
variables rather than per-stage environments or a deployment pipeline. Expect to design
dev/stage/prod separation, remote state or stack separation per environment, and promotion between
them yourself — and see `iac-hardening.md` for the specific gaps to close before regulated data is
in scope.

---

## Limits that shape design

Values below were current at the last revision and several are adjustable, so **re-verify against
[the quotas page](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)
and against the account** before you design around any of them — an adjustable default that someone
already raised, or lowered, will change the answer.

| Limit | Value | Design impact |
|---|---|---|
| Runtime request timeout | 15 min, fixed | Long work must be async |
| Async job max duration | 8 hrs | The real ceiling for background work |
| Streaming max duration | 60 min | Separate from both of the above |
| Idle session timeout | 15 min default, adjustable (`idleRuntimeSessionTimeout`) | Return `HealthyBusy` from `/ping` during background work |
| Max session lifetime | 8 hrs default, adjustable (`maxLifetime`) | Not for durable state — use Memory |
| Max payload | 100 MB | Ample for case data; not for bulk exports |
| Gateway invocation timeout | 15 min, adjustable | |
| Targets per gateway / tools per target | 100 / 1,000, both adjustable | Plenty for one platform API |
| Session creation rate | 25 TPS/account, adjustable | Shared across all endpoints — consider for batch fan-out (§6, §9) |
| Data plane request rate | 1,000 TPS/account, adjustable | Shared across *all* data plane APIs, not per-API |
| Memory resources per Region | 150, adjustable | Rules out naive per-tenant memory resources at scale |
| Session hardware | 2 vCPU / 8 GB, fixed | |

**A `/ping` trap worth knowing.** The optional `time_of_last_update` field must be set only on an
actual status change. Advancing it on every ping signals continuous change, the idle timeout never
fires, and sessions live until `maxLifetime` — quietly consuming the session quota and billing for
microVMs nobody is using. If you use the AgentCore SDK the ping response is handled for you; if you
hand-rolled `/ping`, check this. See the
[HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
and [handling long-running agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html).

**Note the caller's timeout too.** API Gateway REST integrations cap at 29 seconds regardless of
what AgentCore allows — see `production-rules.md` §11.

---

## MMDSv2 enforcement

Enforcement began **June 30, 2026** and is now in effect: AgentCore rejects invocations to runtimes
whose `metadataConfiguration` is unset, or has `requireMMDSV2` false or null, returning a
`ValidationException`. Newly created runtimes generally default to enabled — verify with
`get-agent-runtime` and fix any older runtime by calling
[`UpdateAgentRuntime`](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
with `requireMMDSV2: true`. Existing sessions are unaffected by the update; new invocations succeed
afterwards. This is a good thing to assert in a deployment pre-flight rather than discover from a
`ValidationException` in production —
[troubleshooting](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)
covers the error.

Understand what it does and does not buy you. MMDSv2 hardens the metadata endpoint against
request-forgery-style access; it does not change the fact that *any* code inside the microVM can
read execution-role credentials from that endpoint legitimately. Scoping the execution role remains
the actual control.

---

## Post-deploy verification

The most common way a compliance agent deployment is wrong is not that it failed — it is that every
resource reports healthy and the thing still does not work. Status is a statement about the control
plane, not about behaviour.

**Status is not evidence.** A runtime at `READY`, a policy engine at `ENFORCE`, a target at
`ACTIVE` — each of these proves the resource was accepted and loaded. None of them proves it
behaves correctly. A Cedar policy referencing an action name that does not exist enforces nothing;
a Gateway target pointing at the wrong function ARN resolves and lists tools; an IAM policy missing
the inference-profile ARN (`production-rules.md` §1) shows no sign of trouble until the first model
call. Build post-deploy checks that **exercise the real path in both directions**: a call that
should succeed and does, and a call that should be refused and is. The negative case is the one
that carries the information, and it is the one people skip.

**Absence of a CloudWatch log group is proof a Lambda was never invoked.** This is the fastest
unambiguous check available for "is this target genuinely wired up" — not an empty log group, *no
log group*, which means the function has never run and therefore was never called. It costs one
`describe-log-groups` call and it settles the question that agent narrative cannot: `production-rules.md`
§18 records an agent reporting a write that never happened, caught exactly this way. Use it as a
standing post-deploy assertion for every Lambda target and interceptor, and reach for it first
whenever an agent claims an action succeeded and the system of record disagrees.

One qualification, because this section is about not trusting misleading signals: a missing log
group is unambiguous *once you have confirmed the function's execution role can write logs at all*.
A role without `logs:` permissions produces the same absence for a function that ran perfectly
(`production-rules.md` §6). Check the grant once, then the absence means what you want it to mean.

Sequence matters as much as content: persist the deployment record *before* running verification,
so a slow-failing check cannot orphan a live resource (`production-rules.md` §4). Verification is a
diagnostic, never a gate on durability.

---

## Recommended starting architecture

```
Analyst (authenticated, JWT)
      │
      ▼
Platform backend ─── derives session ID (tenant-namespaced, server-side)
      │              resolves tenant from resource record, never request body
      ▼
AgentCore Gateway ── Cedar Policy (ENFORCE) evaluates every tool call
      │              REQUEST interceptor: RFC 8693 token exchange → analyst's own identity
      │              inbound: OAuth/JWT · outbound: user-delegated
      ├──► Lambda target ────► purpose-built read tools ──► Platform API
      │                                                    └─► RLS + per-tenant decryption
      ▼
AgentCore Harness ── microVM per session · model + prompt as config
      │              command-execution APIs denied in IAM
      ▼
Structured proposal ──► deterministic validation ──► human decision (append-only record)
```

The properties that matter: the agent never touches the database, never holds a credential, cannot
write, cannot choose its own session, and cannot dispose of anything. Every one of those is
enforced by infrastructure rather than by prompt.

And every one of them should be *demonstrated* before the deployment is called done — a denied
write attempt, a cross-tenant tool call that fails, a session ID from a request body that does not
route. A control you have only configured is a control you are hoping for.
