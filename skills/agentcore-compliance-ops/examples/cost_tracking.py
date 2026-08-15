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
"""

import os
from decimal import Decimal

import boto3

dynamodb = boto3.resource("dynamodb")
aggregation_table = dynamodb.Table(os.environ["AGGREGATION_TABLE_NAME"])

# ── Rate table ───────────────────────────────────────────────────────────────
# USD per 1,000 tokens as (input, output).
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
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # placeholder values — replace with verified rates for your region
    "amazon.nova-micro-v1:0":  (Decimal("0.000023"), Decimal("0.000184")),
    "amazon.nova-lite-v1:0":   (Decimal("0.000078"), Decimal("0.000156")),
    "amazon.nova-pro-v1:0":    (Decimal("0.000525"), Decimal("0.0021")),
    "amazon.nova-2-lite-v1:0": (Decimal("0.000429"), Decimal("0.001635")),
}

GEO_PREFIXES = ("eu.", "us.", "apac.", "global.")


def resolve_pricing(model_id: str) -> tuple[Decimal, Decimal] | None:
    """Return (input_rate, output_rate) per 1,000 tokens, or None if unknown.

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
    for prefix in GEO_PREFIXES:
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break

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

        agg = per_tenant.setdefault(tenant_id, {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "request_count": 0,
            "input_cost": Decimal("0"), "output_cost": Decimal("0"),
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
            in_rate, out_rate = rates
            agg["input_cost"] += Decimal(input_tokens) * in_rate / Decimal("1000")
            agg["output_cost"] += Decimal(output_tokens) * out_rate / Decimal("1000")

        agg["input_tokens"] += input_tokens
        agg["output_tokens"] += output_tokens
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
            ":tid": tenant_id,
        }
        add_clause = ("ADD input_tokens :i, output_tokens :o, "
                      "total_tokens :t, request_count :c")
        set_clause = "SET tenant_id = :tid"

        if agg["input_cost"] or agg["output_cost"]:
            add_clause += ", input_cost :ic, output_cost :oc, total_cost :tc"
            values[":ic"] = agg["input_cost"]
            values[":oc"] = agg["output_cost"]
            values[":tc"] = agg["input_cost"] + agg["output_cost"]

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

# The aggregation layer is the SINGLE source of truth for cost, because only it
# knows which model produced each record.
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
    "input_cost, output_cost, total_cost, "
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
            "input_cost": None,
            "output_cost": None,
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
        "input_cost": row.get("input_cost"),
        "output_cost": row.get("output_cost"),
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
