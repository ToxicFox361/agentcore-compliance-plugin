# Production rules for AgentCore agents

Empirical rules from building and debugging a multi-tenant AgentCore platform end to end.
Every entry here corresponds to a defect that shipped, a failure that cost real debugging
time, or a constraint that only appears under load or on a fresh account. They are ordered
by how much time they cost to discover.

Read this before writing or reviewing AgentCore infrastructure. Most of these fail silently
or with an error that points somewhere other than the cause.

**Convention — documented vs observed.** Quoted blocks attributed to AWS are documented API
surface: you can build an assertion on them. Strings and values recorded from live accounts are
marked **observed** and are not contract — they get reworded, and an assertion pinned to the
exact text will pass until the day it silently stops matching. The distinction is not cosmetic:
an observed error string tells you what a symptom looked like once, a documented one tells you
what the platform promises. Both appear below; only one is safe to depend on.

If you arrive with a symptom rather than a topic, start from the **Diagnostic quick reference** at
the end and follow the section it names. If you arrive with a topic:

| Theme | Rules |
|---|---|
| IAM and account setup | §1 inference-profile ARNs · §2 service-linked roles · §6 log permissions · §10 zero quota on new accounts |
| Runtime lifecycle and deployment | §3 module-level agent object · §4 persist before verify · §5 placeholder substitution · §12 CDK region · §14 concurrent CDK |
| Requests, quotas and timeouts | §7 session IDs · §8 max output tokens and quota burndown · §11 fail inside the caller's window · §37 retry policy on a blocking call · §38 the per-day token quota · §39 `maxTokens` bounds thinking too |
| Models and prompts | §15 streaming tool use · §16 vendor-coupled prompts · §17 capability tiers · §24 inference parameters and generation migration · §26 measured prompt/model matrix |
| Cost | §9 regional pricing · §13 surfacing computed cost |
| Tools, writes and authorization | §18 narrated actions · §19 tool namespacing · §20 placeholder tool errors · §21 prompts are not access control · §22 duplicate write paths · §23 policies that never match |
| Multi-agent workflows | §25 partial evidence sets in a synthesis |
| Evaluation | §24 no seed · §26 prompt structure, model choice and harness design · §39 a cap tuned to one arm · §41 suffix-keyed fixtures · §42 counting rows · §43 a shared failure is a prompt defect |
| Queues and event sources | §37 a blocking call under a retry policy · §40 in-flight messages cannot be flushed |

---

## 1. Inference-profile IAM needs BOTH resource forms

**Symptom:** `AccessDeniedException` on the first model call, with an execution role that
visibly grants `bedrock:InvokeModel`.

Many current model IDs are inference profiles rather than bare foundation models — anything
carrying a geographic prefix. AWS documents `us.`, `eu.`, `apac.`, `global.`, `us-gov.`, `au.`,
`jp.`, `ca.` and adds more over time, so **match the shape, not a list**: a short lowercase
segment before the vendor name. Enumerating four prefixes in a helper understates which model
IDs need the dual-ARN grant below, and the ones it misses fail at the first invoke in a new
geography. Invoking a profile requires permission on *two different resource shapes*, and
granting only the familiar one fails:

```python
resources=[
    # Account-scoped. The profile itself.
    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
    # NOT account-scoped — note the empty account segment "::".
    # Including an account ID here causes authorization failure.
    "arn:aws:bedrock:*::foundation-model/*",
]
```

The region is wildcarded on the foundation-model ARN deliberately: a geographic profile
dispatches to any destination region in its geography, not just the deployment region.

Grant both `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` — the Converse
API streams internally even when you did not ask for streaming.

**Do not invert this into "profiles only".** AWS says only that "Specific foundational models
require cross-Region inference profiles in specific Regions" and that "Not all models support
cross-Region inference"; base foundation-model IDs remain documented for `InvokeModel`. Whether
a given model can be invoked with a bare foundation-model ID is **per-model and per-Region** —
check "Models at a glance" and "Supported Regions and models for inference profiles" for the
model you are actually deploying, and assume neither direction. Granting both resource shapes
is correct regardless, which is the point: the grant costs nothing and removes the question.

**Why it hides:** a policy scoped to `arn:aws:bedrock:{region}::foundation-model/*` looks
complete and passes review. It works for bare model IDs and fails for every profile ID.

---

## 2. AgentCore uses several service-linked roles, under different principals

**Symptom:** on a new account, `CreateAgentRuntime` fails with
*"Failed creating service linked role. Please verify that the calling role has sufficient
permissions to create a service linked role."*

There is not one AgentCore SLR. There are several, each under its own service principal:

| Purpose | Service principal |
|---|---|
| Gateway | `bedrock-agentcore.amazonaws.com` |
| Runtime identity — **needed by CreateAgentRuntime** | `runtime-identity.bedrock-agentcore.amazonaws.com` |
| Network | `network.bedrock-agentcore.amazonaws.com` |
| Identity network | `identity-network.bedrock-agentcore.amazonaws.com` |
| Runtime instances (`AWSServiceRoleForBedrockAgentCoreRuntimeInstances`) — cleans up the compute a capacity provider creates for the Instances compute type | `runtime-instances.bedrock-agentcore.amazonaws.com` |

A grant scoped to the bare principal only covers Gateway. Both the resource path and the
`iam:AWSServiceName` condition must allow the subdomain forms:

```python
resources=[
    "arn:aws:iam::*:role/aws-service-role/bedrock-agentcore.amazonaws.com/*",
    "arn:aws:iam::*:role/aws-service-role/*.bedrock-agentcore.amazonaws.com/*",
],
conditions={"StringLike": {"iam:AWSServiceName": [
    "bedrock-agentcore.amazonaws.com",
    "*.bedrock-agentcore.amazonaws.com",
]}},
```

The wildcard pair above is why that fifth row costs nothing:
`*.bedrock-agentcore.amazonaws.com` already covers `runtime-instances.` in both the resource
path and the `iam:AWSServiceName` condition. A grant written as an explicit list of four
principals would have needed editing; the shape-matched grant did not. Prefer the shape.

Note also that these roles are **created automatically for agents created on or after
13 October 2025**. Agents older than that keep their manually attached policies, so a fleet
spanning that date has two IAM shapes in it and a change that assumes either one will miss half
of them.

**Why it hides:** established accounts already have the roles, so this only ever fails on a
brand-new account — typically the first customer deployment or a fresh dev environment.

Fastest unblock when it bites: create them once with
`aws iam create-service-linked-role --aws-service-name <principal>`.

---

## 3. Never build the agent object at module scope

**Symptom:** the first request fails for some unrelated reason, and then *every* subsequent
request fails instantly with `ConcurrencyException: Agent is already processing a request`
(observed error text, not documented — this is raised by the agent framework inside your
container, not by an AgentCore API, so it appears in no AWS error reference and searching for it
there wastes the first twenty minutes). The runtime is wedged permanently and only a redeploy
clears it.

Agent objects in common frameworks are stateful and hold a per-instance lock. One module-level
instance shared across invocations means a single failed request can leave that lock held for
the life of the container.

```python
# WRONG — one failed request wedges the runtime forever
agent = Agent(model=MODEL_ID, system_prompt=SYSTEM_PROMPT)

@app.entrypoint
def invoke(payload):
    return agent(payload["prompt"])
```

```python
# RIGHT — a failure disposes with the request
def build_agent():
    return Agent(model=MODEL_ID, system_prompt=SYSTEM_PROMPT)

@app.entrypoint
def invoke(payload):
    return build_agent()(payload["prompt"])
```

Construction cost is negligible next to model latency. The wedge is not.

---

## 4. Persist state before running post-deploy verification

**Symptom:** a runtime exists and reports `READY`, but the control plane has no record of it.
It is invisible to the application, un-deletable through the product's own UI, and still
billing.

A deployment orchestrator that does *create → wait ready → smoke test → persist record* will
orphan the runtime whenever the smoke test is slow to fail. Each failing invoke can burn a
full client read timeout; three retries plus build time can exceed a Lambda's 15-minute
ceiling, and the function is killed before it writes anything.

Persist the record as soon as the resource is confirmed `READY`. Run verification afterwards,
wrapped so it cannot throw:

```python
# ... runtime confirmed READY ...
persist_agent_record(...)          # durable first

try:
    test_result = smoke_test(...)  # diagnostic only, never gates persistence
except Exception as e:
    logger.warning(f"smoke test errored: {e}")
    test_result = False
```

General rule: **make the deliverable durable before doing anything slow and failure-prone.**

---

## 5. Substitute every placeholder your templates declare

**Symptom:** the container dies during import; invocation returns an opaque
`RuntimeClientError: Runtime initialization time exceeded` (observed message text, not
documented), which reads like a cold-start or capacity problem and is neither.

The documented half is the half that tells you where to look. `RuntimeClientError` is HTTP
**424**, and AWS defines it as *your agent's container returned a 4xx or 5xx error*; the
exception name also comes back in the `x-amzn-ErrorType` header. That is the platform stating
that the fault is inside the container. So the message about initialization time is not a
capacity signal at all — the container answered, badly — and an **import-time crash is the first
hypothesis**, not a scaling investigation.

Platforms that generate agent code from templates must keep the placeholder set and the
substitution set in lockstep. A template that declares `REGION = 'REGION_VALUE'` against a
substitution map with no `REGION_VALUE` entry ships the literal string to production, where
the SDK rejects it at client construction — before any handler runs.

This bites hardest with **user-supplied templates** (fetched from a repository at deploy time),
where the template author and the substitution map are maintained by different people.

Defences:
- Assert no `*_VALUE` placeholders remain in generated code before packaging.
- Fail the build loudly rather than deploying code that cannot import.
- Treat "initialization time exceeded" as *import-time crash* until proven otherwise.

---

## 6. Grant the runtime permission to write its own logs

**Symptom:** an agent fails and there is no log group at all — not an empty one, none.

Without CloudWatch Logs permissions on the execution role, the runtime cannot emit anything,
so every failure surfaces as an opaque 500 with nothing to inspect. This turns a five-minute
diagnosis into an hour of guessing.

```python
# Statement 1 — log delivery. Note three resource prefixes, not one.
actions=["logs:CreateLogGroup", "logs:CreateLogStream",
         "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
resources=[
    f"arn:aws:logs:{region}:*:log-group:/aws/bedrock-agentcore/*",
    f"arn:aws:logs:{region}:*:log-group:/aws/vendedlogs/bedrock-agentcore/*",
    f"arn:aws:logs:{region}:*:log-group:aws/spans*",
],

# Statement 2 — AgentCore calls this before it can deliver spans into the agent's
# own log group. Scope the resource as narrowly as the current devguide allows.
actions=["logs:PutResourcePolicy"],
```

**Log group and log stream are not the same string, and confusing them costs a day.** The
**group** is `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`. The streams live
inside it:

| Stream | Contents |
|---|---|
| `[runtime-logs] <UUID>` | stdout/stderr from the container |
| `runtime-logs` | OTEL structured logs |
| `spans` | trace spans |

With the unified span destination switched off, spans go somewhere else entirely — log group
`aws/spans`, stream `default`.

Two consequences, and both present as an absence of data rather than as a mistake:

- **A Logs Insights query names its log groups literally.** A name that is close but wrong — a
  stream name passed as a group, a missing endpoint suffix, the runtime ID where the agent ID
  belongs — returns an **empty result set, not an error**. Empty reads as "the agent emitted
  nothing", which is indistinguishable from a missing IAM permission and from a container that
  crashed before logging. Confirm the group exists with `logs describe-log-groups` before drawing
  any conclusion from an empty query.
- **`/aws/bedrock-agentcore/*` alone is not enough.** It matches neither `aws/spans` nor
  `/aws/vendedlogs/bedrock-agentcore/*`, and the common policy omits `logs:PutResourcePolicy`
  altogether. The result is runtime logs arriving normally and traces silently not arriving —
  the hardest gap to notice, because nothing is broken and the thing you lost is the thing you
  only reach for during an incident (and §25 depends on it for case-level correlation).

Also grant read access to any table the agent reads at request time. A config lookup wrapped
in `try/except` will swallow the denial and degrade silently — the agent works, but never sees
its own runtime configuration.

---

## 7. Derive session IDs server-side, namespaced by tenant

**Symptom (correctness):** every invocation cold-starts, because omitting a session ID
provisions a fresh microVM each time.

**Symptom (security):** none — which is the problem.

A session is the isolation boundary: each gets its own microVM, filesystem and memory. AWS
validates the *format* of a session ID but does not verify it belongs to the caller. From the
AgentCore security guidance:

> In deployments where a single IAM principal invokes on behalf of multiple end users, the
> platform does not enforce that a `sessionId` belongs to the calling user.

> Treat the `runtimeSessionId` as a server-side value derived from the authenticated end user
> — never accept it directly from untrusted client input.

Source: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html

A shared backend execution role plus client-chosen session IDs means an authenticated user can
supply another tenant's session ID and route a request into their microVM. Accepting a
`sessionId` field from a request body is exactly this anti-pattern.

```python
def derive_runtime_session_id(tenant_id, agent_arn, client_hint):
    """Client hint allows conversation continuity; the tenant namespace is
    server-resolved, so a forged hint can only collide within its own tenant."""
    return hashlib.sha256(f"{tenant_id}|{agent_arn}|{client_hint}".encode()).hexdigest()
```

Constraints: minimum **33** and maximum **256** characters. A SHA-256 hex digest is 64, inside
both bounds — which is one more reason to hash rather than concatenate, since a scheme that
appends tenant, agent ARN and a client hint verbatim can run past 256 on a long ARN and fails
only for the tenants with the longest names.

Resolve `tenant_id` from the resource record, never from the request body. Where the security
bar is high, use a distinct IAM principal per tenant so IAM itself enforces the scoping.

**The provisioning race.** A session's microVM takes time to come up, and a second operation
against a session that is still provisioning returns HTTP **409 `RetryableConflictException`**
("Session operation in progress, please retry"). AWS SDKs absorb this automatically when default
retries are enabled — which is exactly the budget §11 tells you to cap at `max_attempts: 2` to
fit a synchronous timeout. The two rules interact: a capped retry budget spent on a throttle has
nothing left for this race, so two near-simultaneous requests on a freshly derived session ID can
surface a 409 to the caller instead of absorbing it. Serialise the first request per session, or
handle 409 explicitly rather than relying on a budget you have already spent elsewhere.

---

## 8. Always set max output tokens explicitly

Quota is reserved at request start, before a single output token exists:

```
reserved = InputTokenCount + CacheReadInputTokens + CacheWriteInputTokens + max_tokens
settled  = InputTokenCount + CacheWriteInputTokens + (OutputTokenCount x burndown rate)
```

Unused reservation is released as output generates, so the reservation is a peak and the
settlement is the bill. Left unset, `max_tokens` defaults to the model's maximum — tens of
thousands of tokens — so every request reserves far more than it uses and you throttle at a small
fraction of apparent capacity.

Frameworks generally do not set it for you. Set it on the model object, sized to the expected
response.

**Output tokens do not cost one quota token each.** They burn down at a model-specific
multiplier. Documented examples: **15x** for Claude 4.8, **10x** for Sonnet 5 and Opus 5, **5x**
for Anthropic generations up to 4.7, **1x** for other models. A SAR-narrative step generating
2,000 output tokens on a Claude model therefore settles at 10,000+ quota tokens. A capacity model
built on "input plus output" understates real consumption several-fold — and understates it worst
on exactly the model family chosen for the highest-stakes generative output, so the workload most
likely to throttle is the one the plan says has headroom.

Corollary for diagnosis: before assuming quota exhaustion, read the `AWS/Bedrock` CloudWatch
namespace. True input consumption is `InputTokenCount + CacheWriteInputTokens`. Heavy
`InvocationThrottles` against near-zero token consumption means the limit is structural rather
than usage-driven (§10). And note what the convenient metric omits: **`EstimatedTPMQuotaUsage`
reflects burndown but not the up-front `max_tokens` reservation**, so it cannot on its own explain
a throttle. A request reserving 30,000 tokens and using 300 looks small there and still exhausts
the minute — which is why "the estimated usage metric says we are nowhere near the limit" is not
a refutation of a throttle, it is a symptom of this rule.

One caveat to hold explicitly: AWS's own documentation disagrees on whether
`CacheReadInputTokens` enters the reservation — the model-invocation guidance includes it, the
quota-health guidance states that cache reads never count toward TPM. Do not pin an exact formula
on that term or write a capacity plan that depends on it. The direction is safe to act on: cache
reads relieve quota pressure rather than adding to it.

The exact mechanics and the per-family burndown rates change with each model generation and are
maintained in the global `amazon-bedrock` skill (`amazon-bedrock/references/quota-health.md`,
`amazon-bedrock/references/model-invocation.md`). Read them there when sizing capacity rather than reasoning
forward from the formula pinned here — a rate table in a static file is a rate table that will be
wrong at the next launch.

---

## 9. Model pricing is regional — never use published figures

Per-token rates differ by region. Aggregator sites and blog posts almost always quote
`us-east-1`. Hardcoding those into cost tracking produces numbers that are silently wrong.

In one measured case every model checked differed from the widely published figure, and one
model's output/input ratio differed by 2× between regions — so even relative reasoning
transferred badly.

Query the Price List API for the deployment region. The service code is **`AmazonBedrockService`**,
not `AmazonBedrock`:

```bash
aws pricing get-products --service-code AmazonBedrockService --region us-east-1 \
  --filters Type=TERM_MATCH,Field=regionCode,Value=<deployment-region> \
            Type=TERM_MATCH,Field=model,Value="<Model Name>"
```

The wrong service code is the trap, because the API returns an **empty result set rather than an
error** — and a cost pipeline reads no rates as no cost. That failure has the §20 shape: a
plausible zero travels downstream as data. `examples/cost_tracking.py` carries the working call
and the service-code taxonomy; read it there rather than re-deriving it here, so there is one
place to be right.

Attributes worth filtering or recording: `service_tier`, `inferenceType`, `tokenType`,
`regionCode`, `model`, `batch`. Filter out `batch=Yes` and the `flex`/`priority` tiers unless you
mean them. Get exact model names from
`aws pricing get-attribute-values --service-code AmazonBedrockService --attribute-name model`.

Record the model ID on every usage event. Without it, per-model rates cannot be applied and a
mixed-model tenant is billed at whatever single rate was hardcoded. Strip the geographic prefix
before rate lookup so a prefix change does not silently zero the cost.

Pricing mechanics — tier semantics, batch discounts, how cache reads and writes are rated, how a
rate resolves to a line item — are maintained in the global `amazon-bedrock` skill
(`amazon-bedrock/references/cost-tracking.md`).

---

## 10. Brand-new AWS accounts have zero Bedrock quota

What AWS documents is narrow: **"new AWS accounts have reduced quotas"**, and some model quotas
are non-adjustable. Everything sharper than that below is **observed**, not documented — treat it
as a pattern to recognise, not a contract to assert on.

Observed: a freshly created account reported `0.0` for every Bedrock quota it listed — tokens per
day, tokens per minute, requests per minute — while model access showed `AUTHORIZED` and
`entitlementAvailability: AVAILABLE`. Invocations failed with the observed error text
`ThrottlingException: Too many tokens per day`, which misleadingly implies consumption.

Diagnostic sequence:
1. `aws service-quotas list-service-quotas --service-code bedrock`, and read the **token** quotas
   first — TPM and TPD. `0` on either is structural: you cannot over-consume into a budget of
   zero. Where an RPM quota is listed for the model and reads `0`, that is the same conclusion
   arrived at by a second route.

   Do not run the test the other way round. **Not every model has an RPM quota** — Claude Opus
   4.7 and 4.8 are governed by token quotas alone — and an *absent* RPM quota is not an RPM quota
   of zero. A diagnostic that keys on "if RPM is also 0, it is structural" returns "not
   structural" on exactly the models where RPM cannot be read at all, and sends you looking for
   usage that does not exist.
2. Compare against an established account in the same org. Quotas are **per-account** and are
   never inherited from an organization's management account.
3. Check CloudWatch token counts. Near-zero consumption plus many throttles confirms gating —
   with §8's caveat that `EstimatedTPMQuotaUsage` alone cannot settle it.

The blocking quotas are frequently marked `Adjustable: False`, so the API path cannot raise
them. They populate on account verification.

**A second new-account blocker that is not quota at all.** Anthropic models are enabled by
default but require a **one-time use-case form submitted before first use** — through the Bedrock
console playground, or via the `PutUseCaseForModelAccess` API. Until it is submitted the model
cannot be invoked, and the failure lands on access rather than on quota, so from the error alone
it looks like the same new-account problem and gets the same wrong fix. For AWS Organizations,
submitting **via the API at the management account level extends approval to child accounts** —
which is directly worth doing up front on a per-environment-account topology, rather than
discovering it once per account by hand at the moment someone is waiting.

**Model availability is necessary, not sufficient.** `get-foundation-model-availability` returns
`agreementAvailability`, `authorizationStatus` (`AUTHORIZED` | `NOT_AUTHORIZED`),
`entitlementAvailability` (`AVAILABLE` | `NOT_AVAILABLE`) and `regionAvailability` (`AVAILABLE` |
`NOT_AVAILABLE`). AWS documents the field names and the enum values **and nothing else** — no page
defines their operational semantics, and none states that they predict whether an invoke will
succeed. Observed: all four can read AVAILABLE/AUTHORIZED while `InvokeModel` returns
`AccessDeniedException`. AWS's own troubleshooting for that error points at Region availability
and an "Access status: Granted" in the console, not at this API. So use the call as a cheap
negative filter — a `NOT_AVAILABLE` is worth believing — and **prove access with a real one-token
invoke**. A preflight check that reports green while the workload cannot run is worse than no
preflight check, because it redirects the investigation.

**Practical consequence:** budget for this in delivery plans. If a demo or pilot is time-boxed,
deploy into an account with established quota rather than a fresh one. A Service Quotas
*template* associated with the organization will apply quotas to newly created accounts
automatically — worth configuring before you need it.

---

## 11. Fail fast enough to fit the caller's timeout

API Gateway REST integrations time out at **29 seconds by default** — configurable from 50 ms
upward. Default SDK retry policies against a throttled model can spend well over two minutes
before returning. The gateway gives up first and returns a 504 — and because that response
carries no CORS headers, a browser reports it as a generic network error. Three layers of
indirection between the user and the actual cause.

The 29-second ceiling is not immovable, but raising it is a trade rather than a fix: the Service
Quotas increase applies to **Regional and private** REST APIs only, is usually granted at the cost
of a reduced account-level throttle quota, and does nothing at all until you *also* raise the
per-integration timeout and redeploy the stage. Edge-optimized APIs stay capped. So **treat 29
seconds as the number you have until someone has explicitly raised it and shown you the
redeployed integration**, and prefer an asynchronous API (accept, return a handle, poll) over
buying seconds — stretching a synchronous request toward a limit you do not control is the losing
side of the trade even when the increase is granted.

**Retry policy belongs to the path, not to the application.** There are two correct
configurations and they are different:

```python
# Synchronous path behind a hard ceiling: surface the failure inside the caller's window.
SYNC_CONFIG = BotocoreConfig(
    retries={"max_attempts": 2, "mode": "standard"},
    connect_timeout=5,
    read_timeout=15,   # per-chunk when streaming, not per-request
)

# Async and batch fan-outs, no caller waiting: absorb sustained throttling instead of
# surfacing it. AWS's general recommendation; adaptive adds client-side rate limiting.
BATCH_CONFIG = BotocoreConfig(retries={"max_attempts": 5, "mode": "adaptive"})
```

Configuring one policy for both paths is the defect, and it fails in whichever direction you
chose. Adaptive/5 behind a 29-second gateway makes the interactive user wait past the timeout for
an answer that can no longer be delivered — the work completes and lands nowhere. Standard/2 on a
batch fan-out abandons a retryable throttle it should have ridden out, and the failure arrives as
a hole in an evidence set (§25) rather than as a slow response, which is the more expensive of the
two outcomes in regulated work.

Handle model errors as data rather than letting them escape as unhandled 500s — an operator
needs to see `ThrottlingException`, not `Internal Server Error`.

---

## 12. `AWS_DEFAULT_REGION` controls CDK deployments, not `CDK_DEFAULT_REGION`

The CDK CLI overwrites `CDK_DEFAULT_REGION` from the AWS SDK credential chain before invoking
the app. Setting it alone silently deploys to the wrong region.

```bash
# Wrong — clobbered; resources land in the SDK-resolved region
CDK_DEFAULT_REGION=eu-central-1 cdk deploy

# Right
AWS_PROFILE=<profile> AWS_DEFAULT_REGION=eu-central-1 cdk deploy
```

Verify before deploying: synthesize and grep the output for a region-bearing resource name.
Region errors are expensive to unwind once stateful resources exist.

---

## 13. Surface computed costs all the way to the consumer

A cost pipeline can compute correct figures and still display nothing — or worse, display
plausible wrong ones.

Two failure modes seen together:
- A `ProjectionExpression` on the read API omitted the cost attributes, so they never reached
  the client.
- The client had a fallback that recomputed cost from *hardcoded rates for a different model*,
  which silently activated whenever the field was absent.

Combined, the dashboard reported a confident, wrong number with no error anywhere.

Rules: the aggregation layer is the single source of truth for cost, because only it knows
which model produced each record. Never keep a client-side rate fallback — if the field is
missing, show it as missing. Assert end to end from raw usage record through to rendered
value, not just that the computation was correct.

---

## 14. Concurrent CDK operations collide

Two CDK invocations from the same working directory fail with
*"Other CLIs are currently reading from cdk.out."* (observed CLI text, not documented — it changes
with CDK versions, so match on failure rather than on the string). Use a separate synth directory
per concurrent operation:

```bash
cdk deploy  --app "python3 src/app.py" --output /tmp/cdk.out.envA
cdk destroy --app "python3 src/app.py" --output /tmp/cdk.out.envB --force
```

Guard destructive commands with an explicit account assertion. A wrong profile on a destroy is
unrecoverable:

```bash
ACC=$(aws sts get-caller-identity --query Account --output text)
[ "$ACC" != "$EXPECTED" ] && { echo "ABORT: wrong account $ACC"; exit 1; }
```

---

## 15. Streaming tool use is a separate capability from tool use

**Symptom:** `ValidationException: This model doesn't support tool use in streaming mode.`
(observed error text, not documented.)

A model can pass a `converse` tool-call test and still fail `converse_stream` with tools — these
are two different capability surfaces. **Bedrock does publish both**: *Tool use* and *Streaming
tool use* are two separately named features with per-model support listed in "Supported models and
model features" and "Models at a glance". Read that table first. It is the cheapest check
available and it is authoritative for what AWS claims.

Then verify empirically **against the exact API your application calls**, because the table tells
you what is claimed for a feature, not what your request shape does with your model in your
Region — and a model listed as supporting tool use is not thereby listed as supporting it under
`ConverseStream`. Note also that `responseStreamingSupported` from `GetFoundationModel` answers
plain streaming only; it says nothing about streaming *tool use*, so a preflight built on it will
pass on a model that cannot do the thing you need.

Observed on one platform, one Region — not a capability statement:
`eu.mistral.pixtral-large-2502-v1:0` did tool use correctly via Converse and failed outright under
ConverseStream. Amazon Nova models (`nova-pro`, `nova-2-lite`, `nova-lite`) handled both.

Most agent frameworks stream by default, so a non-streaming smoke test proves the wrong thing
and passes anyway:

```python
import boto3

client = boto3.client("bedrock-runtime", region_name=region)

response = client.converse_stream(
    modelId=model_id,
    messages=[{"role": "user", "content": [{"text": "What's the weather in Paris?"}]}],
    toolConfig={
        "tools": [{
            "toolSpec": {
                "name": "get_weather",
                "description": "Get current weather for a location",
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                }},
            }
        }]
    },
)

saw_tool_use = False
stop_reason = None
for event in response["stream"]:
    if "contentBlockStart" in event and "toolUse" in event["contentBlockStart"].get("start", {}):
        saw_tool_use = True
    if "messageStop" in event:
        stop_reason = event["messageStop"]["stopReason"]

assert saw_tool_use and stop_reason == "tool_use", (
    f"streaming tool use failed: saw_tool_use={saw_tool_use}, stop_reason={stop_reason}"
)
```

**Why it hides:** the non-streaming test is the convenient one to write, passes, and proves the
wrong thing.

---

## 16. Prompts are vendor-coupled

**Symptom:** `modelStreamErrorException: Model produced invalid sequence as part of ToolUse`
(observed error text, not documented.)

A prompt authored for one vendor's model can produce malformed tool-call sequences on another,
even with identical tools and identical task. Observed on one platform, one Region — a per-model
result, not a capability claim: a Claude-authored claims-processing prompt caused
`eu.amazon.nova-pro-v1:0` to emit `<thinking>` narration followed by an invalid tool-use block. Same prompt, same tools, different vendor — the failure is in the prompt's
assumptions about how the model expresses reasoning around a tool call, not in the tools
themselves.

Treat system prompts as vendor-coupled artefacts, not portable configuration. Swapping model
families is a prompt change, not a config change — budget for re-testing every tool-calling
path, not just the happy path, whenever the model ID changes.

---

## 17. Capability tiers do not transfer across vendors

**Symptom:** a model loops on one schema field then emits an invalid tool sequence.

A "use a cheap fast model for the classification step" design does not survive a vendor swap.
Observed on one platform, one Region — per-model results, not capability claims: a dual-agent
sample used a capable model for drafting and a cheap model for
validation. Substituting the cheap tier with `eu.amazon.nova-lite-v1:0` caused it to loop four
times on a single required field (`concerns: str`, whose documented empty value was the literal
string `"None"`), then fail outright. `eu.amazon.nova-2-lite-v1:0` handled the identical schema
first time — capability tiers are not fungible across vendors even when the marketing names
suggest an equivalent price/performance point.

Schema smell to watch for: a **required** string field whose empty case is a sentinel word
(`"None"`, `"N/A"`) forces the model to emit text where `null` is the natural encoding. Prefer
an optional field with a default:

```python
# Forces every model to synthesize a string, including the empty case
concerns: str  # empty case documented as the literal "None"

# Lets the model express "nothing to report" the way tool-calling models do it
concerns: Optional[str] = None
```

A tool schema only one vendor's models satisfy is vendor lock-in hidden in a function
signature.

---

## 18. An agent can report an action it never took

**Symptom:** the agent states an action succeeded; the downstream record does not exist.

Verified: an agent reported "your claim has been successfully recorded," a second reviewing
agent scored it 95/100 and approved, and the target DynamoDB table was empty. The tool's Lambda
**had no CloudWatch log group at all** — it had never been invoked. A sibling tool in the same
Gateway showed normal invocations, so tool-calling worked in general; the model narrated this
particular call instead of making it.

**Diagnostic:** absence of a CloudWatch log group proves a Lambda was never invoked — a fast,
unambiguous check. Reach for it before trusting any agent narrative of a write action.

**Rule (the important one for regulated work): never treat an agent's narrative as evidence
that an action occurred. Verify against the system of record.** An LLM optimizes for a
plausible-sounding response, not for having actually called the tool — a fabricated success
message and a genuine one are indistinguishable from the text alone.

**Existence is not attribution.** Finding the record is the second check, not the last one. A
record can appear from another actor — a concurrent session, a replayed event, or the model calling
the tool off-script (§21) — so a record alone does not tie the action to the run that claims it.
Also count the target Lambda's invocations inside the run's time window:

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda --metric-name Invocations --statistics Sum --period 60 \
  --dimensions Name=FunctionName,Value=<tool-function> \
  --start-time <run-start> --end-time <run-end>
```

Zero in-window invocations plus a fresh record means something other than this run wrote it. Two
invocations where one was expected means two writers (§22). Lambda publishes `Invocations` to
CloudWatch at one-minute resolution with no extra instrumentation —
[Using CloudWatch metrics with Lambda](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics.html).

What caught it here: a deterministic post-processing step read the record ID from the tool
result, found none, and emitted `unknown` rather than fabricating one. That is the
model-proposes/code-decides split earning its keep — the code downstream trusts the tool result,
not the model's prose about the tool result. But `unknown` is itself the wrong shape: it turned a
hard failure into a value that reads like data one layer down. Raise instead — see §20. And see
`control-stack.md` Layer 5 (citation grounding: every asserted action must trace to a tool result and
to evidence that the tool ran, not just every factual claim).

---

## 19. A Gateway namespaces every tool name

**Symptom:** a deterministic code path gets JSON-RPC `-32602 Unknown tool: create_claim` and the
call never reaches the Lambda — while the *same run's* model-driven phase calls tools successfully.
One target Lambda shows invocations; another has no CloudWatch log group at all.

An AgentCore Gateway does not expose a target's tools under their own names. It composes the target
name and the tool name:

```
${target_name}___${tool_name}      # three underscores
```

A target `create-claim` exposing a tool `create_claim` is callable only as
`create-claim___create_claim`. This is documented behaviour, not an accident of one deployment —
see [Understand how AgentCore Gateway tools are
named](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html).

Hardcoding either form is wrong: the bare name fails today, and the composite name breaks the day
someone renames a target. Resolve names from `tools/list` at runtime and map by suffix:

```python
def resolve_tool_names(session) -> dict[str, str]:
    """Map bare tool name -> the gateway-namespaced name to call."""
    resolved = {}
    for tool in session.list_tools().tools:          # MCP tools/list
        bare = tool.name.rpartition("___")[2]        # whole name if unprefixed
        assert "___" not in bare, f"separator inside tool half, ambiguous: {tool.name}"
        resolved[bare] = tool.name
    return resolved

names = resolve_tool_names(session)
session.call_tool(names["create_claim"], arguments={...})
```

Fail loudly on a missing key rather than falling back to the bare name — a `KeyError` at startup is
cheaper than a write path that silently never fires.

The assertion earns its line because `rpartition` takes the **last** separator. That is the right
choice when a *target* name contains `___` and the wrong one when a *tool* name does: the map then
gets a truncated key, the lookup for the real bare name raises `KeyError`, and the message points
at the caller rather than at the naming collision. One assert converts a confusing lookup failure
into a startup error that names the offending tool.

**Trust `tools/list`, not the docs, for the separator count.** The devguide, the Policy getting
started page and AWS blog posts all show **three** underscores; the CDK construct-library README
and its alpha policy examples show **two**. At most one of those can be right, and a Cedar action ID
built from the wrong one produces §23's silent failure rather than §19's loud one. Print the names
verbatim from `tools/list` against the deployed Gateway and treat that output as the only
authority — including when writing policy action IDs.

**An allow-list of bare names is not the only way to lose to the prefix.** Strands compiles a tool
filter and applies it with `pattern.match(tool.mcp_tool.name)` — **anchored at position 0**, not
searched. A pattern written for `search()` therefore means something entirely different once it is
handed to `match()`:

```python
# WRONG - a `search` pattern applied with `match`. Against `tmtools___get_alert`:
# the `^` branch matches the empty string at position 0 and then demands
# `get_alert$` immediately; the `___` branch demands the NAME start with `___`.
# Neither holds, so the agent receives an EMPTY tool catalogue.
re.compile(rf"(^|___){name}$")

# RIGHT - anchored, with the namespace prefix optional.
re.compile(rf"^(?:.*___)?{name}$")
```

A filter that matches nothing is not a strict filter. Depending on the framework it is either an
empty catalogue — an agent that retrieves nothing and reads as a weak model — or no filtering at
all, which is §21's failure with extra steps. Assert on the outcome rather than on the pattern:
`if not tools: raise` is the difference between a named startup error and a triage that quietly
retrieved no evidence.

**Why it hides:** the model never hits this. A framework builds the model's tool list from
`tools/list`, so the model receives the namespaced names and calls them correctly. Hand-written
deterministic code is usually written from the Lambda handler or the target's tool schema, where
only the bare name appears. The two halves of one agent then disagree, and only the hand-written
half is broken — which reads as "tool calling works, this one tool is flaky."

**Diagnostic:** call `tools/list` against the Gateway and print the names verbatim
([List available tools in an AgentCore
gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-list.html)).
Then check per-target Lambda log groups: a working target and a target with no log group at all,
in the same Gateway, is this defect's signature.

The same composite name appears again in Cedar policy action IDs — see §23. Get it wrong there and
the failure is silent rather than loud.

---

## 20. Never collapse a tool error into a placeholder value

**Symptom:** a downstream record carries `"unknown"`, `"n/a"` or an empty string where an
identifier should be — and nothing anywhere reports an error.

Real defect: a helper wrapped every tool call in `try/except`, returned `{"raw": <text>}` on any
failure, and the caller then did `result.get("claim_id", "unknown")`. A hard failure — the tool had
not executed at all — became the string `"unknown"` and travelled downstream as if the write had
succeeded.

```python
# WRONG — an exception becomes a value, and the value looks like data
try:
    return json.loads(tool_result)
except Exception:
    return {"raw": tool_result}
...
claim_id = result.get("claim_id", "unknown")

# RIGHT — a failure stays a failure, at the point of failure
parsed = json.loads(tool_result)        # let a malformed result raise
if "claim_id" not in parsed:
    raise ToolResultError(f"{tool_name} returned no claim_id: {parsed!r}")
```

**Why it hides:** the placeholder is syntactically valid, does not raise, does not log, and renders
fine. Every layer above treats it as a fact. Worse, it is *self-consistent* with a successful run,
so reconciliation and dashboards agree with each other while all of them are wrong.

**Rule: a helper that catches an exception must re-raise or return a value the caller cannot
mistake for data.** Raising and letting the caller surface the failure is almost always right; a
distinct error type is acceptable; a plausible-looking string never is. A placeholder that reads
like data is worse than an exception, because an exception stops the run and a placeholder ships.

This is the same principle as `control-stack.md` Layer 3 (no silent repair) applied to tool results
rather than model output: a response the system could not produce correctly is a signal, not an
inconvenience.

---

## 21. A prompt instruction is not an access control

**Symptom:** the model calls a tool the system prompt explicitly forbids.

Verified: a system prompt said "Do NOT call `create_claim`" — the deterministic layer was supposed
to own that write — and the model called it anyway. The compounding failure: because the
deterministic path was broken by §19, that off-script call was the *only* thing writing records at
all. The pipeline looked like it worked, and the prompt violation was the thing making it look that
way.

Enforce tool access structurally. The model can only call what it is offered, so the control is the
tool list, not the prompt:

```python
import re
from strands.tools.mcp import MCPClient

# Offered to the model: read tools only. Filters apply when tools are listed/loaded,
# so a write tool is never presented and cannot be selected.
read_client = MCPClient(
    transport_factory,
    tool_filters={"allowed": [re.compile(r".*___(get|list|search)_")]},
)

# Used by the deterministic layer: unfiltered, calls write tools directly.
write_client = MCPClient(transport_factory)
```

The mechanic that makes the split work: filters apply to `list_tools` / `load_tools`, not to
`call_tool_async` — so the same Gateway can present a restricted list to the model while your own
code calls the full set. String matchers match the server-side tool name exactly, which after §19
means the *namespaced* name; a regex is usually the more robust choice.

That mechanic is an **observed property of one framework version**, not a platform guarantee — the
filter is client-side library behaviour and nothing in AgentCore enforces it. So pin the framework
version, and assert the behaviour in a test: list the tools the model will actually be offered and
fail if any write tool appears. A filter that silently stops filtering is §23 in a different layer.
The control still looks configured, the code still reads as least-privilege, and it no longer does
anything.

Filtering the list is a client-side control: it decides what the model is offered, not what the
Gateway will accept. For a boundary the agent code cannot bypass, put a policy engine in front of
the Gateway (§23) — enforcement outside agent code is the point of it.

**Why it hides:** prompt instructions are obeyed most of the time. Occasional violation reads as
model flakiness rather than as a missing control, and no test asserts on "the model did not call a
tool it was told not to call." Least privilege belongs in the tool list, not the prompt. See
`control-stack.md` Layer 1.

---

## 22. Fixing a broken write path can create a second one

**Symptom:** after correcting a tool name, every run produces two records with different IDs.

If the model has been writing off-script (§21) and you fix the deterministic caller's tool name
(§19) *without* removing the model's write access in the same change, both paths now work. The
model writes during its phase, the deterministic layer writes again afterwards with its own
generated ID, and one business event becomes two records. Counts, reconciliation and dedup all
break — and in a regulated context there is now no defensible answer to which record is
authoritative.

**Rule: the tool-name fix and the tool-list restriction are one change. Ship them together.** If
they must be staged, remove the model's write access *first*. A write path that fails is a visible
defect; a write path that duplicates is a silent data-integrity defect, and the second is far more
expensive to unwind.

**Diagnostic:** count target Lambda invocations per run window (§18) rather than counting records.
Two invocations of the same write tool in one run is the signature even if deduplication downstream
hides the second record.

---

## 23. A policy that never matches is indistinguishable from no policy

**Symptom:** none — which is the problem. A Cedar `forbid` policy reports `ACTIVE`, its policy
engine is attached to the Gateway in `ENFORCE` mode, and a request the policy is supposed to block
succeeds anyway.

Verified: a rule intended to block claims at or above a threshold conditioned on
`context.toolName`. That attribute does not exist in the AgentCore Gateway request context, so the
condition never evaluated true, the rule never matched, and the request was authorised by the rest
of the policy set. **`ACTIVE` plus `ENFORCE` proves the policy is loaded and the engine is
enforcing. It proves nothing about whether any condition ever matches a real request.**

Two facts decide the shape of the policy:

- **Tool identity lives in the Cedar action, not in the context.** The action ID is the same
  composite name as §19: `AgentCore::Action::"<targetName>___<toolName>"`.
- **Tool arguments arrive under `context.input`**, keyed by the parameter names in the target's
  tool schema — `context.input.<parameter>`.

Verified working form:

```cedar
forbid(principal,
       action == AgentCore::Action::"create-claim___create_claim",
       resource == AgentCore::Gateway::"<gateway-arn>")
when { context has "input"
       && context.input has "estimated_amount"
       && context.input.estimated_amount >= 100000 };
```

Constraining the action forces a concrete resource. `resource is AgentCore::Gateway` is rejected at
deploy time (observed wording, not documented — the constraint it describes is real, the sentence
is not a contract):

```
ValidationException: When parsing the policy statement, a constrained action scope was
encountered, please constrain the resource to a specific AgentCore::Gateway resource when
creating tool-specific policies.
```

Wildcard resources are rejected generally, so the Gateway must exist before its policies can be
written — plan for a two-phase deploy (create Gateway, read its ARN, add policies, redeploy).

**Rule: verify every authorization policy empirically, in both directions, by calling the Gateway
directly — before trusting the control.** One request above the threshold must be denied, and one
below it must still succeed. A single-direction test proves nothing: everything is denied by
default, so a lone deny can come from an absent permit, and a lone allow can come from a forbid
that never matched.

**Wait out the cache before you believe the result.** Gateway access-policy changes are cached and
take **up to 15 minutes** to propagate. A probe run immediately after a policy edit may still be
exercising the *previous* policy — so the run that "verified" the control tested something that no
longer exists, and the run that "proved" a rule inert may have been testing its absence. This is
the failure mode most likely to leave a broken control recorded as verified, because it produces a
green result rather than a confusing one. Re-probe after the propagation window, and record the
timestamp of the policy change next to the timestamp of the probe so the evidence carries its own
refutation if the gap is too small.

**Diagnostic — a policy denial is not a JSON-RPC error.** AWS documents a denial as a JSON-RPC
**success response** carrying an error flag inside the result:

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"AuthorizeActionException - Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"}],"isError":true}}
```

So assert on **`result.isError == true`**, then parse the **bracketed reason**, which is the part
that carries the information:

- `[No policy applies to the request (denied by default).]` — nothing matched and default-deny
  fired. This is what an inert rule produces, and it is textually near-identical to a real match.
  Seeing "Tool Execution Denied" is therefore **not** evidence that your condition matched.
- A bracket naming a **policy ID** — that rule matched and denied. This is the evidence you want,
  and it is the only form of it.

Two consequences follow. First, **a client that only checks for a JSON-RPC error code reads a
denial as a success**: there is no `error` object, the call returned normally, and a naive wrapper
hands the model or the caller a result it should have rejected — a denial silently becoming a
tool result is the §20 failure at the authorization boundary. Second, `-32002` is not this code:
it belongs to a **VPC endpoint policy** rejection, a different control entirely. A test that keys
on `-32002` is testing network path, not authorization, and will pass while the Cedar policy does
nothing.

*Not* seeing a denial at all on a request that should have been blocked means the condition never
matched. It does not mean enforcement is off.

Two further controls worth knowing when the policy is the compliance boundary:

- **`LOG_ONLY` mode** evaluates and logs decisions without enforcing them. Use it to observe what a
  new rule *would* decide before promoting it, then re-run the two-direction test after promoting —
  after the 15-minute propagation window, not immediately after the mode change.
- **`bedrock-agentcore:UpdateGateway` alone can disable enforcement** — it can flip the engine's
  mode from `ENFORCE` to `LOG_ONLY`, or detach the engine entirely, with no separate action or
  condition key protecting the field. Treat that permission as equivalent to permission to remove
  the control, and grant it accordingly.

Sources:
[Getting started with Policy in AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-getting-started.html),
[Understanding Cedar policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html),
[Policy enforcement modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-enforcement-modes.html),
[Using a gateway with a policy](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/use-gateway-with-policy.html)
(the denial response shape and the caching behaviour).

---

## 24. An unset inference parameter is a model default, not a neutral one

**Symptom:** a workflow that was stable becomes erratic after a model-ID change nobody
classified as a behaviour change — wider run-to-run variation, occasionally a different
verdict on an identical alert — with no diff in the prompt, the tools or the schema. Or no
symptom at all, until someone asks which settings produced a decision on file and the record
cannot say.

Bedrock's `InferenceConfiguration` carries exactly four members: `maxTokens`, `stopSequences`,
`temperature` and `topP`. Anything else a model supports — `top_k` among them — travels in
`additionalModelRequestFields`, not here. For `temperature` and `topP` the API reference says
what it says for `maxTokens` in §8: *"The default value is the default value for the model
that you are using."*

Frameworks make the omission invisible. Strands' `BedrockModel` assembles `inferenceConfig`
from a comprehension that drops every key whose value is `None`:

```python
"inferenceConfig": {
    key: value
    for key, value in [
        ("maxTokens", self.config.get("max_tokens")),
        ("temperature", self.config.get("temperature")),
        ("topP", self.config.get("top_p")),
        ("stopSequences", self.config.get("stop_sequences")),
    ]
    if value is not None
},
```

A parameter you never set is therefore not sent as a neutral value — it is not sent at all,
and the vendor's default applies. **That makes a model-ID swap a change to sampling behaviour
as well as to prompt behaviour.** Same failure family as §16 and §17: the model ID looks like
one decision and is in fact several, only one of which anyone re-tests.

**Temperature 0 is greedy decoding, not reproducibility.** `InferenceConfiguration` has no
`seed` member, so there is nothing to hold fixed across runs. Temperature 0 pushes the model
toward the highest-probability continuation at each step, which narrows variance
considerably — it does not make a run replayable, and an identical request can still return a
different response. The distinction matters more here than in most domains: writing "set
temperature 0 for auditability" into a design document manufactures a determinism guarantee
the platform does not offer, and a control everyone believes is present is worse than an
acknowledged gap. Reconstructing a compliance decision rests on the audit record — inputs,
model ID, and the parameters actually sent — and on the deterministic post-generation layer
(`control-stack.md` Layer 5), never on the model repeating itself.

Set `temperature` **or** `topP`, not both. They truncate the same distribution and interact
unhelpfully. AWS documents this for Anthropic models on Bedrock — *"When adjusting sampling
parameters, modify either `temperature` or `top_p`. Do not modify both at the same time"* —
and for Claude Sonnet 4.5 and Haiku 4.5 it is an API restriction rather than advice. Across
vendors generally it is standard practice rather than a documented rule. Note the
consequence: whichever axis you leave unset resolves to the vendor's default, so pinning the
model ID is what makes the unset half determinate — another reason the two belong on the
record together.

**Rule: state `maxTokens`, `temperature` (or `topP`) and `stopSequences` explicitly, pin them
per workflow, and record the set actually sent alongside the model ID on every decision
record.** A drafting workflow and a classification workflow have no reason to share sampling
settings, and "whatever the model does by default" is not a setting anyone chose.

**A generation bump inside one vendor is a migration, not a config edit.** §16 and §17 cover
swapping vendors; the change that actually happens to a running platform is Claude *n* to Claude
*n+1*, and it arrives looking like a one-line diff. Moving 4.5 to 4.6 alone: **prefill is removed
outright** (a hard 400, not a degradation, so any prompt that seeds the assistant turn stops
working); the structured-output parameter is **renamed** (`output_format` → `output_config.format`;
Bedrock Converse `outputConfig.textFormat`); extended thinking changes from
`{"type": "enabled", "budget_tokens": N}` to `{"type": "adaptive"}`; **which models accept an
effort parameter changes**; the context window moves **200K → 1M**, so a prompt sized for the new
model fails on failover to the old one — the failure appears during an incident, at the worst
moment, in the path nobody tested; and the **prompt-cache minimum token threshold moves**, so
content that used to cache silently stops and the cost and latency regression has no error
attached to it.

None of that is visible in a diff that changes a model ID string. **Treat a generation bump as
full re-validation against the golden set**, at the same depth as a vendor swap. The current
breaking-change table lives in the global `amazon-bedrock` skill
(`amazon-bedrock/references/model-migration.md`) — read it at the time of the change rather than trusting any list
pinned here, which is stale the day the next generation ships.

**Diagnostic:** log the resolved `inferenceConfig` — what went on the wire, not what the
config object intended — and assert the keys you expect are present. A parameter absent from
the request is a parameter the vendor chose. To size the variance you are actually carrying,
run one ambiguous golden fixture repeatedly at the configured settings and record the spread;
that number, rather than an assumption of determinism, is what an evaluation is measuring
against.

Sources:
[InferenceConfiguration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_InferenceConfiguration.html),
[Influence response generation with inference parameters](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-parameters.html),
[Anthropic Claude Messages API request and response](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html).

---

## 25. A synthesiser cannot tell a short evidence set from a complete one

**Symptom:** a case write-up reads as complete, well-sourced and confident. One specialist in the
fan-out failed, its findings are simply absent, and nothing in the output says so. Or a fact in the
narrative turns out, on challenge, to trace back to no specialist at all.

The fan-out shape is right: one task per specialist, gathered with `return_exceptions=True`
(`architecture-patterns.md` pattern 6), so a failed OSINT lookup does not fail the case. But it
returns exceptions *as values*, and unless the contract downstream forces the distinction, the
synthesiser receives a list it has no way to read as short. "OSINT found nothing" and "OSINT never
ran" arrive as the same input. Absence is not a token the model sees, and a model asked to
synthesise the available material will synthesise the material available.

The merge is also where a fact can acquire an author it never had. Two specialists' outputs sit in
one context window, and a plausible link between them is exactly the kind of sentence a model
writes down. Without per-fact attribution — asserting specialist, source tool result, confidence,
retrieval timestamp — nothing distinguishes an asserted link from a manufactured one, and a QA or
verifier agent has only the prose to re-read.

**Why it hides:** every child record is correct. The specialist that failed recorded its failure;
the ones that ran recorded valid runs. The defect exists only at the case level — which is the
level nobody persisted (`control-stack.md`, case-level records for multi-agent workflows). It also
inverts the blame: the finished narrative looks like synthesiser hallucination, so the
investigation goes at the synthesiser's prompt and its temperature, when the cause is an input set
that was three of four.

**Rule: a synthesis step consumes an explicit manifest, not a list of results** — which specialists
were dispatched, which returned, which failed — **and every fact carries its attribution.** A case
whose evidence set was incomplete is recorded as incomplete, and that is a finding about the case,
not an operational detail to be tidied away.

**Diagnostic:** compare the specialists the case record claims against the specialists that
actually emitted spans under the case's trace ID — which requires W3C Trace Context propagated
across every agent boundary, or there is nothing to join on and correlation falls back to
timestamps. Then assert that every fact in the write-up resolves to an attribution entry, and fail
the *case* rather than the fact when one does not. As in §18, the narrative is not the evidence.

Sources:
[AGENTOPS05-BP01 Establish end-to-end tracing and telemetry for agent operations](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html),
[AGENTOPS05-BP03 Implement structured logging and comprehensive audit trails](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html).

---

## 26. What a controlled prompt/model matrix actually measured

Every rule above is a defect. This one is a measurement, and it is here because the questions it
answers — *which prompt?* and *which model?* — are the two most often settled by assumption in a
design review, and the assumptions are wrong in specific, repeatable ways.

**The run.** An AML alert-triage golden set of 15 fixtures (2 clear true positive, 2 clear false
positive, 4 ambiguous, 2 multi-typology, 4 bias probes, 1 malformed input) × 3 prompt structures ×
6 models, with 3 passes on the repeat-worthy categories: **630 graded invocations**, temperature 0,
`maxTokens` 4096. All figures below are *observed* on that set — they are evidence about a method,
not a league table to copy.

**The prompts.** **v1** discursive baseline: role, prohibitions, enum definitions, schema. **v2**
the same content restructured terse — bare enum values, definitions moved above the schema, and an
explicit instruction never to echo a field's guidance text as its value. **v3** chain-of-thought —
reason inside `<analysis>` tags, then emit JSON.

| Prompt | Mean across 6 models | Best single result |
|---|---|---|
| v2 terse | **77.3%** | nova-pro 83.9% |
| v1 discursive | 72.0% | nova-pro **86.7%** — 100% schema, 5.8s p95 |
| v3 chain-of-thought | 48.1% | nova-pro 80.7% at 97.1% schema |

| Model | Mean across 3 prompts | p95 latency, range across the 3 prompts |
|---|---|---|
| nova-pro | 83.8% | 4.5s – 7.3s |
| opus-4-6 | 75.2% | 27.9s – 66.6s |
| nova-2-lite | 66.8% | 3.8s – 4.3s |
| haiku-4-5 | 58.9% | 16.9s – 41.8s |
| sonnet-4-6 | 56.2% | 45.9s – 85.1s |
| nova-lite | 53.8% | 2.8s – 5.4s |

The latency column is a range rather than a single figure deliberately: prompt structure moves p95
by more than 2× on some models and barely at all on others. The Nova models are nearly flat across
all three prompts; the Claude models are not, and the spread is almost entirely v3 — chain-of-thought
roughly doubled p95 on opus-4-6 and haiku-4-5. So a prompt change is a latency change, and on the
models where it is a large one it is also (26.2) where format compliance collapsed. You pay twice
for the same decision.

**26.1 "Which prompt is better" has two answers, and you have to say which one you asked.** v2 won
on 5 of 6 models and won the prompt mean (77.3% against 72.0%). v1 won on exactly one model —
nova-pro — and won there by enough to take the best single configuration in the whole matrix. Both
readings are correct: if you are pinning a model, ship the prompt that wins *on that model*; if you
are defending against a model swap, ship the prompt with the best *mean*. Reporting one winner
without saying which question it answers hides the robustness question entirely.

The deeper consequence: **prompt and model are not independently optimisable.** The best prompt for
the best model was not the best prompt on average, so the usual method — fix the model, tune the
prompt, then revisit the model — finds a local optimum and reports it as the answer. Vary both, or
state plainly that you searched one axis.

**26.2 Chain-of-thought is a capability of the model, not a property of the prompt.** v3 did not
lose on reasoning quality; it lost on **format**. Schema compliance under v3: 28.6% on sonnet-4-6,
31.4% on haiku-4-5, 51.4% on nova-lite. Only nova-pro and opus-4-6 held format while reasoning
first. Asking a mid-tier model to think before emitting structured output trades format compliance
for reasoning and usually loses the trade — and it loses it invisibly, because the output that
survives parsing looks better reasoned, so the surviving sample flatters the change. Where a
reasoning trace is needed for audit, take it from a **separate call**, or from a model that
demonstrably holds format under it. Do not buy it by restructuring the prompt on a model that
cannot pay.

**26.3 A more expensive model is not a better model for a bounded classification task.** nova-pro
(83.8%) beat opus-4-6 (75.2%) and beat sonnet-4-6 by 27.6 points on this set. This is §17 arriving
from the opposite direction: §17 is a cheap model failing where cheapness was assumed adequate,
this is an expensive model losing where expense was assumed sufficient. Neither intuition
transfers. Select on measured performance over your fixtures, not on tier.

**26.4 Latency is a selection criterion, not a footnote.** p95 at equal-or-better quality: nova-pro
5.8s, opus-4-6 28.8s, sonnet-4-6 45.9s. An 8x spread decides architecture as firmly as the score
does — 5.8s fits inside a synchronous request and a human's attention; 45.9s does not fit inside
§11's 29-second ceiling at all and forces the async shape on you. Score quality and p95 together,
or you will pick a model whose real cost is a rewrite.

**26.5 Grade truncation separately from schema failure.** The 67 failed invocations broke down as
**40** missing schema fields, **18** no JSON object in the response, and **9** no JSON object *with*
`stopReason=max_tokens` — a 4096-token budget running out mid-object. Those 9 are a token-budget
defect, fixed by raising `maxTokens` and paying for it in quota (§8), and they have nothing to do
with the prompt. A harness that does not capture `stopReason` files them alongside the other 18 and
sends you to rewrite a prompt that was working. Capture `stopReason` on every invocation and make
truncation its own category in the error taxonomy.

**26.6 Do not inline field guidance inside the schema.** A schema that puts a field's explanation
next to the field invites the model to return the explanation **as** the value. One model emitted a
recommendation enum of `"APPROVE (release, residual risk explainable)"` — a syntactically valid
string, an invalid enum member, and a §20-shaped failure the moment anything downstream accepts it
rather than rejecting it. Put enum values **bare** in the schema and move the definitions above it.
That single change accounts for much of what separated v2 from v1.

**26.7 A bias probe is only interpretable when both arms parse.** The only two flagged bias
divergences in the entire run were on the worst-performing configuration, and **each had a schema
failure in one arm** — the harness compared a parsed verdict against a parse failure and reported
the difference as bias. **A schema-invalid arm must be excluded from the comparison, not scored as
divergence:** fail the probe as *inconclusive* and say which arm did not parse. This is a
harness-design rule, not a nicety. A false fairness finding is worse than no finding, because it
will be escalated, investigated, traced to a parsing bug — and it teaches reviewers to discount the
next divergence, including the real one.

**26.8 One pass is a sample, not a measurement.** There is no seed (§24), so a single run per cell
measures the run rather than the configuration. Repeat the categories where repetition is
informative — ambiguous, multi-typology, bias probes — and report the **spread** next to the mean.
A configuration that wins by two points on one pass has not won anything, and a rank order built
from single passes will reorder itself the next time it is run.

---

## 27. Direct code deployment does not install your dependencies

**Symptom:** the runtime reaches `READY`, every invoke returns `RuntimeClientError` / HTTP 502, and
the container's log holds one line — `ModuleNotFoundError: No module named 'bedrock_agentcore'` —
naming a package that is listed in the `requirements.txt` sitting inside the artifact you uploaded.

AgentCore's direct code deployment path asks you to "package your code and dependencies into a zip
archive", and at run time it simply puts `/var/task` first on `sys.path`. **Nothing runs `pip`.** A
`requirements.txt` inside the zip is an inert text file: it documents an intention that no step in
the pipeline acts on. Vendor the wheels at build time, for the runtime's platform rather than the
build host's:

```bash
pip install \
  --target build/ \
  --platform manylinux2014_aarch64 \   # the runtime is ARM64
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt
find build -name '__pycache__' -type d -prune -exec rm -rf {} +
```

That last line is not tidiness. AgentCore rejects an artifact containing Python cache files —
`Your artifact contains Python cache files that are incompatible with the target runtime` — and a
`pip install --target` leaves hundreds of `__pycache__` directories behind. The same rejection
fires for a second, more confusing reason: a developer who runs the test suite before packaging has
byte-compiled the source tree, so the artifact ships `.pyc` files that a fresh clone would not have.
**That deploy therefore succeeds from a clean checkout and fails on the machine where the code is
actually worked on**, which reads as an environment problem rather than a packaging one. Exclude
`__pycache__`, `*.pyc`, `*.pyo`, `.pytest_cache` and `.mypy_cache` from every asset built out of a
directory a human runs code in — not just from the obvious ones.

**Why it hides:** the starter toolkit performs this packaging for you. Every tutorial path therefore
works, and the defect appears only when you build the artifact yourself — which is exactly what an
infrastructure-as-code deployment does. The failure also arrives one layer away from its cause: the
runtime status is `READY`, so the deploy is green, and the missing import surfaces as a 5xx from the
first invoke.

**Diagnostic:** unzip the artifact and list its top level. If you see your package and a
`requirements.txt` and nothing else, no dependency was ever installed. Confirm from the container
log rather than the runtime status — status is not evidence (§4).

---

## 28. An MCP transport `auth=` that type-checks, runs, and is ignored

**Symptom:** `MCPClientInitializationError: unhandled errors in a TaskGroup`, naming neither
authentication nor the parameter responsible.

Against a Gateway with `AWS_IAM` inbound auth, **three distinct defects produce that identical
string**, which is why they have to be peeled one at a time:

1. **The client never signs at all.** An MCP client written against a token-authenticated Gateway
   sends nothing SigV4, the Gateway answers 401, and the MCP client re-raises it as the TaskGroup
   error above.
2. **The signing is configured on a parameter that is accepted and ignored.**
   `streamablehttp_client(url, auth=…)` emits a `DeprecationWarning` reading *"…are deprecated and
   will be ignored. Configure these on the `httpx.AsyncClient` instead."* A **warning**, not an
   error. The call type-checks, runs, and puts the request on the wire unsigned.
3. **The auth flow is a sync generator.** `httpx` drives a custom auth flow with `__anext__`, so
   `async_auth_flow` must be `async def`. A `def` version raises inside the TaskGroup and surfaces
   as — again — the same string.

```python
# WRONG — accepted, warned about, ignored. The request goes out unsigned.
async with streamablehttp_client(url, auth=sigv4_auth) as streams:
    ...

# RIGHT — configure auth on the client, then hand the client to the transport.
async with httpx.AsyncClient(
        auth=sigv4_auth,
        timeout=httpx.Timeout(30.0, read=300.0)) as client:
    async with streamable_http_client(url, http_client=client) as streams:
        ...
```

When you supply the client, its lifetime is yours to manage — the `async with` above is what closes
it, and dropping that is a socket leak that only shows under load.

**Why it hides:** a deprecation warning is invisible under Python's default warning filters, and the
error the user sees is raised several layers above the mistake, by a construct (`TaskGroup`) whose
job is to aggregate other exceptions. Nothing in the message points at auth, at the parameter, or at
the Gateway's 401.

**Diagnostic:** run the client once under `python -W default` (or `logging.captureWarnings(True)`)
and read the warnings — the deprecation names the fix in its own text. Then confirm from the
Gateway's side that the request arrived unauthenticated, rather than inferring it from the client.

---

## 29. A Gateway interceptor's envelope is nested, in both directions

**Symptom:** two different ones, and the second only appears after you fix the first. Either the
interceptor refuses every request with an **empty** tool name, or the Gateway returns **500 against
its own interceptor's output**.

The verified shape, inbound and outbound:

```
in   {"interceptorInputVersion": "1.0",
      "mcp": {"gatewayRequest": {"headers": {...}, "body": {...}},
              "requestContext": {...}}}

out  {"interceptorOutputVersion": "1.0",
      "mcp": {"transformedGatewayRequest": {"body": {...}}}}
```

**Reading `event["body"]`** — the flat shape a handler author naturally assumes — yields `{}` on
every real request. The tool name is then empty and `method` is absent, so an unknown-tool refusal
fires on the MCP `initialize` handshake: no session is ever established and no tool is ever reached,
which presents as a Gateway that cannot talk to its own target rather than as a handler bug.

**Returning the inbound `headers`** — the apparently safe "pass everything through unchanged" move —
produces the 500. With `passRequestHeaders` enabled those headers describe the request as it
*arrived*, so `host` and `content-length` are wrong for the request the Gateway then makes
downstream, and the Gateway rejects its own interceptor's output. Omit the `headers` key entirely
and return the body alone; AWS's own interceptor example returns a minimal header set rather than an
echo. Shapes tried, and what each did:

| Output shape | Result |
|---|---|
| bare `{"transformedGatewayRequest": {"body": …}}` | every request refused; no handshake at all |
| version + `mcp` wrapper, echoing `headers` | handshake reaches `tools/list`, then 500 |
| the original `gatewayRequest` object with `body` replaced | worse — 500 at `initialize` |
| version + `mcp` wrapper, **no `headers` key**, body only | works; tools invoked |

**A REQUEST interceptor sees all gateway traffic**, not only `tools/call` — `initialize`,
`notifications/initialized`, `tools/list` and `ping` all pass through it. Let them through
untouched; they invoke no tool and read no tenant data. But make sure a body that omits `method`
falls through to the **refusal** path rather than the pass-through path, or the pass-through becomes
a way to reach a tool with no scope injected.

**Why it hides, and this is the part worth keeping:** the handler's unit tests passed throughout,
because they fed a flat envelope of the test author's own invention. A test that asserts a contract
the platform does not deliver is worse than no test — it converts an unknown into a false
certainty, and it will keep doing so after the handler is fixed. **The fix that stops this
recurring is feeding the documented shape in the test, not the change to the handler.** The same
defect, in the same codebase, had already been found and fixed once in a tool handler's argument
parsing; the tests were what let it come back in a different file.

**Diagnostic:** log `sorted(event.keys())` and the keys of `event["mcp"]["gatewayRequest"]` on the
first request and read them before writing any parsing logic. Keys only, never values — with
`passRequestHeaders` on, the values include the caller's `Authorization` header.

---

## 30. A Gateway does not propagate the runtime session id to its interceptor

**Symptom:** the interceptor refuses every tool call with something like `no session id on the
request`, while the runtime was invoked with a perfectly valid `runtimeSessionId` and the session
itself is working.

The diagnostic is unambiguous once you print the envelope: `gatewayRequest` carries `headers`,
`body`, `httpMethod`, `path`, and a `context` holding only `identity`. There is **no AgentCore
runtime session id anywhere in it**, and no `mcp-session-id` header either. A design that expects
the Gateway to carry the runtime session through to a REQUEST interceptor is designed against
behaviour the platform does not have.

If the interceptor's job is to look up a server-side binding keyed by session — which is how §7's
server-derived session id becomes an access control rather than a cache key — then the agent has to
carry the identifier, because nothing else on the path does:

```python
async with httpx.AsyncClient(
        auth=sigv4_auth,
        headers={"x-amzn-bedrock-agentcore-runtime-session-id": session_id}) as client:
    ...
```

**This does not hand the agent its scope, and the distinction is the whole control.** The header
names a *session*; the binding behind that session is written server-side and remains the only
source of tenant and customer. The agent holds exactly one session id — its own, supplied at invoke
— and where the id is `sha256(tenant | customer | work_item | salt)` per §7, another tenant's is not
guessable. What changes is who carries the identifier, not who decides what it means.

Two conditions make that true rather than merely plausible, and both are worth asserting in a test.
The binding lookup must **fail closed**: an absent binding, an expired one, or one missing either
scope field is a refusal, never a default. And the interceptor must **strip** any scope fields the
caller supplied before injecting its own — a caller-supplied `tenant_id` is either a tool-schema
regression or an injection attempt, and both deserve an alarm rather than a silent overwrite.

**Diagnostic:** on the "no session id" path, log the inbound header **names**, the
`requestContext` **keys**, and the `gatewayRequest.context` **keys** — structure only. Without that
you learn where the id is absent from but never where it actually is. Cross-reference §7 and
`deployment-patterns.md` → *Session and tenant binding*.

---

## 31. A permission error can be an isolation breach reporting itself

**Symptom:** `AccessDeniedException` on a data-plane action — say `dynamodb:Scan` — from the agent's
execution role. The obvious reading is that a grant was forgotten.

Sometimes it is. But the error also fires when a code path is doing something it should never have
been able to do, and then the obvious fix is the damaging one. A real case: a helper that built a
one-line alert summary called `table.scan(FilterExpression="alert_id = :a")` **from inside the
agent**, on a table holding every tenant's alerts. That is two defects wearing one error message —
a full-table read across all tenants narrowed client-side, and a direct table read from a component
whose entire design says data arrives only through scope-injected tools.

Granting the role `dynamodb:GetItem` would have cleared the error in one line, left the cross-tenant
read in place, and left the tool path bypassed. **Granting a cross-tenant read to fix a permission
error is how an isolation model dies** — quietly, in a commit that looks like plumbing. The correct
fix deleted the read: the scoped tool for that record already existed, was already in the agent's
catalogue, and was already bound to the right alert.

So before granting anything to clear an `AccessDenied`, answer two questions in order: *what did
this code path want?* and *is it allowed to want it?* Only the second one tells you whether the
missing grant is the bug.

**Worth noting in the other direction:** the isolation model caught this itself. The execution role
held no data-plane permissions at all, so the breach could not execute — it could only be attempted.
A role scoped to exactly what a component legitimately does converts a design error into a runtime
error on the first attempt, which is the cheapest place to find one.

**Diagnostic:** for every `AccessDenied` on a data action, find the calling line before writing the
policy statement. If the caller is the agent or the model-driven path rather than a tool or a
deterministic layer, treat it as §21 and §22 territory — an access-control question — not as an IAM
gap.

---

## 32. A logger with a level and no handler discards every INFO line

**Symptom:** your agent's structured logs are missing from the runtime log group, but its
*errors* are there. Tool Lambdas running the identical logging setup log fine. It reads as "the
framework swallows agent logs" rather than as a defect in your code.

The two lines at the top of almost every AWS Python example:

```python
log = logging.getLogger()
log.setLevel(logging.INFO)
```

**`setLevel` is not enough on its own.** With no handler attached to the root logger, Python falls
back to `logging.lastResort`, whose level is **WARNING**. Every `log.info` is discarded silently;
`log.error` survives, because it clears that threshold. Reproduce it in four lines:

```python
>>> log = logging.getLogger(); log.setLevel(logging.INFO)
>>> log.handlers
[]
>>> logging.getLevelName(logging.lastResort.level)
'WARNING'
```

**Why it hides, and why it is worse in AgentCore than in Lambda:** the Lambda runtime installs a
root handler for you, so identical code logs correctly there. An AgentCore Runtime container does
not. A codebase whose tools are Lambdas and whose agent is a container therefore sees exactly half
its telemetry — the half that isn't the agent — and the log group looks alive the whole time
because the error path works.

The cost lands precisely where it hurts a compliance deployment: the per-invocation **usage
metering line** is normally `log.info`, so a deployment built around "the provider's logs hold usage
telemetry only" emits no usage telemetry at all. The privacy control still runs — the projection
gate blocks on an undeclared field whether or not anyone can read the result — but the *evidence
that it ran* is gone, and an examiner asking "show me what left the boundary" gets nothing.

```python
if not log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
```

**Diagnostic:** log one line at INFO and one at ERROR from inside the container and compare. If only
the error arrives, you have this. Do not conclude the log group or the delivery configuration is
broken until you have ruled it out — the symptom points squarely at delivery and the cause is three
lines from the top of your own file.

---

## 33. A duration validated as a count raises a privacy alarm on a slow run

**Symptom:** a field-level content control fires and reports something alarming — an allowlisted
field carrying unexpected content, i.e. a suspected PII leak — on a run where nothing unusual
happened except that it took a while.

A metering projection typically declares each emitted field with a predicate:

```python
def _is_count(v): return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000

METERING_ALLOWLIST = {
    "red_flags_count": _is_count,
    "latency_ms":      _is_count,     # <- wrong: this is a duration, not a cardinal
}
```

`10_000` is a sensible ceiling for "how many red flags" and a nonsense one for "how many
milliseconds". A tool-calling triage runs 10–30s routinely, so an 11.5s run exceeds it, the field is
diverted, and the pipeline announces a privacy defect **because the model took eleven seconds**.

Separate the predicates, and ceiling the duration at something that means "anomaly":

```python
def _is_duration_ms(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 900_000  # runtime max
```

**The general form is worth more than the instance.** This is a control whose **alarm was correct**
— an allowlisted field really did carry a value its predicate rejected — and whose **diagnosis was
wrong**. The predicate did exactly what it was told; what it was told is that a duration is a small
cardinal. Two consequences, and the second is the expensive one: the field is lost from the
telemetry, and every reader is trained that this alarm means "slow run". The next one will be real.

**Diagnostic:** when a content control fires, check the *predicate* before you check the data. Ask
what range the field legitimately occupies in production — not in the unit test, where latency is a
hand-written 5_824. Any predicate reused across fields whose units differ is a candidate.

---

## 34. Assert on the SDK's real result object, not on a shape you invented

**Symptom:** two failures at once, both silent. Structured output "cannot be parsed" from a model
response that is visibly perfect in the logs, and the tool/retrieval ledger is empty on runs where
tools demonstrably executed.

Both come from guessing at an agent framework's result object. Taken from Strands, whose
`AgentResult` is a dataclass with fields `stop_reason`, `message`, `metrics`, `state`, `interrupts`,
`structured_output`, `checkpoint`:

```python
# WRONG, twice.
text   = str(getattr(result, "message", result) or "")   # `message` is a dict
ledger = from_results(getattr(result, "tool_results", []) or [])  # no such attribute
```

- **`message` is a `Message` TypedDict** — a plain dict at runtime. `str()` of it is a Python repr:
  single-quoted keys, and real newlines rendered as a literal `\n`. A JSON scan then meets a
  backslash where only whitespace is legal and fails at every candidate. `AgentResult.__str__` is
  the method that walks `content` and concatenates the text blocks, so `str(result)` is both simpler
  and correct — the defensive `getattr` had it exactly backwards, preferring the broken path and
  falling back to the right one.
- **`tool_results` does not exist**, so the `getattr` default fires on every run and the ledger is
  `[]` forever. `metrics.tool_metrics` is not a substitute: it counts calls and carries no result
  envelope. The transcript is the source — pair `toolUse` and `toolResult` content blocks by
  `toolUseId` off `agent.messages`.

**Why both hide.** The parse failure is **formatting-dependent**: a compact object has `"` straight
after `{` and survives the repr, a pretty-printed one does not — so the same code passes and fails
run to run and gets written off as model flakiness. The empty ledger is worse, because nothing
fails at all; and where the ledger feeds a control (§20's "an unavailable source cannot support an
approval"), an empty one silently switches that control **off** while every log line still looks
healthy.

**A `getattr` with a default on an SDK object is an assumption with the alarm disabled.** It cannot
fail loudly, by construction. Where the attribute is load-bearing, read it directly and let the
`AttributeError` find you on the first run.

**Diagnostic:** open the installed package — for a vendored artifact, the copy you actually shipped
— and read the dataclass. Then write the regression test against **that** shape. A test feeding a
hand-built stand-in asserts a contract the SDK does not offer, which is how this class of defect
survives a green suite (§29 is the same lesson, one layer down).

---

## 35. A DynamoDB write to a CMK-encrypted table needs `kms:Decrypt`

**Symptom:** `PutItem` fails with an opaque `ClientError` on a table encrypted with a customer
managed key, while an `s3:PutObject` to a bucket encrypted with **the same key**, from **the same
role**, in **the same function**, succeeds. Nothing in the failure mentions KMS.

The asymmetry is the whole tell, and it is not arbitrary. S3 SSE-KMS on the write path needs
`GenerateDataKey` and `Encrypt`. DynamoDB additionally **decrypts the table's data key** in order to
encrypt the item, so it needs `kms:Decrypt` on a write that reads nothing back. AWS states the
minimum set:

> At a minimum, DynamoDB requires the following permissions on a customer managed key:
> `kms:Encrypt`, `kms:Decrypt`, `kms:ReEncrypt*`, `kms:GenerateDataKey*`, `kms:DescribeKey`,
> `kms:CreateGrant`
> — [DynamoDB encryption at rest usage
> notes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html)

**Why this bites a least-privilege design specifically.** The instinct for a write-only audit path
is `key.grantEncrypt(role)`, which grants `Encrypt`, `ReEncrypt*` and `GenerateDataKey*` and reads
as exactly right: the component writes evidence and must not read it back. It is right for S3 and
insufficient for DynamoDB, so a component that seals to both gets a half-written record — and the
half that fails is the queryable one.

**Granting `Decrypt` is a real widening, so scope it.** In a typical stack one CMK encrypts the
evidence bucket AND the audit table, so an unconditioned `Decrypt` hands the writer the
cryptographic half of reading its own sealed evidence. `kms:ViaService` removes that: the
permission exists only for requests DynamoDB makes on the role's behalf.

```ts
role.addToPolicy(new iam.PolicyStatement({
  sid: 'AuditTableCmkViaDynamoDbOnly',
  actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*',
            'kms:GenerateDataKey*', 'kms:DescribeKey', 'kms:CreateGrant'],
  resources: [key.keyArn],
  // Region is a wildcard because AWS requires the permission to be
  // Region-independent for global tables — not sloppiness.
  conditions: { StringLike: { 'kms:ViaService': 'dynamodb.*.amazonaws.com' } },
}));
```

Keep the data-plane grant as narrow as it was: `dynamodb:PutItem` and nothing else. KMS is
necessary, not sufficient — a role with `Decrypt` and no `GetItem` still cannot read the table.

**Diagnostic:** when one store in a two-store write succeeds and the other fails, compare what each
service needs from the key before you re-read your own code. Reproduce the failing call with an
admin principal: if the item shape is accepted there, the fault is the role, not the payload. Do
that against a scratch table — see the standing rule below about writing probes into audit stores.

**And log the error CODE, not the exception type.** `"reason": "ClientError"` names the layer and
nothing else — a missing permission, a throttle and a validation error are indistinguishable. Pull
`response["Error"]["Code"]` into the log line; it is the difference between a diagnosis and a round
of guessing (§20's principle applied to your own error handling).

---

## 36. A projection gate does not stop the framework printing past it

**Symptom:** none. That is the defect. The metering line is clean, the allow-list is enforced, the
privacy assertion passes — and the model's full reasoning is in the provider's log group anyway.

A compliance deployment that promises "the provider's logs hold usage telemetry only" typically
implements it as a projection: an allow-list of fields, a predicate per field, and an emit function
that refuses to publish anything undeclared. That control can be perfect and still be irrelevant,
because it governs **one channel** and the log group has others:

- `BedrockAgentCoreApp(debug=True)` writes request/response logging to the container's stdout.
- An agent framework's **default callback handler** streams every model token to stdout — Strands
  does this unless you pass `callback_handler=None`. It is why `<thinking>` blocks appear in a log
  group nobody chose to send them to.

In a container, stdout **is** a log group. So the narrative goes around the gate rather than through
it, and every artefact that inspects the gate reports success. Measured on a real deployment: the
metering line carried no `rationale`, and the runtime log group held the full `rationale` string and
73 `<thinking>` blocks.

```python
app = BedrockAgentCoreApp(debug=False)
agent = Agent(model=model, tools=tools, callback_handler=None, system_prompt=prompt)
```

**The generalisable lesson is about how the boundary was tested, not about these two flags.** The
check that passed was "read the metering line, assert it carries no narrative" — a true statement
about the sanctioned channel, and no evidence at all about the protected side. **Test a boundary by
searching the protected side for the forbidden thing:**

```bash
aws logs filter-log-events --log-group-name /aws/bedrock-agentcore/runtimes/... \
  --filter-pattern 'rationale'      # expect zero events, not a clean projection
```

A boundary is only as good as its widest bypass, and the bypass is rarely in the code that
implements the control.

**Where the narrative should go, and the trap on the way there.** Silencing stdout deletes the only
copy unless the internal record actually exists. A design that documents "the examinable record
lives in the firm's own store" and leaves that write as a `TODO` has not split the data, it has
discarded half of it — and an audit table that reads 0 rows will be misread as a broken pipeline for
as long as it stays unwritten. Seal the record (`s3:PutObject` + `dynamodb:PutItem`, no update, no
delete) **before or with** closing the leak.

**One more trap, worth naming because it is tempting and quiet.** A review UI that needs to show
reasoning can be pointed at the log group in about twenty lines, and it works — because of the leak.
Do that and the UI now depends on the defect: closing it breaks the console, and whoever comes next
has a reason to leave the narrative in the provider's logs. Point the UI at the internal record,
which is inside the privacy perimeter and has a retention schedule, even when that means the panel
stays empty until the seal ships.

---

## 37. One botocore `Config` for the control plane and a blocking data-plane call multiplies your agent

**Symptom:** every queue message ends in `ReadTimeoutError` after ~125s while the invoking
function's own timeout is 600s and nowhere near reached. The agent runs anyway. Records appear,
late, with latencies an order of magnitude above normal.

A single client config shared across a resource and a data-plane client looks tidy and is a
duplication bug waiting for a slow call:

```python
_CFG = Config(retries={"max_attempts": 3, "mode": "standard"},
              connect_timeout=5, read_timeout=30)
_ddb = boto3.resource("dynamodb", config=_CFG)
_agentcore = boto3.client("bedrock-agentcore", config=_CFG)   # <- wrong client for this profile
```

30s and three attempts is right for DynamoDB. `invoke_agent_runtime` is **synchronous — it blocks
for the entire agent run**. Measured median for a 10-tool-call triage: 87s, max 325s. So the read
timeout fires mid-run, the retry policy starts a **second agent on the same alert**, then a third,
then a fourth, each ~30s apart. Confirmed by four `triage_start` lines carrying four `run_id`s
thirty seconds apart for one message.

Three compounding consequences, and the first is the one nobody predicts:

- **The duplicates are not independent.** `runtimeSessionId` is derived from the work item, so all
  four land on the **same** microVM and compete for the same account RPM quota. At 5 RPM they
  starve each other. You have not retried the work; you have built a self-inflicted thundering
  herd against a single session.
- **Token burn multiplies by the attempt count**, which is how a daily quota (§38) disappears
  during what looks like one run.
- **The caller reports failure and the queue redrives**, so the duplication outlives the invocation.

```python
# Control plane: unchanged.
_CFG = Config(retries={"max_attempts": 3, "mode": "standard"},
              connect_timeout=5, read_timeout=30)
_ddb = boto3.resource("dynamodb", config=_CFG)

# Data plane: its own profile. Retries OFF, and a read timeout inside the caller's ceiling.
_AGENTCORE_CFG = Config(retries={"max_attempts": 1, "mode": "standard"},
                        connect_timeout=5, read_timeout=550)
_agentcore = boto3.client("bedrock-agentcore", config=_AGENTCORE_CFG)
```

**Retries must be off, not merely fewer.** The call is not idempotent and the runtime keeps
executing after the client disconnects, so a retry does not *replace* a lost run — it *races* one
still in flight. There is no attempt count at which that is safe.

**The read timeout must sit inside the caller's own hard ceiling, not above it.** With a 600s Lambda
and `read_timeout=900`, Lambda kills the invocation first; a killed invocation returns no response
at all, so `batchItemFailures` never reaches SQS and the **whole batch** redelivers instead of the
one message that failed. Botocore has to be the component that gives up first. 550 against 600.

**Diagnostic:** grep the runtime log for the run identifier emitted at agent start and count
distinct values per inbound message. More than one means this. It is invisible from the caller,
which sees a single timeout, and invisible in the record store, which sees plausible rows.

**Write it right the first time:** any client whose calls block for the duration of downstream work
— agent invocation, long polls, synchronous orchestration — gets its own `Config`, declared next to
the client, with a comment saying what bounds it. Sharing a `Config` is only safe across calls with
the same latency profile.

---

## 38. Bedrock enforces a per-DAY token quota, and it is not the one you checked

**Symptom:** `ThrottlingException: Too many tokens per day, please wait before trying again` on a
workload well inside its requests-per-minute and tokens-per-minute limits. A 14-token request fails
identically to a large one, which rules out anything about the payload.

Quota discussions gravitate to RPM because that is what binds under burst. There is a separate
**daily** ceiling, and on a restricted account it is small enough to bind first. Observed on one
account in `eu-central-1`:

| quota | Opus-class | Sonnet-class |
|---|---|---|
| requests/min, cross-region | 5 | 10 |
| tokens/min, cross-region | 3,000,000 | 6,000,000 |
| **tokens/day, cross-region** | **2,592,000** | 10,800,000 |

Tokens-per-minute in the millions invites the conclusion that token quotas never bind. Per-minute
never does. Per-day does: at ~21k tokens per triage that is ~122 runs, and under a §37 retry storm
~30.

Two properties make it worse than an RPM cap:

- **It is not adjustable.** Both daily codes read `Adjustable: false` (`L-ED2BADF9`,
  `L-82CD9B28` for that model). This is a support/account path, not self-service — the same shape as
  an account-level RPM restriction.
- **Do not assume the applied value is the model's ceiling.** Measured across four accounts in one
  organisation, the *applied* daily quota was **0.06% of the AWS default**: 2,592,000 against a
  default of **4,320,000,000**. Every account was restricted, including the organisation's
  management account. A number that looks like a product limit because it is the same everywhere can
  still be a restriction applied everywhere.

```bash
# Always read the DEFAULT alongside the applied value. A quota below its default has been
# restricted; a quota above it has been increased. The remediation differs completely.
aws service-quotas get-service-quota         --service-code bedrock --quota-code L-ED2BADF9
aws service-quotas get-aws-default-service-quota --service-code bedrock --quota-code L-ED2BADF9
```
- **Recovery is not observable.** Nothing in the console or the API reports remaining daily budget
  or when the window rolls.

```bash
# Enumerate what actually applies — do not assume RPM is the whole story.
aws service-quotas list-service-quotas --service-code bedrock \
  --query 'Quotas[?contains(QuotaName, `per day`)].{n:QuotaName,v:Value,adj:Adjustable}'
```

**The only reliable readiness check is a one-token `converse` against each model you intend to
use** — the §"Standing rule" habit, applied to exhaustion rather than entitlement. It costs seconds
and it is the sole way to learn the budget has returned.

**Write it right the first time:** capacity-plan a batch or evaluation run in **tokens per day**, not
requests per minute, and measure per-unit token cost before sizing it. An evaluation that fits the
RPM budget comfortably can still be structurally impossible to run twice in one day.

---

## 39. With adaptive thinking, `maxTokens` bounds reasoning too — and tuning it to one model rigs any comparison

**Symptom:** a model that worked is swapped for another, and every run dies with
`MaxTokensReachedException: Model stopped generating due to maximum token limit`, surfacing to the
caller as a runtime 500 rather than anything mentioning tokens.

```python
INFERENCE = {
    "maxTokens": 4096,
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "high"},
}
```

Reasoning tokens are drawn from the **same** budget as the answer, so this caps thinking + output
together. A cap that fits one model is not a neutral setting — it is a threshold that model happens
to clear. Measured across 84 runs, the incumbent's output was 2,996 tokens median, 3,780 max: inside
4096 by under 8%. The replacement produced **10,243** on its first clean run and could not complete
a single triage at 4096.

**A model comparison run at a cap tuned to one arm measures which model fits the cap.** Publishing
"the cheaper model failed 16/16" from that setup reports a configuration artefact as a capability
result, and it fails in the direction that confirms the expensive model — which is exactly the
direction nobody audits.

The cap is a **truncation guard, not a spend control**. Raising it does not make a model generate
more; it stops a model being cut off mid-answer. Spend is governed by what the model actually emits,
which the record already meters.

**This also inverts a cost premise.** "Model A costs 1.7x model B" is a **rate** ratio; cost per unit
of work is rate x tokens. The cheaper model here consumed 3.4x the output tokens and 2.1x the wall
clock. Until both arms run under an identical cap, the direction of the cost comparison is not
known, let alone its magnitude.

**Write it right the first time:** set `maxTokens` from the most verbose model you might plausibly
run, not the one currently pinned, and re-validate it as part of any model migration (§24). If a
comparison is the point, fix every inference parameter across arms first and record it in each run's
audit record — the record already carries `inference_parameters`; use it as the check that the arms
were comparable.

---

## 40. An in-flight SQS message cannot be flushed, only outlasted

**Symptom:** a run aborted mid-flight leaves `ApproximateNumberOfMessagesNotVisible=1`. Every
documented flush leaves it there, and it reappears up to the visibility timeout later — inside the
next run, where it triages a work item nobody queued and seals a record indistinguishable from a
real one.

Three levers, all tried against one message, all measured, none of which worked:

| lever | result |
|---|---|
| `purge-queue` | documented not to remove in-flight messages |
| `VisibilityTimeout` 3600 → 10, drain, restore | drained **0** — a message carries the timeout in force when it was **received** |
| `MessageRetentionPeriod` 345600 → 60, wait 4 min, restore | still `NotVisible=1` — retention does not evict in flight either |

Deleting requires a receipt handle, and only the consumer that received the message holds one. If
that was an event-source-mapping poller you disabled, the handle is gone with it. The message
returned on schedule: received 13:22:44Z with a 3600s timeout, visible again 14:20:38Z, unaffected
by either attribute change.

**So do not design a procedure around draining it.** Either wait out the full original visibility
timeout, or take the consumer out of the path entirely — disable the event source mapping and drive
the work by invoking the consumer directly for the duration of the run. The second is strictly
better for an evaluation: it makes the phantom **structurally impossible** rather than merely
mistimed, and a synchronous invoke returns a definitive per-item result instead of leaving you to
infer completion by polling.

**Write it right the first time:** for any run whose results must be attributable, drive the work
through a path with no queue in it. Reserve the queue path for testing the queue.

---

## 41. Keying a record by an id SUFFIX collides, and the collision is silent

**Symptom:** an evaluation reports a fixture count that does not match the fixture list, or a
fixture's assertions appear to pass on a run that plainly violates them.

Two instances of the same mistake, in the same repository, one of them inside the script that
grades the other:

- A runner selected work items by matching the **last three characters** of a UUID and picked up
  nine instead of eight. Two tenants' ids ended `...0001`; the tenant prefix was the part carrying
  the distinction.
- A comparison script built its fixture map as `{f.alert_id[-3:]: f for f in FIXTURES}`. Two
  fixtures referenced the **same** work item — a strict one and a deliberately loose isolation
  control. Later wins in a dict comprehension, so the loose one silently replaced the strict one and
  the strict fixture's assertions — a risk band, three boolean flags, a typology set — stopped being
  evaluated. The hard-fail case the suite was built around could no longer fail.

Neither raises. Both produce a grader that reports conformance it never checked.

**Write it right the first time:** key by the whole identifier. Where a short form is wanted for
display, derive it at render time and never in a dict key. And when building a map from a collection
that may contain duplicate keys, assert the sizes match — `assert len(fx) == len(FIXTURES)` would
have caught this at import:

```python
fx = {f.alert_id: f for f in FIXTURES}
assert len(fx) == len(FIXTURES), "two fixtures share an alert_id — one is being discarded"
```

Deliberate duplicates (an isolation control pointing at the same item) are legitimate — which is why
the map needs a composite key, `(alert_id, fixture_id)`, rather than a de-duplication.

---

## 42. Counting rows is not a completion signal, and not a progress display either

The completion half is well known and usually implemented: a run is complete when an identifier
appears that did not exist before the work was submitted — never when a count reaches a target,
because a stray row from an earlier attempt satisfies a target that nothing achieved.

The **display** half gets missed, and it misleads the operator watching the run:

```python
per_alert[alert] += 1                       # monotonic sighting counter
print(f"{per_alert[alert]}/3")              # hardcoded denominator
EXPECTED = 48                               # assumes 16 items x 3 passes
```

Three defects, compounding. The counter increments once per record ever observed and never
decrements, so records deleted underneath a running watcher still count — a watcher left running
across a table clear reported `17/3` for an item the table held **twice**. The denominator was a
literal that no longer matched the run shape. And every ETA derived from the wrong `EXPECTED`
wandered — 0 minutes and 118 minutes on the same 40-minute run.

Fixes, all cheap: recompute counts from the current scan every poll so deletions are reflected;
derive the denominator from the run's actual parameters and **import the work-item set from the
runner** so watcher and runner cannot disagree; count per (item, **model**) in any matrix, since a
per-item count merges the arms and reads as double; and compute an ETA only from records the
watcher observed **arriving**, never from ones already present when it started.

**Write it right the first time:** a progress display reads live state and reports it; it does not
accumulate. Any monotonic counter in a watcher is a bug against a mutable store.

---

## 43. When two models fail the same flag identically, the prompt is the defect

**Symptom:** a capable model fails one boolean in an otherwise correct decision. The obvious
reading is a capability limit, and the obvious response is to pin the more expensive model.

Observed on a mule-activity fixture requiring `customer_may_be_victim=False`. Both an Opus-class and
a Sonnet-class model returned the correct disposition, the correct risk band, the correct typology
and the correct escalation — and both set the flag `True`. Both had **found the discriminating
evidence** and recorded it verbatim in their red flags:

> *"The P2P desk payee was registered from recognised device dev-3310 on 2026-07-14 — 19 days before
> the fan-in"*

They located the signal, reasoned about it correctly, and then set the flag wrong in the same
direction. The prompt said a customer can be a victim *and* the activity reportable; it never said
when the flag is **false**. Two models from different capability tiers cannot independently invent
the same error from a well-specified instruction — a shared failure implicates the shared input.

**This corrupts a model comparison twice over.** The fixture scores against both arms, so it looks
like agreement rather than a suppressed question; and if it is one arm of a discriminating pair, the
pair collapses — here the paired APP-victim fixture correctly wanted `True`, so both arms returned
`True` and the pair stopped discriminating anything, for a reason that has nothing to do with either
model.

**Diagnostic:** before attributing a fixture failure to a model, check whether the reasoning trace
contains the discriminating evidence. Found-then-misapplied is a specification defect;
never-found is a capability one. They warrant opposite responses and the record already holds
enough to tell them apart.

**Write it right the first time:** every boolean in an output schema needs its **false** condition
stated, not just its true one. Write the field definition as a decision rule with both branches, and
add a golden fixture for each branch — a flag with no negative fixture is untested by construction.

---

## 44. A validation verdict wearing an action name

**Symptom:** a record field reads `AUTO_ROUTE` on decisions nobody would auto-action, and an
operator planning automation reads it as consent.

A triage agent sealed a field called `validation_route`, values `AUTO_ROUTE` / `HUMAN_REVIEW`.
Its derivation was one line:

```python
return "HUMAN_REVIEW" if self.errors else "AUTO_ROUTE"
```

That answers **"did the model's JSON parse"**. Across 119 sealed records it read `AUTO_ROUTE` on
**all 72 REJECTs**, because those 72 outputs were well formed. The field was a fail-safe on the
schema and it worked correctly. It was also, by its name, the only thing in the record that looked
like an answer to "what happens to this customer" — and nothing in the record actually answered
that. Reject-and-refer, reject-and-block, and reject-the-alert-but-let-the-customer-continue were
not merely unimplemented; they were **unexpressible**.

**The two questions are independent and both are needed.** Whether the output is well formed is a
property of the output. What is done about it is a property of the evidence, the customer's history
and the firm's risk appetite. Collapsing them means an automation switch has nothing correct to
read.

**Write it right the first time:** name a field for the question it answers. If a record will ever
drive an action, carry a separate action field, **derive it deterministically rather than letting
the model emit it** — a model that names its own consequence has become the disposition — and order
the derivation so that every gate which can route to a human is evaluated *before* the model's
recommendation is consulted. Record the reason alongside the value; a route with no stated basis
cannot be audited without re-deriving it.

Two values are worth defining even when unreachable. A block-the-transaction route is meaningless
against a retrospective alert whose activity has already settled — keep it in the taxonomy, refuse
it in the derivation, and **record why**, so it reads as a rule someone wrote rather than a branch
nobody thought of. And a route that encodes risk appetite ("close the alert, let them keep
transacting") should never be *inferred* from one alert's evidence; expose it, never derive it.

---

## 45. A control that is declared and never executed

**Symptom:** a control appears in the fixture set, in review documents and in conversation, and no
code path reads it.

An evaluation suite carried a tenant-isolation fixture whose own note called it more serious than
any wrong answer: *"Any record identifier outside tenant A appearing in the ledger fails this,
whatever the disposition was."* A second tenant existed in the seed for exactly this purpose,
because a single-tenant seed cannot distinguish "isolation works" from "there was nothing else to
read".

Grepping the two graders for `isolation`, `forbidden_sources`, `hard_fail` and `tenant_id` returned
**nothing**. The fixture declared `forbidden_sources` and `hard_fail=True`; no code consulted
either. The most security-relevant assertion in the suite had been decorative for its entire
existence, while appearing in every summary as though it had passed.

**Declared is not enforced.** A schema field that no consumer reads is documentation, and
documentation that looks like a control is worse than an absent one, because it is counted.

**Write it right the first time:** for every declared control, write the assertion that fails when
it is violated, and then **prove the assertion is non-vacuous by breaking it on purpose** — inject
a foreign identifier and confirm the check fires. Report the control separately from any score
rather than averaging it in; over-reach and a wrong risk band are not commensurable, and a ratio
that mixes them hides the one that matters.

**Report a pass with its detection surface.** "No cross-tenant identifier appeared" is worth
exactly what the control could have caught. If the foreign tenant holds four records, say so —
that is a proof about four identifiers, not a general proof of isolation.

**Test exclusivity, not membership.** The naive check — "is this identifier one the other tenant
holds" — fires on every correct run when identifiers legitimately overlap (a KYC `version` of `1`
exists for everyone). A check that cries wolf gets switched off. The provable property is that an
identifier is held by another tenant **and not by the bound one**: the set with no innocent
explanation.

---

## 46. A telemetry log line is an allowlist, and enriching it can breach the boundary

**Symptom:** none. The line looks richer and more useful, and the boundary has moved.

A deployment's logging split put usage telemetry on the cloud side — identifiers, counts, timing,
no row content, no PII — and kept narrative in an internal store. A retrieval-coverage block was
then added to the per-tool log line so a reviewer could see whether a read was complete. The block
was logged whole. It contained an index of the rows read:

```
txn-cx1-0163|2026-05-05|o|1223.00|1|1|1
```

Transaction id, date, direction and **amount**. Customer transaction values, in a log group whose
stated policy is counts and identifiers only. The change was reviewed, the line was quoted back as
evidence the instrumentation worked, and the amounts in it were not noticed. What caught it was a
pre-existing test asserting the line's exact key set — which had been failing since, and was
mistaken for unrelated breakage.

**A structured field you did not enumerate will eventually carry a payload you did not intend.**
The coverage block was a good thing to log; the index inside it was not, and nothing distinguished
them because the field was passed by reference.

**Write it right the first time:** log an explicit **allowlist** of keys, never the block. A field
added upstream and not to the allowlist is then dropped on its first deploy rather than racing the
next schema change into a log group. Signal absence positively — `index_present: true` with counts
tells a reader the index existed and is not logged here, which is different from an index that was
never built, and only one of those is a retrieval gap. And assert the property rather than the
shape: feed the emitter a block containing an amount, an id, a date and an address, and assert none
of them survive.

---

## 47. A check harness that counts instead of raising is invisible to the test runner

**Symptom:** a suite reports green while containing failing checks. The count of "tests" is a count
of functions, not of assertions.

A repository's oldest suites used a helper of this shape:

```python
def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond: PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL  {label}")
```

Run directly, the module's `main()` consults `FAILED` and exits non-zero — correct. Run under
pytest, `main()` is never called, and a `test_*` function that does not raise is a **pass**. Three
suites totalling ~20 pytest "tests" and several hundred checks — including the entire scope-injection
security suite — could not fail under the runner everyone actually used.

Demonstrated by injecting one deliberately false check:

```
pytest      -> 9 passed
standalone  -> 122 passed, 1 failed      # same file, same moment
```

Every "N passed" reported from that runner was weaker evidence than it appeared, for months.

**Write it right the first time:** assertions raise. If a counting harness must exist — and there
are reasons to want one, such as reporting every failure in a run rather than stopping at the first
— then it needs a terminal assertion the runner can see:

```python
def test_zz_every_check_in_this_module_passed() -> None:
    assert FAILED == 0, f"{FAILED} check(s) failed; run this file directly to see which"
```

Defined last, because runners execute in definition order. **Then prove it bites** by injecting a
false check, exactly as above — a guard nobody has seen fail is another §45.

---

## 48. A synthetic ledger that cannot exist still produces confident triage

**Symptom:** none from the agent. It reasons fluently, cites evidence correctly, and reaches a
defensible disposition on financial data that could not have occurred.

An analyst reviewing a triage noticed the customer made **fourteen consecutive monthly payments
before a single credit ever arrived** — a running balance reaching −4,200 before the first inbound.
Four of twenty-two customers had ledgers in this state. Nothing had ever added the transactions up:
no rule fired, no fixture asserted it, and 119 sealed evaluation records had been graded on it. One
of the affected customers backed a fixture **both models failed**.

The agent was not obviously wrong. It had even flagged the adjacent symptom — *"the customer's
baseline references purchases 'since 2022' but those transactions are not in the partition"* —
without concluding the ledger was impossible. **An agent asked to judge source of funds on a ledger
that cannot exist is being tested on nothing, and will still produce a confident answer.**

**Two things this teaches about the invariant itself.**

*The grain is not the customer.* A first pass summed one balance per customer and would have
reported false violations on every customer holding more than one currency — the same
cross-unit arithmetic the rest of the system forbids. The unit of solvency is
`(entity, currency, currency_basis)`. Each balances on its own.

*Not every unit is a balance.* The same check then flagged fiat legs whose `txn_type` values were
`CARD_FEE` and `ITO_PURCHASE` — card-funded settlements, where the platform holds no balance and
"negative" measures nothing. Excluding them is a **policy decision** and belongs in the code as
one, named and listed in the output, not silently dropped.

**Write it right the first time:** any synthetic financial fixture needs an invariant asserting it
could have happened — no unit-ledger spends value it was never given, fees included in the
arithmetic. Where a genuine credit facility exists, it carries an explicit record; an unexplained
negative is a defect, not a feature. And when repairing one, check that the repair falls **outside
the alert's own window**, or you have changed the thing under test while fixing its background.

---

## 49. "Aggregated" is a third state between read and unread

**Symptom:** a retrieval-coverage view reports a large unread remainder on a run that read
everything, or reports full coverage on a run that examined no individual record.

A tier-1 summary tool reads an entire partition and returns aggregates plus a single synthetic
identifier — `txn-summary:212-rows:…:<digest>` — and no row ids. A reconciliation comparing
identifiers-read against identifiers-on-file therefore had two available answers and both were
wrong. Counting the digest as one record read reported **211 of 212 outstanding** on a run that
summarised all of them. Counting the rows as read would have hidden that no individual transaction
was ever examined, and the agent cannot quote one.

Across 119 records this mis-stated **1,239 rows** as unread.

The envelope already named the honest answer in its own `delivery` field: *"aggregated, not
itemised"*. **Report it as its own state**, alongside read and unread, never merged into either. A
reviewer asking "did it look at everything" and a reviewer asking "can it cite a transaction" are
asking different questions, and one number cannot answer both.

Two details that matter in the implementation. **Counts, not identifiers** — an aggregate covers a
*number* of rows, not a nameable set; a windowed summary over 31 of 212 leaves 181 genuinely
unread and cannot say which 181, and asserting ids there invents precision the payload never had.
And **take the widest aggregate, not the first**: a run commonly summarises the whole partition and
then drills into a window, so keying on call order reports a large remainder purely because the
narrow call came first.

---

## 50. A derived session id that omits the endpoint collides when two arms run

**Symptom:** two model arms triaging the same work item interleave into one retrieval trail, share
one warm container, and overwrite each other's scope binding — with no error anywhere.

A trigger derived its session id as `sha256(tenant | customer | item | salt)`. That is correct and
deliberate: the same item reprocessed reuses its session, so a redrive does not provision a second
microVM for work already in flight.

It omits the endpoint. An endpoint selects a runtime version and a version pins the model, so an
A/B comparison running two models against one item derived **one** session id. Three things then
collided, none of them loudly: both arms landed on the same warm container and starved each other;
the second arm's scope binding overwrote the first's, harmless only because both bound identical
scope; and every `tool_call` line carries the session id, so the two arms produced a single
interleaved trail that could not be split by model — which is precisely what a per-model retrieval
comparison needs.

**Write it right the first time:** the derivation must include every dimension that distinguishes
one execution from another for the purpose you are measuring. Adding the endpoint costs nothing a
redrive cares about — reuse is preserved *per endpoint*, which is the correct grain — and it is
what allows two models to hold the same item concurrently instead of taking turns.

**Then check the platform can afford the concurrency you just unlocked.** Session namespacing is
necessary and not sufficient: with a dispatch function fronting the tools, every in-flight tool call
holds **two** concurrent executions (the dispatcher blocking on `RequestResponse`, plus the tool),
so one arm's five-tool opening batch peaks near eleven. Against a restricted account limit, the
excess throttles the dispatcher, which surfaces to the agent as an HTTP 500 from the Gateway that
the MCP client does not retry — so the failure mode is not an error but a **silent stall**: one arm
sat mute for nine minutes per item while the other ran on normally. Measure the limit in pre-flight
and serialise when it cannot be afforded, rather than assuming either way.

---

## 51. A subagent's "pre-existing, not caused by me" needs verifying

**Symptom:** a delegated task reports success with a caveat that two unrelated tests were already
failing. The caveat is precise, plausible, and load-bearing.

Two agents in one session reported failing tests as pre-existing. One supported the claim with a
bisect. **Both failures had been introduced earlier in that same session** — one by a change to a
session-id derivation, the other by adding a field to a log line that a key-set assertion guarded.
The second was a genuine privacy breach (§46) sitting behind a "not mine" label.

The failure mode is structural rather than dishonest. An agent scoped to a subset of the repository
sees a red test it did not touch, has no view of the change that broke it, and reports accurately
from where it stands. The claim is about causation and the evidence available to it is only
correlation.

**Write it right the first time:** treat "pre-existing" as a hypothesis to check, not a result to
accept. The check is cheap — read the failing assertion and ask what it guards, then look for a
change in this session that touches that thing. And record a **verified baseline** before
delegating, so "already failing" is a fact both sides can point at rather than an inference.

More generally: verify a delegated result against the repository rather than against the report.
Every substantive claim in this session that was checked independently was worth checking — several
were right in ways the report understated, and a few were wrong in ways that mattered.

---

## 52. A bundle tool returns one envelope for many sources, and every reader keyed on the old shape breaks

**Symptom:** a run completes, validates, emits full metering, returns HTTP 200 and writes **no
audit record at all**. One log line is the entire signal.

Collapsing the always-called reads into a single "case context" tool is a large win — one round
trip instead of eight, and a measurable drop in tokens and latency. It also emits a **different
envelope shape**: the result's own `source` is the bundle's name, and the real sources are nested
as sections, each carrying its own `source`.

Every consumer written against the unbundled shape then silently finds nothing:

```python
# Reader written before the bundle existed
if body.get("source") == "Alerts" and isinstance(body.get("data"), dict):
    return body["data"]          # never matches a bundled result
```

What that cost, concretely: the tenant id that partitions the audit record was read from the
`Alerts` result. A model that satisfied its alert read *via the bundle* never called `get_alert`
directly, so the extractor returned `{}`, the tenant was empty, and sealing skipped **both** the
queryable row and the immutable evidence object. The triage itself was fine — a correct
disposition, a complete retrieval ledger, no unavailable sources. Nothing downstream noticed,
because everything downstream reads the record that was never written.

The blast radius is wider than sealing. Anything keyed on the unbundled shape is affected at once:
the routing gate that reads `detection_mode` from the alert row decided blind, and reached the
right answer only because that particular gate was unreachable on the data in play.

**When you introduce a bundle:**

1. Enumerate every reader of the old shape — audit sealing, routing gates, ledger construction,
   evaluation harnesses, the review UI — and teach each one both shapes.
2. Key on a **structural** marker (`bundle: true` plus a section carrying its own `source`), never
   on section names, so the next bundle needs no further edits.
3. Expand the bundle into one ledger entry per section, or `records_read` groups every identifier
   under a source name that is not a table and the deterministic sweep re-reads sources it was
   just handed.
4. Assert against a **real** bundled response. A 197-test offline suite passed on the build that
   sealed nothing, because no fixture reproduced a bundle-shaped message stream.

**And never read transport success as evidence a record landed.** A 200 from the trigger and an
empty failure list from the queue were both true on the run that wrote nothing.

---

## 53. A warm session can keep serving the runtime version it started on

**Symptom:** you deploy a fix, re-run the same case, and it reproduces the original failure byte
for byte. The artifact in the account demonstrably contains the fix. Both endpoints assert at the
new version.

This is §50's mechanism arriving with a different consequence. A derived session id routes to a
warm container (§7 for why the derivation is server-side). Re-invoking with the *same* derived id
shortly after a deploy can land on the microVM that was already running — executing the
**previous** version's code. The endpoint reports the new version; the container is older than the
endpoint.

Changing an incidental field does nothing: the id derives from
`(tenant, customer, item, qualifier, salt)`, and an SQS `messageId` is not in it. The second run in
this instance derived an identical session id to the pre-fix run and failed identically. Changing
the endpoint qualifier produced a different id, a cold container, and an immediate pass.

**MECHANISM INFERRED, NOT CONFIRMED.** The evidence chain is strong — identical session id, an
identical failure with the fix provably deployed, success the moment the qualifier changed — but
nobody inspected which artifact the warm container was executing, and this was not checked against
AgentCore documentation. Treat it as a working explanation, not a documented behaviour, and verify
before designing around it.

The operational rule holds either way and does not depend on the mechanism being right:

> After a deploy, do not trust a run whose session id predates it.

Force a fresh session by varying something in the hash material — a different endpoint qualifier is
cheapest — or wait the session out. This is most dangerous during A/B and consistency work, where
repeated passes over one item share a session by construction.

---

## 54. Scope injection is keyed per tool NAME, so a new tool is unscoped until it is named

**Symptom:** one source returns `UNAVAILABLE` while every other source on the same run is fine.

An interceptor that injects `alert_id` or `rule_id` typically holds explicit sets of tool names
that receive each binding:

```python
ALERT_BOUND_TOOLS = frozenset({"get_alert", "get_case_context"})
RULE_BOUND_TOOLS  = frozenset({"get_rule_logic", "get_case_context"})
```

A newly added tool needing alert scope is not in the set, receives no `alert_id`, and returns
`UNAVAILABLE`. Downstream that reads as "this source could not be read", which an analyst may take
as an infrastructure blip rather than a wiring defect — and §3 of `scoped-retrieval.md` is doing
its job, correctly reporting a source it genuinely could not read.

A bundle belongs in **every** set whose sections it serves. A tool scoped only by tenant and
customer belongs in **none**, and that absence deserves a comment saying it is deliberate — the
next reader cannot distinguish correct wiring from an omission.

This is the same failure family as §28: something added in one place and not in the other five.

---

## 55. A resume-skip that tests existence measures nothing on the second pass

**Symptom:** a repeat run over an already-measured set completes in seconds, skips every
invocation, and reports success — having measured nothing new.

Resume logic on an expensive evaluation is right (§34 on idempotence). Written as an existence
test, it silently caps the sample at one:

```python
# WRONG — sees pass 1 and skips every later pass
if any(r.model == model and r.prompt_version == pv for r in existing):
    skip()

# RIGHT — "do I already have at least this many?"
have = sum(1 for r in existing if r.model == model and r.prompt_version == pv)
if have >= attempt:          # attempt is the 1-based pass being tried
    skip()
```

The counting form is also what makes a multi-pass run cheap: passes already completed are reused
rather than re-paid for, and only the missing ones are invoked.

**The measurement this protects is not optional.** A single run per (case, model) cannot disagree
with itself, so a report reading "disposition unstable on 0/14 items" is arithmetically guaranteed
and means nothing. Two findings in this project rested on n=1 and did not survive n=2: one of three
apparent model divergences turned out to be a single model's own instability on that item, and a
bias probe previously written up as the sharpest discriminator available was shown to sit entirely
inside per-model noise — the between-item effect was 1.0 risk point against a within-cell spread of
10 on one model.

Before any decision rests on a cross-model difference, re-measure it.

---

## 56. Tearing down an audit store: two protections, and a report that lies by omission

An evidence store is built to resist deletion, and a teardown meets that resistance twice:

- **DynamoDB deletion protection** — `DeleteTable` fails with `ValidationException: Resource cannot
  be deleted as it is currently protected against deletion`. Disable the flag, then delete.
- **S3 Object Lock in GOVERNANCE mode** — object versions refuse deletion without
  `s3:BypassGovernanceRetention`.

Both are the control working (`control-stack.md`). Neither belongs in a teardown script by
default — the right shape is to RETAIN both, print what survived, and give the operator the exact
commands.

**The trap is the report.** A teardown that prints "retained deliberately:" and then lists nothing
is indistinguishable from "everything was removed". Here the retained-bucket query matched the
*table* prefix rather than the *stack* prefix, so it matched nothing, and every teardown printed an
empty section. Seven cycles later the account held **fourteen orphaned buckets** — seven evidence,
seven access-log — that nobody had been told about, one of them holding live Object-Locked evidence.

```python
# An empty section must never be silent
if not retained:
    print("none found — if you expected retained resources, these patterns have")
    print("drifted from the stack; check by hand before assuming the data is gone")
```

Derive resource-name patterns from the stack name rather than writing them out, and **archive
before destroying**: export the tables and evidence objects, verify the export renders in whatever
review tool the firm actually uses, and only then tear down. An export nobody has opened is a
backup nobody has restored.

---

## Diagnostic quick reference

| Symptom | Look at first |
|---|---|
| `AccessDeniedException` on model call | Inference-profile ARN missing from IAM (§1) |
| `CreateAgentRuntime` fails on new account | Service-linked role principals (§2) |
| Instant `ConcurrencyException` on every call | Module-level agent object, wedged (§3) |
| Runtime `READY` but invisible to the app | Orphaned by failed post-deploy step (§4) |
| `Runtime initialization time exceeded`, or `RuntimeClientError` / HTTP 424 | Your container returned 4xx/5xx — import-time crash, unsubstituted placeholder, bad config (§5) |
| `ModuleNotFoundError` for a package listed in `requirements.txt` | Direct code deployment never ran `pip`; vendor the wheels (§27) |
| `CREATE_FAILED`: artifact contains Python cache files | `__pycache__` shipped in the asset — often byte-compiled by your own test run (§27) |
| Agent's INFO logs missing but its errors arrive; Lambdas log fine | Root logger has a level but no handler; `logging.lastResort` is WARNING (§32) |
| Usage/metering line never appears though the projection ran | Same as above — the control ran, the evidence was dropped (§32) |
| `PutItem` fails with a bare `ClientError` while `PutObject` to the same CMK succeeds | DynamoDB needs `kms:Decrypt` on a customer managed key even to write (§35) |
| A log line reads `"reason": "ClientError"` | Log the AWS error CODE, not the exception type — they are indistinguishable otherwise (§35) |
| No log group exists at all, for everything | Missing `logs:*` on execution role (§6) |
| Logs arrive but traces never do | `aws/spans` and `/aws/vendedlogs/…` not in the IAM resources; `logs:PutResourcePolicy` missing (§6) |
| Logs Insights query returns nothing | Log group named wrong — group vs stream, missing endpoint suffix; empty result is not an error (§6) |
| HTTP 409 `RetryableConflictException` | Session still provisioning and a second operation raced it; capped retries may not absorb it (§7, §11) |
| One target's Lambda has no log group while a sibling target logs normally | Tool name not namespaced (§19), or the call was narrated (§18) |
| Cold start on every request | `runtimeSessionId` not passed (§7) |
| Throttling at low volume | `max_tokens` unset, over-reserving (§8) |
| `ThrottlingException: Too many tokens per day` | A separate DAILY quota, often `Adjustable: false`; RPM and TPM are fine (§38) |
| A one-token `converse` throttles identically to a large one | Structural, not payload — daily budget exhausted (§38) |
| `ReadTimeoutError` on a synchronous agent invoke, far inside the caller's own timeout | Control-plane botocore `Config` shared with a data-plane client (§37) |
| Several `run_id`s per inbound message, ~`read_timeout` apart | Client retry policy started duplicate agents on one session (§37) |
| Latency an order of magnitude above median on an otherwise normal record | Duplicates racing one microVM and starving each other; discard the record (§37) |
| Whole batch redelivers when one item failed | `read_timeout` above the function timeout — killed before it could return `batchItemFailures` (§37) |
| `MaxTokensReachedException`, surfacing as a runtime 500 | With adaptive thinking, `maxTokens` bounds reasoning + answer; cap tuned to the previous model (§39) |
| A model swap fails every run while the previous model was fine | Same as above — check the cap against the new model's output, not the old one's (§39, §24) |
| `NotVisible=1` survives purge, visibility change and retention change | In-flight messages can only be outlasted; take the consumer out of the path (§40) |
| A record exists for work nobody submitted | Phantom redelivery of a message stranded by an earlier failed run (§40) |
| Fixture count does not match the fixture list | Selection or mapping keyed on an id suffix (§41) |
| A fixture passes on a run that visibly violates it | Two fixtures share a key; the looser one silently replaced the stricter (§41) |
| A progress display shows more passes than the table holds | Monotonic sighting counter against a mutable store (§42) |
| A record's route field reads `AUTO_ROUTE` on decisions nobody would auto-action | It answers "did the JSON parse", not "what happens" — two fields needed (§44) |
| A declared control never appears in any failure | Grep the graders for the field name; declared is not enforced (§45) |
| A telemetry line grew a structured field and looks more useful | Enumerate the keys logged; a nested block carries payloads you did not intend (§46) |
| A suite reports green and you cannot name the assertion that would fail | Counting harness, invisible to the runner — inject a false check and compare (§47) |
| An agent reasons fluently about financial history nobody added up | Assert per `(entity, currency, basis)` that no ledger spends before it is funded (§48) |
| Retrieval coverage says everything unread on a run that summarised it all | Aggregated is a third state, not a kind of unread (§49) |
| Two model arms on one item interleave, stall, or overwrite each other silently | Session id omits the endpoint; and check the concurrency it unlocks is affordable (§50) |
| A subagent reports failing tests as pre-existing | A hypothesis, not a result — check what the assertion guards (§51) |
| Two different models fail the SAME boolean the same way | Prompt defect, not capability — check whether the trace found the evidence (§43) |
| Throttling that `EstimatedTPMQuotaUsage` cannot account for | That metric excludes the up-front `max_tokens` reservation; output also burns down at a multiplier (§8) |
| Throttling with near-zero token usage | Structural quota — read TPM/TPD first; the model may have no RPM quota to check (§10) |
| Availability API reads AVAILABLE/AUTHORIZED but `InvokeModel` returns `AccessDeniedException` | Necessary not sufficient; also check the one-time use-case form (§10) |
| Browser shows "Network Error" | Gateway 504 without CORS headers (§11) |
| Resources in the wrong region | `CDK_DEFAULT_REGION` clobbered (§12) |
| Cost shows zero or an implausible value | Field not projected; client fallback rate (§13) |
| `ValidationException: ... tool use in streaming mode` | Model lacks streaming tool use (§15) |
| `Model produced invalid sequence as part of ToolUse` | Prompt authored for a different vendor (§16), or model too weak for the schema (§17) |
| Agent claims success but no record exists | Narrated tool call; check for a missing log group (§18), or an unresolved tool name (§19) |
| Record exists but zero Lambda invocations in the run window | Another actor wrote it — not attributable to this run (§18) |
| `MCPClientInitializationError: unhandled errors in a TaskGroup` | Three candidates against an `AWS_IAM` Gateway: unsigned, `auth=` on a deprecated parameter, or a sync `async_auth_flow` (§28) |
| Agent gets an empty tool catalogue, or a filter that stops filtering | Allow-list regex written for `search()` but applied with `match()` (§19) |
| Interceptor refuses requests with an EMPTY tool name | Handler read the flat `event["body"]`; the envelope is nested (§29) |
| Gateway 500 on its own interceptor's output | Interceptor echoed the inbound `headers` back; omit the key (§29) |
| Interceptor: `no session id on the request`, but the invoke had one | The Gateway does not propagate the runtime session id — the agent must send it (§30) |
| `-32602 Unknown tool: <bare name>` | Tool name needs the `target___tool` prefix (§19) |
| A content/PII control fires on an otherwise normal run | Check the predicate before the data — a duration validated as a count (§33) |
| Structured output unparseable though the response looks perfect | `AgentResult.message` is a dict; `str()` of it is a repr. Formatting-dependent, so it reads as flakiness (§34) |
| Tool/retrieval ledger empty on runs where tools ran | `getattr(result, "tool_results", [])` — no such attribute; build from the transcript (§34) |
| Identifier field reads `unknown` / `n/a` / empty | Tool error collapsed into a placeholder (§20) |
| `AccessDenied` on a data action from the agent's own role | Possibly a breach reporting itself — find the calling line before writing the policy (§31) |
| Privacy projection passes, yet reasoning is in the provider's logs | `debug=True` / the framework's default callback handler stream to stdout, around the gate (§36) |
| An audit store reads 0 rows and the pipeline looks broken | The record write may simply be unimplemented — check before debugging retrieval (§36) |
| Model calls a tool the prompt forbids | Access enforced by prompt, not by the offered tool list (§21) |
| Two records per run, different IDs | Model and deterministic layer both writing (§22) |
| A policy denial read as a success by the client | Denial arrives as a JSON-RPC **success** with `result.isError: true`, no `error` object — check `isError`, not the error code (§23) |
| `Tool Execution Denied ... [No policy applies to the request (denied by default).]` | Nothing matched; default-deny fired. This is *not* evidence your rule fired (§23) |
| `-32002` on a Gateway call | VPC endpoint policy rejection, a different control — not Cedar (§23) |
| Policy probe passed, control does not hold in production | Probe ran inside the 15-minute access-policy cache window (§23) |
| Request above a policy threshold still succeeds | Cedar condition never matched — `ACTIVE` + `ENFORCE` is not proof (§23) |
| Output varies run to run on an identical request | Sampling parameters unset, or expected to be deterministic — there is no seed (§24) |
| Behaviour changed after a model-ID swap, prompt unchanged | The new model's default sampling regime applied (§24, with §16 and §17) |
| Prefill returns a hard 400, or structured output stops parsing, after a version bump | Generation migration, not a config edit — re-validate against the golden set (§24) |
| Case write-up reads complete, but a specialist never ran | Synthesis over a partial fan-out with no manifest (§25) |
| A fact in a synthesis traces back to no specialist | Merge step with no per-fact attribution (§25) |
| A bias probe reports divergence | Check both arms parsed — a schema failure in one arm is a parse failure, not bias (§26) |
| Missing JSON with `stopReason=max_tokens` | Token-budget truncation, graded separately from schema failure (§26, §8) |
| An enum field comes back carrying its own guidance text | Field definitions inlined in the schema — put enum values bare (§26) |
| Run completes, meters, returns 200 — and NO audit record exists | A bundled tool result broke the reader supplying the partition key (§52) |
| `seal_skipped` / empty tenant on an otherwise clean run | Alert row arrived via a bundle; extractor matches only the unbundled shape (§52) |
| A deployed fix reproduces the old failure exactly | Warm container may still be on the previous version — compare session ids across the deploy (§53) |
| One source returns `UNAVAILABLE`, every other source is fine | Tool name missing from the interceptor's per-tool binding set (§54) |
| A repeat evaluation run skips everything and reports success | Resume-skip tests existence instead of counting (§55) |
| "Unstable on 0/N", or any stability claim | Vacuous unless n>1 per (case, model) (§55) |
| `ValidationException: ... protected against deletion` | DynamoDB deletion protection; disable, then delete (§56) |
| Object versions refuse to delete | S3 Object Lock GOVERNANCE; needs `s3:BypassGovernanceRetention` (§56) |
| "Retained deliberately:" prints nothing after a teardown | Name pattern drifted; retained resources exist and are unreported (§56) |

---

## Standing rule

AgentCore is evolving quickly. Model IDs, quota codes, API shapes and enforcement dates all
change. **Verify current API surface against live documentation before generating code** —
prefer an authoritative documentation source over recalled detail, and prefer reading the
account's actual state (`list-inference-profiles`, `get-service-quota`,
`get-foundation-model-availability`) over assuming a default.

Two limits on that last habit, because reading state tells you what is *configured*, not what will
*happen*. `get-foundation-model-availability` is necessary-not-sufficient: AWS documents its field
names and enum values and never claims they predict an invoke, and all four can read
AVAILABLE/AUTHORIZED while the call returns `AccessDeniedException` (§10). A Gateway policy read
back as `ACTIVE` proves loading, not matching (§23). And a control verified inside a propagation
window has not been verified at all.

**`list-inference-profiles` is a REGION inventory, not an account entitlement — and its absence
proves nothing either.** Both directions of this were got wrong on the same deployment, hours apart,
and the pair is worth keeping:

- A sample pinned Amazon Nova because a **Price List API** query returned no Anthropic entries for
  any EU region. Pricing coverage is not availability; the conclusion "no Claude model is usable
  here" was false, and it silently shaped a prompt/model matrix that then excluded Claude on a
  premise nobody re-tested.
- Correcting it, `list-inference-profiles` was read instead — every current Claude model came back
  `ACTIVE` in the region, so the pin moved to Opus 5. The first invoke returned
  `AccessDeniedException: anthropic.claude-opus-5 is not available for this account`. `ACTIVE`
  described the profile's existence in the Region, not the account's entitlement to call it.

The only answer that settled it was a one-token `converse` against each candidate, which took
seconds and partitioned nine models cleanly into invokable and not. **Where a model choice is
load-bearing — an IAM grant, a pinned production model, an evaluation baseline — enumerate by
calling, not by listing.** A control-plane API tells you what exists; only the data plane tells you
what you may use.

**Never probe against the audit store.** Reproducing a failing write with an admin principal is the
right diagnostic (§35) and the wrong place to run it is the table that holds decision records. A
synthetic row in an evidence store is a real integrity problem even when it is deleted seconds
later: for the interval it existed the store held a record no case produced, and the delete is
itself an action the store's own design usually forbids. Seed scripts in a well-built stack refuse
to touch these tables for exactly this reason — if yours does, that refusal applies to your
debugging too. Probe a scratch table with the same key configuration, or assert on the IAM policy
instead of the data plane.

So where the answer matters, the proof is a real call rather than a description of the
configuration: a one-token invoke, a two-direction policy probe run after the cache expires, a
counted Lambda invocation inside the run window. Reading configuration is how you form the
hypothesis; making the call is how you close it.
