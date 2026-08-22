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
| `skills/agentcore-compliance-ops/` | The skill. Activates on AgentCore and compliance-agent work; routes to the reference files below and to copy-adaptable examples. |
| `agents/agentcore-infra-reviewer.md` | Read-only review of IAM, CDK/CloudFormation, agent templates and deployment orchestration against known production failure modes. |
| `agents/compliance-agent-architect.md` | Workflow design — orchestration shape, human-in-the-loop gates, Harness vs Runtime, evaluation scope. Invoke before implementation. |
| `agents/compliance-prompt-engineer.md` | System prompts, structured output schemas, golden-set fixtures and evaluation rubrics. |
| `hooks/` | A `SessionStart` check that an authoritative AgentCore documentation source is available — the `amazon-bedrock` skill, or failing that an AWS documentation MCP. |

## Relationship to the first-party `amazon-bedrock` skill

The two are complementary, not competing.

**`amazon-bedrock` is the platform API authority.** It is AWS's own guidance and it wins on Bedrock
and AgentCore mechanics: current model IDs and inference-profile shapes, Runtime, Gateway, Harness
and Memory API surface, model selection and migration, prompt caching, quota and throttling
mechanics, Guardrails configuration. If you have it installed, read it before WebFetching and prefer
it over anything recalled.

**This plugin is the compliance-control layer on top.** Where an agent sits near a supervised
compliance decision, where the human gate belongs, which layer enforces a prohibition, what the
audit record must capture, and the catalogue of AgentCore defects that fail silently.

The rule in both directions: **where the two disagree on a platform detail, the AWS skill wins;
where they disagree on how a control must be built for a supervised compliance decision, this one
does.** Two carve-outs where this plugin is currently more current and stays authoritative:

- **MMDSv2.** `amazon-bedrock`'s Runtime deployment workflow omits the MMDSv2 update step entirely.
  A runtime built by following it cannot be invoked. `references/deployment-patterns.md` here covers
  it.
- **Policy/Cedar.** `amazon-bedrock` defers it to live documentation.
  `examples/cedar_policies.md` here is substantially more detailed.

## The documentation hook

AgentCore moves quickly — model IDs, quota codes, API shapes, IAM resource formats and enforcement
dates all change. Answering from recalled detail produces confidently wrong infrastructure code.

The hook checks that *some* authoritative source is present. It is **silent when one is**, always
exits 0, and never blocks a session.

1. **First it looks for the first-party `amazon-bedrock` skill** at
   `~/.claude/skills/amazon-bedrock/`. That is the primary source, so if it is there the hook exits
   immediately and says nothing.
2. **Otherwise it checks for an AWS documentation MCP** (`aws-agents@claude-plugins-official`),
   which is supplementary: it reaches the pages `amazon-bedrock` does not carry — Policy/Cedar
   detail, current AgentCore quotas and limits, MMDSv2. Any equivalent AWS documentation MCP works
   just as well; the skill locates documentation tools via `ToolSearch` rather than hardcoding tool
   names.

Only when both are missing does it speak, so that AgentCore specifics in the session's output are
flagged as unverified rather than quietly trusted. If you have deliberately set the MCP plugin to
`false` in your settings, the hook honours that opt-out and stays silent. It also exits silently
when `jq` is unavailable, since it cannot read settings without it and a false "not enabled" would
be worse than saying nothing.

## Reference material

| File | Covers |
|---|---|
| `references/production-rules.md` | Numbered production defects with symptom, mechanism, fix and a symptom-first diagnostic table. The debugging checklist. |
| `references/control-stack.md` | Layered output controls, human-in-the-loop placement, audit records, action grounding. |
| `references/audit-trail.md` | Audit-record mechanisms, reasoning-trace capture, per-fact attribution, log retention and PII masking — what makes a decision examinable years later. |
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
