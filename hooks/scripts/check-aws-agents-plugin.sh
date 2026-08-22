#!/usr/bin/env bash
# SessionStart hook — verify an authoritative AgentCore documentation source is available.
#
# The agentcore-compliance-ops skill instructs the agent to verify AgentCore API
# surface against an authoritative source rather than recalled detail. AgentCore
# changes quickly: model IDs, quota codes, API shapes and enforcement dates all
# move. With no authoritative source the agent answers from memory, which is
# exactly the failure the skill warns about.
#
# There are two acceptable sources, in order of preference:
#   1. The first-party global `amazon-bedrock` skill (~/.claude/skills/amazon-bedrock/).
#      AWS's own guidance; covers Runtime, Gateway, Harness, Memory, model
#      selection, quota mechanics and Guardrails, and loads by description.
#      Its presence is sufficient — this hook goes silent.
#   2. An AWS documentation MCP (aws-agents@claude-plugins-official), which is
#      supplementary: it can reach the pages `amazon-bedrock` does not carry
#      (Policy/Cedar detail, current AgentCore quotas, MMDSv2).
#
# Silent when a source is present. Only speaks when both are missing.
# Always exits 0 — a broken doc-check must never block a session.

set -uo pipefail

PLUGIN="aws-agents"
MARKETPLACE="claude-plugins-official"
PLUGIN_ID="${PLUGIN}@${MARKETPLACE}"
CACHE_DIR="${HOME}/.claude/plugins/cache/${MARKETPLACE}/${PLUGIN}"
BEDROCK_SKILL="${HOME}/.claude/skills/amazon-bedrock/SKILL.md"

# Preferred source present — the session has AWS's own guidance. Say nothing.
# Checked before anything else so users with the better source are never nagged.
[ -f "$BEDROCK_SKILL" ] && exit 0

# This check is best-effort. Without jq the settings inspection below cannot run,
# and a silent false "not enabled" would be worse than saying nothing at all.
command -v jq >/dev/null 2>&1 || exit 0

# Resolve the project root. The hook payload also carries .cwd, but reading stdin
# blocks to the hook timeout if stdin is ever a terminal, so rely on the
# environment instead — Claude Code sets CLAUDE_PROJECT_DIR for hooks.
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

SETTINGS_FILES=(
  "${PROJECT_DIR}/.claude/settings.json"
  "${PROJECT_DIR}/.claude/settings.local.json"
  "${HOME}/.claude/settings.json"
)

enabled_in() {
  # Enabled if the key is literally true, or an array (version-constraint form).
  local f="$1"
  [ -f "$f" ] || return 1
  jq -e --arg id "$PLUGIN_ID" '
    .enabledPlugins[$id] as $v
    | ($v == true) or ($v | type == "array")
  ' "$f" >/dev/null 2>&1
}

disabled_in() {
  # Explicitly set to false — a deliberate opt-out, not an oversight.
  local f="$1"
  [ -f "$f" ] || return 1
  jq -e --arg id "$PLUGIN_ID" '.enabledPlugins[$id] == false' "$f" >/dev/null 2>&1
}

is_enabled=1
for f in "${SETTINGS_FILES[@]}"; do
  if enabled_in "$f"; then is_enabled=0; break; fi
done

# Not enabled anywhere, but explicitly disabled somewhere — honour the opt-out.
if [ "$is_enabled" -ne 0 ]; then
  for f in "${SETTINGS_FILES[@]}"; do
    if disabled_in "$f"; then exit 0; fi
  done
fi

is_installed=1
[ -d "$CACHE_DIR" ] && [ -n "$(ls -A "$CACHE_DIR" 2>/dev/null)" ] && is_installed=0

# Everything present — say nothing.
if [ "$is_enabled" -eq 0 ] && [ "$is_installed" -eq 0 ]; then
  exit 0
fi

if [ "$is_enabled" -ne 0 ]; then
  reason="not enabled in this project's settings"
else
  reason="enabled in settings but not present in the plugin cache"
fi

CONTEXT="No authoritative AgentCore documentation source was found. The
first-party amazon-bedrock skill is not installed at
~/.claude/skills/amazon-bedrock/, and the AWS documentation MCP (${PLUGIN_ID})
is ${reason}.

This project's agentcore-compliance-ops skill needs one of them. Amazon Bedrock
AgentCore changes frequently — model IDs, service quotas, API shapes, IAM
resource formats and enforcement dates all move. Answering from recalled detail
produces confidently wrong infrastructure code.

Preferred: the first-party amazon-bedrock skill. It is AWS's own guidance and the
authority for the Bedrock and AgentCore API surface — Runtime, Gateway, Harness,
Memory, model selection, prompt caching, quota mechanics and Guardrails.

Supplementary: the AWS documentation MCP, which reaches the pages that skill does
not carry — Policy/Cedar detail, current AgentCore quotas and limits, and MMDSv2.
Install it with:

  claude plugin install ${PLUGIN_ID} --scope project

Then run /reload-plugins. The MCP tools appear as
mcp__plugin_aws-agents_awsknowledge__aws___search_documentation and
...___read_documentation — load them with ToolSearch before use.

Note the split of authority in both directions. On platform detail the AWS
sources win over anything recalled. But this plugin stays authoritative on two
points where amazon-bedrock is behind: its Runtime deployment workflow omits the
MMDSv2 update step entirely, so following it yields a runtime that cannot be
invoked, and it defers Policy/Cedar to live docs where this plugin's
examples/cedar_policies.md is substantially more detailed.

With neither source available, say so plainly and flag that AgentCore specifics
in your output are unverified against current documentation."

jq -nc \
  --arg ctx "$CONTEXT" \
  --arg msg "No AgentCore doc source found (amazon-bedrock skill absent; ${PLUGIN_ID} ${reason}) — AgentCore guidance this session will be unverified." \
  '{
     systemMessage: $msg,
     hookSpecificOutput: {
       hookEventName: "SessionStart",
       additionalContext: $ctx
     }
   }'

exit 0
