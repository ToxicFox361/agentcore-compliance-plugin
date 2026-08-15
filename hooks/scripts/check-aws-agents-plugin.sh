#!/usr/bin/env bash
# SessionStart hook — verify the AWS documentation MCP is available.
#
# The agentcore-compliance-ops skill instructs the agent to verify AgentCore API
# surface against live documentation rather than recalled detail. AgentCore
# changes quickly: model IDs, quota codes, API shapes and enforcement dates all
# move. Without the aws-agents plugin the agent has no authoritative source and
# will answer from memory, which is exactly the failure the skill warns about.
#
# Silent when everything is present. Only speaks when something is missing.
# Always exits 0 — a broken doc-check must never block a session.

set -uo pipefail

PLUGIN="aws-agents"
MARKETPLACE="claude-plugins-official"
PLUGIN_ID="${PLUGIN}@${MARKETPLACE}"
CACHE_DIR="${HOME}/.claude/plugins/cache/${MARKETPLACE}/${PLUGIN}"

# Resolve the project root from the hook payload when available, else cwd.
PAYLOAD="$(cat 2>/dev/null || true)"
PROJECT_DIR="$(printf '%s' "$PAYLOAD" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -z "$PROJECT_DIR" ] && PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

enabled_in() {
  # Enabled if the key is literally true, or an array (version-constraint form).
  local f="$1"
  [ -f "$f" ] || return 1
  jq -e --arg id "$PLUGIN_ID" '
    .enabledPlugins[$id] as $v
    | ($v == true) or ($v | type == "array")
  ' "$f" >/dev/null 2>&1
}

is_enabled=1
for f in "${PROJECT_DIR}/.claude/settings.json" \
         "${PROJECT_DIR}/.claude/settings.local.json" \
         "${HOME}/.claude/settings.json"; do
  if enabled_in "$f"; then is_enabled=0; break; fi
done

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

CONTEXT="The AWS documentation MCP (${PLUGIN_ID}) is ${reason}.

This project's agentcore-compliance-ops skill requires an authoritative
documentation source. Amazon Bedrock AgentCore changes frequently — model IDs,
service quotas, API shapes, IAM resource formats and enforcement dates all move.
Answering from recalled detail produces confidently wrong infrastructure code.

Before generating or reviewing any AgentCore code this session, install it:

  claude plugin install ${PLUGIN_ID} --scope project

Then run /reload-plugins. The MCP tools appear as
mcp__plugin_aws-agents_awsknowledge__aws___search_documentation and
...___read_documentation — load them with ToolSearch before use.

If the user declines, say so plainly and flag that AgentCore specifics in your
output are unverified against current documentation."

jq -nc \
  --arg ctx "$CONTEXT" \
  --arg msg "AWS docs MCP (${PLUGIN_ID}) ${reason} — AgentCore guidance this session will be unverified." \
  '{
     systemMessage: $msg,
     hookSpecificOutput: {
       hookEventName: "SessionStart",
       additionalContext: $ctx
     }
   }'

exit 0
