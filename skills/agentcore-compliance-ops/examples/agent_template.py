"""Reference agent entrypoint for a compliance workflow on AgentCore Runtime.

Every construct here exists because its absence caused a production failure.
Cross-references are to references/production-rules.md.

Adapt freely — but understand each guard before removing it.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotocoreConfig
from bedrock_agentcore import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

app = BedrockAgentCoreApp(debug=True)

REGION = os.environ["AWS_REGION"]

# Deployment-time configuration. If your platform generates this file from a
# template, assert that no placeholder survives substitution before packaging —
# an unsubstituted value reaches boto3 and kills the container at import, which
# surfaces only as "Runtime initialization time exceeded" (§5).
MODEL_ID = os.environ["MODEL_ID"]              # e.g. eu.amazon.nova-2-lite-v1:0
TENANT_ID = os.environ["TENANT_ID"]
QUEUE_URL = os.environ["USAGE_QUEUE_URL"]
SYSTEM_PROMPT = os.environ["SYSTEM_PROMPT"]
GATEWAY_URL = os.environ["GATEWAY_URL"]        # AgentCore Gateway MCP endpoint

# ── Inference parameters: state them, never inherit them ─────────────────────
#
# Bedrock's InferenceConfiguration has exactly four members — maxTokens,
# stopSequences, temperature and topP. Anything else a model supports (top_k
# among them) travels in additionalModelRequestFields instead. Strands builds
# inferenceConfig from a comprehension that drops every key whose value is
# None, so a parameter left unset is not sent as some neutral value: it is not
# sent at all, and the vendor's own default applies. That default is
# model-specific, which makes a model-ID swap a change to sampling behaviour as
# well as to prompt behaviour — the same failure family as §16 and §17 (§24).
#
# Quota is reserved per request as input_tokens + max_tokens. Unset means the
# model's maximum — tens of thousands of tokens — so you throttle at a fraction
# of real capacity. Size to the expected response (§8).
MAX_TOKENS = 2048

# Temperature 0 is greedy decoding. It is NOT a reproducibility guarantee:
# InferenceConfiguration has no seed member, so nothing is held fixed between
# runs and an identical request can still come back different. It narrows
# variance; it does not make a run replayable. Reconstructing a compliance
# decision therefore rests on the audit record — inputs, model ID and the
# parameters below — plus the deterministic validation that runs after
# generation (examples/output_validation.py), never on the model repeating
# itself. Claiming determinism you do not have is worse than admitting the
# gap, because the control everyone believes is present is not.
TEMPERATURE = 0.0

# Set temperature OR topP, not both: they truncate the same distribution and
# interact unhelpfully. AWS documents this for Anthropic models on Bedrock
# ("modify either temperature or top_p. Do not modify both at the same time"),
# and Claude Sonnet 4.5 / Haiku 4.5 reject both being specified at once.
#
# Left None deliberately, which is not the same as forgetting it: topP then
# resolves to this model's default. Pinning MODEL_ID on the record is what
# makes that unset half determinate — which is why the two are recorded
# together below.
TOP_P = None

# Part of the output contract rather than a formatting detail — an empty list
# is a stated choice, and it is sent as such rather than dropped.
STOP_SEQUENCES: list[str] = []

# Exactly what goes on the wire, mirroring the framework's own drop-if-None
# rule, so the decision record states what was sent rather than what was
# intended. Anything absent here was chosen by the vendor, not by us (§24).
# Note `is not None` rather than a truthiness test: temperature 0.0 is falsy,
# and a truthy filter would silently drop the setting most likely to be chosen
# deliberately — from the request as well as from the record.
INFERENCE_PARAMETERS = {
    key: value
    for key, value in (
        ("maxTokens", MAX_TOKENS),
        ("temperature", TEMPERATURE),
        ("topP", TOP_P),
        ("stopSequences", STOP_SEQUENCES),
    )
    if value is not None
}

# Callers typically sit behind an API Gateway REST integration, which times out
# at a hard 29s. Default Bedrock retry policy can spend minutes on a retryable
# error, so the gateway gives up first and the user sees an opaque network
# error. Cap retries so failures surface inside the caller's window (§11).
# read_timeout is per-chunk on streaming responses, so a low value here does
# not truncate long generations.
BEDROCK_CLIENT_CONFIG = BotocoreConfig(
    retries={"max_attempts": 2, "mode": "standard"},
    connect_timeout=5,
    read_timeout=15,
)

sqs = boto3.client("sqs", region_name=REGION)
identity = boto3.client("bedrock-agentcore", region_name=REGION)


def _workload_token() -> str:
    """The runtime's OWN identity — a fallback, not the default.

    Use this only where the agent legitimately acts as itself. If Gateway
    policy distinguishes analysts from supervisors (it should), this path hands
    every caller the same entitlements and quietly erases that distinction.
    Prefer a token minted from the end user's JWT.
    """
    return identity.get_workload_access_token(
        workloadName=os.environ["WORKLOAD_NAME"]
    )["workloadAccessToken"]


# ── Tool exposure is the control ─────────────────────────────────────────────
#
# Least privilege for an agent is the list of tools the model is OFFERED, not
# what the prompt tells it to avoid. A system prompt saying "do NOT call
# close_alert" does not stop the model calling close_alert: it is a request,
# competing with everything else in the context, and it loses to a sufficiently
# persuasive input, a long conversation, or an ordinary misread. Every incident
# of this shape ends with someone pointing at the prompt line that was supposed
# to prevent it.
#
# A tool absent from this list cannot be called. There is no phrasing that
# reaches it. That is the difference between a control and an instruction.
#
# So: this agent reads. Anything that writes — closing an alert, filing a
# report, changing a rating — is not in the list, and the deterministic write
# paths live in code that runs after a human decides (see
# references/guardrails.md). Cedar policy at the Gateway is the second layer
# that catches a mistake here; IAM is the third. The prompt is not a layer.
#
# Names are as the AgentCore Gateway presents them — `<targetName>___<toolName>`
# — because that is what the model is offered. See examples/cedar_policies.md §1.
ALLOWED_TOOL_NAMES = frozenset({
    "case-read___get_case",
    "case-read___list_alerts",
    "customer-read___get_customer_profile",
    "customer-read___get_transaction_history",
    "screening-read___get_screening_results",
})


# ── The payload is the one path with no model and no guardrail in it ─────────
#
# `payload` is parsed from arbitrary JSON the caller controls. Every other
# control in this file sits BEHIND the model: the system prompt shapes what it
# does, the tool allow-list above bounds what it can reach, Cedar policy checks
# the call at the Gateway. All of them assume the model is in the path.
#
# A non-string message is how a caller steps out of that path. If the value is
# a list of content blocks — particularly a `toolUse` block — a framework that
# accepts structured content dispatches the named tool immediately: no model
# reasoning, no system prompt, no guardrail evaluated. The request reaches a
# tool by a route none of the controls above are watching. AWS documents this
# as a real attack, not a theoretical one — see
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html
#
# So the type check IS the control, and it belongs before the agent loop rather
# than inside it.
#
# Note what does not work: `payload.get("prompt", "")` supplies a default but
# validates nothing — whenever the key is present, the caller's dict or list is
# returned unchanged. A default is not a type check.
#
# This entrypoint is single-turn by design, so it refuses caller-supplied
# conversation history outright. If you adapt it to accept history, that is the
# other half of the same attack and needs the other half of the guard: strip
# every `toolUse` and `toolResult` content block from the caller's messages
# before they reach the agent. A `toolUse` block sitting in the history
# executes on the next event-loop turn exactly as one in the prompt does.
#
# A Pydantic model declaring `prompt: str` is the idiomatic form of this check.
# The explicit guard below keeps this file dependency-free and shows what is
# actually being asserted.

HISTORY_KEYS = ("messages", "history", "conversation")


class PayloadError(ValueError):
    """The caller's payload is unusable. Returned as data, never raised out."""


def validate_payload(payload) -> str:
    """Return the caller's message, or raise PayloadError.

    Enforces the three properties the agent loop cannot enforce for itself:
    the payload is an object, it carries no caller-supplied history, and the
    message is a genuinely non-empty string rather than something merely
    truthy.
    """
    if not isinstance(payload, dict):
        raise PayloadError(
            f"payload must be a JSON object, got {type(payload).__name__}"
        )

    supplied_history = [k for k in HISTORY_KEYS if payload.get(k) is not None]
    if supplied_history:
        raise PayloadError(
            f"caller-supplied conversation history is not accepted: "
            f"{supplied_history}; this entrypoint is single-turn"
        )

    # Deliberately not `payload.get("message") or payload.get("prompt")`. That
    # form falls through on any falsy value, so a caller sending a list of
    # content blocks under "message" is silently re-read from "prompt" — and
    # worse, it accepts whatever it finds without ever checking the type.
    message = payload.get("message")
    if message is None:
        message = payload.get("prompt")

    if message is None:
        raise PayloadError("message or prompt is required")
    if not isinstance(message, str):
        raise PayloadError(
            f"message must be a string, got {type(message).__name__} — "
            f"structured content blocks are rejected here by design"
        )
    if not message.strip():
        raise PayloadError("message must be a non-empty string")

    return message


def gateway_client(access_token: str) -> MCPClient:
    """MCP client for the AgentCore Gateway fronting this agent's tools.

    The token should carry the *end user's* identity, not the runtime's, so the
    agent inherits the analyst's entitlements rather than a superset of
    everyone's — that is what makes the Gateway's Cedar policies able to
    distinguish callers at all. Obtain it through AgentCore Identity
    (GetWorkloadAccessTokenForJWT, which validates the token; not the userId
    variant, which does not — see examples/agent_runtime.tf).
    """
    return MCPClient(
        lambda: streamablehttp_client(
            GATEWAY_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        ),
        # Enforce the allow-list in the client, not after it. `tool_filters`
        # constrains what list_tools_sync can return at all, so there is no
        # window in which an unfiltered catalogue exists to be passed to an
        # Agent by mistake. Matchers may also be regexes or callables.
        #
        # This also strips the `x_amz_bedrock_agentcore_search` tool the Gateway
        # injects — deliberate here, since the point is a closed list.
        tool_filters={"allowed": sorted(ALLOWED_TOOL_NAMES)},
    )


def select_tools(catalogue: list) -> list:
    """Verify the filtered catalogue is what we expect, and return it.

    The allow-list is applied by the client above; this is the assertion that
    it produced what we intended.

    Fails loudly when an expected tool is missing rather than quietly running
    with fewer capabilities. A renamed Gateway target changes every tool name
    under it, and the allow-list then matches nothing — an agent that has
    silently lost its evidence-gathering tools still answers, just without the
    evidence, which is the worst of both outcomes.

    Note the asymmetry that makes this check worth writing: over-restriction is
    silent (the model simply never mentions the tool it does not have), while
    under-restriction is the thing everyone remembers to test for.
    """
    available = {t.tool_name for t in catalogue}
    missing = ALLOWED_TOOL_NAMES - available
    if missing:
        raise RuntimeError(
            f"expected tools absent from Gateway catalogue: {sorted(missing)}; "
            f"available: {sorted(available)}"
        )
    return list(catalogue)


def build_agent(tools: list | None = None) -> Agent:
    """Build a fresh Agent per invocation.

    Agent objects are stateful and hold a per-instance lock. A single
    module-level instance raises ConcurrencyException on every call after the
    first, and one failed request wedges the runtime permanently until
    redeploy (§3). Construction cost is negligible beside model latency.

    `tools` defaults to none at all. Strands' own docstring reads "If None, all
    tools will be available", which is easy to misread as a default toolset —
    it refers to `./tools/` directory discovery, which is off unless you set
    `load_tools_from_directory=True`. An Agent constructed without `tools` has
    an empty registry. Two other settings do add tools implicitly and are worth
    knowing about where the tool list is a control: an agentic context manager
    injects context-management tools, and a configured sandbox vends its own.
    """
    return Agent(
        model=BedrockModel(
            model_id=MODEL_ID,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            top_p=TOP_P,               # None: dropped from inferenceConfig, by
                                       # decision rather than by omission (§24)
            stop_sequences=STOP_SEQUENCES,
            boto_client_config=BEDROCK_CLIENT_CONFIG,
        ),
        system_prompt=SYSTEM_PROMPT,
        tools=tools or [],
    )


def emit_usage(input_tokens: int, output_tokens: int, total_tokens: int,
               tenant_id: str, request_id: str) -> None:
    """Emit a usage event for cost attribution.

    model_id is mandatory. Without it the aggregation layer cannot apply
    per-model rates and a tenant running several models is billed at whichever
    single rate happened to be hardcoded (§9).

    Deliberately does NOT carry prompt or response text — usage events are
    retained for billing, and customer PII should not ride along.
    """
    try:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": tenant_id,
                "model_id": MODEL_ID,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "request_id": request_id,
            }),
        )
    except Exception as e:
        # Never fail the request because metering failed.
        app.logger.error(f"usage emit failed: {type(e).__name__}: {e}")


@app.entrypoint
def invoke(payload, context):
    """Compliance workflow entrypoint.

    Returns a *proposal*, never a decision. Nothing downstream may convert this
    into a disposition without a human acting under their own identity — see
    references/guardrails.md.
    """
    # context.session_id is set by AgentCore from the runtimeSessionId the
    # caller supplied. The caller must derive that server-side, namespaced by
    # tenant — see examples/tenant_isolation.py (§7).
    #
    # The "unknown" default here is a LOG label and nothing else — it is never
    # written to a record, never returned, and never used to look anything up.
    # A placeholder standing in for a value the caller will treat as real is a
    # different and much worse thing; see examples/output_validation.py.
    session_id = getattr(context, "session_id", None) or "unknown"
    request_id = str(uuid.uuid4())

    # Validate BEFORE the agent loop, not inside it. See the section above:
    # this is the one path that reaches a tool with no model and no guardrail
    # in it, so it is the one place a type check is a control rather than
    # hygiene. Rejected payloads come back as data — an unhandled raise here
    # would surface to the operator as an opaque 500 (§11).
    try:
        user_message = validate_payload(payload)
    except PayloadError as e:
        app.logger.warning(
            f"payload rejected tenant={TENANT_ID} request={request_id}: {e}"
        )
        return {"error": str(e), "request_id": request_id}

    app.logger.info(
        f"invoke tenant={TENANT_ID} session={session_id[:12]}... request={request_id}"
    )

    # Model and runtime failures are returned as data. Letting them escape as
    # an unhandled 500 means the operator sees "Internal Server Error" instead
    # of ThrottlingException, which is the thing they need to act on (§11).
    try:
        # The tool list is assembled per request, from the Gateway, and
        # filtered before the model ever sees it. The model is never offered a
        # write tool, so no prompt can talk it into calling one.
        access_token = payload.get("access_token") or _workload_token()
        with gateway_client(access_token) as gateway:
            tools = select_tools(gateway.list_tools_sync())
            result = build_agent(tools)(user_message)
    except Exception as e:
        app.logger.error(
            f"agent invocation failed tenant={TENANT_ID} request={request_id}: "
            f"{type(e).__name__}: {e}"
        )
        return {"error": f"{type(e).__name__}: {e}", "request_id": request_id}

    if hasattr(result, "metrics"):
        usage = result.metrics.accumulated_usage
        emit_usage(
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            usage.get("totalTokens", 0),
            TENANT_ID,
            request_id,
        )

    return {
        "proposal": result.message,
        "request_id": request_id,
        "model_id": MODEL_ID,   # pinned on the record: a decision under one
                                # model is not evidence about another
        # Pinned for the same reason, and because there is no seed to fall back
        # on: the reconstruction of this decision is the record — inputs, model
        # and settings — not the hope that a re-run reproduces it (§24).
        "inference_parameters": INFERENCE_PARAMETERS,
    }


if __name__ == "__main__":
    app.run()
