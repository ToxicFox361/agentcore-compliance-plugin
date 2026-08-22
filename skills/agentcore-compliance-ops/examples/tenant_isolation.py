"""Tenant isolation for agent invocation in a multi-tenant compliance platform.

The session ID is AgentCore's isolation boundary — one microVM, filesystem and
memory per session. AWS validates a session ID's *format* but does not verify
it belongs to the caller:

    "AgentCore does not enforce session-to-user mappings — your client backend
     should maintain the relationship between users and their session IDs."

    "Treat the runtimeSessionId as a server-side value derived from the
     authenticated end user — never accept it directly from untrusted client
     input."
    -- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html

Where one backend role invokes on behalf of many tenants — the shape of every
compliance SaaS — a client that chooses its own session ID can route a request
into another tenant's microVM and read their state.

Cross-references are to references/production-rules.md §7.
"""

import hashlib
import json

import boto3

bedrock_agentcore = boto3.client("bedrock-agentcore")

# No DynamoDB client here. The table is INJECTED (`agent_table` below), which is
# what lets a caller pass a test double and what stops this module deciding which
# table it reads. A module-level `boto3.resource("dynamodb")` was declared and
# never used — harmless in itself, but a resource handle that exists and is
# unused is an invitation to start using it, which would quietly reintroduce the
# coupling the injection was there to avoid.


def resolve_tenant_from_agent(agent_runtime_arn: str, agent_table) -> str:
    """Resolve the tenant from the agent record, NOT from the request body.

    A client-supplied tenant ID is a claim, not a fact. Deriving it from the
    resource being addressed means a caller cannot assert their way into
    another tenant's data, nor dodge that tenant's usage limits.
    """
    resp = agent_table.query(
        IndexName="agentRuntimeArn-index",
        KeyConditionExpression="agentRuntimeArn = :arn",
        ExpressionAttributeValues={":arn": agent_runtime_arn},
        ProjectionExpression="tenantId",
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        raise PermissionError(f"no agent record for {agent_runtime_arn}")
    return items[0]["tenantId"]


def derive_runtime_session_id(tenant_id: str, agent_runtime_arn: str,
                              client_hint: str) -> str:
    """Build the runtimeSessionId server-side.

    The client hint is retained so a caller can continue a conversation, but it
    is namespaced under the server-resolved tenant before hashing. A forged or
    stolen hint can therefore only ever collide within the caller's own tenant —
    the blast radius of guessing is bounded to data they may already see.

    For per-analyst isolation within a tenant, add the authenticated user's
    subject claim to the namespace.
    """
    namespaced = f"{tenant_id}|{agent_runtime_arn}|{client_hint}"
    session_id = hashlib.sha256(namespaced.encode("utf-8")).hexdigest()

    # No length check, deliberately. AgentCore's documented bounds for
    # runtimeSessionId are 33-256 characters — note the MAXIMUM, which is usually
    # left out of the retelling — and a SHA-256 hex digest is always exactly 64,
    # for every possible input. The property holds by construction, so a runtime
    # check here would be asserting something about hashlib rather than
    # validating anything about the data.
    #
    # The ceiling is worth knowing anyway, because it is reachable the moment you
    # stop hashing: a readable session ID built by CONCATENATING tenant, ARN,
    # subject claim and a client string passes 256 without anyone noticing, and
    # the API then rejects the invocation for a reason that reads nothing like
    # "your session ID is too long".
    #
    # What stood here was `assert len(session_id) >= 33`, and it was worth
    # removing even though it could never fail. `python -O` strips assert
    # statements entirely, so validation written that way silently disappears in
    # an optimised deployment — and it disappears from the security-shaped checks
    # first, because those are the ones people naturally express as invariants.
    # Validate with an `if` and a raise; keep `assert` for what you are
    # documenting, not for what you are enforcing.
    return session_id


def invoke_for_tenant(agent_runtime_arn: str, user_message: str,
                      client_session_hint: str, agent_table) -> dict:
    """Invoke an agent with server-derived tenant and session binding."""
    tenant_id = resolve_tenant_from_agent(agent_runtime_arn, agent_table)

    # Passing runtimeSessionId is also what lets AgentCore reuse an existing
    # microVM. Omit it and every request provisions a fresh session, paying a
    # full cold start and risking initialization timeouts.
    runtime_session_id = derive_runtime_session_id(
        tenant_id, agent_runtime_arn, client_session_hint
    )

    response = bedrock_agentcore.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        runtimeSessionId=runtime_session_id,
        payload=json.dumps({"message": user_message}).encode("utf-8"),
        contentType="application/json",
    )

    return {
        "response": response,
        # Echo the caller's own hint, never the derived ID. Returning the
        # derived value hands clients the thing they must not be able to choose.
        "session_hint": client_session_hint,
    }


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# def invoke_WRONG(body):
#     bedrock_agentcore.invoke_agent_runtime(
#         agentRuntimeArn=body["agentId"],
#         runtimeSessionId=body["sessionId"],   # client-chosen → cross-tenant routing
#         ...
#     )
#
# def invoke_ALSO_WRONG(body):
#     tenant = body["tenantId"]                 # a claim, not a fact
#     ...
#
# def invoke_STILL_WRONG(body):
#     bedrock_agentcore.invoke_agent_runtime(
#         agentRuntimeArn=body["agentId"],      # no session ID at all:
#         ...                                   # cold start every request
#     )
