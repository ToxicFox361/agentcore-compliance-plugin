"""CDK IAM constructs for AgentCore in a multi-tenant compliance platform.

The inference-profile and service-linked-role shapes below are the two IAM
defects that cost the most debugging time, because both produce errors that
point somewhere other than the cause.

Cross-references are to references/production-rules.md.
"""

from aws_cdk import Stack, aws_iam as iam
from constructs import Construct


def agent_execution_role(scope: Construct, region: str, *,
                         config_table_arn: str) -> iam.Role:
    """Execution role for an AgentCore Runtime hosting a compliance agent."""
    account_id = Stack.of(scope).account

    return iam.Role(
        scope,
        "AgentExecutionRole",
        assumed_by=iam.ServicePrincipal(
            "bedrock-agentcore.amazonaws.com",
            # Confused-deputy protection. Without these conditions any account
            # could induce the service to assume this role on their behalf.
            conditions={
                "StringEquals": {"aws:SourceAccount": account_id},
                "ArnLike": {
                    "aws:SourceArn":
                        f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"
                },
            },
        ),
        inline_policies={
            "AgentPolicy": iam.PolicyDocument(statements=[

                # ── Model invocation ─────────────────────────────────────────
                # Modern model IDs are inference profiles (eu./us./apac./global.
                # prefixes) and need BOTH resource shapes. A policy with only
                # the foundation-model ARN looks complete, passes review, and
                # fails on every profile ID with AccessDeniedException (§1).
                #
                # Note the empty account segment "::" on the foundation-model
                # ARN — foundation models are not account-scoped, and inserting
                # an account ID there causes authorization failure.
                #
                # The region is wildcarded because a geographic profile
                # dispatches to any destination region in its geography.
                #
                # AWS's worked example, including the tighter form that pins
                # each destination region and adds a bedrock:InferenceProfileArn
                # condition — prefer that once your destination set is known:
                # https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "bedrock:InvokeModel",
                        # Converse streams internally even when you do not ask
                        # for streaming — omitting this breaks non-stream calls.
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[
                        f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                        "arn:aws:bedrock:*::foundation-model/*",
                    ],
                ),

                # ── Observability ────────────────────────────────────────────
                # Without these the container cannot emit logs AT ALL — no log
                # group is created, and every failure is an opaque 500 with
                # nothing to inspect (§6).
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                        "logs:DescribeLogStreams",
                        "logs:DescribeLogGroups",
                    ],
                    resources=[
                        f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*"
                    ],
                ),
                # CloudWatch namespaces are CASE-SENSITIVE, and AWS's own docs
                # disagree about this one across three pages: the AgentCore
                # observability guide gives `bedrock-agentcore`, others give
                # `Bedrock-AgentCore` and `Bedrock-Agentcore`. AWS's instruction
                # is to DISCOVER the value rather than copy it, which is the
                # tell that the published value is not reliable.
                #
                # The failure mode: a StringEquals on the wrong casing denies
                # PutMetricData SILENTLY. The agent raises nothing — the call is
                # made by the ADOT layer, which swallows the denial — and the
                # metric simply never appears. The visible symptom is "metrics
                # are not wired up", so the debugging goes into instrumentation
                # rather than into a condition key, and this is a hard defect to
                # find twice because the first time nobody wrote it down.
                #
                # `?` matches exactly one character in StringLike, so the
                # pattern below tolerates all three documented casings without
                # widening the grant to arbitrary namespaces.
                #
                # Discovery — run all three against your own region; only one
                # returns metrics, and record the date you checked:
                #   aws cloudwatch list-metrics --namespace "bedrock-agentcore"
                #   aws cloudwatch list-metrics --namespace "Bedrock-AgentCore"
                #   aws cloudwatch list-metrics --namespace "Bedrock-Agentcore"
                # Checked 2026-08-17: the devguide observability page documents
                # `bedrock-agentcore`. Verify in your account rather than
                # inheriting that. Kept identical to examples/agent_runtime.tf —
                # two examples disagreeing on a control is its own defect.
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["cloudwatch:PutMetricData"],
                    resources=["*"],
                    conditions={"StringLike": {
                        "cloudwatch:namespace": "?edrock-?gent?ore"
                    }},
                ),
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["xray:PutTraceSegments",
                             "xray:PutTelemetryRecords"],
                    resources=["*"],
                ),

                # ── Workload identity ────────────────────────────────────────
                # BOTH ARNs. AWS's own service-linked-role policy grants the
                # directory resource as well as the workload identities under
                # it, and the token calls are authorized against both — so the
                # narrower list looks correct and fails with
                # AccessDeniedException on a call that names neither ARN in its
                # message. examples/agent_runtime.tf already listed both; this
                # file listed only the second, which is worse than either being
                # wrong consistently: whichever example a reader copies, the
                # other one silently contradicts it.
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["bedrock-agentcore:GetWorkloadAccessToken",
                             "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{region}:{account_id}"
                        f":workload-identity-directory/default",
                        f"arn:aws:bedrock-agentcore:{region}:{account_id}"
                        f":workload-identity-directory/default/workload-identity/*",
                    ],
                ),

                # ── Runtime config read ──────────────────────────────────────
                # The agent reads its own config each invocation. A missing
                # grant is swallowed by the template's try/except and the agent
                # silently runs on defaults forever (§6).
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["dynamodb:GetItem"],
                    resources=[config_table_arn],
                ),
            ])
        },
    )


def agent_memory_actor_isolation(role: iam.Role, memory_arn: str,
                                 actor_id_claim: str = "${aws:PrincipalTag/actorId}") -> None:
    """Constrain memory access to one actor, enforced by IAM.

    Application-layer actorId checks are best-effort. An IAM condition is the
    authoritative boundary, and cross-actor access then fails with
    AccessDeniedException rather than quietly returning another user's context.

    `bedrock-agentcore:actorId` is a real condition key; so are
    `bedrock-agentcore:sessionId`, `:namespace` and `:runtimeSessionId`. Check
    the current list before inventing one — the service defines both
    CamelCase and lowercase keys, and a misspelled condition key does not fail
    closed, it is simply absent from evaluation:
    https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html

    IMPORTANT LIMIT: an IAM condition matches against the *IAM principal* making
    the call. Where end users authenticate with OAuth/OIDC rather than AWS
    credentials — the usual shape when a backend calls on behalf of analysts —
    the caller is one IAM principal for everybody, so this condition cannot
    separate them. For that case use fine-grained access control at the
    Gateway, which evaluates Cedar against the JWT's claims:
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-gateway-fgac.html
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-gateway-fgac-policy-examples.html

    Gateway-side policy only governs traffic through the Gateway. Pair it with a
    resource-based policy on the Memory resource so the data plane cannot be
    reached directly, bypassing the checks:
    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-gateway-restrict-access.html

    For multi-tenant use, namespace the actor by tenant (`tenantA/user1`) so the
    condition scopes to a tenant as well as a user.
    """
    role.add_to_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        actions=["bedrock-agentcore:CreateEvent",
                 "bedrock-agentcore:ListEvents",
                 "bedrock-agentcore:RetrieveMemoryRecords"],
        resources=[memory_arn],
        conditions={"StringEquals": {
            "bedrock-agentcore:actorId": actor_id_claim
        }},
    ))


def deployment_lambda_slr_grants(fn) -> None:
    """Grants for a Lambda that creates AgentCore runtimes.

    AgentCore uses SEVERAL service-linked roles, each under its own service
    principal. Scoping to the bare principal covers Gateway only, so the first
    CreateAgentRuntime in a fresh account fails with "Failed creating service
    linked role" — an error that reads like a permissions bug in your own code
    and is actually about a role you have never heard of (§2).

        bedrock-agentcore.amazonaws.com                  Gateway
        runtime-identity.bedrock-agentcore.amazonaws.com  needed by CreateAgentRuntime
        network.bedrock-agentcore.amazonaws.com           Network
        identity-network.bedrock-agentcore.amazonaws.com  Identity network

    Only bites on brand-new accounts — typically the first customer deployment.
    """
    slr_resources = [
        "arn:aws:iam::*:role/aws-service-role/bedrock-agentcore.amazonaws.com/*",
        "arn:aws:iam::*:role/aws-service-role/*.bedrock-agentcore.amazonaws.com/*",
    ]

    fn.add_to_role_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        actions=["iam:CreateServiceLinkedRole"],
        resources=slr_resources,
        conditions={"StringLike": {"iam:AWSServiceName": [
            "bedrock-agentcore.amazonaws.com",
            "*.bedrock-agentcore.amazonaws.com",
        ]}},
    ))
    fn.add_to_role_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        actions=["iam:GetRole"],
        resources=slr_resources,
    ))

    # DeleteAgentRuntime also tears down the runtime's workload identity.
    # Without these the delete fails and the runtime keeps billing, invisible
    # to your own delete path.
    fn.add_to_role_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        actions=[
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:GetAgentRuntime",
            "bedrock-agentcore:ListAgentRuntimes",
            "bedrock-agentcore:DeleteAgentRuntime",
            "bedrock-agentcore:DeleteWorkloadIdentity",
            "bedrock-agentcore:GetWorkloadIdentity",
        ],
        resources=["*"],
    ))

    # UpdateAgentRuntime is needed too, and not only for config changes:
    # MMDSv2 can only be set through it, never at create time. See
    # examples/deployment_orchestration.py.
    fn.add_to_role_policy(iam.PolicyStatement(
        effect=iam.Effect.ALLOW,
        actions=["bedrock-agentcore:UpdateAgentRuntime"],
        resources=["*"],
    ))


# ── Where least privilege actually lives ─────────────────────────────────────
#
# IAM is the outermost boundary and the last one to hold, but it is not where
# most agent over-reach is prevented — it is too coarse. An execution role that
# can call the platform API cannot distinguish "read this case" from "close it";
# that distinction lives above IAM.
#
# The layers, strongest first:
#
#   1. The tool list the model is OFFERED. A tool that is not in the list
#      cannot be called, by any prompt, in any language, under any injection.
#      This is the control with no bypass, and it is one line of code —
#      see examples/agent_template.py.
#   2. Cedar policy at the Gateway. Catches the case where someone widens
#      the list, and is enforced outside the agent's execution boundary —
#      see examples/cedar_policies.md.
#   3. IAM, below — the boundary that holds if the Gateway is bypassed.
#   4. The system prompt. NOT a control. A prompt saying "do NOT call
#      close_alert" is a request, and the model is free to decline it, be
#      argued out of it, or simply lose track of it in a long context.
#      If the tool is offered, treat it as callable.
#
# The recurring mistake is spending the effort at layer 4 because it is the
# easiest to write, and then reasoning about the system as though it were
# layer 1. Deterministic writes belong in code the model does not mediate.
