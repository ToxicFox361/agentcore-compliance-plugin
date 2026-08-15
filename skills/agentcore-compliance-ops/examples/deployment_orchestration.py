"""Deploying a per-tenant agent runtime from a control-plane Lambda.

The ordering below is the whole point. Getting it wrong orphans live runtimes
that bill indefinitely while being invisible to your own platform.

See references/production-rules.md §4 and §5.
"""

import os
import time

import boto3

REGION = os.environ["AWS_REGION"]
agentcore = boto3.client("bedrock-agentcore-control", region_name=REGION)
agentcore_runtime = boto3.client("bedrock-agentcore", region_name=REGION)

READY_POLL_SECONDS = 5
READY_MAX_ATTEMPTS = 60


class PlaceholderError(RuntimeError):
    """Generated code still contains an unsubstituted placeholder."""


def assert_no_placeholders(source: str, *, allow: set[str] | None = None) -> None:
    """Fail the build rather than deploy code that cannot import.

    A template declaring REGION = 'REGION_VALUE' against a substitution map with
    no REGION_VALUE entry ships the literal string to production. boto3 rejects
    it at client construction — before any handler runs — and the only symptom
    is "Runtime initialization time exceeded", which reads like a cold-start or
    capacity problem (§5).

    Bites hardest with user-supplied templates, where the template author and
    the substitution map are maintained by different people.
    """
    import re
    found = {m for m in re.findall(r"\b[A-Z][A-Z0-9_]*_VALUE\b", source)}
    if allow:
        found -= allow
    if found:
        raise PlaceholderError(
            f"unsubstituted placeholders in generated agent code: {sorted(found)}"
        )


def wait_until_ready(agent_runtime_id: str) -> str:
    """Poll until the runtime reports READY."""
    for attempt in range(1, READY_MAX_ATTEMPTS + 1):
        status = agentcore.get_agent_runtime(
            agentRuntimeId=agent_runtime_id
        )["status"]
        print(f"attempt {attempt}/{READY_MAX_ATTEMPTS}: status={status}")
        if status == "READY":
            return status
        # Documented terminal failure states. The full set is
        # CREATING | CREATE_FAILED | UPDATING | UPDATE_FAILED | READY | DELETING;
        # treat anything unexpected as a reason to stop polling, not to loop.
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}:
            raise RuntimeError(f"runtime {agent_runtime_id} entered {status}")
        time.sleep(READY_POLL_SECONDS)
    raise TimeoutError(f"runtime {agent_runtime_id} not READY after "
                       f"{READY_MAX_ATTEMPTS * READY_POLL_SECONDS}s")


def smoke_test(agent_runtime_arn: str) -> bool:
    """Diagnostic only. Must never gate persistence."""
    try:
        agentcore_runtime.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId="deployment-smoke-test-" + "0" * 20,  # ≥33 chars
            payload=b'{"message": "healthcheck"}',
            contentType="application/json",
        )
        return True
    except Exception as e:
        print(f"smoke test failed (non-fatal): {type(e).__name__}: {e}")
        return False


def deploy_agent(tenant_id: str, config: dict, source: str,
                 s3_key: str, agent_table) -> dict:
    """Deploy an agent runtime for a tenant.

    ORDER MATTERS — persist before verify.

    A pipeline that runs create → wait ready → smoke test → persist will orphan
    the runtime whenever the smoke test is slow to fail. Each failing invoke can
    burn a full client read timeout; three retries plus build time exceeds a
    15-minute Lambda ceiling, and the function is killed before writing
    anything. The runtime is live, billing, and unknown to your platform (§4).

    The general rule: make the deliverable durable before doing anything slow
    and failure-prone.
    """
    assert_no_placeholders(source)

    # 1 ── create
    resp = agentcore.create_agent_runtime(
        agentRuntimeName=_runtime_name(tenant_id),
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": os.environ["CODE_BUCKET"], "prefix": s3_key}},
            "runtime": "PYTHON_3_13",
            "entryPoint": ["main.py"],
        }},
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=os.environ["AGENT_ROLE_ARN"],
        # NOTE: metadataConfiguration is NOT a CreateAgentRuntime parameter —
        # see ensure_mmdsv2 below. Passing it here raises ParamValidationError
        # before the request is even sent.
        tags={"tenantId": tenant_id},
    )
    runtime_id = resp["agentRuntimeId"]
    runtime_arn = resp["agentRuntimeArn"]

    # 2 ── wait for READY
    wait_until_ready(runtime_id)

    # 3 ── PERSIST. Before anything slow. The runtime is confirmed READY, so
    #      the record is truthful; everything after this is diagnostic.
    agent_table.put_item(Item={
        "tenantId": tenant_id,
        "agentRuntimeId": runtime_id,
        "agentRuntimeArn": runtime_arn,
        "status": "READY",
        "modelId": config["modelId"],
        "deployedAt": _now_iso(),
    })

    # 4 ── assert MMDSv2, remediating if needed. After the persist for the same
    #      reason as everything else here: the runtime already exists and must
    #      be tracked whether or not this call succeeds.
    mmdsv2 = ensure_mmdsv2(runtime_id)

    # 5 ── verify, wrapped so it cannot throw
    try:
        test_passed = smoke_test(runtime_arn)
    except Exception as e:
        print(f"smoke test errored: {e}")
        test_passed = False

    return {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArn": runtime_arn,
        "mmdsV2Enabled": mmdsv2,
        "smokeTestPassed": test_passed,
    }


def ensure_mmdsv2(agent_runtime_id: str) -> bool:
    """Assert the runtime requires MMDSv2, enabling it if not.

    Since 2026-06-30 AgentCore Runtime rejects invocations to runtimes that do
    not require MMDSv2 — `InvokeAgentRuntime` (and ExecuteCommand, GetAgentCard,
    the WebSocket stream) fail with a ValidationException reading "This runtime
    is not MMDSv2-enabled". That error names a property most people have never
    configured, on a runtime that reports READY.

    The trap: `requireMMDSV2` lives in `metadataConfiguration`, which
    UpdateAgentRuntime accepts and **CreateAgentRuntime does not**. There is no
    way to set it at creation, so a deploy path that only ever calls create has
    no way to express the requirement and no reason to suspect a gap. Newly
    created runtimes are expected to satisfy it by default — assert rather than
    assume, because the cost of asserting is one control-plane read and the cost
    of assuming is a runtime that deploys clean and cannot be invoked.

    https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html
    https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html
    """
    current = agentcore.get_agent_runtime(agentRuntimeId=agent_runtime_id)
    if current.get("metadataConfiguration", {}).get("requireMMDSV2") is True:
        return True

    print(f"runtime {agent_runtime_id} is not MMDSv2-enabled; enabling")
    # UpdateAgentRuntime requires the artifact and role as well — echo the
    # runtime's current values rather than reconstructing them, or the update
    # silently rewrites the runtime to whatever you happened to pass.
    agentcore.update_agent_runtime(
        agentRuntimeId=agent_runtime_id,
        agentRuntimeArtifact=current["agentRuntimeArtifact"],
        roleArn=current["roleArn"],
        networkConfiguration=current["networkConfiguration"],
        metadataConfiguration={"requireMMDSV2": True},
    )
    return True


def _runtime_name(tenant_id: str) -> str:
    from datetime import datetime, timezone
    safe = tenant_id.replace("-", "_").replace(".", "_")[:20]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"agentcore_{safe}_{stamp}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Teardown ─────────────────────────────────────────────────────────────────
#
# DeleteAgentRuntime also tears down the runtime's workload identity, so the
# caller needs bedrock-agentcore:DeleteWorkloadIdentity as well. Without it the
# whole delete fails and the runtime keeps billing — see examples/iam_policies.py.
#
# Guard any destructive operation with an explicit account assertion. A wrong
# profile on a destroy is unrecoverable:
#
#   ACC=$(aws sts get-caller-identity --query Account --output text)
#   [ "$ACC" != "$EXPECTED" ] && { echo "ABORT: wrong account $ACC"; exit 1; }
#
# And give concurrent CDK operations separate synth directories, or they
# collide on cdk.out (§14).
