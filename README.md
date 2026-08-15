# agentcore-compliance-ops

A Claude Code plugin for building AI agents on **Amazon Bedrock AgentCore** that sit inside a
regulated financial-crime compliance function — alert triage, case investigation, SAR narrative
drafting, transaction monitoring, customer risk assessment, due diligence, QA sampling and fraud
detection.

It is opinionated about one thing: **a control that lives in a prompt is not a control.** Most of
what it carries is the catalogue of AgentCore defects that fail silently, or blame the wrong cause.

## Install

From a local checkout:

```bash
claude plugin marketplace add /path/to/agentcore-compliance-ops-plugin
claude plugin install agentcore-compliance-ops@agentcore-compliance-ops
```

Or, once pushed to a git remote:

```bash
claude plugin marketplace add <your-git-url>
claude plugin install agentcore-compliance-ops@agentcore-compliance-ops
```

Add `--scope project` to enable it for one repository rather than your user account. Run
`/reload-plugins` afterwards.

## What it ships

| Component | Purpose |
|---|---|
| `skills/agentcore-compliance-ops/` | The skill. Activates on AgentCore and compliance-agent work; routes to six reference files and ten copy-adaptable examples. |
| `agents/agentcore-infra-reviewer.md` | Read-only review of IAM, CDK/CloudFormation, agent templates and deployment orchestration against known production failure modes. |
| `agents/compliance-agent-architect.md` | Workflow design — orchestration shape, human-in-the-loop gates, Harness vs Runtime, evaluation scope. Invoke before implementation. |
| `agents/compliance-prompt-engineer.md` | System prompts, structured output schemas, golden-set fixtures and evaluation rubrics. |
| `hooks/` | A `SessionStart` check that an authoritative AWS documentation source is available. |

## The documentation hook

AgentCore moves quickly — model IDs, quota codes, API shapes, IAM resource formats and enforcement
dates all change. Answering from recalled detail produces confidently wrong infrastructure code.

The hook checks whether an AWS documentation MCP is installed and enabled. It is **silent when
everything is present**, always exits 0, and never blocks a session. When the source is missing it
says so, so that AgentCore specifics in the session's output are flagged as unverified rather than
quietly trusted.

It looks for `aws-agents@claude-plugins-official`. Any equivalent AWS documentation MCP works just
as well — the skill locates documentation tools via `ToolSearch` rather than hardcoding tool names.

## Reference material

| File | Covers |
|---|---|
| `references/production-rules.md` | 23 numbered production defects with symptom, mechanism, fix and a symptom-first diagnostic table. The debugging checklist. |
| `references/guardrails.md` | Layered output controls, human-in-the-loop placement, audit records, action grounding. |
| `references/deployment-patterns.md` | Harness vs Runtime, tenant data access, session binding, quotas, post-deploy verification. |
| `references/architecture-patterns.md` | Reusable agent shapes, and how to vet sample code before copying it. |
| `references/workflow-catalog.md` | Which compliance workflows to build, and in what order. |
| `references/iac-hardening.md` | Choosing an IaC flavour, and auditing what starter-grade IaC reliably gets wrong. |

## Scope

Use it whenever an AI agent goes near a supervised compliance decision, and for AgentCore platform
problems generally — execution-role IAM for inference profiles, session isolation, Gateway targets,
Cedar policy, quotas, MMDSv2, per-tenant cost attribution.

It is **not** for AML domain questions with no AI component, or for agent platforms other than
AgentCore.

## Licence

MIT.
