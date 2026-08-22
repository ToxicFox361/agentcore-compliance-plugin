"""The far side of `route() -> "HUMAN_REVIEW"`.

`examples/output_validation.py` ends by returning the string `"HUMAN_REVIEW"` for
every path, which is correct and is also the entire human-in-the-loop
implementation. A string is a routing label. It is not a gate, it does not record
who decided, and nothing about it makes the reverse impossible. This file is what
sits behind that label.

**Two types, and no function that converts one into the other.** A `Proposal` is
what the model produced. A `Decision` is what a named human authored under their
own identity. There is no `Proposal.to_decision()`, no `promote()`, no
`status` field advancing from `PENDING` to `APPROVED`. That absence is the
control: `references/control-stack.md` Layer 1 requires the agent to be
structurally incapable of disposing of an alert, and the structural version of
that requirement is that the disposition type cannot be constructed from the
proposal type. If the two shared one row with a `status` column, then every code
path that can write `status` can dispose of an alert — a migration, a bulk
backfill, a retry handler, an admin screen, the agent's own tool if a filter is
ever mis-set. Separate records with separate authorship is also what makes the
maker-checker split reconstructable years later, because the question "who
decided this" has a row of its own to answer it rather than a mutation history to
be inferred from.

Six failure modes the guards in `approve` exist to prevent:

  1. **A double-submit becoming a second disposition.** Two clicks, a retried
     request, a client that fires on both `keyup` and `click`. Without a
     deterministic key and a conditional write, the second one is a new
     disposition of an already-disposed alert.
  2. **Approving against evidence that changed after drafting.** The reviewer
     read a draft grounded in one evidence set; a late-arriving transaction, a
     re-scored screening hit or a corrected counterparty changed it. Approving
     the old draft against the new evidence is approving something nobody read.
  3. **Approving a stale draft.** A proposal sitting in a queue for a week was
     produced against a customer state that has moved on.
  4. **An unentitled actor disposing.** Entitlement is per risk tier, and the
     role that can administer the platform is not thereby qualified to close a
     high-tier alert.
  5. **The requester approving their own request** where four-eyes applies.
  6. **The agent's own client performing the write.** The whole read-only
     property of the agent's tool list is defeated if the approval path reuses
     that client — and it is the natural thing to reuse, because it is already
     constructed and already has a session.

And one the graph validator prevents: **an AI node reaching a decisioning node
with nothing mandatory in between.** `control-stack.md` requires that to be
rejected in graph validation rather than left to convention, because convention
is what a new workflow author does not know about.

What AgentCore Identity does and does not give you, stated here because it is
routinely over-read: it proves **which user's token was exchanged** for the
credentials a call ran under. That is real and worth having. It is not evidence
that a qualified person read a draft and approved it — a token exchange happens
because a session existed, not because a human formed a judgement. The approval
record is yours to write, and this file is the shape of it.

Stdlib only, plus a guarded `boto3` seam, so this imports and self-checks with no
AWS credentials present.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import log_projection
from log_projection import (
    HASH_ALGORITHM,
    WORKFLOW_NAMES,
    Profile,
    Projection,
    content_hash,
    emit_metering,
    is_uuid,
    split,
)

try:  # pragma: no cover - import guard, not logic
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]


# ── Entitlement, keyed on the vocabularies that already exist ────────────────

ACTOR_ROLES = frozenset({
    "analyst_l1", "analyst_l2", "mlro", "qa_reviewer", "platform_admin",
})

RISK_TIERS = frozenset({"standard", "elevated", "high"})

# Who may dispose, by risk tier. Note who is absent: `platform_admin` is
# entitled to nothing here, and `qa_reviewer` only to the tier they sample.
# Administering the platform is not a compliance qualification, and the
# conflation is easy to make precisely because admin roles are the ones already
# holding every other permission — so "give them approve as well, they can do
# everything anyway" reads as tidying up rather than as widening a control.
ENTITLEMENTS: dict[str, frozenset[str]] = {
    "standard": frozenset({"analyst_l1", "analyst_l2", "mlro"}),
    "elevated": frozenset({"analyst_l2", "mlro"}),
    "high": frozenset({"mlro"}),
}

# Where the approver may not be the requester. Keyed on `log_projection`'s
# workflow vocabulary rather than on a second list of strings, so a renamed
# workflow cannot silently drop out of the four-eyes set — a four-eyes rule keyed
# on a name that no longer occurs is a rule that never fires and reports nothing.
FOUR_EYES_WORKFLOWS = frozenset({"sar_draft", "edd_review"})

_unknown = FOUR_EYES_WORKFLOWS - WORKFLOW_NAMES
if _unknown:
    # An `if` and a `raise`, not an `assert`: `python -O` strips assert
    # statements, and it strips the security-shaped ones first because those are
    # the checks people naturally express as invariants
    # (examples/tenant_isolation.py makes the same point).
    raise ValueError(
        f"FOUR_EYES_WORKFLOWS names workflows unknown to log_projection: "
        f"{sorted(_unknown)} — a four-eyes rule on a workflow name that never "
        f"occurs never fires"
    )

_MAX_RATIONALE_CHARS = 4_000
_IDEMPOTENCY_KEY_RE = re.compile(r"[0-9a-zA-Z._:-]{16,128}")

# The UUIDv5 namespace for derived decision IDs. Fixed for the life of the
# format: change it and every historical decision ID stops re-deriving, so a
# replay of an old submission would create a second disposition instead of
# colliding with the first.
_DECISION_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


# ── Refusals are typed and they raise ────────────────────────────────────────
#
# Raise rather than return a status, for the reason
# examples/output_validation.py gives about tool results: a function that
# promises a disposition either produces a real one or raises. A refusal returned
# as data becomes `outcome.get("approved", False)` at some call site, then
# `outcome.get("approved", True)` in the hotfix that "fixes the false negatives",
# and the gate is gone with nothing in the diff that looks like removing a
# control.

class ApprovalRefused(RuntimeError):
    """The gate refused. No decision was written."""


class ActorIdentityMismatch(ApprovalRefused):
    """The decision claims an actor other than the authenticated one."""


class EvidenceChanged(ApprovalRefused):
    """The evidence set no longer hashes to what the proposal was drafted on."""


class ProposalExpired(ApprovalRefused):
    """The proposal is past `expires_at`."""


class ActorNotEntitled(ApprovalRefused):
    """The actor's role is not entitled to dispose at this risk tier."""


class FourEyesViolation(ApprovalRefused):
    """The approver is the requester, on a workflow where that is not allowed."""


def _require_aware(value: datetime, name: str) -> datetime:
    """Refuse naive datetimes at construction rather than at comparison.

    A naive `expires_at` compared against an aware `now` raises TypeError deep
    inside the expiry guard — which fails closed, but fails with a traceback
    about offset-naive comparison rather than about a stale proposal, at the
    moment somebody is trying to approve something. Worse, if both sides happen
    to be naive the comparison silently succeeds against whatever the server's
    local zone is, and an expiry that is wrong by one timezone offset is an
    expiry that lets a stale proposal through for an hour.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{name} must be timezone-aware; a naive timestamp on an audit "
            f"record is only interpretable if you know which host wrote it"
        )
    return value


# ── The two types ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Actor:
    """The authenticated human, resolved SERVER-SIDE.

    Frozen, and deliberately holds no display name. Two reasons: a name is not a
    stable identifier (people change them, and a record keyed on one stops
    resolving), and keeping it out of the gate's input means it cannot end up on
    the wrong side of the projection by accident. Resolve names at read time,
    from the directory, for the human reading the record.

    Construct this from the validated token's subject claim, never from a request
    body. A client-supplied actor ID is a claim, not a fact — the same argument
    examples/tenant_isolation.py makes about session IDs, and the same
    consequence: a caller who can name the actor can attribute their own
    disposition to somebody else.
    """

    actor_id: str
    role: str

    def __post_init__(self) -> None:
        # `is_uuid` from the gate, not a local regex, so "what counts as an
        # identifier" has one definition in this codebase. It also enforces the
        # canonical form, which matters because the value is compared for
        # equality in the four-eyes guard: an uppercase UUID and its lowercase
        # twin are the same person and different strings.
        if not is_uuid(self.actor_id):
            raise ValueError(
                f"actor_id must be a canonical UUID from the identity "
                f"provider's subject claim, got {self.actor_id!r}"
            )
        if self.role not in ACTOR_ROLES:
            raise ValueError(
                f"unknown role {self.role!r}; known roles are "
                f"{sorted(ACTOR_ROLES)}. An unknown role must fail closed — a "
                f"role nobody has entitled cannot be entitled by default."
            )


@dataclass(frozen=True)
class Proposal:
    """What the model produced. Immutable, and never a disposition.

    Frozen because a proposal that can be edited in place is a proposal whose
    approved contents cannot be established afterwards: the reviewer approved
    what they read, and if the object mutated between reading and approving,
    `evidence_hash` is the only thing that would have caught it. Freezing the
    type removes the case where nothing catches it.
    """

    proposal_id: str
    tenant_id: str
    run_id: str
    workflow: str
    risk_tier: str
    # The model's draft, verbatim. Narrative and PII-bearing by construction.
    model_output: Mapping[str, Any]
    # HMAC over the canonical evidence set AS DRAFTED, under the tenant key.
    # Not a plain digest: an unkeyed hash of a small evidence set is a lookup
    # oracle for its contents.
    evidence_hash: str
    evidence_hash_alg: str
    expires_at: datetime
    schema_version: str
    prompt_version: str
    model_id: str
    # The actor who requested the draft. The four-eyes comparison needs it, and
    # it must be recorded at request time — reconstructing "who asked for this"
    # afterwards from a queue log is exactly the attribution that does not
    # survive an examination.
    requested_by: str
    created_at: datetime
    alert_id: str | None = None
    customer_id: str | None = None
    case_id: str | None = None

    def __post_init__(self) -> None:
        if self.workflow not in WORKFLOW_NAMES:
            raise ValueError(f"unknown workflow {self.workflow!r}")
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(f"unknown risk tier {self.risk_tier!r}")
        if not is_uuid(self.requested_by):
            raise ValueError("requested_by must be a canonical actor UUID")
        _require_aware(self.expires_at, "expires_at")
        _require_aware(self.created_at, "created_at")

    # There is deliberately NO to_decision(), no approve(), no
    # with_status("APPROVED"). If you find yourself adding one, the thing you
    # want is a Decision authored by an Actor, and the reason it feels like more
    # work is that authoring a decision IS more work than flipping a field —
    # which is the property the design is buying.


@dataclass(frozen=True)
class Decision:
    """What a named human authored. The decision of record.

    `agreed` is a statement about the PROPOSAL, not a platform disposition. The
    difference matters and is the subject of one of the anti-patterns below: this
    type records that a person read a draft and agreed or disagreed with it. It
    does not carry an instruction like "CLOSE" or "FILE" for the gate to execute.
    The platform mutation is a separate, separately-audited write triggered by
    this record's existence.
    """

    proposal_id: str
    actor_id: str
    actor_role: str
    decided_at: datetime
    agreed: bool
    # The caller's de-duplication token for THIS submission attempt — a request
    # ID, not a fresh UUID per click. See `decision_id`.
    idempotency_key: str
    disagreement_rationale: str | None = None

    def __post_init__(self) -> None:
        if not is_uuid(self.actor_id):
            raise ValueError("actor_id must be a canonical UUID")
        if self.actor_role not in ACTOR_ROLES:
            raise ValueError(f"unknown role {self.actor_role!r}")
        if not isinstance(self.agreed, bool):
            # Strict bool for the same reason output_validation.py insists on
            # one: a model or a form that sends the string "false" has not
            # answered the question, and a truthy "false" is an approval nobody
            # gave.
            raise ValueError(f"agreed must be a bool, got {type(self.agreed).__name__}")
        if _IDEMPOTENCY_KEY_RE.fullmatch(self.idempotency_key) is None:
            raise ValueError(
                f"idempotency_key must be 16-128 identifier characters, got "
                f"{self.idempotency_key!r}. Short keys collide across "
                f"submissions, which turns idempotency into data loss."
            )
        _require_aware(self.decided_at, "decided_at")
        if not self.agreed and not (self.disagreement_rationale or "").strip():
            # control-stack.md requires a disagreement rationale when the human
            # overrides. It is the highest-value field in the record for quality
            # purposes — the delta between draft and disposition is the signal
            # that tells you whether the model is useful — and a disagreement
            # recorded without one is a data point that can never be analysed.
            raise ValueError(
                "a disagreement needs a rationale; without one the override is "
                "unanalysable as a quality signal and unexplainable to an "
                "examiner"
            )
        if len(self.disagreement_rationale or "") > _MAX_RATIONALE_CHARS:
            raise ValueError("disagreement_rationale exceeds the stored maximum")

    @property
    def decision_id(self) -> str:
        """Derived, never supplied. This is what makes the conditional write work.

        The conditional write's condition is `attribute_not_exists(decision_id)`,
        so the key has to be a deterministic function of the submission. A caller
        that generates a fresh UUID per click would collide with nothing, the
        condition would always hold, and a double-submit would write two
        dispositions while every line of the idempotency machinery ran exactly as
        designed. Deriving it here removes that possibility from the API rather
        than documenting against it.
        """
        return derive_decision_id(self.proposal_id, self.idempotency_key)


def derive_decision_id(proposal_id: str, idempotency_key: str) -> str:
    """UUIDv5 over (proposal, idempotency key). Same submission, same key."""
    return str(uuid.uuid5(_DECISION_ID_NAMESPACE,
                          f"{proposal_id}|{idempotency_key}"))


# ── The store seam ───────────────────────────────────────────────────────────

class DecisionStore(Protocol):
    """Where dispositions live. Injected so `approve` is testable.

    Note what this store is NOT: the archival record. An IAM deny on
    `UpdateItem`/`DeleteItem` approximates append-only, and the gap matters —
    there are no triggers, so the control lives entirely in policy that a later
    change can re-grant, and its compensating control is CloudTrail **data**
    events on item-level operations, which are **off by default**. An unenabled
    compensating control is no control. So this table is the idempotency and
    concurrency mechanism; the immutable examinable copy of the decision goes
    through `examples/audit_record.py` into S3 Object Lock, and the row that
    points at it into a table with a `BEFORE UPDATE OR DELETE` trigger.
    """

    def get(self, decision_id: str) -> Mapping[str, Any] | None: ...

    def put_if_absent(self, item: Mapping[str, Any]) -> bool:
        """True if written, False if an item with that decision_id existed."""
        ...


class DynamoDecisionStore:
    """The real shape, with the real condition expression.

    **Its own client, not the agent's.** The agent's client is constructed with
    the write tools filtered out of the tool list — that filtering is the
    read-only control (`control-stack.md` Layer 1), and it applies when tools are
    listed for the model, not to direct invocation. Which is exactly what makes
    the split work: the deterministic layer performs the write through an
    unfiltered client of its own, and the model never sees the tool. Reuse the
    agent's client here and you have handed the write path back to the component
    the filter existed to constrain.
    """

    def __init__(self, table_name: str, *, dynamodb_resource: Any = None):
        if dynamodb_resource is None:
            if boto3 is None:  # pragma: no cover
                raise RuntimeError(
                    "boto3 is not installed; pass dynamodb_resource explicitly"
                )
            dynamodb_resource = boto3.resource("dynamodb")
        self._table = dynamodb_resource.Table(table_name)

    def get(self, decision_id: str) -> Mapping[str, Any] | None:
        # ConsistentRead, because the read here is a de-duplication check. An
        # eventually-consistent read is exactly the read that misses the first
        # submission of a double-submit two hundred milliseconds ago, which is
        # the case this whole mechanism exists for.
        response = self._table.get_item(
            Key={"decision_id": decision_id}, ConsistentRead=True
        )
        return response.get("Item")

    def put_if_absent(self, item: Mapping[str, Any]) -> bool:
        try:
            self._table.put_item(
                Item=dict(item),
                ConditionExpression="attribute_not_exists(decision_id)",
            )
            return True
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            # Matched on the error CODE rather than on the exception class,
            # because the class is generated at runtime by botocore and
            # importing it couples this module to the SDK it is meant to be
            # seam-isolated from. Anything else re-raises: a throttle or a
            # credential failure is NOT "already decided", and swallowing it
            # here would turn a failed write into a reported success.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return False
            raise


@dataclass
class ApprovalOutcome:
    """The disposition, and whether this call is the one that created it."""

    decision_id: str
    item: dict[str, Any]
    # True when this call found an existing decision — a replayed submit or the
    # loser of a concurrent race. The caller renders the same screen either way;
    # what it must not do is treat it as a new disposition.
    replayed: bool


# ── The gate ─────────────────────────────────────────────────────────────────

def approve(
    proposal: Proposal,
    decision: Decision,
    *,
    actor: Actor,
    evidence: Mapping[str, Any],
    tenant_hmac_key: bytes,
    store: DecisionStore,
    entitlements: Mapping[str, frozenset[str]] | None = None,
    four_eyes_workflows: frozenset[str] = FOUR_EYES_WORKFLOWS,
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Record a human decision, or refuse and record nothing.

    The order below is load-bearing and each step carries the failure mode it
    closes. Note that `approve` never takes the disposition as an argument: it
    takes a `Decision` a human authored, and its own job is to refuse or to
    persist. See the anti-patterns for why that distinction is not pedantry.
    """
    entitlements = entitlements if entitlements is not None else ENTITLEMENTS
    moment = now or datetime.now(timezone.utc)
    decision_id = decision.decision_id

    if decision.proposal_id != proposal.proposal_id:
        raise ApprovalRefused(
            f"decision names proposal {decision.proposal_id} but was submitted "
            f"against {proposal.proposal_id}"
        )

    # ── 1. Idempotency, FIRST ────────────────────────────────────────────────
    #
    # Failure mode: a double-submit becoming a second disposition. Two clicks, a
    # retried POST, a client firing on keyup and click both.
    #
    # And the reason this is first rather than after the guards, which is the
    # part that is easy to get backwards: a retry of an ALREADY-COMPLETED
    # approval can arrive after `expires_at`, or after the evidence has legitimately
    # moved on, or after the approver's role changed. Guards-first would refuse
    # it — turning a successful, already-recorded approval into an error the
    # user is shown and may try to "fix". The decision that exists is the
    # decision of record; a replay returns it.
    existing = store.get(decision_id)
    if existing is not None:
        return ApprovalOutcome(decision_id=decision_id, item=dict(existing),
                               replayed=True)

    # ── 2. Re-derive the evidence hash ───────────────────────────────────────
    #
    # Failure mode: approving against evidence that changed after drafting. The
    # reviewer read a draft grounded in one evidence set. A late transaction, a
    # re-scored screening hit, a corrected counterparty — any of these changes
    # the set, and approving the old draft against the new evidence is approving
    # something nobody read. Re-derived from the evidence as it is NOW, not
    # trusted from the request: a hash the caller sends is a hash the caller
    # chose.
    recomputed = content_hash(evidence, key=tenant_hmac_key)
    if proposal.evidence_hash_alg != HASH_ALGORITHM:
        raise EvidenceChanged(
            f"proposal was hashed under {proposal.evidence_hash_alg!r}, this "
            f"process computes {HASH_ALGORITHM!r} — the schemes must match "
            f"before the values can be compared"
        )
    if not _constant_time_equal(recomputed, proposal.evidence_hash):
        raise EvidenceChanged(
            f"evidence for proposal {proposal.proposal_id} no longer matches "
            f"the set it was drafted against. Re-draft and have it re-read; do "
            f"not approve the old draft."
        )

    # ── 3. Expiry ────────────────────────────────────────────────────────────
    #
    # Failure mode: approving a stale draft. A proposal that sat in a queue for a
    # week describes a customer state that has moved on. Note the direction — an
    # expired proposal is REFUSED, never auto-approved and never auto-closed.
    # See the anti-patterns on timeout auto-approval.
    if moment > _require_aware(proposal.expires_at, "expires_at"):
        raise ProposalExpired(
            f"proposal {proposal.proposal_id} expired at "
            f"{proposal.expires_at.isoformat()}; it is now "
            f"{moment.isoformat()}. Re-draft against current evidence."
        )

    # ── 4. Entitlement ───────────────────────────────────────────────────────
    #
    # Failure mode: an unentitled actor disposing. Two checks, because the second
    # is worthless without the first: the decision must claim the authenticated
    # actor, and that actor's role must be entitled at this proposal's risk tier.
    # A gate that checks the role named IN the decision is checking a string the
    # submitter chose.
    if decision.actor_id != actor.actor_id or decision.actor_role != actor.role:
        raise ActorIdentityMismatch(
            f"decision claims actor {decision.actor_id}/{decision.actor_role} "
            f"but the authenticated actor is {actor.actor_id}/{actor.role}"
        )
    entitled = entitlements.get(proposal.risk_tier, frozenset())
    if actor.role not in entitled:
        raise ActorNotEntitled(
            f"role {actor.role!r} is not entitled to dispose at risk tier "
            f"{proposal.risk_tier!r} (entitled: {sorted(entitled)})"
        )

    # ── 5. Four eyes ─────────────────────────────────────────────────────────
    #
    # Failure mode: the requester approving their own request. Compared on the
    # actor UUID, not on a name or an email, so an actor with two accounts is a
    # gap you close in the directory rather than one this check pretends to
    # cover.
    if (proposal.workflow in four_eyes_workflows
            and actor.actor_id == proposal.requested_by):
        raise FourEyesViolation(
            f"{proposal.workflow} requires four eyes: actor {actor.actor_id} "
            f"requested this draft and cannot also dispose of it"
        )

    # ── 6. Only now, the write — through this store's own client ─────────────
    #
    # Failure mode: the agent's filtered client performing the write. See
    # DynamoDecisionStore.
    #
    # The conditional write closes the race the read in step 1 cannot: two
    # concurrent first submissions both find nothing, both pass every guard, and
    # the condition picks one winner. The loser reads the winner's decision and
    # returns it, which is the same answer a replay gets — one disposition, two
    # callers told the truth.
    item = {
        "decision_id": decision_id,
        "proposal_id": proposal.proposal_id,
        "tenant_id": proposal.tenant_id,
        "run_id": proposal.run_id,
        "workflow": proposal.workflow,
        "risk_tier": proposal.risk_tier,
        "actor_id": decision.actor_id,
        "actor_role": decision.actor_role,
        "decided_at": decision.decided_at.isoformat(),
        "agreed": decision.agreed,
        "disagreement_rationale": decision.disagreement_rationale,
        "idempotency_key": decision.idempotency_key,
        # Pinned onto the decision so "what was this approved against" is
        # answerable from the decision alone, without re-reading the proposal
        # record and trusting that it did not move.
        "evidence_hash": proposal.evidence_hash,
        "evidence_hash_alg": proposal.evidence_hash_alg,
        "schema_version": proposal.schema_version,
        "prompt_version": proposal.prompt_version,
        "model_id": proposal.model_id,
        "requested_by": proposal.requested_by,
    }
    if store.put_if_absent(item):
        return ApprovalOutcome(decision_id=decision_id, item=item,
                               replayed=False)

    raced = store.get(decision_id)
    if raced is None:  # pragma: no cover - a store that lies about its own state
        raise ApprovalRefused(
            f"conditional write for {decision_id} failed but no decision is "
            f"readable; refusing rather than retrying, because a store that "
            f"reports both is not one to write a disposition into"
        )
    return ApprovalOutcome(decision_id=decision_id, item=dict(raced),
                           replayed=True)


def _constant_time_equal(a: str, b: str) -> bool:
    """`hmac.compare_digest` semantics, imported through the gate's habit.

    A plain `==` on a MAC returns early at the first differing byte and leaks how
    much of a forgery was correct, which is enough to construct one a byte at a
    time. The habit matters more than this call site does.
    """
    import hmac as _hmac
    return _hmac.compare_digest(a, b)


# ── Emitting the decision through the gate ───────────────────────────────────
#
# Nothing reaches an AWS sink except through `emit_metering`, decision records
# included. What the split does to a decision is worth reading closely, because
# it is not what you would guess:
#
# **The actor UUID and the decision timestamp DIVERT to the internal record.**
# `log_projection.METERING_ALLOWLIST` has no entry for `actor_id`, `actor_role`,
# `decided_at` or `agreed`, so all four are diverted — which is the allowlist
# failing in the direction it was designed to fail. Two things follow.
#
# First, this is arguably the correct side of the split rather than a limitation.
# An actor UUID in an AWS-side log is pseudonymous EMPLOYEE data: a per-analyst
# decision rate, an agree/disagree ratio, a timestamp series showing when
# somebody works. That is workforce monitoring, with its own lawful basis, its
# own works-council questions in some jurisdictions and its own retention
# argument — and it lands in a store the firm shares with a cloud provider.
# `control-stack.md`'s audit table puts "Human decision, actor, timestamp" in the
# record, and the record is the tenant-encrypted bundle.
#
# Second, if you do want the four fields in metering — and there is a real case,
# because "how many high-tier alerts were disposed today" is an operational
# metric — then the correct change is a deliberate addition to the allowlist
# itself, in `log_projection.py`, alongside a predicate each:
#
#     "actor_id":   is_uuid,
#     "actor_role": is_enum(ACTOR_ROLES),
#     "agreed":     is_bool,
#     "decided_at": is_iso8601,        # a new predicate; see below
#
# Two mechanics to know before making that edit. `decided_at` needs a predicate
# that DECLARES its pattern, because the sweep reads an ISO-8601 timestamp's
# leading `2026-08-15` as a phone-shaped digit run and blocks the whole event
# unless the shape is declared; and an epoch-seconds integer is worse, since it
# exceeds `MAX_METERING_INT` and blocks as a suspected account number. Also note
# what NOT to do, which is the anti-pattern below: widen the allowlist from a
# consumer module at import time. A gate any importer can widen is not a gate,
# and `SAFE_VOCABULARY` is derived at import anyway, so a late registration
# passes the allowlist and then fails the sweep on its own key name.

def project_decision(
    proposal: Proposal,
    decision: Decision,
    *,
    hmac_key: bytes,
    profile: Profile = Profile.PROD,
) -> Projection:
    """Split the decision into its AWS-safe half and its examinable half.

    The metering half carries the identifiers, the versions and the draft's enum
    fields — enough to answer "which workflow, which model, which tier, how
    many". The examinable half carries who decided, when, whether they agreed and
    why they did not. The pairing HMAC `split` adds is what stops one run's
    metering row being matched to another run's bundle.
    """
    draft = proposal.model_output
    record: dict[str, Any] = {
        "workflow": proposal.workflow,
        "tenant_id": proposal.tenant_id,
        "run_id": proposal.run_id,
        "schema_version": proposal.schema_version,
        "prompt_version": proposal.prompt_version,
        "model_id": proposal.model_id,
        # Diverted, every one of them — see the note above.
        "decision_id": decision.decision_id,
        "proposal_id": proposal.proposal_id,
        "risk_tier": proposal.risk_tier,
        "actor_id": decision.actor_id,
        "actor_role": decision.actor_role,
        "decided_at": decision.decided_at.isoformat(),
        "agreed": decision.agreed,
        "disagreement_rationale": decision.disagreement_rationale,
        "requested_by": proposal.requested_by,
        "four_eyes_required": proposal.workflow in FOUR_EYES_WORKFLOWS,
        "evidence_hash": proposal.evidence_hash,
        "evidence_hash_alg": proposal.evidence_hash_alg,
        "proposal_created_at": proposal.created_at.isoformat(),
        "expires_at": proposal.expires_at.isoformat(),
    }
    if proposal.alert_id is not None:
        record["alert_id"] = proposal.alert_id
    if proposal.customer_id is not None:
        record["customer_id"] = proposal.customer_id
    if proposal.case_id is not None:
        record["case_id"] = proposal.case_id

    # The draft's own fields, so the metering row can carry what was PROPOSED
    # even though it cannot carry whether the human agreed. That asymmetry is
    # not an accident of this code — it is the four-line allowlist gap named
    # above, visible in the data.
    for name in ("recommendation", "risk_score", "confidence",
                 "primary_typology", "escalation_recommended",
                 "account_takeover_suspected", "customer_may_be_victim"):
        if name in draft:
            record[name] = draft[name]
    for name in ("rationale", "red_flags", "mitigating_factors", "gaps",
                 "recommended_actions", "additional_typologies"):
        if name in draft:
            record[name] = draft[name]

    return split(
        record,
        tenant_id=proposal.tenant_id,
        run_id=proposal.run_id,
        hmac_key=hmac_key,
        # The disagreement rationale is the reviewer's own narrative about a
        # named customer. It goes where the model's narrative goes: the
        # tenant-encrypted bundle.
        reasoning_trace=decision.disagreement_rationale,
        profile=profile,
    )


# ── Workflow graph validation ────────────────────────────────────────────────
#
# `control-stack.md` Layer 1: "validation must reject any graph where an AI node
# reaches a decisioning node without an intervening mandatory human-review node.
# Enforce in graph validation, not convention."
#
# The reason it has to be validation rather than convention is not that authors
# are careless. It is that the property is about PATHS, and a path is not visible
# in the diff that creates it. Someone adds one edge — a retry route, a fast
# path for low-risk cases, an error handler that routes to the closer — and the
# edge is locally reasonable while the graph now contains a route from a model to
# a disposition. Nobody reviewing that one edge is looking at the whole graph.
# A reachability check is.

AI_KINDS = frozenset({"AI", "AI_AGENT", "LLM"})
HUMAN_KINDS = frozenset({"HUMAN_REVIEW"})
DECISION_KINDS = frozenset({"DECISION", "DISPOSITION", "FILING"})
PASSTHROUGH_KINDS = frozenset({"START", "END", "TOOL", "TRANSFORM", "ROUTER",
                               "NOTIFY", "RETRIEVAL"})
KNOWN_KINDS = AI_KINDS | HUMAN_KINDS | DECISION_KINDS | PASSTHROUGH_KINDS


def validate_workflow_graph(graph: Mapping[str, Any]) -> list[str]:
    """Return the violations. Empty list means the graph is acceptable.

    Expected shape, so a graph loaded from YAML or JSON validates directly:

        {"nodes": {"triage": {"kind": "AI"},
                   "review": {"kind": "HUMAN_REVIEW", "mandatory": True},
                   "close":  {"kind": "DECISION"}},
         "edges": {"triage": ["review"], "review": ["close"]}}

    A human-review node only blocks if it is `mandatory`. An optional review step
    is precisely the convention this check exists to replace — it is a node the
    graph can route around, so it is not a gate, and a violation through one says
    so explicitly rather than reporting a bare unreachability.
    """
    violations: list[str] = []
    nodes = graph.get("nodes")
    edges = graph.get("edges", {})
    if not isinstance(nodes, Mapping) or not isinstance(edges, Mapping):
        return ["graph must have a 'nodes' mapping and an 'edges' mapping"]

    def kind_of(node_id: str) -> str:
        spec = nodes.get(node_id) or {}
        return str(spec.get("kind", "")) if isinstance(spec, Mapping) else ""

    # Structural checks first. A graph the validator cannot fully read cannot be
    # cleared, so an unknown node kind is a violation rather than a shrug: the
    # question this function answers is "can a model reach a disposition", and a
    # node of unknown kind might be one.
    for node_id in nodes:
        kind = kind_of(node_id)
        if kind not in KNOWN_KINDS:
            violations.append(
                f"node {node_id!r} has unknown kind {kind!r}; it cannot be "
                f"shown not to be a decisioning node. Known kinds: "
                f"{sorted(KNOWN_KINDS)}"
            )
    for source, targets in edges.items():
        if source not in nodes:
            violations.append(f"edge from unknown node {source!r}")
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Iterable):
            violations.append(f"edges[{source!r}] must be a list of node ids")
            continue
        for target in targets:
            if target not in nodes:
                violations.append(
                    f"edge {source!r} -> {target!r} names an unknown node"
                )

    def successors(node_id: str) -> Sequence[str]:
        targets = edges.get(node_id, ())
        if isinstance(targets, (str, bytes)) or not isinstance(targets, Iterable):
            return ()
        return [t for t in targets if t in nodes]

    # Reachability, per AI node. A mandatory human-review node blocks traversal:
    # paths that go through it are exactly the paths that are fine, so we stop
    # expanding there rather than reporting them.
    for start in sorted(n for n in nodes if kind_of(n) in AI_KINDS):
        # Depth-first with an explicit path, so the violation names the route.
        # `seen` makes a cyclic graph terminate — and graphs with retry loops are
        # normal, so this is the common case rather than a defensive extra.
        stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
        seen: set[str] = set()
        while stack:
            current, path = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for nxt in successors(current):
                kind = kind_of(nxt)
                if kind in DECISION_KINDS:
                    optional_reviews = [
                        p for p in path
                        if kind_of(p) in HUMAN_KINDS
                    ]
                    detail = ""
                    if optional_reviews:
                        detail = (
                            f" The path passes review node(s) "
                            f"{optional_reviews}, but none is mandatory, so the "
                            f"graph can route around them."
                        )
                    violations.append(
                        f"AI node {start!r} reaches decisioning node {nxt!r} "
                        f"with no intervening mandatory human review: "
                        f"{' -> '.join((*path, nxt))}.{detail}"
                    )
                    continue
                if kind in HUMAN_KINDS and _is_mandatory(nodes.get(nxt)):
                    # Blocked here, correctly. Do not expand past it.
                    continue
                stack.append((nxt, (*path, nxt)))

    return violations


def _is_mandatory(spec: Any) -> bool:
    # Absent means NOT mandatory. Defaulting the other way would make a
    # forgotten flag look like a gate, which is the failure this validator is
    # for — and a human-review node whose mandatory flag nobody set is exactly
    # the node somebody meant to make optional.
    return bool(spec.get("mandatory")) if isinstance(spec, Mapping) else False


def validate_workflow_graph_or_raise(graph: Mapping[str, Any]) -> None:
    """Deploy-time gate. Call it from the pipeline, not from a review checklist."""
    violations = validate_workflow_graph(graph)
    if violations:
        raise ValueError(
            f"{len(violations)} workflow graph violation(s):\n  "
            + "\n  ".join(violations)
        )


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# 1. **Timeout auto-approval.** "If nobody reviews within 48 hours, approve." It
#    is presented as an SLA mechanism and it is a disposition mechanism: it closes
#    alerts on the basis that nobody looked at them, and it closes the most of
#    them exactly when the team is most overloaded — which is when review quality
#    matters most. There is no version of this that survives being described out
#    loud to an examiner. A timeout may escalate, re-queue, page a supervisor or
#    expire the proposal. It may not decide. Note the direction `approve` takes on
#    expiry: an expired proposal is REFUSED.
#
# 2. **Confidence as the gate.** `if output["confidence"] == "high": auto_close()`.
#    Model confidence is a self-report, it is not calibrated against outcomes, and
#    it is highest on the fluent cases — which include the fluently wrong ones.
#    Worse, it inverts the control: the component being supervised chooses whether
#    it is supervised. Confidence belongs in the record as an asserted value and in
#    routing as a reason to add review (`output_validation.route` sends low
#    confidence to a human), never as a reason to remove it.
#
# 3. **A shared `status` column.** One `alerts` row with
#    `status IN ('OPEN','AI_REVIEWED','CLOSED')` and a `closed_by` column that is
#    sometimes an agent. Now every path that can write `status` can dispose of an
#    alert: a backfill, a retry handler, an admin screen, a migration, the agent's
#    own tool if a filter is ever mis-set. And the audit answer to "who closed
#    this" is a mutation history rather than a record. Two types, two rows, two
#    authors.
#
# 4. **`approve()` taking the disposition as an argument.**
#    `approve(alert_id, disposition="CLOSED_NO_ACTION")` makes the caller the
#    decider and reduces the gate to a logger — whatever the caller passes is what
#    happens, and the caller may be a batch job. Note the distinction from
#    `Decision.agreed`, which looks similar and is not: `agreed` is a named
#    human's statement about a specific draft they read, captured under their own
#    identity, and it does not name a platform action. The mutation is a separate
#    write triggered by the decision's existence, so there is no argument to this
#    function that means "close the alert".
#
# 5. **The agent's own client performing the write.** The read-only property is
#    implemented by filtering write tools out of the list the model is offered.
#    Reuse that client for the approval write and the constraint is gone — and the
#    reuse is tempting because the client is already constructed and already has a
#    session. Worse, it has been observed that a model instructed not to call a
#    write tool called it anyway, and because the sanctioned path was broken at the
#    time, that off-script call was the only thing writing records at all. The
#    deterministic layer gets its own unfiltered client; the model gets a list
#    without the tool on it.
#
# 6. **Approving without re-checking the evidence hash.** The most invisible one
#    on this list, because everything works: the draft is there, the reviewer
#    reads it, the approval records cleanly, and the evidence it was grounded in
#    changed an hour ago. Nothing errors, nothing alarms, and the record asserts
#    that a human approved an assessment of an evidence set that no longer exists.
#
# 7. **Widening the projection gate from a consumer module.** The temptation this
#    file specifically had: `log_projection.METERING_ALLOWLIST["actor_id"] =
#    is_uuid` at import time, so the decision's actor could reach metering
#    without editing the gate. Do not. An allowlist any importer can extend is
#    not an allowlist, the widening happens as an import side effect nobody
#    reviews, and it would not even work — `SAFE_VOCABULARY` is derived at import,
#    so the field would clear the allowlist and then be blocked by the sweep on
#    its own key name. A legitimate new field is a deliberate line added to the
#    allowlist, in the gate, in a diff.
#
# 8. **AgentCore Identity presented as the approval record.** It proves which
#    user's token was exchanged for the credentials a call ran under. A token
#    exchange happens because a session existed — not because a qualified person
#    read a draft and formed a judgement. Identity tells you whose credentials;
#    only this record tells you whose decision.


# ── Verification ─────────────────────────────────────────────────────────────
#
# The cases below run with no AWS account and no test framework:
# `python3 human_approval_gate.py`.
#
# What still needs a real deployment, because a fake proves nothing about it:
#
#   * That the conditional write actually rejects the second writer. Run two
#     concurrent submissions against a real table with the same idempotency key
#     and assert exactly one item and one `ConditionalCheckFailedException`.
#   * That the approval path's client is genuinely a different client from the
#     agent's. Assert the negative in both directions: the agent's client must be
#     REFUSED the write, and the gate's client must succeed
#     (`examples/cedar_policies.md` §8 on asserting the specific denial
#     signature rather than "the call failed").
#   * That `validate_workflow_graph_or_raise` runs in the deploy pipeline. A
#     validator nobody calls is a convention with extra steps — grep the pipeline
#     for it, and add a graph that must fail as a pipeline test.

if __name__ == "__main__":  # pragma: no cover
    import json
    from datetime import timedelta

    KEY = b"self-check-tenant-hmac-key-not-for-production"
    TENANT = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    RUN = "c1a2b3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    MLRO = "a1b2c3d4-e5f6-4718-8a9b-0c1d2e3f4a5b"
    L1 = "b2c3d4e5-f607-4829-9bac-1d2e3f4a5b6c"
    L2 = "c3d4e5f6-0718-493a-8bcd-2e3f4a5b6c7d"
    NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

    EVIDENCE = {
        "txn-3": {"amount": "9800", "counterparty": "Northgate Logistics",
                  "booked_at": "2026-03-04"},
        "txn-1": {"amount": "9700", "counterparty": "Northgate Logistics",
                  "booked_at": "2026-03-02"},
    }

    class FakeStore:
        """In-memory, with the conditional-write semantics that matter."""

        def __init__(self) -> None:
            self.items: dict[str, dict[str, Any]] = {}
            self.reads = 0
            self.condition_failures = 0

        def get(self, decision_id):
            self.reads += 1
            return self.items.get(decision_id)

        def put_if_absent(self, item):
            key = item["decision_id"]
            if key in self.items:
                self.condition_failures += 1
                return False
            self.items[key] = dict(item)
            return True

    def make_proposal(**over: Any) -> Proposal:
        base: dict[str, Any] = dict(
            proposal_id="7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e",
            tenant_id=TENANT, run_id=RUN, workflow="sar_draft",
            risk_tier="high",
            model_output={
                "recommendation": "REJECT", "risk_score": 84,
                "confidence": "high", "primary_typology": "STRUCTURING",
                "escalation_recommended": True,
                "account_takeover_suspected": False,
                "customer_may_be_victim": False,
                "additional_typologies": ["MULE_ACTIVITY"],
                "rationale": ("Maria Gonzalez received EUR 87,400 across five "
                              "credits from Northgate Logistics in Rotterdam."),
                "red_flags": [{"statement": "Five credits under the threshold",
                               "kind": "OBSERVATION", "evidence_id": "txn-3"}],
                "mitigating_factors": [],
                "gaps": ["No response to the contact attempt of 4 March"],
                "recommended_actions": ["File within 30 days"],
            },
            evidence_hash=content_hash(EVIDENCE, key=KEY),
            evidence_hash_alg=HASH_ALGORITHM,
            expires_at=NOW + timedelta(hours=24),
            schema_version="alert-triage-v1", prompt_version="sar-draft-v3",
            model_id="eu.amazon.nova-pro-v1",
            requested_by=L2,
            created_at=NOW - timedelta(hours=1),
            alert_id="b3d4f8a1-0c2e-4b6d-9f1a-7e5c2d8a4b60",
            customer_id="9c858901-8a57-4791-81fe-4c455b099bc9",
        )
        base.update(over)
        return Proposal(**base)

    def make_decision(**over: Any) -> Decision:
        base: dict[str, Any] = dict(
            proposal_id="7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e",
            actor_id=MLRO, actor_role="mlro", decided_at=NOW, agreed=True,
            idempotency_key="submit-2026-08-15-req-0001",
        )
        base.update(over)
        return Decision(**base)

    MLRO_ACTOR = Actor(actor_id=MLRO, role="mlro")

    def call(store, proposal=None, decision=None, actor=None,
             evidence=None, now=NOW):
        return approve(
            proposal or make_proposal(), decision or make_decision(),
            actor=actor or MLRO_ACTOR, evidence=evidence or EVIDENCE,
            tenant_hmac_key=KEY, store=store, now=now,
        )

    passed = failed = 0

    def check(label: str, cond: bool) -> None:
        global passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")

    def refuses(label: str, expected: type[BaseException], **kw) -> None:
        global failed
        store = FakeStore()
        try:
            call(store, **kw)
            check(label, False)
        except expected:
            # Refused AND nothing written. A gate that raises after writing is
            # not a gate; it is a disposition with an error message.
            check(label, store.items == {})
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {label} (wrong exception {type(exc).__name__}: {exc})")
            failed += 1

    def _refused(thunk) -> bool:
        """True if constructing the value raised. Used on the type guards."""
        try:
            thunk()
        except (ValueError, TypeError):
            return True
        return False

    # 1 — there is no function that turns a Proposal into a Decision.
    check("1 no converting function on Proposal",
          not any(hasattr(Proposal, n) for n in
                  ("to_decision", "approve", "promote", "with_status", "dispose")))
    check("1 Proposal and Decision are both immutable",
          Proposal.__dataclass_params__.frozen
          and Decision.__dataclass_params__.frozen)
    check("1 neither type carries a status field",
          "status" not in Proposal.__dataclass_fields__
          and "status" not in Decision.__dataclass_fields__)

    # 2 — the happy path.
    store = FakeStore()
    first = call(store)
    check("2 a clean approval is written once",
          not first.replayed and len(store.items) == 1)
    check("2 the item records actor, role, time and agreement",
          store.items[first.decision_id]["actor_id"] == MLRO
          and store.items[first.decision_id]["actor_role"] == "mlro"
          and store.items[first.decision_id]["agreed"] is True
          and store.items[first.decision_id]["decided_at"].startswith("2026-08-15"))
    check("2 and pins the evidence hash it was approved against",
          store.items[first.decision_id]["evidence_hash"]
          == content_hash(EVIDENCE, key=KEY))

    # 3 — double submit is a no-op, not a second disposition.
    second = call(store)
    check("3 double submit is idempotent",
          second.replayed and second.decision_id == first.decision_id
          and len(store.items) == 1)
    check("3 the derived decision_id is what makes that work",
          derive_decision_id("7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e",
                             "submit-2026-08-15-req-0001") == first.decision_id)
    check("3 a different submission gets a different key",
          derive_decision_id("7a1b2c3d-4e5f-4061-8172-839a4b5c6d7e",
                             "submit-2026-08-15-req-0002") != first.decision_id)

    # 3b — a replay AFTER expiry still returns the decision rather than refusing.
    #      This is why the idempotency read is first.
    late = call(store, now=NOW + timedelta(days=30))
    check("3b replay after expiry returns the decision, not a refusal",
          late.replayed and len(store.items) == 1)

    # 3c — the concurrent race the step-1 read CANNOT close: two first
    #      submissions, both find nothing, both pass every guard. The condition
    #      picks one winner; the loser must return the winner's decision rather
    #      than raise or write a second one.
    class RacingStore(FakeStore):
        """The other request commits in the window between our read and write."""

        def get(self, decision_id):
            self.reads += 1
            if self.reads == 1:
                return None                      # our pre-write read: empty
            return self.items.get(decision_id)

        def put_if_absent(self, item):
            if self.reads == 1:                  # the other writer got there first
                self.items[item["decision_id"]] = {
                    **item, "actor_id": L2, "actor_role": "analyst_l2",
                    "note": "written by the concurrent request",
                }
                self.condition_failures += 1
                return False
            return super().put_if_absent(item)

    racing = RacingStore()
    lost = call(racing)
    check("3c the loser of a concurrent race gets the winner's decision",
          lost.replayed and racing.condition_failures == 1
          and len(racing.items) == 1)
    check("3c and does not overwrite it",
          racing.items[lost.decision_id]["actor_id"] == L2)

    # 4 — mutated evidence is refused. This is case 2 of the module docstring.
    mutated = json.loads(json.dumps(EVIDENCE))
    mutated["txn-3"]["amount"] = "9900"      # a corrected amount
    refuses("4 mutated evidence refused", EvidenceChanged, evidence=mutated)
    added = json.loads(json.dumps(EVIDENCE))
    added["txn-9"] = {"amount": "9600", "counterparty": "Northgate Logistics",
                      "booked_at": "2026-03-06"}
    refuses("4 a late-arriving transaction refused too", EvidenceChanged,
            evidence=added)
    refuses("4 a mismatched hash scheme refused before comparison",
            EvidenceChanged,
            proposal=make_proposal(evidence_hash_alg="sha256-plain-v0"))

    # 5 — an expired proposal is refused, never auto-approved.
    refuses("5 expired proposal refused", ProposalExpired,
            now=NOW + timedelta(hours=25))

    # 6 — entitlement, per risk tier.
    refuses("6 under-entitled actor refused", ActorNotEntitled,
            decision=make_decision(actor_id=L1, actor_role="analyst_l1"),
            actor=Actor(actor_id=L1, role="analyst_l1"))
    refuses("6 platform_admin is entitled to nothing", ActorNotEntitled,
            decision=make_decision(actor_id=L1, actor_role="platform_admin"),
            actor=Actor(actor_id=L1, role="platform_admin"))
    tier_store = FakeStore()
    ok_standard = approve(
        make_proposal(risk_tier="standard", workflow="alert_triage"),
        make_decision(actor_id=L1, actor_role="analyst_l1"),
        actor=Actor(actor_id=L1, role="analyst_l1"), evidence=EVIDENCE,
        tenant_hmac_key=KEY, store=tier_store, now=NOW,
    )
    check("6 the same actor IS entitled at the standard tier",
          not ok_standard.replayed)
    refuses("6 a claimed role the token does not carry is refused",
            ActorIdentityMismatch,
            decision=make_decision(actor_id=MLRO, actor_role="mlro"),
            actor=Actor(actor_id=MLRO, role="analyst_l2"))

    # 7 — four eyes: the requester cannot dispose of their own draft.
    refuses("7 same-actor four-eyes refused", FourEyesViolation,
            decision=make_decision(actor_id=L2, actor_role="analyst_l2"),
            actor=Actor(actor_id=L2, role="analyst_l2"),
            proposal=make_proposal(risk_tier="standard"))
    fe_store = FakeStore()
    fe_ok = approve(
        make_proposal(workflow="alert_triage", risk_tier="standard"),
        make_decision(actor_id=L2, actor_role="analyst_l2"),
        actor=Actor(actor_id=L2, role="analyst_l2"), evidence=EVIDENCE,
        tenant_hmac_key=KEY, store=fe_store, now=NOW,
    )
    check("7 and is allowed on a workflow where four eyes does not apply",
          not fe_ok.replayed)

    # 8 — a disagreement needs a rationale; the type refuses to exist without one.
    try:
        make_decision(agreed=False)
        check("8 disagreement without a rationale refused", False)
    except ValueError:
        check("8 disagreement without a rationale refused", True)
    disagreed = make_decision(
        agreed=False,
        disagreement_rationale=("The five credits are salary from a known "
                                "employer; Gonzalez provided payslips on "
                                "6 March."))
    check("8 with a rationale it constructs", disagreed.agreed is False)
    check("8 a non-bool agreement is refused",
          _refused(lambda: make_decision(agreed="false")))  # type: ignore[arg-type]

    # 9 — the projection: actor and timestamp to the internal record, never a
    #     name anywhere, and the metering half still emits.
    proj = project_decision(make_proposal(), disagreed, hmac_key=KEY)
    metering = emit_metering(proj)
    blob = json.dumps(metering, default=str)
    internal_blob = json.dumps(proj.internal, default=str)
    check("9 metering emits through the gate", isinstance(metering, dict))
    check("9 no narrative or name reaches metering",
          not any(t in blob for t in
                  ("Maria", "Gonzalez", "Northgate", "Rotterdam", "payslips")))
    check("9 actor UUID is NOT in metering (no allowlist entry)",
          MLRO not in blob and "actor_id" not in metering)
    check("9 actor UUID and role ARE in the internal record",
          MLRO in internal_blob and "mlro" in internal_blob)
    check("9 decided_at is internal, not metering",
          "decided_at" not in metering
          and "2026-08-15T12:00:00+00:00" in internal_blob)
    check("9 the disagreement rationale is internal only",
          "payslips" in internal_blob and "payslips" not in blob)
    check("9 the draft's enums do project",
          metering.get("recommendation") == "REJECT"
          and metering.get("risk_score") == 84
          and metering.get("primary_typology") == "STRUCTURING")
    check("9 narrative counts project, narrative does not",
          metering.get("red_flags_count") == 1
          and metering.get("gaps_count") == 1
          and "red_flags" not in metering)
    check("9 the pairing hash is present on both halves",
          metering.get("content_hash_alg") == HASH_ALGORITHM
          and log_projection.verify_pairing(proj, key=KEY))
    check("9 diverting the decision fields is not recorded as a defect",
          not proj.defects)

    # 10 — the graph validator.
    GOOD = {
        "nodes": {"start": {"kind": "START"},
                  "enrich": {"kind": "RETRIEVAL"},
                  "triage": {"kind": "AI"},
                  "review": {"kind": "HUMAN_REVIEW", "mandatory": True},
                  "close": {"kind": "DECISION"},
                  "end": {"kind": "END"}},
        "edges": {"start": ["enrich"], "enrich": ["triage"],
                  "triage": ["review"], "review": ["close"], "close": ["end"]},
    }
    check("10 a graph with a mandatory human gate validates",
          validate_workflow_graph(GOOD) == [])

    DIRECT = {
        "nodes": {"triage": {"kind": "AI"}, "close": {"kind": "DECISION"}},
        "edges": {"triage": ["close"]},
    }
    v_direct = validate_workflow_graph(DIRECT)
    check("10 AI feeding a decision node directly fails",
          len(v_direct) == 1 and "no intervening mandatory human review"
          in v_direct[0])
    check("10 and the violation names the path",
          "triage -> close" in v_direct[0])

    OPTIONAL = {
        "nodes": {"triage": {"kind": "AI"},
                  "review": {"kind": "HUMAN_REVIEW"},   # mandatory not set
                  "close": {"kind": "DECISION"}},
        "edges": {"triage": ["review"], "review": ["close"]},
    }
    v_opt = validate_workflow_graph(OPTIONAL)
    check("10 an optional review node does not count as a gate",
          len(v_opt) == 1 and "none is mandatory" in v_opt[0])

    BYPASS = {
        "nodes": {"triage": {"kind": "AI"},
                  "review": {"kind": "HUMAN_REVIEW", "mandatory": True},
                  "fast_path": {"kind": "ROUTER"},
                  "close": {"kind": "DECISION"}},
        # The one locally-reasonable edge that opens a route around the gate.
        "edges": {"triage": ["review", "fast_path"], "review": ["close"],
                  "fast_path": ["close"]},
    }
    v_bypass = validate_workflow_graph(BYPASS)
    check("10 a bypass edge around a mandatory gate fails",
          any("fast_path -> close" in v for v in v_bypass))

    CYCLE = {
        "nodes": {"triage": {"kind": "AI"}, "retry": {"kind": "TOOL"},
                  "review": {"kind": "HUMAN_REVIEW", "mandatory": True},
                  "close": {"kind": "DECISION"}},
        "edges": {"triage": ["retry"], "retry": ["triage", "review"],
                  "review": ["close"]},
    }
    check("10 a retry cycle terminates and validates",
          validate_workflow_graph(CYCLE) == [])

    UNKNOWN = {
        "nodes": {"triage": {"kind": "AI"}, "mystery": {"kind": "AUTO_CLOSER"}},
        "edges": {"triage": ["mystery"]},
    }
    check("10 an unknown node kind fails closed",
          any("unknown kind" in v for v in validate_workflow_graph(UNKNOWN)))
    check("10 a dangling edge is reported",
          any("unknown node" in v for v in validate_workflow_graph(
              {"nodes": {"a": {"kind": "AI"}}, "edges": {"a": ["ghost"]}})))
    try:
        validate_workflow_graph_or_raise(DIRECT)
        check("10 or_raise raises", False)
    except ValueError:
        check("10 or_raise raises", True)

    # 11 — the types refuse malformed identity outright.
    check("11 a non-UUID actor is refused",
          _refused(lambda: Actor(actor_id="mlro@example.com", role="mlro")))
    check("11 an unknown role is refused",
          _refused(lambda: Actor(actor_id=MLRO, role="superuser")))
    check("11 a naive decided_at is refused",
          _refused(lambda: make_decision(
              decided_at=datetime(2026, 8, 15, 12, 0))))
    check("11 a short idempotency key is refused",
          _refused(lambda: make_decision(idempotency_key="abc")))

    print(f"\nhuman_approval_gate self-check: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
