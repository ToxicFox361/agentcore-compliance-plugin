"""Two projections of one agent run: what may reach AWS-side logs, and what
belongs in the firm's own encrypted store.

A compliance output carries PII by construction. `rationale`,
`red_flags[].statement`, `gaps` and `recommended_actions` are narrative prose
about a named person's transactions — a 120-word rationale contains names,
amounts, employers and locations. "The output JSON has no PII" therefore cannot
hold for the whole output, and it cannot be secured by instructing the model to
omit PII: a prompt instruction is a request, not a control
(references/control-stack.md, layers 2 and 5). The split has to be a
deterministic gate between generation and logging. This file is that gate.

Four failure modes it exists to prevent:

  1. **The full output logged "just for debugging".** That puts a named
     customer's transaction narrative in a log group the firm does not control,
     outside its tenant key, its retention schedule and any erasure path it can
     operate. The bytes are unrecoverable once written.
  2. **A new schema field leaking on its first deploy** — closed by making the
     gate an allowlist. See METERING_ALLOWLIST for the argument.
  3. **An allowlisted field carrying unexpected content** — `alert_id` holding
     "alert for Maria Gonzalez, EUR 9,400 structuring" instead of a UUID. Every
     permitted field declares a type predicate, and a value failing its
     predicate is diverted AND reported as a defect.
  4. **A dev profile reaching production** — closed by `Profile`, whose
     permissive path requires an explicit `synthetic_data=True` assertion that
     PROD rejects outright.

METERING_ALLOWLIST is the authoritative list of what may cross into an AWS-side
log; it is deliberately not restated here, because a second copy would drift.
Everything else — the reasoning trace, every narrative field, the retrieved
evidence, the raw output — forms the examinable record and lives in the firm's
own store under a tenant-scoped key. `internal_record` builds that bundle and
`encrypt_for_tenant` is the seam that wraps it.

`sweep_metering` re-reads the assembled projection for free text and PII shapes
and blocks emission on a hit. Be clear about what it is: a regex cannot prove a
string is not personal data. It catches values that entered the projection
without passing a predicate, and shapes no predicate thought to reject. **The
allowlist is the control. The sweep is the seatbelt.**

Stdlib only, so the gate can run in a unit test as easily as in the agent
container. boto3 is needed by the encryption seam alone and is imported guarded,
so this module imports with no AWS credentials present.

See references/control-stack.md ("Audit record: what to persist per invocation")
for where this sits in the control stack, and references/audit-trail.md for the
storage mechanism, the retention tiers and the crypto-shredding constraints on
the tenant key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

# boto3 is needed only by the encryption seam at the bottom of this file. The
# gate itself must be importable — and testable — with no AWS credentials and no
# SDK installed, because a control that can only run in a deployed container is
# a control nobody exercises in CI.
try:  # pragma: no cover - import guard, not logic
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]


# ── Closed vocabularies ──────────────────────────────────────────────────────
#
# The strings that may appear AWS-side, enumerated. Closed sets rather than "any
# short string" is what lets the sweep distinguish a declared symbol from free
# text: `get_transaction_history` is safe because it is on this list, not because
# it looks tidy.
#
# The intended consequence: a newly added tool, table or workflow is diverted and
# flagged on its first run rather than emitted. That is a five minute edit to a
# constant. A permissive shape rule that admits whatever the next feature emits
# is not fixable after the fact.

WORKFLOW_NAMES = frozenset({
    "alert_triage", "case_summarisation", "copilot_question", "sar_draft",
    "edd_review", "ongoing_due_diligence", "qa_sample", "fraud_detection",
})

# The Gateway tool registry, which is known at deploy time — the model is only
# ever offered this filtered list (examples/agent_template.py).
TOOL_NAMES = frozenset({
    "get_alert", "get_customer_profile", "get_transaction_history",
    "get_prior_alerts", "get_sanctions_screening", "get_typology_reference",
    "search_adverse_media",
})

# Source tables for the row identifiers read as references during fact
# collection. Grouping by table is what makes "which records did this agent
# read" answerable — a flat list of UUIDs is not an answer, because a UUID with
# no table is not resolvable to a record.
SOURCE_TABLES = frozenset({
    "alerts", "customers", "accounts", "transactions", "prior_reports",
    "screening_results",
})

RECOMMENDATIONS = frozenset({"APPROVE", "STEP_UP_AUTH", "REJECT"})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})

# Typology labels are an enum, not prose — the same bounded-assertion argument as
# examples/output_validation.py. A free-text typology would be a narrative field
# wearing a classification field's name.
TYPOLOGIES = frozenset({
    "STRUCTURING", "ACCOUNT_TAKEOVER", "MULE_ACTIVITY", "TRADE_BASED",
    "SANCTIONS_EVASION", "TERRORIST_FINANCING", "FRAUD_SCAM", "UNCLEAR",
})

# The canonicalisation-and-MAC scheme label. It travels next to the hash rather
# than inside it, because a bare hex string has to satisfy `is_hash` and a
# prefixed one would not. See `content_hash` for why the label is not optional.
HASH_ALGORITHM = "hmac-sha256-canonical-json-v1"
HASH_ALGORITHMS = frozenset({HASH_ALGORITHM})


# ── Predicates ───────────────────────────────────────────────────────────────
#
# A predicate is a named check plus, where it has one, the vocabulary or pattern
# of strings it accepts. The sweep at the bottom reuses those declarations, so
# adding a permitted field in one place cannot leave the sweep out of step with
# the allowlist — the drift between two lists that must agree is its own failure
# mode, and this file would otherwise have two such lists.

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Versions are shape-checked rather than enumerated: they change every deploy, so
# a closed set would be stale before it shipped. The shape is deliberately narrow
# — lowercase, no spaces, 64 chars — so a version field cannot become a comment.
#
# The `(?=.*\d)` is load-bearing and not decoration. The sweep reuses this pattern
# as an allowed shape, so without a required digit it would fullmatch any single
# lowercase word: `"gonzalez"` would clear the sweep anywhere in the projection.
# Requiring a digit closes the widest hole this file's own backstop would
# otherwise have. Every real version we emit has one (`alert-triage-v1`,
# `eu.amazon.nova-2-lite-v1:0`); one that does not is a version worth renaming.
_VERSION_RE = re.compile(r"(?=.*\d)[a-z0-9][a-z0-9._:\-]{0,63}")


@dataclass(frozen=True)
class Predicate:
    """A type check on one allowlisted field.

    `vocabulary` and `pattern` are what the field is *declared* to emit. The
    sweep trusts nothing else, which is why they are part of the predicate
    rather than a second table maintained alongside it.
    """

    name: str
    check: Callable[[Any], bool]
    vocabulary: frozenset[str] = frozenset()
    pattern: re.Pattern[str] | None = None

    def __call__(self, value: Any) -> bool:
        return self.check(value)


def _is_canonical_uuid(value: Any) -> bool:
    # `uuid.UUID()` also accepts "urn:uuid:...", braces, and bare 32-hex. All
    # parse; none is the string you validated. Requiring the round-trip means the
    # value that goes into the log is the value the predicate inspected, so a
    # later consumer splitting on "-" cannot be surprised.
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return str(parsed) == value.lower()


is_uuid = Predicate("is_uuid", _is_canonical_uuid, pattern=_UUID_RE)

is_bool = Predicate("is_bool", lambda v: isinstance(v, bool))


def is_enum(members: Iterable[str]) -> Predicate:
    """A value drawn from a closed set, declared to the sweep."""
    allowed = frozenset(members)
    return Predicate(
        name=f"is_enum[{len(allowed)}]",
        check=lambda v: isinstance(v, str) and v in allowed,
        vocabulary=allowed,
    )


def is_int_range(lo: int, hi: int) -> Predicate:
    """A bounded integer. `bool` is excluded explicitly.

    In Python `True` is an `int` equal to 1, so a naive range check accepts
    `risk_score: true` and passes a boolean off as a score. Cheap to exclude,
    invisible if you do not.
    """
    return Predicate(
        name=f"is_int_range[{lo},{hi}]",
        check=lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi,
    )


def is_hex(nchars: int) -> Predicate:
    """Lowercase fixed-length hex — a digest or a W3C trace ID."""
    pattern = re.compile(rf"[0-9a-f]{{{nchars}}}")
    return Predicate(
        name=f"is_hex[{nchars}]",
        check=lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None,
        pattern=pattern,
    )


is_hash = is_hex(64)        # HMAC-SHA256 hexdigest
is_trace_id = is_hex(32)    # W3C Trace Context trace-id, 16 bytes as hex

is_version = Predicate(
    name="is_version",
    check=lambda v: isinstance(v, str) and _VERSION_RE.fullmatch(v) is not None,
    pattern=_VERSION_RE,
)


def _is_non_negative_number(value: Any) -> bool:
    # `math.isfinite` is the point of this helper. A cost computed from a rate
    # table can arrive as `inf` or `nan` when a divisor is zero, and both are
    # floats that pass `>= 0` checks written the obvious way (`nan >= 0` is
    # False, `inf >= 0` is True) — so one silently emits and the other silently
    # diverts. Neither is a cost; both should be defects.
    #
    # `Decimal` is accepted because that is what a money value should be and what
    # examples/cost_tracking.py produces. Leave it out and every priced run's cost
    # diverts as a predicate failure — a `defects` list full of entries about a
    # field that was always correct, which is the fastest way to teach a team to
    # stop reading the defects list.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    if isinstance(value, Decimal):
        return value.is_finite() and value >= 0
    return math.isfinite(value) and value >= 0


is_non_negative_number = Predicate("is_non_negative_number", _is_non_negative_number)


def list_of(item: Predicate, *, max_items: int) -> Predicate:
    """A bounded list whose every element satisfies `item`.

    Bounded because an unbounded identifier list is how a metering event grows
    to megabytes and starts getting truncated by the log sink — and a truncated
    JSON record is an unparseable one, so the whole event is lost rather than
    shortened.
    """
    return Predicate(
        name=f"list_of[{item.name},<={max_items}]",
        check=lambda v: (
            isinstance(v, list) and len(v) <= max_items and all(item(e) for e in v)
        ),
        vocabulary=item.vocabulary,
        pattern=item.pattern,
    )


def mapping_of(key: Predicate, value: Predicate) -> Predicate:
    """A dict whose keys satisfy `key` and whose values satisfy `value`.

    The key predicate's vocabulary is carried through, which is what lets the
    sweep accept `"transactions"` as a dict key: it is a declared table name, not
    an arbitrary short string that looked harmless.
    """
    return Predicate(
        name=f"mapping_of[{key.name}->{value.name}]",
        check=lambda v: (
            isinstance(v, dict)
            and all(key(k) for k in v)
            and all(value(x) for x in v.values())
        ),
        vocabulary=key.vocabulary | value.vocabulary,
        pattern=value.pattern,
    )


is_identifier_list = list_of(is_uuid, max_items=500)
is_identifier_map = mapping_of(is_enum(SOURCE_TABLES), is_identifier_list)


# ── The allowlist ────────────────────────────────────────────────────────────
#
# Every field that may cross into an AWS-side log, with the predicate that
# decides whether this run's value is really that field.
#
# Why an allowlist and not a denylist, stated plainly because the choice gets
# revisited by every new maintainer: a denylist enumerates the fields you knew
# about when you wrote it. The schema then gains `subject_summary`,
# `counterparty_notes`, `analyst_hint` — each a narrative field, none on the
# list — and each leaks on the deploy that introduces it. The allowlist inverts
# the default: an unknown field is diverted, the reviewer notices a metering
# dashboard missing a column, and the fix is a one-line addition made
# deliberately. That difference — fails closed versus races the next schema
# change — is the difference between a control and a hope.

MAX_TOKENS_PER_RUN = 10_000_000
MAX_LATENCY_MS = 3_600_000

METERING_ALLOWLIST: dict[str, Predicate] = {
    # What ran
    "workflow": is_enum(WORKFLOW_NAMES),
    "invocation_count": is_int_range(1, 1_000),
    "tool_names_called": list_of(is_enum(TOOL_NAMES), max_items=100),

    # Who and what it was about. Pseudonymous, NOT anonymous — see the
    # anti-patterns at the bottom. These identifiers keep the metering store
    # inside the privacy perimeter.
    "tenant_id": is_uuid,
    "customer_id": is_uuid,
    "alert_id": is_uuid,
    "run_id": is_uuid,
    "trace_id": is_trace_id,

    # Which rows were read as references during fact collection. Identifiers
    # only, grouped by table, never content — the R2 rule in audit-trail.md.
    "records_read": is_identifier_map,

    # Usage. Four token axes, because a cached request bills on four and an
    # emitter that sends two makes a real zero indistinguishable from an
    # unreported one (examples/cost_tracking.py).
    "input_tokens": is_int_range(0, MAX_TOKENS_PER_RUN),
    "output_tokens": is_int_range(0, MAX_TOKENS_PER_RUN),
    "cache_read_tokens": is_int_range(0, MAX_TOKENS_PER_RUN),
    "cache_write_tokens": is_int_range(0, MAX_TOKENS_PER_RUN),
    "cost_usd": is_non_negative_number,
    "latency_ms": is_int_range(0, MAX_LATENCY_MS),

    # Structured output fields that carry no PII: enums, bounded ints, booleans.
    "recommendation": is_enum(RECOMMENDATIONS),
    "risk_score": is_int_range(0, 100),
    "confidence": is_enum(CONFIDENCE_LEVELS),
    "primary_typology": is_enum(TYPOLOGIES),
    "escalation_recommended": is_bool,
    "account_takeover_suspected": is_bool,
    "customer_may_be_victim": is_bool,

    # Provenance. A stored output is only interpretable against the prompt,
    # schema and model in force when it was produced — so the metering row
    # carries them too, or a metering-only trend line cannot be attributed to
    # the version of the system that produced it.
    #
    # `model_id` is the RESOLVED model ID, not an inference-profile ARN. An ARN
    # fails `is_version` (slashes, and a 12-digit account number the sweep reads
    # as a long digit run) and would be diverted as a defect. Resolve the profile
    # to its model first — examples/cost_tracking.py already does that for
    # pricing, and the two should agree on what they call the model.
    "schema_version": is_version,
    "prompt_version": is_version,
    "model_id": is_version,

    # The pairing to the internal bundle.
    "content_hash": is_hash,
    "content_hash_alg": is_enum(HASH_ALGORITHMS),
}

# Narrative fields whose CARDINALITY is safe and whose content is not. The count
# is derived here; the field itself is never allowlisted, so there is no
# configuration in which the prose is emitted by accident.
#
# `additional_typologies` earns its place: its count is the signal that a second
# concurrent typology was identified at all, which is the missed-concurrent-
# typology failure from examples/alert_triage_prompt.md made visible in metering
# without shipping a word of the finding.
DERIVED_COUNTS: dict[str, str] = {
    "red_flags": "red_flags_count",
    "mitigating_factors": "mitigating_factors_count",
    "gaps": "gaps_count",
    "recommended_actions": "recommended_actions_count",
    "additional_typologies": "additional_typologies_count",
}

# The key under which the dev profile quarantines the full record. Named as a
# constant so the sweep can skip exactly this key and nothing else.
DEV_FULL_RECORD_KEY = "dev_full_record_synthetic_only"


class Profile(Enum):
    """Deployment profile. PROD is the default everywhere it appears.

    The failure mode this enum closes: `PROFILE = os.getenv("PROFILE", "dev")` in
    a container whose environment was not fully templated. One missing variable
    and every run writes its full narrative output to CloudWatch under a real
    customer's name. Defaulting to the safe profile is the only version of this
    that survives a misconfigured deployment — and mid-rollout, misconfigured is
    the normal state.
    """

    PROD = "prod"
    DEV = "dev"


class ProfileAssertionError(ValueError):
    """The profile and the synthetic-data assertion contradict each other."""


class MeteringLeakBlocked(RuntimeError):
    """The sweep found free text or a PII shape in the metering projection.

    Blocking rather than a warning, because a warning inside a log pipeline is
    read by nobody and the failure is unrecoverable the instant the bytes land in
    a log the firm does not control. Losing one metering event is recoverable;
    disclosing a narrative is not.
    """


@dataclass(frozen=True)
class Diversion:
    """One field that did not reach the metering projection, and why."""

    field_name: str
    reason: str
    predicate: str | None = None
    is_defect: bool = False

    def __str__(self) -> str:
        where = f" (predicate {self.predicate})" if self.predicate else ""
        return f"{self.field_name}: {self.reason}{where}"


@dataclass
class Projection:
    """The two artefacts and the record of what moved between them.

    Both halves come out of one call to `split` on purpose. A codebase where the
    metering dict and the internal bundle are assembled by two functions grows a
    third caller that builds only the metering half, from the raw record, with
    no gate in between — and that caller is the leak. Making the gate the only
    producer of either half means there is no "quick" path that skips it.
    """

    metering: dict[str, Any]
    internal: dict[str, Any]
    diversions: list[Diversion] = field(default_factory=list)
    profile: Profile = Profile.PROD
    synthetic_data: bool = False

    @property
    def defects(self) -> list[Diversion]:
        """Allowlisted fields whose value failed its predicate.

        Surface these loudly — alarm on a non-empty list, do not merely count
        it. A field on the allowlist carrying something other than its declared
        type means either the schema moved or the model is filling a structured
        slot with prose. The first is a deploy that needs a gate update; the
        second is the exact mechanism by which PII reaches an allowlisted field,
        and the only reason it did not reach the log this time is that the
        predicate happened to be narrow enough.
        """
        return [d for d in self.diversions if d.is_defect]


# ── The gate ─────────────────────────────────────────────────────────────────

def metering_projection(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Diversion]]:
    """Project `record` down to the fields permitted in an AWS-side log.

    Returns the projection and the list of diversions. Does not add the content
    hash — that is `split`'s job, because the hash is over the internal bundle
    and so cannot be computed before the bundle exists.

    Three outcomes per field, and the middle one is the interesting one:

      * on the allowlist and the predicate holds -> copied
      * on the allowlist and the predicate fails -> diverted AND flagged as a
        defect
      * not on the allowlist -> diverted, quietly, because narrative fields
        living in the internal record is the normal case and not news
    """
    metering: dict[str, Any] = {}
    diversions: list[Diversion] = []

    for key, value in record.items():
        predicate = METERING_ALLOWLIST.get(key)
        if predicate is not None:
            if predicate(value):
                metering[key] = value
            else:
                diversions.append(Diversion(
                    field_name=key,
                    reason=f"allowlisted but failed its type predicate: {value!r:.80}",
                    predicate=predicate.name,
                    is_defect=True,
                ))
            continue

        count_key = DERIVED_COUNTS.get(key)
        if count_key is not None:
            if isinstance(value, list):
                metering[count_key] = len(value)
                diversions.append(Diversion(
                    field_name=key,
                    reason=f"narrative content -> internal record (cardinality "
                           f"emitted as {count_key})",
                ))
            else:
                # A narrative list arriving as something other than a list is a
                # schema defect worth naming. The content diverts either way, so
                # this is about noticing rather than about containment — but a
                # missing count column on a dashboard with no defect recorded is
                # how "the model stopped emitting red_flags" goes unnoticed.
                diversions.append(Diversion(
                    field_name=key,
                    reason=f"narrative field is not a list, {count_key} not "
                           f"derivable: {type(value).__name__}",
                    is_defect=True,
                ))
            continue

        diversions.append(Diversion(
            field_name=key,
            reason="not on the metering allowlist -> internal record",
        ))

    return metering, diversions


def internal_record(
    record: Mapping[str, Any],
    *,
    reasoning_trace: str | None,
    retrieved_evidence: Any,
    diversions: Iterable[Diversion],
    tenant_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Build the examinable bundle: everything the metering projection is not.

    This is the artefact an examiner is shown, so it holds the full output
    verbatim rather than a redaction of it — a redacted narrative cannot be
    defended as the record of the decision, and the whole point of the split is
    that redaction is unnecessary once the two artefacts have different homes.

    The reasoning trace is stored as testimony, not causation: it is the model's
    narration of a process, not a verified account of one, and it discharges
    neither citation grounding nor action grounding
    (references/control-stack.md). Storing it is still right — it is the first
    thing an examiner asking "how was this concluded" wants to read.

    Ready for `encrypt_for_tenant`, not encrypted here. Keeping construction and
    encryption separate is what lets a test assert the bundle's contents without
    a KMS call.
    """
    return {
        "tenant_id": tenant_id,
        "run_id": run_id,
        # Verbatim. Not a copy of the diverted fields — the whole output, so a
        # reconstruction does not depend on this gate's field-by-field decisions
        # having been right.
        "output": dict(record),
        "reasoning_trace": reasoning_trace,
        "retrieved_evidence": retrieved_evidence,
        # Which fields the gate held back, so the pair of artefacts is
        # self-describing: reading the internal record tells you what the
        # metering row could not have contained.
        "diverted_fields": sorted({d.field_name for d in diversions}),
    }


def split(
    record: Mapping[str, Any],
    *,
    tenant_id: str,
    run_id: str,
    hmac_key: bytes,
    reasoning_trace: str | None = None,
    retrieved_evidence: Any = None,
    profile: Profile = Profile.PROD,
    synthetic_data: bool = False,
) -> Projection:
    """Split one validated agent output into its two projections.

    Call this AFTER examples/output_validation.py has passed the output and
    BEFORE anything is logged or stored. Nothing downstream should ever see the
    raw record again.

    `profile` defaults to PROD and `synthetic_data` defaults to False, so the
    strict path is what you get by omission. The two arguments are checked
    against each other rather than trusted independently:

      * PROD with `synthetic_data=True` raises. A caller asserting synthetic
        data in production is either wrong about the data or wrong about the
        profile, and both are worth stopping at the call site. Silently
        ignoring the flag would teach callers that it does not matter.
      * DEV without `synthetic_data=True` falls back to the strict projection
        and records a defect. A dev deployment that cannot assert its fixtures
        are synthetic might be pointed at a production replica, and that is the
        case where being permissive is worst.
    """
    if profile is Profile.PROD and synthetic_data:
        raise ProfileAssertionError(
            "synthetic_data=True asserted under Profile.PROD. Production data is "
            "not synthetic; if this really is a fixture run, set "
            "profile=Profile.DEV explicitly."
        )

    metering, diversions = metering_projection(record)

    bundle = internal_record(
        record,
        reasoning_trace=reasoning_trace,
        retrieved_evidence=retrieved_evidence,
        diversions=diversions,
        tenant_id=tenant_id,
        run_id=run_id,
    )

    # Hash the bundle, then pin the hash on the metering row. Order is forced:
    # the hash is the pairing between the two artefacts, so it cannot be
    # computed before the artefact it identifies exists.
    metering["content_hash"] = content_hash(bundle, key=hmac_key)
    metering["content_hash_alg"] = HASH_ALGORITHM
    bundle["content_hash"] = metering["content_hash"]
    bundle["content_hash_alg"] = HASH_ALGORITHM

    permissive = profile is Profile.DEV and synthetic_data
    if profile is Profile.DEV and not synthetic_data:
        diversions.append(Diversion(
            field_name="__profile__",
            reason="Profile.DEV without synthetic_data=True — strict projection "
                   "applied. Assert the data is synthetic or fix the profile.",
            is_defect=True,
        ))

    if permissive:
        # The one place the rule is relaxed, and it took a positive assertion in
        # code to get here — not an env var, not a default, not a config file
        # someone can copy between environments. Quarantined under a single
        # named key so the sweep can skip exactly this and keep checking
        # everything else.
        metering[DEV_FULL_RECORD_KEY] = dict(record)

    return Projection(
        metering=metering,
        internal=bundle,
        diversions=diversions,
        profile=profile,
        synthetic_data=synthetic_data,
    )


# ── Last-resort sweep ────────────────────────────────────────────────────────
#
# Everything below runs on the ASSEMBLED metering projection, after the
# allowlist has already had its say. It exists because the allowlist governs
# fields, and a projection is also touched by assembly code: derived counts, the
# content hash, whatever a future maintainer appends to the dict between `split`
# and the log call. Those values never met a predicate.
#
# It is a backstop and not a proof. A regex cannot establish that a string is
# not personal data — "Smith" is eight characters of lowercase letters and a
# surname. What it can do is reject shapes that no permitted field should ever
# produce, and that is worth having as long as nobody mistakes it for the
# control. The allowlist is the control.

MAX_METERING_STRING = 72

# Any whitespace at all. Every permitted string is an identifier, an enum member,
# a hex digest or a version — none contains a space. A space is the single most
# reliable indicator that prose has arrived somewhere prose does not belong.
_WHITESPACE_RE = re.compile(r"\s")

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
# Loose on purpose: a false positive costs one blocked metering event and an
# investigation, a false negative costs a disclosed phone number.
_PHONE_RE = re.compile(r"\+?\d[\d().\-]{7,}\d")
_IBAN_RE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
_LONG_DIGITS_RE = re.compile(r"\d{9,}")

# An integer with nine or more digits in a metric field is more plausibly an
# account number, a card number or a national ID than a measurement. Every
# numeric field here is bounded well below that by its predicate, so a value this
# large means something bypassed one.
MAX_METERING_INT = 100_000_000

_NUMERIC_STRING_RE = re.compile(r"-?\d{1,8}(\.\d{1,6})?")


def _safe_vocabulary() -> frozenset[str]:
    """Strings some allowlist predicate declared it would emit.

    Derived from the allowlist rather than restated, so the sweep cannot drift
    out of step with the gate it backs up.
    """
    vocab: set[str] = set(METERING_ALLOWLIST)
    vocab |= set(DERIVED_COUNTS.values())
    vocab.add(DEV_FULL_RECORD_KEY)
    for predicate in METERING_ALLOWLIST.values():
        vocab |= predicate.vocabulary
    return frozenset(vocab)


def _safe_patterns() -> tuple[re.Pattern[str], ...]:
    seen: dict[str, re.Pattern[str]] = {}
    for predicate in METERING_ALLOWLIST.values():
        if predicate.pattern is not None:
            seen[predicate.pattern.pattern] = predicate.pattern
    seen[_NUMERIC_STRING_RE.pattern] = _NUMERIC_STRING_RE
    return tuple(seen.values())


SAFE_VOCABULARY = _safe_vocabulary()
SAFE_PATTERNS = _safe_patterns()


def _sweep_string(path: str, value: str) -> list[str]:
    hits: list[str] = []

    # Shape rejections run FIRST, so a vocabulary entry that somehow contains a
    # space or an email is still blocked. Ordering the allow-check first would
    # make the vocabulary a bypass.
    if len(value) > MAX_METERING_STRING:
        hits.append(f"{path}: string of {len(value)} chars exceeds "
                    f"{MAX_METERING_STRING} — free text")
    if _WHITESPACE_RE.search(value):
        hits.append(f"{path}: string contains whitespace — free text")
    if _EMAIL_RE.search(value):
        hits.append(f"{path}: email-shaped substring")
    if _IBAN_RE.search(value):
        hits.append(f"{path}: IBAN-shaped substring")
    if _PHONE_RE.search(value) or _LONG_DIGITS_RE.search(value):
        # Digests and trace IDs are hex and can legitimately contain long digit
        # runs, so exempt exactly the shapes that are declared hex.
        if not any(p.fullmatch(value) for p in SAFE_PATTERNS):
            hits.append(f"{path}: long digit run or phone-shaped substring")

    if hits:
        return hits

    if value in SAFE_VOCABULARY:
        return hits
    if any(p.fullmatch(value) for p in SAFE_PATTERNS):
        return hits

    return [f"{path}: {value!r:.60} is not a declared enum member, UUID, hex "
            f"digest, version or number"]


def sweep_metering(projection: Projection) -> list[str]:
    """Re-read the assembled metering projection for free text and PII shapes.

    Returns the hits. `emit_metering` turns a non-empty result into a blocking
    error; this function is separate so a test can assert on the hits without
    catching an exception.

    In the dev-permissive profile the quarantined full record is skipped and
    every other key is still checked — a relaxed rule is not the same as no
    rule, and the fields that feed dashboards should be as clean in dev as in
    prod so a leak is not discovered only after promotion.
    """
    hits: list[str] = []
    skip_dev_key = projection.profile is Profile.DEV and projection.synthetic_data

    def walk(node: Any, path: str) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, str):
            hits.extend(_sweep_string(path, node))
            return
        if isinstance(node, int):
            if abs(node) >= MAX_METERING_INT:
                hits.append(f"{path}: integer {node} is too large for any metric "
                            f"field — possible account or ID number")
            return
        if isinstance(node, (float, Decimal)):
            finite = node.is_finite() if isinstance(node, Decimal) \
                else math.isfinite(node)
            if not finite:
                hits.append(f"{path}: non-finite number {node!r}")
            elif abs(node) >= MAX_METERING_INT:
                hits.append(f"{path}: number {node} is too large for any metric "
                            f"field")
            return
        if node is None:
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    hits.append(f"{path}.{key!r}: non-string key")
                    continue
                if path == "" and skip_dev_key and key == DEV_FULL_RECORD_KEY:
                    continue
                hits.extend(_sweep_string(f"{path}.{key} (key)" if path else
                                          f"{key} (key)", key))
                walk(child, f"{path}.{key}" if path else key)
            return
        if isinstance(node, (list, tuple)):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")
            return

        # An object of a type the sweep does not understand cannot be cleared.
        # Defaulting to "probably fine" here would make the sweep's silence
        # meaningless for every type added later.
        hits.append(f"{path}: unexpected type {type(node).__name__} in metering "
                    f"projection")

    walk(projection.metering, "")
    return hits


def emit_metering(projection: Projection) -> dict[str, Any]:
    """The only way a metering projection leaves this module.

    Sweeps, then returns the dict for the log sink. Raises on any hit, so a
    caller cannot get a partially-cleared projection and decide for itself
    whether the hits mattered.

    Requires a `Projection` rather than a plain dict. That type can only be
    produced by `split`, which makes "log the raw output" a type error at the
    call site instead of a code review someone has to catch.

    One thing the sweep cannot check for you: `cost_usd` is a `Decimal`, which is
    correct for money and which `json.dumps` refuses without an encoder. Give the
    sink one. A metering event the encoder rejects is not a truncated event, it is
    a dropped one, and a dropped metering event is a silent billing gap for that
    tenant — the failure surfaces on the invoice, not in the logs.
    """
    if not isinstance(projection, Projection):
        raise TypeError(
            f"emit_metering requires a Projection produced by split(), got "
            f"{type(projection).__name__} — the gate is not optional."
        )

    hits = sweep_metering(projection)
    if hits:
        raise MeteringLeakBlocked(
            f"{len(hits)} sweep hit(s) in the metering projection; nothing "
            f"emitted:\n  " + "\n  ".join(hits)
        )
    return dict(projection.metering)


# ── Hashing is not encryption ────────────────────────────────────────────────
#
# These are two different mechanisms answering two different questions, and
# conflating them produces an archive nobody can read.
#
#   * `content_hash` is one-way. It gives tamper evidence — the metering row and
#     the internal bundle are provably the same run — and a blind index for
#     lookup. It does NOT give retrievability. A record that is only hashed
#     satisfies the integrity obligation and fails the production obligation:
#     you cannot show an examiner a hash, and no amount of key material turns
#     one back into a narrative.
#   * `encrypt_for_tenant` is reversible. It is what makes the examinable record
#     producible years later, and what makes erasure possible by destroying the
#     key rather than the WORM object (references/audit-trail.md).
#
# Both, always. Hash for indexing and pairing, encrypt for retrieval.

def canonical_json(payload: Any) -> bytes:
    """Deterministic serialisation. Part of the record format, not a detail.

    PUBLIC deliberately, despite reading like an internal helper. Any module that
    stores a hash of a record — `audit_record.py` does — must serialise through
    exactly this function, because every stored digest is over these exact bytes.
    Leaving it private invites a second implementation elsewhere, and a second
    implementation is a fork of the record format that surfaces months later as a
    fleet-wide integrity alarm indistinguishable from tampering.

    The digest is over these exact bytes, so the canonicalisation rules are
    frozen by every hash already stored. Change the separators, the key order or
    the ASCII escaping and every historical pairing stops verifying — which
    surfaces as a fleet-wide integrity alarm indistinguishable from real
    tampering. That is why the scheme is versioned in HASH_ALGORITHM and why the
    version travels on the record next to the hash.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode("utf-8")


# Back-compat alias. `audit_record.py` imports the private name under an
# alias, and internal call sites below still use it. Both point at one
# implementation, which is the property that matters.
_canonical_json = canonical_json

def _json_default(value: Any) -> str:
    # Decimal is the one type worth handling, because cost arrives as one.
    # Everything else raises: a silent `str(value)` fallback would let two
    # different objects with the same repr hash identically, and would make the
    # digest depend on a repr that is not part of any contract.
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(
        f"{type(value).__name__} is not serialisable in the canonical form; "
        f"convert it explicitly before hashing"
    )


def content_hash(payload: Mapping[str, Any], *, key: bytes) -> str:
    """Keyed HMAC-SHA256 over the canonical serialisation of `payload`.

    Keyed, not a plain digest, and the reason is concrete: an unkeyed digest of
    a small guessable value is reversible by brute force. `sha256("STRUCTURING")`
    or `sha256(a customer's date of birth)` is a lookup oracle — anyone holding
    the hash can confirm a guess offline, which makes the "integrity artefact"
    a disclosure channel. An HMAC cannot be checked against a guess without the
    key.

    The key is tenant-scoped and shares the subject's lifecycle, so shredding
    the tenant key destroys the verification artefact along with the evidence
    instead of leaving a checkable fingerprint of erased data behind.

    Hash the BUNDLE, never individual field values, for the same oracle reason —
    a per-field hash of a name is a name lookup.

    Production alternative worth knowing: KMS HMAC keys with `GenerateMac` /
    `VerifyMac` keep the key out of application memory entirely. Two documented
    constraints decide whether that fits — `Message` is capped at 4,096 bytes,
    so a bundle must be digested locally and the DIGEST passed as the message;
    and KMS gives digests no special handling, so verification must present the
    same digest that was signed. Valid `MacAlgorithm` values are `HMAC_SHA_224`,
    `HMAC_SHA_256`, `HMAC_SHA_384` and `HMAC_SHA_512`.
    """
    if not key:
        raise ValueError(
            "content_hash requires a tenant-scoped HMAC key; an unkeyed digest "
            "is a lookup oracle, not an integrity artefact"
        )
    return hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()


def verify_pairing(projection: Projection, *, key: bytes) -> bool:
    """Confirm the metering row and the internal bundle describe the same run.

    `hmac.compare_digest`, not `==`. A plain string comparison on a MAC returns
    early at the first differing byte and leaks how much of a forgery was
    correct, which is enough to construct one byte at a time. The habit matters
    more than this call site does.
    """
    stored = projection.metering.get("content_hash")
    if not isinstance(stored, str):
        return False

    bundle = {k: v for k, v in projection.internal.items()
              if k not in {"content_hash", "content_hash_alg"}}
    return hmac.compare_digest(stored, content_hash(bundle, key=key))


# ── Encryption seam ──────────────────────────────────────────────────────────

@dataclass
class EncryptedBundle:
    """Envelope-encrypted internal record, ready for the firm's own store."""

    ciphertext: bytes
    wrapped_data_key: bytes
    key_id: str
    encryption_context: dict[str, str]
    algorithm: str


# (data_key, plaintext, associated_data) -> ciphertext
AeadEncrypt = Callable[[bytes, bytes, bytes], bytes]


def encrypt_for_tenant(
    bundle: Mapping[str, Any],
    *,
    tenant_key_arn: str,
    encryption_context: Mapping[str, str],
    aead: AeadEncrypt | None = None,
    kms_client: Any = None,
) -> EncryptedBundle:
    """Envelope-encrypt the internal record under a tenant-scoped KMS key.

    The KMS half is real. `GenerateDataKey` returns a plaintext data key and a
    copy wrapped under `tenant_key_arn`; you encrypt with the plaintext key,
    store the wrapped key beside the ciphertext, and throw the plaintext away.
    Verified parameters: `KeyId`, `EncryptionContext`, and `KeySpec` OR
    `NumberOfBytes` — never both.

    `encryption_context` is an access-control mechanism, not metadata. KMS
    requires a case-sensitive exact match on decrypt, so binding tenant, subject
    and record IDs into it means a ciphertext lifted from one tenant's store
    cannot be decrypted under another tenant's context even by a principal
    holding both grants. It is also logged in CloudTrail, so it must contain no
    PII — IDs only.

    The AEAD step is a seam, deliberately. The standard library has no
    authenticated cipher, and the wrong way to work around that is a hand-rolled
    CBC-plus-HMAC construction, which is how padding oracles get shipped. Pass
    `aead` — `cryptography`'s `AESGCM(key).encrypt(nonce, plaintext, aad)` or the
    AWS Encryption SDK, which handles nonce management, key commitment and the
    envelope format for you. With no `aead` this raises rather than returning
    something that looks encrypted.

    Hash separately with `content_hash`. Ciphertext is not an integrity artefact
    you can verify without decrypting, and the metering row must carry a value
    an examiner-facing check can use without the tenant key.
    """
    # A cheap check on a real mistake: putting a customer email in the encryption
    # context writes it to CloudTrail on every encrypt and decrypt, which is a
    # log the firm does not control and cannot crypto-shred. The context is an
    # access-control input, not a place to note who the record is about.
    for ctx_key, ctx_value in encryption_context.items():
        if _EMAIL_RE.search(str(ctx_value)) or _WHITESPACE_RE.search(str(ctx_value)):
            raise ValueError(
                f"encryption_context[{ctx_key!r}] is not an identifier: "
                f"{ctx_value!r}. The context is written to CloudTrail and must "
                f"hold IDs only."
            )

    client = kms_client
    if client is None:
        if boto3 is None:
            raise RuntimeError(
                "boto3 is not installed; pass kms_client explicitly or install "
                "it. The gate itself needs neither."
            )
        client = boto3.client("kms")

    response = client.generate_data_key(
        KeyId=tenant_key_arn,
        KeySpec="AES_256",
        EncryptionContext=dict(encryption_context),
    )
    # bytearray so the buffer can actually be overwritten below. `bytes` is
    # immutable, so zeroing a `bytes` data key is a no-op that reads like a
    # precaution — worth knowing that Python offers no strong guarantee here
    # either way, and that the real mitigation is a short-lived process.
    plaintext_key = bytearray(response["Plaintext"])
    wrapped_key = response["CiphertextBlob"]

    try:
        if aead is None:
            raise NotImplementedError(
                "no AEAD supplied. Pass aead=lambda k, pt, aad: "
                "AESGCM(k).encrypt(nonce, pt, aad) from `cryptography`, or use "
                "the AWS Encryption SDK. The standard library has no "
                "authenticated cipher and hand-rolling one is not an option."
            )
        associated_data = _canonical_json(dict(encryption_context))
        ciphertext = aead(bytes(plaintext_key), _canonical_json(bundle),
                         associated_data)
    finally:
        for i in range(len(plaintext_key)):
            plaintext_key[i] = 0

    return EncryptedBundle(
        ciphertext=ciphertext,
        wrapped_data_key=wrapped_key,
        key_id=response.get("KeyId", tenant_key_arn),
        encryption_context=dict(encryption_context),
        algorithm="kms-generate-data-key+aead",
    )


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# Every one of these has shipped somewhere, and each looks reasonable in the
# diff that introduces it.
#
# 1. `log.info(f"output: {output}")` for debugging. The most common route by
#    which a named customer's transaction narrative reaches a log group the firm
#    does not control. It is added under time pressure, it works, and it is never
#    removed because nothing breaks. Two AWS-side settings are the same defect at
#    platform scale: Bedrock model invocation logging and AgentCore
#    APPLICATION_LOGS both capture payloads verbatim, which is why in production
#    they are OFF rather than carefully configured — an account-and-Region-wide
#    destination with no per-tenant key cannot be made tenant-safe by
#    configuration.
#
# 2. A denylist of PII field names. `EXCLUDE = {"rationale", "red_flags",
#    "gaps"}` passes review, ships, and leaks the day someone adds
#    `analyst_summary` — see METERING_ALLOWLIST.
#
# 3. "The prompt tells the model not to include PII in the structured fields."
#    A prompt instruction is a request. It has no enforcement, no error, and no
#    evidence of having operated — and the fields in question are prose about a
#    named person, so compliance would require the model to solve the problem the
#    gate exists to solve. This is the skill's recurring point: an instruction is
#    not a control.
#
# 4. Storing only the hash as the archival record. Tamper evidence without
#    retrievability. An examiner cannot be shown a hash, the reasoning trace is
#    gone, and the discovery happens at the evidence request rather than at the
#    design review. Hash for pairing, encrypt for production.
#
# 5. Building the metering event in the same code path that writes the internal
#    record, with no gate between them. Someone needs "just the metrics" on a
#    path where the bundle is not, and reads them straight off the raw record —
#    no allowlist, no sweep, no reason to be noticed. See `Projection` for why
#    both halves come out of one call and why `emit_metering` refuses a dict.
#
# 6. Treating UUIDs as anonymous. They are PSEUDONYMOUS. With the mapping table
#    in the firm's own store, `customer_id` resolves to a person, which puts the
#    metering store inside the privacy perimeter: in scope for erasure requests,
#    for retention limits, for cross-border transfer analysis, and for the
#    tenant's own DSAR obligations. Two practical consequences people miss — a
#    metering store excluded from the erasure runbook because "it only holds
#    IDs" leaves a per-customer activity history behind after erasure completes;
#    and per-field hashing does not fix it, because a hash of a small guessable
#    value is a lookup oracle for that value.
#
# 7. Sweep hits logged as warnings. A warning inside a log pipeline is read by
#    nobody, and the event it warns about has already been written. Block.
#
#
# ── Verification ─────────────────────────────────────────────────────────────
#
# Exercise the paths that fail, not just the one that works:
#
#   1. A realistic triage output whose `rationale` and `red_flags[].statement`
#      name a person and quote amounts. Assert the metering projection contains
#      no whitespace-bearing string, that the narrative appears in the internal
#      bundle, and that `red_flags_count` is present while `red_flags` is not.
#   2. An allowlisted field carrying prose — `alert_id: "alert for Maria
#      Gonzalez"`. Assert it is absent from metering AND present in
#      `Projection.defects`. A test that only checks absence passes against an
#      implementation that drops the field silently, which is the version where
#      nobody learns the schema moved.
#   3. `Profile.PROD` with `synthetic_data=True`. Assert it raises.
#   4. `Profile.DEV` without `synthetic_data=True`. Assert the projection is
#      strict and a defect is recorded.
#   5. A field appended to `projection.metering` after `split` returns. Assert
#      `emit_metering` raises — this is the case the allowlist cannot see and the
#      only reason the sweep exists.
#   6. `verify_pairing` on a tampered bundle. Mutate one character of the
#      narrative in the internal record and assert it returns False.
#   7. A new tool name or source table not in the vocabularies. Assert it is
#      diverted and flagged, then confirm the fix is a constant edit. Failing
#      closed on a legitimate addition is the intended cost.


# ── Executable self-check ────────────────────────────────────────────────────
#
# The seven cases above, runnable: `python3 log_projection.py`.
#
# This gate is the boundary between a tenant's PII and a log estate the firm
# does not control. Documenting the cases it must pass and not shipping them
# executable is the same defect this skill names elsewhere — a control asserted
# in prose and unenforced in code. A reader who edits the allowlist, widens a
# predicate or adds a field needs a command that tells them whether the boundary
# still holds, and it has to run with no AWS account and no test framework.

if __name__ == "__main__":  # pragma: no cover
    import copy

    KEY = b"self-check-hmac-key-not-for-production-use"
    PII = ("Maria", "Gonzalez", "Northgate", "Rotterdam", "87400",
           "gonzalez@", "NL91ABNA", "+31 6")

    def _record() -> dict[str, Any]:
        """A triage output whose narrative names a person and quotes amounts.

        Deliberately realistic: this is what a compliance rationale looks like,
        and it is why "the output JSON has no PII" cannot hold for the whole
        output.
        """
        return {
            "workflow": "alert_triage",
            "invocation_count": 4,
            "tool_names_called": ["get_alert", "get_customer_profile",
                                  "get_transaction_history"],
            "tenant_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            "customer_id": "9c858901-8a57-4791-81fe-4c455b099bc9",
            "alert_id": "b3d4f8a1-0c2e-4b6d-9f1a-7e5c2d8a4b60",
            "run_id": "c1a2b3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
            "records_read": {
                "alerts": ["b3d4f8a1-0c2e-4b6d-9f1a-7e5c2d8a4b60"],
                "transactions": ["7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e",
                                 "8b2c3d4e-5f60-4172-9283-94ab5c6d7e8f"],
            },
            "input_tokens": 3199, "output_tokens": 612,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "cost_usd": Decimal("0.0184"), "latency_ms": 5824,
            "recommendation": "REJECT", "risk_score": 84, "confidence": "high",
            "primary_typology": "STRUCTURING",
            "escalation_recommended": True,
            "account_takeover_suspected": False,
            "customer_may_be_victim": False,
            "schema_version": "alert-triage-v1",
            "prompt_version": "alert-triage-v1",
            "model_id": "eu.amazon.nova-pro-v1",
            # Narrative — PII by construction. None of this may reach AWS.
            "rationale": ("Maria Gonzalez received EUR 87,400 across five "
                          "credits from Northgate Logistics in Rotterdam, each "
                          "below the 10,000 reporting threshold."),
            "red_flags": [
                {"statement": "Five credits from Northgate Logistics just under "
                              "the threshold", "kind": "OBSERVATION",
                 "evidence_id": "txn-3"},
                {"statement": "Counterparty registered 6 days before the first "
                              "credit", "kind": "OBSERVATION",
                 "evidence_id": "txn-1"},
            ],
            "gaps": ["No response to the contact attempt of 4 March"],
            "recommended_actions": ["Escalate to L2 for Maria Gonzalez"],
            "additional_typologies": ["MULE_ACTIVITY"],
        }

    passed = failed = 0

    def check(label: str, cond: bool) -> None:
        global passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")

    # 1 — the narrative must not survive into metering, and counts must replace it.
    p = split(_record(), tenant_id="t", run_id="r", hmac_key=KEY,
              reasoning_trace="Gonzalez appeared in two prior alerts.",
              retrieved_evidence={"note": "gonzalez@example.com, +31 6 1234"})
    emitted = emit_metering(p)
    blob = json.dumps(emitted, default=str)
    check("1 no PII token in metering", not any(t in blob for t in PII))
    check("1 no whitespace-bearing string",
          not any(isinstance(v, str) and (" " in v) for v in emitted.values()))
    check("1 narrative field names absent",
          not any(k in emitted for k in
                  ("rationale", "red_flags", "gaps", "recommended_actions")))
    check("1 counts present", emitted.get("red_flags_count") == 2
          and emitted.get("gaps_count") == 1)
    check("1 narrative retained internally",
          "Gonzalez" in json.dumps(p.internal, default=str))
    check("1 no defects on a clean record", not p.defects)
    check("1 pairing verifies", verify_pairing(p, key=KEY))

    # 2 — an allowlisted field carrying prose: diverted AND flagged, not dropped.
    r = _record()
    r["alert_id"] = "alert for Maria Gonzalez, EUR 87,400 structuring"
    r["risk_score"] = "high risk, see rationale"
    p2 = split(r, tenant_id="t", run_id="r", hmac_key=KEY)
    check("2 bad alert_id absent from metering", "alert_id" not in p2.metering)
    check("2 bad risk_score absent", "risk_score" not in p2.metering)
    check("2 both recorded as defects",
          {d.field_name for d in p2.defects} >= {"alert_id", "risk_score"})
    check("2 still present internally", "Maria Gonzalez" in
          json.dumps(p2.internal, default=str))

    # 3 — the permissive path cannot be reached from the strict profile.
    try:
        split(_record(), tenant_id="t", run_id="r", hmac_key=KEY,
              profile=Profile.PROD, synthetic_data=True)
        check("3 PROD+synthetic raises", False)
    except ProfileAssertionError:
        check("3 PROD+synthetic raises", True)

    # 4 — DEV that cannot assert synthetic data falls back to strict.
    p4 = split(_record(), tenant_id="t", run_id="r", hmac_key=KEY,
               profile=Profile.DEV)
    check("4 DEV without assertion is strict",
          not any(t in json.dumps(p4.metering, default=str) for t in PII))
    check("4 DEV without assertion flags a defect", bool(p4.defects))

    # 5 — the case the allowlist cannot see: a field appended after the gate ran.
    p5 = split(_record(), tenant_id="t", run_id="r", hmac_key=KEY)
    p5.metering["debug_note"] = "customer Maria Gonzalez, gonzalez@example.com"
    try:
        emit_metering(p5)
        check("5 post-split append blocked", False)
    except MeteringLeakBlocked:
        check("5 post-split append blocked", True)
    try:
        emit_metering(_record())  # type: ignore[arg-type]
        check("5 raw dict rejected", False)
    except (TypeError, AttributeError):
        check("5 raw dict rejected", True)

    # 6 — tamper detection, and key binding.
    p6 = split(_record(), tenant_id="t", run_id="r", hmac_key=KEY)
    t6 = copy.deepcopy(p6)
    t6.internal["output"]["rationale"] = \
        t6.internal["output"]["rationale"].replace("87,400", "87,401")
    check("6 tampered bundle fails pairing", not verify_pairing(t6, key=KEY))
    check("6 wrong key fails pairing", not verify_pairing(p6, key=b"other-key"))

    # 7 — an unknown symbol fails closed rather than passing through.
    r7 = _record()
    r7["tool_names_called"] = ["get_alert", "search_pep_register"]
    r7["records_read"] = {"pep_register": ["7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e"]}
    p7 = split(r7, tenant_id="t", run_id="r", hmac_key=KEY)
    check("7 unknown tool/table diverted",
          "tool_names_called" not in p7.metering
          or "search_pep_register" not in p7.metering.get("tool_names_called", []))
    check("7 and flagged", bool(p7.defects))

    print(f"\nlog_projection self-check: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
