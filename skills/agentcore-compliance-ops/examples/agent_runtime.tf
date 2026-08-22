##############################################################################
# AgentCore Runtime — Terraform, hardened for a regulated multi-tenant build
#
# Terraform has the broadest native AgentCore resource coverage of the common
# IaC options (Runtime, Browser, Code Interpreter, Memory, log delivery), and
# the trust policies in its published examples already carry confused-deputy
# conditions — which the CDK equivalents frequently omit.
#
# Where quickstart Terraform for AgentCore is weakest is model invocation: it
# routinely grants `Resource = "*"` on bedrock:InvokeModel. This file fixes that
# and the other gaps that separate a working demo from a deployable system.
#
# Cross-references are to references/production-rules.md.
##############################################################################

terraform {
  required_version = ">= 1.11"
  required_providers {
    # A `~>` floor is a promise about which arguments exist, so set it from the
    # newest argument you actually reference, not from whatever was current when
    # you started. `~> 6.21` permits 6.21.0, which predates the AgentCore
    # arguments this file relies on:
    #
    #   code_configuration            added in 6.22.0
    #   require_service_s3_endpoint   added in 6.55.0  (read-only; the comment
    #                                 on network_configuration below describes it)
    #
    # Under-flooring does not fail at plan with "unsupported argument for this
    # provider version" — it fails with a bare "unsupported argument", so the
    # first instinct is to doubt the argument rather than the constraint.
    #
    # `hashicorp/null` was declared here and never used — no null_resource
    # anywhere in this file. Removed, and worth removing rather than leaving as
    # harmless: the pattern `null` exists to serve is local-exec build glue, and
    # the omissions section at the bottom of this file rejects exactly that. A
    # dependency whose only purpose contradicts the file's own guidance is an
    # invitation, not dead weight.
    aws = { source = "hashicorp/aws", version = "~> 6.55" }
  }

  # Remote state is NOT optional for a regulated deployment. Quickstarts ship
  # this commented out and default to local state with no locking — fine for a
  # one-person experiment, unacceptable once more than one person deploys or
  # once the state file contains anything you would have to disclose.
  #
  # Use a distinct key per environment. Nothing here enforces state isolation
  # between dev/stage/prod for you.
  #
  # `use_lockfile` (S3-native state locking, replacing the DynamoDB lock table)
  # was added in Terraform 1.10 and left experimental until 1.11, which is also
  # where `dynamodb_table` became formally deprecated — hence >= 1.11 above.
  # On 1.9 and earlier this argument is not recognised, so you deploy believing
  # state is locked when it is not.
  backend "s3" {
    bucket       = "REPLACE-terraform-state"
    key          = "agentcore/ENVIRONMENT/terraform.tfstate"
    region       = "REPLACE"
    encrypt      = true
    use_lockfile = true
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

##############################################################################
# Variables
##############################################################################

variable "environment" {
  type = string
  validation {
    # Leaving this a free string makes it easy to deploy "prod" config into a
    # "prd" state file, which you discover much later. Gate it.
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "network_mode" {
  type = string
  # The valid values are PUBLIC and VPC — there is no "PRIVATE". Writing
  # PRIVATE is a natural guess that fails at apply with a ValidationException
  # from the API rather than at plan time, so gate it here where the error is
  # cheap. https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_NetworkConfiguration.html
  #
  # Quickstarts default to PUBLIC. For a platform handling customer PII, VPC is
  # the correct default and PUBLIC the deliberate, documented exception.
  default = "VPC"
  validation {
    condition     = contains(["PUBLIC", "VPC"], var.network_mode)
    error_message = "network_mode must be PUBLIC or VPC."
  }
}

variable "vpc_subnet_ids" {
  description = "Private subnets for network_mode = VPC. Required in that mode."
  type        = list(string)
  default     = []

  # network_mode defaults to VPC — deliberately, see above — while this defaults
  # to []. The provider marks `subnets` and `security_groups` REQUIRED inside
  # network_mode_config, so the default configuration builds a block with two
  # empty lists and fails at the API on apply, after the role and repository have
  # already been created. Same argument as the network_mode validation directly
  # above: gate it at plan time where the error is cheap and names its own cause.
  #
  # Cross-variable references in `validation` need Terraform >= 1.9; the
  # required_version above is 1.11, so this is safe to rely on.
  validation {
    condition     = var.network_mode != "VPC" || length(var.vpc_subnet_ids) > 0
    error_message = "vpc_subnet_ids must be non-empty when network_mode = VPC."
  }
}

variable "vpc_security_group_ids" {
  description = "Security groups for network_mode = VPC. Required in that mode."
  type        = list(string)
  default     = []

  validation {
    condition     = var.network_mode != "VPC" || length(var.vpc_security_group_ids) > 0
    error_message = "vpc_security_group_ids must be non-empty when network_mode = VPC."
  }
}

variable "allowed_model_ids" {
  description = <<-EOT
    Model IDs this agent may invoke, bare (no geographic prefix).
    Constrains IAM to approved models — a requirement where model usage must be
    attributable and restricted. Empty list means all models, which you should
    only choose deliberately.
  EOT
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  type = number
  # Quickstarts hardcode 14 days. AML record-keeping obligations are measured
  # in years; set this from your retention policy, not from an example.
  #
  # Wired to aws_cloudwatch_log_group.agent_runtime below. It was previously
  # declared with this comment and referenced NOWHERE, which is the worse of the
  # two failures it warns about: a documented retention control that configures
  # nothing reads as satisfied in review. Because the execution role grants
  # logs:CreateLogGroup, AgentCore creates the group itself on first use at the
  # CloudWatch default — NEVER EXPIRE — so the actual behaviour was not 14 days
  # but indefinite retention of prompts, responses and customer PII. Grep for a
  # variable's uses before trusting its comment.
  default = 365
}

variable "agent_name" {
  type = string
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.agent_name))
    error_message = "agent_name must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$."
  }
}

variable "image_tag" {
  description = <<-EOT
    Container image tag, built and pushed by CI. Deliberately NOT defaulted to
    "latest": a mutable tag makes a deployment unreproducible, and you cannot
    tell an examiner which image made a decision. Pass an immutable digest or
    commit SHA.
  EOT
  type        = string
}

variable "model_id" {
  description = <<-EOT
    Full model ID including geographic prefix, e.g. eu.amazon.nova-2-lite-v1:0.

    That example is a cheap placeholder for documentation, not a recommendation
    for supervised triage. Make the selection against the global first-party
    `amazon-bedrock` skill's model-selection guide (installed at
    ~/.claude/skills/amazon-bedrock/), and against your own golden set — see the
    note above MODEL_ID in examples/agent_template.py for a measured case where
    tier intuitions did not transfer.
  EOT
  type        = string
}

variable "tenant_id" {
  description = "Tenant this runtime serves. Omit for a shared runtime using session-level isolation."
  type        = string
  default     = ""
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # `.region`, not `.id` — `.id` on the aws_region data source is deprecated,
  # terraform validate warns on it, and the provider will eventually remove it.
  region = data.aws_region.current.region

  # Both resource shapes are required for inference-profile model IDs
  # (eu./us./apac./global. prefixes). The foundation-model ARN has an EMPTY
  # account segment — inserting an account ID there causes authorization
  # failure. Region is wildcarded because a geographic profile dispatches to
  # any destination region in its geography (§1).
  #
  # Tighter still, once you know your destination regions: enumerate them and
  # add a bedrock:InferenceProfileArn condition. See
  # https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html
  model_resources = length(var.allowed_model_ids) > 0 ? concat(
    [for m in var.allowed_model_ids : "arn:aws:bedrock:*::foundation-model/${m}"],
    [for m in var.allowed_model_ids :
    "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/*.${m}"],
    ) : [
    "arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:${local.region}:${local.account_id}:inference-profile/*",
  ]

  # Per-tenant cost attribution has to be wired at the resource, not inferred
  # later. The runtime resource supports `tags`/`tags_all` and carried none,
  # while tenant_id was passed only as a container env var — visible to the
  # agent, invisible to Cost Explorer. The sibling control-plane path already
  # does this correctly (examples/deployment_orchestration.py passes
  # tags={"tenantId": tenant_id} to CreateAgentRuntime), so the two deploy paths
  # disagreed about whether a tenant's spend was attributable.
  #
  # Two things to know before relying on these:
  #
  #  * A tag key must be ACTIVATED as a cost allocation tag in Billing before it
  #    appears in Cost Explorer, and activation is not retroactive in effect —
  #    expect roughly a day before tagged usage shows up. Applying the tag and
  #    checking the console the same afternoon looks like the tag did not work.
  #  * Tag-based attribution is Cost Explorer-shaped: a 24-48h lag, and no
  #    per-request granularity. It is the reconciliation target, not the live
  #    signal — see examples/cost_tracking.py for why both exist.
  #
  # tenantId is omitted rather than empty for a shared runtime. An empty tag
  # value is accepted by AWS and silently produces an attribution bucket named
  # "" that aggregates every shared runtime in the account; a shared runtime's
  # spend is genuinely not attributable at the resource level, and must come from
  # the per-request pipeline instead.
  cost_allocation_tags = merge(
    {
      environment = var.environment
      agentName   = var.agent_name
    },
    var.tenant_id != "" ? { tenantId = var.tenant_id } : {},
  )
}

##############################################################################
# Execution role
##############################################################################

resource "aws_iam_role" "agent_execution" {
  name = "agentcore-${var.agent_name}-${var.environment}"

  # Confused-deputy protection. Without these conditions any account could
  # induce the service to assume this role on their behalf. Published Terraform
  # for AgentCore generally includes this; CDK Python equivalents often do not.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:*" }
      }
    }]
  })
}

# Do NOT additionally attach the AWS-managed BedrockAgentCoreFullAccess policy,
# as quickstarts commonly do — it defeats every bit of the scoping below.
# Inline, least-privilege only.

resource "aws_iam_role_policy" "agent_execution" {
  role = aws_iam_role.agent_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BedrockModelInvocation"
        Effect = "Allow"
        # Converse streams internally even when streaming is not requested,
        # so both actions are required.
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = local.model_resources
      },
      {
        # Without these the container cannot emit logs AT ALL — no log group is
        # created and every failure is an opaque 500 (§6).
        Sid    = "Observability"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
          "logs:DescribeLogStreams", "logs:DescribeLogGroups",
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        # CloudWatch namespaces are CASE-SENSITIVE and AWS's own documentation is
        # not self-consistent about this one: the observability guide gives
        # `bedrock-agentcore`, other pages give `Bedrock-AgentCore` and
        # `Bedrock-Agentcore`. AWS tells you to DISCOVER the value rather than
        # copy it, and that instruction is the actual guidance here.
        #
        # A StringEquals on the wrong casing is a silent-denial generator. The
        # agent logs no error — PutMetricData is called by the ADOT layer, which
        # swallows the denial — the metric simply never appears in CloudWatch, and
        # the natural conclusion is that instrumentation is not wired up. You then
        # debug the agent for a condition key.
        #
        # `?` matches exactly one character in StringLike, so this tolerates the
        # three documented casings without widening to arbitrary namespaces:
        #   bedrock-agentcore / Bedrock-AgentCore / Bedrock-Agentcore
        #
        # Discovery commands — run all three, in your region, and pin the answer
        # with a comment recording the date. Only one returns metrics:
        #   aws cloudwatch list-metrics --namespace "bedrock-agentcore"
        #   aws cloudwatch list-metrics --namespace "Bedrock-AgentCore"
        #   aws cloudwatch list-metrics --namespace "Bedrock-Agentcore"
        # Checked 2026-08-17: the devguide observability page documents
        # `bedrock-agentcore`. Re-verify against your own account rather than
        # inheriting that.
        Condition = { StringLike = { "cloudwatch:namespace" = "?edrock-?gent?ore" } }
      },
      {
        Sid    = "WorkloadIdentity"
        Effect = "Allow"
        # GetWorkloadAccessTokenForUserId is deliberately EXCLUDED. AWS
        # documents that the platform "treats the userId value as an opaque
        # string and does not verify it against an authenticated end-user
        # identity" — the binding rests entirely on the caller passing the right
        # value. Where a JWT is available, GetWorkloadAccessTokenForJWT
        # validates issuer, signature and expiry instead. Denying the userId
        # variant prevents one authenticated user impersonating another.
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/get-workload-access-token.html
        Action = [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        ]
        Resource = [
          "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${local.region}:${local.account_id}:workload-identity-directory/default/workload-identity/*",
        ]
      },
    ]
  })
}

##############################################################################
# Container registry
##############################################################################

resource "aws_ecr_repository" "agent" {
  name = "agentcore-${var.agent_name}-${var.environment}"

  # Immutable tags. With MUTABLE, the image behind a tag can change after
  # deployment, so the artefact that produced a decision is no longer
  # identifiable — which defeats the point of pinning a model version.
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration { scan_on_push = true }

  # Customer-managed keys are normally required where the image may embed
  # prompts or reference data. AWS-managed encryption is the lazy default.
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
}

variable "kms_key_arn" {
  description = <<-EOT
    Customer-managed KMS key ARN, wired to the ECR repository and the runtime's
    CloudWatch log group in this file. It does NOT cover agent state — Memory,
    session storage and any DynamoDB or S3 you add are separate wirings you have
    to make yourself. The description previously claimed "ECR, logs and any agent
    state" while only ECR was wired, which is the kind of overclaim that gets
    read as coverage in a control review.
  EOT
  type        = string
}

##############################################################################
# Runtime
##############################################################################

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = replace("${var.agent_name}_${var.environment}", "-", "_")
  role_arn           = aws_iam_role.agent_execution.arn

  # Exactly one of container_configuration / code_configuration may be set.
  #
  # If you switch to code_configuration, note a live worked example of provider
  # lag: as of provider 6.60.0 `runtime` accepts only PYTHON_3_10 through
  # PYTHON_3_13, while the AgentCore API and CloudFormation also accept
  # PYTHON_3_14 and NODE_22. The provider rejects a value the service supports,
  # at plan time, with what reads like an invalid-input error about your code
  # rather than a gap in the provider — so the reflex is to downgrade the runtime
  # instead of checking. Same shape as the MMDSv2 gap documented below: verify
  # against the API reference, not the provider docs, when a value is refused.
  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agent.repository_url}:${var.image_tag}"
    }
  }

  network_configuration {
    network_mode = var.network_mode

    # Only meaningful when network_mode = VPC. Note that
    # require_service_s3_endpoint is read-only in the provider and is rejected
    # if you try to set it.
    dynamic "network_mode_config" {
      for_each = var.network_mode == "VPC" ? [1] : []
      content {
        subnets         = var.vpc_subnet_ids
        security_groups = var.vpc_security_group_ids
      }
    }
  }

  environment_variables = {
    AWS_REGION         = local.region
    AWS_DEFAULT_REGION = local.region
    MODEL_ID           = var.model_id
    TENANT_ID          = var.tenant_id
  }

  tags = local.cost_allocation_tags

  depends_on = [aws_iam_role_policy.agent_execution]
}

##############################################################################
# Runtime log group — retention and encryption
##############################################################################

# Create the group explicitly. Do NOT let AgentCore create it for you.
#
# The execution role above grants logs:CreateLogGroup, so if this resource is
# absent AgentCore creates the group on first use with the CloudWatch default
# retention, which is NEVER EXPIRE. For a group carrying full prompt and response
# text plus customer PII, indefinite retention is a worse outcome than the
# 14-day quickstart default the log_retention_days comment warns about: one is a
# gap in an audit trail, the other is an unbounded PII store nobody decided to
# create and no erasure request can be honoured against.
#
# Naming: the group is /aws/bedrock-agentcore/runtimes/<agent-runtime-id>-<endpoint>
# and the default endpoint is DEFAULT. The name depends on the runtime's ID, so
# this resource necessarily applies AFTER the runtime exists. Two consequences:
#
#  * There is a window between runtime creation and this apply. Nothing logs
#    until the runtime is first invoked, so a normal apply closes it — but if the
#    runtime has already run (a re-apply against an existing deployment, or a
#    smoke test between steps), AgentCore owns the group already and this fails
#    with ResourceAlreadyExistsException. `terraform import` it rather than
#    deleting the group, which would discard records you are required to keep.
#  * If you add non-default endpoints, each gets its OWN group. Retention set on
#    this one says nothing about theirs.
resource "aws_cloudwatch_log_group" "agent_runtime" {
  name              = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.agent.agent_runtime_id}-DEFAULT"
  retention_in_days = var.log_retention_days

  # The CMK's key policy must allow the CloudWatch Logs service principal
  # `logs.${local.region}.amazonaws.com` to Encrypt/Decrypt/GenerateDataKey and
  # DescribeKey, conditioned on the log-group ARN. Without that grant, log
  # delivery FAILS SILENTLY: the group exists, the association is accepted, and
  # events simply never arrive. There is no error on the agent side, so the
  # symptom is indistinguishable from an agent that was never invoked — which is
  # the one diagnostic this skill leans on elsewhere (see production-rules.md
  # §6). Encrypting the log group with a key the service cannot use therefore
  # costs you the observability AND the evidence.
  kms_key_id = var.kms_key_arn

  tags = local.cost_allocation_tags
}

# ⚠ MMDSv2 — THE GAP THIS FILE CANNOT CLOSE IN TERRAFORM
#
# Since 2026-06-30, AgentCore refuses to invoke a runtime that does not require
# MMDSv2: InvokeAgentRuntime fails with a ValidationException reading "This
# runtime is not MMDSv2-enabled". The runtime still creates, still reports
# READY, and still shows no drift.
#
# `requireMMDSV2` lives in `metadataConfiguration`, which only UpdateAgentRuntime
# accepts — there is no create-time parameter in the API, and as of provider
# 6.60.0 the resource above exposes no argument for it either (support is still
# an open PR). So a runtime created by this Terraform alone is uninvocable, and
# Terraform will never tell you.
#
# Remediate after apply, and re-check it in CI rather than trusting it once:
#
#   aws bedrock-agentcore-control update-agent-runtime \
#     --agent-runtime-id "$ID" \
#     --metadata-configuration requireMMDSV2=true \
#     --agent-runtime-artifact ... --role-arn ... --network-configuration ...
#
# (UpdateAgentRuntime is a full replace — echo back the runtime's current
# artifact, role and network configuration or you will silently rewrite them.)
#
# See examples/deployment_orchestration.py (ensure_mmdsv2) for the API version.
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html
#
# Resource reference:
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/bedrockagentcore_agent_runtime

##############################################################################
# Multi-agent: scope the invoke grant
##############################################################################

# The common multi-agent pattern grants InvokeAgentRuntime on `runtime/*` —
# every runtime in the account and region — and relies on an env var to decide
# which one to actually call. That is not a control: a compromised or
# prompt-injected orchestrator can reach any agent in the account.
#
# Scope it to the specific specialist:
#
# resource "aws_iam_role_policy" "orchestrator_invoke_specialist" {
#   role = aws_iam_role.orchestrator.id
#   policy = jsonencode({
#     Version = "2012-10-17"
#     Statement = [{
#       Effect   = "Allow"
#       Action   = ["bedrock-agentcore:InvokeAgentRuntime"]
#       Resource = aws_bedrockagentcore_agent_runtime.specialist.agent_runtime_arn
#     }]
#   })
# }

##############################################################################
# What this file deliberately omits
##############################################################################
#
# Patterns that appear routinely in quickstart IaC and must not survive into a
# regulated deployment:
#
#  * Hardcoded credentials in the template. Anything written as a literal lands
#    in Terraform state, CloudFormation outputs and custom-resource properties —
#    none of which are secret stores, all of which are readable by anyone with
#    read access to the pipeline. Worse, an output not marked `sensitive` prints
#    in plaintext in CI logs. Generate at deploy time into Secrets Manager and
#    reference it; mark every credential-bearing output `sensitive = true`.
#
#  * Weakened password policy on the demo identity pool — complexity flags
#    disabled and an 8-character minimum are common in getting-started code and
#    have no place in an environment holding customer PII.
#
#  * Synchronous build-in-apply. Shelling out to a build script via local-exec
#    (or a Lambda custom resource with a hard 15-minute ceiling) blocks the
#    apply on a live Docker build with no retry or resume. Build the image in
#    CI, push it, and pass the tag in as a variable — which is why `image_tag`
#    above is required rather than defaulted.
#
#    Build it for ARM64: `docker buildx build --platform linux/arm64`. AgentCore
#    Runtime executes arm64 containers only, and nothing in the path checks. An
#    x86_64 image builds, passes `docker push`, passes the ECR scan, and is
#    accepted by CreateAgentRuntime; the failure arrives at first invocation as
#    an opaque runtime error with no mention of architecture. On an Apple Silicon
#    laptop the native build is already arm64, so this bites hardest in CI, where
#    the runner is x86_64 and the same Dockerfile silently produces the wrong
#    artefact.
