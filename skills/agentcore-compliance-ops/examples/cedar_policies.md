# Cedar policies for compliance agent tool access

Policy evaluates every tool call **at the Gateway, outside the agent's execution
boundary**. It is the control layer that survives prompt injection, model
change and framework change — the place to express anything that must not
depend on the model behaving.

**There are two independent enforcement settings, and conflating them is how a
control ends up half-on:**

- **Per policy** — `ACTIVE` (default) or `LOG_ONLY`. AWS: "Every policy in a
  policy engine has an enforcement mode of either ACTIVE or LOG_ONLY... ACTIVE
  and LOG_ONLY are treated as two isolated sets." The engine additionally reports
  which `LOG_ONLY` policies *would have flipped* the decision.
- **Per engine↔gateway attachment** — `ENFORCE` or `LOG_ONLY`.

So you can trial one rule at `LOG_ONLY` without moving the whole engine out of
`ENFORCE`. Prefer that to the read-the-logs workflow when promoting a rule: the
blast radius is one rule, every already-proven rule keeps enforcing, and the
engine tells you directly whether the new rule would have changed the outcome —
which is the actual question, and not one a log line answers on its own.

Under `ENFORCE` evaluation is default-deny, and any matching `forbid` wins.

### The Gateway role needs three IAM actions, and the safer-looking mode hides their absence

Before any of the above matters, the **Gateway execution role** must be able to
reach the policy engine. Three actions, from `policy-permissions.html`:

| Action | Resource | What it does |
|---|---|---|
| `bedrock-agentcore:GetPolicyEngine` | the policy engine ARN | Loads the engine's configuration. |
| `bedrock-agentcore:AuthorizeAction` | engine ARN **and** gateway ARN | Evaluates Cedar for one tool call. |
| `bedrock-agentcore:PartiallyAuthorizeActions` | engine ARN **and** gateway ARN | Filters `tools/list` so the catalogue the model sees reflects the policy. |

Both failure modes are silent in the direction that matters, and **which one you
get depends on the enforcement mode you chose**:

- Missing `GetPolicyEngine` "causes silent failures that only surface when
  switching to ENFORCED mode." In `LOG_ONLY` the stack deploys clean, every
  status reads healthy, and **the policy engine quietly does nothing**. The
  decision log you are reading to decide whether to promote to `ENFORCE` is the
  log of an engine that is not evaluating.
- Without all three, "all tool invocations will be denied by default even if you
  have permit policies configured" — a gateway that denies everything, which
  reads as a correctly-strict policy until someone notices the agent has never
  retrieved anything.

So: a policy that authorises nothing, or a gateway that denies everything. This
is the strongest form of the failure this file keeps circling — **a control that
fails silently in the configuration that looks safer.** The recommended
`LOG_ONLY`-first rollout is exactly the configuration in which a missing
`GetPolicyEngine` produces no symptom at all, and a clean `LOG_ONLY` period is
the evidence normally used to justify promoting to `ENFORCE`.

Deploying straight to `ENFORCE` turns it into a loud failure at Gateway
creation:

```
Access denied while calling GetPolicyEngine on Policy Engine: <engine-arn>
with Gateway role: <role-arn>
```

That is the lucky outcome. Two consequences for how you build:

1. **The gateway ARN cannot be referenced from the role in most IaC** — the
   Gateway assumes the role, so naming the gateway's ARN in a policy the Gateway
   depends on is a dependency cycle. Scope to
   `arn:<partition>:bedrock-agentcore:<region>:<account>:gateway/*`, which is the
   form AWS's own example uses for the same reason, and narrow to the gateway id
   if you split gateways per tenant. A bare `*` is not required.
2. **Attach the grants INLINE on the role. `DependsOn` is not enough** — and this
   was established by deploying, after `DependsOn` was tried and failed:

   ```
   role created                            20:21:15
   separate IAM::Policy CREATE_COMPLETE    20:21:44   <- 36s head start
   Gateway create                          20:22:20   -> AccessDenied
   ```

   CloudTrail showed the service *did* assume the role, and the same session was
   denied `kms:Decrypt` in the same second with "because no identity-based policy
   allows" — for a grant in a policy resource that had not been created yet.
   `simulate-custom-policy` confirmed the document itself allowed the action on
   that exact engine ARN. The role evaluated as though it had **no policies at
   all**: a stale view of a role that briefly existed with nothing attached.

   Put creation-time grants (the three Policy actions, and the CMK grant if the
   gateway is encrypted with a CMK) in the role resource itself — CDK
   `inlinePolicies`, CloudFormation `Policies`, Terraform `inline_policy`. The
   role then never exists in a policy-less state, so there is no stale view to
   cache. Ordering a separate policy resource cannot give you that, and the
   handler does not retry: the failure carried `SDK Attempt Count: 1`.

   Keep a `DependsOn` as well for any grant you *cannot* move inline — in CDK the
   L2's target and interceptor grants land in a generated `DefaultPolicy`, which
   in the failing deploy had not been created at all when the Gateway was.

`bedrock-agentcore:GetWorkloadAccessToken` is **not** in this set. It is needed
only when a temporal policy (§6–7) is active. Grant it — scoped to the
workload-identity directory — if and when you introduce one, or tool invocations
fail at the token-mint step with `AccessDenied`.

None of this is visible from a resource status. It is the reason the §8 probes
test an allowed path *and* a denied path: `ACTIVE` + `ENFORCE` + a healthy
engine is consistent with a role that cannot evaluate a single policy.

⚠ Policy protects only traffic that actually flows through the Gateway.
Restrict the Runtime's resource policy so only the Gateway can invoke it, or
these are decorative — see `deployment-patterns.md`.

Current references:

- Tutorial and worked policy —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-getting-started.html
- Enforcement mode on the engine↔gateway attachment (`ENFORCE` / `LOG_ONLY`) —
  https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GatewayPolicyEngineConfiguration.html
- Policy scope — principal, action and resource forms —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-scope.html
- What a denial looks like to the MCP caller —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/use-gateway-with-policy.html
- IAM permissions the Gateway role needs for policy —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
- Tool naming —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html
- Cedar structure, default-deny and evaluation semantics —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html
- Memory fine-grained access control, the best-documented worked policy set —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-gateway-fgac-policy-examples.html
- Temporal (session-aware) policies, §6–7 —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal.html
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-temporal-authoring.html
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-session-based-temporal.html

---

## 1. Get the action name right, or the policy is decorative

This is the failure that motivates everything below, because it leaves no trace.

**A Gateway namespaces every target's tools.** The name the agent sees over MCP
is not the name you gave the tool — it is:

```
<targetName>___<toolName>
```

Three underscores. A target named `case-writes` exposing `close_alert` is
presented as `case-writes___close_alert`. Write `AgentCore::Action::"close_alert"`
in a policy and it matches nothing.

**Tool identity lives in the ACTION, not the context.** `context.toolName` is
**not present in the generated Cedar schema.** That is a documented absence
rather than a documented prohibition — AWS does not say "this attribute is
invalid", it simply never populates one — so read it as "nothing ever fills this
in", which has the same consequence. A condition keyed on it is not an error; it
is a condition that is never true:

```cedar
// WRONG — context.toolName does not exist. This forbid can never fire.
forbid(principal, action, resource)
when { context.toolName == "close_alert" };
```

That policy parses. It deploys. It reports status `ACTIVE` with the engine in
`ENFORCE`. It never denies anything. **A `forbid` whose condition never matches
is indistinguishable from having no policy at all** — and every signal you would
naturally check says the control is on. `ACTIVE` and `ENFORCE` prove the policy
is *loaded*, not that it *fires*. The only proof is §8.

**Tool input is addressed as `context.input.<field>`**, matching the tool's
declared `inputSchema`. Guard before you dereference — an unguarded attribute
access on a request that lacks the field is an evaluation error, not a deny:

```cedar
when {
  context has "input" &&
  context.input has "transaction_amount" &&
  context.input.transaction_amount >= 10000
}
```

(AWS's published examples write the equivalent unquoted form, `context has input
&& context.input has amount`. Both are valid Cedar.)

**Constrain the resource whenever you constrain the action.** AWS states the
requirement plainly: "When specifying one or more specific actions, you must use
specific AgentCore Gateway ARNs." A tool-specific policy with a loose resource is
rejected at create time. The wording observed in practice:

> `ValidationException`: When parsing the policy statement, a constrained action
> scope was encountered, please constrain the resource to a specific
> `AgentCore::Gateway` resource when creating tool-specific policies.

⚠ That exact string is **observed, not documented.** AWS documents the
requirement, not this sentence. Assert on the exception type and the fact of
rejection; do not pattern-match the message.

So for an action-constrained policy the specific ARN is required. Below,
`<gateway-arn>` stands for
`arn:aws:bedrock-agentcore:<region>:<account-id>:gateway/<gateway-id>`.

This forces a two-phase deployment — the Gateway must exist before you can write
a policy naming it. Deploy the Gateway, read back its ARN, then attach policy.

**And the Gateway existing is not sufficient: its TARGETS must exist too.** This
is the same failure wearing the costume of the one above, and it is worth
recognising because it accuses you of the error §1 has just spent a page warning
about. Creating the policy against a gateway whose target had not yet been
created produced:

```
unrecognized action `AgentCore::Action::"tmtools___get_chain_analytics"`
  at line 15, column 5
did you mean `AgentCore::Action::"InvokeAgent"`?
```

The names were correct. `CreatePolicy` and `UpdatePolicy` **validate the actions
in the statement against the Gateway's published capabilities** — which is why
the policy-creating principal needs `bedrock-agentcore:InvokeGateway` on the
Gateway ARN, a requirement that otherwise looks arbitrary. Tools are published by
the **target**, so a gateway with no target advertises no `<target>___<tool>`
actions at all, and every one of them is rejected as unknown. `InvokeAgent` is
suggested because it is genuinely the only action such a gateway offers.

So the ordering is three-phase, not two: **Gateway → Target → Policy.** In
CloudFormation/CDK the policy needs an explicit dependency on the *target*
resource; the gateway alone will not order it correctly, because the target and
the policy are otherwise siblings with no relationship.

The direction of this failure is worth noticing, because it is the opposite of
the `context.toolName` trap above: a policy naming an action the gateway does not
publish is **rejected loudly at create time** rather than deploying and silently
matching nothing. That is a real guard-rail — it means a typo'd or un-namespaced
tool name in an *action* cannot deploy as decorative policy. It does not extend
to conditions, which is why §8 still exists.

**The docs contradict each other on resource scope. Know it rather than trust
one page.** `policy-getting-started.html` troubleshooting says wildcard
resources are rejected outright. `policy-scope.html` offers
`forbid(principal, action, resource);` as valid — "Blocks all actions" — permits
`resource is AgentCore::Gateway` for any-action policies, and also carries a
malformed example mixing a type check with an ARN
(`resource is AgentCore::Gateway::"arn:..."`). Both readings cannot be right, and
picking one for you would be guessing on your behalf.

Where this file lands: every action-constrained example (§2, §4, §5, §6, §7) uses
a specific ARN, which satisfies either reading. §3's unconstrained-action policy
relies on the `policy-scope.html` reading that a loose resource is legal there.
Since the thesis of this whole file is that an inert policy is invisible, resolve
it empirically — deploy, confirm the create call was **accepted**, then run the
§8 probes. Acceptance is a fact about your account and Region; the doc page you
happened to read is not.

**Read the names off the Gateway; do not derive them from your own config.**
Target names get normalised, and a rename ships a policy that silently stops
matching:

```python
# The Gateway is the authority on what the model is offered.
tools = mcp_client.list_tools_sync()
for t in tools:
    print(t.tool_name)      # e.g. case-writes___close_alert
```

Pin the output into a test. A tool name changing under a policy is exactly the
change that produces a live, ACTIVE, enforcing, useless control.

---

## 2. Read-only agents

The single most valuable policy. An investigation agent has no business writing.

```cedar
// Deny every mutating tool outright, regardless of principal.
// Names are as the Gateway presents them — target prefix included.
forbid(
  principal,
  action in [
    AgentCore::Action::"case-writes___close_alert",
    AgentCore::Action::"case-writes___file_report",
    AgentCore::Action::"case-writes___update_risk_rating",
    AgentCore::Action::"rules-admin___deploy_rule",
    AgentCore::Action::"account-ops___freeze_account"
  ],
  resource == AgentCore::Gateway::"<gateway-arn>"
);
```

`forbid` wins over any `permit`, so this holds even if a later policy is
broader than intended. Belt and braces on top of not exposing the tools — and
note the ordering of defences: the tool list the model is *offered* is the
primary control (`examples/agent_template.py`), IAM is the outer boundary
(`examples/iam_policies.py`), and this is the layer that catches the case where
someone widens either one. A prompt instructing the model not to call a write
tool is not part of that stack at all.

---

## 3. Tenant scoping on tool arguments

```cedar
// A tool call may only carry the tenant ID bound to the caller's identity.
// Action is unconstrained here, so the resource may be too.
forbid(
  principal,
  action,
  resource
) unless {
  principal is AgentCore::OAuthUser &&
  principal.hasTag("tenant_id") &&
  context has "input" &&
  context.input has "tenant_id" &&
  context.input.tenant_id == principal.getTag("tenant_id")
};
```

Claims arrive as principal *tags*, read with `hasTag` / `getTag` — not as bare
attributes like `principal.tenant_id`. Guard with `hasTag` first; an absent tag
is an evaluation error otherwise.

Defence in depth behind server-derived session IDs. If a prompt injection
persuades the agent to pass a different tenant ID, the Gateway rejects the call
rather than the downstream API serving it.

⚠ Because the action is unconstrained, this denies **every** tool whose input
has no `tenant_id` field — the guard short-circuits false and the `unless` bites.
That is fail-closed and defensible in this domain, but it is a broader rule than
it looks, and it will deny the "unrelated tool" probe in §8. If that is not what
you want, constrain the action to the set of tenant-scoped tools.

---

## 4. Threshold-gated disposition

```cedar
// Auto-disposition forbidden at or above the reporting threshold —
// a hard limit outside the model's control.
forbid(
  principal,
  action == AgentCore::Action::"disposition___propose_auto_clear",
  resource == AgentCore::Gateway::"<gateway-arn>"
) when {
  context has "input" &&
  context.input has "transaction_amount" &&
  context.input.transaction_amount >= 10000
};
```

Encoding the threshold here rather than in a prompt means it is auditable,
versioned, and cannot be argued away by a persuasive input.

Note the failure mode this section is most likely to hide: if the field in the
tool's `inputSchema` is `amount` and you wrote `transaction_amount`, the guard
short-circuits false, the `forbid` never fires, and every over-threshold call is
allowed. Verify against the schema the target actually publishes, then prove it
with §8.

---

## 5. Role-gated sensitive tools

```cedar
// Only supervisors may draft a regulatory filing.
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"filings___draft_sar_narrative",
  resource == AgentCore::Gateway::"<gateway-arn>"
) when {
  principal.hasTag("role") &&
  (principal.getTag("role") == "supervisor" || principal.getTag("role") == "mlro")
};
```

Works against JWT claims propagated through the Gateway, so the agent inherits
the *analyst's* entitlements rather than holding a superset of everyone's.

Under default-deny, a `permit` is what makes the tool reachable at all — so a
misspelled action name here fails in the safe direction (nobody can call it),
which is the opposite of the `forbid` case. Both are wrong; only one is loud.

---

## 6. Temporal policies: rules over the session, not the request

Session-aware rules are a distinct feature with its own language. They are
written in **Dogwood**, which is Cedar-compatible and accepts all existing Cedar
policies, and their session-aware conditions live in a **`temporal` block**.
Standard Cedar policies see only the current request; temporal policies see the
trajectory of the session.

⚠ **The syntax and mechanics in this section are unverified against
`policy-temporal-authoring.html`.** They were not confirmed in the last
documentation pass, and they were not contradicted either — confirm them before a
firm builds a control on them. Specifically unconfirmed: the statement going
under `policy` rather than `cedar`; the `::request` / `::response` / `::error`
event kinds; the `eventResource: resource` requirement; `sum`, `count`,
`formerly within` and `since within` syntax; and the self-referential-`::request`
behaviour. Note also that AWS names the construct a `temporal` block while the
examples below write `when temporal { }` — check which keyword form the service
accepts, or you will debug a parse failure as if it were a logic error.

The operational facts in §7 are a separate matter: those are confirmed against
the devguide, and they are the ones that decide whether temporal policy is a
control or a nudge.

Mechanics that are easy to get wrong:

- The statement goes under `policy` in the create-policy definition, **not**
  under `cedar` as for a stateless policy.
- Each condition is a predicate naming a window, an action, and an event kind:
  `::request` (recorded for each authorised request), `::response` (only on
  successful completion), `::error` (history only).
- **Every predicate must include `eventResource: resource`**, tying the match to
  the resource in the policy head.
- A prior action is only recorded as a `::response` if it was *permitted*. Under
  default-deny that means you must also `permit` the prerequisite, or your
  temporal rule can never be satisfied and you will debug the wrong policy.
- Self-referential conditions include the current request. This is why the
  cool-down and one-time-use patterns match `::response` — matching `::request`
  makes the current call match its own event and blocks the action forever.

### Cumulative exposure

Forty small requests adding up to one large extraction is structurally the same
control as detecting structuring — and an agent enumerating an unusual breadth
of customer records in one session is the signature of both a compromised agent
and a compromised analyst account.

```
forbid (
    principal,
    action == AgentCore::Action::"exports___bulk_export_transactions",
    resource == AgentCore::Gateway::"<gateway-arn>"
)
when temporal {
    exists (total: Long).
        (sum n for (n: Long), (t: Timepoint).
            where (formerly within 24h (AgentCore::Action::"exports___bulk_export_transactions"::request{ eventResource: resource, input.record_count: n } && tp(t)))) == total
        && total >= 50000
};
```

`sum` and `count` aggregate over matching events in the window; `formerly
within` matches a prior event; `since within` holds a condition since an anchor
event. The summed field must be an input field of the action.

---

## 7. Temporal: cross-call provenance

The strongest pattern here, and the one most worth the effort. It blocks the
agent **acting on an identifier it was never legitimately given**:

```
permit (
    principal,
    action == AgentCore::Action::"case-writes___flag_account",
    resource == AgentCore::Gateway::"<gateway-arn>"
)
when temporal {
    formerly within 1h AgentCore::Action::"lookup___get_customer_accounts"::response{
        eventResource: resource,
        output.account_id: context.input.account_id
    }
};

// Required companion: the lookup must be permitted, or it is never recorded
// as a response and the rule above can never be satisfied.
permit (
    principal,
    action == AgentCore::Action::"lookup___get_customer_accounts",
    resource == AgentCore::Gateway::"<gateway-arn>"
);
```

Correlating `output.account_id` from the recorded lookup against
`context.input.account_id` on the current request ties the write to a real prior
read. Prompt injection that persuades the model to target a different customer
fails at the Gateway, because that account never appeared in a response. For a
compliance platform this is close to a complete answer to "what if the agent is
manipulated into touching the wrong customer".

### The caveat that decides whether this is a control or a nudge

**The caller supplies the session ID.** You send it in the
`x-amzn-bedrock-agentcore-policy-session-id` header, and the Gateway does not
generate one for you — omit it, and a policy engine holding temporal policies
fails the request with a validation error.

(An AWS blog post states the opposite, that the Gateway generates the session ID.
The devguide is authoritative and says the caller supplies it; this file follows
the devguide. If you meet code written to the blog's version, the code is wrong,
and it is wrong in the direction of having no session at all.)

That makes session ID derivation exactly as load-bearing here as
`runtimeSessionId` is in `examples/tenant_isolation.py`, and for the same
reason: **anyone who can choose their own session ID can reset the history.**
A cumulative limit is then a limit per session, not per analyst — a caller who
starts a new session starts a new budget. AWS is explicit that session-scoped
counting shapes behaviour within a cooperative session rather than enforcing a
hard limit against a caller who controls the ID.

So derive the policy session ID server-side, from the authenticated user and
the unit of work, and never accept it from the client. Provenance rules (§7) are
robust to this in a way that counting rules (§6) are not: resetting the session
does not conjure a lookup that never happened, it only clears a tally.

Other operational facts worth knowing before you commit:

- Look-back is capped at **24 hours**; older trajectory events are deleted.
- **Changing any temporal policy invalidates open sessions.** The next request
  reusing one fails with HTTP 409 `ConflictException`. Start a new session and
  retry — and expect this on every policy deploy.
- **`GetWorkloadAccessToken` — and the reason decides how you scope it.** A
  temporal policy has to propagate the caller's session identity, which the
  Gateway does by minting a Workload Access Token; on the **AWS IAM inbound
  flow** that mint calls `bedrock-agentcore:GetWorkloadAccessToken`. It is
  required only while temporal policy is active, and not needed when temporal
  policy is disabled. Without it, tool invocations fail with `AccessDenied` on
  `GetWorkloadAccessToken` at the token-mint step — which presents as a broken
  tool, not as a missing permission, so it is worth recognising on sight. Scope
  it to the directory and this Gateway's workload identity rather than `*`:

  ```
  arn:aws:bedrock-agentcore:<region>:<account-id>:workload-identity-directory/default
  arn:aws:bedrock-agentcore:<region>:<account-id>:workload-identity-directory/default/workload-identity/<gatewayId>*
  ```

- **Multi-hop session and identity propagation is single-account,
  single-Region.** Where a call chains Gateway → Runtime → Gateway, the caller's
  session identity travels in the service-managed
  `X-Amz-Bedrock-AgentCore-Identity-WAT` header, and AWS documents that as
  working only within one account and Region — no cross-account and no
  cross-Region session propagation. This is a constraint on propagation across
  hops, not a blanket rule that every Gateway target must be co-located; do not
  over-read it into an architecture decision it does not make.
- Temporal policies are not available in every Region — check before designing
  around them.
- A `::response` is recorded shortly *after* the call completes, so a dependent
  call issued immediately may not see it. Sequence, do not fire back to back.

---

## 8. Prove enforcement empirically, in both directions

A policy's *status* tells you it loaded. Only a call tells you it fires. Run
these against the live Gateway after every policy change — the whole point of §1
is that the failure mode is invisible to every other check.

⚠ **Wait before you probe.** Gateway access-policy changes take **up to 15
minutes** to propagate; the policy is cached. A probe run immediately after an
edit may be exercising the *previous* policy — which is the failure mode most
likely to make a broken control look verified, because the old policy denies, you
record a pass, and the rule you actually shipped is inert. Wait out the window,
or re-run the probes later and require both runs to agree before you record the
result as evidence.

Three probes, and you need all three:

| Probe | Expectation | What its absence would hide |
|---|---|---|
| Over-threshold / forbidden call | **denied, and the bracket names a policy ID** | The policy never matches — inert control, and a default-deny that looks like a pass |
| Under-threshold / permitted call | **allowed** | The policy matches everything — over-broad |
| Unrelated tool | **allowed** | Collateral denial you will debug as an outage |

The second and third are the ones that get skipped, and they are what separate
"my policy works" from "my Gateway is broken in a way that happens to deny the
thing I tested".

```python
import re

# Names must be the Gateway-presented names — see §1.
TOOL = "disposition___propose_auto_clear"

def policy_denial(result):
    """A denial is a *successful* JSON-RPC response carrying isError.
    Returns (denied, reason) — reason is None when the request was denied by
    default because nothing matched, which is not your rule firing."""
    if not getattr(result, "isError", False):
        return (False, None)
    text = "".join(c.text for c in result.content if c.type == "text")
    if "Tool call not allowed due to policy enforcement" not in text:
        return (False, None)              # some other tool error, not a denial
    bracket = re.search(r"\[(.*?)\]", text)
    reason = bracket.group(1) if bracket else ""
    if "No policy applies" in reason:
        return (True, None)               # default-deny — nothing matched
    return (True, reason)                 # names the policy that fired

# 1. Over threshold → expect denial BY YOUR POLICY, not by default-deny.
r = mcp_client.call_tool_sync(TOOL, {"transaction_amount": 25000})
denied, reason = policy_denial(r)
assert denied and reason, "inert policy: denied by default, nothing matched"

# 2. Under threshold → expect success. Proves the forbid is conditional,
#    not a blanket block that only looks correct from probe 1.
r = mcp_client.call_tool_sync(TOOL, {"transaction_amount": 500})
assert not policy_denial(r)[0]

# 3. A tool the policy should not touch → expect success.
r = mcp_client.call_tool_sync("lookup___get_customer_accounts", {...})
assert not policy_denial(r)[0]
```

**A policy denial arrives as a JSON-RPC *success* response, not an error.** This
is the detail that quietly breaks test harnesses:

```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"AuthorizeActionException - Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"}],"isError":true}}
```

The transport succeeded; the *tool result* carries the refusal. So:

- **Assert on `result.isError == true`, then parse the bracketed reason.** A
  client that checks only for a JSON-RPC error code reads a denial as a success —
  and a suite built that way reports green while nothing is being enforced. This
  is the §1 failure mode wearing a test harness.
- **`-32002` is a different control.** It is the code for a **VPC endpoint
  policy** rejection — different cause, different fix, different team. Do not
  assert on it for Cedar.
- **The bracket is the only thing separating "matched" from "nothing matched",**
  because the `Tool Execution Denied` text is identical for both:
  - `[No policy applies to the request (denied by default).]` — default-deny,
    nothing matched. If you expected a rule to fire, this is §1: check the action
    name and the condition guards, not the enforcement mode.
  - a bracket naming a **policy ID** — a policy matched. That is the evidence you
    want, both for debugging and for the record, and it tells you *which* policy
    fired when several could have.
- `[Policy evaluation denied due to <policyId>]` is the **observed** form of the
  second case, not a documented string. Assert that a policy ID is present; do
  not assert on the sentence wrapped around it.

None of this is served by asserting "the call failed". A throttle, a bad ARN and a
policy denial are all failures, and only one of them means your control works.

Keep these probes as a test that runs on every policy change, and record the
run: "we verified the control was enforced on this date" is the evidence an
examiner is asking for, and it is not reconstructable after the fact from
configuration alone.

---

## Rollout

1. Deploy the Gateway and its targets first — you cannot name the Gateway ARN in
   a policy before it exists.
2. Read the real tool names off the Gateway (§1). Do not derive them.
3. Attach the engine in `LOG_ONLY` for the first policy, and run real traffic
   through it.
4. Read the decision logs in CloudWatch. Expect surprises — tools you forgot the
   agent uses, arguments shaped differently than assumed, conditions that never
   evaluate true.
5. Fix the policy, not the log.
6. Switch the attachment to `ENFORCE` once the log is clean for a representative
   period.
7. **From here on add rules at per-policy `LOG_ONLY`, not by moving the engine
   back.** The engine reports which `LOG_ONLY` policies would have flipped the
   decision, so the new rule gets its counterfactual while every proven rule keeps
   enforcing. Promote it to `ACTIVE` once that report is what you expect.
8. Wait out the propagation window — up to 15 minutes — before trusting a probe.
9. **Run the §8 probes.** `ACTIVE` + `ENFORCE` is not evidence of enforcement.
10. Treat policy changes as production changes: reviewed, versioned,
    rollback-able. If any temporal policy is involved, deploying one invalidates
    open sessions — plan for in-flight requests failing with 409 rather than being
    surprised.

Pair with **request interceptor Lambdas** on the Gateway for anything policy
cannot express — token exchange for per-user identity propagation, response
redaction stripping PII before it reaches the model or the trace, and limits
that must hold across sessions rather than within one (§7's caveat).
