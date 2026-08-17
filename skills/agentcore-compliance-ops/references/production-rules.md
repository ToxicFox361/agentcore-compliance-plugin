# Production rules for AgentCore agents

Empirical rules from building and debugging a multi-tenant AgentCore platform end to end.
Every entry here corresponds to a defect that shipped, a failure that cost real debugging
time, or a constraint that only appears under load or on a fresh account. They are ordered
by how much time they cost to discover.

Read this before writing or reviewing AgentCore infrastructure. Most of these fail silently
or with an error that points somewhere other than the cause.

If you arrive with a symptom rather than a topic, start from the **Diagnostic quick reference** at
the end and follow the section it names. If you arrive with a topic:

| Theme | Rules |
|---|---|
| IAM and account setup | §1 inference-profile ARNs · §2 service-linked roles · §6 log permissions · §10 zero quota on new accounts |
| Runtime lifecycle and deployment | §3 module-level agent object · §4 persist before verify · §5 placeholder substitution · §12 CDK region · §14 concurrent CDK |
| Requests, quotas and timeouts | §7 session IDs · §8 max output tokens · §11 fail inside the caller's window |
| Models and prompts | §15 streaming tool use · §16 vendor-coupled prompts · §17 capability tiers · §24 inference parameters |
| Cost | §9 regional pricing · §13 surfacing computed cost |
| Tools, writes and authorization | §18 narrated actions · §19 tool namespacing · §20 placeholder tool errors · §21 prompts are not access control · §22 duplicate write paths · §23 policies that never match |
| Multi-agent workflows | §25 partial evidence sets in a synthesis |

---

## 1. Inference-profile IAM needs BOTH resource forms

**Symptom:** `AccessDeniedException` on the first model call, with an execution role that
visibly grants `bedrock:InvokeModel`.

Modern model IDs are inference profiles, not bare foundation models — anything prefixed
`eu.`, `us.`, `apac.` or `global.`. Invoking one requires permission on *two different
resource shapes*, and granting only the familiar one fails:

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

**Why it hides:** established accounts already have the roles, so this only ever fails on a
brand-new account — typically the first customer deployment or a fresh dev environment.

Fastest unblock when it bites: create them once with
`aws iam create-service-linked-role --aws-service-name <principal>`.

---

## 3. Never build the agent object at module scope

**Symptom:** the first request fails for some unrelated reason, and then *every* subsequent
request fails instantly with `ConcurrencyException: Agent is already processing a request`.
The runtime is wedged permanently and only a redeploy clears it.

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
`RuntimeClientError: Runtime initialization time exceeded`, which reads like a cold-start or
capacity problem and is neither.

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
actions=["logs:CreateLogGroup", "logs:CreateLogStream",
         "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
resources=[f"arn:aws:logs:{region}:*:log-group:/aws/bedrock-agentcore/*"],
```

Runtime logs appear at `/aws/bedrock-agentcore/runtimes/<runtime-id>-<endpoint>-DEFAULT`.

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

Constraints: minimum **33 characters** (a SHA-256 hex digest is 64, comfortably over).
Resolve `tenant_id` from the resource record, never from the request body. Where the security
bar is high, use a distinct IAM principal per tenant so IAM itself enforces the scoping.

---

## 8. Always set max output tokens explicitly

Quota is reserved at request start as `input_tokens + max_tokens`. Left unset, `max_tokens`
defaults to the model's maximum — tens of thousands of tokens — so each request reserves far
more than it uses and you throttle at a small fraction of apparent capacity.

Frameworks generally do not set it for you. Set it on the model object, sized to the expected
response.

Corollary for diagnosis: before assuming quota exhaustion, check
`InputTokenCount`/`OutputTokenCount` in the `AWS/Bedrock` CloudWatch namespace. Heavy
`InvocationThrottles` against near-zero token consumption means the limit is structural, not
usage-driven.

---

## 9. Model pricing is regional — never use published figures

Per-token rates differ by region. Aggregator sites and blog posts almost always quote
`us-east-1`. Hardcoding those into cost tracking produces numbers that are silently wrong.

In one measured case every model checked differed from the widely published figure, and one
model's output/input ratio differed by 2× between regions — so even relative reasoning
transferred badly.

Query the Price List API for the deployment region:

```bash
aws pricing get-products --service-code AmazonBedrock --region us-east-1 \
  --filters Type=TERM_MATCH,Field=regionCode,Value=<deployment-region> \
            Type=TERM_MATCH,Field=model,Value="<Model Name>"
```

Filter out `batch=Yes` and the `flex`/`priority` tiers unless you mean them. Get exact model
names from `aws pricing get-attribute-values --service-code AmazonBedrock --attribute-name model`.

Record the model ID on every usage event. Without it, per-model rates cannot be applied and a
mixed-model tenant is billed at whatever single rate was hardcoded. Strip the geographic prefix
before rate lookup so a prefix change does not silently zero the cost.

---

## 10. Brand-new AWS accounts have zero Bedrock quota

A freshly created account can report `0.0` for **every** Bedrock quota — tokens per day,
tokens per minute, and requests per minute — while model access shows `AUTHORIZED` and
`entitlementAvailability: AVAILABLE`. Invocations fail with
`ThrottlingException: Too many tokens per day`, which misleadingly implies consumption.

Diagnostic sequence:
1. `aws service-quotas list-service-quotas --service-code bedrock` — if RPM is also `0`, it is
   structural. You cannot over-consume into a requests-per-minute of zero.
2. Compare against an established account in the same org. Quotas are **per-account** and are
   never inherited from an organization's management account.
3. Check CloudWatch token counts. Near-zero consumption plus many throttles confirms gating.

The blocking quotas are frequently marked `Adjustable: False`, so the API path cannot raise
them. They populate on account verification.

**Practical consequence:** budget for this in delivery plans. If a demo or pilot is time-boxed,
deploy into an account with established quota rather than a fresh one. A Service Quotas
*template* associated with the organization will apply quotas to newly created accounts
automatically — worth configuring before you need it.

---

## 11. Fail fast enough to fit the caller's timeout

API Gateway REST integrations time out at a hard **29 seconds**. Default SDK retry policies
against a throttled model can spend well over two minutes before returning. The gateway gives
up first and returns a 504 — and because that response carries no CORS headers, a browser
reports it as a generic network error. Three layers of indirection between the user and the
actual cause.

Cap retries so failures surface inside the caller's window:

```python
BEDROCK_CLIENT_CONFIG = BotocoreConfig(
    retries={"max_attempts": 2, "mode": "standard"},
    connect_timeout=5,
    read_timeout=15,   # per-chunk when streaming, not per-request
)
```

Handle model errors as data rather than letting them escape as unhandled 500s — an operator
needs to see `ThrottlingException`, not `Internal Server Error`.

Where genuinely long work is unavoidable, make the API asynchronous (accept, return a handle,
poll) instead of stretching a synchronous request toward a limit you do not control.

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
*"Other CLIs are currently reading from cdk.out."* Use a separate synth directory per
concurrent operation:

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

A model can pass a `converse` tool-call test and still fail `converse_stream` with tools —
these are two different capability surfaces, and Bedrock does not advertise support for one as
support for the other. Verified: `eu.mistral.pixtral-large-2502-v1:0` does tool use correctly
via Converse and fails outright under ConverseStream. Amazon Nova models (`nova-pro`,
`nova-2-lite`, `nova-lite`) support both.

Most agent frameworks stream by default, so a non-streaming smoke test proves the wrong thing
and passes anyway. Verify against the exact API the application calls:

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

A prompt authored for one vendor's model can produce malformed tool-call sequences on another,
even with identical tools and identical task. Verified: a Claude-authored claims-processing
prompt caused `eu.amazon.nova-pro-v1:0` to emit `<thinking>` narration followed by an invalid
tool-use block. Same prompt, same tools, different vendor — the failure is in the prompt's
assumptions about how the model expresses reasoning around a tool call, not in the tools
themselves.

Treat system prompts as vendor-coupled artefacts, not portable configuration. Swapping model
families is a prompt change, not a config change — budget for re-testing every tool-calling
path, not just the happy path, whenever the model ID changes.

---

## 17. Capability tiers do not transfer across vendors

**Symptom:** a model loops on one schema field then emits an invalid tool sequence.

A "use a cheap fast model for the classification step" design does not survive a vendor swap.
Verified: a dual-agent sample used a capable model for drafting and a cheap model for
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
`guardrails.md` Layer 5 (citation grounding: every asserted action must trace to a tool result and
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
        resolved[bare] = tool.name
    return resolved

names = resolve_tool_names(session)
session.call_tool(names["create_claim"], arguments={...})
```

Fail loudly on a missing key rather than falling back to the bare name — a `KeyError` at startup is
cheaper than a write path that silently never fires.

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

This is the same principle as `guardrails.md` Layer 3 (no silent repair) applied to tool results
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
means the *namespaced* name; a regex is usually the more robust choice. See [MCP tools — Strands
Agents](https://strandsagents.com/docs/user-guide/concepts/tools/mcp-tools/).

Filtering the list is a client-side control: it decides what the model is offered, not what the
Gateway will accept. For a boundary the agent code cannot bypass, put a policy engine in front of
the Gateway (§23) — enforcement outside agent code is the point of it.

**Why it hides:** prompt instructions are obeyed most of the time. Occasional violation reads as
model flakiness rather than as a missing control, and no test asserts on "the model did not call a
tool it was told not to call." Least privilege belongs in the tool list, not the prompt. See
`guardrails.md` Layer 1.

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
deploy time:

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

**Diagnostic:** a policy denial reaches the MCP caller as JSON-RPC `-32002`:

```
Tool Execution Denied: Tool call not allowed due to policy enforcement
[Policy evaluation denied due to <policyId>]
```

That error names the rule that fired — seeing it is proof the condition matched. *Not* seeing it on
a request that should have been blocked means the condition never matched; it does not mean
enforcement is off.

Two further controls worth knowing when the policy is the compliance boundary:

- **`LOG_ONLY` mode** evaluates and logs decisions without enforcing them. Use it to observe what a
  new rule *would* decide before promoting it, and re-run the two-direction test after promoting.
- **`bedrock-agentcore:UpdateGateway` alone can disable enforcement** — it can flip the engine's
  mode from `ENFORCE` to `LOG_ONLY`, or detach the engine entirely, with no separate action or
  condition key protecting the field. Treat that permission as equivalent to permission to remove
  the control, and grant it accordingly.

Sources:
[Getting started with Policy in AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-getting-started.html),
[Understanding Cedar policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html),
[Policy enforcement modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-enforcement-modes.html).

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
(`guardrails.md` Layer 5), never on the model repeating itself.

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
level nobody persisted (`guardrails.md`, case-level records for multi-agent workflows). It also
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

## Diagnostic quick reference

| Symptom | Look at first |
|---|---|
| `AccessDeniedException` on model call | Inference-profile ARN missing from IAM (§1) |
| `CreateAgentRuntime` fails on new account | Service-linked role principals (§2) |
| Instant `ConcurrencyException` on every call | Module-level agent object, wedged (§3) |
| Runtime `READY` but invisible to the app | Orphaned by failed post-deploy step (§4) |
| `Runtime initialization time exceeded` | Import-time crash — unsubstituted placeholder, bad config (§5) |
| No log group exists at all, for everything | Missing `logs:*` on execution role (§6) |
| One target's Lambda has no log group while a sibling target logs normally | Tool name not namespaced (§19), or the call was narrated (§18) |
| Cold start on every request | `runtimeSessionId` not passed (§7) |
| Throttling at low volume | `max_tokens` unset, over-reserving (§8) |
| Throttling with near-zero token usage | Structural quota, check RPM (§10) |
| Browser shows "Network Error" | Gateway 504 without CORS headers (§11) |
| Resources in the wrong region | `CDK_DEFAULT_REGION` clobbered (§12) |
| Cost shows zero or an implausible value | Field not projected; client fallback rate (§13) |
| `ValidationException: ... tool use in streaming mode` | Model lacks streaming tool use (§15) |
| `Model produced invalid sequence as part of ToolUse` | Prompt authored for a different vendor (§16), or model too weak for the schema (§17) |
| Agent claims success but no record exists | Narrated tool call; check for a missing log group (§18), or an unresolved tool name (§19) |
| Record exists but zero Lambda invocations in the run window | Another actor wrote it — not attributable to this run (§18) |
| `-32602 Unknown tool: <bare name>` | Tool name needs the `target___tool` prefix (§19) |
| Identifier field reads `unknown` / `n/a` / empty | Tool error collapsed into a placeholder (§20) |
| Model calls a tool the prompt forbids | Access enforced by prompt, not by the offered tool list (§21) |
| Two records per run, different IDs | Model and deterministic layer both writing (§22) |
| `-32002 Tool Execution Denied ... policy enforcement` | A policy matched and denied; the policy ID names it (§23) |
| Request above a policy threshold still succeeds | Cedar condition never matched — `ACTIVE` + `ENFORCE` is not proof (§23) |
| Output varies run to run on an identical request | Sampling parameters unset, or expected to be deterministic — there is no seed (§24) |
| Behaviour changed after a model-ID swap, prompt unchanged | The new model's default sampling regime applied (§24, with §16 and §17) |
| Case write-up reads complete, but a specialist never ran | Synthesis over a partial fan-out with no manifest (§25) |
| A fact in a synthesis traces back to no specialist | Merge step with no per-fact attribution (§25) |

---

## Standing rule

AgentCore is evolving quickly. Model IDs, quota codes, API shapes and enforcement dates all
change. **Verify current API surface against live documentation before generating code** —
prefer an authoritative documentation source over recalled detail, and prefer reading the
account's actual state (`list-inference-profiles`, `get-service-quota`,
`get-foundation-model-availability`) over assuming a default.
