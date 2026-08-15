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
    aws  = { source = "hashicorp/aws", version = "~> 6.21" }
    null = { source = "hashicorp/null", version = "~> 3.0" }
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
}

variable "vpc_security_group_ids" {
  description = "Security groups for network_mode = VPC. Required in that mode."
  type        = list(string)
  default     = []
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
  description = "Full model ID including geographic prefix, e.g. eu.amazon.nova-2-lite-v1:0"
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
        Sid       = "Metrics"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
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
  description = "Customer-managed KMS key for ECR, logs and any agent state."
  type        = string
}

##############################################################################
# Runtime
##############################################################################

resource "aws_bedrockagentcore_agent_runtime" "agent" {
  agent_runtime_name = replace("${var.agent_name}_${var.environment}", "-", "_")
  role_arn           = aws_iam_role.agent_execution.arn

  # Exactly one of container_configuration / code_configuration may be set.
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

  depends_on = [aws_iam_role_policy.agent_execution]
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
