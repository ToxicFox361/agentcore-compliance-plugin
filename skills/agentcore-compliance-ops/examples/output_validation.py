"""Deterministic validation of a compliance agent's output.

Runs AFTER the model responds and BEFORE a human sees the result. These are the
rules the model cannot override — the difference between a proposal a reviewer
can trust and one they must independently re-derive.

See references/control-stack.md for the full control stack. This file implements
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

# Schemas are versioned alongside prompts (control-stack.md, layer 3): a stored
# output is only interpretable against the schema in force when it was produced.
# Bump this whenever a field is added, removed, or changes meaning, and carry it
# on the audit record next to the prompt version, model ID and inference
# parameters — see examples/agent_template.py. Without it, a re-read of a
# six-month-old assessment silently applies today's field semantics to
# yesterday's output, and a QA sample or an examiner cannot tell that the
# schema moved underneath the record.
SCHEMA_VERSION = "alert-triage-v1"

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

# The bounded-assertion model from references/control-stack.md layer 2, expressed
# as an enum the validator can actually enforce. GAP is deliberately absent:
# gaps have their own field, so a "gap" arriving as a red flag is a
# mis-classification worth blocking rather than accepting.
#
# The failure mode this closes: a free-text red flag is how a legal conclusion
# re-enters an output whose schema was supposed to make that impossible.
# `red_flags: ["the customer is laundering money"]` — the exact sentence
# control-stack.md names as the excluded case — used to pass every check and route
# clean, because `isinstance(list)` was the only assertion made about the field.
# control-stack.md requires violations to be *detectable*, not merely discouraged,
# and a prompt instruction ("you may NOT reach legal conclusions") is a request.
# This is the part that is a control.
VALID_RED_FLAG_KINDS = {"OBSERVATION", "CONSISTENCY_NOTE"}

MAX_RATIONALE_WORDS = 120


@dataclass
class ValidationResult:
    """Outcome of validating one model response."""
    passed: bool
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    forced_recommendation: Recommendation | None = None

    def to_audit_record(self) -> dict[str, Any]:
        # The schema version travels with the verdict, not just with the output.
        # A stored "passed: true" is a claim about which rules ran, and those
        # rules change — an assertion that validation passed is uninterpretable
        # without knowing which schema it passed against.
        return {
            "schema_version": SCHEMA_VERSION,
            "passed": self.passed,
            "blocking_errors": self.blocking_errors,
            "warnings": self.warnings,
            "forced_recommendation": self.forced_recommendation,
        }


def validate_red_flags(red_flags: Any) -> list[str]:
    """Structural validation of the red_flags entries themselves. Blocking.

    Blocking rather than a warning, because a red flag is the field that carries
    the model's assertions about the subject. A warning would let the assertion
    reach the reviewer with a note attached, and notes are read as commentary
    while the sentence is read as a finding.

    Three properties, each closing a distinct route back to free text:

      * dict, not str — a bare string has no `kind` slot at all, so there is
        nowhere for the bounded-assertion model to be checked. This is the
        canonical violation and the reason this function exists.
      * `kind` in VALID_RED_FLAG_KINDS — blocks a well-formed object that simply
        declares its own category, e.g. `kind: "LEGAL_CONCLUSION"`. A model
        inventing a category is not an error the schema can absorb.
      * `evidence_id` a string — makes citation grounding checkable at all.
        check_citation_grounding below can only compare what exists.
    """
    errors: list[str] = []

    # A non-list red_flags is already reported by the LIST_FIELDS check in
    # validate_schema; do not report it twice.
    if not isinstance(red_flags, list):
        return errors

    for i, flag in enumerate(red_flags):
        if not isinstance(flag, dict):
            errors.append(
                f"red_flags[{i}] must be an object with statement/kind/"
                f"evidence_id, got {type(flag).__name__}: {flag!r}"
            )
            continue

        statement = flag.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            errors.append(
                f"red_flags[{i}].statement {statement!r} must be a non-empty string"
            )

        kind = flag.get("kind")
        if kind not in VALID_RED_FLAG_KINDS:
            errors.append(
                f"red_flags[{i}].kind {kind!r} not in "
                f"{sorted(VALID_RED_FLAG_KINDS)}"
            )

        cite = flag.get("evidence_id")
        if not isinstance(cite, str):
            errors.append(
                f"red_flags[{i}].evidence_id must be a string, got "
                f"{type(cite).__name__}"
            )

    return errors


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

    errors += validate_red_flags(output.get("red_flags"))

    # Type before length. `output.get("rationale", "")` looks safe and is not:
    # the default only applies when the key is ABSENT, so a model emitting
    # `"rationale": null` supplies the key, gets None back, and `.split()`
    # raises AttributeError straight out of validate_schema and validate() —
    # crashing the pipeline on exactly the malformed response this file exists
    # to convert into a blocking error routed to human review. An unhandled
    # exception is not a fail-safe: the caller sees a 500, not a case needing a
    # reviewer.
    rationale = output.get("rationale")
    if not isinstance(rationale, str):
        errors.append(f"rationale {rationale!r} must be a string")
    elif len(rationale.split()) > MAX_RATIONALE_WORDS:
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

    blocks.extend(_threshold_block(evidence))

    # Escalate rather than reject: the correct disposition is a human's to make.
    return blocks, ("STEP_UP_AUTH" if blocks else None)


def _threshold_block(evidence: dict[str, Any]) -> list[str]:
    """The amount-versus-threshold comparison, with currency treated as required.

    This check used to compare two bare numbers:

        if Decimal(str(amount)) >= Decimal(str(threshold)): ...

    On a single-currency book that is correct. On a multi-currency one it is a
    categorical control that silently does not apply: 8,000 USD against a 10,000
    EUR threshold reads as under-threshold, the block never fires, and the case
    auto-approves on a comparison that meant nothing. Nothing in the output looks
    wrong, which is why it survives review.

    Three rules, and the last one is the point:

      1. Same currency — compare, as before.
      2. Different currencies — use a conversion the CALLER supplied, together
         with the rate and its as-of timestamp.
      3. Different currencies and no conversion supplied, or a currency missing
         altogether — **block**, and record it as a blocking error.

    Rule 3 is the change. Skipping the comparison is the old behaviour and it
    fails open on exactly the case the control exists for.

    Conversion deliberately does not happen here. An FX rate is a data
    dependency and an audit input: the rate used and its as-of date belong on the
    decision record, because a threshold decision is only reconstructable if you
    know what rate produced it. A validator that quietly fetches a rate makes its
    own input invisible, and makes the same case decide differently on Tuesday
    than on Monday with no record of why. A bare number is not an amount.
    """
    blocks: list[str] = []

    threshold = evidence.get("auto_approve_threshold")
    amount = evidence.get("transaction_amount")
    if threshold is None or amount is None:
        return blocks  # nothing asserted about amount; not this check's business

    t_ccy = evidence.get("auto_approve_threshold_currency")
    a_ccy = evidence.get("transaction_currency")

    if not t_ccy or not a_ccy:
        blocks.append(
            "amount or threshold supplied without a currency — comparison "
            "refused, APPROVE blocked"
        )
        return blocks

    if t_ccy == a_ccy:
        if Decimal(str(amount)) >= Decimal(str(threshold)):
            blocks.append(
                f"amount {amount} {a_ccy} at or above auto-approve threshold "
                f"{threshold} {t_ccy}"
            )
        return blocks

    # Currencies differ. Require a pre-converted amount plus its provenance.
    converted = evidence.get("transaction_amount_in_threshold_currency")
    rate = evidence.get("fx_rate")
    rate_as_of = evidence.get("fx_rate_as_of")

    if converted is None or rate is None or not rate_as_of:
        blocks.append(
            f"amount is {a_ccy} and threshold is {t_ccy} with no supplied "
            f"conversion (need transaction_amount_in_threshold_currency, "
            f"fx_rate and fx_rate_as_of) — comparison refused, APPROVE blocked"
        )
        return blocks

    if Decimal(str(converted)) >= Decimal(str(threshold)):
        blocks.append(
            f"amount {amount} {a_ccy} = {converted} {t_ccy} at rate {rate} "
            f"as of {rate_as_of}, at or above auto-approve threshold "
            f"{threshold} {t_ccy}"
        )
    return blocks


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
        else:
            # An `if isinstance(...)` with no `else` is a grounding check that
            # skips precisely the entries it should reject: a bare string has no
            # evidence_id to look up, so the loop body was unreachable and the
            # unciteable claim passed through unremarked. validate_red_flags
            # blocks this shape earlier, but this function is also callable on
            # its own — a check that only holds because something upstream held
            # is not a check.
            warnings.append(
                f"red_flags[{i}] is not an object and carries no citation: "
                f"{flag!r}"
            )
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
    # standing quality sampling — see control-stack.md, "The graduation question".
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
