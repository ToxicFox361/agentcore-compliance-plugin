"""Deterministic validation of a compliance agent's output.

Runs AFTER the model responds and BEFORE a human sees the result. These are the
rules the model cannot override — the difference between a proposal a reviewer
can trust and one they must independently re-derive.

See references/guardrails.md for the full control stack. This file implements
layers 3 (typed output, no silent repair) and 5 (deterministic post-generation
validation).

The last two sections extend the same principle past the model's response to
the tool calls made on its behalf: a failed write must surface as a failure,
and confirming a record exists is not the same as confirming your run wrote it.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

Recommendation = Literal["APPROVE", "STEP_UP_AUTH", "REJECT"]

# Mirrors the output schema in examples/alert_triage_prompt.md, field for
# field. Keep the two in step. A field the prompt asks for but the validator
# does not require is a field the model can quietly stop producing: nothing
# fails, the reviewer sees a well-formed assessment, and the only trace is an
# absence nobody is looking for.
#
# `additional_typologies` is the one that has actually bitten. It exists to
# catch the missed-concurrent-typology failure that prompt file documents —
# account takeover identified, simultaneous structuring in the same fixture
# missed entirely — and it can only catch it if its absence is an error rather
# than a silence.
REQUIRED_FIELDS = {
    "alert_id", "recommendation", "risk_score", "confidence",
    "primary_typology", "additional_typologies", "account_takeover_suspected",
    "customer_may_be_victim", "red_flags", "mitigating_factors", "gaps",
    "escalation_recommended", "recommended_actions", "rationale",
}

LIST_FIELDS = ("additional_typologies", "red_flags", "mitigating_factors",
               "gaps", "recommended_actions")

# Strict bools. A model that emits the string "false" has not answered the
# question, and treating it as an answer is how a truthy "false" becomes an
# escalation that never happens.
BOOLEAN_FIELDS = ("account_takeover_suspected", "customer_may_be_victim",
                  "escalation_recommended")

VALID_RECOMMENDATIONS = {"APPROVE", "STEP_UP_AUTH", "REJECT"}
VALID_CONFIDENCE = {"low", "medium", "high"}

MAX_RATIONALE_WORDS = 120


@dataclass
class ValidationResult:
    """Outcome of validating one model response."""
    passed: bool
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forced_recommendation: Recommendation | None = None

    def to_audit_record(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking_errors": self.blocking_errors,
            "warnings": self.warnings,
            "forced_recommendation": self.forced_recommendation,
        }


def validate_schema(output: dict[str, Any]) -> list[str]:
    """Structural validation. Errors here mean retry, then flag for human.

    Never silently repair. A response the model could not produce correctly is
    a signal about the model, the prompt, or the input — not an inconvenience
    to paper over.
    """
    errors: list[str] = []

    missing = REQUIRED_FIELDS - output.keys()
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")

    rec = output.get("recommendation")
    if rec not in VALID_RECOMMENDATIONS:
        errors.append(f"recommendation {rec!r} not in {sorted(VALID_RECOMMENDATIONS)}")

    if output.get("confidence") not in VALID_CONFIDENCE:
        errors.append(f"confidence {output.get('confidence')!r} invalid")

    score = output.get("risk_score")
    if not isinstance(score, int) or not 0 <= score <= 100:
        errors.append(f"risk_score {score!r} must be an integer 0-100")

    alert_id = output.get("alert_id")
    if not isinstance(alert_id, str) or not alert_id.strip():
        errors.append(f"alert_id {alert_id!r} must be a non-empty string")

    for list_field in LIST_FIELDS:
        if not isinstance(output.get(list_field), list):
            errors.append(f"{list_field} must be a list")

    for bool_field in BOOLEAN_FIELDS:
        if not isinstance(output.get(bool_field), bool):
            errors.append(
                f"{bool_field} {output.get(bool_field)!r} must be a boolean"
            )

    rationale = output.get("rationale", "")
    if len(rationale.split()) > MAX_RATIONALE_WORDS:
        errors.append(f"rationale exceeds {MAX_RATIONALE_WORDS} words")

    return errors


def check_categorical_blocks(output: dict[str, Any],
                             evidence: dict[str, Any]) -> tuple[list[str], Recommendation | None]:
    """Policy expressed in code, not prompt.

    Certain fact patterns must block a clear/low-friction recommendation
    regardless of what the model concluded or how confident it was. Model
    confidence is not evidence, and a high-confidence clear on a blocked
    pattern is a defect in the pipeline rather than a judgement to respect.

    Tune these to your obligations — they are illustrative, not exhaustive.
    """
    blocks: list[str] = []

    if output.get("recommendation") != "APPROVE":
        return blocks, None

    if evidence.get("sanctions_hit"):
        blocks.append("sanctions hit present — APPROVE blocked")
    if evidence.get("prior_filed_report"):
        blocks.append("prior filed report on this subject — APPROVE blocked")
    if evidence.get("pep_status") in {"domestic", "foreign", "international"}:
        blocks.append("PEP status present — APPROVE blocked")

    threshold = evidence.get("auto_approve_threshold")
    amount = evidence.get("transaction_amount")
    if threshold is not None and amount is not None and Decimal(str(amount)) >= Decimal(str(threshold)):
        blocks.append(f"amount {amount} at or above auto-approve threshold {threshold}")

    # Escalate rather than reject: the correct disposition is a human's to make.
    return blocks, ("STEP_UP_AUTH" if blocks else None)


def check_internal_consistency(output: dict[str, Any]) -> list[str]:
    """Catch outputs that contradict themselves.

    A recommendation inconsistent with its own stated findings is a defect you
    can detect mechanically — and one a reviewer under time pressure will miss,
    because the prose reads fluently.
    """
    warnings: list[str] = []

    rec = output.get("recommendation")
    red_flags = output.get("red_flags") or []
    mitigating = output.get("mitigating_factors") or []
    score = output.get("risk_score", 0)

    if rec == "APPROVE" and len(red_flags) >= 3:
        warnings.append(f"APPROVE with {len(red_flags)} red flags")
    if rec == "APPROVE" and isinstance(score, int) and score >= 70:
        warnings.append(f"APPROVE with risk_score {score}")
    if rec == "REJECT" and isinstance(score, int) and score <= 30:
        warnings.append(f"REJECT with risk_score {score}")

    # An assessment with no mitigating factors is usually an incomplete review
    # rather than a genuinely one-sided case. Requiring the field makes
    # one-sidedness visible instead of invisible.
    if not mitigating:
        warnings.append("no mitigating factors listed — review may be incomplete")

    if not output.get("gaps"):
        warnings.append("no evidence gaps identified — unusual for a real case")

    return warnings


def check_citation_grounding(output: dict[str, Any],
                             evidence_ids: set[str]) -> list[str]:
    """Every cited fact must trace to supplied evidence.

    Flag unciteable claims rather than displaying them as fact. Fluent,
    unsupported assertions are the characteristic failure of narrative
    generation, and they read as confident.
    """
    warnings: list[str] = []
    for i, flag in enumerate(output.get("red_flags") or []):
        if isinstance(flag, dict):
            cite = flag.get("evidence_id")
            if cite and cite not in evidence_ids:
                warnings.append(f"red_flags[{i}] cites unknown evidence {cite!r}")
            elif not cite:
                warnings.append(f"red_flags[{i}] has no evidence citation")
    return warnings


def validate(output: dict[str, Any], evidence: dict[str, Any],
             evidence_ids: set[str] | None = None) -> ValidationResult:
    """Full validation pass. Call before any human sees the output."""
    blocking = validate_schema(output)
    if blocking:
        # Structural failure — do not attempt semantic checks on a malformed
        # object. Retry bounded, then flag for human.
        return ValidationResult(passed=False, blocking_errors=blocking)

    block_reasons, forced = check_categorical_blocks(output, evidence)
    warnings = check_internal_consistency(output)
    if evidence_ids is not None:
        warnings += check_citation_grounding(output, evidence_ids)

    return ValidationResult(
        passed=not block_reasons,
        blocking_errors=block_reasons,
        warnings=warnings,
        forced_recommendation=forced,
    )


# ── Deterministic routing ────────────────────────────────────────────────────

def route(output: dict[str, Any], validation: ValidationResult) -> str:
    """Decide what happens next. Pure code — no model involved.

    This is the property that makes the pipeline defensible: the model
    contributes an assessment, and code makes the routing call.

    Fail-safe in every direction. There is no path from a malformed, blocked or
    low-confidence response to an unattended clear.
    """
    if validation.blocking_errors:
        return "HUMAN_REVIEW"
    if validation.forced_recommendation:
        return "HUMAN_REVIEW"
    if output.get("confidence") == "low":
        return "HUMAN_REVIEW"
    if len(validation.warnings) >= 2:
        return "HUMAN_REVIEW"

    # Even a clean APPROVE goes to a human in a supervised deployment.
    # Only open an unattended path after measured reliability evidence and with
    # standing quality sampling — see guardrails.md, "The graduation question".
    return "HUMAN_REVIEW"


# ── Tool results are output too ──────────────────────────────────────────────

class ToolCallError(RuntimeError):
    """A tool call failed. Carries no result, because there is no result."""


def record_case_note(client, case_id: str, body: str) -> str:
    """Write a case note and return its ID.

    The rule: a function that promises an identifier either returns a real one
    or raises. It never returns something shaped like an answer.
    """
    try:
        resp = client.create_case_note(caseId=case_id, body=body)
    except Exception as e:
        # Re-raise with context. Do NOT return a partial dict — see below.
        raise ToolCallError(
            f"create_case_note failed for case {case_id}: {type(e).__name__}: {e}"
        ) from e

    note_id = resp.get("noteId")
    if not note_id:
        # The call "succeeded" but did not give us the thing that proves it.
        # Treat that as a failure too — a response missing its identifier is
        # not evidence a note exists.
        raise ToolCallError(
            f"create_case_note returned no noteId for case {case_id}: {resp!r}"
        )
    return note_id


# The observed defect, kept because the shape is so easy to reintroduce:
#
#   def record_case_note_WRONG(client, case_id, body):
#       try:
#           resp = client.create_case_note(caseId=case_id, body=body)
#           return {"note_id": resp["noteId"]}
#       except Exception as e:
#           log.error(f"write failed: {e}")
#           return {}                       # ← no note_id key
#
#   note_id = record_case_note_WRONG(...).get("note_id", "unknown")
#
# Every individual line is defensible. Together they convert a failed write into
# a successful-looking one: the caller gets the string "unknown", writes it to
# the audit record as though it were an identifier, and reports success. Nothing
# raises, nothing alarms, and the case note does not exist.
#
# A placeholder that reads like data is worse than an exception, because an
# exception stops the pipeline while a placeholder propagates. "unknown",
# "N/A", "", 0 and {} are all the same defect wearing different clothes.
#
# This matters more in compliance than elsewhere: the audit trail is the
# deliverable. A record asserting that a note was filed, referencing an
# identifier that was never issued, is worse than no record — it is a false
# statement about a control having operated, and it will be believed.
#
# Errors as data (see examples/agent_template.py) is a different thing and still
# correct: a *typed error value the caller must handle* is not a placeholder
# silently occupying the slot where a real value belongs.


# ── Verification: existence is not attribution ───────────────────────────────
#
# When confirming that an agent run did what it claimed, "the record is there"
# is a weaker check than it looks. It answers "does a matching record exist",
# not "did this run create it". Backfills, retries, a colleague's manual entry,
# a previous test, or an unrelated service can all satisfy it.
#
# Check both halves:
#
#   1. The record exists, with the expected content and identifier.
#   2. The target actually ran, in the run window, the expected number of times.
#      For a Lambda-backed Gateway target, that is the Invocations metric:
#
#        aws cloudwatch get-metric-statistics \
#          --namespace AWS/Lambda --metric-name Invocations \
#          --dimensions Name=FunctionName,Value=<target-function> \
#          --start-time <run-start> --end-time <run-end> \
#          --period 300 --statistics Sum
#
# Zero invocations plus a present record means something other than your agent
# wrote it — which is exactly the situation where you would otherwise conclude
# the pipeline works and move on. Non-zero invocations plus an absent record is
# the inverse and equally worth knowing.
#
# The same asymmetry applies to a policy denial: assert on the specific denial
# signature, not on "the call failed" — see examples/cedar_policies.md §8.
