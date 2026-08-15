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
| **Terraform** | Broad — Runtime, Browser, Code Interpreter, Memory, log delivery | Backend model fits per-environment state isolation; trust policies commonly carry confused-deputy conditions | Model IAM is frequently left as `Resource = "*"`; provider lag behind new AgentCore features |
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

**The CDK generation matters.** AgentCore L2 constructs now ship in stable
[`aws-cdk-lib.aws_bedrockagentcore`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_bedrockagentcore-readme.html)
([Python reference](https://docs.aws.amazon.com/cdk/api/v2/python/aws_cdk.aws_bedrockagentcore/README.html)).
Guidance written against the earlier alpha package, or against the period when
CDK could not package an agent artifact at all, is now actively misleading. In
particular, `AgentRuntimeArtifact` covers local Dockerfile assets, existing ECR
images, direct code assets and S3 zips — so a hand-rolled ECR-plus-CodeBuild
custom resource whose only job is producing an image is very likely dead weight
you can delete. Check before maintaining it.

CloudFormation resource reference:
[`AWS::BedrockAgentCore::Runtime`](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-bedrockagentcore-runtime.html).

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
(interface endpoints for the AgentCore data and control planes, ECR, CloudWatch
Logs, and an S3 gateway endpoint for image layers; private subnets with NAT,
because public subnets do not give a runtime internet access). See
[Configure AgentCore Runtime and tools for VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html).
You can enforce this organisationally with the `bedrock-agentcore:subnets` and
`bedrock-agentcore:securityGroups`
[IAM condition keys](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-vpc-condition.html),
which is far more reliable than a code-review convention.

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

`READY`, `ACTIVE` and `ENFORCE` all mean *loaded*. A Cedar policy naming an action
that does not exist reaches `ENFORCE` and denies nothing. A Gateway target
pointing at the wrong function ARN goes `ACTIVE` and lists tools happily. An
execution role missing the inference-profile ARN deploys clean and fails on the
first model call. None of these show up in stack output.

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
- [ ] `requireMMDSV2: true` in `metadataConfiguration` on every runtime
- [ ] Containers run as a non-root user; base image refreshed on a schedule
- [ ] No credentials in state, outputs, or custom-resource properties
- [ ] Every credential-bearing output marked `sensitive`
- [ ] VPC-attached networking unless public is justified; enforced with the VPC IAM condition keys
- [ ] Log retention set from your retention policy on every log group
- [ ] Customer-managed KMS keys on S3, ECR, CloudWatch Logs, and the Gateway
- [ ] Remote state with locking; distinct key per environment
- [ ] Image built in CI with the tag passed in — or direct code deployment, with no build during apply
- [ ] Tests that assert, against synthesised templates and imported handlers rather than live infra
- [ ] Post-deploy functional gate: allowed path succeeds, denied path is denied, log groups exist
