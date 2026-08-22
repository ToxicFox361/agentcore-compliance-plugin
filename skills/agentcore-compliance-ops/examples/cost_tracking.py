"""Per-model cost attribution for multi-tenant agent usage.

Three defects converge here, and together they produce a dashboard that shows a
confident wrong number with no error anywhere:

  1. Usage events without a model ID — per-model rates cannot be applied
  2. Rates hardcoded from published figures — those are us-east-1, not yours
  3. Cost fields not projected through the read API — activating a client-side
     fallback at some other model's rate

The rule that follows from all three: when the cost of a record is not known,
say so. This module never substitutes a fallback rate for an unrecognised or
missing model — it counts the tokens, marks the tenant's cost
`pricing_unavailable`, and emits `cost: null` at the read boundary. A rate
substituted for an unknown model is defect 2 wearing defect 1's clothes: the
number is arithmetically perfect and about the wrong model, and nothing in the
pipeline can tell afterwards which rows it touched.

See references/production-rules.md §9 and §13.

── Why this exists alongside application inference profiles, not instead ──────

AWS's recommended path for per-product, per-team or per-tenant Bedrock cost
attribution is **application inference profiles carrying cost allocation tags**:
create a profile per tenant with `CreateInferenceProfile`, tag it, invoke through
its ARN, and the spend lands in Cost Explorer already partitioned. That is
AWS-side ground truth, it needs no rate table, and it cannot drift from the
invoice because it *is* the invoice. If you have not read that guidance, read it
before reading this file — a reader who cannot tell that this pipeline was a
deliberate choice will reasonably assume it was written by someone unaware of
the alternative.

This pipeline exists because tagged profiles answer a different question. Cost
Explorer is a 24-48h-lagged, daily-granularity reporting surface. It cannot
enforce a per-tenant token limit mid-conversation, cannot show an operator what a
tenant has spent in the last ten minutes, and cannot attribute a single request.
A platform that on-bills tenants and applies usage caps needs a per-request,
near-real-time figure, and that has to be computed.

The two are complementary, and the reconciliation is the point:

  * The tagged profile is the authoritative number. Reconcile the computed
    figure against it on a schedule and alarm on the divergence — that is what
    catches a stale rate table, a missed geographic prefix, and a model swap
    nobody told the pricing layer about.
  * A computed figure with no path back to the invoice is unattestable. For a
    platform that on-bills, "our system says the tenant owes this" is not a
    defensible statement unless it can be tied to what AWS charged. The rate
    table is a model of the bill; the bill is the bill.

So: tag the profiles anyway (see the runtime tags in examples/agent_runtime.tf),
and treat this module as the low-latency estimate that must be shown to agree.
"""

import os
import re
from decimal import Decimal
from typing import NamedTuple

import boto3

dynamodb = boto3.resource("dynamodb")
aggregation_table = dynamodb.Table(os.environ["AGGREGATION_TABLE_NAME"])

# Control-plane client, for resolving an inference-profile ARN to the model
# behind it. Needs `bedrock:GetInferenceProfile`; without that grant every
# ARN-addressed record lands in pricing_unavailable, so grant it or accept that
# outcome knowingly.
bedrock = boto3.client("bedrock")

# ── Rate table ───────────────────────────────────────────────────────────────
# USD per 1,000 tokens.
#
# FOUR axes, not two. A cached request bills on input, output, cache reads and
# cache writes, at different rates: a cache read is heavily discounted against
# uncached input, and a cache write costs MORE than it (AWS documents the write
# at 1.25x the uncached input rate). A two-column table gets both wrong at once
# and in opposite directions — it overcharges the cache reads, which are the bulk
# of a caching workload's input tokens, and undercharges the writes.
#
# Worth being blunt about the direction of the error: on a workload with a long
# cached system prompt, a two-column table reports a number materially HIGHER
# than the invoice. It flatters the platform's margin calculation and looks
# conservative, so nobody questions it, and the discrepancy is discovered by
# whoever eventually reconciles against Cost Explorer.
#
# Not every model supports prompt caching, and those that do are not priced by a
# uniform multiplier. Verify per model and per region like every other rate here.
#
# Keyed on the BARE model ID. Agents are deployed against geographic inference
# profiles (eu.amazon.nova-2-lite-v1:0), so the prefix is stripped before
# lookup — otherwise changing geography silently drops every cost to the
# fallback rate.
#
# ⚠ These rates are illustrative. Bedrock pricing is REGIONAL and published
# figures are almost always us-east-1. Populate from the Price List API for
# your deployment region and record the date:
#
#   aws pricing get-products --service-code AmazonBedrockService --region us-east-1 \
#     --filters Type=TERM_MATCH,Field=regionCode,Value=<your-region> \
#               Type=TERM_MATCH,Field=model,Value="<Model Name>" \
#               Type=TERM_MATCH,Field=feature,Value="On-demand Inference" \
#               Type=TERM_MATCH,Field=inferenceType,Value="Input tokens"
#
# Note the service code. There are several, and picking the wrong one returns an
# empty result set rather than an error — which reads like "no pricing exists":
#
#   AmazonBedrockService         current-generation model SKUs
#   AmazonBedrock                older models; also has the `batch` attribute
#   AmazonBedrockFoundationModels  has NO `model` attribute at all
#   AmazonBedrockAgentCore       AgentCore's own charges, separate from tokens
#
# `--region us-east-1` is the Price List API endpoint, NOT the region you are
# pricing — that is the `regionCode` filter. Conflating them is how you end up
# with us-east-1 rates again by a longer route.
#
# Discriminators you must set or you will silently sum several tiers together:
#   feature        "On-demand Inference" excludes reserved/provisioned SKUs
#   inferenceType  input vs output, and "... long context" is priced separately
#   crossRegion    "Geo" vs "Global" are different rates
#   service_tier   "standard" vs flex/priority/batch, where offered
#   batch          on AmazonBedrock only, and the values are the strings
#                  "True"/"False" — not "Yes"/"No"
#
# Enumerate the real attribute values rather than guessing:
#   aws pricing get-attribute-values --service-code AmazonBedrockService \
#     --attribute-name model
class Rates(NamedTuple):
    """Per-1,000-token rates for one model. Named, because a 4-tuple read
    positionally is how cache_read and cache_write get transposed."""
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


MODEL_PRICING: dict[str, Rates] = {
    # Placeholder values — replace with verified rates for your region.
    # cache_read/cache_write here are the documented SHAPE (0.1x and 1.25x of
    # uncached input), not verified figures. Treat them as wrong until priced.
    "amazon.nova-micro-v1:0": Rates(
        Decimal("0.000023"), Decimal("0.000184"),
        Decimal("0.0000023"), Decimal("0.00002875")),
    "amazon.nova-lite-v1:0": Rates(
        Decimal("0.000078"), Decimal("0.000156"),
        Decimal("0.0000078"), Decimal("0.0000975")),
    "amazon.nova-pro-v1:0": Rates(
        Decimal("0.000525"), Decimal("0.0021"),
        Decimal("0.0000525"), Decimal("0.00065625")),
    "amazon.nova-2-lite-v1:0": Rates(
        Decimal("0.000429"), Decimal("0.001635"),
        Decimal("0.0000429"), Decimal("0.00053625")),
}

# Strip a leading geographic inference-profile prefix.
#
# The previous form was the tuple ("eu.", "us.", "apac.", "global.") — an
# enumeration that was already incomplete when written. `us-gov.`, `jp.` and
# `au.` are all real routing scopes (the CDK's own
# CrossRegionInferenceProfileRegion enum lists GLOBAL, EU, US, US_GOV, APAC, JP,
# AU), and AWS adds geographies faster than example lists get revised. Every
# prefix the list misses lands its records in `pricing_unavailable`: the same
# silent-zero failure this module's header warns about, arriving by a different
# door — not a wrong rate this time, but a whole geography's spend quietly
# reported as unpriceable after a routine region rollout.
#
# So match the SHAPE instead. A geographic prefix is a short lowercase token,
# optionally hyphenated, followed by a dot — and crucially, what remains after
# stripping it must ITSELF still contain a dot, because a bare model ID is
# `<vendor>.<model>` with exactly one. That lookahead is what stops the pattern
# eating a four-letter vendor prefix: `meta.llama3-70b-instruct-v1:0` leaves
# `llama3-70b-instruct-v1:0`, which has no dot, so nothing is stripped.
GEO_PREFIX_RE = re.compile(r"^[a-z]{2,6}(?:-[a-z]{2,6})?\.(?=[^.]+\.)")

# An inference-profile ARN resolves to a model ID via one control-plane call,
# so cache it. Successes only — see _model_id_behind_profile.
_PROFILE_MODEL_CACHE: dict[str, str] = {}

_PROFILE_ARN_RE = re.compile(
    r"^arn:[^:]*:bedrock:[^:]*:[^:]*:(?:application-)?inference-profile/(?P<id>.+)$"
)


def _model_id_behind_profile(profile_arn: str) -> str | None:
    """Resolve an inference-profile ARN to the bare model ID behind it.

    An application inference profile is addressed by ARN, never by a model ID —
    that is the whole mechanism. So the moment a platform adopts AWS's own
    recommended attribution path, every `model_id` reaching this module is an
    ARN, matches nothing in the rate table, and EVERY record becomes
    `pricing_unavailable` with the loud warning firing on every single one. The
    failure is total and it is triggered by taking AWS's advice, which is a
    genuinely nasty shape: the correct action breaks the tool.

    Failing closed was still better than fabricating a rate, but it is not the
    only option. `GetInferenceProfile` returns the foundation model(s) the
    profile fronts, so the ID is recoverable in one call.
    """
    if profile_arn in _PROFILE_MODEL_CACHE:
        return _PROFILE_MODEL_CACHE[profile_arn]

    try:
        resp = bedrock.get_inference_profile(inferenceProfileIdentifier=profile_arn)
    except Exception as e:
        # NOT cached. A throttle or a transient failure must not permanently
        # mark a priceable profile unpriceable for the life of the process —
        # that would convert one bad minute into a silently incomplete bill.
        print(f"WARNING: GetInferenceProfile failed for {profile_arn!r}: "
              f"{type(e).__name__}: {e}; cost unavailable for this record")
        return None

    # A profile fronting a cross-region system profile lists one modelArn per
    # destination region — same model, different regions. Reduce to the distinct
    # model IDs and require exactly one: more than one means the profile spans
    # models priced differently, and there is no single rate to apply. Say so
    # rather than picking the first.
    bare_ids = {
        arn.rsplit("/", 1)[-1]
        for arn in (m.get("modelArn", "") for m in resp.get("models") or [])
        if "/" in arn
    }
    if len(bare_ids) != 1:
        print(f"WARNING: profile {profile_arn!r} fronts {sorted(bare_ids)}; "
              f"no single rate applies, cost recorded as unavailable")
        return None

    model_id = bare_ids.pop()
    _PROFILE_MODEL_CACHE[profile_arn] = model_id
    return model_id


def resolve_pricing(model_id: str) -> Rates | None:
    """Return per-1,000-token Rates, or None if unknown.

    There is deliberately no fallback rate. `None` means "we do not know what
    this cost", which is a real answer and the only honest one available.

    The fallback this replaced was the defect in miniature: an unrecognised
    model was billed at whichever rate happened to be hardcoded, the warning
    below went to a log nobody reads, and the dashboard showed a confident
    figure. Because the substituted ID resolved cleanly, nothing downstream
    could distinguish a rated row from a guessed one — not the aggregate, not
    the audit trail, not the invoice.

    Missing is a state a cost dashboard must be able to render. Wrong is not.
    """
    bare = model_id

    # ARN first: an inference-profile ARN has no bare model ID to strip a prefix
    # from, so prefix handling would just pass it through unchanged.
    #
    # Both ARN forms go through the same resolution. A SYSTEM_DEFINED profile's
    # ID segment happens to look like a prefixed model ID, so it could be parsed
    # locally — but that resemblance is a coincidence of naming, not a contract,
    # and an APPLICATION profile's ID is opaque. Ask the service for both; the
    # cache means it costs one call per distinct ARN per process.
    arn_match = _PROFILE_ARN_RE.match(bare)
    if arn_match:
        resolved = _model_id_behind_profile(bare)
        if resolved is None:
            return None
        bare = resolved

    bare = GEO_PREFIX_RE.sub("", bare, count=1)

    if bare in MODEL_PRICING:
        return MODEL_PRICING[bare]

    # Still log loudly — this is a fix-the-rate-table signal, not a routine
    # condition. The difference from before is that the loud warning is now the
    # only thing that happens, rather than a footnote to a fabricated number.
    print(f"WARNING: no pricing for {model_id!r} (resolved {bare!r}); "
          f"tokens counted, cost recorded as unavailable — add the verified "
          f"regional rate to MODEL_PRICING")
    return None


def send_to_dlq(record: dict, *, reason: str) -> None:
    """Park an unusable usage record where someone will see it.

    A dead-letter queue with an alarm on depth. The point is that the record
    stops here loudly rather than continuing as a plausible-looking row.
    """
    print(f"ERROR: quarantining usage record {record.get('id')!r}: {reason}")
    # sqs.send_message(QueueUrl=USAGE_DLQ_URL, MessageBody=json.dumps(record))


def process_usage_records(records: list[dict]) -> None:
    """Aggregate usage into per-tenant cost.

    Cost is resolved PER RECORD, not per tenant: one tenant may run several
    agents on different models, each with its own rate.
    """
    per_tenant: dict[str, dict] = {}

    for rec in records:
        # No default. A record without a tenant is not a record belonging to a
        # tenant called "unknown" — that placeholder creates a real, billable
        # aggregation row that looks like data and hides a broken producer.
        # Divert it instead: raising here would poison the whole SQS batch and
        # replay the good records, so quarantine the one bad message and let
        # the rest aggregate.
        tenant_id = rec.get("tenant_id")
        if not tenant_id:
            send_to_dlq(rec, reason="missing tenant_id")
            continue

        # Defect 1 from the header, in the one place it can still slip through.
        # The old shape here was `model_id = rec.get("model_id") or FALLBACK`,
        # which rated the record at the fallback WITHOUT tripping the warning
        # in resolve_pricing, because the fallback ID is in the table and
        # resolves cleanly.
        #
        # The choice it appeared to force — drop the usage, or mis-rate it —
        # was false. There is a third option: keep the tokens, which are real
        # and correct, and record the cost as unavailable, which is true.
        model_id = rec.get("model_id")
        if not model_id:
            print(f"WARNING: usage record {rec.get('id')!r} for tenant "
                  f"{tenant_id!r} has no model_id; tokens counted, cost "
                  f"unavailable — fix the emitter, not this line")

        input_tokens = int(rec.get("input_tokens", 0))
        output_tokens = int(rec.get("output_tokens", 0))

        # Emitted from Bedrock's usage object fields `cacheReadInputTokens` and
        # `cacheWriteInputTokens` — see emit_usage in examples/agent_template.py.
        # Both default to 0, which is correct: a model or request without caching
        # reports no cache tokens. What is NOT correct is an emitter that never
        # sends them while caching is switched on, because the zero is then
        # indistinguishable from a real zero and the discount silently never
        # reaches the tenant's bill. Assert on the emitter, not on this default.
        cache_read_tokens = int(rec.get("cache_read_tokens", 0))
        cache_write_tokens = int(rec.get("cache_write_tokens", 0))

        agg = per_tenant.setdefault(tenant_id, {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "request_count": 0,
            "input_cost": Decimal("0"), "output_cost": Decimal("0"),
            "cache_read_cost": Decimal("0"), "cache_write_cost": Decimal("0"),
            "unpriced_records": 0, "unpriced_models": set(),
        })

        rates = resolve_pricing(model_id) if model_id else None
        if rates is None:
            # One unpriceable record makes this tenant's cost incomplete, and
            # an incomplete total is not a smaller total — it is a number with
            # no defined meaning. Count it so the read side can refuse to
            # render a figure at all.
            agg["unpriced_records"] += 1
            agg["unpriced_models"].add(model_id or "<missing model_id>")
        else:
            thousand = Decimal("1000")
            agg["input_cost"] += Decimal(input_tokens) * rates.input / thousand
            agg["output_cost"] += Decimal(output_tokens) * rates.output / thousand
            agg["cache_read_cost"] += (
                Decimal(cache_read_tokens) * rates.cache_read / thousand)
            agg["cache_write_cost"] += (
                Decimal(cache_write_tokens) * rates.cache_write / thousand)

        agg["input_tokens"] += input_tokens
        agg["output_tokens"] += output_tokens
        agg["cache_read_tokens"] += cache_read_tokens
        agg["cache_write_tokens"] += cache_write_tokens
        agg["total_tokens"] += int(rec.get("total_tokens", 0))
        agg["request_count"] += 1

    # Why the stored row holds a partial sum plus a flag, rather than a literal
    # null in total_cost: these are atomic counters. `ADD` accumulates across
    # batches, and it fails with a ValidationException if the attribute it
    # targets is currently NULL — so writing null once would break every
    # subsequent increment for that tenant. Atomic accumulation and "this value
    # is unknown" do not compose in one attribute.
    #
    # Split them instead. The counters stay numeric and keep accumulating over
    # the records that COULD be priced; `unpriced_records` counts the ones that
    # could not. The null is produced at the read boundary, by
    # cost_for_response below, which is where a consumer actually sees it.
    #
    # `pricing_unavailable` is only ever set true, never back to false, and
    # `unpriced_records` only ever increases — so the condition is sticky. A
    # later clean batch must not be able to launder an earlier unpriced one
    # into a total that reads as complete.
    for tenant_id, agg in per_tenant.items():
        values = {
            ":i": Decimal(agg["input_tokens"]),
            ":o": Decimal(agg["output_tokens"]),
            ":t": Decimal(agg["total_tokens"]),
            ":c": Decimal(agg["request_count"]),
            ":cr": Decimal(agg["cache_read_tokens"]),
            ":cw": Decimal(agg["cache_write_tokens"]),
            ":tid": tenant_id,
        }
        add_clause = ("ADD input_tokens :i, output_tokens :o, "
                      "total_tokens :t, request_count :c, "
                      "cache_read_tokens :cr, cache_write_tokens :cw")
        set_clause = "SET tenant_id = :tid"

        priced_cost = (agg["input_cost"] + agg["output_cost"]
                       + agg["cache_read_cost"] + agg["cache_write_cost"])
        if priced_cost:
            # total_cost sums all FOUR axes. Omitting the cache axes from the
            # total while storing them separately would give a dashboard two
            # plausible answers to "what did this tenant cost" and no way to tell
            # which one it rendered.
            add_clause += (", input_cost :ic, output_cost :oc, "
                           "cache_read_cost :crc, cache_write_cost :cwc, "
                           "total_cost :tc")
            values[":ic"] = agg["input_cost"]
            values[":oc"] = agg["output_cost"]
            values[":crc"] = agg["cache_read_cost"]
            values[":cwc"] = agg["cache_write_cost"]
            values[":tc"] = priced_cost

        if agg["unpriced_records"]:
            add_clause += ", unpriced_records :u, unpriced_models :m"
            set_clause += ", pricing_unavailable = :flag"
            values[":u"] = Decimal(agg["unpriced_records"])
            values[":m"] = agg["unpriced_models"]   # string set — ADD unions
            values[":flag"] = True

        aggregation_table.update_item(
            Key={"aggregation_key": f"tenant:{tenant_id}"},
            UpdateExpression=f"{add_clause} {set_clause}",
            ExpressionAttributeValues=values,
        )


# ── Read API ─────────────────────────────────────────────────────────────────

# The aggregation layer is the single source of truth for cost *attribution* —
# which tenant, which model, which request — because only it knows which model
# produced each record. It is deliberately NOT the source of truth for what the
# firm owes: that is the invoice, and the tagged application inference profile is
# what reconciles against it (see the header). Conflating the two is how a
# computed figure reaches a tenant invoice with no path back to AWS billing,
# which for a platform that on-bills is a control finding rather than a rounding
# argument.
#
# Project the cost fields. Omitting them does not blank the dashboard's cost
# column — it activates whatever client-side fallback exists, typically
# recomputing from rates for a different model. The result is a plausible,
# confident, wrong number and no error anywhere (§13).
#
# The unpriced fields are projected for the same reason as the cost fields. A
# consumer that cannot see `pricing_unavailable` renders whatever is in
# `total_cost` — the partial sum — as though it were the answer, which is the
# fabricated number arriving by a different route.
USAGE_PROJECTION = (
    "aggregation_key, tenant_id, total_tokens, token_limit, #ts, "
    "input_tokens, output_tokens, request_count, "
    "cache_read_tokens, cache_write_tokens, "
    "input_cost, output_cost, cache_read_cost, cache_write_cost, total_cost, "
    "pricing_unavailable, unpriced_records, unpriced_models"
)
USAGE_EXPRESSION_NAMES = {"#ts": "timestamp"}   # 'timestamp' is reserved


def cost_for_response(row: dict) -> dict:
    """Shape one aggregation row for the read API.

    Cost is a number or it is null. There is no third option, and there is
    certainly no best-effort figure — this is the boundary where the rule "if
    the field is missing, show it as missing" is actually enforced.

    Tokens are reported either way: they are counted accurately even when the
    rate is unknown, and a tenant's usage is still useful without its price.
    """
    unpriced = int(row.get("unpriced_records", 0) or 0)
    if row.get("pricing_unavailable") or unpriced:
        return {
            "tenant_id": row.get("tenant_id"),
            "total_tokens": row.get("total_tokens"),
            "request_count": row.get("request_count"),
            "cache_read_tokens": row.get("cache_read_tokens"),
            "cache_write_tokens": row.get("cache_write_tokens"),
            "input_cost": None,
            "output_cost": None,
            "cache_read_cost": None,
            "cache_write_cost": None,
            "total_cost": None,
            "pricing_unavailable": True,
            "unpriced_records": unpriced,
            # Name the models so the fix is obvious: these are the rates
            # missing from MODEL_PRICING.
            "unpriced_models": sorted(row.get("unpriced_models") or []),
        }

    return {
        "tenant_id": row.get("tenant_id"),
        "total_tokens": row.get("total_tokens"),
        "request_count": row.get("request_count"),
        "cache_read_tokens": row.get("cache_read_tokens"),
        "cache_write_tokens": row.get("cache_write_tokens"),
        "input_cost": row.get("input_cost"),
        "output_cost": row.get("output_cost"),
        "cache_read_cost": row.get("cache_read_cost"),
        "cache_write_cost": row.get("cache_write_cost"),
        "total_cost": row.get("total_cost"),
        "pricing_unavailable": False,
    }


# ── Verification ─────────────────────────────────────────────────────────────
#
# Assert end to end, not just that the computation is right:
#
#   1. usage record carries model_id
#   2. aggregation row carries input_cost/output_cost/total_cost
#   3. the read API RETURNS those fields
#   4. the rendered value matches input_tokens * rate / 1000
#
# Step 3 is the one that gets missed, and the one that fails silently.
#
# Then assert the unpriced path explicitly, because it is the one nobody
# exercises: feed in a usage record naming a model absent from MODEL_PRICING,
# and one with no model_id at all. Both should produce counted tokens, a
# warning, `pricing_unavailable` true on the row, and `total_cost: null` out of
# cost_for_response — never a number. A test suite that only ever sends known
# models cannot tell this implementation from the fallback one it replaced.
#
# Three more cases, each one a defect this file has actually carried:
#
#   5. An inference-profile ARN as model_id. Resolves via GetInferenceProfile
#      and prices normally. Before that path existed, adopting AWS's own
#      recommended attribution mechanism sent EVERY record unpriced.
#   6. A geography the prefix handling has never seen — `jp.`, `au.`,
#      `us-gov.`, and whatever ships next. All must price. Assert on the shape,
#      not on a list you will forget to update.
#   7. A cached request: non-zero cache_read_tokens and cache_write_tokens must
#      appear in total_cost at their own rates, and must survive the read
#      projection. A test that only sends uncached requests passes against a
#      two-column table.
#
# And reconcile against the bill. Sum `total_cost` for a tenant over a settled
# period and compare it to Cost Explorer filtered on that tenant's cost
# allocation tag. Alarm on the divergence exceeding a threshold you choose
# deliberately. This is the only test that catches a rate table that was correct
# when it was written.
