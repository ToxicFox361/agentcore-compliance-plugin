"""The per-invocation decision record, and the storage split that makes it
examinable years later.

`examples/output_validation.py` decides whether an output is well-formed.
`examples/log_projection.py` decides which half of it may reach an AWS-side log.
This file is what happens next: it assembles the record
`references/control-stack.md` specifies ("Audit record: what to persist per
invocation"), writes the bulky examinable half to S3 Object Lock, and writes the
queryable half to a relational row that points at that exact object version.

Six failure modes it exists to prevent:

  1. **A row pointing at an object that does not exist.** Write the row first and
     any failure between the two writes leaves a dangling audit record — a
     structured assertion that evidence exists, referencing a key that was never
     stored. That is a false statement about a control having operated, and it
     will be believed. Bundle first, then row: an object with no row is merely
     orphaned, and orphans are discoverable by listing the bucket.
  2. **An intact protected record reported as gone.** A plain `DELETE` writes a
     delete marker over a WORM-protected version. `HeadObject` then answers 404
     for evidence that is sitting there, intact, and "no record" is a worse
     answer to an examiner than either the truth or a real gap. `verify` lists
     versions.
  3. **A hash presented as the archival record.** Hashing is one-way. A record
     that is only hashed satisfies tamper evidence and fails the requirement it
     exists for, because an examiner cannot be shown a hash. Hash for pairing,
     encrypt for retrieval, always both (`references/audit-trail.md` §12).
  4. **A record that cannot be reconstructed.** No seed is available for Bedrock
     inference, so the stored inference parameters are the *only* reconstruction
     of what ran. A record missing `temperature`, `prompt_version` or the
     resolved model ID is a record whose output cannot be re-derived or
     defended.
  5. **A permanent mistake in a retention date.** `COMPLIANCE` mode retention can
     be extended and never shortened, and no principal including the account
     root can delete the version. A wrong `RetainUntilDate` on first write is
     permanent for its whole term. See `RETENTION` below, which is loud on
     purpose.
  6. **A trace ID reconstructed from timestamps.** `InvokeAgentRuntime` returns
     the trace identifiers, so the caller can pin the trace onto the record at
     invocation time. `InvokeHarness` does not, so on that path the caller must
     supply one. Matching by timestamp produces wrong answers exactly when
     concurrent requests overlap, which is the normal condition for a fan-out.

Composition, stated because duplicating any of it would be the defect: the
projection gate, the canonical serialisation, the HMAC pairing and the
tenant-scoped envelope encryption all come from `log_projection`. This module
adds the storage mechanics and the archival digest-then-MAC that KMS's
`GenerateMac` message cap forces.

Stdlib only, plus a guarded `boto3` seam, so this imports and self-checks with no
AWS credentials present.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

import log_projection
from log_projection import (
    HASH_ALGORITHM,
    Profile,
    Projection,
    emit_metering,
    encrypt_for_tenant,
    split,
)

# `_canonical_json` is private by name and load-bearing by function: it IS the
# frozen record format, and every hash already stored was taken over its exact
# bytes. Re-implementing it here to avoid touching a private name would create
# the second copy of a serialisation rule that `log_projection` itself warns
# about — two lists that must agree, drifting. Importing it is the smaller
# problem, and the right fix is for that module to export it under a public
# name.
from log_projection import _canonical_json as canonical_json

# boto3 is needed only if the caller does not inject clients. Everything in this
# file — including the self-check at the bottom — runs against injected fakes, so
# the module imports with no SDK and no credentials.
try:  # pragma: no cover - import guard, not logic
    import boto3
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]


# ── The record format is versioned, including its digest algorithm ───────────
#
# `GenerateMac` caps `Message` at 4,096 bytes. An evidence bundle carrying a
# reasoning trace and a retrieved evidence set is larger than that, so you MAC a
# *digest* of the bundle rather than the bundle — and AWS states the consequence
# plainly: "If you generate an HMAC for a hash digest of a message, you must
# verify the HMAC of the same hash digest."
#
# That makes the digest algorithm part of the record format rather than an
# implementation detail. Record it beside the prompt and schema versions, or a
# verification in year four fails for a reason nobody can distinguish from
# tampering — and "we think it was SHA-256" is not a reconciliation an examiner
# accepts.

RECORD_FORMAT_VERSION = "decision-record-v1"

# Named for what it digests, not just for the hash function: the bytes are the
# canonical plaintext AS STORED, so `verify` decrypts, digests the bytes it got,
# and needs no canonicaliser at all. A digest defined over "the bundle" instead
# would make verification depend on re-serialising a decoded object identically,
# which is a second place the format can drift.
BUNDLE_DIGEST_ALG = "sha256-of-stored-plaintext-v1"

# A documented `MacAlgorithm` value, so the same label works for a local HMAC and
# for KMS. Valid values are HMAC_SHA_224, HMAC_SHA_256, HMAC_SHA_384,
# HMAC_SHA_512.
BUNDLE_MAC_ALG = "HMAC_SHA_256"

KMS_GENERATE_MAC_MAX_MESSAGE_BYTES = 4096


# ── Object Lock retention: read this before choosing a mode ──────────────────
#
# ############################################################################
# #  COMPLIANCE MODE IS IRREVERSIBLE. Retention can be EXTENDED and never    #
# #  SHORTENED. No principal can delete a protected version — including the  #
# #  account root. AWS documents exactly one escape and it is not one: "the   #
# #  only way to delete an object under the compliance mode before its        #
# #  retention date expires is to delete the associated AWS account."        #
# #                                                                          #
# #  So a wrong `RetainUntilDate` on the FIRST WRITE is permanent for its     #
# #  whole term. A fat-fingered 70 instead of 7 is 70 years of storage you    #
# #  cannot cancel, and — the part that actually hurts — 70 years in which an #
# #  erasure request cannot be honoured for that object by any means except   #
# #  destroying the tenant key, which takes every surrounding record with it. #
# ############################################################################
#
# GOVERNANCE plus a break-glass role is the defensible default for PII-bearing
# evidence: protection ordinary principals cannot remove, and an auditable
# erasure path that exists. It needs `s3:BypassGovernanceRetention` AND the
# explicit `x-amz-bypass-governance-retention:true` header, held by one role,
# under a two-person rule, with an alarm on use.
#
# COMPLIANCE is correct only where the record-keeping obligation is *documented*
# as overriding the erasure right and a named person owns that determination. It
# is a legal decision, not a default, and it does not belong to whoever wrote the
# CDK stack.
#
# Retrofitting is possible, and the folklore that it is not makes teams abandon a
# remediation they could still perform. The documented rule is "before you
# lock", not "before you write": Object Lock can be enabled on an EXISTING
# bucket via `PutObjectLockConfiguration` with the
# `x-amz-bucket-object-lock-token` header, and object versions already sitting
# there can then be protected individually with `PutObjectRetention` or in bulk
# with S3 Batch Operations. A platform that has been writing evidence to an
# unlocked bucket for a year has a backfill job, not a dead end — and the
# backfill is worth doing precisely because the unprotected window is the part an
# examiner will ask about.

GOVERNANCE = "GOVERNANCE"
COMPLIANCE = "COMPLIANCE"

# Seven years is the common AML record-keeping floor. It is a placeholder for
# YOUR obligation, per record class — audit-trail.md argues for tiered retention
# rather than one blanket policy, and the bucket default is a floor for objects
# written without an explicit override, not a statement about every object
# present.
DEFAULT_RETENTION_YEARS = 7


class RetentionMisconfigured(ValueError):
    """A retention parameter that would be permanent, set without saying so."""


def object_lock_params(
    *,
    mode: str = GOVERNANCE,
    years: int | None = DEFAULT_RETENTION_YEARS,
    days: int | None = None,
    now: datetime | None = None,
    acknowledge_irreversible: bool = False,
) -> dict[str, Any]:
    """Build the `PutObject` Object Lock parameters, refusing the silent mistake.

    `COMPLIANCE` requires `acknowledge_irreversible=True`. A boolean is not a
    control against a determined caller and is not meant to be — it is a control
    against an *undeliberate* one, which is the actual failure mode: a mode
    string copied from a blog post into a CDK stack by someone who did not know
    the sentence about deleting the AWS account. Making the irreversible choice
    cost one more keystroke than the reversible one puts the decision in the diff
    where a reviewer can see it.
    """
    if mode not in (GOVERNANCE, COMPLIANCE):
        raise RetentionMisconfigured(
            f"Object Lock mode must be {GOVERNANCE!r} or {COMPLIANCE!r}, got "
            f"{mode!r}"
        )
    # `Days` OR `Years`, never both — the API rejects both, and a caller who
    # passes both usually believes one of them is being ignored.
    if (years is None) == (days is None):
        raise RetentionMisconfigured(
            "give exactly one of years= or days= for the retention period"
        )
    if mode == COMPLIANCE and not acknowledge_irreversible:
        raise RetentionMisconfigured(
            "COMPLIANCE mode retention cannot be shortened and the version "
            "cannot be deleted by any principal including root, so this "
            "RetainUntilDate is permanent. Pass "
            "acknowledge_irreversible=True if a named owner has made that "
            "determination, or use GOVERNANCE with a break-glass role."
        )

    started = now or datetime.now(timezone.utc)
    # 365-day years, deliberately approximate and deliberately not calendar
    # arithmetic: a retention floor is a minimum, so rounding must never round
    # DOWN. Under COMPLIANCE a date that is one day short cannot be corrected
    # downward later — it can only be extended, which is the safe direction.
    span_days = days if days is not None else years * 365  # type: ignore[operator]
    delta = timedelta(days=span_days)
    return {
        "ObjectLockMode": mode,
        "ObjectLockRetainUntilDate": started + delta,
    }


# ── The record ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InferenceParameters:
    """The parameters as SENT, not as configured.

    `control-stack.md`: the same model at different settings is a different
    system. There is no seed parameter available, so this record is the only
    reconstruction that exists — which is why the values that were actually put
    on the wire are stored, rather than a reference to a config file whose
    contents at the time nobody can now establish.

    `temperature` OR `topP`, not both: sending both is accepted and the
    interaction is unspecified, so a record showing both cannot be reconstructed
    even by re-running it.
    """

    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.temperature is not None and self.top_p is not None:
            raise ValueError(
                "send temperature OR topP, not both — a record showing both "
                "describes a run whose sampling behaviour is unspecified"
            )

    def as_sent(self) -> dict[str, Any]:
        sent: dict[str, Any] = {"maxTokens": self.max_tokens}
        if self.temperature is not None:
            sent["temperature"] = self.temperature
        if self.top_p is not None:
            sent["topP"] = self.top_p
        if self.stop_sequences:
            sent["stopSequences"] = list(self.stop_sequences)
        return sent


@dataclass
class DecisionRecord:
    """One agent invocation, complete enough to reconstruct the decision.

    The fields are `control-stack.md`'s table, plus the versions and parameters
    that table requires and that are easiest to omit.

    **Which fields are narrative, and therefore internal-only.** Every field
    below marked NARRATIVE is prose about a named person's transactions. None of
    it may reach an AWS-side log under the prod profile; all of it belongs in the
    firm's own store under the tenant key. The enforcement is not this comment —
    it is `log_projection`'s allowlist, which diverts anything not explicitly
    permitted. The comment tells you what to expect; the gate is what makes it
    true.

      * `raw_model_output`     NARRATIVE in part. Its enum, numeric and boolean
                              fields project; `rationale`, `red_flags[].statement`,
                              `mitigating_factors`, `gaps` and
                              `recommended_actions` do not. The gate splits it
                              field by field and emits counts for the lists.
      * `reasoning_trace`      NARRATIVE, entirely. Also testimony rather than
                              causation: it is the model's narration of a
                              process, not a verified account of one, and it
                              discharges neither citation grounding nor action
                              grounding. Store it as evidence of what the model
                              *said* its reasoning was.
      * `evidence`             NARRATIVE, entirely. The supplied evidence set is
                              customer data by definition.
      * `tool_calls`           NARRATIVE in part — the tool NAMES project, the
                              results are customer records.
      * `disagreement_rationale` NARRATIVE. A reviewer's own words about a named
                              customer. The highest-value field in the record for
                              quality purposes and never projectable.

    Everything else — identifiers, versions, enums, counts, hashes, timestamps —
    is projectable, and that is what makes a metering-only trend line attributable
    to the version of the system that produced it.
    """

    # Identity and join keys. `trace_id` is the join to the case-level record and
    # to telemetry; see `traceparent` below for where it comes from.
    tenant_id: str
    run_id: str
    trace_id: str
    workflow: str
    alert_id: str | None = None
    customer_id: str | None = None
    case_id: str | None = None

    # Provenance. A stored output is only interpretable against the prompt,
    # schema, model and reference data in force when it was produced.
    schema_version: str = ""
    prompt_version: str = ""
    # The RESOLVED model ID, never an inference-profile ARN. An ARN fails the
    # gate's `is_version` predicate (slashes, and a 12-digit account number the
    # sweep reads as a long digit run) and would be diverted as a defect. Resolve
    # the profile first; examples/cost_tracking.py already does that for pricing,
    # and the two should agree on what they call the model.
    model_id: str = ""
    inference_parameters: InferenceParameters | None = None
    reference_data_version: str = ""
    # Kept for the audit trail even though it does not project: which profile
    # produced the resolved model ID above.
    inference_profile_arn: str | None = None

    # What the model said, and what code made of it.
    raw_model_output: Mapping[str, Any] = field(default_factory=dict)
    reasoning_trace: str | None = None
    evidence: Any = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    # `ValidationResult.to_audit_record()` from examples/output_validation.py. A
    # stored "passed: true" is a claim about WHICH checks ran, so it carries its
    # own schema version.
    validation_result: Mapping[str, Any] = field(default_factory=dict)
    routing_decision: str = ""

    # Usage, for the metering half.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int = 0

    # The decision of record, once a human has authored one. Populated by
    # examples/human_approval_gate.py, never by the agent — and note that it is a
    # SEPARATE record with separate authorship, referenced here rather than
    # mutated into place. This field holds the reference and the outcome, not the
    # authority to produce them.
    human_decision: Mapping[str, Any] | None = None

    created_at: str = ""

    def gate_input(self) -> dict[str, Any]:
        """The flat mapping handed to `log_projection.split`.

        Flat because the gate's allowlist is keyed on field names: nesting the
        output under `output` would divert every field of it as one unknown key,
        losing the per-field decisions and the derived counts. The narrative
        fields stay in this dict on purpose — the gate is what removes them, and
        pre-filtering here would mean two places decide what is projectable.
        """
        record: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "workflow": self.workflow,
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "latency_ms": self.latency_ms,
            # Not on the allowlist — diverted to the bundle, which is where
            # control-stack.md's table puts them anyway.
            "record_format_version": RECORD_FORMAT_VERSION,
            "reference_data_version": self.reference_data_version,
            "inference_parameters": (
                self.inference_parameters.as_sent()
                if self.inference_parameters else None
            ),
            "inference_profile_arn": self.inference_profile_arn,
            "tool_calls": [dict(c) for c in self.tool_calls],
            "validation_result": dict(self.validation_result),
            "routing_decision": self.routing_decision,
            "human_decision": (dict(self.human_decision)
                               if self.human_decision else None),
            # Inside the MACed plaintext, not beside it. An HMAC tag carries no
            # timestamp of its own, so a tag over an untimestamped bundle is
            # replayable — the same tag verifies a bundle re-presented years
            # later as the record of a different run.
            "created_at": self.created_at or _utcnow_iso(),
        }
        if self.alert_id is not None:
            record["alert_id"] = self.alert_id
        if self.customer_id is not None:
            record["customer_id"] = self.customer_id
        if self.case_id is not None:
            record["case_id"] = self.case_id
        # The model's own output last, so a field it shares with the envelope
        # above cannot silently overwrite the envelope's value.
        for key, value in self.raw_model_output.items():
            record.setdefault(key, value)
        return record


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── Digest, then MAC ─────────────────────────────────────────────────────────

def bundle_digest(plaintext: bytes) -> str:
    """SHA-256 of the exact bytes stored, as hex.

    Over the STORED bytes rather than over the decoded object, so verification
    never depends on re-serialising a parsed bundle identically.
    """
    return hashlib.sha256(plaintext).hexdigest()


def mac_digest(
    digest_hex: str,
    *,
    key: bytes | None = None,
    kms_client: Any = None,
    kms_key_id: str | None = None,
) -> str:
    """MAC the DIGEST, never the bundle.

    Two reasons, and the first is a hard API limit: `GenerateMac` caps `Message`
    at 4,096 bytes, and any real evidence bundle exceeds that. The second is the
    documented consequence — verification must present the same digest that was
    signed — which is why `BUNDLE_DIGEST_ALG` travels on the row.

    The KMS path keeps the key out of application memory entirely and is the
    production shape. The local path exists so this can be exercised in CI with
    no account, and takes a TENANT-SCOPED key: a global MAC key turns a blind
    index into a cross-tenant join, and an index key that outlives a
    crypto-shred leaves a working oracle for the shredded plaintext.
    """
    message = bytes.fromhex(digest_hex)
    if len(message) > KMS_GENERATE_MAC_MAX_MESSAGE_BYTES:  # pragma: no cover
        raise ValueError("message exceeds the GenerateMac cap")

    if kms_client is not None:
        if not kms_key_id:
            raise ValueError("kms_client given without kms_key_id")
        response = kms_client.generate_mac(
            KeyId=kms_key_id,
            Message=message,
            MacAlgorithm=BUNDLE_MAC_ALG,
        )
        return response["Mac"].hex()

    if not key:
        raise ValueError(
            "mac_digest needs a tenant-scoped HMAC key or a KMS client; an "
            "unkeyed digest is a lookup oracle, not an integrity artefact"
        )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _checksum_sha256_b64(body: bytes) -> str:
    """S3's `ChecksumSHA256` is BASE64 of the digest, not hex.

    Worth its own function because the hex form is accepted by the SDK's type
    checks and rejected by the service, and the error names the header rather
    than the encoding.
    """
    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")


# ── Storage seams ────────────────────────────────────────────────────────────

class RowStore(Protocol):
    """The relational half. Injected, so the ordering below is testable."""

    def insert(self, row: Mapping[str, Any]) -> None: ...


class SealFailed(RuntimeError):
    """A durable write failed. Carries no record, because there is no record."""


@dataclass
class SealedRecord:
    """What `seal` produced, and everything `verify` needs to check it."""

    bucket: str
    key: str
    version_id: str
    checksum_sha256: str
    bundle_digest: str
    bundle_digest_alg: str
    bundle_mac: str
    bundle_mac_alg: str
    content_hash: str
    content_hash_alg: str
    object_lock_mode: str
    retain_until: datetime
    row: dict[str, Any]
    # Already through the gate. The caller sends it to the sink; it does not go
    # from here, so this module has no opinion about which sink.
    metering: dict[str, Any]
    projection: Projection


def seal(
    record: DecisionRecord,
    *,
    hmac_key: bytes,
    bucket: str,
    s3_client: Any,
    row_store: RowStore,
    tenant_key_arn: str,
    aead: Any = None,
    kms_client: Any = None,
    mac_key: bytes | None = None,
    mac_kms_client: Any = None,
    mac_kms_key_id: str | None = None,
    object_lock: Mapping[str, Any] | None = None,
    profile: Profile = Profile.PROD,
    key_prefix: str = "decision-records",
    now: datetime | None = None,
) -> SealedRecord:
    """Split, encrypt, write the bundle, then write the row that points at it.

    The ordering is the point of this function, so it is stated before the code:

      1. **Gate first, and it is free.** `split` and `emit_metering` are pure. A
         run whose metering projection is blocked has a defect worth stopping —
         and stopping before the first durable write makes the failure atomic
         rather than half-written. The trade-off is real and worth naming: a
         sweep hit costs you the record for that run until the defect is fixed
         and the run replayed. The alternative — store the bundle, drop the
         metering — leaves a metering gap that surfaces on an invoice instead of
         in an alarm.
      2. **Bundle to S3, and capture `VersionId`.** WORM, encrypted under the
         tenant key.
      3. **Row last, referencing bucket, key and that exact `VersionId`.**

    Why 2 before 3, which is the ordering people reverse because the row is the
    cheaper write: a row written first and an object write that then fails leaves
    a **dangling audit record** — a structured claim that evidence exists,
    naming a key that holds nothing. Every query finds the row, the retrieval
    finds nothing, and the discovery happens at the evidence request. Reversed,
    the worst case is an **orphaned object**: evidence with no index row, which
    is recoverable by listing the bucket and re-deriving the row from the bundle
    it contains. One is a false statement, the other is a housekeeping task.
    """
    if not record.created_at:
        record.created_at = _utcnow_iso()

    gate_input = record.gate_input()
    projection = split(
        gate_input,
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        hmac_key=hmac_key,
        reasoning_trace=record.reasoning_trace,
        retrieved_evidence=record.evidence,
        profile=profile,
    )
    # Raises MeteringLeakBlocked before anything durable happens. See (1) above.
    metering = emit_metering(projection)

    # Encrypt for retrieval. `encrypt_for_tenant` canonicalises the bundle with
    # the same serialiser this module digests with, so the plaintext under the
    # ciphertext and the bytes we digest are the same bytes by construction
    # rather than by agreement between two call sites.
    plaintext = canonical_json(projection.internal)
    digest_hex = bundle_digest(plaintext)
    mac_hex = mac_digest(
        digest_hex,
        key=mac_key or hmac_key,
        kms_client=mac_kms_client,
        kms_key_id=mac_kms_key_id,
    )

    encryption_context = {
        "tenant_id": record.tenant_id,
        "run_id": record.run_id,
        "record_format": RECORD_FORMAT_VERSION,
    }
    encrypted = encrypt_for_tenant(
        projection.internal,
        tenant_key_arn=tenant_key_arn,
        encryption_context=encryption_context,
        aead=aead,
        kms_client=kms_client,
    )
    body = encrypted.ciphertext

    lock = dict(object_lock) if object_lock else object_lock_params(now=now)
    key = f"{key_prefix}/{record.tenant_id}/{record.run_id}.bundle"

    try:
        response = s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            # Over the CIPHERTEXT, because that is what S3 received. This is
            # transfer and at-rest integrity for the object. The digest+MAC above
            # is evidentiary integrity for the PLAINTEXT — a tag over ciphertext
            # survives re-wrapping and proves only that the ciphertext is
            # unaltered, not that it decrypts to the same evidence. Two checks,
            # two questions; neither substitutes for the other.
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=_checksum_sha256_b64(body),
            **lock,
            # Object metadata is NOT encrypted and is returned by HeadObject to
            # anyone who can read the object. IDs and wrapped key material only —
            # the same argument as the encryption context.
            Metadata={
                "wrapped-data-key": base64.b64encode(
                    encrypted.wrapped_data_key).decode("ascii"),
                "kms-key-id": encrypted.key_id,
                "bundle-digest-alg": BUNDLE_DIGEST_ALG,
                "record-format-version": RECORD_FORMAT_VERSION,
            },
        )
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        # No row is written. A failed evidence write must surface as a failure,
        # never as a record asserting evidence that is not there.
        raise SealFailed(
            f"bundle write failed for run {record.run_id}; no row written: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    version_id = response.get("VersionId")
    if not version_id:
        # A PutObject with no VersionId means the bucket is not versioned, and
        # Object Lock requires versioning — so the WORM protection this record
        # claims is not in force. Treat it as a failed write rather than storing
        # a row that overstates the protection. This is the same defect as
        # accepting a response missing the identifier that proves it
        # (examples/output_validation.py): a placeholder that reads like data.
        raise SealFailed(
            f"PutObject returned no VersionId for s3://{bucket}/{key} — the "
            f"bucket is not versioned, so Object Lock is not in effect and the "
            f"row would overstate the protection"
        )

    row = {
        "tenant_id": record.tenant_id,
        "run_id": record.run_id,
        "trace_id": record.trace_id,
        "case_id": record.case_id,
        "alert_id": record.alert_id,
        "customer_id": record.customer_id,
        "workflow": record.workflow,
        "schema_version": record.schema_version,
        "prompt_version": record.prompt_version,
        "model_id": record.model_id,
        "record_format_version": RECORD_FORMAT_VERSION,
        "routing_decision": record.routing_decision,
        "validation_passed": bool(record.validation_result.get("passed")),
        # The pointer. All three parts, because a bucket and key without a
        # version identifies "whatever is current", and the current version is
        # exactly what a delete marker changes.
        "bundle_bucket": bucket,
        "bundle_key": key,
        "bundle_version_id": version_id,
        "bundle_checksum_sha256": _checksum_sha256_b64(body),
        # Two hash values doing two jobs. `content_hash` pairs this row to the
        # bundle `split` produced (in-process HMAC over the whole payload, no
        # size cap). `bundle_mac` is the archival integrity artefact, sized for
        # KMS's 4,096-byte message cap. Storing only the first means the
        # verification path cannot be moved to KMS later without re-MACing
        # history.
        "content_hash": projection.metering["content_hash"],
        "content_hash_alg": HASH_ALGORITHM,
        "bundle_digest": digest_hex,
        "bundle_digest_alg": BUNDLE_DIGEST_ALG,
        "bundle_mac": mac_hex,
        "bundle_mac_alg": BUNDLE_MAC_ALG,
        "object_lock_mode": lock["ObjectLockMode"],
        "retain_until": lock["ObjectLockRetainUntilDate"],
        "created_at": record.created_at,
    }
    try:
        row_store.insert(row)
    except Exception as exc:  # noqa: BLE001
        # The bundle is now an orphan. Say so loudly enough that the reconcile
        # job gets written: orphans are recoverable, and the recovery is a scan
        # of the bucket for versions with no row.
        raise SealFailed(
            f"row write failed for run {record.run_id}; the bundle is stored "
            f"and ORPHANED at s3://{bucket}/{key} versionId={version_id} — "
            f"re-derive the row from the bundle, do not re-write the bundle: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return SealedRecord(
        bucket=bucket,
        key=key,
        version_id=version_id,
        checksum_sha256=row["bundle_checksum_sha256"],
        bundle_digest=digest_hex,
        bundle_digest_alg=BUNDLE_DIGEST_ALG,
        bundle_mac=mac_hex,
        bundle_mac_alg=BUNDLE_MAC_ALG,
        content_hash=projection.metering["content_hash"],
        content_hash_alg=HASH_ALGORITHM,
        object_lock_mode=lock["ObjectLockMode"],
        retain_until=lock["ObjectLockRetainUntilDate"],
        row=row,
        metering=metering,
        projection=projection,
    )


@dataclass
class VerificationResult:
    """Whether the stored evidence is the evidence the row was written against."""

    ok: bool
    findings: list[str] = field(default_factory=list)
    version_found: bool = False
    # True when a delete marker is the current version. The record is HIDDEN,
    # not gone — a distinction worth reporting separately, because the two need
    # different responses.
    delete_marker_present: bool = False
    plaintext: bytes | None = None


def verify(
    row: Mapping[str, Any],
    *,
    s3_client: Any,
    hmac_key: bytes | None = None,
    decrypt: Any = None,
    mac_kms_client: Any = None,
    mac_kms_key_id: str | None = None,
) -> VerificationResult:
    """Re-read that exact object version and recompute the digest and the MAC.

    **`ListObjectVersions`, never `HeadObject`.** A delete request with no
    version ID returns `200 OK`, inserts a delete marker, and that marker becomes
    the current version — while a versioned delete against a protected version
    returns `403 Forbidden`. AWS is explicit that delete markers are not
    WORM-protected regardless of retention or legal hold, so the hiding move is
    available to anyone with ordinary delete permission. A retrieval procedure
    built on current objects therefore reports protected evidence as missing, and
    that answer to an examiner is worse than the truth or a real gap. Listing
    versions finds it; the delete marker becomes a finding of its own and
    deserves an alarm, because it is the one destructive-looking action Object
    Lock does not prevent.
    """
    findings: list[str] = []
    bucket = row["bundle_bucket"]
    key = row["bundle_key"]
    version_id = row["bundle_version_id"]

    listing = s3_client.list_object_versions(Bucket=bucket, Prefix=key)
    versions = [v for v in listing.get("Versions", []) if v.get("Key") == key]
    markers = [m for m in listing.get("DeleteMarkers", []) if m.get("Key") == key]
    match = next((v for v in versions if v.get("VersionId") == version_id), None)

    if markers:
        findings.append(
            f"delete marker present over s3://{bucket}/{key} — the current "
            f"object is a marker, not the evidence. The protected version is "
            f"still there; alarm on this and remove the marker."
        )
    if match is None:
        # Genuinely absent, which under Object Lock should be impossible before
        # the retention date. Either the retention was never applied or this row
        # names a version that was never written.
        findings.append(
            f"version {version_id} not present among {len(versions)} listed "
            f"version(s) of s3://{bucket}/{key} — a protected version cannot "
            f"be deleted, so either the lock was never applied or the row "
            f"names a version that was never written"
        )
        return VerificationResult(
            ok=False, findings=findings, version_found=False,
            delete_marker_present=bool(markers),
        )

    obj = s3_client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
    body = obj["Body"].read() if hasattr(obj["Body"], "read") else obj["Body"]

    stored_checksum = row.get("bundle_checksum_sha256")
    if stored_checksum and _checksum_sha256_b64(body) != stored_checksum:
        findings.append(
            "ciphertext checksum mismatch: the stored object is not the bytes "
            "PutObject acknowledged"
        )

    if decrypt is None:
        findings.append(
            "no decrypt seam supplied; the evidentiary digest is over the "
            "PLAINTEXT and cannot be checked against ciphertext"
        )
        return VerificationResult(
            ok=False, findings=findings, version_found=True,
            delete_marker_present=bool(markers),
        )

    plaintext = decrypt(body, obj.get("Metadata", {}))

    # The digest is over the bytes as stored, so no canonicaliser runs here — the
    # verification path has one fewer place to drift from the write path.
    recomputed_digest = bundle_digest(plaintext)
    if not hmac.compare_digest(recomputed_digest, str(row["bundle_digest"])):
        findings.append(
            f"bundle digest mismatch under {row.get('bundle_digest_alg')}: "
            f"stored evidence is not the evidence this row was written against"
        )

    # MAC the digest we just recomputed, not the one on the row — MACing the
    # stored digest would verify the row against itself and pass on a tampered
    # bundle.
    recomputed_mac = mac_digest(
        recomputed_digest,
        key=hmac_key,
        kms_client=mac_kms_client,
        kms_key_id=mac_kms_key_id,
    )
    # compare_digest, not ==. A plain comparison on a MAC returns early at the
    # first differing byte and leaks how much of a forgery was correct.
    if not hmac.compare_digest(recomputed_mac, str(row["bundle_mac"])):
        findings.append(
            f"bundle MAC mismatch under {row.get('bundle_mac_alg')}: the "
            f"digest is not the digest that was signed, or the MAC key is not "
            f"the tenant's"
        )

    # A delete marker does not invalidate the evidence, so it does not fail
    # verification — it is reported, loudly, as an operational finding.
    integrity_ok = not [f for f in findings if "mismatch" in f]
    return VerificationResult(
        ok=integrity_ok,
        findings=findings,
        version_found=True,
        delete_marker_present=bool(markers),
        plaintext=plaintext if integrity_ok else None,
    )


# ── The structured row ───────────────────────────────────────────────────────
#
# A `BEFORE UPDATE OR DELETE` trigger that raises, not a revoked grant.
#
# The argument, because it gets revisited: an IAM or role-level deny enumerates
# who may not write today. A later migration, a new service account, a
# well-meant `GRANT ALL` in a hotfix — any of these re-opens it, silently, and
# nothing in the schema records that the append-only property stopped holding. A
# trigger lives **in the data path**: it fires for every role including the table
# owner and the superuser, it cannot be re-granted around, and removing it is a
# `DROP TRIGGER` that appears in a migration diff where a reviewer sees it. A
# future change can silently re-add a grant; it cannot silently remove a trigger.
#
# Belt and braces, not either/or — keep the least-privilege grants too. The point
# is which one you rely on when the grants turn out to be wrong.

ROW_DDL = """
-- The queryable half of the audit record. The bulky examinable half lives in
-- S3 Object Lock; this row points at one specific object VERSION of it.
CREATE TABLE agent_decision_record (
    run_id                  uuid        PRIMARY KEY,
    tenant_id               uuid        NOT NULL,
    trace_id                char(32)    NOT NULL,
    case_id                 uuid            NULL,
    alert_id                uuid            NULL,
    customer_id             uuid            NULL,
    workflow                text        NOT NULL,

    -- Provenance. Without these a re-read applies today's semantics to
    -- yesterday's output and nobody can tell that the schema moved.
    schema_version          text        NOT NULL,
    prompt_version          text        NOT NULL,
    model_id                text        NOT NULL,
    record_format_version   text        NOT NULL,

    routing_decision        text        NOT NULL,
    validation_passed       boolean     NOT NULL,

    -- The pointer. version_id is NOT optional: bucket+key names "whatever is
    -- current", and the current object is exactly what a delete marker
    -- replaces.
    bundle_bucket           text        NOT NULL,
    bundle_key              text        NOT NULL,
    bundle_version_id       text        NOT NULL,
    bundle_checksum_sha256  text        NOT NULL,   -- base64, over ciphertext

    -- Pairing hash (row <-> bundle) and archival integrity artefact. Both
    -- algorithms are stored, because a verification in year four must know
    -- which scheme produced the value it is checking.
    content_hash            char(64)    NOT NULL,
    content_hash_alg        text        NOT NULL,
    bundle_digest           char(64)    NOT NULL,
    bundle_digest_alg       text        NOT NULL,
    bundle_mac              text        NOT NULL,
    bundle_mac_alg          text        NOT NULL,

    object_lock_mode        text        NOT NULL,
    retain_until            timestamptz NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT object_lock_mode_known
        CHECK (object_lock_mode IN ('GOVERNANCE', 'COMPLIANCE'))
);

-- Locating a subject's records without decrypting any of them: the blind index
-- of audit-trail.md §12. Index on tenant AND subject, not only on the case ID,
-- because erasure and subject-access requests arrive by subject.
CREATE INDEX agent_decision_record_subject
    ON agent_decision_record (tenant_id, customer_id, created_at DESC);
CREATE INDEX agent_decision_record_trace
    ON agent_decision_record (trace_id);

-- Append-only, enforced in the data path.
CREATE OR REPLACE FUNCTION agent_decision_record_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'agent_decision_record is append-only: % rejected on run_id %',
        TG_OP, COALESCE(OLD.run_id::text, '(unknown)')
        USING ERRCODE = 'restrict_violation';
END;
$$;

-- FOR EACH ROW, and BEFORE, so nothing is written and no partial statement
-- takes effect. A statement-level trigger would let a 0-row UPDATE pass, which
-- is harmless in itself and teaches the wrong thing about what the table
-- enforces.
CREATE TRIGGER agent_decision_record_no_update_or_delete
    BEFORE UPDATE OR DELETE ON agent_decision_record
    FOR EACH ROW EXECUTE FUNCTION agent_decision_record_immutable();

-- TRUNCATE is not UPDATE or DELETE and is not covered above. It is
-- statement-level only, which is why it needs its own trigger.
CREATE TRIGGER agent_decision_record_no_truncate
    BEFORE TRUNCATE ON agent_decision_record
    FOR EACH STATEMENT EXECUTE FUNCTION agent_decision_record_immutable();
"""


# ── W3C trace context ────────────────────────────────────────────────────────
#
# The trace ID is the join key between the per-invocation records, the
# case-level record and the telemetry. Propagated, a request through five agents
# is one trace with five spans; unpropagated it is five disconnected traces and
# correlation degrades to matching timestamps — which produces WRONG answers
# exactly when concurrent requests overlap, the normal condition for a fan-out.
#
# Where the ID comes from differs by invocation path, and this is the detail that
# costs a day:
#
#   * `InvokeAgentRuntime` accepts `traceId`, `traceParent`, `traceState`,
#     `baggage`, `runtimeSessionId` and `runtimeUserId` as request parameters,
#     and RETURNS the trace identifiers in the response. So the caller can pin
#     the trace ID onto the decision record at invocation time.
#   * `InvokeHarness` takes the same parameters but returns an EVENT STREAM and
#     NOT the trace headers. On the Harness path the caller must supply the trace
#     ID rather than learn it — which is fine, and only a problem if you wrote
#     the calling code against the Runtime response shape.
#
# And the security rule, the same one that applies to session IDs: **a trace ID
# arriving from a tenant-facing API is a correlation hint, never an identity.** A
# caller can inject any trace ID and any sampling decision. Derive it
# server-side; treat an inbound one as untrusted input.
#
# Unrelated trap worth knowing here because the two get conflated:
# `runtimeSessionId` has a MINIMUM length of 33, which quietly rejects a 32-hex
# trace ID used as a session ID.

_TRACEPARENT_RE = re.compile(
    r"00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})"
)


def new_trace_id() -> str:
    """A server-derived 16-byte trace ID as 32 lowercase hex."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """An 8-byte span ID as 16 lowercase hex."""
    return uuid.uuid4().hex[:16]


def format_traceparent(trace_id: str, span_id: str, *, sampled: bool = True) -> str:
    """Build a version-00 `traceparent`.

    All-zero IDs are invalid per the spec and are rejected here rather than
    propagated: an all-zero trace ID is what a partially-initialised SDK emits,
    and it correlates every such request into one trace.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or trace_id == "0" * 32:
        raise ValueError(f"trace_id must be 32 lowercase hex, non-zero: {trace_id!r}")
    if not re.fullmatch(r"[0-9a-f]{16}", span_id) or span_id == "0" * 16:
        raise ValueError(f"span_id must be 16 lowercase hex, non-zero: {span_id!r}")
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(header: str | None) -> dict[str, Any] | None:
    """Parse a `traceparent`, returning None on anything malformed.

    None rather than a raise, and rather than a fabricated ID: an unparseable
    inbound header means you do not know the caller's trace, and inventing one
    that looks like theirs is worse than starting a new one honestly. The caller
    decides — see `inject_traceparent`.
    """
    if not header:
        return None
    match = _TRACEPARENT_RE.fullmatch(header.strip())
    if match is None:
        return None
    if match["trace_id"] == "0" * 32 or match["span_id"] == "0" * 16:
        return None
    return {
        "trace_id": match["trace_id"],
        "parent_span_id": match["span_id"],
        "sampled": match["flags"].endswith("1"),
    }


def inject_traceparent(
    headers: Mapping[str, str] | None = None,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    sampled: bool = True,
    baggage: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return headers carrying this hop's trace context.

    `baggage` is where tenant and case IDs travel — identifiers only. Baggage is
    propagated to every downstream hop and lands in whatever those hops log, so
    it is subject to exactly the same rule as an AWS-side log: no narrative, no
    names. Put a customer name in baggage and you have re-created the leak the
    projection gate exists to prevent, one layer down and outside its reach.
    """
    out = dict(headers or {})
    resolved_trace = trace_id or new_trace_id()
    out["traceparent"] = format_traceparent(
        resolved_trace, span_id or new_span_id(), sampled=sampled
    )
    if baggage:
        for name, value in baggage.items():
            if not re.fullmatch(r"[0-9a-zA-Z._:-]{1,64}", str(value)):
                raise ValueError(
                    f"baggage[{name!r}]={value!r} is not an identifier; baggage "
                    f"propagates to every downstream log"
                )
        out["baggage"] = ",".join(f"{k}={v}" for k, v in baggage.items())
    return out


# ── Anti-patterns ────────────────────────────────────────────────────────────
#
# Each of these has shipped, and each reads as reasonable in the diff.
#
# 1. **The OTEL span as the record.** "We have full tracing, that IS the audit
#    trail." X-Ray trace retention is **30 days and not configurable**, so a
#    seven-year record-keeping obligation is met by an artefact that is gone in a
#    month — and nobody discovers it, because the design review happens in month
#    one. Two further disqualifiers on top of retention: AWS's own guidance
#    recommends SAMPLING (100% of errors plus a percentage of successes), and
#    sampled telemetry is not an audit trail; and past roughly fifty annotations
#    a span's attributes stop being searchable, so facts written into span
#    attributes are in the trace and not findable in it. Traces corroborate the
#    record. They are not it.
#
# 2. **DynamoDB with an IAM deny, presented as equivalent to WORM.** It
#    approximates append-only and the gap matters: there are no triggers, so the
#    control lives entirely in policy that a later change can re-grant, and
#    nothing in the table records that the property stopped holding. Its
#    compensating control is CloudTrail **data** events on item-level
#    operations — which are **OFF BY DEFAULT**. An unenabled compensating control
#    is not a weaker control, it is *no* control, and it is the kind of gap that
#    reads as fine in a design document and is discovered on the first evidence
#    request. If you must use it, enable the data events and assert they are on.
#
# 3. **PII in the trace.** A customer name in a span attribute, a rationale in an
#    annotation, an account number in baggage. It goes to a store with its own
#    retention, its own access model and no tenant key, it is copied to every
#    downstream hop, and it is outside the projection gate entirely — the gate
#    guards the log call, not the tracer. Identifiers only, on spans and in
#    baggage both.
#
# 4. **A hash as the archival record.** "The bundle is hashed with the tenant's
#    keys" passes review because it sounds cryptographically serious. Then the
#    first evidence request arrives and there is nothing to produce. Hashing is
#    one-way; no examiner has accepted a digest as a reasoning trace. The
#    diagnostic to run on your own design, on paper: answer "produce the
#    reasoning trace for case X". If the answer involves a hash, the design fails
#    in front of an examiner rather than in front of you.
#
# 5. **Row and bundle written by two paths that can disagree.** An ingest worker
#    writes the S3 bundle, a separate API handler writes the row, and they share
#    a convention about the key format. The convention drifts — a prefix change,
#    a tenant-ID casing difference, a retry that writes a second version — and
#    now some rows point at objects that exist and some do not, with no way to
#    tell which without reading every one. One function writes both, in one
#    order, and returns the identifiers it wrote. `seal` is that function; the
#    reason it takes both stores as arguments is so there is no second caller
#    that has only one of them.
#
# 6. **`HeadObject` as the retrieval check.** Covered above and repeated here
#    because it is the one that looks most like diligence: the retrieval
#    procedure confirms the object is there before reporting, and answers "no
#    record" for a record sitting under a delete marker, protected and intact.
#
# 7. **`assert` for the guards.** `python -O` strips assert statements, and it
#    strips the security-shaped ones first because those are the checks people
#    naturally express as invariants. Every refusal in this file is an `if` and a
#    `raise`.


# ── Verification ─────────────────────────────────────────────────────────────
#
# The cases below run against injected fakes: `python3 audit_record.py`, no AWS
# account, no test framework. They are the paths that FAIL, because the path that
# works is the one every hand-test already covers.
#
# What is deliberately NOT asserted here, and needs a real bucket:
#
#   * That Object Lock is actually in force. A fake accepts `ObjectLockMode` and
#     proves nothing. Assert it against the real bucket: `PutObject` a test
#     object, then attempt `DeleteObject --version-id` and require a 403; then
#     attempt a plain `DeleteObject` and require a 200 plus a delete marker.
#     Both directions, because the second is the one people are surprised by.
#   * That a lifecycle rule cannot expire a protected version. Verify once in
#     your own bucket and cite your test — the guarantee is stated in AWS
#     Knowledge Center material rather than in the S3 User Guide.
#   * That the trigger fires. `UPDATE agent_decision_record SET workflow=...`
#     must raise, run as the table OWNER and not only as the application role.
#   * That a cross-tenant decrypt is refused BY KMS rather than by your code —
#     mismatch the tenant in the encryption context and confirm where the
#     refusal comes from.

if __name__ == "__main__":  # pragma: no cover
    import copy
    import json
    from decimal import Decimal

    HMAC_KEY = b"self-check-tenant-hmac-key-not-for-production"
    TENANT = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    RUN = "c1a2b3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    PII = ("Maria", "Gonzalez", "Northgate", "Rotterdam", "87400")

    # ── Fakes ────────────────────────────────────────────────────────────────
    #
    # Not a cipher. A reversible byte transform standing in for AESGCM so the
    # round trip is exercisable with no dependency. Never in anything real: no
    # authentication, no nonce, no key commitment.
    def fake_aead(data_key: bytes, plaintext: bytes, aad: bytes) -> bytes:
        pad = data_key or b"\x01"
        return bytes(b ^ pad[i % len(pad)] for i, b in enumerate(plaintext))

    def fake_decrypt(ciphertext: bytes, metadata: Mapping[str, str]) -> bytes:
        wrapped = base64.b64decode(metadata["wrapped-data-key"])
        data_key = bytes(b ^ 0x5A for b in wrapped)  # undo FakeKMS's "wrap"
        return fake_aead(data_key, ciphertext, b"")

    class FakeKMS:
        def generate_data_key(self, *, KeyId, KeySpec=None, EncryptionContext=None,
                              **kw):
            plaintext = bytes(range(32))
            return {
                "Plaintext": plaintext,
                "CiphertextBlob": bytes(b ^ 0x5A for b in plaintext),
                "KeyId": KeyId,
            }

        def generate_mac(self, *, KeyId, Message, MacAlgorithm):
            if len(Message) > KMS_GENERATE_MAC_MAX_MESSAGE_BYTES:
                raise ValueError("ValidationException: Message too long")
            return {"Mac": hmac.new(b"kms-" + KeyId.encode(), Message,
                                    hashlib.sha256).digest(),
                    "MacAlgorithm": MacAlgorithm}

    class FakeS3:
        """Versioned, and able to hold a delete marker over an intact version."""

        def __init__(self, *, versioned: bool = True, fail_put: bool = False):
            self.versioned, self.fail_put = versioned, fail_put
            self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
            self.markers: list[dict[str, Any]] = []
            self.puts: list[dict[str, Any]] = []
            self._n = 0

        def put_object(self, **kw):
            self.puts.append(kw)
            if self.fail_put:
                raise RuntimeError("SlowDown: please reduce your request rate")
            self._n += 1
            vid = f"v{self._n:04d}"
            self.objects[(kw["Bucket"], kw["Key"], vid)] = {
                "Body": kw["Body"], "Metadata": kw.get("Metadata", {}),
                "ChecksumSHA256": kw.get("ChecksumSHA256"),
                "ObjectLockMode": kw.get("ObjectLockMode"),
                "ObjectLockRetainUntilDate": kw.get("ObjectLockRetainUntilDate"),
            }
            return {"VersionId": vid} if self.versioned else {}

        def list_object_versions(self, *, Bucket, Prefix=""):
            return {
                "Versions": [
                    {"Key": k, "VersionId": v}
                    for (b, k, v) in self.objects if b == Bucket
                    and k.startswith(Prefix)
                ],
                "DeleteMarkers": [m for m in self.markers
                                  if m["Key"].startswith(Prefix)],
            }

        def get_object(self, *, Bucket, Key, VersionId):
            return self.objects[(Bucket, Key, VersionId)]

        def delete_object(self, *, Bucket, Key):
            """A plain DELETE: 200 OK, a marker, and the version untouched."""
            self.markers.append({"Key": Key, "VersionId": "marker-1"})
            return {"DeleteMarker": True}

    class FakeRowStore:
        def __init__(self, *, fail: bool = False):
            self.rows: list[dict[str, Any]] = []
            self.fail = fail

        def insert(self, row):
            if self.fail:
                raise RuntimeError("deadlock detected")
            self.rows.append(dict(row))

    def make_record() -> DecisionRecord:
        return DecisionRecord(
            tenant_id=TENANT,
            run_id=RUN,
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            workflow="alert_triage",
            alert_id="b3d4f8a1-0c2e-4b6d-9f1a-7e5c2d8a4b60",
            customer_id="9c858901-8a57-4791-81fe-4c455b099bc9",
            case_id="5d6e7f80-1a2b-4c3d-9e4f-5a6b7c8d9e0f",
            schema_version="alert-triage-v1",
            prompt_version="alert-triage-v1",
            model_id="eu.amazon.nova-pro-v1",
            inference_parameters=InferenceParameters(
                max_tokens=2048, temperature=0.0,
                stop_sequences=("</assessment>",)),
            reference_data_version="typologies-2026-02",
            inference_profile_arn=(
                "arn:aws:bedrock:eu-west-1:123456789012:"
                "application-inference-profile/abcd1234"),
            raw_model_output={
                "recommendation": "REJECT", "risk_score": 84,
                "confidence": "high", "primary_typology": "STRUCTURING",
                "escalation_recommended": True,
                "account_takeover_suspected": False,
                "customer_may_be_victim": False,
                "additional_typologies": ["MULE_ACTIVITY"],
                "rationale": ("Maria Gonzalez received EUR 87,400 across five "
                              "credits from Northgate Logistics in Rotterdam, "
                              "each below the 10,000 reporting threshold."),
                "red_flags": [
                    {"statement": "Five credits just under the threshold",
                     "kind": "OBSERVATION", "evidence_id": "txn-3"},
                ],
                "mitigating_factors": [],
                "gaps": ["No response to the contact attempt of 4 March"],
                "recommended_actions": ["Escalate to L2 for Maria Gonzalez"],
            },
            reasoning_trace="Gonzalez appeared in two prior alerts in Rotterdam.",
            evidence={"txn-3": {"amount": Decimal("9800"),
                                "counterparty": "Northgate Logistics"}},
            tool_calls=({"tool": "get_transaction_history",
                         "result_rows": 42},),
            validation_result={"schema_version": "alert-triage-v1",
                               "passed": True, "blocking_errors": [],
                               "warnings": [], "forced_recommendation": None},
            routing_decision="HUMAN_REVIEW",
            input_tokens=3199, output_tokens=612, latency_ms=5824,
        )

    def seal_once(s3, rows, **kw):
        return seal(
            make_record(), hmac_key=HMAC_KEY, bucket="firm-evidence",
            s3_client=s3, row_store=rows,
            tenant_key_arn=f"arn:aws:kms:eu-west-1:123456789012:key/{TENANT}",
            aead=fake_aead, kms_client=FakeKMS(), **kw,
        )

    passed = failed = 0

    def check(label: str, cond: bool) -> None:
        global passed, failed
        if cond:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")

    # 1 — the happy path, and what the two halves are each allowed to hold.
    s3, rows = FakeS3(), FakeRowStore()
    sealed = seal_once(s3, rows)
    metering_blob = json.dumps(sealed.metering, default=str)
    check("1 metering holds no PII token",
          not any(t in metering_blob for t in PII))
    check("1 narrative diverted, counts emitted",
          "rationale" not in sealed.metering
          and sealed.metering.get("red_flags_count") == 1
          and sealed.metering.get("gaps_count") == 1)
    check("1 reasoning trace and evidence are internal only",
          "Gonzalez" in json.dumps(sealed.projection.internal, default=str)
          and "Gonzalez" not in metering_blob)
    check("1 inference parameters are on the record as sent",
          sealed.projection.internal["output"]["inference_parameters"]
          == {"maxTokens": 2048, "temperature": 0.0,
              "stopSequences": ["</assessment>"]})
    check("1 profile ARN kept internally, resolved model ID projected",
          sealed.metering.get("model_id") == "eu.amazon.nova-pro-v1"
          and "arn:aws" not in metering_blob)
    check("1 no gate defects on a clean record",
          not sealed.projection.defects)
    check("1 row carries bucket, key and versionId",
          rows.rows[0]["bundle_version_id"] == sealed.version_id
          and rows.rows[0]["bundle_bucket"] == "firm-evidence")
    check("1 ChecksumSHA256 is base64 of 32 bytes, not hex",
          len(base64.b64decode(sealed.checksum_sha256)) == 32
          and sealed.checksum_sha256 == s3.puts[0]["ChecksumSHA256"])
    check("1 Object Lock params present on the PutObject",
          s3.puts[0]["ObjectLockMode"] == GOVERNANCE
          and isinstance(s3.puts[0]["ObjectLockRetainUntilDate"], datetime))
    check("1 created_at is inside the MACed plaintext",
          "created_at" in sealed.projection.internal["output"])

    # 2 — verification of an untampered record.
    v = verify(rows.rows[0], s3_client=s3, hmac_key=HMAC_KEY,
               decrypt=fake_decrypt)
    check("2 untampered record verifies", v.ok and not v.findings)
    check("2 plaintext is the examinable bundle, in the clear",
          v.plaintext is not None and b"Gonzalez" in v.plaintext)

    # 3 — a tampered bundle must fail. This is the whole point of the digest.
    s3_t, rows_t = FakeS3(), FakeRowStore()
    sealed_t = seal_once(s3_t, rows_t)
    stored = s3_t.objects[("firm-evidence", sealed_t.key, sealed_t.version_id)]
    body = bytearray(stored["Body"])
    body[len(body) // 2] ^= 0x01          # one bit, mid-bundle
    stored["Body"] = bytes(body)
    v3 = verify(rows_t.rows[0], s3_client=s3_t, hmac_key=HMAC_KEY,
                decrypt=fake_decrypt)
    check("3 tampered bundle fails verification", not v3.ok)
    check("3 and the finding names the digest",
          any("digest mismatch" in f for f in v3.findings))
    check("3 no plaintext returned from a failed verification",
          v3.plaintext is None)

    # 4 — a wrong MAC key fails even when the digest is intact.
    v4 = verify(rows.rows[0], s3_client=s3, hmac_key=b"another-tenants-key",
                decrypt=fake_decrypt)
    check("4 wrong tenant MAC key fails", not v4.ok
          and any("MAC mismatch" in f for f in v4.findings))

    # 5 — bundle FIRST, then row. Proven by failure injection, not by reading
    #     the call order: a PutObject failure must leave no row behind.
    s3_f, rows_f = FakeS3(fail_put=True), FakeRowStore()
    try:
        seal_once(s3_f, rows_f)
        check("5 failed bundle write raises", False)
    except SealFailed:
        check("5 failed bundle write raises", True)
    check("5 and writes no dangling row", rows_f.rows == [])

    # 6 — the inverse: a row failure leaves an orphan, and says so.
    s3_o, rows_o = FakeS3(), FakeRowStore(fail=True)
    try:
        seal_once(s3_o, rows_o)
        check("6 failed row write raises", False)
    except SealFailed as exc:
        check("6 failed row write raises", True)
        check("6 and names the orphan for the reconcile job",
              "ORPHANED" in str(exc) and "versionId=" in str(exc))
    check("6 the bundle is still stored", len(s3_o.objects) == 1)

    # 7 — an unversioned bucket cannot carry Object Lock, so the row must not
    #     claim it does.
    s3_u, rows_u = FakeS3(versioned=False), FakeRowStore()
    try:
        seal_once(s3_u, rows_u)
        check("7 missing VersionId raises", False)
    except SealFailed as exc:
        check("7 missing VersionId raises", "not versioned" in str(exc))
    check("7 and writes no row", rows_u.rows == [])

    # 8 — a delete marker hides an intact protected version. HeadObject would
    #     report the record as gone; listing versions finds it.
    s3.delete_object(Bucket="firm-evidence", Key=sealed.key)
    v8 = verify(rows.rows[0], s3_client=s3, hmac_key=HMAC_KEY,
                decrypt=fake_decrypt)
    check("8 record still verifies under a delete marker", v8.ok)
    check("8 marker reported as its own finding",
          v8.delete_marker_present
          and any("delete marker" in f for f in v8.findings))

    # 9 — a row naming a version that was never written is not the same finding.
    ghost = dict(rows.rows[0], bundle_version_id="v9999")
    v9 = verify(ghost, s3_client=s3, hmac_key=HMAC_KEY, decrypt=fake_decrypt)
    check("9 absent version fails and is distinguished from a marker",
          not v9.ok and not v9.version_found)

    # 10 — retention: COMPLIANCE cannot be chosen by accident.
    try:
        object_lock_params(mode=COMPLIANCE, years=7)
        check("10 COMPLIANCE without acknowledgement refused", False)
    except RetentionMisconfigured as exc:
        check("10 COMPLIANCE without acknowledgement refused",
              "cannot be shortened" in str(exc))
    check("10 acknowledged COMPLIANCE is allowed",
          object_lock_params(mode=COMPLIANCE, years=7,
                             acknowledge_irreversible=True)
          ["ObjectLockMode"] == COMPLIANCE)
    for bad in ({"years": 7, "days": 30}, {"years": None, "days": None}):
        try:
            object_lock_params(**bad)  # type: ignore[arg-type]
            check(f"10 refuses {bad}", False)
        except RetentionMisconfigured:
            check(f"10 refuses {bad}", True)

    # 11 — the MAC is over the digest, so it fits KMS's message cap for a bundle
    #      of ANY size. Asserted on the cap rather than on this fixture, which is
    #      a deliberately compact ~1.5 kB and would squeeze under it: a check
    #      that only passes because the test bundle is small would go green right
    #      up to the first real reasoning trace.
    kms = FakeKMS()
    key_arn = "arn:aws:kms:eu-west-1:123456789012:key/mac"
    digest = bundle_digest(canonical_json(sealed.projection.internal))
    kms_mac = mac_digest(digest, kms_client=kms, kms_key_id=key_arn)
    check("11 KMS GenerateMac accepts a 32-byte digest as Message",
          len(kms_mac) == 64 and len(bytes.fromhex(digest)) == 32)
    try:
        kms.generate_mac(
            KeyId=key_arn,
            Message=b"x" * (KMS_GENERATE_MAC_MAX_MESSAGE_BYTES + 1),
            MacAlgorithm=BUNDLE_MAC_ALG)
        check("11 and rejects a message one byte over the 4,096 cap", False)
    except ValueError:
        check("11 and rejects a message one byte over the 4,096 cap", True)
    check("11 the digest is constant-size regardless of bundle size",
          len(bytes.fromhex(bundle_digest(b"x" * 5_000_000))) == 32)

    # 12 — the two hashes do two jobs, and the pairing one still holds.
    check("12 pairing hash survives onto the row",
          rows.rows[0]["content_hash"]
          == sealed.projection.metering["content_hash"])
    check("12 pairing verifies against the bundle",
          log_projection.verify_pairing(sealed.projection, key=HMAC_KEY))
    tampered = copy.deepcopy(sealed.projection)
    tampered.internal["output"]["rationale"] = "nothing to see here"
    check("12 pairing fails on a mutated bundle",
          not log_projection.verify_pairing(tampered, key=HMAC_KEY))

    # 13 — the DDL. String-level only: this asserts the DDL SAYS the right
    #      thing, not that Postgres enforces it. The enforcement check needs a
    #      database and is listed above.
    check("13 DDL declares a BEFORE UPDATE OR DELETE row trigger",
          "BEFORE UPDATE OR DELETE ON agent_decision_record" in ROW_DDL
          and "FOR EACH ROW" in ROW_DDL)
    check("13 DDL raises rather than returning NULL",
          "RAISE EXCEPTION" in ROW_DDL)
    check("13 DDL covers TRUNCATE separately",
          "BEFORE TRUNCATE ON agent_decision_record" in ROW_DDL)
    check("13 DDL requires the version ID, not just bucket and key",
          "bundle_version_id       text        NOT NULL" in ROW_DDL)

    # 14 — trace context.
    tp = format_traceparent("4bf92f3577b34da6a3ce929d0e0e4736",
                            "00f067aa0ba902b7")
    round_trip = parse_traceparent(tp)
    check("14 traceparent round-trips",
          round_trip is not None
          and round_trip["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
          and round_trip["sampled"] is True)
    check("14 malformed and all-zero headers return None, not a fake ID",
          parse_traceparent("garbage") is None
          and parse_traceparent(f"00-{'0'*32}-00f067aa0ba902b7-01") is None
          and parse_traceparent(None) is None)
    check("14 injected headers carry the pinned trace ID",
          inject_traceparent(trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
                             span_id="00f067aa0ba902b7")["traceparent"] == tp)
    try:
        inject_traceparent(trace_id=new_trace_id(),
                           baggage={"customer": "Maria Gonzalez"})
        check("14 baggage refuses free text", False)
    except ValueError:
        check("14 baggage refuses free text", True)
    check("14 baggage accepts identifiers",
          "baggage" in inject_traceparent(baggage={"tenant_id": TENANT}))

    # 15 — inference parameters that cannot describe a reproducible run.
    try:
        InferenceParameters(max_tokens=1, temperature=0.2, top_p=0.9)
        check("15 temperature AND topP refused", False)
    except ValueError:
        check("15 temperature AND topP refused", True)

    print(f"\naudit_record self-check: {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
