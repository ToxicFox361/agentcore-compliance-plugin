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
| [Hosting: Harness or Runtime](#hosting-harness-or-runtime) | Which to choose; the Runtime and protocol contracts; container vs direct code deployment; the endpoint step |
| [What Harness moves into the request](#what-harness-moves-into-the-request) | Which controls become caller-supplied on Harness, and what your backend must strip |
| [Session and tenant binding](#session-and-tenant-binding) | Who owns the session-to-tenant mapping, session lifecycle, and what persists |
| [Close the Gateway bypass](#close-the-gateway-bypass) | Stopping direct Runtime invocation; command-execution APIs; payload validation |
| [Credentials](#credentials) | Token Vault, user-delegated vs autonomous, identity propagation via interceptors |
| [Policy as a deterministic control](#policy-as-a-deterministic-control) | Cedar controls that survive prompt injection — and proving they work |
| [Memory](#memory) | Tenant namespacing, and whether long-term memory is defensible at all |
| [Observability as audit evidence](#observability-as-audit-evidence) | What traces and CloudTrail do and do not prove |
| [Evaluations](#evaluations) | Built-in, LLM-as-judge and code-based evaluators |
| [Deployment topology](#deployment-topology) | Accounts, Regions, environments, per-tenant vs shared |
| [Infrastructure as code](#infrastructure-as-code) | Current CDK surface and packaging choices — see `iac-hardening.md` for the full audit |
| [Limits that shape design](#limits-that-shape-design) | Quotas worth designing around, the `/ping` trap, retry policy per path |
| [Prompt caching](#prompt-caching) | Why a compliance prompt caches unusually well, and how it fails silently |
| [MMDSv2 enforcement](#mmdsv2-enforcement) | Now in effect — what to assert |
| [Post-deploy verification](#post-deploy-verification) | Why `READY`/`ENFORCE` proves nothing, real Gateway target states, and what to check instead |
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

That recommendation comes with a condition, and it is not optional: what Harness turns into config,
`InvokeHarness` also lets a caller override per request. Read
[What Harness moves into the request](#what-harness-moves-into-the-request) before exposing a harness
to anything but your own backend — four of this skill's controls depend on what that backend strips.

Three of the defects catalogued in `production-rules.md` — the module-level agent object (§3),
unset max output tokens (§8), and placeholder substitution (§5) — are "you own the loop" defects
that a managed loop cannot have. Weigh that. Harness's `maxTokens` and `maxIterations` are the
config-level answer to §8 and to runaway loops; see
[harness observability and cost controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html).

**Runtime contract if you do own the loop** — from the
[HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html):
ARM64 container, host `0.0.0.0`, port **8080**, `POST /invocations` (JSON or SSE), `GET /ping`,
and an optional `/ws` for bidirectional streaming.

**That is one of four contracts, and this file deliberately does not carry the other three.** AWS
maintains protocol contracts for HTTP, MCP, A2A and AG-UI, and they differ in port, health-check
path and — for A2A — an agent-card file the container must serve. The protocol has to be chosen
**before the container is built**, because changing it means rebuilding and redeploying, which makes
it a design decision rather than a configuration one. Take the current specification from the global
`amazon-bedrock` skill or from AWS's protocol-contract pages rather than from anything written here.
The reason for that instruction is concrete: AWS's own two pages currently disagree with each other
on the HTTP health-check path, on three of the four ports, and on the A2A card filename. A skill
carrying a copy would simply be wrong in a way nobody notices until a container fails its health
check. The HTTP figures above match the runtime-level document — the likelier authority — and are
what to build against for the workflows in this skill.

**The data plane, for the backend that calls it.** The operation is `InvokeAgentRuntime`, the path is
`POST /runtimes/{agentRuntimeArn}/invocations`, and the SigV4 signing service name is
**`bedrock-agentcore`** — *not* `bedrock-agentcore-control`, which signs the control plane. Signing
with the wrong service name produces a signature mismatch that reads like a credentials problem.
Streaming is requested with `accept: text/event-stream` and confirmed by the agent answering
`Content-Type: text/event-stream`. The session header is protocol-dependent, which catches people
who move an existing client across protocols: `Mcp-Session-Id` for MCP, and
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` for HTTP, A2A and AG-UI.

**Two ways to package it.** Container images are no longer the only option —
[direct code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy.html)
ships a zip to S3 with no Docker at all. The trade-off worth knowing for regulated work is who
patches what: with direct code deploy AWS patches the language runtime (but *not* past its end of
support — see
[supported language runtimes and deprecation policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html)),
whereas with a container image rebuilding on a current base image is yours.

Package size is the constraint that decides it for some dependency trees: a **container image is
capped at 2 GB**, while **direct code deploy is capped at 250 MB compressed / 750 MB uncompressed**,
and none of those three numbers is adjustable. A dependency tree that fits comfortably in a container
will not always fit direct code deploy — an ML-heavy agent pulling a scientific stack is the usual
casualty — so check your artifact size before committing to the packaging model, because switching
later changes the build pipeline, the patching story and the IaC resource shape together.

**Creating the runtime is not the last step — the endpoint is.** AWS's deployment procedure makes
Runtime Endpoint creation an explicit step with its own readiness poll:
[`CreateAgentRuntimeEndpoint`](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntimeEndpoint.html),
then `get-agent-runtime-endpoint` until it reports `READY`. The runtime is **not invocable until the
endpoint is active**. A `DEFAULT` endpoint is created for you, and the visible evidence of it is the
log group name — `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>` — but any *named*
endpoint you rely on for version pinning is yours to create and yours to wait for. So poll the
endpoint, not just the runtime: a runtime sitting at `READY` behind an endpoint that is not is
exactly the failure shape the "persist before verify" rule (`production-rules.md` §4) exists to
contain, because the deployment reads as finished and the first invocation does not work.

---

## What Harness moves into the request

Defaulting to Harness moves a set of decisions out of your container and into the API call. That is
the point of it. It also means **four claims made elsewhere in this skill stop holding the moment
caller input reaches `InvokeHarness` unfiltered** — and because the whole appeal of Harness is that
config changes need no redeploy, nothing about the deployment will look different when they stop
holding.

`InvokeHarness` can override the harness configuration **for a single call, without redeploying**.
The overridable fields are `model`, `systemPrompt`, `tools`, `allowedTools`, `skills`,
`maxIterations`, `maxTokens`, `timeoutSeconds` and `actorId`. In a multi-tenant compliance platform
that list is an attack surface, and it is worth reading it as one:

| Override | What it breaks |
|---|---|
| `tools` / `allowedTools` | "Enforce it in the tool list the model is offered, never in the prompt" (`control-stack.md`, `production-rules.md` §21) — the control this skill leans on hardest. On Harness the **caller** supplies the list, so a narrowed read-only tool surface becomes a default rather than a boundary. |
| `skills` | Skills are fetched per session (AWS Skills, Git, Amazon S3, session filesystem) and injected as **trusted context including any scripts they carry**; an invoke-time skill with the same name overrides the harness default; and **there is no IAM condition key that can restrict this field**. This is arbitrary code arriving in the agent's context, not a tool call you can review or a policy you can write. |
| `actorId` | Memory is scoped per actor. One backend service principal serving many analysts, plus a caller-chosen `actorId`, is one analyst reading another's long-term memory. This is the session-ID isolation problem (`production-rules.md` §7) arriving through Memory instead of through the session. |
| `model` / `systemPrompt` | The model and prompt recorded on the decision record become caller-chosen, which undoes the reproducibility the record exists for. Worse, `model.additionalParams` is passed to the provider **unchanged**, so a LiteLLM `apiBase` can redirect the inference call to an endpoint you do not operate. |

**The rule: your backend constructs the `InvokeHarness` request and *strips* every override field.**
Strips, not validates. Start from a request you built and add back only what a caller has a stated
reason to set; do not start from the caller's request and remove what looks dangerous. The
difference is not stylistic — the field list grows, and a validating filter forwards the next field
AWS adds while a stripping constructor does not. For `skills` in particular there is no other
control available: no IAM condition key, no Cedar policy in that path, nothing at the Gateway. The
application layer is the entire defence.

**Execution limits are overrides too.** `maxIterations`, `maxTokens` and `timeoutSeconds` are the
config-level answer to runaway loops and to the unset-max-output-tokens defect
(`production-rules.md` §8) — and all three are in the overridable list. Set them explicitly on the
harness *and* strip them from caller input, because a limit the caller can raise is a budget, not a
limit.

**Inbound auth is one method at a time, and the difference that matters is verification, not
capability.** A harness accepts SigV4 when it has no `authorizerConfiguration` and OAuth JWT when it
does. There is no mixed mode; it rejects the wrong credential type outright, and switching means a
separate version per auth type rather than an in-place edit.

Both paths can carry a per-user identity downstream: the user-ID header reaches
`GetWorkloadAccessTokenForUserId` and the on-behalf-of token exchange on either. The decisive
difference is that on the SigV4 path **the platform treats that user ID as an opaque string it does
not verify** — nothing ties it to an authenticated end user, so a backend that forwards a
client-supplied value has an audit trail naming whoever the client claimed to be. On the OAuth JWT
path the identity is validated against the issuer, which is why AWS recommends JWT for production.

So SigV4 is usable, conditionally: the user ID must be **derived server-side** from the authenticated
session and never accepted from the request, exactly as session IDs are. If your backend cannot
guarantee that, the identity on the record is decorative and JWT is the only honest option. Either
way, monitor the token-exchange calls in CloudTrail — that is AWS's own compensating control, and it
is what makes a forged user ID detectable rather than invisible.

When you configure JWT, set `allowedAudience` and `allowedClients`: an authorizer with neither
accepts any valid token from the issuer, including a token minted for a different service entirely.

**VPC mode needs a NAT gateway, and the failure arrives late.** A VPC-mode harness pulls its
container from **Amazon ECR Public** at the start of *every session*, and ECR Public has no VPC
endpoint. VPC mode therefore requires a NAT gateway with a route to an internet gateway, or sessions
fail at start with an image-pull timeout — *after* the harness has reported healthy. That is why this
one surfaces as a mysterious invocation failure rather than a deployment failure, and why it is worth
asserting in a pre-flight rather than diagnosing under pressure.

**Harness IAM, including the grant everybody misses.** `InvokeHarness` needs **both**
`bedrock-agentcore:InvokeHarness` and `bedrock-agentcore:InvokeAgentRuntime` — the harness call and
the underlying runtime call are authorised separately. `CreateHarness` needs
`bedrock-agentcore:CreateHarness` plus **`iam:PassRole`** for the execution role, plus
`GetAgentRuntime`, `CreateAgentRuntime`, `GetMemory` and `CreateMemory`, because it creates those
resources on your behalf. Omitting `iam:PassRole` is the most common cause of a `CreateHarness`
`AccessDenied`, and the error message does not name it.

**`harnessName` is capped at 40 characters** (letters, digits and underscores, starting with a
letter). Worth stating plainly because the name-validation regex in `examples/agent_runtime.tf`
allows 48 — correct for a runtime name, and wrong the moment somebody reuses it for a harness.

**Harness inference config is a different shape from Bedrock's**, which catches people porting
configuration across and changes how one of this skill's rules is implemented:

```json
{"model": {"bedrockModelConfig": {
  "modelId": "<model-id>",
  "maxTokens": 4096, "temperature": 0.7, "topP": 0.9,
  "apiFormat": "converse_stream",
  "additionalParams": {}
}}}
```

Four differences from Bedrock's `InferenceConfiguration`: the union variant key is
**`bedrockModelConfig`**, not a bare `bedrock`; the tuning parameters are **flat, with no
`inferenceConfig` wrapper**; there is **no `stopSequences` member** in that shape; and
`additionalParams` is the route for anything the shape does not name, including `top_k`. Note also
that `systemPrompt` is a **list of content blocks**, not a string.

So the rule about recording the parameters actually sent has a different worked form here. On Harness
the thing to persist is the **resolved** `bedrockModelConfig` and the **resolved** `systemPrompt`
list — captured after your backend has applied its defaults and stripped the caller's overrides,
because that, and not the harness configuration on file, is what the model actually ran under.

Current field lists, provider variants and the security model are in the global `amazon-bedrock`
skill (`amazon-bedrock/references/agentcore-harness.md`), the
[CreateHarness](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateHarness.html)
and [InvokeHarness](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html)
API references, and
[Harness security and access controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html).

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

**The session id does not travel by itself.** If the authorization mapping is enforced downstream —
by a Gateway REQUEST interceptor looking the session up and injecting the resolved scope — note that
the Gateway does **not** propagate the AgentCore runtime session id to that interceptor. The
`gatewayRequest` it receives carries `headers`, `body`, `httpMethod`, `path` and a `context` holding
only `identity`; there is no runtime session id in it and no `mcp-session-id` header. The agent has
to send its own session id as a request header for the binding to be findable at all, which is safe
for a specific reason and not in general: the header names a session, while the binding behind it
stays server-written. See `production-rules.md` §30 for the header, the fail-closed conditions that
make it safe, and the diagnostic.


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
it outright, and a request reusing the same ID afterwards silently gets a *new* execution environment
rather than an error.

**What survives that, and what does not.** In-memory state never survives. The replacement execution
environment starts clean, so anything an agent stashed in a module-level variable or a process-local
cache is gone — which is one more reason the module-level agent object (`production-rules.md` §3) is a
defect rather than an optimisation. Filesystem state *can* survive, but only because you asked for
it: configure **session storage** (`filesystemConfigurations` on the API,
`session_storage { mount_path }` in Terraform) and data written under the mount path persists across
stop and resume cycles, capped at **1 GB per session**. The session-state vocabulary explains the
behaviour worth designing against — a session is **Active**, **Idle** or **Stopped**; a Stopped
session returns to Active on the next invoke; and the session stays valid until the **runtime ARN
itself is deleted**, not until some shorter clock expires.

For a compliance platform this cuts both ways, and both directions need a decision. It is a
legitimate persistence option for case work that spans an analyst's day without pushing intermediate
state through Memory. It is also a **data-at-rest surface inside the session** — a place customer PII
can accumulate outside your database, outside your retention schedule and outside your erasure
tooling, in a location that the "nothing in the session is durable" mental model says cannot exist.
If you enable session storage it joins the PII inventory, with an encryption and retention answer of
its own; if you do not need it, leaving it unconfigured is the cheaper control. See
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

Outbound credentials live in AgentCore Identity's **Token Vault**, backed by a Secrets Manager secret
in your account — same Region only, no cross-Region references. See
[AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
and
[Understanding credentials management](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-credentials-management.html).

**Who owns that secret depends on the provider type, and getting it wrong is expensive in a regulated
shop.** For **API-key credential providers** the service creates, encrypts and stores the key in
Secrets Manager *itself*: the create response hands back an `apiKeySecretArn`, you must **not** create
the secret manually, and rotation goes through **`update-api-key-credential-provider`** — explicitly
*not* `secretsmanager rotate-secret` against the service-managed secret, which is precisely the
instrument a compliance function will reach for and which breaks the provider. OAuth credential
providers follow the same service-managed pattern.

The consequence to plan for: if your control set says "customer-managed KMS key and an automated
rotation schedule on every secret" — and in a supervised environment it usually does — then Token
Vault provider secrets need a documented exception, satisfied by *rotation through the AgentCore API*
rather than by a Secrets Manager rotation schedule. Write that down before the control owner finds the
secret in an inventory and configures rotation on it. **One claim here needs confirming rather than
designing around:** whether any provider type genuinely accepts a bring-your-own secret with your own
CMK and your own rotation. That claim circulates, this skill can no longer verify it, and the failure
mode of assuming it is an unrotatable production credential — so treat it as unconfirmed and check the
current API reference for the specific provider type you need.

**Ordering is mandatory:** create the credential provider **before** the gateway target that uses it,
or target creation fails with a "credential provider not found" error. This bites in IaC, where
ordering is inferred from references — if the target does not reference the provider directly, declare
the dependency explicitly rather than relying on luck in the graph.

**And prefer not to reach for an API key at all.** AWS's own preference is the right one to adopt
here: an API key is a long-lived credential, whereas IAM is ephemeral and auto-rotated, and OAuth
carries the identity of a person. Use IAM where the target supports it, OAuth where the target
supports it and a human is in the loop, and an API key only where neither is available — then treat it
as the thing you have to justify in a control review, with the rotation path above named in the
justification.

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
- **6 strategies per memory resource, and that cap is not adjustable** — the account-level ceiling is
  900 strategies and that one is. The per-resource cap is the one that surprises people, because it
  constrains how many distinct extraction behaviours a single memory can carry, and a memory
  configured with semantic extraction, summarization, user preference and two custom strategies is
  already at five. Memory resources per Region is a separate quota (150, adjustable — in the table
  below). Design a per-tenant or per-workflow memory layout against the per-resource cap first, since
  it is the one you cannot raise by asking.

Long-term extraction is **asynchronous** — memories are not immediately retrievable after an event.
Do not write a post-deploy check that asserts a memory is readable immediately after writing it;
it will be flaky for reasons that have nothing to do with your deployment.

---

## Observability as audit evidence

Built on OpenTelemetry, surfaced in CloudWatch GenAI Observability: agent metrics, session views,
and traces with per-span tool parameters, latency and token usage. Runtime-hosted agents are
auto-instrumented for those built-in spans. It does require a **one-time per-account** enablement of
[CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html)
before spans and traces are visible at all, which is the usual reason a correctly instrumented
agent appears to emit nothing. Overview:
[AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html);
setup: [get started](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html).

**"Nothing to add" is true of the built-in spans and false of the two things you probably came for.**
AgentCore **Evaluations** reads specific OTEL attributes — agent input, agent output, tool calls with
their inputs and outputs, latency per step — and an evaluation configuration pointed at traces that do
not carry them cannot work at all. The failure presents as an evaluation problem (empty results,
nothing to score) rather than as the instrumentation problem it is. Token and GenAI usage metrics are
not published automatically either, which matters for the per-tenant cost attribution this platform
needs. Both require the **ADOT SDK in your agent code**, so budget for it during the build rather than
discovering it when the first evaluation run comes back with nothing. That is not in tension with the
point below that the ADOT *Collector* is unsupported — the SDK is a sanctioned path and the collector
is not.

Traces are operational telemetry — **not** a compliance audit record. Retention, schema and
availability are not under your control. Persist your own decision record (see `control-stack.md`) to
an append-only store. Use traces to debug; use your own records to answer examiners.

Five things to get right in a regulated deployment:

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
- **The invoke APIs are CloudTrail *data* events, so by default they are not logged at all.**
  `InvokeAgentRuntime`, `InvokeAgentRuntimeCommand` and `InvokeAgentRuntimeCommandShell` are recorded
  as **data events** under `resources.type` `AWS::BedrockAgentCore::Runtime`. Trails do not record data
  events unless you configure them to, and data events **never appear in Event History** — so a fresh
  account has no record whatsoever of who invoked which agent, and the console offers no hint that
  anything is missing. This is off, not merely unreliable. Turn it on explicitly:

  ```bash
  aws cloudtrail put-event-selectors --trail-name <trail> \
    --advanced-event-selectors '[{
      "Name": "AgentCore runtime data events",
      "FieldSelectors": [
        {"Field": "eventCategory", "Equals": ["Data"]},
        {"Field": "resources.type", "Equals": ["AWS::BedrockAgentCore::Runtime"]}
      ]
    }]'
  ```

  `put-event-selectors` **replaces** the trail's selectors rather than appending to them, so read the
  existing configuration first and re-send it alongside this one — otherwise turning agent logging on
  turns something else off. Control-plane operations (`CreateAgentRuntime`, `UpdateAgentRuntime`,
  `CreateHarness` and the rest) are management events and are logged by default. Set metric filters and
  alarms on the two command-execution APIs; and do not assume anything is retained to your
  record-keeping policy by default.
- **What a data event proves — and the alarm that follows from it.** The event carries caller identity,
  timestamp, source IP, response status **and the target `sessionId`, in the same record**. That
  combination is worth more than an audit trail: because the authenticated principal and the session it
  routed to appear together, a principal invoking a session that does not belong to it becomes a
  *detectable event* rather than something you infer afterwards. In a platform where sessions are
  server-derived and tenant-namespaced (`production-rules.md` §7), that is a concrete metric filter and
  alarm on cross-principal session routing — the one signal that catches the session-isolation failure
  this file keeps warning about *while it is happening*. It still does not answer *what they asked*,
  which lands in the agent's CloudWatch log group; correlate by request ID for the whole picture.
- **Bedrock model invocation logging is a fourth PII sink, and it is nobody's default.** Separate from
  AgentCore observability, opt-in, off until somebody switches it on, it writes **complete prompts and
  responses** to CloudWatch Logs and/or S3, with its own log group, its own retention setting and its
  own KMS decision. Two consequences. First, it is what makes Guardrails PII masking irrelevant to your
  log estate: masking shapes what the model and the user see, not what the invocation log captured on
  the way through. Second, somebody will switch it on wanting an audit record of what the agent asked
  the model — and it is the wrong instrument for that, plainly. It is service-managed, its retention
  lives somewhere other than your record-keeping system, its schema is not a stable contract, it is not
  tamper-evident, and it is a full-fidelity copy of every prompt including the PII the rest of this file
  works to keep out of places you do not control. Build the audit record properly instead.

  **Where the deployment policy is that the provider's logs hold usage telemetry only, this is not a
  judgement call — it is off in production.** Same for AgentCore `APPLICATION_LOGS`, whose
  `request_payload` and `response_payload` are unredacted; `USAGE_LOGS` is the signal that profile
  wants. Both are genuinely useful in a development account against synthetic fixtures, which is where
  the full-fidelity configuration belongs. And because invocation logging is account-and-Region-wide
  with a single destination pair, one enable in a shared account defeats the policy for every workload
  in it — which is the argument for separate accounts per environment with an SCP denying
  `bedrock:PutModelInvocationLoggingConfiguration` in the production one (`Put` only, never `Delete`,
  so remediation stays possible if a configuration ever exists), so the configuration cannot be created
  rather than merely being absent. The vended-delivery half cannot be closed as cleanly: a blanket deny
  on `logs:PutDeliverySource` would also block the `USAGE_LOGS` delivery this profile wants, and no
  documented condition key distinguishes log type on that call — so scope those calls to a single
  deployment role, pin the permitted log types in its IaC, and add a scheduled assertion over
  `describe-deliveries`. That half is detective rather than preventive, and saying so is the point:
  it is a further argument for the gate in front of the sink. `audit-trail.md` carries the profile
  split and the per-rule verification; `examples/log_projection.py` carries the gate that decides what
  may be emitted at all.

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

**The mapping worth writing down.** Which built-in evaluators exist will change; what they *mean* for a
supervised compliance workflow does not, and two of them are simply the automated form of controls this
skill already insists on:

| Evaluator | What it automates |
|---|---|
| `Faithfulness`, `ContextRelevance` | **Citation grounding** — is every claim supported by retrieved material, and was the material retrieved actually relevant |
| `ToolSelectionAccuracy`, `ToolParameterAccuracy` | **Action grounding** — did the run call the tool it should have, with the arguments it should have |
| `Refusal`, `Stereotyping` | Complement the bias probes in `control-stack.md` |
| `GoalSuccessRate`, `Coherence`, `Conciseness` | Least useful here — see below |

That last row needs its reasoning stated, because the names sound like exactly what you want. A fluent,
coherent, concise, **wrong** disposition is this domain's characteristic failure mode, and all three of
those evaluators score it well. They are not harmful; they measure the dimension that was never at
risk, and a dashboard of them reads as reassurance.

**Evaluator `level` matters as much as evaluator choice.** Use `TOOL_CALL` level for action grounding
and `SESSION` level for the case-level record. This is not a detail to tune later: a multi-specialist
case (§10) is **one session containing several invocations**, so a `TRACE`-level evaluator cannot see
the thing you need judged — it sees one leg of the work and scores it in isolation, which is how a
correctly-configured evaluation ends up certifying a case nobody assessed as a whole.

Map this onto the monitoring in `control-stack.md`: on-demand runs against the golden set on every
prompt change; online evaluation as the continuous drift signal. Evaluation throughput is itself
quota-bound (tokens per minute, spans per evaluation, evaluators per configuration), so a
continuously-sampling online configuration over high alert volume needs a sampling rate chosen
against those limits rather than set to "everything". **Start at 5–10% of production traffic**: each
evaluation is itself a model invocation, billed and quota-consuming like any other, so a configuration
set to 100% roughly doubles your inference footprint in order to measure it. Raise the rate only if the
signal is too noisy at that sample to act on.

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
| Max session lifetime | 8 hrs default, adjustable (`maxLifetime`) | In-memory state does not survive it — use Memory, or session storage if filesystem state must persist |
| Session storage per session | 1 GB | Only if `filesystemConfigurations` is set; a PII surface once it is |
| Max payload | 100 MB | Ample for case data; not for bulk exports |
| Gateway invocation timeout | 15 min, adjustable | |
| Targets per gateway / tools per target | 100 / 1,000, both adjustable | Plenty for one platform API |
| Session creation rate | 25 TPS/account, adjustable | Shared across all endpoints — consider for batch fan-out (§6, §9) |
| Data plane request rate | 1,000 TPS/account, adjustable | Shared across *all* data plane APIs, not per-API |
| Memory resources per Region | 150, adjustable | Rules out naive per-tenant memory resources at scale |
| Strategies per memory resource | 6, **not** adjustable (900 per account, adjustable) | Caps distinct extraction behaviours on one memory — design the layout around it |
| Container image size | 2 GB, not adjustable | |
| Direct code deploy package | 250 MB compressed / 750 MB uncompressed, not adjustable | A tree that fits a container may not fit here |
| Session hardware | 2 vCPU / 8 GB, fixed | |

**A `/ping` trap worth knowing.** The optional `time_of_last_update` field must be set only on an
actual status change. Advancing it on every ping signals continuous change, the idle timeout never
fires, and sessions live until `maxLifetime` — quietly consuming the session quota and billing for
microVMs nobody is using. If you use the AgentCore SDK the ping response is handled for you; if you
hand-rolled `/ping`, check this. See the
[HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
and [handling long-running agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html).

**Note the caller's timeout too, and match the retry policy to the path.** API Gateway REST
integrations cap at **29 seconds** regardless of what AgentCore allows (`production-rules.md` §11).
That 29 seconds is API Gateway's *default* rather than an absolute ceiling: it can be raised through a
Service Quotas increase, but only for **Regional and private REST APIs**, and only if you also raise
the per-integration timeout and redeploy the stage. Three steps — skip either of the last two and the
quota increase is granted while the old behaviour stays in place, which is a confusing afternoon.

Retry configuration then follows from which path the call is on, because the two paths want opposite
behaviour:

| Path | Retry config | Why |
|---|---|---|
| Synchronous, behind a 29-second ceiling | `max_attempts=2, mode="standard"` | A retry that lands after the caller has already timed out is work you pay for and nobody reads. Fail fast and surface it. |
| Async jobs and batch fan-outs (§6, §9) | `max_attempts=5, mode="adaptive"` | Nothing is waiting, so retries are cheap — and adaptive's **client-side rate limiting** is the part you actually want here, because it throttles the whole client when it sees rejection instead of letting a hundred workers race into the same quota. |

Adaptive mode is the wrong default for interactive work for exactly the reason it is right for batch:
it deliberately slows the client down.

---

## Prompt caching

A compliance prompt is unusually well shaped for caching, and the reason is the governance discipline
this skill already asks for. The system prompt, the bounded-assertion rules, the tool definitions, the
few-shot examples and the pinned typology corpus are **identical across every alert in a batch**, and
they change only when somebody deliberately versions them. That stability is precisely the
precondition prompt caching needs, and most workloads do not have it — so this is a lever that pays off
better here than the general advice suggests.

Three effects, in order of how much they should change the design.

**Cache reads do not count toward the Bedrock token quota at all.** For a batch fan-out (§6, §9) that
is a *throughput increase*, not merely a discount — it is the one lever that raises effective tokens
per minute without a quota request, which matters because the alternative is a Service Quotas case
with a lead time attached. Treat it as relieving quota pressure rather than as a formula you can
compute: AWS's own documentation is inconsistent about whether cache reads enter the initial
reservation, so size the batch against observed throughput in your account instead of arithmetic from
a doc page.

**Cost moves sharply in both directions.** A cache write costs roughly **25% more** than standard
input; a cache read costs roughly **90% less**. You therefore need **two requests inside the TTL to
break even**, and caching a prefix that is used once an hour costs money instead of saving it. Match
the TTL to the work: the **5-minute default** suits a batch sweep where hundreds of alerts share a
prefix within minutes, while the **1-hour TTL** suits a system prompt and reference corpus that should
outlive a single run.

**It fails silently, and this skill's own habits are the likeliest cause.** Cache keys are exact,
byte-for-byte prefix matches. A request ID, a retrieval timestamp, a corpus version string, or a tool
schema re-serialised with a different key order — anything that varies per request and sits *before*
the cache point — produces a fresh cache write on every single request, with no error, no warning, and
a bill that looks exactly like caching was never switched on. The habits that cause it are ones this
file recommends: stamping an audit identifier into the prompt, recording the retrieval time,
version-pinning the corpus. All of that belongs **after** the cache point. Put the stable prefix first,
mark it, and let every per-request value and every audit-record field follow it.

Content below the model's minimum token threshold is ignored just as silently, and that threshold
**differs by model and moves between generations of the same model** — so a model-ID change is a
caching change, and the verification below has to be re-run after one.

**Verify rather than assume.** The first request should report `cacheWriteInputTokens > 0` and the
second `cacheReadInputTokens > 0` in the Converse `usage` object. Both zero means one of three things:
the content is below the threshold, the model does not support caching, or your prefix is fragmented by
a per-request value. Make that a standing post-deploy assertion alongside the others in this file,
because it is the only way this particular failure ever becomes visible.

Per-model thresholds, which models support the 1-hour TTL, and the `cachePoint` mechanics belong in the
global `amazon-bedrock` skill (`amazon-bedrock/references/prompt-caching.md`) rather than pinned here — they change
per model generation, and a stale number reproduces exactly the silent failure described above.

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

**Status is not evidence.** `READY` on a runtime, `READY` on a Gateway target and `ENFORCE` on a policy
engine all mean *loaded*. Each proves the resource was accepted. None of them proves it behaves
correctly. A Cedar policy referencing an action name that does not exist enforces nothing; a Gateway
target pointing at the wrong function ARN resolves and lists tools; an IAM policy missing the
inference-profile ARN (`production-rules.md` §1) shows no sign of trouble until the first model
call. Build post-deploy checks that **exercise the real path in both directions**: a call that
should succeed and does, and a call that should be refused and is. The negative case is the one
that carries the information, and it is the one people skip.

**Read Gateway target status precisely, because the state names are not the ones people assume.**
`GetGatewayTarget` reports `CREATING`, `UPDATING`, `UPDATE_UNSUCCESSFUL`, `DELETING`, `READY`,
`FAILED`, `SYNCHRONIZING`, `SYNCHRONIZE_UNSUCCESSFUL`, `CREATE_PENDING_AUTH`, `UPDATE_PENDING_AUTH` or
`SYNCHRONIZE_PENDING_AUTH`. There is **no `ACTIVE` state** — a wait loop or runbook written for one
waits forever and then reports a timeout that has nothing to do with the target. Two of these read like
failures and are not: `CREATE_PENDING_AUTH` and `UPDATE_PENDING_AUTH` mean the target is **waiting on a
user to complete OAuth federation**, which is the expected resting state of a user-delegated target
nobody has authorised yet — treat it as a prompt to the analyst, not an incident. The genuine error
states are `FAILED`, `UPDATE_UNSUCCESSFUL` and `SYNCHRONIZE_UNSUCCESSFUL`, and the field that tells you
*why* is **`statusReasons`**. Read it before theorising; it usually names the problem outright, and a
wait loop that logs only the status throws that away. A target still in `CREATING` past about ten
minutes is a support case rather than a configuration bug you can fix by editing the target.

**Assert readiness on the endpoint, not only the runtime.** The runtime and its endpoint report status
independently, and the runtime reaching `READY` first is the normal ordering — so a check that stops
there declares success before the agent is invocable. Poll `get-agent-runtime-endpoint` as well and
treat the pair as the readiness condition.

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
      │              constructs the InvokeHarness request; strips every override field
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
write, cannot choose its own session, cannot choose its own tools, model, prompt or actor, and
cannot dispose of anything. Every one of those is enforced by infrastructure or by the calling
backend rather than by prompt.

And every one of them should be *demonstrated* before the deployment is called done — a denied
write attempt, a cross-tenant tool call that fails, a session ID from a request body that does not
route, an `InvokeHarness` request carrying a `skills` override that the backend drops on the floor.
A control you have only configured is a control you are hoping for.
