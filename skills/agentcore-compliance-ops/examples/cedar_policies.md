# Cedar policies for compliance agent tool access

Policy evaluates every tool call **at the Gateway, outside the agent's execution
boundary**. It is the control layer that survives prompt injection, model
change and framework change — the place to express anything that must not
depend on the model behaving.

Two modes: `LOG_ONLY` (evaluates and traces, does not enforce) and `ENFORCE`
(default-deny; any matching `forbid` wins). **Deploy in `LOG_ONLY` first and
read the logs**, or you will discover your policy is wrong by breaking
production.

⚠ Policy protects only traffic that actually flows through the Gateway.
Restrict the Runtime's resource policy so only the Gateway can invoke it, or
these are decorative — see `deployment-patterns.md`.

Current references:

- Tutorial and worked policy —
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-getting-started.html
- Enforcement modes —
  https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GatewayPolicyEngineConfiguration.html
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

**Tool identity lives in the ACTION, not the context.** There is no
`context.toolName` attribute. A condition keyed on one is not an error — it is a
condition that is never true:

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

**Constrain the resource whenever you constrain the action.** A tool-specific
policy with a loose resource is rejected at deploy time:

> `ValidationException`: When parsing the policy statement, a constrained action
> scope was encountered, please constrain the resource to a specific
> `AgentCore::Gateway` resource when creating tool-specific policies.

So `resource is AgentCore::Gateway` fails; the specific ARN is required. Below,
`<gateway-arn>` stands for
`arn:aws:bedrock-agentcore:<region>:<account-id>:gateway/<gateway-id>`.

This forces a two-phase deployment — the Gateway must exist before you can write
a policy naming it. Deploy the Gateway, read back its ARN, then attach policy.

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
policies, and their session-aware conditions live in a `when temporal { }`
block. Standard Cedar policies see only the current request; temporal policies
see the trajectory of the session.

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
- The Gateway's IAM role needs `bedrock-agentcore:GetWorkloadAccessToken`, even
  when outbound auth is IAM. Without it, temporal enforcement fails.
- Gateway and all targets must be in the **same account and Region**; there is
  no cross-account or cross-Region session propagation.
- Temporal policies are not available in every Region — check before designing
  around them.
- A `::response` is recorded shortly *after* the call completes, so a dependent
  call issued immediately may not see it. Sequence, do not fire back to back.

---

## 8. Prove enforcement empirically, in both directions

A policy's *status* tells you it loaded. Only a call tells you it fires. Run
these against the live Gateway after every policy change — the whole point of §1
is that the failure mode is invisible to every other check.

Three probes, and you need all three:

| Probe | Expectation | What its absence would hide |
|---|---|---|
| Over-threshold / forbidden call | **denied** | The policy never matches — inert control |
| Under-threshold / permitted call | **allowed** | The policy matches everything — over-broad |
| Unrelated tool | **allowed** | Collateral denial you will debug as an outage |

The second and third are the ones that get skipped, and they are what separate
"my policy works" from "my Gateway is broken in a way that happens to deny the
thing I tested".

```python
# Names must be the Gateway-presented names — see §1.
TOOL = "disposition___propose_auto_clear"

# 1. Over threshold → expect denial
r = mcp_client.call_tool_sync(TOOL, {"transaction_amount": 25000})   # DENIED

# 2. Under threshold → expect success. Proves the forbid is conditional,
#    not a blanket block that only looks correct from probe 1.
r = mcp_client.call_tool_sync(TOOL, {"transaction_amount": 500})     # ALLOWED

# 3. A tool the policy should not touch → expect success.
r = mcp_client.call_tool_sync("lookup___get_customer_accounts", {...})  # ALLOWED
```

A denial surfaces to the MCP caller as JSON-RPC error code **-32002**:

```
Tool Execution Denied: Tool call not allowed due to policy enforcement
[Policy evaluation denied due to <policyId>]
```

Assert on that, not on "the call failed" — a throttle, a bad ARN and a policy
denial are all failures, and only one of them means your control works. The
`policyId` in the message tells you *which* policy fired, which is what you need
when several could have.

Keep these probes as a test that runs on every policy change, and record the
run: "we verified the control was enforced on this date" is the evidence an
examiner is asking for, and it is not reconstructable after the fact from
configuration alone.

---

## Rollout

1. Deploy the Gateway and its targets first — you cannot name the Gateway ARN in
   a policy before it exists.
2. Read the real tool names off the Gateway (§1). Do not derive them.
3. Author in `LOG_ONLY`. Run real traffic through it.
4. Read the decision logs in CloudWatch. Expect surprises — tools you forgot the
   agent uses, arguments shaped differently than assumed, conditions that never
   evaluate true.
5. Fix the policy, not the log.
6. Switch to `ENFORCE` once the log is clean for a representative period.
7. **Run the §8 probes.** `ACTIVE` + `ENFORCE` is not evidence of enforcement.
8. Treat policy changes as production changes: reviewed, versioned, rollback-able.
   If any temporal policy is involved, deploying one invalidates open sessions —
   plan for in-flight requests failing with 409 rather than being surprised.

Pair with **request interceptor Lambdas** on the Gateway for anything policy
cannot express — token exchange for per-user identity propagation, response
redaction stripping PII before it reaches the model or the trace, and limits
that must hold across sessions rather than within one (§7's caveat).
