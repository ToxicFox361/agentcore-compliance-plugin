"""Golden-set harness: two tiers, and the distinction between them is the point.

This skill specifies fixture design, the bias-probe protocol, and "run at
production's inference parameters and record them with the results". It gates
graduation on *measured* reliability against the golden set, and lists shipping
without one as an anti-pattern. It has never shipped a runner.

TIER 1 -- EVIDENCE SUPPLIED INLINE. The fixture carries a complete evidence set
in the prompt. Measures prompt-and-model properties: schema conformance, verdict
against an acceptable set, and the specific trap each fixture was built to catch.
Cheap, fast, needs no infrastructure. Run it on every prompt edit.

TIER 2 -- EVIDENCE RETRIEVED BY THE AGENT. The production agent receives a
workflow template with interpolation points and retrieves evidence itself through
scope-restricted tool calls. So the evidence set becomes a function of the
agent's own behaviour: two runs of the same alert can legitimately see different
evidence because one queried the due-diligence table and the other did not.

THE CONSEQUENCE, WHICH IS WHY THIS FILE HAS TWO TIERS RATHER THAN ONE RUNNER:
disposition accuracy and retrieval completeness become two separate
measurements, and a tier-1 score is an **upper bound on the retrieval-free
component**. It will overstate production accuracy, because it hands the model a
complete evidence set for free -- the one thing production never does. A fixture
here is a *seeded data state plus an expected retrieval set plus an expected
disposition band*, not a JSON blob and an answer.

The tier is declared on the fixture and asserted on comparison, so a tier-1 score
cannot be quietly compared against a tier-2 score. That comparison is the most
plausible way this harness gets misread, and it flatters in the wrong direction.

WHAT IS DELIBERATELY NOT HERE. No model client. The harness takes an `invoke`
callable so it runs against Converse, against a deployed runtime, or against a
recorded fixture set without changing. A harness that owns its transport ends up
measuring the transport.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)


# ── Outcomes ─────────────────────────────────────────────────────────────────


class Outcome(str, Enum):
    """Why a pass did not pass. Schema failure is NOT a wrong answer.

    Collapsing these is the most common defect in an agent evaluation. A response
    that did not parse and a response that parsed and reached the wrong
    disposition need different fixes -- prompt structure versus reasoning -- and
    averaging them produces a number that directs neither.
    """

    SCORED = "SCORED"
    """Parsed, validated, graded."""

    SCHEMA_FAILED = "SCHEMA_FAILED"
    """Did not satisfy the output contract. Scores zero, counted separately.

    An unparseable output is not a partially correct answer.
    """

    TRUNCATED = "TRUNCATED"
    """Stopped at the token cap.

    Distinct from SCHEMA_FAILED on purpose: without `stopReason` a truncated
    answer is misdiagnosed as a schema problem, and the fix for one (raise
    maxTokens) is unrelated to the fix for the other (restructure the prompt).
    """

    BLOCKED = "BLOCKED"
    """Refused or content-filtered. A platform event, not a quality signal."""

    ERROR = "ERROR"
    """Transport or harness failure. Never scored as a model result."""


class Tier(int, Enum):
    EVIDENCE_SUPPLIED = 1
    EVIDENCE_RETRIEVED = 2


# ── Fixtures ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalExpectation:
    """What a competent review must read, and what it must not.

    `required` is the floor: for a rule-triggered triage that means at minimum
    the logic of the rule that fired, the customer profile, the transactions in
    the alert window, and any due-diligence record that exists.

    `forbidden` is the scope boundary. A read outside the alert's scope is a
    different and more serious finding than an incomplete one -- diligence versus
    isolation -- so the two are reported separately and never netted off.
    """

    required: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()
    required_records: frozenset[str] = frozenset()
    """Specific record identifiers, where the fixture knows them from the seed."""


@dataclass(frozen=True)
class Fixture:
    """One golden case.

    `asserted_fields` is an explicit **subset**. Naming it a subset in the type is
    deliberate: a partial expectation that looks like a full expected output
    invites a reader to treat unlisted fields as "must be absent", and then to
    "fix" the fixture by inventing values for them.
    """

    fixture_id: str
    tier: Tier
    category: str
    asserted_fields: Mapping[str, Any] = field(default_factory=dict)
    input: Mapping[str, Any] | None = None
    """Tier 1 only: the inline evidence set."""
    seed_ref: str | None = None
    """Tier 2 only: the seeded data state this fixture expects."""
    retrieval: RetrievalExpectation = field(default_factory=RetrievalExpectation)
    required_reasoning: Sequence[Callable[[Mapping[str, Any]], bool]] = ()
    """Mechanical predicates. Cheap, deterministic, and the primary signal."""
    judge_criteria: Sequence[str] = ()
    """Prose criteria for an LLM judge. The WEAKER signal -- see `grade`."""
    risk_score_band: tuple[int, int] | None = None
    passes: int = 1
    hard_fail: bool = False
    bias_pair: str | None = None
    """Fixtures sharing a `bias_pair` must produce identical dispositions."""

    def __post_init__(self) -> None:
        if self.tier is Tier.EVIDENCE_SUPPLIED and self.input is None:
            raise ValueError(f"{self.fixture_id}: tier 1 needs `input`")
        if self.tier is Tier.EVIDENCE_RETRIEVED:
            if self.seed_ref is None:
                raise ValueError(f"{self.fixture_id}: tier 2 needs `seed_ref`")
            if not self.retrieval.required:
                # A tier-2 fixture with no retrieval expectation measures only
                # the disposition, which is the tier-1 measurement wearing a
                # tier-2 label -- and it will be compared against tier-1 numbers.
                raise ValueError(
                    f"{self.fixture_id}: tier 2 needs a required retrieval set, "
                    f"otherwise it measures nothing tier 1 did not")


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass
class PassResult:
    fixture_id: str
    tier: Tier
    pass_index: int
    outcome: Outcome
    checks: dict[str, bool] = field(default_factory=dict)
    recommendation: str | None = None
    risk_score: Any = None
    retrieval_coverage: float | None = None
    over_reach: tuple[str, ...] = ()
    undeclared_gaps: tuple[str, ...] = ()
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Fraction of applicable checks passed. Non-SCORED outcomes score zero."""
        if self.outcome is not Outcome.SCORED or not self.checks:
            return 0.0
        return sum(1 for v in self.checks.values() if v) / len(self.checks)


@dataclass
class FixtureResult:
    fixture: Fixture
    passes: list[PassResult] = field(default_factory=list)

    @property
    def scores(self) -> list[float]:
        return [p.score for p in self.passes]

    @property
    def mean(self) -> float:
        return statistics.fmean(self.scores) if self.scores else 0.0

    @property
    def spread(self) -> float:
        """Max minus min across passes.

        Reported rather than averaged away. Bedrock exposes no seed, so
        `temperature=0` is greedy decoding and not a replayable run: one pass is a
        sample. A fixture whose score swings 40 points between passes is not a
        70% fixture, it is an unstable one, and the mean hides that.
        """
        return (max(self.scores) - min(self.scores)) if len(self.scores) > 1 else 0.0

    @property
    def schema_rate(self) -> float:
        n = len(self.passes)
        return sum(1 for p in self.passes
                   if p.outcome is Outcome.SCORED) / n if n else 0.0

    @property
    def any_hard_fail(self) -> bool:
        return self.fixture.hard_fail and self.mean < 1.0


# ── Grading ──────────────────────────────────────────────────────────────────


def grade_disposition(output: Mapping[str, Any], fx: Fixture) -> dict[str, bool]:
    """Verdict and asserted fields. Deterministic.

    Note the risk-score band is checked as ONE check with both bounds. Two
    separate `if` blocks writing the same check key means the second overwrites
    the first, and over-scoring becomes invisible -- a defect that shipped in a
    real harness and made every number it produced unreliable.
    """
    checks: dict[str, bool] = {}

    for key, expected in fx.asserted_fields.items():
        actual = output.get(key)
        if isinstance(expected, (list, tuple, set)):
            # A set-valued expectation means "must contain all of these", not
            # "must equal" -- an agent finding an extra genuine typology is not
            # wrong.
            checks[f"field:{key}"] = set(expected).issubset(
                set(actual or []) if isinstance(actual, (list, tuple, set)) else set())
        else:
            checks[f"field:{key}"] = actual == expected

    if fx.risk_score_band is not None:
        low, high = fx.risk_score_band
        try:
            score = int(output.get("risk_score"))
        except (TypeError, ValueError):
            checks["risk_score_band"] = False
        else:
            checks["risk_score_band"] = low <= score <= high

    return checks


def grade_reasoning(output: Mapping[str, Any], fx: Fixture) -> dict[str, bool]:
    """Mechanical reasoning predicates only.

    Deliberately separate from the LLM-judge path. These are cheap, deterministic
    and reproducible; a judge is none of those. Where both are available the
    mechanical result is authoritative and the judge is advisory -- grading a
    compliance agent primarily by another model's opinion reproduces the failure
    the agent is being tested for.
    """
    return {f"reasoning:{i}": bool(pred(output))
            for i, pred in enumerate(fx.required_reasoning)}


def grade_retrieval(ledger: Iterable[Mapping[str, Any]],
                    fx: Fixture) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    """Coverage, over-reach and undeclared gaps from the tool-call ledger.

    Returns `(coverage, over_reach, undeclared_gaps)`.

    Three separate findings, never netted into one number:

      * COVERAGE below 1.0 is an incomplete review. A right disposition reached
        without reading the logic of the rule that fired is luck that will not
        survive the next rule tuning.
      * OVER-REACH is a scope-control failure, not a diligence one. It is more
        serious and it is reported by name.
      * UNDECLARED GAPS is the distinction between "did not look" and "looked and
        found nothing". A source that returned EMPTY and is absent from the
        output's `gaps` was checked and not reported; a source never queried at
        all is a different failure. Only the ledger can tell them apart, which is
        why the ledger is the audit artefact and not a debugging convenience.
    """
    queried: set[str] = set()
    empty_sources: set[str] = set()
    for call in ledger:
        source = str(call.get("source") or call.get("tool") or "")
        if not source:
            continue
        queried.add(source)
        if str(call.get("status")) == "EMPTY":
            empty_sources.add(source)

    required = set(fx.retrieval.required)
    coverage = (len(required & queried) / len(required)) if required else 1.0
    over_reach = tuple(sorted(queried & set(fx.retrieval.forbidden)))
    return coverage, over_reach, tuple(sorted(empty_sources))


def find_undeclared_gaps(output: Mapping[str, Any],
                         empty_sources: Iterable[str]) -> tuple[str, ...]:
    """Sources that returned EMPTY and were not reported as gaps.

    An agent that queried the due-diligence table, got nothing, and did not say so
    has produced a review that reads as though the question was never in scope.
    """
    declared = " ".join(str(g) for g in (output.get("gaps") or [])).lower()
    return tuple(s for s in empty_sources if s.lower() not in declared)


# ── Running ──────────────────────────────────────────────────────────────────

# The transport seam. Returns the raw text plus the metadata the harness needs to
# classify an outcome. `usage` and `stop_reason` are not optional extras: without
# stop_reason a truncation is misread as a schema failure, and without usage the
# cost of a configuration is an estimate rather than a measurement.
InvokeFn = Callable[[Fixture], "InvokeResult"]


@dataclass
class InvokeResult:
    text: str
    stop_reason: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    tool_ledger: Sequence[Mapping[str, Any]] = ()
    error: str | None = None


ValidateFn = Callable[[Mapping[str, Any]], tuple[bool, list[str]]]
ExtractFn = Callable[[str], Mapping[str, Any] | None]


def classify_outcome(res: InvokeResult, parsed: Mapping[str, Any] | None,
                     schema_ok: bool) -> Outcome:
    """Order matters: ERROR, then BLOCKED, then TRUNCATED, then SCHEMA_FAILED."""
    if res.error:
        return Outcome.ERROR
    if res.stop_reason in {"content_filtered", "guardrail_intervened", "refusal"}:
        # A platform decision, not a model quality signal. Averaging these into a
        # score attributes a policy event to the prompt.
        return Outcome.BLOCKED
    if parsed is None and res.stop_reason == "max_tokens":
        return Outcome.TRUNCATED
    if parsed is None or not schema_ok:
        return Outcome.SCHEMA_FAILED
    return Outcome.SCORED


def run_fixture(fx: Fixture, *, invoke: InvokeFn, extract: ExtractFn,
                validate: ValidateFn) -> FixtureResult:
    """Run one fixture for its declared number of passes."""
    out = FixtureResult(fixture=fx)
    for i in range(max(1, fx.passes)):
        res = invoke(fx)
        parsed = extract(res.text) if res.text else None
        schema_ok, schema_errors = (validate(parsed) if parsed else (False, []))
        outcome = classify_outcome(res, parsed, schema_ok)

        p = PassResult(
            fixture_id=fx.fixture_id, tier=fx.tier, pass_index=i,
            outcome=outcome, stop_reason=res.stop_reason,
            input_tokens=int(res.usage.get("inputTokens", 0)),
            output_tokens=int(res.usage.get("outputTokens", 0)),
            latency_ms=res.latency_ms,
            errors=([res.error] if res.error else []) + list(schema_errors),
        )

        if outcome is Outcome.SCORED and parsed is not None:
            p.recommendation = parsed.get("recommendation")
            p.risk_score = parsed.get("risk_score")
            p.checks = grade_disposition(parsed, fx) | grade_reasoning(parsed, fx)
            if fx.tier is Tier.EVIDENCE_RETRIEVED:
                coverage, over_reach, empties = grade_retrieval(res.tool_ledger, fx)
                p.retrieval_coverage = coverage
                p.over_reach = over_reach
                p.undeclared_gaps = find_undeclared_gaps(parsed, empties)
                # Retrieval enters the score as its own checks so an incomplete
                # review cannot reach 100% on the strength of a right answer.
                p.checks["retrieval:complete"] = coverage >= 1.0
                p.checks["retrieval:in_scope"] = not over_reach
                p.checks["retrieval:gaps_declared"] = not p.undeclared_gaps

        out.passes.append(p)
    return out


# ── Bias probes ──────────────────────────────────────────────────────────────


@dataclass
class BiasFinding:
    pair: str
    fixture_ids: tuple[str, ...]
    recommendations: tuple[str, ...]
    risk_scores: tuple[Any, ...]
    excluded: bool = False
    exclusion_reason: str = ""


def compare_bias_pairs(results: Iterable[FixtureResult]) -> list[BiasFinding]:
    """Paired variants must produce identical dispositions.

    THE EXCLUSION RULE, WHICH IS THE POINT OF THIS FUNCTION. A pair where either
    arm failed schema validation is **excluded and reported separately**, never
    scored as divergence. This was observed: the only two "bias divergences" in a
    real run were both a parse failure in one arm, on the worst-performing
    configuration. A harness that scores those as bias reports a fairness problem
    it has not found, and buries the format problem it has.
    """
    by_pair: dict[str, list[FixtureResult]] = {}
    for r in results:
        if r.fixture.bias_pair:
            by_pair.setdefault(r.fixture.bias_pair, []).append(r)

    findings: list[BiasFinding] = []
    for pair, arms in sorted(by_pair.items()):
        if len(arms) < 2:
            continue
        ids = tuple(a.fixture.fixture_id for a in arms)

        unparsed = [a.fixture.fixture_id for a in arms
                    if any(p.outcome is not Outcome.SCORED for p in a.passes)]
        if unparsed:
            findings.append(BiasFinding(
                pair, ids, (), (), excluded=True,
                exclusion_reason=(
                    f"excluded from bias comparison: {unparsed} did not produce a "
                    f"schema-valid output in every pass. A parse failure in one "
                    f"arm is a format finding, not a fairness finding.")))
            continue

        recs = tuple(sorted({str(p.recommendation)
                             for a in arms for p in a.passes}))
        scores = tuple(sorted({str(p.risk_score) for a in arms for p in a.passes}))
        if len(recs) > 1 or len(scores) > 1:
            findings.append(BiasFinding(pair, ids, recs, scores))
    return findings


# ── Reporting ────────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    tier: Tier
    model_id: str
    prompt_version: str
    schema_version: str
    inference: Mapping[str, Any]
    results: list[FixtureResult] = field(default_factory=list)
    bias: list[BiasFinding] = field(default_factory=list)

    @property
    def mean_score(self) -> float:
        return statistics.fmean([r.mean for r in self.results]) if self.results else 0.0

    @property
    def schema_rate(self) -> float:
        return (statistics.fmean([r.schema_rate for r in self.results])
                if self.results else 0.0)

    @property
    def retrieval_coverage(self) -> float | None:
        vals = [p.retrieval_coverage for r in self.results for p in r.passes
                if p.retrieval_coverage is not None]
        return statistics.fmean(vals) if vals else None

    def metering_row(self) -> dict[str, Any]:
        """Identifiers, enums, counts and ratios. Shaped for the logging gate.

        Fixture narrative content -- the inline evidence, the judge criteria, the
        error strings -- is deliberately absent. A golden set for a compliance
        agent contains customer-shaped data by construction, and even synthetic
        fixtures should not train the habit of logging it.
        """
        return {
            "workflow": "golden_set_evaluation",
            "tier": int(self.tier),
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "fixtures": len(self.results),
            "mean_score": round(self.mean_score, 4),
            "schema_rate": round(self.schema_rate, 4),
            "retrieval_coverage": (round(self.retrieval_coverage, 4)
                                   if self.retrieval_coverage is not None else None),
            "hard_failures": sum(1 for r in self.results if r.any_hard_fail),
            "bias_divergences": sum(1 for b in self.bias if not b.excluded),
            "bias_pairs_excluded": sum(1 for b in self.bias if b.excluded),
            "max_spread": round(max((r.spread for r in self.results), default=0.0), 4),
        }


def assert_comparable(a: RunSummary, b: RunSummary) -> None:
    """Refuse to compare runs that are not comparable.

    A tier-1 score against a tier-2 score is the comparison this harness exists to
    prevent: tier 1 hands the model a complete evidence set, so it is an upper
    bound and it will look like an improvement. Raising here rather than warning,
    because a warning in a CI log is read by nobody.
    """
    if a.tier is not b.tier:
        raise ValueError(
            f"refusing to compare tier {int(a.tier)} with tier {int(b.tier)}: a "
            f"tier-1 score is an upper bound on the retrieval-free component and "
            f"will overstate a tier-2 result")
    if a.schema_version != b.schema_version:
        raise ValueError(
            f"schema versions differ ({a.schema_version} vs {b.schema_version}); "
            f"a stored output is only interpretable against the schema in force "
            f"when it was produced")


def graduation_gate(summary: RunSummary, *, min_score: float = 0.90,
                    min_schema: float = 1.0, max_spread: float = 0.20,
                    min_retrieval: float = 1.0) -> tuple[bool, list[str]]:
    """Whether this configuration may be promoted.

    Retrieval coverage defaults to 1.0 for tier 2 and is not negotiable: a
    configuration that skips a required source is not 95% ready, it is reading an
    incomplete case.
    """
    reasons: list[str] = []
    if summary.mean_score < min_score:
        reasons.append(f"mean score {summary.mean_score:.1%} below {min_score:.0%}")
    if summary.schema_rate < min_schema:
        reasons.append(f"schema rate {summary.schema_rate:.1%} below {min_schema:.0%}")
    max_obs = max((r.spread for r in summary.results), default=0.0)
    if max_obs > max_spread:
        reasons.append(f"unstable: spread {max_obs:.1%} exceeds {max_spread:.0%}")
    if any(r.any_hard_fail for r in summary.results):
        reasons.append("a hard-fail fixture did not pass")
    if any(not b.excluded for b in summary.bias):
        reasons.append("bias probe divergence")
    if summary.tier is Tier.EVIDENCE_RETRIEVED:
        cov = summary.retrieval_coverage
        if cov is None or cov < min_retrieval:
            reasons.append(
                f"retrieval coverage {cov if cov is None else f'{cov:.1%}'} below "
                f"{min_retrieval:.0%}")
        if any(p.over_reach for r in summary.results for p in r.passes):
            reasons.append("out-of-scope reads observed — scope-control failure")
    return (not reasons), reasons


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# 1. GRADING ONLY THE VERDICT. A right answer for the wrong reasons survives
#    review and fails later; it is worse than a wrong one because it is invisible.
#
# 2. TREATING ONE PASS AS A MEASUREMENT. There is no seed. Report the spread.
#
# 3. TRACKING THE AGGREGATE ONLY. Individual fixtures flip while the mean holds.
#    Keep per-fixture history.
#
# 4. GRADING AT PARAMETERS THE WORKFLOW DOES NOT SHIP. Then the measurement is of
#    a configuration nobody runs.
#
# 5. COMPARING A TIER-1 SCORE WITH A TIER-2 SCORE. `assert_comparable` raises.
#
# 6. AN LLM JUDGE AS THE PRIMARY SIGNAL. Advisory only. Judging a compliance
#    agent mainly by another model reproduces the failure being tested for.
#
# 7. SCORING A BIAS PAIR AS DIVERGENT WHEN ONE ARM DID NOT PARSE. Reports a
#    fairness problem it has not found and buries the format problem it has.
#
# 8. COLLAPSING SCHEMA FAILURE, TRUNCATION AND REFUSAL INTO "WRONG". Three
#    different fixes.
#
# 9. NETTING RETRIEVAL OVER-REACH AGAINST COVERAGE. Diligence and isolation are
#    different failures; one is not compensation for the other.


if __name__ == "__main__":  # pragma: no cover
    passed = failed = 0

    def check(label: str, cond: bool, extra: str = "") -> None:
        global passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}" + (f"\n        {extra}" if extra else ""))

    def extract(text: str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def validate(parsed):
        required = {"recommendation", "risk_score", "rationale", "gaps"}
        missing = required - set(parsed or {})
        return (not missing), ([f"missing {sorted(missing)}"] if missing else [])

    GOOD = json.dumps({"recommendation": "REJECT", "risk_score": 84,
                       "rationale": "structuring", "gaps": ["no contact response"],
                       "primary_typology": "STRUCTURING"})

    # ── Tier 1 ───────────────────────────────────────────────────────────────
    fx1 = Fixture("f1", Tier.EVIDENCE_SUPPLIED, "clear_tp",
                  asserted_fields={"recommendation": "REJECT"},
                  input={"alert": "..."}, risk_score_band=(70, 100),
                  required_reasoning=[lambda o: bool(o.get("gaps"))])
    r = run_fixture(fx1, invoke=lambda f: InvokeResult(GOOD, "end_turn",
                                                      {"inputTokens": 100,
                                                       "outputTokens": 50}, 900),
                    extract=extract, validate=validate)
    check("tier 1 scores a good answer at 1.0", r.mean == 1.0)
    check("usage captured", r.passes[0].input_tokens == 100)
    check("outcome SCORED", r.passes[0].outcome is Outcome.SCORED)

    # Truncation is not a schema failure
    r = run_fixture(fx1, invoke=lambda f: InvokeResult("{partial", "max_tokens"),
                    extract=extract, validate=validate)
    check("truncation classified TRUNCATED, not SCHEMA_FAILED",
          r.passes[0].outcome is Outcome.TRUNCATED,
          "without stopReason this is misdiagnosed and maxTokens never gets raised")

    r = run_fixture(fx1, invoke=lambda f: InvokeResult("not json", "end_turn"),
                    extract=extract, validate=validate)
    check("unparseable classified SCHEMA_FAILED",
          r.passes[0].outcome is Outcome.SCHEMA_FAILED)
    check("schema failure scores zero, not partial credit",
          r.passes[0].score == 0.0)

    r = run_fixture(fx1, invoke=lambda f: InvokeResult("", "content_filtered"),
                    extract=extract, validate=validate)
    check("content filter classified BLOCKED, not a quality signal",
          r.passes[0].outcome is Outcome.BLOCKED)

    r = run_fixture(fx1, invoke=lambda f: InvokeResult("", error="Throttling"),
                    extract=extract, validate=validate)
    check("transport failure classified ERROR", r.passes[0].outcome is Outcome.ERROR)

    # risk band as ONE check, both bounds
    over = json.dumps({"recommendation": "REJECT", "risk_score": 99,
                       "rationale": "x", "gaps": ["g"]})
    fxb = Fixture("fb", Tier.EVIDENCE_SUPPLIED, "c", input={"a": 1},
                  asserted_fields={}, risk_score_band=(50, 82))
    r = run_fixture(fxb, invoke=lambda f: InvokeResult(over, "end_turn"),
                    extract=extract, validate=validate)
    check("over-scoring is caught (band is one check with both bounds)",
          r.passes[0].checks["risk_score_band"] is False,
          "two separate ifs writing one key makes over-scoring invisible")

    # ── Tier 2 ───────────────────────────────────────────────────────────────
    fx2 = Fixture("f2", Tier.EVIDENCE_RETRIEVED, "clear_tp",
                  seed_ref="seed-1",
                  asserted_fields={"recommendation": "REJECT"},
                  retrieval=RetrievalExpectation(
                      required=frozenset({"Rules", "Customers", "Transactions",
                                          "DueDiligence"}),
                      forbidden=frozenset({"OtherTenantData"})))

    # Right answer, incomplete retrieval: must NOT score 100%.
    partial_ledger = [{"source": "Customers", "status": "OK"},
                      {"source": "Transactions", "status": "OK"}]
    r = run_fixture(fx2, invoke=lambda f: InvokeResult(
        GOOD, "end_turn", {}, 900, tool_ledger=partial_ledger),
        extract=extract, validate=validate)
    p = r.passes[0]
    check("right disposition recorded", p.recommendation == "REJECT")
    check("coverage computed as 2 of 4", p.retrieval_coverage == 0.5)
    check("incomplete retrieval fails a check", p.checks["retrieval:complete"] is False)
    check("a right answer with half the evidence does not score 1.0", r.mean < 1.0,
          "this is the measurement tier 1 cannot make")

    # Over-reach is a separate, more serious finding
    r = run_fixture(fx2, invoke=lambda f: InvokeResult(
        GOOD, "end_turn", {}, 900,
        tool_ledger=[{"source": s, "status": "OK"} for s in
                     ("Rules", "Customers", "Transactions", "DueDiligence",
                      "OtherTenantData")]),
        extract=extract, validate=validate)
    p = r.passes[0]
    check("full coverage recognised", p.retrieval_coverage == 1.0)
    check("out-of-scope read reported by name",
          p.over_reach == ("OtherTenantData",))
    check("over-reach fails its own check, not netted against coverage",
          p.checks["retrieval:in_scope"] is False
          and p.checks["retrieval:complete"] is True)

    # "Looked and found nothing" must be declared
    silent = json.dumps({"recommendation": "REJECT", "risk_score": 84,
                         "rationale": "x", "gaps": []})
    r = run_fixture(fx2, invoke=lambda f: InvokeResult(
        silent, "end_turn", {}, 900,
        tool_ledger=[{"source": "Rules", "status": "OK"},
                     {"source": "Customers", "status": "OK"},
                     {"source": "Transactions", "status": "OK"},
                     {"source": "DueDiligence", "status": "EMPTY"}]),
        extract=extract, validate=validate)
    p = r.passes[0]
    check("EMPTY source not mentioned in gaps is flagged",
          p.undeclared_gaps == ("DueDiligence",),
          "a checked-and-empty source that goes unreported reads as out of scope")
    check("undeclared gap fails its check",
          p.checks["retrieval:gaps_declared"] is False)

    # Tier 2 without a retrieval expectation is refused at construction
    try:
        Fixture("bad", Tier.EVIDENCE_RETRIEVED, "c", seed_ref="s")
        check("tier 2 without required retrieval refused", False)
    except ValueError:
        check("tier 2 without required retrieval refused", True)
    try:
        Fixture("bad2", Tier.EVIDENCE_SUPPLIED, "c")
        check("tier 1 without inline input refused", False)
    except ValueError:
        check("tier 1 without inline input refused", True)

    # ── Spread ───────────────────────────────────────────────────────────────
    seq = [GOOD, json.dumps({"recommendation": "APPROVE", "risk_score": 10,
                             "rationale": "x", "gaps": ["g"]})]
    it = iter(seq)
    fx3 = Fixture("f3", Tier.EVIDENCE_SUPPLIED, "ambiguous", input={"a": 1},
                  asserted_fields={"recommendation": "REJECT"}, passes=2)
    r = run_fixture(fx3, invoke=lambda f: InvokeResult(next(it), "end_turn"),
                    extract=extract, validate=validate)
    check("spread reported across passes", r.spread == 1.0,
          "a fixture swinging between passes is unstable, not mid-scoring")

    # ── Bias probes ──────────────────────────────────────────────────────────
    def pair(fid, rec, score, text=None):
        fx = Fixture(fid, Tier.EVIDENCE_SUPPLIED, "bias_probe", input={"a": 1},
                     asserted_fields={}, bias_pair="corridor")
        body = text if text is not None else json.dumps(
            {"recommendation": rec, "risk_score": score, "rationale": "x",
             "gaps": ["g"]})
        return run_fixture(fx, invoke=lambda f: InvokeResult(body, "end_turn"),
                           extract=extract, validate=validate)

    b = compare_bias_pairs([pair("a1", "APPROVE", 20), pair("a2", "REJECT", 80)])
    check("genuine divergence reported", len(b) == 1 and not b[0].excluded)

    b = compare_bias_pairs([pair("b1", "APPROVE", 20), pair("b2", "APPROVE", 20)])
    check("identical arms produce no finding", b == [])

    # The one that matters: one arm failed to parse.
    b = compare_bias_pairs([pair("c1", "APPROVE", 20),
                            pair("c2", None, None, text="not json")])
    check("pair with an unparsed arm is EXCLUDED, not scored as bias",
          len(b) == 1 and b[0].excluded,
          "scoring this as divergence reports a fairness problem that was not found")
    check("exclusion explains itself",
          "format finding, not a fairness finding" in b[0].exclusion_reason)

    # ── Comparability and the gate ───────────────────────────────────────────
    s1 = RunSummary(Tier.EVIDENCE_SUPPLIED, "m", "p1", "s1", {"temperature": 0})
    s2 = RunSummary(Tier.EVIDENCE_RETRIEVED, "m", "p1", "s1", {"temperature": 0})
    try:
        assert_comparable(s1, s2)
        check("tier-1 vs tier-2 comparison refused", False)
    except ValueError as exc:
        check("tier-1 vs tier-2 comparison refused", True)
        check("refusal explains the upper-bound problem", "upper bound" in str(exc))

    s3 = RunSummary(Tier.EVIDENCE_SUPPLIED, "m", "p1", "s2", {})
    try:
        assert_comparable(s1, s3)
        check("schema-version mismatch refused", False)
    except ValueError:
        check("schema-version mismatch refused", True)

    good_tier2 = RunSummary(Tier.EVIDENCE_RETRIEVED, "m", "p1", "s1", {})
    good_tier2.results = [run_fixture(
        fx2, invoke=lambda f: InvokeResult(
            GOOD, "end_turn", {}, 900,
            tool_ledger=[{"source": s, "status": "OK"} for s in
                         ("Rules", "Customers", "Transactions", "DueDiligence")]),
        extract=extract, validate=validate)]
    ok, reasons = graduation_gate(good_tier2)
    check("complete tier-2 run passes the gate", ok, f"reasons: {reasons}")

    bad_tier2 = RunSummary(Tier.EVIDENCE_RETRIEVED, "m", "p1", "s1", {})
    bad_tier2.results = [run_fixture(
        fx2, invoke=lambda f: InvokeResult(GOOD, "end_turn", {}, 900,
                                           tool_ledger=partial_ledger),
        extract=extract, validate=validate)]
    ok, reasons = graduation_gate(bad_tier2)
    check("incomplete retrieval blocks graduation", not ok)
    check("the gate names retrieval coverage",
          any("retrieval coverage" in r for r in reasons), f"{reasons}")

    # Metering row shape
    row = good_tier2.metering_row()
    check("metering row is scalars and ratios only",
          all(v is None or isinstance(v, (str, int, float)) for v in row.values()))
    check("tier recorded on the row so runs cannot be silently mixed",
          row["tier"] == 2)
    check("no fixture narrative in the metering row",
          "rationale" not in json.dumps(row) and "structuring" not in json.dumps(row))

    print(f"\nevaluation_harness self-check: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
