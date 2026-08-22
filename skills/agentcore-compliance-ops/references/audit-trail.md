# Audit trail mechanisms for AgentCore compliance agents

`control-stack.md` settles *what* to persist and *why*: the per-invocation field table, the case-level
record for multi-agent runs, the storage split (structured queryable rows in a store with a
`BEFORE UPDATE OR DELETE` trigger, the bulky immutable bundle in S3 Object Lock, a content hash in
the row binding the two), and the epistemics — a reasoning trace is testimony, not causation. Read
that first. This file is the mechanism layer beneath it: the AWS APIs, parameter names, schemas and
hard limits that make that design real on this platform, and the ones that quietly do not.

Nothing here needs a paid third-party subscription. CloudWatch Logs, OpenTelemetry via AgentCore's
own instrumentation, X-Ray, DynamoDB, S3 and CloudTrail are sufficient — which matters, because an
audit trail whose continuity depends on a vendor contract renewing is a control with a commercial
dependency nobody wrote down.

Throughout, a claim sourced to AWS documentation is stated as documented and cited. A claim that is
inference from an API shape, from the absence of any setting, or from design reasoning is labelled as
such. Keep the distinction when you quote this file to a reviewer, because a reviewer will check it.

---

## Deployment profiles: decide what AWS is allowed to hold, before anything else

Everything below depends on a decision most teams take implicitly and discover late. It is not a
retention question. It is **which artefacts may exist in the cloud account at all.**

| | `dev` | `prod` |
|---|---|---|
| Data in play | Synthetic fixtures — fabricated customers, fabricated transactions | Real customer data |
| AWS-side capture | Full fidelity. Prompts, completions, payloads, narrative output: capture all of it, it is the cheapest debugging surface you will ever have | **Usage telemetry only** |
| Permitted in AWS | Everything | Workflow name invoked (alert triage, case summarisation, copilot question), invocation counts, tool names called, the row UUIDs read as references during fact collection, token usage, cost, latency, and the output JSON **only insofar as it is UUIDs, enums, numbers, booleans and hashes** |
| Prohibited in AWS | Nothing | Agent reasoning, prompts, completions, PII, and any narrative field that can carry PII |
| Where the examinable record lives | Anywhere. Nobody examines a synthetic run | **The firm's own store**, encrypted under a tenant-scoped key, holding the reasoning trace, the narrative output, the retrieved evidence and the outcome |

Because the profiles differ in what may exist rather than in how long it is kept, several mechanisms
in this file are **dev-only**: valuable where the PII is fabricated, prohibited where it is real.
Read the profile column before the rule.

| Rule | Profile | Why |
|---|---|---|
| §1 Bedrock model invocation logging | **dev-only** | Captures prompts and completions verbatim. In prod the rule inverts: verify it is off and keep it off |
| §2 vended delivery — `APPLICATION_LOGS` | **dev-only** | `request_payload` and `response_payload` are unredacted |
| §2 vended delivery — `USAGE_LOGS` | **prod** | Documented fields are identifiers, counters and durations. Exactly the prod shape |
| §3 Traces: retention, not sampling | both | The disqualifier — 30-day X-Ray retention — is profile-independent |
| §4 PII masking and the unmask path | both, differently | dev: the working control. prod: the backstop for leakage, never the primary control |
| §5 Retention and the two ceilings | both | Applies to whatever you do keep. In prod what you keep is metering, so the ten-year ceiling stops binding |
| §6 CloudTrail integrity validation and data events | both | The call ledger carries no payloads — and it is how you alarm on a prod account drifting toward dev |
| §7 Object Lock | both, different target | In prod it protects the firm's own evidence store, because there is no AWS-held payload copy to protect |
| §8 Span attributes | both, different force | Keeping message content off spans is advice in dev and a prohibition in prod |
| §9 Per-fact attribution | both, split | Telemetry layer (identifiers) to AWS; record layer (`claim_text`) to your own store |
| §10 Erasure reconciliation | both, different target | In prod the AWS side holds pseudonymous identifiers, so the crypto-shredding design moves to your store |
| §11 The two projections | **prod** | The mechanism that makes the prod profile enforceable rather than aspirational. **If you are building for prod, read §11 before §1** |
| §12 Encrypt for retrieval, HMAC for indexing | both | Conflating them produces a record that is tamper-evident and unreadable |
| §13 UUIDs are pseudonymous, not anonymous | **prod** | Scopes what the prod profile did and did not achieve |
| §14 The approver's identity is employee data | **prod** | A second lawful basis the customer-PII rule does not cover, and the one nobody asks about |

### The profile difference has to be structural, not conventional

This is the load-bearing point of the whole file. A dev profile that reaches production is not a
misconfiguration, it is a **mass PII disclosure**: model invocation logging is account-and-Region
scoped (§1), so a single enable captures every workflow, every tenant and every customer in that
Region until someone notices. "We set the flag correctly" is not a control. It is an assertion about
human behaviour, and the same objection this skill raises everywhere else applies here — a policy can
be re-granted, a flag can be flipped, a template can be copied from the dev stack by someone in a
hurry.

So build it so the prod configuration **cannot be created**, rather than merely being absent:

- **Separate accounts per environment**, never separate flags in one account. Sharing an account
  defeats the policy by construction, because §1's configuration is a per-account-and-Region
  singleton — there is no per-agent scoping to hide behind.
- **An SCP on the prod OU denying `bedrock:PutModelInvocationLoggingConfiguration`.** This one is
  clean: prod never wants that configuration, so a blanket deny has no collateral cost. Do **not**
  also deny `bedrock:DeleteModelInvocationLoggingConfiguration` — if the configuration somehow
  exists, you need the ability to remove it. Deny the create, keep the remediation.
  (The API operations are documented; IAM action names mirror them. Confirm the exact action strings
  against the Service Authorization Reference before the SCP ships — an SCP with a misspelled action
  denies nothing and reports no error.)
- **The delivery path needs a different shape, and it is weaker — say so.** A blanket SCP deny on
  `logs:PutDeliverySource` would also block the `USAGE_LOGS` delivery that prod *wants* (§2), and no
  documented condition key distinguishes `logType` on that call — treat a log-type condition as
  unverified until you find it in the Service Authorization Reference. What works instead: deny
  `logs:PutDeliverySource`, `logs:PutDeliveryDestination` and `logs:CreateDelivery` to every
  principal in the prod account **except one deployment role**, put the log-type restriction in that
  role's infrastructure-as-code, and add a detective control — a scheduled assertion over
  `describe-deliveries` that fails if any `APPLICATION_LOGS` delivery exists against an AgentCore
  resource ARN.
- **A deterministic gate on the data path (§11).** This is the control that does not depend on the
  absence of a configuration, because it acts where the data is produced rather than where the
  account is configured. It is also the only one of these you can unit-test in CI. If you build one
  thing from this file, build this.

Two kinds of control, deliberately: the account boundary and SCP stop the platform from capturing
anything, and the gate stops your own code from emitting anything. Each covers the other's failure
mode.

---

## The five things an examination asks for

For a regulatory examination of an AI-assisted compliance decision — alert triage, case
investigation, SAR narrative drafting — the firm must be able to reconstruct:

| | Requirement |
|---|---|
| **R1** | The reasoning trace: what the model was actually asked, and what it returned |
| **R2** | Every reference used, **including which specific customer and transaction records were read** |
| **R3** | Per-fact attribution: in a multi-agent run, a stated fact traces to the sub-agent that asserted it |
| **R4** | The outcome, and which human approved it |
| **R5** | Enough to prove the record was not altered afterwards |

Under the prod profile these five have a **split answer, and which side owns each one is now the
central architectural fact of this file.** Neither store answers an examination alone:

| | Requirement | Owned by | Mechanism |
|---|---|---|---|
| **R1** | The reasoning trace: what the model was asked, what it returned | **The firm's store, entirely** | The encrypted full record (§11, §12). AWS holds no prompt and no completion |
| **R2a** | *Which* customer and transaction records were read | **AWS-side telemetry** | `compliance.records.read` on the tool span, the row UUIDs in the metering projection (§8, §9, §11) |
| **R2b** | The *content* of those records | **The firm's store** | Retrieved evidence inside the encrypted bundle |
| **R3** | Per-fact attribution across a fan-out | **Split.** AWS holds the join surface; the firm's store holds the assertions | Span hierarchy, `tool_span_id`, the tool-call ledger in AWS; `claim_text`, `asserting_agent`, `derived_from` in the firm's store (§9) |
| **R4a** | Which human approved it | **The firm's store.** Not the metering projection — the approver's UUID and decision timestamp are, in aggregate, workforce monitoring rather than operational telemetry, and that is a second lawful basis (§14). No AWS mechanism *produces* this either way; your application writes it | The approval event in the encrypted record (§11, §14), and `control-stack.md` Layer 1 for what the record must contain. Where an aggregate is genuinely needed, compute it internally and emit only the aggregate |
| **R4b** | The outcome as reasoned | **Split.** `recommendation`, `risk_score`, `confidence` project cleanly; `rationale` and the reviewer's note do not | §11 |
| **R5** | Proof the record was not altered | **Both — and the pairing is the mechanism** | Content hash of the full record in the AWS-side metering row; the record itself under Object Lock in the firm's store; CloudTrail integrity validation over the call ledger (§6, §7, §12) |

What each AWS mechanism actually satisfies — not what it is marketed as covering:

| Mechanism | Profile | Satisfies | Does not |
|---|---|---|---|
| Bedrock model invocation logging (§1) | **dev-only** | In dev: R1 in full, R2 partly, R5 with an Object Lock destination | R3, R4 in any profile. In prod it satisfies nothing, because it must not exist |
| AgentCore `APPLICATION_LOGS` via vended delivery (§2) | **dev-only** | R1, R2 partly, the R3 join keys, R5 via an S3 destination | R3 itself — join keys are not attribution. And it carries both payloads unredacted, so prod cannot use it as shipped |
| AgentCore `USAGE_LOGS` via vended delivery (§2) | **prod** | That an invocation happened, per-session attribution, cost and utilisation | Every reconstruction requirement. It is a meter, not a record |
| Span structured logs in `aws/spans` or the agent's own group (§3, §8) | both | R2a identifiers, the R3 join surface, corroboration | Nothing authoritative. It is the corroborating half — and in prod it must carry no message content |
| X-Ray trace store (§3) | both | Debugging | Everything. 30-day retention, with no setting to change it |
| CloudTrail management + data events (§6) | both | R4a partly (who called what, when), R5 for the trail itself, and the alarm that catches profile drift | R1, R2, R3. It records the call, not the content |
| CloudWatch Logs data protection policy (§4) | dev primary, prod backstop | No requirement directly | In prod it limits the damage of a leak. That is not the same as permission to leak |
| S3 Object Lock (§7, parameters in `control-stack.md`) | both — in prod the target is the firm's evidence store | R5 | Nothing else. It protects bytes, it does not produce them — and it does not stop a delete marker hiding them |
| The two projections and the allowlist gate (§11) | **prod** | Makes the prod profile enforceable on the data path rather than by configuration convention | Nothing evidentiary itself. It decides where evidence goes; it is not evidence |
| Your own fact-level decision record (§9) | both | R1–R4 by construction; R5 paired with the hash and the WORM bundle | Nothing — but only if you build it |
| Tenant-scoped encryption versus HMAC index (§12) | both | Retrievability (encryption); tamper evidence and blind lookup (HMAC) | Each other. A hash cannot be shown to an examiner |
| Per-subject KMS key and `ScheduleKeyDeletion` (§10) | both | Erasure | Evidence. It is the reconciliation, not a record |

Rules by theme:

| Theme | Rules |
|---|---|
| Deciding what AWS may hold, and enforcing it | §11 the two projections · §1 and §2, which in prod read as prohibitions · §13 what the design did not achieve |
| Capturing what the model saw and said (**dev**) | §1 model invocation logging · §2 `APPLICATION_LOGS` |
| Usage telemetry (**prod**) | §2 `USAGE_LOGS` · §6 CloudTrail · §8 span identifiers |
| What the trace store is and is not for | §3 retention not sampling · §8 span attributes |
| Keeping the record legally holdable | §4 PII masking · §5 retention · §7 Object Lock · §10 erasure reconciliation · §12 encryption versus hashing |
| Proving it was not altered | §6 CloudTrail integrity validation · §7 Object Lock · §12 the hash pairing |
| Attribution across a fan-out | §9 per-fact attribution |

---

## The headline: no AWS mechanism makes a CloudWatch log group immutable

There is no Object Lock for log groups. There is no WORM mode, no compliance-mode retention, no
per-event digest chain. The strongest control available is a combination of policy:

- a resource policy on the log group, plus an SCP denying `logs:DeleteLogGroup`,
  `logs:DeleteLogStream`, `logs:PutRetentionPolicy` and `logs:DeleteDataProtectionPolicy` to
  everything except one break-glass role;
- CloudTrail management events on those calls, with an alarm.

That is policy, and **policy can be re-granted.** It is precisely the objection `control-stack.md`
already raises against approximating an append-only store with DynamoDB plus an IAM deny: a future
change can silently re-add a grant, but cannot silently remove a database trigger or unlock a
protected object version. An SCP is a stronger version of the same weak thing.

So a log group is never the tamper-evident record. **It is a feeder into one.** Every mechanism in
§1–§4 produces evidence that must be landed somewhere durable to count, and the durable place is
the structured row plus the WORM bundle that `control-stack.md` already specifies. That is not a
concession — it is the argument for the storage split, made from the mechanism side. If a log group
could be made immutable, the split would be optional. It cannot, so it is not.

Two practical consequences. First, when a control review asks "is the agent log group immutable",
the correct answer is *no, and it is not supposed to be* — followed by the retention, delivery and
hashing configuration that makes the derived record immutable. Second, every log group you do keep
needs a retention period that outlives the window in which you extract from it, and a delivery to
S3 configured **before** the first production invocation, because none of these signals backfill.

**The prod profile improves this position rather than only constraining it, and that is worth saying
out loud in a design review.** When AWS held a full payload copy, the weakest link in the tamper-
evidence story was a mutable log group full of evidence. Under the prod profile the log group holds
metering, and the artefact that needs tamper evidence lives in the firm's own store — where a
`BEFORE UPDATE OR DELETE` trigger and an Object Lock retention period are both available and neither
is a re-grantable policy. The policy that looks like a restriction on capture is also a structural
upgrade to R5.

---

## 1. Bedrock model invocation logging captures verbatim prompts — configure it in dev, prove it is off in prod

**Symptom (dev):** an examiner — or, more usefully, a prompt-regression investigation — asks what
prompt produced a given output. The application logged the rendered template it *intended* to send.
Nobody can show what actually crossed the wire, and if the application post-processed the response
before logging it, nobody can show what came back either.

**Symptom (prod):** someone enabled it in a shared account to debug a throttling problem. Six months
later a Region-wide log group holds every prompt every tenant's agents have sent — full customer
names, account numbers and transaction narratives — under no classification, no data protection
policy, no per-tenant key and no retention anyone chose. Nobody enabled it maliciously and nobody
enabled it for this workload; the configuration is account-wide, so one person's debugging decision
captured everyone.

**Profile:** **dev-only to enable.** In prod the required state is that *no configuration exists*, and
this rule is how to verify that and keep it that way.

**Satisfies:** in dev, R1 in full, R2 partly, R5 when the destination is an Object Lock bucket. In
prod it satisfies nothing, because its absence is the control — §11 supplies the record it would
otherwise have given you, on the firm's own side of the boundary.

Bedrock has a first-party invocation log, off by default, configured per account and Region:

- `PutModelInvocationLoggingConfiguration`
- `GetModelInvocationLoggingConfiguration`
- `DeleteModelInvocationLoggingConfiguration`

It covers `Converse`, `ConverseStream`, `InvokeModel` and `InvokeModelWithResponseStream` — but
only **on the `bedrock-runtime` endpoint.** AWS states the limit explicitly: the same APIs called
on other endpoints, `bedrock-mantle` named directly, "are not currently captured by invocation
logging." And the configuration is **account-and-Region scoped, not per-agent** — one singleton
configuration governs every agent, every workflow and every tenant in that Region. That single
property is why the two profiles need two accounts and not two config values: there is no
per-agent, per-workflow or per-tenant scope to enable it narrowly for the one thing you are
debugging.

### Prod: the required state is empty, and empty has to be asserted

There is no "logging is off" resource to inspect, so the check is the absence of a response:

```bash
aws bedrock get-model-invocation-logging-configuration
```

An empty response is the desired prod state. Three things to build around it:

- **Assert it in deployment verification**, as a check that fails the deploy — not as a runbook step.
  The assertion is cheap, it runs per Region, and it is the only way the desired state gets
  re-checked after the fiftieth unrelated change to the stack.
- **Alarm on the create call.** `PutModelInvocationLoggingConfiguration` is a Bedrock control-plane
  operation, and AWS documents that "Amazon Bedrock logs the remainder of Amazon Bedrock API
  operations as management events" and that CloudTrail "logs management event API operations by
  default" — so the event is already in your trail with no data-event selector and no additional
  charge. Match it and page someone:

  ```json
  {
    "source": ["aws.bedrock"],
    "detail-type": ["AWS API Call via CloudTrail"],
    "detail": {
      "eventSource": ["bedrock.amazonaws.com"],
      "eventName": ["PutModelInvocationLoggingConfiguration"]
    }
  }
  ```

  Treat a firing as an incident with a disclosure assessment attached, not as a config-drift ticket:
  between the call and the remediation, real prompts were written to a store outside the design.
- **Deny the call in the prod OU** so the alarm is a backstop rather than the control. The SCP shape,
  and why the deny covers `Put` but not `Delete`, is in the deployment-profile section above.

If the diagnostic returns a configuration in a prod account, the response order is: disable it,
then determine the window it was active, then treat every case decided in that window as having
prompt data in an unclassified store. That is a disclosure question and a records question at the
same time, and the second one is the one people forget — the same window's cases now have evidence
in two places with different retention.

### Dev: configure it fully, because with synthetic fixtures it is the best tool here

Everything from here is dev configuration guidance, with the prod reading noted where the same fact
cuts both ways. With fabricated customers there is no minimisation argument against verbatim capture,
and this is the cheapest way to see exactly what the model was asked — including the resolved
inference parameters, which no application-side log reliably records. It is genuinely the best tool in
this file for prompt-regression work, which is why the prod prohibition needs the dev profile to be
generous: take the capability away everywhere and people rebuild a worse version of it by hand, in
prod, in application code.

```bash
aws bedrock put-model-invocation-logging-configuration \
  --logging-config file://logging-config.json
```

```json
{
  "textDataDeliveryEnabled": true,
  "imageDataDeliveryEnabled": false,
  "embeddingDataDeliveryEnabled": false,
  "videoDataDeliveryEnabled": false,
  "s3Config": {
    "bucketName": "acme-compliance-model-invocations",
    "keyPrefix": "bedrock/invocations/"
  },
  "cloudWatchConfig": {
    "logGroupName": "/aws/bedrock/modelinvocations",
    "roleArn": "arn:aws:iam::111122223333:role/BedrockModelInvocationLogging",
    "largeDataDeliveryS3Config": {
      "bucketName": "acme-compliance-model-invocations",
      "keyPrefix": "bedrock/large-data/"
    }
  }
}
```

The log entry schema — the field names you will actually write queries and extractors against:

| Field | Notes |
|---|---|
| `schemaType` | Always `ModelInvocationLog` |
| `schemaVersion` | Pin it in your extractor; a schema change is a parsing change |
| `timestamp`, `accountId`, `region` | |
| `requestId` | **The join key.** Correlates to your application record and to the trace |
| `operation` | Which of the four runtime operations |
| `modelId` | Satisfies the model-ID row of the `control-stack.md` audit table from the platform's own side |
| `identity.arn` | Which principal called — the execution role, not the end user |
| `requestMetadata` | The only caller-supplied field. See below |
| `input.inputContentType`, `input.inputBodyJson`, `input.inputTokenCount` | The prompt as sent, including the resolved inference parameters |
| `output.outputContentType`, `output.outputBodyJson`, `output.outputTokenCount` | The raw response, before your post-processing |

**Trap — `requestMetadata` is the only field you control, and without it an entry cannot be tied to
a case.** Everything else is platform-generated. An entry with no metadata is a prompt and a
response floating in a Region-wide log with a `requestId` you can only join if your application
happened to record it. Stamp the identifiers that make the entry evidence:

```json
{"case_id": "C-2026-0184", "alert_id": "A-99311", "workflow": "alert-triage",
 "prompt_version": "v7", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}
```

On `Converse` and `ConverseStream` this is a `requestMetadata` request field. On `InvokeModel` and
`InvokeModelWithResponseStream` it travels as the `X-Amzn-Bedrock-Request-Metadata` header, whose
value is a JSON object of string keys to string values, and **the header must be included in the
`SignedHeaders` list or the request is rejected with `InvalidSignatureException`.** That is a
signing error, so the first instinct is to look at credentials rather than at the header just added;
SDKs that expose request metadata as a parameter handle the signing for you, which is a reason to
prefer the parameter over a raw-header escape hatch that appends after signing.

Budget the keys before designing the join: **at most 16 entries per request, keys and values at
most 256 characters**, over a restricted character set — key pattern
`[a-zA-Z0-9\s:_@$#=/+,-.]{1,256}`, values the same with a minimum length of 0. Exceeding any of
those rejects the request with a validation error, so metadata cannot absorb a whole evidence
manifest. Sixteen slots is generous for identifiers and hopeless for content, which is the correct
shape: identifiers here, content in the bundle.

**Trap — bodies inline to 100 KB.** Input and output JSON bodies up to 100 KB appear inline. Binary
data and bodies larger than 100 KB are uploaded as individual objects under the destination's data
prefix instead, and the log entry carries a reference to that S3 location. Two consequences worth
separating: with an S3 destination the large body still exists, one dereference away; with a
CloudWatch-only destination **and no `largeDataDeliveryS3Config`, only S3 is supported for that
data, so it is not delivered at all** — the entry keeps its metadata and the payload is simply
absent. The cases that overflow are the long ones, the multi-account investigation with fifty
transactions in the prompt, which are exactly the cases an examiner selects. For an alert-triage
workload the S3 destination is not optional. Read the same fact from the prod side and it argues for
the prohibition: overflow bodies land as *individual objects* under a bucket prefix, so an accidental
enable in prod produces not one log group to classify but a prefix full of unclassified prompt
objects, each one a separate thing to find, assess and delete.

**Trap — one configuration per Region, with no per-tenant key.** There is a single destination pair
for the whole account and Region. If per-tenant or per-subject key separation is part of your
erasure story (§10), this store sits outside it — which is not a footnote but the reason the prod
profile switches the mechanism off rather than configuring it carefully. In dev the same fact is
harmless: there is no subject to erase.

**In prod, `requestMetadata` is not a mitigation.** Stamping case identifiers on an entry makes the
entry findable; it does not make the prompt beside it any less of a prompt. And with logging off the
metadata has nowhere to land, so the join key prod relies on is the `requestId` your own code reads
from the API response and writes to its own record (§9, §11) — available on every call regardless of
this configuration.

**It is not the decision record.** It records what was sent and what came back. It does not record
which evidence the model was *supposed* to have, which deterministic validation ran
(`control-stack.md` Layer 5), which reference-data version applied, or who approved the outcome. It is
the strongest possible corroboration of R1 and a poor substitute for R1–R5. Under the prod profile
you give up that corroboration deliberately and reconstruct R1 from the firm's own encrypted record
instead — which is a weaker *independent* attestation and a stronger privacy position, and the
trade is the policy, not an oversight. Record it as an accepted limitation with that reasoning, so a
reviewer reads a decision rather than a gap.

**Diagnostic:** `aws bedrock get-model-invocation-logging-configuration`, in every Region you invoke
in — the configuration is per Region, so a single-Region check proves one Region. The API defines no
`ResourceNotFoundException` and returns `loggingConfig` as a non-required element, so an empty
response is how "never configured" presents; that reading is inference from the API shape rather
than documented behaviour, but the operational conclusion is not. Read the same empty response two
ways depending on profile: **in prod it is the passing assertion**, and in dev it means no model call
in this Region has ever been logged, including every call already made. There is no retrospective
capture, so a dev gap is permanent and — if a case was ever decided against non-synthetic data in
that account — belongs in the case file as a finding rather than in a backlog as a task.

Sources:
[Monitor model invocation using CloudWatch Logs and S3](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html),
[Track invocations with request metadata](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-request-metadata.html),
[PutModelInvocationLoggingConfiguration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_PutModelInvocationLoggingConfiguration.html),
[DeleteModelInvocationLoggingConfiguration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_DeleteModelInvocationLoggingConfiguration.html),
[Bedrock management events in CloudTrail](https://docs.aws.amazon.com/bedrock/latest/userguide/logging-using-cloudtrail.html),
[Amazon Bedrock events delivered via CloudTrail to EventBridge](https://docs.aws.amazon.com/eventbridge/latest/ref/events-ref-bedrock.html),
[Converse](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html).

---

## 2. AgentCore vended log delivery: `USAGE_LOGS` is the prod signal, `APPLICATION_LOGS` is dev-only

**Symptom (dev):** the traces show a tool call happened and how long it took. The prompt and the tool
result are nowhere, because nobody configured a delivery and the console view was mistaken for
storage.

**Symptom (prod):** the delivery was created because the team wanted the join keys — `request_id`,
`trace_id`, `session_id` in one place — and `request_payload` and `response_payload` came with them,
because they are fields of the same record. The intent was correlation; the effect was a payload
store.

**Profile:** `USAGE_LOGS` is the prod-appropriate signal. `APPLICATION_LOGS` is **dev-only** as
shipped, for the reason in the second symptom.

**Satisfies:** `APPLICATION_LOGS` in dev — R1, R2 partly, the R3 join keys, R5 via an S3 destination.
`USAGE_LOGS` in prod — that an invocation happened, per-session attribution, cost and utilisation.
Nothing reconstructive.

AgentCore emits three signal types you can route yourself, through CloudWatch vended log delivery.
Take them in the order the prod profile cares about.

### `USAGE_LOGS` — the shape prod wants

Its documented fields are identifiers, counters and durations, with nothing free-text in them:

`event_timestamp`, `resource_arn`, `service.name`, `cloud.provider`, `cloud.region`, `account.id`,
`region`, `resource.id`, `session.id`, `agent.name`, `elapsed_time_seconds`,
`agent.runtime.vcpu.hours.used`, `agent.runtime.memory.gb_hours.used`

At one-second granularity, this is a cost and utilisation signal rather than evidence — and it is the
cleanest per-tenant attribution surface AWS gives you, provided `session.id` is tenant-namespaced
(`production-rules.md` §7). Under the prod profile it is promoted from a cost feed to one of the two
AWS-side signals you actually rely on, alongside your own emitted metering events (§11).

Two caveats, both load-bearing now that this is a primary signal rather than a nice-to-have. Usage
data **may lag by up to 60 minutes**, so it cannot be the thing an operator watches in real time.
And `USAGE_LOGS` exists only for **Runtime and Tools** — Identity, Memory, Gateway and Payments offer
`APPLICATION_LOGS` and `TRACES` only. That second fact has a sharper consequence under this policy
than it did as a costing footnote: for four of the primitives there is no usage stream at all and the
payload-bearing stream is prohibited, so **your own metering events are the primary meter and
`USAGE_LOGS` corroborates it** — not the other way round. A prod design that treats `USAGE_LOGS` as
the source of truth is blind on Gateway and Memory.

### `APPLICATION_LOGS` — what it carries, and why prod cannot take it as shipped

One structured record carrying every join key **and both payloads**:

`timestamp`, `resource_arn`, `event_timestamp`, `account_id`, `request_id`, `session_id`,
`trace_id`, `span_id`, `service_name`, `operation`, `request_payload`, `response_payload`

That single schema is the join surface for the whole evidence set: `request_id` to Bedrock model
invocation logging (§1) and to CloudTrail (§6), `trace_id` and `span_id` to the spans (§8),
`session_id` to the runtime session, `resource_arn` to the agent. In dev, deliver it and use it.

In prod, the options in order of preference:

**1. Do not create the `APPLICATION_LOGS` delivery at all.** Rely on `USAGE_LOGS` plus your own
emitted metering events (§11). What you give up is the platform's independent copy of the join keys —
but the keys themselves are not lost: your gate already emits `request_id`, `trace_id`, `span_id` and
`session_id` into the metering projection, because those are identifiers and identifiers are exactly
what it is allowed to emit. The loss is *independence of attestation*, not the ability to join. Name
that loss in the design record and move on; it is the same trade §1 makes.

**2. If you want the platform's copy of the join keys, check whether the payload fields can be
excluded — and verify it in your own account, because the answer is not documented per service.**
The mechanism exists and is documented generically: `CreateDelivery` and `UpdateDeliveryConfiguration`
take a `recordFields` list — "the list of record fields to be delivered to the destination, in order.
If the delivery's log source has mandatory fields, they must be included in this list" — up to 128
entries. What a caller is allowed to put there is service-specific and discoverable:
`DescribeConfigurationTemplates` returns `allowedFields`, "the allowed fields that a caller can use in
the `recordFields` parameter", per `service`, `logType`, `resourceType` and `deliveryDestinationType`.

**Not verifiable from AWS documentation, and stated as such rather than guessed: whether AgentCore's
`APPLICATION_LOGS` configuration template lists `request_payload` and `response_payload` among
`allowedFields`, or treats them as mandatory fields that `recordFields` must include.** Per-service
field lists are not published. Check it, per Region, before designing around it:

```bash
aws logs describe-configuration-templates \
  --service bedrock-agentcore --log-types APPLICATION_LOGS
```

Read `allowedFields` and `defaultDeliveryConfigValues`. If the payload fields are omittable, a
delivery whose `recordFields` names only identifiers is a **structural** control — the payload cannot
arrive, because the delivery has no field for it — which is the kind of control this file argues for
everywhere. Make it a deployment-verification assertion rather than a one-time check: an allowed-field
list is a service-side property and can change under you, and a `recordFields` list that silently
gained a field is exactly the drift nobody looks for. If the payload fields turn out to be mandatory,
option 1 is the only compliant answer.

**3. Never: create the delivery with payloads and rely on a data protection policy to make it safe.**
§4 explains why — keyword-proximity detection over narrative prose does not establish absence, and
describing it as though it does converts a good control into a misrepresentation.

### Creating a delivery

The standard CloudWatch Logs vended-delivery triple. One prerequisite is easy to miss, and it is a
**permission**, not an API call: the caller needs an `AllowVendedLogDeliveryForResource` action for
the service that owns the resource — documented as
`bedrock-agentcore:AllowVendedLogDeliveryForResource` in AgentCore's own observability IAM policy —
alongside `logs:PutDeliverySource`, `logs:PutDeliveryDestination` and `logs:CreateDelivery`. If you
are unsure of the action string for a given resource type,
`DescribeConfigurationTemplates` returns it directly in
`allowedActionForAllowVendedLogsDeliveryForResource`, described as "the action permissions that a
caller needs to have to be able to successfully create a delivery source on the desired resource type
when calling `PutDeliverySource`". There is no separate opt-in call to make; a missing permission
presents as an access-denied on `PutDeliverySource`, which is easy to misread as a Logs problem.

```python
import boto3

logs = boto3.client("logs")

# Prod: the usage stream, not the payload stream.
logs.put_delivery_source(
    name="agentcore-triage-usage",
    logType="USAGE_LOGS",                # APPLICATION_LOGS | TRACES | USAGE_LOGS
    resourceArn=agent_runtime_arn,
)

dest = logs.put_delivery_destination(
    name="compliance-metering-bucket",
    deliveryDestinationType="S3",        # S3 | CWL | FH | XRAY
    deliveryDestinationConfiguration={"destinationResourceArn": metering_bucket_arn},
)

logs.create_delivery(
    deliverySourceName="agentcore-triage-usage",
    deliveryDestinationArn=dest["deliveryDestination"]["arn"],
    # Where the log type permits it, pin the field list rather than accepting the default —
    # an explicit allowlist here is the same argument as the projection gate in §11.
    # recordFields=[...],  # verify against describe-configuration-templates first
)
```

In dev, an S3 destination is what brings the payloads inside the reach of Object Lock (§7),
lifecycle, per-subject KMS keys (§10) and Macie (§4) — none of which can touch a log group.

**Trap — configure delivery before the first production invocation.** These signals do not
backfill. CloudWatch Logs rejects any event older than 14 days or preceding the log group's
retention period, so there is no mechanism, for you or for AWS Support, that puts last month's
payloads into a store created today.

**Trap — the span destination has three prerequisites and a cutover date.**
`UNIFIED_TRACES_DESTINATION_ENABLED=true` delivers spans into the agent's own log group
(`/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`, stream `spans`) instead of the shared
`aws/spans` group. For a regulated deployment that is a material win: one KMS key, one access scope,
one retention setting and one export path *per agent*, rather than every agent in the account
sharing one group. It needs Transaction Search enabled with segments going to CloudWatch Logs (§3),
`logs:PutResourcePolicy` on the agent's log group granted to the execution role
(`production-rules.md` §6), and `aws-opentelemetry-distro>=0.18.0` — **earlier ADOT versions ignore
the span-destination configuration and deliver to `aws/spans` with no error**, so the failure is a
correct-looking configuration and spans in the wrong place.

Two dating facts make this a fleet problem rather than a setting. **Changing the destination does not
move existing span data** — spans AgentCore already delivered stay in their original log group — so a
mid-life switch splits one agent's evidence across two groups with different retention and different
access control. And from **20 July 2026** newly created agents in supported Regions default to the
agent's own log group, while agents created earlier keep `aws/spans` unless opted in. A fleet
spanning that date therefore has two span layouts in it, exactly as it has two IAM shapes across the
13 October 2025 service-linked-role change (`production-rules.md` §2), and an extraction script that
assumes either one will silently return nothing for half the fleet.

**Trap — `request_payload` and `response_payload` are unredacted.** That is exactly why the record
is useful in dev and exactly why it is prohibited in prod: in a compliance workload those fields hold
customer names, account numbers and transaction detail in the clear. In dev, §4 is not optional
alongside this rule — it is the other half of it. In prod, §4 is the backstop and §11 is the control;
if you find yourself relying on masking to make this delivery acceptable, you have chosen option 3
above without deciding to.

**Diagnostic:** `aws logs describe-delivery-sources` and `aws logs describe-deliveries`, and read the
result against the profile. **In dev**, no `APPLICATION_LOGS` delivery means the payloads were never
persisted anywhere you control, whatever the GenAI Observability console renders — the console reads
live signals; it is not an assertion that anything was stored to your policy. **In prod**, an
`APPLICATION_LOGS` delivery against an AgentCore resource ARN is a finding, and one whose
`recordFields` includes `request_payload` or `response_payload` is an active leak — check the field
list on the delivery, not just its existence, because `describe-deliveries` returns `recordFields` and
a delivery that looks approved can have been widened.

Sources:
[AgentCore Runtime observability metrics and log schemas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html),
[Configure AgentCore observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html),
[AgentCore observability IAM permissions, including `bedrock-agentcore:AllowVendedLogDeliveryForResource`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/payments-observability.html),
[PutDeliverySource](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutDeliverySource.html),
[PutDeliveryDestination](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutDeliveryDestination.html),
[CreateDelivery — `recordFields` and mandatory fields](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_CreateDelivery.html),
[DescribeConfigurationTemplates](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_DescribeConfigurationTemplates.html),
[ConfigurationTemplate — `allowedFields`, `allowedActionForAllowVendedLogsDeliveryForResource`](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_ConfigurationTemplate.html),
[PutLogEvents](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutLogEvents.html).

---

## 3. The reason traces are not the case record is retention, not sampling

**Symptom:** a design review dismisses the trace store wholesale — "traces are sampled, so they are
not evidence" — and the team stops looking at a store that in fact holds every span. Or the
opposite: a team relies on traces as the reasoning record and discovers at month thirteen that
nothing older than thirty days exists.

**Satisfies (span logs):** R2a identifiers, the R3 join surface, corroboration. Nothing
authoritative.

**Profile:** both. The disqualifier below — 30-day X-Ray retention — is profile-independent. What
changes in prod is the *content* of the spans being ingested: identifiers only, never message content
(§8). That makes the span log group the one telemetry store the prod profile keeps at long retention,
and the one most likely to acquire prose by accident from an instrumentation default.

Correct the mis-argument first, because it costs a usable evidence source. **With Transaction
Search enabled, all spans are ingested as structured logs.** AWS is explicit that the sampling
applies to the *index*, not the ingestion: Transaction Search "switches all spans ingestion through
X-Ray into cost effective collection mode", and "by default you will also index 1% of ingested spans
for free as trace summary for analysis, which is typically sufficient given you already have full
end-to-end trace visibility on all ingested spans". The percentage is tunable from 0 to 100:

```bash
aws xray update-indexing-rule --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 100}}'
```

So "sampled" is true of the index and false of the span logs. The claim in `control-stack.md` that
sampled telemetry is not an audit trail stands — it just does not apply to the layer people assume.

**The real disqualifier is a hard number: X-Ray trace data is retained for 30 days.** AWS documents
the period flatly — "Trace data is retained for 30 days", the same for service-graph data — and
offers no API, console field or CloudFormation property to change it. (Treat "fixed" as inferred
from the absence of any setting rather than from a sentence saying so; the operational consequence
is identical.) Against a five-to-ten-year AML record-keeping obligation, that single fact ends the
argument without needing any of the schema, sampling or availability reasoning. Thirty days is not a
shortfall to mitigate; it is a different category of system.

The distinction that makes this useful rather than merely disqualifying: **the trace store's 30 days
are not yours, and the span log group's retention is.** Once Transaction Search routes segments into
CloudWatch Logs, those spans live under your retention setting (§5), your KMS key and your data
protection policy (§4). Same spans, two stores, two entirely different governance regimes — and
teams that hear "X-Ray keeps 30 days" often discard both.

Three stores, three jobs. Confusing them is the most common structural error in this area:

| Store | Coverage | Retention | Role |
|---|---|---|---|
| X-Ray trace store | Summary index sampled (1% default) | **30 days, no retention setting** | Debugging. Never evidence |
| Span structured logs — `aws/spans`, or the agent's own group (§2) | All ingested spans | Yours, up to 3653 days (§5) | Corroborating evidence and the join surface |
| Your own decision and fact record (§9) | Complete by construction | Your policy | Authoritative |

Enabling Transaction Search is three steps, and the first is the one people miss:

```bash
# 1. Let X-Ray write spans into your log groups.
aws logs put-resource-policy \
  --policy-name TransactionSearchAccess \
  --policy-document file://xray-logs-policy.json

# 2. Send segments to CloudWatch Logs.
aws xray update-trace-segment-destination --destination CloudWatchLogs

# 3. Optional: raise the summary-index sampling percentage.
aws xray update-indexing-rule --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 100}}'
```

The resource policy grants `xray.amazonaws.com` the `logs:PutLogEvents` action on `aws/spans` and
`/aws/application-signals/data`, with `ArnLike` on `aws:SourceArn` and `StringEquals` on
`aws:SourceAccount` so the grant is confused-deputy safe. Note the log group names are **asymmetric**
— `aws/spans` has no leading slash, `/aws/application-signals/data` does — and a policy with the
slash normalised onto both is a policy that grants nothing on the group that matters. Without the
policy, step 2 still succeeds and no spans arrive: the same absence-of-data failure as
`production-rules.md` §6, and equally silent.

**Diagnostic:** `aws xray get-trace-segment-destination` should return
`{"Destination": "CloudWatchLogs", "Status": "ACTIVE"}`. Allow up to ten minutes before spans become
available, so an empty query immediately after enablement proves nothing either way.

Sources:
[CloudWatch Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html),
[Enable Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html),
[X-Ray concepts — trace data is retained for 30 days](https://docs.aws.amazon.com/xray/latest/devguide/xray-concepts.html),
[AWS::XRay::TransactionSearchConfig](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-xray-transactionsearchconfig.html).

---

## 4. Mask PII in agent log groups at ingestion, and keep one named unmask path

**Symptom:** the agent works, the payload logs are complete, and a privacy review discovers that
every customer name and account number in the portfolio is sitting in the clear in a log group with
`logs:GetLogEvents` granted broadly. Or the inverse: masking is switched on, described to a
regulator as proof PII is absent, and a spot check finds unmasked names throughout the free-text
narrative fields.

**Satisfies:** no requirement directly. It is what makes the payload-bearing stores keepable.

**Profile:** both, with different jobs, and confusing them is the most consequential mistake in this
file. **In dev** this is the working control: fixtures are synthetic but realistic, and masking keeps
a habit of minimisation in place where it costs nothing to practise. **In prod the primary control is
not putting the data there at all** (§11) — which is stronger than any masking, because a value that
was never emitted cannot be unmasked, cannot be exported by a misconfigured subscription filter and
does not need a lawful reader. Masking in prod is **defence in depth against leakage you have not
found yet.**

And something will leak, eventually. Not through the designed path — through a stack trace carrying a
customer name in an exception message, an error log echoing a memo field, a third-party library
logging its own request body at DEBUG, a `logger.info(output)` added at 2am during an incident and
never removed. Plan for the leak as a certainty on a long enough timeline and this rule is
proportionate. Treat masking as the reason PII in prod logs is acceptable and you have mis-stated your
own architecture to yourself: a review that describes the masking policy as the prod PII control has
described the backstop and omitted the control.

A CloudWatch Logs data protection policy detects sensitive data at ingestion and masks it in the
default read path, while a named role can read the original. That combination — minimisation by
default plus an auditable examiner path — is the only mechanism here that delivers both.

```json
{
  "Name": "agentcore-compliance-pii",
  "Description": "Mask customer identifiers in agent payload logs",
  "Version": "2021-06-01",
  "Statement": [
    {
      "Sid": "audit-findings",
      "DataIdentifier": [
        "arn:aws:dataprotection::aws:data-identifier/Name",
        "arn:aws:dataprotection::aws:data-identifier/Address",
        "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
        "arn:aws:dataprotection::aws:data-identifier/Ssn-US",
        "arn:aws:dataprotection::aws:data-identifier/BankAccountNumber-US",
        "arn:aws:dataprotection::aws:data-identifier/BankAccountNumber-GB",
        "arn:aws:dataprotection::aws:data-identifier/CreditCardNumber",
        "arn:aws:dataprotection::aws:data-identifier/DriversLicense-US"
      ],
      "Operation": {
        "Audit": {
          "FindingsDestination": {
            "S3": { "Bucket": "acme-compliance-dp-findings" }
          }
        }
      }
    },
    {
      "Sid": "deidentify",
      "DataIdentifier": [
        "arn:aws:dataprotection::aws:data-identifier/Name",
        "arn:aws:dataprotection::aws:data-identifier/Address",
        "arn:aws:dataprotection::aws:data-identifier/EmailAddress",
        "arn:aws:dataprotection::aws:data-identifier/Ssn-US",
        "arn:aws:dataprotection::aws:data-identifier/BankAccountNumber-US",
        "arn:aws:dataprotection::aws:data-identifier/BankAccountNumber-GB",
        "arn:aws:dataprotection::aws:data-identifier/CreditCardNumber",
        "arn:aws:dataprotection::aws:data-identifier/DriversLicense-US"
      ],
      "Operation": { "Deidentify": { "MaskConfig": {} } }
    }
  ]
}
```

```bash
aws logs put-data-protection-policy \
  --log-group-identifier /aws/bedrock-agentcore/runtimes/<agent-id>-<endpoint> \
  --policy-document file://data-protection-policy.json
```

Three structural details that break the policy if you get them wrong. The first statement performs
the **detection**, and its `FindingsDestination` must already exist — log group, Firehose stream or
S3 bucket, created first. The second performs the masking, and `MaskConfig` **must be the empty
object**; `{}` is what masks. And **the two `DataIdentifier` arrays must match exactly** — AWS states
it twice, in those words — because a term audited but not deidentified is detected and left in the
clear, producing findings reports that read as coverage. (The AWS prose spells the key
`DataIdentifer` in places; the working key is `DataIdentifier`, as in the examples. Do not "fix" your
policy to match the typo.)

**Trap — the policy is not retroactive.** It applies to events ingested after it exists. Events
already in the group stay in the clear for the life of the group, and no operation fixes it. Create
the policy **with the log group, before the first invocation.**

**Trap — enumerate the groups, and let the list differ by profile.** In dev there are four that hold
payloads: the agent's own log group; `aws/spans` or the agent's `spans` stream (§2); the
vended-delivery destination; and the model-invocation-logging group (§1). A policy on one of four is a
control that exists in the architecture diagram. In prod the first two of those still exist and the
last two should not, which makes the enumeration *shorter and less reliable* — the groups that matter
in prod are the ones nobody enumerated, because they were created by something other than the agent
stack: a Lambda's default group, an ECS task's group, a step function's, an SDK's retry logger. This
is precisely why the account-level policy stops being a nice-to-have. Add the backstop with
`PutAccountPolicy` (`policyType` `DATA_PROTECTION_POLICY`, which needs
both `logs:PutAccountPolicy` and `logs:PutDataProtectionPolicy`, and only **one** such policy exists
per account): account-level and log-group-level policies are **cumulative**, so any term named in
either is masked and the backstop cannot be weakened by a narrower group policy. Applying it to
existing groups is eventually consistent — up to five minutes before masking begins — which is a
deployment-ordering detail, not a retroactive fix. One account-level policy is the only control in
this file that covers a log group nobody remembered, and under the prod profile that is the category
of log group that matters. Set it at account bootstrap, before any workload lands, and treat its
presence as a deployment-verification assertion like §1's.

**Trap — the unmask path is a named role, not a permission you sprinkle.** Grant `logs:Unmask` to
exactly one break-glass role, require it for the examiner extraction, and alarm on its use. An
unmask nobody notices is the same control failure as an unmonitored bypass in §7. Know the two
mechanics, because they are different APIs: `GetLogEvents`, `FilterLogEvents`, `GetLogRecord` and
`GetLogObject` take an `unmask` request parameter, while **`StartQuery` has no such parameter** —
in Logs Insights, unmasking is a query command, `fields @timestamp, unmask(@message)`, which also
requires `logs:Unmask`. A runbook written against the wrong one reads masked data and concludes the
examiner extraction is impossible.

**Trap — do not put these groups on the Infrequent Access log class.** Masking itself works on IA;
what IA removes is every way to reverse it. The `unmask` query command is unsupported there (along
with `pattern`, `diff` and `filterIndex`), anomaly detection is unsupported, field indexing is
unsupported — and the `GetLogEvents` and `FilterLogEvents` API operations are unsupported entirely,
which is where the other `unmask` parameter lives. Choosing IA to save money on payload logs
produces a store that is masked with no lawful reader, which is the worst of both obligations.

**Never describe masking to a regulator as proof PII is absent.** CloudWatch Logs describes detection
as pattern matching and machine-learning models, and says outright that "for some types of managed
data identifiers, the detection depends on also finding certain keywords in proximity with the
sensitive data." It does not publish the distance; Macie's managed-data-identifier documentation,
which covers the same identifier family, puts it at typically within 30 characters. `CreditCardNumber`
is stricter still, and this one *is* documented in the CloudWatch Logs identifier reference: detection
requires a 13-19 digit sequence that passes the Luhn check and carries a recognised issuer prefix,
plus the supporting keywords. Free-form narrative prose — the exact shape of a SAR draft or an analyst
note — defeats keyword-proximity detection routinely. Masking reduces exposure; it does not establish
absence, and claiming otherwise converts a good control into a misrepresentation.

**Masking is a view control.** The bytes remain in the log group; the mask is applied on read. It
therefore does **not** discharge an erasure obligation. §10 is where erasure is actually addressed.

Ceilings worth knowing before designing around this: one data protection policy per log group; one
`Audit` statement per policy; at most 10 custom data identifiers; each custom regex at most 200
characters; the whole `policyDocument` at most 30,720 characters.

**Diagnostic:** `aws logs get-data-protection-policy` to confirm the policy is attached, then read
the audit findings report at the `FindingsDestination`. It names which identifiers fired and how
often, which is the closest thing available to a PII inventory of your agent logs — and the fastest
way to discover that a field you assumed was structured is arriving as prose.

**Macie cannot help here.** It scans S3 objects and bucket configuration. Payloads living in
CloudWatch Logs are entirely outside its reach; landing them in S3 through vended delivery (§2) is
what brings Macie into scope at all.

Sources:
[Help protect sensitive log data with masking](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/mask-sensitive-log-data.html),
[Financial data identifiers](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/protect-sensitive-log-data-types-financial.html),
[PII data identifiers](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/protect-sensitive-log-data-types-pii.html),
[PutDataProtectionPolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutDataProtectionPolicy.html),
[PutAccountPolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutAccountPolicy.html),
[Query command support by log class](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_AnalyzeLogData_Classes.html),
[Macie keyword requirements](https://docs.aws.amazon.com/macie/latest/user/managed-data-identifiers-keywords.html).

---

## 5. Set retention from the obligation, and know the two ceilings

**Symptom:** the log group holding the only copy of a year of agent payloads is on the default
retention nobody chose. Or the reverse — everything is set to never expire, and the privacy review
now has an unbounded PII store to explain.

**Satisfies:** nothing on its own. It decides whether R1–R5 still exist in year five, which is when
they are asked for.

**Profile:** both — what differs is *what* is being retained, and therefore which ceiling binds. When
the log group held the evidence, the 3653-day ceiling was a live problem against a five-to-ten-year
AML obligation. When it holds metering, the long-retention obligation moves to the firm's own store,
where it is a database-and-lifecycle question rather than a CloudWatch one. The batch and event-size
ceilings below still bind on the metering path, and the no-backfill rule binds on both.

Valid `retentionInDays` values are a fixed enumeration, not any integer — 22 of them:

**1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922,
3288, 3653**

Check that list against whatever helper validates it in your stack, because the **17-value
pre-2023 set** — the same list without 1096, 2192, 2557, 2922 and 3288 — still circulates in stale
SDK documentation. A validator built from it rejects a lawful seven-year retention (2557) as an
invalid value, and the failure looks like an API error rather than a stale constant.

`retentionInDays` is **required** on `PutRetentionPolicy`; there is no "unset" value to pass. A log
group with no retention policy keeps events **indefinitely**, and `DeleteRetentionPolicy` is what
returns it to that state. There is nothing between 3653 days (ten years) and never expiring, which
is the first ceiling: a jurisdiction requiring longer than ten years means S3 with a lifecycle policy
and an Object Lock retention period is the durable copy, and the log group is only the working copy.
Design for that at the start rather than discovering it at year ten.

**You cannot backfill.** `PutLogEvents` rejects "events older than 14 days or preceding the log
group's retention period" while accepting the rest of the batch — and reports them in
`rejectedLogEventsInfo` rather than failing the call, so a partially-rejected batch looks like a
success unless you read the response. Nothing repairs a gap discovered in month three: not you, not a
migration, not AWS Support. Treat it as a finding about the affected cases — the posture
`control-stack.md` takes on a partial evidence set — rather than an operational task to be tidied away.
At the other end, expiry is a destruction schedule: AWS says deletion "typically takes up to 72
hours" after events reach their retention setting, "but in rare situations might take longer", and
while the user guide does not address recovery, AWS re:Post states plainly that neither you nor AWS
can recover expired logs. Plan on no restore.

The second ceiling is the one that forces the storage split: **one log event caps at 1 MB, and one
`PutLogEvents` batch at 1,048,576 bytes** — computed as the sum of message bytes in UTF-8 **plus 26
bytes per event**, with at most 10,000 events and a span of no more than 24 hours per batch. A full
reasoning trace plus the evidence set for a multi-account investigation exceeds that comfortably.
This is the concrete number behind `control-stack.md`'s bundle-in-S3, hash-in-the-row design: not an
aesthetic preference about where bulk belongs, a limit that rejects the record.

**Index the join keys.** Two of them are already indexed for you: every Standard-class log group has
default field indexes on `traceId` and `attributes.session.id`, alongside `@logStream`,
`@aws.region`, `@aws.account`, `@source.log`, `@data_source_name`, `@data_source_type`,
`@data_format` and `severityText`. (Ten defaults, and they do not count against your quota. The
`PutIndexPolicy` API reference is stale and lists five — read the user guide instead.) So the trace
and session joins are fast by default, and what you add is the compliance vocabulary:

```json
{
  "Fields": ["case_id"],
  "FieldsV2": {
    "request_id":    { "type": "FIELD_INDEX" },
    "specialist_id": { "type": "FACET" }
  }
}
```

`PutIndexPolicy` is Standard class only, up to 20 fields per policy, 100 characters per field name,
`policyDocument` up to 51,200 characters, and the policy must include at least one field index. Three
details cost a deployment each: the type key is lowercase **`"type"`**, not `"Type"`; the field names
in `Fields` and `FieldsV2` **must be mutually exclusive**, so naming the same field in both is a
rejected policy rather than a merged one; and field names are **case-sensitive** — `case_id` and
`caseId` are different indexes, and querying the wrong one returns zero rows rather than an error.

Two limits change what indexing is for. Indexes are **not retroactive** — only events ingested after
the policy exists are indexed — and a matching event stays indexed for **30 days from ingestion**.
So field indexes accelerate the live investigation window, not the year-three examination; the
examination is served by the record store (§9) and the WORM bundle. Indexing also only applies to
JSON and recognised service log formats, which is another reason payloads should be structured at
emission rather than concatenated into a message string.

Finally, only `filter field = value` and `filter field IN [...]` use an index. AWS is explicit that
**`filter field like` never does** and "always scan[s] all log events in the selected log groups", so
a substring search over years of agent logs is a full scan you pay for twice — once in scanned bytes,
once in the twenty minutes an examiner spends waiting.

Sources:
[PutRetentionPolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html),
[PutLogEvents](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutLogEvents.html),
[CloudWatch Logs quotas](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/cloudwatch_limits_cwl.html),
[Field indexes](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CloudWatchLogs-Field-Indexing.html),
[PutIndexPolicy](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutIndexPolicy.html),
[filter command and indexes](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax-Filter.html),
[expired logs cannot be recovered](https://repost.aws/knowledge-center/cloudwatch-prevent-logs-deletion).

---

## 6. Turn on CloudTrail log file integrity validation, and log AgentCore invocations as data events

**Symptom:** the trail exists and has years of history. Asked to demonstrate the trail itself was
not edited, nobody can — validation was never enabled, and enabling it now says nothing about the
past. Separately: `InvokeAgentRuntime` calls are nowhere in the trail, because data events are off
by default and Event History never showed them.

**Satisfies:** R4a partly. R5 for the trail.

**Profile:** both, and under the prod profile it gains a second job. CloudTrail records calls, not
content, so it is profile-neutral by construction — and because the configuration changes that would
break the prod profile are themselves management events, logged by default, the trail is where you
detect a prod account drifting toward dev. Alarm on at least
`PutModelInvocationLoggingConfiguration` (§1), `PutDeliverySource` / `CreateDelivery` /
`UpdateDeliveryConfiguration` (§2), and `DeleteDataProtectionPolicy` / `DeleteAccountPolicy` (§4).
These are cheap alarms on events you are already paying to collect.

Integrity validation is a single flag, default **false**:

```bash
aws cloudtrail update-trail --name compliance-trail --enable-log-file-validation
```

(`EnableLogFileValidation` on `CreateTrail`/`UpdateTrail`.) CloudTrail then hashes delivered log
files with SHA-256 and signs digest files with SHA-256 with RSA, delivering the digests to the S3
bucket. Verify with:

```bash
aws cloudtrail validate-logs \
  --trail-arn arn:aws:cloudtrail:eu-west-2:111122223333:trail/compliance-trail \
  --start-time 2026-01-01T00:00:00Z
```

**Trap — enable it at trail creation.** AWS documents the discontinuity precisely: "When you disable
log file integrity validation, the chain of digest files is broken after one hour", no digest files
are created for log files delivered while validation was off, and "the same applies whenever you stop
CloudTrail logging or delete a trail." Digests arrive hourly, in a separate folder of the same S3
bucket. A gap in the chain is itself evidence, and an examiner reads it as one: the honest answer to
"why does the chain restart in March" needs to be a change record, not a shrug.

**Trap — enabling delivers digests but verifies nothing.** Nobody runs `validate-logs` unless
something makes them. Run it on a schedule and **record the result**, because the artefact that
convinces a reviewer is a dated series of successful validations, not the flag being set.

**Trap — validate in place, and know what it checks.** The CLI "will validate files in the location
where CloudTrail delivered them"; moving them to an archive bucket, or re-encrypting them under a
different key, means writing your own validator against the digest format. Within the time range you
give it, it checks **only the log files referenced in their corresponding digest files** — other
objects in the bucket are not examined, so a validated range is not an assertion that the bucket is
clean. For an organization trail, `--account-id` is required as well as `--trail-arn`. Decide the
bucket layout before the first delivery.

Then the data events, because the AgentCore invocation is not a management event:

```bash
aws cloudtrail put-event-selectors --trail-name compliance-trail \
  --advanced-event-selectors file://selectors.json
```

```json
[
  {
    "Name": "AgentCore runtime invocations",
    "FieldSelectors": [
      { "Field": "eventCategory", "Equals": ["Data"] },
      { "Field": "resources.type", "Equals": ["AWS::BedrockAgentCore::Runtime"] }
    ]
  },
  {
    "Name": "Compliance audit table writes",
    "FieldSelectors": [
      { "Field": "eventCategory", "Equals": ["Data"] },
      { "Field": "resources.type", "Equals": ["AWS::DynamoDB::Table"] },
      { "Field": "resources.ARN", "StartsWith":
        ["arn:aws:dynamodb:eu-west-2:111122223333:table/compliance-audit"] }
    ]
  }
]
```

`InvokeAgentRuntime` and `InvokeAgentRuntimeCommand` are **data** events under `resources.type`
`AWS::BedrockAgentCore::Runtime` (a sibling type `AWS::BedrockAgentCore::RuntimeEndpoint` exists too,
and selecting one does not select the other). Trails do not log data events by default, they carry
additional charges, and AgentCore's own guidance states that data events "must" be explicitly enabled
and that Event History "doesn't record data events" — so the console showing nothing is not evidence
that nothing happened.

The second selector is the concrete form of a point `control-stack.md` makes about the DynamoDB
append-only approximation: its compensating control is CloudTrail data events on item-level
operations, and those are off by default, so the control is nothing until this selector exists. This
is the selector. One shaping detail when you write it: `AWS::DynamoDB::Table` logs table **and**
DynamoDB Streams events by default, so add an `eventName` filter if stream reads would otherwise bury
the writes you are watching for.

CloudTrail records the call, not the content (`deployment-patterns.md`, observability as audit
evidence). But it carries **both the authenticated principal and the target `sessionId` in the same
`InvokeAgentRuntime` event**, which turns cross-principal session routing into a concrete alarm:
one session ID appearing under two principals is either a tenant-isolation defect or a session-ID
derivation bug (`production-rules.md` §7), and both are findings.

Sources:
[Validating CloudTrail log file integrity](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-intro.html),
[Validating with the AWS CLI](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-log-file-validation-cli.html),
[Logging data events](https://docs.aws.amazon.com/awscloudtrail/latest/userguide/logging-data-events-with-cloudtrail.html),
[AgentCore Harness operations and CloudTrail event types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html),
[Runtime session security — correlate principal and session ID](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-instances-security.html).

---

## 7. Object Lock protects a version, not your ability to find it

**Symptom:** the evidence bucket is configured exactly as designed, and a retrieval reports a case's
bundle as missing. It is there, intact and protected, under a delete marker. Or the inverse: a
control document states that objects written before Object Lock was enabled can never be protected,
so nobody tries, and an unlocked year of evidence stays unlocked.

**Satisfies:** R5.

**Profile:** both — the mechanism facts are unchanged, the bucket they apply to moves. In dev the
protected versions are AWS-held payload copies. **In prod there is no AWS-held payload copy, so every
fact below applies to the firm's own evidence store** — the encrypted bundles holding the reasoning
trace, the narrative output and the retrieved evidence (§11, §12). That is where an examination's
answer now lives, so that is where the tamper-evidence requirement went with it. Read this rule as
being about your bucket rather than about a Bedrock destination, and it is if anything more important
than before: it is no longer one copy of two.

`control-stack.md` carries the parameters, the `GOVERNANCE`-versus-`COMPLIANCE` decision, the
lifecycle and legal-hold facts, and the argument that the mode belongs to counsel rather than to
whoever wrote the CDK stack. Do not re-derive them. Four mechanism facts sit underneath that
section, and the first is a correction to it.

**1. The documented rule is "before you lock", not "before you write".** Versioning is required —
Object Lock "works only in buckets that have S3 Versioning enabled" — and once Object Lock is on you
can neither disable it nor suspend versioning. But AWS's rule is that you must enable versioning and
Object Lock "before you lock any objects", and it states plainly that "you can enable Object Lock for
new or existing buckets": `PutObjectLockConfiguration` with the `x-amz-bucket-object-lock-token`
header, after which object versions already sitting in the bucket can be locked with
`PutObjectRetention` or, at volume, S3 Batch Operations. The stronger folklore version of the rule is
worth correcting precisely because of what it causes: a team that believes an unlocked history is
unprotectable stops looking for the remediation that exists. Day-one enablement is still the right
design; retrofitting is still available when day one has passed.

**2. COMPLIANCE mode has exactly one documented escape, and it is not one.** AWS: "the only way to
delete an object under the compliance mode before its retention date expires is to delete the
associated AWS account." Put that sentence in front of whoever is choosing the mode, before the
choice rather than after, because it is the sentence that ends the "we will work something out if we
have to" conversation. The crypto-shredding path in §10 is the only actual answer, and it has to be
designed in before the first object is written.

**3. A plain `DELETE` hides an intact protected version, and the delete marker is not
WORM-protected.** A delete request without a version ID returns `200 OK`, inserts a delete marker,
and that marker becomes the current version — while a versioned delete against a protected version
returns `403 Forbidden`. AWS is explicit that "delete markers are not WORM-protected, regardless of
any retention period or legal hold in place on the underlying object", so the hiding move is available
to anyone with ordinary delete permission and undoing it means removing the marker. Two consequences
for procedure: **evidence retrieval must list object versions, not current objects** — a procedure
that lists current objects will report protected evidence as missing, and that answer to an examiner
is worse than the truth — and a delete-marker creation on the evidence bucket deserves an alarm of
its own, because it is the one destructive-looking action Object Lock does not prevent.

**4. The lifecycle guarantee is real but thinly sourced.** That S3 Lifecycle cannot expire versions
Object Lock protects, and cannot bypass governance retention, is stated in AWS Knowledge Center and
blog material rather than in the S3 User Guide. It is almost certainly what you want to rely on;
verify it once in your own bucket and cite your test alongside the AWS material, rather than citing a
blog post to an examiner.

Sources:
[S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html),
[Configuring Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html),
[PutObjectLockConfiguration](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObjectLockConfiguration.html),
[Managing Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html),
[Object Lock with S3 Batch Operations](https://docs.aws.amazon.com/AmazonS3/latest/userguide/batch-ops-object-lock.html).

---

## 8. Name span attributes deliberately, and never make a span attribute the fact carrier

**Symptom:** the spans arrive, and the GenAI Observability tabs and Evaluations show nothing for
half of them. Or worse: the case's facts were written into span annotations, and past the fiftieth
annotation searchability stops — the facts are in the trace and not findable in it.

**Satisfies:** R2a identifiers, the R3 join surface. Not the facts.

**Profile:** both, with different force. In dev, keeping message content off spans is advice — spans
are a lossy, truncating, 50-annotation-limited place to put facts, so it is bad engineering
independently of privacy. In prod it is a prohibition: a span attribute is an AWS-side log record, so
the §11 rule applies to span attributes exactly as it applies to a log line. Span attributes are the
most common way the prod profile leaks, because instrumentation defaults set them for you and no code
review sees a diff.

Set `gen_ai.operation.name` on every span you want AgentCore to classify. **AgentCore recognises
three values** — `invoke_agent` (an invoke-agent span), `execute_tool` (a tool-execution span) and
`chat` (an inference span) — and that is the list to build against. Longer value lists circulate
(`create_agent`, `text_completion`, `embeddings`, `retrieval` and friends); those come from the wider
OpenTelemetry GenAI conventions and other services' documentation, not from the AgentCore devguide, so
setting them buys you nothing here. The service honours framework fallbacks where the attribute is
absent: `traceloop.span.kind` = `workflow` or `tool`, `openinference.span.kind` = `AGENT`, `CHAIN`,
`TOOL` or `LLM`, and — note the asymmetry — for an inference span the fallback is
**`llm.request.type` = `chat`**, not a `traceloop.span.kind` value. AWS is blunt about the
consequence: **"the service skips a span that carries no recognized identifying attribute."** A
skipped span is invisible to Evaluations and to the GenAI Observability views, which reads as missing
instrumentation rather than as one missing attribute. For a top-level turn there is an escape hatch —
`agentcore.invocation.user_prompt` and `agentcore.invocation.agent_response`, read from any scope —
but it covers the turn boundary only, not the internal structure an examination cares about.

Do not confuse the service's attributes with yours. AgentCore's own `InvokeAgentRuntime` span uses
`aws.*` names plus `session.id`: `aws.operation.name`, `aws.resource.arn`, `aws.resource.type`,
`aws.agent.id`, `aws.endpoint.name`, `aws.request_id`, `aws.account.id`, `aws.region`,
`aws.xray.origin`, `latency_ms` and `error_type` (`throttle`, `system` or `user`, present only on
error). That `session.id` is your bridge from a `runtimeSessionId` to a trace — the join that answers
"which trace served this case's session" mechanically instead of by timestamp matching, which is the
thing that breaks under concurrency (`production-rules.md` §25).

**Pin the telemetry convention and record it on the case.** The same message content lands under
different keys depending on mode and framework: `gen_ai.input.messages` / `gen_ai.output.messages`
as span attributes under unified telemetry, `body.input.messages` / `body.output.messages` as event
records under split telemetry, `gen_ai.user.message` and `gen_ai.choice` as inline span events from
Strands, `traceloop.entity.input` / `.output` from one instrumentation, `llm.input_messages.*` or
`input.value` / `output.value` from another. (The `gen_ai.prompt` and `gen_ai.completion` names that
appear in third-party material are legacy OpenLLMetry conventions and do not appear in AWS
documentation — do not write an extractor against them.) A stored trace is only interpretable against
the convention in force when it was produced, which is the argument `control-stack.md` makes for prompt
and schema versions, and it fails the same way: silently, years later, when an extractor reads a key
that was never there and finds nothing.

Put your own attributes in your own namespace — `compliance.case.id`, `compliance.specialist.id`,
`compliance.workflow.version` — and **never invent a `gen_ai.*` key.** That namespace belongs to a
convention that evolves underneath you; a collision is a field that changes meaning without a
deployment. And keep `gen_ai.input.messages` and `gen_ai.output.messages` — and the framework
variants listed above, `body.input.messages`, `gen_ai.user.message`, `traceloop.entity.input`,
`llm.input_messages.*`, `input.value` / `output.value` — **off** spans entirely. In dev that is
because the payload stores of §1 and §2 already hold the content under controls you chose, and a
truncated second copy in the trace path buys nothing. **In prod it is because a span attribute is an
AWS-side log record**: putting message content there breaches the profile just as surely as logging
the output JSON would, and it does so through a library default rather than through a line of your
code. Two things follow. Set the instrumentation's content-capture switch off explicitly rather than
relying on its default, and pin the ADOT and framework versions that make that switch mean what you
think — §2 already shows an ADOT version silently changing where spans land, and a content-capture
default that flips in a minor release is the same failure with a disclosure attached. And add a
span-processor assertion in prod that drops any attribute key not on your
allowlist, which is the §11 gate applied to the telemetry path: the same predicate, a different
emitter.

Then the limits that stop a span being the fact carrier:

- **50 annotations per trace.** The segment-document reference frames it as an indexing ceiling
  ("X-Ray indexes up to 50 annotations per trace"), the SDK guides as a usage cap ("you can use up to
  50 annotations per trace"). Either way, 50 is where searchability stops. Annotation keys cap at 500
  alphanumeric characters and values at 1,000, and non-alphanumeric keys do not work with filters.
- **Attributes convert to metadata by default**, and metadata is not indexed or searchable. To make
  an OTEL span attribute an annotation, its key must be listed in the `aws.xray.annotations`
  attribute.
- A segment document caps at **64 KB**; one log event at 1 MB (§5).

**Facts go in the record store. The span carries the content hash and the object key.** That
division is what makes the trace a join surface rather than a competing, lossy, silently truncated
copy of the evidence.

Query the span logs directly rather than through the console when you need the hierarchy:

```
fields @timestamp, spanId, parentSpanId,
       attributes.gen_ai.operation.name,
       attributes.compliance.specialist.id
| filter traceId = "4bf92f3577b34da6a3ce929d0e0e4736"
| sort @timestamp asc
```

Confirm the flattened attribute paths with `aws logs get-log-group-fields` against the real log
group first. The flattening of nested attributes is not something to assume, and **a wrong path
returns zero rows rather than an error** — indistinguishable from an agent that never ran
(`production-rules.md` §6).

Sources:
[Span attributes AgentCore recognises, by framework](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/supported-frameworks-generic.html),
[AgentCore Runtime span attributes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html),
[X-Ray segment documents](https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html),
[Migrating to OTEL — attributes become metadata](https://docs.aws.amazon.com/xray/latest/devguide/xray-sdk-migration.html),
[GetLogGroupFields](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetLogGroupFields.html).

---

## 9. Per-fact attribution is a data structure you build; the trace is the corroborating half

**Symptom:** the case write-up is coherent and well-sourced. A challenged sentence traces to no
specialist, or traces to the synthesiser that merely repeated it. The QA agent passed the case,
because all it could do was re-read the prose.

**Satisfies:** R3, and R1, R2 and R4 at the level an examination actually asks about.

**Profile:** both, and the two-layer structure below turns out to be the same split as §11 — which is
the strongest evidence that the split is natural rather than imposed by the policy. The **telemetry
layer** is identifiers, spans and join keys: it is projectable, and it lives AWS-side. The **record
layer** holds `claim_text` — a narrative assertion about a named person — so it lives in the firm's
encrypted store, with only its `fact_id`, its structural fields and its `content_hash` projected into
the AWS-side metering row. Built this way before the policy existed, this structure needed no
redesign to satisfy it; a design that put `claim_text` on a span would have needed one.

`control-stack.md` requires per-fact attribution and `production-rules.md` §25 shows what its absence
costs. This is the implementation. It has two layers, and both are necessary.

### Telemetry layer — the spans

| Span | `gen_ai.operation.name` | Attributes |
|---|---|---|
| Case root | `invoke_agent` | `compliance.case.id`, `compliance.workflow`, `compliance.workflow.version`, `compliance.semconv.version` |
| Specialist | `invoke_agent` | `gen_ai.agent.name`, `compliance.specialist.id`, `compliance.specialist.version`, `compliance.dispatch.seq` |
| Tool call | `execute_tool` | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `compliance.records.read` |
| Inference | `chat` | `gen_ai.provider.name`, `gen_ai.request.model`, token usage, `compliance.model_invocation.request_id` |
| Synthesis | `invoke_agent` | `compliance.manifest.hash`, `compliance.facts.bundle_key`, `compliance.facts.bundle_hash` |

One case root per case. **One specialist span per *dispatched* specialist, opened before the call
is made** — that ordering is the whole point: a specialist that never returns still leaves a span,
with an error status, and the manifest reconciliation in the QA step below has something to find.
Open the span after the call returns and a failed specialist leaves no trace at all, which is
exactly the partial-evidence-set failure of `production-rules.md` §25 reproduced one layer down.

`compliance.records.read` holds **identifiers only, never content** — `{system, record_id, as_of}`
tuples. That is R2 at the telemetry layer: the answer to "which customer and transaction records
did this agent read" is a list of record IDs with as-of timestamps, not a copy of the records.
`compliance.model_invocation.request_id` is the join to §1, which is what lets the exact prompt be
produced for that specific inference rather than reconstructed.

### Record layer — one row per assertion

| Field | Meaning |
|---|---|
| `fact_id` | ULID. Assigned by the asserting specialist — see below |
| `case_id`, `trace_id` | Case and trace join keys |
| `specialist_span_id`, `tool_span_id` | Join into the telemetry layer |
| `asserting_agent` | Specialist ID **and version** |
| `source_kind` | `tool_result \| reference_data \| model_inference` |
| `source_ref` | The specific source: tool call ID, reference-data key and version, or inference request ID |
| `records_read` | List of `{system, record_id, as_of}` |
| `assertion_type` | `observation \| consistency_note \| gap` — the bounded set from `control-stack.md` Layer 2 |
| `claim_text` | The assertion as stated |
| `confidence` | As asserted, never as an approval gate |
| `retrieved_at` | When the underlying data was read, not when the row was written |
| `derived_from` | List of `fact_id`. Non-empty **only** for synthesiser inferences |
| `content_hash` | HMAC of the source payload as stored in the bundle — see §12 for why this is an HMAC and not a hash, and for the 4,096-byte message limit that decides what you actually MAC |

**Which of these cross into AWS.** `fact_id`, `case_id`, `trace_id`, `specialist_span_id`,
`tool_span_id`, `asserting_agent`, `source_kind`, `assertion_type`, `confidence`, `retrieved_at`,
`derived_from` and `content_hash` are identifiers, enums, timestamps and hashes — projectable, and
worth projecting, because they are what make the QA joins below runnable against AWS-side telemetry
without decrypting anything. `claim_text` is prose about a named person and never crosses.
`records_read` crosses **as identifiers** — the `{system, record_id, as_of}` tuples — while the records
they name stay behind the tenant key. `source_ref` needs a look before you assume: a tool-call ID or a
reference-data key and version projects cleanly, but a `source_ref` that has quietly become a
free-text description of a source is prose, and it is exactly the field an allowlist catches on its
first deploy rather than after a year of logging (§11).

Two rules decide whether this works at all. Both are about who writes what, and both are easy to
implement wrongly in a way that leaves the schema intact and the attribution meaningless.

**1. `fact_id` is assigned by the asserting specialist at assertion time, not by the synthesiser at
merge time.** If the synthesiser assigns IDs, then every fact's author is the synthesiser, and
attribution is nominal — the field is populated, the joins resolve, and the answer to "who asserted
this" is "the thing that wrote it down". That reproduces in the implementation exactly the failure
the design existed to prevent.

**2. A synthesised relationship is itself an assertion and needs its own row** — with
`asserting_agent = synthesiser`, `source_kind = model_inference`, and
`derived_from = [fact_id_A, fact_id_B]`. Without that edge, "specialist A asserted X, specialist B
asserted Y, and the synthesiser inferred a link between them" is indistinguishable in the record
from "someone asserted the link". That indistinguishability *is* the manufactured-link failure, and
it is precisely what a QA agent reading only prose cannot catch. The edge is cheap; its absence is
unrecoverable after the fact.

### QA verification is a set of joins, not a re-read

This is what the structure buys, and it is the reason to build it. Each check is mechanical, and
each failure is a specific finding:

- **Every narrative sentence maps to at least one `fact_id`.** A sentence with none **fails the
  case, not the fact** — there is no fact to fail.
- **Every `tool_result` fact has a `tool_span_id` present in the trace.** A fact citing a tool call
  that left no span was not produced by the run that claims it.
- **Every such tool call's target shows the expected in-window invocation count.** Existence is not
  attribution (`production-rules.md` §18, §22): a record can exist because a concurrent session
  wrote it, and a count of two where one was expected means two writers.
- **Every dispatched specialist has a span, and every span resolves to a manifest entry.** A
  mismatch in either direction is a case-level finding — a span with no manifest entry is an
  undeclared agent, a manifest entry with no span is a specialist that never ran.
- **Every `derived_from` edge resolves to facts asserted by a specialist rather than by the
  synthesiser.** A synthesiser inference derived from another synthesiser inference is a chain with
  no evidentiary floor.

Sources:
[AGENTOPS05-BP01 Establish end-to-end tracing and telemetry for agent operations](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html),
[AGENTOPS05-BP03 Implement structured logging and comprehensive audit trails](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html).

---

## 10. The PII-versus-examiner reconciliation, stated honestly

**Symptom:** two review boards, two irreconcilable answers. Privacy requires that customer data be
minimised and erasable on request. Supervision requires that the evidence for a compliance decision
be retained for years and producible in full. Both are correct, and a design that pretends the
tension away fails one of them at the worst possible time.

**Satisfies:** erasure, without giving up R5 — which is the only combination that satisfies both
review boards rather than choosing one.

**Profile:** both, different target. Written for a world where AWS holds a full copy, these four
layers are machinery for making PII in AWS *survivable*. Under the prod profile PII does not reach
AWS in the first place, which changes their standing rather than their content: **layer 1 stops being
one option among four and becomes the design**, and layers 2 to 4 become the dev profile's working
control and the prod profile's fallback for accidental leakage — applied to the firm's own store
rather than to AWS-held evidence bundles. Same four mechanisms, re-weighted. The re-weighting is the
point: minimisation by *not collecting* dominates minimisation by *masking what you collected*, and
every one of the traps below is a way the second one leaks.

Four layers, in descending order of strength. Each does less than it appears to, and knowing which is
which is the point.

### (1) Keep it out — under the prod profile this is not layer 1, it is the whole design

Span-processor redaction before export, and Bedrock Guardrails — `sensitiveInformationPolicyConfig`
on `CreateGuardrail`, with `piiEntitiesConfig` / `regexesConfig` actions of
`BLOCK | ANONYMIZE | NONE` (note the assessment object in a response is the shorter
`sensitiveInformationPolicy`, which is what makes the config name easy to get wrong). Configuration,
enforcement and the guardrail's own traps are `control-stack.md` Layer 5a and the global
`amazon-bedrock` skill. What belongs here is the storage consequence.

**Guardrails shapes the API channel, not your storage.** Masking does reach both directions of the
exchange — input prompts and model responses — but AWS states the exclusion directly: PII masking
does not apply to model invocation logs, and "the `input` field in Amazon CloudWatch Logs always
contains the original, unmodified request regardless of guardrail intervention." Two further unmasked
egress points to design around: the `match` field in the guardrail trace carries the **original** PII
value by design, so `trace` output is PII wherever your application logs it; and asynchronous stream
processing does not support masking at all. Treat Guardrails as a channel control with a storage side
effect, and put the storage control in §11 (what may be emitted at all), §4 (what happens when
something is emitted anyway) and §10 (erasure).

Two consequences specific to the prod profile. The first of those three exclusions — the one about
model invocation logs — stops mattering, because under this profile those logs do not exist (§1). The
other two get *worse*, because they run through your own code rather than through a platform store:
the guardrail trace's `match` field is a PII-bearing structure inside your process, so it is in scope
for the §11 gate exactly like the model output is. A team that logs the guardrail trace to prove the
guardrail fired has logged the PII the guardrail caught. Log the assessment's action and its
identifier, never the `match`.

**Mechanically, "keep it out" means a deterministic gate between generation and logging.** That is
§11, and it is the only layer here you can test. The other three are recovery.

### (2) Mask after ingestion, reversibly — the leak backstop, not the control

The data protection policy of §4 plus exactly one `logs:Unmask` break-glass role. This is the only
mechanism in this file that delivers minimisation-by-default and a named, auditable examiner path
together. It is also a view control: the bytes remain.

### (3) The WORM-versus-erasure collision, faced squarely — now in your own store

COMPLIANCE versus GOVERNANCE — the decision is in `control-stack.md`, the mechanism facts underneath it
in §7. COMPLIANCE means that for the retention period an erasure request cannot be honoured by
deletion, by anyone, with account deletion as the only documented alternative. GOVERNANCE with a
single break-glass bypass role, two-person control and an alarm is the usual defensible answer for
PII-bearing evidence. Either way it is a decision to take with counsel and to record, not a default
to inherit from a template.

Under the prod profile the collision is entirely inside the firm's own evidence store, which sharpens
it rather than softening it: there is no second AWS-side copy to fall back on if the locked one cannot
be produced, and no second copy to worry about if the locked one is shredded. One store, both
obligations, one decision.

### (4) Crypto-shredding is the actual reconciliation — for the store that holds the evidence

Encrypt each subject's evidence bundle with SSE-KMS under a **per-subject customer managed key**.
To erase, schedule the key for deletion:

```bash
aws kms schedule-key-deletion --key-id <subject-key-arn> --pending-window-in-days 7
```

`PendingWindowInDays` must be between 7 and 30 inclusive, defaulting to 30, and the
`kms:ScheduleKeyDeletionPendingWindowInDays` condition key can enforce a floor so nobody shreds on
seven days' notice by accident. AWS states the effect in the strongest available terms: "when a KMS
key is deleted, all data that was encrypted under the KMS key is unrecoverable." **The WORM object
version remains byte-identical and provably unaltered** against its content hash and its Object Lock
retention — **and permanently unreadable.** Both obligations are satisfied at once, which no other
mechanism here achieves.

Read the documented exception, because it is a design constraint on the key rather than a footnote:
unrecoverability does not follow for **a multi-Region replica key, or an asymmetric or HMAC key with
imported key material.** A multi-Region key whose replica survives elsewhere means the ciphertext is
still decryptable and the erasure did not happen. The shape that actually shreds is a single-Region
symmetric customer managed key with AWS-generated key material — decide that at key-creation time,
and assert it in the erasure runbook rather than assuming it.

Now be honest about where it bites, because every one of these has ended a design that reached for
crypto-shredding late:

- **The key is the erasure unit, so key granularity is the whole design — and it is not
  reversible.** One key per customer is a KMS key-count and cost problem. One key per tenant means
  you cannot erase one customer without erasing that tenant's entire evidence base. Choose before
  the first object is written, because re-keying years of WORM objects is impossible: you cannot
  rewrite them.
- **It works only if the per-subject key is the *only* path to the plaintext.** Anything the same
  content also reached survives the shredding: a log group under a different key, X-Ray, a
  third-party OTEL vendor, a DynamoDB row, a cached prompt. That makes crypto-shredding an
  **architecture constraint on where PII may land**, not a switch thrown at the end. Under the prod
  profile the §11 gate is what keeps that list empty — the same control doing double duty, which is
  the usual sign that a constraint is in the right place.
- **Bedrock model invocation logging used to be the thing that broke this, and under the prod profile
  it no longer exists** (§1). Worth naming as a positive consequence rather than leaving implicit: an
  account-and-Region-wide store with one destination pair and no per-tenant key is unshreddable by
  construction, so as long as it was on, the crypto-shredding design was aspirational — a plaintext
  path survived every key deletion. Switching it off is what makes the rest of this section true. In
  dev it remains on and remains unshreddable, and that is fine, because there is no subject to erase.
  (If you run a non-synthetic workload in an account where it *is* enabled, the honest options are
  unchanged: short retention with the WORM bundle as the long-term copy, or documented as out of
  scope for erasure with a stated legal basis. There is no third option that is true.)
- **The AWS-side metering store is not covered by per-subject shredding, and does not need to be —
  but it is not out of scope either.** It holds pseudonymous identifiers under whatever key its log
  group or bucket uses, not per-subject keys, so erasure there is deletion or documented retention
  rather than crypto-shredding. §13 is why that still belongs in the data-protection register. The
  useful property is that deleting it is *possible*, which was never true of a WORM bundle.
- **The content hash can itself be personal data.** A hash of a single field value is a lookup
  oracle for that value. Hash the **bundle**, not per-field values, and use an HMAC whose key
  shares the subject's shredding lifecycle — so the verification artefact dies with the evidence
  rather than outliving it as a check on guessed plaintext.
- **Deletion is irreversible and whole-key.** There is no way to shred part of a key's data. So
  bundles must be partitioned by subject **at write time**, or not at all.

Sources:
[ScheduleKeyDeletion](https://docs.aws.amazon.com/kms/latest/APIReference/API_ScheduleKeyDeletion.html),
[Deleting AWS KMS keys](https://docs.aws.amazon.com/kms/latest/developerguide/deleting-keys.html),
[KMS condition keys](https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html),
[Guardrails sensitive information filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-sensitive-filters.html),
[Using SSE-KMS with S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html).

---

## 11. Two projections of one output: metering to AWS, the record to your own store

**Symptom:** the output schema was reviewed and approved. Then during an incident someone added
`rationale` to the structured log line, because it was the only way to see why the agent said what it
said. Nothing failed, nothing alarmed, and the deploy passed every test — and the log group now holds
narrative prose about named customers. Or the earlier version of the same failure: the system prompt
says "do not include personal data in the output", the team recorded that sentence as the PII control,
and a review believed them.

**Satisfies:** nothing on its own. It decides *where* R1–R5 land, which is why it sits between
generation and every logging call rather than beside them.

**Profile:** prod. Run it in dev too, unchanged — a gate that is only exercised in the environment
where it matters is a gate whose first real test is a production deploy.

### Why the output cannot be PII-free

A compliance output carries PII by construction. In the alert-triage schema
(`examples/alert_triage_prompt.md`) the narrative fields are `rationale`, `red_flags[].statement`,
`mitigating_factors`, `gaps` and `recommended_actions` — and a 120-word rationale explaining why a
payment pattern looks like layering will contain names, amounts, employers, counterparties and
locations, because those are the facts that make the reasoning legible. Prose about a named person's
transactions is PII whatever field it sits in.

So "the output JSON contains no PII" cannot hold for the output as a whole. And it cannot be *made* to
hold by instructing the model, because an instruction in a prompt is not a control — it is a request,
evaluated by a stochastic system, with no enforcement point and no failure signal
(`production-rules.md` §21 makes this argument about access; it is the same argument). A prompt asking
for identifier-only prose will comply most of the time, which is the worst available reliability
profile for a privacy control: frequent enough to pass a spot check, rare enough to leak steadily.

### The shape that works: one output, two projections

**Metering projection → AWS.** Every field a UUID, an enum member, a number, a boolean or a hash:

- workflow name invoked, tenant UUID, customer UUID, alert or case UUID
- invocation count, tool names called, the row UUIDs read as references during fact collection
- input and output token usage, computed cost, latency
- `recommendation` (enum), `risk_score` (number), `confidence` (enum), the boolean flags
- schema version, prompt version, model ID, inference parameters
- the approver's user UUID and the decision timestamp
- the **content hash of the full record** (§12)

**Full record → the firm's own store**, encrypted under the tenant key (§12), holding the reasoning
trace, every narrative field, the retrieved evidence, the deterministic validation result and the
outcome.

Field by field, against the real schema:

| Output field | Metering projection (AWS) | Full record |
|---|---|---|
| `alert_id`, `case_id`, `customer_id`, `tenant_id` | yes — UUID | yes |
| `recommendation`, `confidence` | yes — enum member | yes |
| `risk_score` | yes — number | yes |
| `account_takeover_suspected`, `customer_may_be_victim`, `escalation_recommended` | yes — boolean | yes |
| `primary_typology`, `additional_typologies[]` | **only if validated against a closed vocabulary.** A free-string typology is prose | yes |
| `red_flags[]` | per element `{kind, evidence_id}`, plus the element count. `statement` never | full objects |
| `mitigating_factors[]`, `gaps[]`, `recommended_actions[]` | count only | full |
| `rationale` | never — only its presence, its length and the record hash | full |
| tool names called, row UUIDs read, tokens, cost, latency, versions | yes | yes |
| reasoning trace, retrieved evidence, reviewer's note | never | yes |

Two rows there are easy to miss. **`red_flags[]` projects partially:** `kind` and `evidence_id` are an
enum and an identifier, so the AWS side can hold the count and the shape of the flags — enough for
drift monitoring and QA sampling (`control-stack.md`) — without a word of their content. That holds
only if `evidence_id` is asserted against your evidence-ID format rather than accepted as "a string";
a model that writes a short description where an ID belongs turns a projectable field into a prose
field, and the predicate is what catches it. And **a count is a projection.** "Three gaps were
reported" is a number, and it is often the signal the metering store actually needed.

### The gate: a deterministic allowlist at a single choke point

Between generation and logging, a pure function. For each field name on the allowlist, apply that
field's type predicate; emit what passes; divert everything else — every unlisted field, and every
listed field whose value fails its predicate — into the internal record, never into an AWS log.

**Allowlist, not denylist.** A denylist is a list of the fields somebody remembered. When the schema
gains `analyst_note` next quarter, a denylist leaks it on its first deploy and the build stays green
until a privacy review notices. An allowlist fails closed: the new field is simply absent from the
metering row, somebody asks why, and the conversation happens before the data does. Cheap failure now
against expensive failure later is the entire argument, and it is the same argument as a default-deny
IAM policy.

**Predicates on values, not on types.** This is where implementations quietly go wrong.
`isinstance(x, str)` is satisfied by a 120-word rationale, so a "string" predicate makes every prose
field projectable and the gate becomes decoration. The predicate for a UUID field is a UUID parse. The
predicate for an enum field is membership in the declared set — the same closed set the deterministic
validator already checks (`examples/output_validation.py`), reused rather than re-declared, so the two
cannot drift apart. The predicate for a number is a number in range; for a hash, a fixed-length hex
digest. **If a field's predicate is "it is a string", that field is not projectable and the allowlist
entry is wrong.**

**One choke point.** If two code paths can write to a logger, the gate is a suggestion. The projection
function must be the only thing that constructs an AWS-bound log record, metric dimension or span
attribute — and that includes error paths. An exception handler that logs the exception with the
output object in its message defeats the gate completely, and does it on the worst day of the quarter.
Log the exception type, the record UUID and the schema version; never the object. That one habit
removes the most common source of the leak §4 exists to survive.

**Test it with fixtures containing known markers.** This is the only PII control in this file that is
unit-testable, which is a strong reason to prefer it: run a synthetic case whose narrative fields
contain unique tokens, project it, and assert that none of those tokens appear anywhere in the
projected output — then assert the same over a serialised span batch, because §8 is the same boundary
with a different emitter. A control you can assert in CI is a different category of control from one
you can only review.

`examples/log_projection.py` carries the worked implementation: the allowlist declaration, the
predicates, the divert path and the tests. Use it rather than reimplementing from this description —
the nested-object, list-projection and hash-format cases are where a from-scratch version leaks.

### The design observation that makes the projection cheaper

If assertions cite evidence by identifier and typology label rather than restating content, the
narrative moves structurally closer to identifier-only, and less has to sit behind the tenant key.
Compare:

- "£9,400 arrived from Ridgeway Haulage Ltd on 4 March and left to M. Osei the same day" — three
  named entities, an amount and a date in one sentence.
- "`txn-3` departs from this customer's own payer baseline; `txn-3` → `txn-4` is a same-day
  pass-through" — two identifiers and two typology labels, and a reader holding the evidence bundle
  can resolve both.

The second is not vaguer, it is **referential**: the facts stay in the evidence and the assertion
points at them. The `evidence_id` field per red flag already exists for exactly this shape
(`examples/alert_triage_prompt.md`), and this is its second payoff — the first was mechanically
checkable citation grounding, the second is that a referential assertion is nearly projectable and
much less damaging if it leaks.

Be honest about what this buys. It shrinks the encrypted half and reduces the harm of an accidental
leak. It does **not** remove the gate, because a model will still write a name into a rationale
sometimes, and "sometimes" is precisely what a deterministic control is for. Prompt-side referencing
and gate-side enforcement are complements, and only one of them is a control.

### Diagnostics

- **Schema-versus-allowlist diff, in the build.** Enumerate the output schema's fields and diff
  against the allowlist and the divert list. A field in neither fails the build. This is what turns a
  new schema field into a five-minute conversation instead of an incident review.
- **Sample the metering store.** A Logs Insights query over the prod metering group flagging any value
  that fails the projectable-type predicates. "Contains a space" is a crude first approximation and it
  finds prose immediately.
- **Ask the reverse question.** Take one metering row and try to answer "why did the agent recommend
  this". If you can, the projection is leaking. The metering row is supposed to prove *what* happened
  and be structurally unable to explain *why* — that inability is the test that the split is real.

Sources: design reasoning — no AWS mechanism performs this split for you, which is why it is code you
own and test. The practice it implements is
[AGENTOPS05-BP03 Implement structured logging and comprehensive audit trails](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp03.html);
the reason it cannot be delegated to the model is the prompt-instruction argument in
`production-rules.md` §21 and `control-stack.md` Layer 5.

---

## 12. Encrypt for retrieval, HMAC for indexing — the conflation ships, so state it as a rule

**Symptom:** a design document says the internal record is "hashed by the tenant's keys". It passes
review because it sounds cryptographically serious, and it gets built. Then the first examination
request arrives and there is nothing to produce — hashing is one-way, and no examiner has ever
accepted a digest as a reasoning trace. Or the milder version: the record is encrypted correctly, but
the only way to find a subject's records is to decrypt every candidate, so a subject-access request
becomes a bulk decryption of an entire tenant.

**Satisfies:** retrievability (encryption) and R5 (the HMAC pairing). Two different requirements. Each
mechanism satisfies one of them and not the other, and that is the whole rule.

**Profile:** both. The tenant-scoped store exists in prod by policy and in dev by symmetry — and a dev
store that skips the encryption is a dev store whose retrieval path has never been exercised.

### Encryption under a tenant-scoped key is what makes the record producible

Envelope encryption: a data key per record, wrapped by a customer managed key **per tenant**.
Retrieval is a `Decrypt` and a read. Two properties to build in deliberately:

- **Gate `kms:Decrypt` on the encryption context, not only on the key.** AWS's least-privilege
  guidance names `kms:EncryptionContext:context-key` and `kms:EncryptionContextKeys` as "the most
  effective way to implement least privileged permissions", because they bind the permission to the
  context bound to the ciphertext. Put the tenant ID in the encryption context and condition the grant
  on it: a cross-tenant decrypt then fails at KMS rather than in your application's authorisation
  code, which is the same reasoning this skill applies to row-level security — a boundary the platform
  enforces beats a boundary the caller enforces. Mind the case-sensitivity rule AWS documents: the
  condition *key* is not case sensitive, the *value*'s sensitivity follows the operator you choose, so
  `StringEquals` on a tenant ID is what you want and `StringEqualsIgnoreCase` is not.
- **An examiner reads through the tenant, not around them.** A per-tenant key means production for an
  examination happens with the tenant's cooperation and leaves a `Decrypt` in CloudTrail — an access
  record for the evidence, which is a feature. It is also an availability dependency: write it into
  the runbook before it is discovered under a regulatory deadline.

### HMAC does two jobs, and neither of them is retrieval

**A blind index.** HMAC the value you need to look up, store the tag, query on the tag. The record
stays encrypted and the lookup decrypts nothing. AWS KMS supports this directly: HMAC KMS keys with
`GenerateMac` and `VerifyMac`, `MacAlgorithm` one of `HMAC_SHA_224`, `HMAC_SHA_256`, `HMAC_SHA_384`,
`HMAC_SHA_512`.

**Tamper evidence.** A tag over the bundle's canonical content binds the structured row to the
encrypted bundle — `content_hash` in §9, the pairing `control-stack.md` specifies.

Four mechanics decide whether either works:

- **`GenerateMac` caps the message at 4,096 bytes.** An evidence bundle is larger, so you MAC a
  *digest* of it rather than the bundle, and AWS states the consequence plainly: "If you generate an
  HMAC for a hash digest of a message, you must verify the HMAC of the same hash digest." That makes
  the digest algorithm part of the record format — record it beside the prompt and schema versions, or
  a verification in year four fails for a reason nobody can distinguish from tampering.
- **HMAC tags carry no timestamp**, and AWS's guidance is to limit the period for which any signing
  mechanism is effective. Put the record's timestamp *inside* the MACed message, so the tag is bound
  to a moment instead of being replayable.
- **The HMAC key is per tenant, with the same lifecycle as the encryption key** (§10). A global HMAC
  key turns a blind index into a cross-tenant join: the same value yields the same tag for every
  tenant, so the index re-identifies across the boundary it was built to respect. And an index key
  that outlives a crypto-shred leaves a working oracle for the shredded plaintext — §10's point about
  the content hash being personal data, restated at the index.
- **MAC the plaintext, and store the object version ID beside the tag.** A tag over ciphertext
  survives re-wrapping but proves only that the ciphertext is unaltered, not that it decrypts to the
  same evidence. A tag over canonical plaintext proves the evidence and needs the plaintext to verify —
  the right trade for an audit record, because verification happens at examination time when you are
  decrypting anyway.

### The rule

**A record that is only hashed satisfies tamper evidence and fails retrievability — which is the
requirement it exists for.** You need both operations, both keyed to the tenant, with distinct keys
and distinct purposes:

| | Operation | Key | Answers |
|---|---|---|---|
| Retrieval | Envelope encryption, `Decrypt` | Per-tenant CMK, `kms:Decrypt` gated on encryption context | "Produce the reasoning trace for case X" |
| Location | HMAC blind index | Per-tenant HMAC key, same lifecycle | "Which records belong to this subject", without decrypting any |
| Integrity | HMAC over the canonical bundle digest | Per-tenant HMAC key, same lifecycle | "This bundle is the one the row was written against" |

**Diagnostic:** take your design and answer, on paper, "produce the reasoning trace for case X". If
the answer involves a hash, the design fails R1 — and it will fail it in front of an examiner rather
than in front of you. Then run the negative: attempt a decrypt with a mismatched tenant in the
encryption context and confirm the refusal comes from KMS, not from your code.

Sources:
[HMAC keys in AWS KMS](https://docs.aws.amazon.com/kms/latest/developerguide/hmac.html),
[GenerateMac](https://docs.aws.amazon.com/kms/latest/APIReference/API_GenerateMac.html),
[Encryption context](https://docs.aws.amazon.com/kms/latest/developerguide/encrypt_context.html),
[KMS least-privilege permissions](https://docs.aws.amazon.com/kms/latest/developerguide/least-privilege.html),
[KMS condition keys](https://docs.aws.amazon.com/kms/latest/developerguide/conditions-kms.html).

---

## 13. UUIDs in AWS logs are pseudonymous, not anonymous

**Symptom:** a privacy review is told the AWS-side metering store "holds no personal data, only
UUIDs". The register records it as out of scope: no lawful basis written down, no retention period
set, access broader than it would otherwise have been. Then an erasure request arrives, and the
question is not whether the store is in scope — it is who explains why the register says it is not.

**Satisfies:** nothing. It scopes what the prod profile achieved, which matters because this design is
easy to over-claim.

**Profile:** prod — it exists because the prod projection invites exactly one wrong conclusion, and
the wrong conclusion is the kind that ends up written into a register and relied on for years.

The metering projection replaces names with UUIDs, and the mapping from UUID to person lives in the
firm's own store. That is the design working as intended — and it means the UUID is **pseudonymised
personal data, not anonymous data.** Under GDPR and comparable regimes pseudonymisation is a security
and minimisation measure; it does not take data out of scope, because the data remains attributable to
an individual using additional information you deliberately kept.

Four consequences, all of them build tasks rather than paperwork:

1. **The AWS-side metering store belongs in the data-protection register**, with a lawful basis, a
   retention period and a named owner, like any other personal-data store.
2. **It is in scope for subject-access and erasure requests**, which means the rows for a subject must
   be *locatable*. That is what the blind index of §12 is for, and it is the reason to index on the
   tenant-and-subject keys rather than only on the case ID.
3. **Access control still matters.** A customer UUID plus a workflow name plus a timestamp plus a
   `recommendation` enum is a statement that a specific person was assessed for financial-crime risk
   and what the assessment concluded. That is a sensitive inference with no name attached — and in
   some jurisdictions the fact of a suspicion carries confidentiality rules of its own, on top of data
   protection.
4. **Confirm your identifiers really are surrogate keys.** A field named `customer_id` holding an
   account number, a national identifier, an email address or a `firstname.lastname` string is not
   pseudonymous at all. It is the identifier in a different font, and it defeats the whole projection
   while passing every "is it a string of the right shape" check. Assert the format, and assert that
   the identifier space is internal and not derivable from customer-facing data.

Then keep it proportionate. **This is not a reason to avoid the design.** Removing narrative, names
and account numbers from the cloud-side store is a large and real improvement: it shrinks the blast
radius of a log-group misconfiguration from customer narratives to opaque identifiers, it makes the
store deletable, and it converts a disclosure risk into a correlation risk. The correct conclusion is
"pseudonymous, therefore in the register, therefore governed" — a normal amount of work. The incorrect
conclusion is "no PII, therefore out of scope", which is wrong and which gets found to be wrong at the
least convenient moment.

Sources: this is a legal characterisation rather than an AWS behaviour — confirm it for your
jurisdictions with whoever owns data protection. It is in this file because the engineering
consequences (the register entry, the locatability requirement, the identifier-format assertion) are
things somebody has to build, and they are cheap in the design and expensive afterwards.

---

## 14. The approver's identity is employee data, and it is a second lawful basis

**Symptom:** none, which is why it reaches production. A team that has carefully kept customer PII out
of AWS-side telemetry puts the approving analyst's UUID and decision timestamp on the metering row,
because both are identifiers and the rule was "identifiers are fine".

They are identifiers. They are also, in aggregate, workforce monitoring.

An actor UUID plus a decision timestamp on every approval yields a per-analyst decision rate, an
agree-versus-disagree ratio against the model, a working-hours series, and a productivity comparison
between named individuals — held in a log store the firm does not control, under a retention schedule
set for operational telemetry rather than for HR records. In several jurisdictions that requires its
own lawful basis, its own notice to the workforce, and in some its own consultation. It is a different
question from the customer-data one, with different answers, and satisfying the customer-data rule
tells you nothing about it.

**Rules:**

1. **The approver's identity belongs in the internal record, not the metering projection.** That is
   where §11's structured row already puts the human decision, the actor and the timestamp. Diverting
   it there is not a compromise forced by the gate — it is the correct side of the split.
2. **Expect the gate to divert these fields, and do not widen the allowlist to admit them.** A
   correctly-built allowlist has no entry for `actor_id`, `actor_role`, `decided_at` or `agreed`, and a
   shape-based sweep will reject an ISO-8601 timestamp as phone-shaped and epoch seconds as an
   oversized integer. Those rejections are the control working. The tempting fix — four lines widening
   the allowlist so the dashboard renders — is a workforce-monitoring dataset created by a config edit.
3. **Notice the asymmetry this produces, and keep it.** The metering row carries what the agent
   *proposed* (`recommendation`, `risk_score`, `confidence`) and **not** whether the human agreed. So
   AWS-side telemetry can answer "what did the model recommend, how often, at what cost" and cannot
   answer "which analysts override it". The first is model performance. The second is performance
   management, and it should require deliberately reaching into the firm's own store.
4. **Where an aggregate genuinely is needed** — model-override rate is a real model-risk metric —
   compute it in the internal store and emit the aggregate, never the per-decision rows. A count of
   overrides per thousand decisions carries the model-risk signal and identifies nobody.

**Why it hides:** every field involved passes the "is it an identifier" test that the customer-PII rule
was written around, so the reviewer who wrote that rule reads the row as compliant. The problem is not
the field, it is the join across many rows — and nothing in a single record looks wrong.

Sources: as with §13, a legal characterisation rather than an AWS behaviour. Confirm it with whoever
owns data protection and, separately, with whoever owns the employment relationship — they are usually
not the same person, and the second one is the one nobody asks.

---

## What none of this gives you

State these as gaps rather than letting a reviewer find them:

- **AWS-side telemetry alone cannot reconstruct a decision.** This is the one the prod profile
  introduces, and it is the most important sentence in the file. The AWS side can prove that an
  invocation happened, which workflow ran, which rows were read, what it cost, and — via the content
  hash — that the record is unaltered. It cannot say what the agent was asked, what it answered, or
  why. **The decision exists only in the firm's own store.** So that store's availability, backup and
  retention are now audit-critical in a way they were not when AWS held a full copy: a lost bundle is
  an unreconstructable decision, not an inconvenience, and "the cloud provider has a copy" is no
  longer a true fallback. Test the restore, on a schedule, and file the restore-test results in the
  same evidence pack as the CloudTrail validation runs (§6) — a dated series of successful restores is
  what makes the architecture defensible, exactly as it is for validation.
- **No immutable log groups.** There is no Object Lock for CloudWatch Logs. The strongest control
  is a resource policy plus an SCP, and policy can be re-granted. The log group is a feeder; the
  WORM bundle plus hashed row is the record.
- **Expired log events are gone.** Retention is a destruction schedule, not a soft-delete: deletion
  typically completes within 72 hours of expiry and can take longer, and there is no restore path for
  you or for AWS.
- **No backfill, anywhere.** Model invocation logging, vended delivery, span destinations, data
  protection policies and field indexes all apply from the moment they exist. A gap is permanent,
  and the correct response is a finding about the affected cases.
- **Macie is blind to CloudWatch Logs.** It sees S3. Payloads sitting in a log group are outside
  every scan you have.
- **AgentCore Identity proves which user's token was exchanged — not that a qualified person read
  and approved a draft.** It answers *whose credential*, which is not R4. The approval record is
  something your application writes, under a human identity, at the moment of the decision
  (`control-stack.md` Layer 1). No AWS mechanism produces it for you.
- **Nothing in AWS knows what a "deployment profile" is.** There is no setting that means "this
  account may not hold reasoning traces". The separation is an account boundary, an SCP and a
  deterministic gate — three things you build and keep testing. A configuration flag is not one of
  them, and neither is a documented intention.
- **Masking never establishes absence.** Detection is pattern matching plus keyword proximity, and
  narrative prose defeats it routinely (§4). Under the prod profile that limitation is survivable
  because masking is the backstop rather than the control; if it ever becomes the control again, the
  limitation becomes the exposure.
- **The gate cannot see inside a value.** §11 proves that a field is a UUID or a member of a closed
  enum. It cannot prove that the UUID space is not derived from something personal, that an enum
  vocabulary does not encode a sensitive inference, or that a timestamp plus a workflow name is not
  itself disclosive (§13). Those are design decisions, checked once by a human, not predicates.
- **`USAGE_LOGS` does not exist for four of the AgentCore primitives.** Identity, Memory, Gateway and
  Payments offer `APPLICATION_LOGS` and `TRACES` only (§2). Under a profile that prohibits the first
  and constrains the second, the AWS-side meter for those primitives is whatever your own code emits —
  so a metering design that assumes platform coverage has four blind spots by construction.
