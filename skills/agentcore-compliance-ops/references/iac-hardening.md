# Choosing and hardening AgentCore infrastructure as code

AgentCore can be provisioned four ways, and they differ materially in coverage,
IAM precision and how much you have to fix before regulated data is in scope.
This is what to pick, and what to audit for in whatever stack you inherit or
generate.

The defect list below is the important half. It describes what *starter-grade*
AgentCore IaC reliably gets wrong — tutorials, quickstarts, console-generated
stacks, CLI scaffolding, and code an LLM writes from any of those. Every item has
been observed repeatedly. Treat it as a review checklist, not a description of
one particular codebase.

Worked hardened starting point: `examples/agent_runtime.tf`.

AgentCore's API surface moves quickly. **Verify resource names, properties and
construct APIs against live documentation before generating code** — the CDK
surface in particular changed recently (see below), so recalled detail is
unusually likely to be wrong.

---

## Flavour comparison

| | Native resource coverage | Strengths | Watch for |
|---|---|---|---|
| **Terraform** | Broad — Runtime, Browser, Code Interpreter, Memory, log delivery | Backend model fits per-environment state isolation; trust policies commonly carry confused-deputy conditions | Model IAM is frequently left as `Resource = "*"`; **provider lag is concrete, not theoretical** — worked example below |
| **CDK L2** (`aws-cdk-lib.aws_bedrockagentcore`) | `Runtime`, `RuntimeEndpoint`, `Gateway`, `GatewayTarget`, `Memory`, `BrowserCustom`, `CodeInterpreterCustom`, `OnlineEvaluation` | Least code; handles artifact packaging; grant helpers make scoped IAM easy to express | Newest surface, so examples on the internet are often written against the older alpha module |
| **CDK L1** (`Cfn*`) | Whatever CloudFormation supports | Complete and predictable; the escape hatch when L2 lags | Verbose; nothing is granted for you, so omissions are silent |
| **CloudFormation** (`AWS::BedrockAgentCore::*`) | Same as L1 | No toolchain beyond the service | Least maintainable at scale; every IAM statement hand-written |

**Choosing.** For a production multi-tenant build, pick on how you intend to run
environments rather than on syntax: Terraform if per-environment state and an
existing Terraform estate are the organising principle, CDK L2 if you want the
grant helpers and artifact packaging and are comfortable pinning `aws-cdk-lib`.
Both are defensible. What is not defensible is mixing L2 and L1 for the same
resource, or generating a stack from an example without checking which CDK
generation it targets.

**Provider lag, worked.** "The provider lags the service" sounds like a
caveat until it removes an option you have already promised someone. The
current example is `codeConfiguration.runtime` for direct code deployment.
The API and CloudFormation accept
`PYTHON_3_10 | PYTHON_3_11 | PYTHON_3_12 | PYTHON_3_13 | PYTHON_3_14 | NODE_22`;
the AWS Terraform provider at **6.60.0** accepts only `PYTHON_3_10` through
`PYTHON_3_13`. So a Terraform stack currently cannot deploy a Node agent or a
Python 3.14 agent at all, and the news arrives as a schema validation error at
plan time rather than as a documented limitation you could have read first.
Check the provider's resource schema for the enum you need *before* committing
to a runtime version, and treat "the API supports it" and "my tool supports it"
as separate questions for every new AgentCore feature. Where the provider has
not caught up and the feature is required, CloudFormation or a CDK L1 `Cfn*`
resource will have it.

**The CDK generation matters.** AgentCore L2 constructs now ship in stable
[`aws-cdk-lib.aws_bedrockagentcore`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)
([Python reference](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrockagentcore/README.html)),
as of `aws-cdk-lib` **v2.265.0** — and the alpha module's equivalents are now marked
deprecated in favour of them, which is as clear a signal as CDK gives. Guidance
written against the earlier alpha package, or against the period when CDK could not
package an agent artifact at all, is now actively misleading, and examples on the
internet still overwhelmingly target the alpha module. In
particular, `AgentRuntimeArtifact` covers local Dockerfile assets, existing ECR
images, direct code assets and S3 zips — so a hand-rolled ECR-plus-CodeBuild
custom resource whose only job is producing an image is very likely dead weight
you can delete. Check before maintaining it.

CloudFormation resource reference:
[`AWS::BedrockAgentCore::Runtime`](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-bedrockagentcore-runtime.html).

### Read what the L2 constructs put in your IAM, because three defaults are wrong for this domain

Observed by reading the installed `aws-cdk-lib` v2.265.0 typings and compiled JS,
not inferred from documentation. Each of these is a construct doing something
reasonable for a general workload and wrong for a regulated one, and each is
invisible unless you synthesise and read the template.

1. **The `Runtime` L2 adds `cloudwatch:PutMetricData` with a case-sensitive
   `StringEquals` on the namespace.** That is exactly the silent-denial shape this
   file warns about two sections down: AWS's own documentation spells the AgentCore
   namespace three different ways, the comparison is case-sensitive, and a
   mismatch denies `PutMetricData` with no error anywhere — the metric simply never
   appears. The construct also grants `GetWorkloadAccessTokenForUserId`, which the
   hardening checklist tells you to *deny* where a JWT is always available, because
   the user id on that path is an opaque string the platform does not verify.
   Synthesise, read the role, and override both.

2. **The `Gateway` L2 builds its execution role with an unconditioned trust
   statement *alongside* a conditioned one.** Trust policy statements are OR'd, so
   an unconditioned statement makes the conditioned one decorative — the
   confused-deputy protection you can see in the template is inert. Pass your own
   role rather than letting the construct build one, and assert on the synthesised
   trust policy rather than on the presence of a `Condition` block somewhere in it.

3. **Omitting `authorizerConfiguration` on the `Gateway` L2 stands up a Cognito
   user pool.** That is a defensible default for a demo and a surprising piece of
   identity infrastructure to find in a compliance deployment — one you did not
   choose, do not manage, and now have to account for. Set the authorizer
   explicitly, even when the answer is SigV4.

The general lesson, which outlives these three specifics: an L2 construct is a
policy decision expressed as code, and for a regulated workload the decision has
to be yours. `cdk synth` and read the IAM. A grant you did not write is a grant
nobody reviewed.

---

## The inference-profile IAM inconsistency

Worth understanding precisely, because it is the difference between a policy
that works and one that fails on every modern model ID.

Modern Bedrock model access goes through
[inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html),
whose ARNs are account-scoped `inference-profile/*` resources — *not*
`foundation-model/*`. Policies get written four ways in the wild:

| Shape granted | Works with profiles? |
|---|---|
| `"*"` | Yes — by being unscoped, which is the problem |
| `foundation-model/*` + `arn:aws:bedrock:{region}:{account}:*` | Yes — the account wildcard covers `inference-profile` implicitly |
| `foundation-model/*` + explicit `inference-profile/*` | Yes — explicitly, and this is the shape to copy |
| `arn:aws:bedrock:{region}::foundation-model/*` alone | **No** |

The last shape is the failure documented in `production-rules.md` §1:

```python
resources=[f"arn:aws:bedrock:{region}::foundation-model/*"]   # BROKEN for profiles
```

It looks complete, passes review, works for bare model IDs, and throws
`AccessDeniedException` on every profile ID. Be explicit rather than relying on a
wildcard that happens to cover it — an implicit grant is one refactor away from
becoming the broken shape, and the refactor will look like tightening.

Scope to your approved model IDs rather than `*` once you know them. Which
profiles exist for which models in which Regions is a
[lookup](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html),
not something to hardcode from memory.

---

## What starter IaC routinely omits

Audit for each of these by reading the code, not by trusting the stack's name.

**Hardcoded credentials.** Demo stacks stand up a Cognito test user with a
literal password so the quickstart works end to end. That value lands in
Terraform state, CloudFormation outputs and custom-resource properties — none of
which are secret stores. Worse, a convenience output that embeds the password in
a ready-to-paste `get-token` command is easy to leave unmarked as sensitive, so
it prints unredacted in `terraform output` and in CI logs.

**Weakened password policy.** The same demo pools commonly disable uppercase,
lowercase, number and symbol requirements with a minimum length of 8 —
hardcoded, not parameterised, and easy to carry into production untouched.

**Unscoped agent-to-agent invocation.** Multi-agent stacks frequently grant
`bedrock-agentcore:InvokeAgentRuntime` on `runtime/*` — every runtime in the
account and Region — and rely on an environment variable to select the target.
IAM then does not enforce that the orchestrator can only reach its own
specialist, so a compromised or prompt-injected orchestrator can invoke any agent
in the account. Scope the grant to the specialist's actual ARN.

**Command-execution APIs left open.** `InvokeAgentRuntimeCommand` and
`InvokeAgentRuntimeCommandShell` execute commands inside the microVM with full
access to its filesystem and credentials, bypassing the model, Cedar policies and
guardrails entirely. Almost no starter stack mentions them, so almost none denies
them. Deny both unless you have a named reason, and scope them to specific
runtime ARNs where you do. See
[Execute shell commands in AgentCore Runtime sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html).

**Resource-based policy on only one resource.** Invocation authorization
evaluates policies on *both* the agent runtime and the agent endpoint, and both
must allow. Restricting one and not the other leaves the gap open. See
[Resource-based policies for AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/resource-based-policies.html).

**No tests, or tests that assert nothing.** Two failure shapes recur. The first
is a print-based smoke script against live infrastructure with zero assertions —
it exercises the deployment and reports nothing a CI system can fail on. The
second is subtler and worse: a unit test that re-implements the handler logic
locally and tests the copy. If the deployed handler diverges, the test keeps
passing forever. Assert against the *synthesised template* for infrastructure,
and against the *imported* handler for behaviour.

**Public networking by default.** Runtimes default to public egress. For a
platform handling customer PII, VPC-attached should be the default and public the
deliberate exception — and the VPC configuration has real prerequisites
(interface endpoints for the AgentCore data and control planes, ECR and CloudWatch
Logs; an S3 gateway endpoint for image layers; private subnets with NAT,
because public subnets do not give a runtime internet access). See
[Configure AgentCore Runtime and tools for VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html).

**And the S3 gateway endpoint is now yours to provision.**
`require_service_s3_endpoint` is **read-only** — the API rejects it on create
*and* on update, so a stack that tries to set it fails rather than quietly doing
nothing. The consequence is the part that matters for a new build: agent runtimes
created **on or after May 5, 2026** do not get a service-managed S3 gateway
endpoint, so with `networkMode = VPC` the endpoint that container image layers
are pulled through is a resource **you** create in the VPC. An older runtime
working since before that date is not evidence the new one will, and the symptom
is an image pull that cannot reach S3 — which presents as a networking mystery
rather than as a missing prerequisite. Put it in the VPC module alongside the
interface endpoints, and assert its presence in the pre-flight.

You can enforce VPC attachment organisationally with `bedrock-agentcore:subnets` and
`bedrock-agentcore:securityGroups`
[IAM condition keys](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-vpc-condition.html),
which is far more reliable than a code-review convention.

**A case-sensitive IAM condition on the CloudWatch metric namespace.** This one
looks like good hygiene and is a silent-denial generator. The
`cloudwatch:namespace` condition key is case-sensitive, and AWS's own
documentation does not agree with itself on the value — it instructs you to
*discover* it, and lists `Bedrock-AgentCore` in one place, `Bedrock-Agentcore` in
another, and `bedrock-agentcore` in a third. A `StringEquals` on that key
therefore has a good chance of matching nothing, and the failure is invisible
from every direction: `PutMetricData` is denied, the agent does not fail (a
metric publication failure does not fail the request that produced it), and the
metric your alarm watches simply never arrives — so the alarm sits in
`INSUFFICIENT_DATA`, or never leaves `OK`. Discover the namespace in your own
account and Region and pin that, or use `StringLike` with a pattern tolerant of
case. Never pin a case-sensitive literal for a value the documentation tells you
to go and look up.

**Container hardening treated as a Dockerfile detail.** Where you keep a
container image at all, the full hardening list — non-root user, ARM64, minimal
base, no build toolchain in the runtime layer, scanning in CI — lives in the
global `amazon-bedrock` skill
(`amazon-bedrock/references/agentcore-runtime-container-build.md`) rather than being duplicated
and left to rot here. The item most often missed is **pinning the base image to a
patch version rather than a floating minor tag**: a floating tag can be
repointed under you, so two builds of the same commit are not the same image.
That is a reproducibility problem before it is a security one, and in a regulated
deployment "which image produced this decision" needs an answer. Pair it with
`image_tag_mutability = "IMMUTABLE"` on the ECR repository so a tag you have
deployed cannot be overwritten either — stricter than AWS's own default posture,
and deliberately so, because the pair is what makes the question answerable.

**Synchronous image build coupled to apply.** Terraform stacks shell out via
`local-exec` to a script that polls CodeBuild under a hard timeout; CDK stacks
use a custom resource whose poll loop is bounded by the Lambda ceiling. Neither
retries or resumes, and a rollback rebuilds from scratch. Build in CI, push, and
pass the image tag in as a variable — or drop the image entirely and use direct
code deployment, which removes this whole class of problem.

**No multi-environment isolation.** No workspace usage, no per-environment
backend key logic, local state with no locking as the documented default.
Separation then depends on an operator remembering to pass different variables
and point at a different backend, which is not a control.

**Fixed literals that should be parameters:** ECR lifecycle retention counts,
CloudWatch Logs retention (often 14 days), the build image and compute type. Log
retention in particular is wrong by orders of magnitude for AML record-keeping —
set it from your retention policy, explicitly, on every log group. See
[log group retention settings](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html#SettingLogRetention).

**Managed policies standing in for least privilege.** `BedrockAgentCoreFullAccess`
is convenient and far too broad for a runtime execution role; AWS's own guidance
is that CLI-generated policies are for development and testing, not production.
Read [what it actually grants](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/BedrockAgentCoreFullAccess.html),
then write inline statements scoped to specific ARNs. Validate them with
[IAM Access Analyzer](https://docs.aws.amazon.com/IAM/latest/UserGuide/what-is-access-analyzer.html).

**Suppression files are the author's own gap list.** Stacks that ship `cdk-nag`
suppressions are telling you exactly what they waived — wildcard IAM resources,
missing Lambda DLQs and
[reserved concurrency](https://docs.aws.amazon.com/lambda/latest/dg/configuration-concurrency.html),
S3 access logging, [DynamoDB PITR](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html),
Cognito MFA, secret rotation, ECR KMS keys, permissive security groups — usually
annotated "apply least-privilege in production". Read that file before copying a
stack. It is the fastest available inventory of what you are about to inherit.

---

## Verify the deployment functionally, not by status

An IaC apply that succeeds proves the control plane accepted your resources. It
does not prove the system behaves correctly, and in a compliance context those
are very different claims.

`READY` on a runtime, `READY` on a Gateway target and `ENFORCE` on a policy engine
all mean *loaded*. A Cedar policy naming an action that does not exist reaches
`ENFORCE` and denies nothing. A Gateway target pointing at the wrong function ARN
reaches `READY` and lists tools happily. An execution role missing the
inference-profile ARN deploys clean and fails on the first model call. None of
these show up in stack output.

**There is no `ACTIVE` state on a Gateway target**, so a wait loop written for one
never exits and reports a timeout that has nothing to do with the target.
`GetGatewayTarget` returns `CREATING`, `UPDATING`, `UPDATE_UNSUCCESSFUL`,
`DELETING`, `READY`, `FAILED`, `SYNCHRONIZING`, `SYNCHRONIZE_UNSUCCESSFUL`,
`CREATE_PENDING_AUTH`, `UPDATE_PENDING_AUTH` or `SYNCHRONIZE_PENDING_AUTH`. The
`*_PENDING_AUTH` states read like failures and are not — they mean the target is
waiting on a user to complete OAuth federation, which is the expected resting state
of a user-delegated target nobody has authorised yet. The real error states are
`FAILED`, `UPDATE_UNSUCCESSFUL` and `SYNCHRONIZE_UNSUCCESSFUL`, and
**`statusReasons` is the field that says why** — log it in the wait loop rather than
the status alone, or you get a failed apply with no reason attached to it. A target
still in `CREATING` after ten minutes is a support case, not a config bug.

So make the post-deploy gate behavioural, and run it from CI rather than by hand:

- **Exercise both directions.** A call that should succeed *and does*, plus a call
  that should be refused *and is*. The negative case carries the information; the
  positive case proves you have not simply broken the agent. Both are required —
  a stack where everything is denied passes a denial test perfectly.
- **Check for the log group.** Absence of a CloudWatch log group for a Lambda
  target or interceptor is unambiguous proof it has never been invoked — not an
  empty log group, *no* log group. One `describe-log-groups` call settles whether
  a target is genuinely wired up, and it is the fastest way to catch an agent
  narrating a tool call it never made (`production-rules.md` §18). Note that a
  missing log group can *also* mean the execution role lacks logs permissions
  (§6), so confirm the grant before concluding the target is unwired.
- **Assert the invariants you claim.** If the design says the agent cannot write,
  attempt a write and require the denial. If it says sessions are tenant-scoped,
  attempt a cross-tenant call and require the failure.
- **Wait on the endpoint, not only the runtime.** Runtime and endpoint report
  readiness independently, and the runtime gets there first, so a gate that stops at
  the runtime passes before the agent is invocable. Poll
  `get-agent-runtime-endpoint` for `READY` too, and log `statusReasons` on anything
  that ends up in an error state.
- **Persist before verifying.** Write the deployment record as soon as the
  resource is confirmed ready, then verify — a slow-failing check must never be
  able to orphan a live resource (`production-rules.md` §4).

---

## Hardening checklist

Before any AgentCore IaC handles regulated data:

- [ ] Model invocation scoped to approved model IDs, both ARN shapes present
- [ ] Confused-deputy conditions (`aws:SourceAccount`, `aws:SourceArn`) on every service trust policy
- [ ] No AWS-managed `BedrockAgentCoreFullAccess` — inline least-privilege only
- [ ] `GetWorkloadAccessTokenForUserId` and `InvokeAgentRuntimeForUser` explicitly denied where JWT is always available
- [ ] `InvokeAgentRuntimeCommand` / `InvokeAgentRuntimeCommandShell` denied, or scoped to named principals and runtime ARNs
- [ ] Resource-based policies present on **both** the runtime and the endpoint
- [ ] Agent-to-agent invoke scoped to the specific target ARN
- [ ] Gateway execution role scoped to the specific interceptor and target Lambda ARNs
- [ ] `iam:PassRole` present on the harness-creating principal
- [ ] `InvokeHarness` override fields (`model`, `systemPrompt`, `tools`, `allowedTools`, `skills`, `actorId`, execution limits) stripped or allowlisted in the calling backend
- [ ] `requireMMDSV2: true` in `metadataConfiguration` on every runtime
- [ ] Containers run as a non-root user; base image refreshed on a schedule
- [ ] Base image pinned to a patch version, not a floating tag; ECR `image_tag_mutability = "IMMUTABLE"`
- [ ] No credentials in state, outputs, or custom-resource properties
- [ ] Every credential-bearing output marked `sensitive`
- [ ] VPC-attached networking unless public is justified; enforced with the VPC IAM condition keys
- [ ] S3 gateway endpoint provisioned by you for any VPC-mode runtime created on or after 2026-05-05
- [ ] No case-sensitive `StringEquals` on `cloudwatch:namespace` — namespace discovered, or `StringLike`
- [ ] Log retention set from your retention policy on every log group
- [ ] Customer-managed KMS keys on S3, ECR, CloudWatch Logs, and the Gateway
- [ ] Remote state with locking; distinct key per environment
- [ ] Image built in CI with the tag passed in — or direct code deployment, with no build during apply
- [ ] Tests that assert, against synthesised templates and imported handlers rather than live infra
- [ ] Endpoint created and `READY`; readiness asserted on the endpoint, not only the runtime
- [ ] Post-deploy functional gate: allowed path succeeds, denied path is denied, log groups exist

Where the deployment policy is that the provider's logs hold usage telemetry only — no reasoning, no
PII, no PII-bearing output — four more, and they are account-boundary controls rather than
resource-level ones. A configuration that *can* be created will be, eventually, by someone debugging
at 2am:

- [ ] Production in its own account, with an SCP denying `bedrock:PutModelInvocationLoggingConfiguration` to every principal — `Put` only, never `Delete`, so remediation stays possible if a configuration ever exists. Confirm the exact IAM action string against the Service Authorization Reference before shipping: a misspelled SCP action denies nothing, silently.
- [ ] Vended log delivery (`logs:PutDeliverySource`, `PutDeliveryDestination`, `CreateDelivery`) denied to every principal except one deployment role, with the permitted log types pinned in that role's IaC. A blanket deny cannot be used here because it would also block the `USAGE_LOGS` delivery this profile wants, and no documented condition key distinguishes log type on those calls — so this half is detective rather than preventive, and weaker than the model-invocation-logging deny.
- [ ] A scheduled assertion over `describe-deliveries` and `get-model-invocation-logging-configuration` that fails the build or pages on drift — the compensating control for the gap above.
- [ ] The allowlist gate (`examples/log_projection.py`) sits between generation and every log sink, and nothing constructs a metering payload without passing through it

`references/audit-trail.md` carries the deployment-profile split, which rules are prod versus
dev-only, and the verification for each.
