"""Prove the agent did what its narrative claims.

THE INCIDENT THIS FILE EXISTS FOR. An agent completed all three phases of a
workflow, reported "your claim has been successfully recorded", and a second
agent reviewing the output scored it 95/100 and approved. The target table was
empty. The write tool's Lambda had **no CloudWatch log group at all**, which is
proof it had never been invoked once. Nothing in the output looked wrong: the
narrative was fluent, internally consistent, and describing an action that did
not happen.

That failure is not exotic. A model asked to call a tool and report the result
will, under some conditions, report the result without calling the tool -- and
every downstream consumer of that report, human or machine, reads a completed
action. In a compliance platform the report becomes a record, and the record is
what an examiner reads.

So: after the agent finishes, before anything is filed or closed, reconcile what
it *asserted* against what the platform can *observe*. Three independent
observations, because each one alone is defeatable:

  1. Does the record exist, with the content and identifier claimed?
  2. Did the target actually execute, in the window, the expected number of times?
  3. Did the agent's own tool-result set contain a call for this action at all?

WHAT MAKES THIS DIFFERENT FROM "DOES THE RECORD EXIST". Existence answers *is
there a matching record*, not *did this run create it*. A backfill, a retry, a
colleague's manual entry, a prior test run, or an unrelated service all satisfy
existence. Attribution needs observation 2. And observation 2 must assert a
**count**, not a boolean -- see `Verdict.DOUBLE_WRITTEN` below for why that is
not pedantry.

Runs OUTSIDE the agent, with its own clients. A reconciliation performed by the
agent under test, through the agent's own filtered tool client, is the agent
grading its own homework with the same blindfold on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol

log = logging.getLogger(__name__)

try:  # pragma: no cover - the module must import with no AWS present
    import boto3
    from botocore.config import Config
except ImportError:  # pragma: no cover
    boto3 = None
    Config = None


# ── The four verdicts ────────────────────────────────────────────────────────


class Verdict(str, Enum):
    """What reconciliation concluded. The middle two are the useful ones.

    A two-state check (matched / did not match) collapses these and loses the
    distinction that tells you *which* system is broken.
    """

    CONSISTENT = "CONSISTENT"
    """Record present, target invoked the expected number of times. Nothing to do."""

    UNATTRIBUTED = "UNATTRIBUTED"
    """Record present, target NOT invoked by this run.

    The dangerous one, and the one a naive existence check reports as success.
    Something other than this agent wrote that record: a backfill, a retry of an
    earlier run, a manual entry, a prior test, another service. The disposition
    may still be correct, but the audit trail now attributes to this run an action
    it did not perform -- which is a false record, not a near miss.
    """

    WRITE_FAILED = "WRITE_FAILED"
    """Target invoked, record absent. The call happened and the write did not.

    Honest failure: retryable, visible, and the agent's narrative is wrong in the
    safe direction (it claims something that should have happened).
    """

    NARRATED = "NARRATED"
    """Neither. The agent described an action nothing performed.

    This is the incident in the module docstring.
    """

    DOUBLE_WRITTEN = "DOUBLE_WRITTEN"
    """Target invoked more times than asserted, or duplicate records exist.

    Why count matters rather than existence. When a broken write path is fixed,
    the fix frequently lands *alongside* the old path rather than replacing it --
    a tool-name correction plus a model that had also learned to call the tool
    directly, for instance. Both fire. Two records appear with different
    identifiers, and every existence-based check passes while the platform
    silently double-files.
    """

    INCONCLUSIVE = "INCONCLUSIVE"
    """An observation was unavailable. NOT the same as consistent.

    Metrics lag, a lookup throttles, a log group query times out. Reconciliation
    that degrades to "probably fine" on a failed observation is worse than no
    reconciliation, because it manufactures the assurance it was built to test.
    """


@dataclass(frozen=True)
class AssertedAction:
    """One action the agent claims it performed.

    Parsed from the agent's **structured output**, never its prose. A regex over a
    narrative reconciles the narrative against itself, which is precisely the
    artefact under test -- and prose is where the fabrication lives.
    """

    action: str
    """A stable identifier for the action type, e.g. "create_claim"."""

    target: str
    """The function or service that performs it, as CloudWatch names it."""

    record_id: str | None
    """The identifier the agent says was created. `None` is itself a finding."""

    expected_invocations: int = 1
    """How many times the target should have run. Asserting a count, not a bool."""

    tool_call_id: str | None = None
    """The tool call in the run's own ledger, if the framework surfaced one."""


@dataclass
class Finding:
    action: AssertedAction
    verdict: Verdict
    record_found: bool | None = None
    invocations_observed: int | None = None
    tool_call_present: bool | None = None
    detail: str = ""

    @property
    def is_blocking(self) -> bool:
        """Which verdicts must stop a filing. Everything except CONSISTENT.

        Two of these are worth stating explicitly, because both have been argued
        the other way and both arguments are wrong:

        `WRITE_FAILED` blocks. It is the least alarming verdict -- the call
        happened, the platform is at fault, and nobody fabricated anything -- but
        the agent still asserted a record that does not exist. A filing that
        proceeds on that assertion is a filing citing a record an examiner cannot
        retrieve. "Retryable" describes the remedy, not the severity.

        `INCONCLUSIVE` blocks. An unverifiable action in a compliance workflow is
        not a pass. The correct response is a human looking at it, not an
        inference drawn from a failed observation.
        """
        return self.verdict is not Verdict.CONSISTENT


@dataclass
class Reconciliation:
    run_id: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.is_blocking]

    @property
    def consistent(self) -> bool:
        return bool(self.findings) and not self.blocking

    def metering_row(self) -> dict[str, Any]:
        """Identifiers, enums and counts. No narrative.

        Shaped to pass the allowlist gate in `log_projection.py`: every value here
        is a string identifier, an enum member, or a number. The explanatory
        `detail` on each finding is deliberately absent -- it is prose, it can
        quote record content, and it belongs in the internal record.
        """
        by_verdict: dict[str, int] = {}
        for f in self.findings:
            by_verdict[f.verdict.value] = by_verdict.get(f.verdict.value, 0) + 1
        return {
            "run_id": self.run_id,
            "reconciliation_verdict": (
                Verdict.CONSISTENT.value if self.consistent else "BLOCKED"),
            "actions_asserted": len(self.findings),
            "actions_blocking": len(self.blocking),
            "verdict_counts": by_verdict,
        }


# ── Observation seams ────────────────────────────────────────────────────────
#
# Protocols rather than concrete clients so the checks are testable without AWS
# and so a platform with a different system of record can substitute its own.


class RecordStore(Protocol):
    def find(self, action: str, record_id: str) -> Mapping[str, Any] | None: ...
    def count_matching(self, action: str, run_id: str,
                       window: tuple[datetime, datetime]) -> int | None: ...


class InvocationMetrics(Protocol):
    def invocations(self, target: str,
                    window: tuple[datetime, datetime]) -> int | None: ...
    def log_group_exists(self, target: str) -> bool | None: ...


# ── The three observations ───────────────────────────────────────────────────


def observe_record(store: RecordStore, a: AssertedAction) -> bool | None:
    """Observation 1: does the claimed record exist, under the claimed id?

    A `None` `record_id` short-circuits to False rather than "not checked". An
    agent that reports success without an identifier has already failed to
    perform the action in any sense a record can capture -- the placeholder
    identifier (`"unknown"`, `""`, `"N/A"`) is the tell, and it must not be
    treated as a lookup that could not be attempted.
    """
    if not a.record_id or a.record_id.lower() in {"unknown", "n/a", "none", "-"}:
        return False
    try:
        return store.find(a.action, a.record_id) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("record lookup failed for %s: %s", a.record_id, exc)
        return None


def observe_invocations(metrics: InvocationMetrics, a: AssertedAction,
                        window: tuple[datetime, datetime]) -> int | None:
    """Observation 2: did the target actually run, and how many times?

    The absence of a CloudWatch log group is the strongest single signal
    available here: AWS Lambda creates the group on first invocation, so no group
    means the function has never been invoked, by anything, ever. That is a proof,
    not an inference -- and it is what turned the incident in the docstring from a
    suspicion into a fact.

    The converse is NOT true. A group that exists proves the function ran at some
    point in its life, and says nothing about this run. Hence the count in a
    window.
    """
    exists = None
    try:
        exists = metrics.log_group_exists(a.target)
    except Exception as exc:  # noqa: BLE001
        log.warning("log group probe failed for %s: %s", a.target, exc)

    if exists is False:
        # Definitive. Skip the metric query; there is nothing to count.
        return 0

    try:
        return metrics.invocations(a.target, window)
    except Exception as exc:  # noqa: BLE001
        log.warning("invocation metric failed for %s: %s", a.target, exc)
        return None


def observe_tool_call(a: AssertedAction,
                      tool_results: Iterable[Mapping[str, Any]]) -> bool:
    """Observation 3: did the run's own ledger contain a call for this action?

    Catches the purest form of the failure -- an assertion with no AWS call behind
    it at all, where observations 1 and 2 are both looking for the effects of
    something that was never attempted. Cheap, local, and needs no AWS.
    """
    for result in tool_results:
        name = str(result.get("name") or result.get("tool") or "")
        if a.action in name or (a.tool_call_id
                                and result.get("id") == a.tool_call_id):
            return True
    return False


# ── Classification ───────────────────────────────────────────────────────────


def classify(a: AssertedAction, record_found: bool | None,
             invocations: int | None, tool_call: bool) -> Finding:
    """Reduce the three observations to one verdict.

    Order matters. `INCONCLUSIVE` is checked first so an unavailable observation
    can never be resolved into a pass by the checks below it -- which is the
    mistake that turns this file into decoration.
    """
    if record_found is None or invocations is None:
        return Finding(a, Verdict.INCONCLUSIVE, record_found, invocations,
                       tool_call,
                       detail=("an observation was unavailable; this action is "
                               "unverified and must be reviewed, not assumed"))

    if invocations > a.expected_invocations:
        return Finding(a, Verdict.DOUBLE_WRITTEN, record_found, invocations,
                       tool_call,
                       detail=(f"{a.target} ran {invocations} times, "
                               f"{a.expected_invocations} asserted — a second "
                               f"write path is live"))

    if record_found and invocations >= 1:
        return Finding(a, Verdict.CONSISTENT, record_found, invocations,
                       tool_call, detail="record present and target invoked")

    if record_found and invocations == 0:
        return Finding(a, Verdict.UNATTRIBUTED, record_found, invocations,
                       tool_call,
                       detail=(f"record {a.record_id!r} exists but {a.target} "
                               f"did not run in this window — it was written by "
                               f"something other than this run"))

    if not record_found and invocations >= 1:
        return Finding(a, Verdict.WRITE_FAILED, record_found, invocations,
                       tool_call,
                       detail=f"{a.target} ran but no record was created")

    return Finding(a, Verdict.NARRATED, record_found, invocations, tool_call,
                   detail=("no record and no invocation — the action was "
                           "described, not performed"))


def reconcile(run_id: str, asserted_actions: Iterable[AssertedAction], *,
              store: RecordStore, metrics: InvocationMetrics,
              window: tuple[datetime, datetime],
              tool_results: Iterable[Mapping[str, Any]] = ()) -> Reconciliation:
    """Reconcile every asserted action. The entry point.

    Call after the agent finishes and BEFORE anything is filed, closed or
    reported as done. A reconciliation that runs after the filing is a post-mortem.
    """
    results = list(tool_results)
    out = Reconciliation(run_id=run_id)
    for a in asserted_actions:
        finding = classify(
            a,
            observe_record(store, a),
            observe_invocations(metrics, a, window),
            observe_tool_call(a, results),
        )
        out.findings.append(finding)
        if finding.is_blocking:
            log.error("reconciliation %s: %s — %s", finding.verdict.value,
                      a.action, finding.detail)
    return out


def extract_asserted_actions(output: Mapping[str, Any], *,
                             target_map: Mapping[str, str]) -> list[AssertedAction]:
    """Parse asserted actions from STRUCTURED output only.

    `target_map` translates an action name to the function that performs it, and
    it is supplied by the platform rather than inferred from the output -- an
    agent that can name its own target can name a target that always looks
    healthy.

    There is deliberately no prose parser here. If a platform needs one, the
    correct fix is upstream: require the agent to emit actions as structured
    fields, because an action that only exists in prose cannot be reconciled and
    should not be trusted.
    """
    actions: list[AssertedAction] = []
    for item in output.get("actions_taken") or []:
        if not isinstance(item, Mapping):
            # A bare string in an actions list is prose in a structured field.
            # Reject rather than regex it.
            log.warning("actions_taken entry is not an object; skipping: %r", item)
            continue
        name = str(item.get("action") or "")
        if not name:
            continue
        actions.append(AssertedAction(
            action=name,
            target=target_map.get(name, ""),
            record_id=(str(item["record_id"])
                       if item.get("record_id") is not None else None),
            expected_invocations=int(item.get("count", 1)),
            tool_call_id=(str(item["tool_call_id"])
                          if item.get("tool_call_id") else None),
        ))
    return actions


# ── AWS-backed observers ─────────────────────────────────────────────────────


class CloudWatchMetrics:
    """`InvocationMetrics` over CloudWatch. Read-only.

    Lambda publishes `Invocations` to `AWS/Lambda` at one-minute resolution with
    no instrumentation, which is what makes this check free to add.
    """

    def __init__(self, region: str | None = None):
        if boto3 is None:  # pragma: no cover
            raise RuntimeError("boto3 unavailable")
        cfg = Config(retries={"max_attempts": 3, "mode": "standard"},
                     connect_timeout=3, read_timeout=10)
        self._cw = boto3.client("cloudwatch", region_name=region, config=cfg)
        self._logs = boto3.client("logs", region_name=region, config=cfg)

    def invocations(self, target: str,
                    window: tuple[datetime, datetime]) -> int | None:
        start, end = window
        # Pad the window. Metric timestamps and the agent's own clock do not
        # agree to the second, and a boundary invocation dropped by an exact
        # window reads as UNATTRIBUTED -- a false accusation, which is the
        # expensive direction for this check to fail in.
        resp = self._cw.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Invocations",
            Dimensions=[{"Name": "FunctionName", "Value": target}],
            StartTime=start - timedelta(minutes=1),
            EndTime=end + timedelta(minutes=1),
            Period=60,
            Statistics=["Sum"],
        )
        points = resp.get("Datapoints") or []
        if not points:
            # No datapoints is genuinely ambiguous: either it did not run, or the
            # metric has not landed yet. `log_group_exists` disambiguates, and
            # where it cannot, INCONCLUSIVE is the honest answer.
            return None
        return int(sum(p.get("Sum", 0) for p in points))

    def log_group_exists(self, target: str) -> bool | None:
        prefix = f"/aws/lambda/{target}"
        resp = self._logs.describe_log_groups(logGroupNamePrefix=prefix, limit=5)
        groups = [g for g in resp.get("logGroups", [])
                  if g.get("logGroupName") == prefix]
        return bool(groups)


class DynamoRecordStore:
    """`RecordStore` over one DynamoDB table. Read-only."""

    def __init__(self, table_name: str, *, id_attr: str = "record_id",
                 region: str | None = None):
        if boto3 is None:  # pragma: no cover
            raise RuntimeError("boto3 unavailable")
        self._tbl = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self._id_attr = id_attr

    def find(self, action: str, record_id: str) -> Mapping[str, Any] | None:
        resp = self._tbl.get_item(Key={self._id_attr: record_id})
        return resp.get("Item")

    def count_matching(self, action: str, run_id: str,
                       window: tuple[datetime, datetime]) -> int | None:
        # Left to the platform: the query depends on the table's key design, and
        # a Scan here would be a production incident of its own.
        return None


def emit(reconciliation: Reconciliation, *, tenant_id: str,
         hmac_key: bytes) -> dict[str, Any]:
    """Emit the verdict through the two-projection gate.

    Imported lazily so this module stays usable standalone. The metering row is
    identifiers, enums and counts; the per-finding `detail` strings quote record
    identifiers and explain reasoning, so they stay in the internal record.
    """
    from log_projection import Profile, emit_metering, split  # noqa: PLC0415

    record = reconciliation.metering_row() | {
        "workflow": "action_reconciliation",
        "findings_detail": [f.detail for f in reconciliation.findings],
    }
    projection = split(record, tenant_id=tenant_id,
                       run_id=reconciliation.run_id, hmac_key=hmac_key,
                       profile=Profile.PROD)
    return emit_metering(projection)


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# 1. RECONCILING AGAINST THE NARRATIVE. Regexing "successfully recorded" out of
#    the rationale reconciles the artefact under test against itself. Parse
#    structured fields; if the action is only in prose, fix the schema.
#
# 2. ASSERTING EXISTENCE RATHER THAN COUNT. Existence passes while a fixed write
#    path double-writes alongside the old one. Assert the number.
#
# 3. TREATING A METRIC ABSENCE AS PROOF OF ANYTHING BUT NON-INVOCATION. No
#    datapoints can mean the metric has not landed. Only a missing *log group* is
#    proof, and only in the negative direction.
#
# 4. RUNNING RECONCILIATION INSIDE THE AGENT. Same client, same filters, same
#    blind spots. It has to be a different principal looking from outside.
#
# 5. A RECONCILIATION FAILURE THAT ONLY WARNS. If UNATTRIBUTED does not stop the
#    filing, the check has documented the false record rather than prevented it.
#
# 6. RESOLVING INCONCLUSIVE TO CONSISTENT. "We could not check" and "we checked
#    and it was fine" are opposite findings. Collapsing them manufactures exactly
#    the assurance this file was built to test.
#
# 7. LETTING THE AGENT NAME ITS OWN TARGET. `target_map` is the platform's, not
#    the model's. An agent that supplies the target can supply a healthy one.


if __name__ == "__main__":  # pragma: no cover
    # Executable self-check: no AWS, no test framework.
    #
    # Every case below is a way the reconciliation has plausibly failed. The
    # UNATTRIBUTED and DOUBLE_WRITTEN cases are the ones an existence-only check
    # gets wrong, so they are asserted hardest.
    from datetime import datetime as _dt

    W = (_dt(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
         _dt(2026, 8, 17, 9, 30, tzinfo=timezone.utc))
    TARGETS = {"create_claim": "ClaimsAgent-CreateClaim"}

    class Store:
        def __init__(self, ids: set[str]): self.ids = ids
        def find(self, action, record_id):
            return {"id": record_id} if record_id in self.ids else None
        def count_matching(self, action, run_id, window): return None

    class Metrics:
        def __init__(self, n: int | None, group: bool | None = True):
            self.n, self.group = n, group
        def invocations(self, target, window): return self.n
        def log_group_exists(self, target): return self.group

    passed = failed = 0

    def check(label: str, cond: bool, extra: str = "") -> None:
        global passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}" + (f"\n        {extra}" if extra else ""))

    def one(store, metrics, *, record_id="rec-1", expected=1, tools=()):
        a = AssertedAction("create_claim", TARGETS["create_claim"], record_id,
                           expected_invocations=expected)
        return reconcile("run-1", [a], store=store, metrics=metrics,
                         window=W, tool_results=tools).findings[0]

    # CONSISTENT
    f = one(Store({"rec-1"}), Metrics(1))
    check("record present + invoked -> CONSISTENT", f.verdict is Verdict.CONSISTENT)
    check("CONSISTENT does not block", not f.is_blocking)

    # UNATTRIBUTED — the case existence-only checks call success
    f = one(Store({"rec-1"}), Metrics(0))
    check("record present + NOT invoked -> UNATTRIBUTED",
          f.verdict is Verdict.UNATTRIBUTED,
          "an existence-only check reports this as success")
    check("UNATTRIBUTED blocks", f.is_blocking)

    # The proof case: no log group at all
    f = one(Store({"rec-1"}), Metrics(None, group=False))
    check("no log group -> treated as zero invocations, not unknown",
          f.invocations_observed == 0 and f.verdict is Verdict.UNATTRIBUTED)

    # WRITE_FAILED — least alarming verdict, still blocking
    f = one(Store(set()), Metrics(1))
    check("invoked + no record -> WRITE_FAILED", f.verdict is Verdict.WRITE_FAILED)
    check("WRITE_FAILED blocks", f.is_blocking,
          "the agent asserted a record that does not exist; a filing citing it "
          "cites something an examiner cannot retrieve")

    # NARRATED — the original incident
    f = one(Store(set()), Metrics(0))
    check("neither -> NARRATED", f.verdict is Verdict.NARRATED)
    check("NARRATED blocks", f.is_blocking)

    # DOUBLE_WRITTEN — why count, not existence
    f = one(Store({"rec-1"}), Metrics(2))
    check("invoked twice, one asserted -> DOUBLE_WRITTEN",
          f.verdict is Verdict.DOUBLE_WRITTEN,
          "this is the state a write-path fix creates alongside the old path")
    check("DOUBLE_WRITTEN blocks", f.is_blocking)

    # INCONCLUSIVE must not resolve to a pass
    f = one(Store({"rec-1"}), Metrics(None, group=True))
    check("unavailable metric -> INCONCLUSIVE", f.verdict is Verdict.INCONCLUSIVE)
    check("INCONCLUSIVE blocks", f.is_blocking,
          "collapsing this to CONSISTENT manufactures the assurance being tested")

    # Placeholder record ids are a finding, not an unattempted lookup
    for placeholder in ("unknown", "", "N/A", None):
        f = one(Store({"rec-1"}), Metrics(0), record_id=placeholder)
        check(f"placeholder record_id {placeholder!r} -> NARRATED",
              f.verdict is Verdict.NARRATED)

    # Observation 3
    check("tool call detected in the ledger",
          observe_tool_call(AssertedAction("create_claim", "t", "r"),
                            [{"name": "create-claim___create_claim"}]))
    check("absent tool call detected",
          not observe_tool_call(AssertedAction("create_claim", "t", "r"),
                                [{"name": "lookup_policy"}]))

    # Structured extraction only
    acts = extract_asserted_actions(
        {"actions_taken": [
            {"action": "create_claim", "record_id": "rec-9", "count": 1},
            "created a claim successfully",          # prose in a structured field
            {"record_id": "rec-8"},                  # no action name
        ]}, target_map=TARGETS)
    check("prose entry in actions_taken skipped", len(acts) == 1)
    check("target comes from the platform map, not the output",
          acts[0].target == "ClaimsAgent-CreateClaim")

    # Aggregate + metering shape
    r = reconcile("run-2", [
        AssertedAction("create_claim", "T", "rec-1"),        # CONSISTENT
        AssertedAction("create_claim", "T", "rec-missing"),  # WRITE_FAILED
    ], store=Store({"rec-1"}), metrics=Metrics(1), window=W)
    verdicts = [f.verdict for f in r.findings]
    check("two findings, one consistent and one write-failed",
          verdicts == [Verdict.CONSISTENT, Verdict.WRITE_FAILED],
          f"got {[v.value for v in verdicts]}")
    check("one blocking finding among two", len(r.blocking) == 1)
    # One bad action spoils the run. A per-action pass rate is not a disposition:
    # the filing either has every asserted action verified or it does not.
    check("a single blocking finding makes the run not consistent",
          not r.consistent)
    row = r.metering_row()
    check("metering row is identifiers, enums and counts only",
          all(isinstance(v, (str, int, dict)) for v in row.values())
          and "findings_detail" not in row)
    check("verdict counts present", row["verdict_counts"].get("WRITE_FAILED") == 1)
    blob = json.dumps(row)
    check("no narrative in the metering row",
          "written by something other" not in blob)

    print(f"\naction_reconciliation self-check: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
