# Client integration contract: Capstone, Coder and future applications (Gateway 0.4.4)

Wire contract **`shared.inference/1`**, unchanged and additive since 0.1.0. SDK: `shared_inference_client` (standard library only).
- **0.4.6, additive (SDK 0.4.2):** `GET /v1/policy?availability=0` (operator policy only), `GET /v1/fleet` (application-scoped snapshot with installed / qualified / available as separate states), `upstream_errors[]` in settled status replies, `GET /health` `release` and `features`, capability purpose `structured_chat`. SDK 0.4.2 adds `gateway_info()`, `policy(availability=False)`, `fleet()` and `fleet_cached()`; every 0.4.1 call is unchanged. The full contract, compatibility rules and recommended launch path: [handoff/GATEWAY-0.4.6-CLIENT-CONTRACT.md](handoff/GATEWAY-0.4.6-CLIENT-CONTRACT.md).
- **Ship SDK 0.4.1** (unchanged in Gateway 0.4.2, 0.4.3 and 0.4.4) (`shared_inference_client.__version__ == '0.4.1'`; the file `client/shared_inference_client.py` in the release package, SHA-256 in `SHA256SUMS`). It retries a timed-out, reset or 503-answered submission or status read with the same `client_key` / `request_id` until the request's own deadline, and backs off its polling (§5). Older SDKs still work against 0.4.1, but fail a flow on the first busy-Gateway timeout or 503.
- The released 0.1.0 SDK still works unchanged against 0.4.1 (`tests/test_cross_version.py`).
- **0.4.1, additive:** lifecycle times in every status reply (§6); 503 with `Retry-After` instead of a dropped connection or a 400 when the Gateway's own limits are full (§2).
- **0.4.4, additive, no client change required:**
  - `model_details[m].parallel = {limit, source, reason}` in `/v1/models` (§3): calls of that model one head takes at once. `source` is `declared`, `measured`, `serial_family`, `slots` or `policy`; further calls wait in the Gateway queue or go to another head.
  - `provenance` gains `attempt`, `attempts[]` and `model_substitution` (always `null`), and six `timing_ms` keys (§6).
  - A busy ledger answers 503 with `Retry-After` within 5 s, inside the SDK's 10 s attempt. The SDK retries it with the same identity, as it already does.
- Exact shapes are pinned by `tests/test_client_contract.py`.
- **0.4.0, no client change required.** Placement and failure handling changed ([FAILURES-AND-PLACEMENT.md](FAILURES-AND-PLACEMENT.md)). A request whose head answered with a server error, or could not be reached, is re-placed once on another head under the same request ID, key and deadline. Its `request.status` then goes back to `queued` briefly and its `resource_id` changes. One request, one result. A suspended model drops out of `/v1/models`, as an unqualified one does.

The Gateway is the only authority for admission, leases and head scheduling. A client chooses *what* to ask for; the Gateway decides *whether and where* it runs.
- **No direct Ollama fallback.** Raw heads refuse 192.0.2.201 and 192.0.2.202.
- **No plaintext or redirected Gateway connection.** The SDK refuses remote plaintext and never follows redirects.

## 1. Client configuration

One file per application host. The SDK reads it with `Client.from_config(path)`.

```json
{
  "configuration_version": 1,
  "contract": "shared.inference/1",
  "service_id": "shared-inference",
  "url": "https://192.0.2.254:8443",
  "application_id": "capstone",
  "token_file": "C:\\ProgramData\\Capstone\\gateway\\capstone.token",
  "ca_file": "C:\\ProgramData\\Capstone\\gateway\\inference-gateway-ca.pem"
}
```

| Field | Rule |
|---|---|
| `url` | Origin only: no path, query or credentials. HTTPS; plain HTTP is accepted only on loopback |
| `service_id` | Must equal the `X-Inference-Service-ID` response header, or every call fails |
| `application_id` | The ID registered in the Gateway, which is also the Arena `app_id`: `capstone`, `coder`, … |
| `token_file` | **The one supported credential source in deployments.** A protected file readable only by the application's service account. Relative paths resolve from the config file. It overrides any environment variable |
| `ca_file` | `inference-gateway-ca.pem`, the Gateway lab CA (public). TLS verifies the certificate and its IP SAN `192.0.2.254` |

**Resolution of the token-name conflict.**
- `CAPSTONE_INFERENCE_TOKEN`, `CAPSTONE_INFRASTRUCTURE_TOKEN` and `APPLICATION_INFERENCE_TOKEN` are **not used** in Gateway deployments.
- `token_environment` remains only for developer loopback use and for 0.1.0 compatibility.
- Capstone's own setting that names this config file belongs to Capstone. The Gateway requires only the file above.

Suggested layout on 192.0.2.201: `C:\ProgramData\Capstone\gateway\` holding `gateway-client.json`, `capstone.token` and `inference-gateway-ca.pem`.
- ACL: SYSTEM and Administrators full; `NT AUTHORITY\LOCAL SERVICE` (Capstone's service account) read; nobody else.

## 2. Routes

Every private route requires `X-Application-ID` and `X-Infrastructure-Token`. The SDK sends both.

| Route | SDK | Since | Scope |
|---|---|---|---|
| `GET /health` | – | 0.1 | Public liveness `{ok, contract, service_id}` |
| `GET /ready` | – | 0.1 | Public readiness. 200 = ledger usable and admission open; 503 = drained, stopping or unavailable. Says nothing about heads |
| `GET /v1/models` | `models()` | 0.1 | Caller-authorized, currently available, qualified models per head (§3) |
| `GET /v1/policy` | `policy()` | **0.3** | Caller's own operator-set policy plus per-model availability |
| `GET /v1/availability?model=&operation=&num_ctx=&capabilities=` | `availability(model, operation, num_ctx=, capabilities=)` | **0.3** | Pre-flight check: whether this request shape can run now, and the reason if not (§4) |
| `GET /v1/usage` | `request('/v1/usage')` | 0.1 | Own request counts and completed totals |
| `POST /v1/submit` | `infer(...)` | 0.1 | Idempotent asynchronous admission (202) |
| `POST /v1/inference` | `request('/v1/inference', body)` | 0.1 | Synchronous; prefer submit and poll |
| `GET /v1/requests/{id}` | `status(id)` | 0.1 | Owner-only envelope |
| `POST /v1/requests/{id}/cancel` | `cancel(id)` | 0.1 | Owner cancel. **Never stops compute already executing on a head** |

**Errors:**
- 400: invalid contract, field or bound, or **this application's** `queue_limit` reached (operator policy; not retried by the SDK).
- 403: credential, application, model, operation, context-policy or resource denial.
- 404: unknown route.
- 503: the Gateway's own limit is full, it is stopping, or it is briefly unavailable. Every 503 carries **`Retry-After`** (seconds) and `retry_after_seconds` in the body. Retry with the same `client_key` (submission) or `request_id` (status). *0.4.1:* a connection over `max_connections` gets this answer (before, it was closed without one); a full `max_pending` budget is 503 (before, 400).
- 504: synchronous timeout.

A submission retried with the same `client_key` and body is idempotent: the Gateway returns the request it already has (the same `request_id`), and a request refused with 503 for the pending budget is queued again under that ID.

Execution failures are terminal envelope states, not HTTP errors.

## 3. Discovery: `GET /v1/models`

Each `resources[]` entry has `resource_id` (`head-192-0-2-N`), `models` and `model_details[model]`.

Every field states its **evidence source**:

| Key | Source | Meaning |
|---|---|---|
| `digest` | measured | Installed model build. The only build identity; the runtime has no model version |
| `qualified_operations` | measured | `completion` and/or `embedding`: the operations that gate dispatch (0.1 meaning) |
| `capabilities`, `declared_capabilities.values` | **runtime_declared** | What `/api/show` claims (`tools`, `vision`, `thinking`, …). **Not proof** |
| `qualifications[purpose]` | measured | For each of `completion`, `embedding`, `chat`, `tools`, `structured_output`, `vision`, `context`: `state` is `passed`, `failed`, `invalidated` (with a `reason`) or `not_tested`; plus `evidence_id`, `at`, and where relevant `max_context`, `embedding_dimensions`, `fixture` |
| `qualified_capabilities` | measured | Purposes whose current state is `passed` |
| `descriptor` | runtime_declared | `name`, `tag`, `digest`, `family`, `families`, `parameter_size`, `quantization_level`, `format`, `architecture`, native `context_length`, `embedding_length`, `runtime_version`, `evidence_id`, `observed_at`; `model_version: null` |
| `execution_policy`, `policy` | operator_policy | `max_context` (qualified), `max_output_tokens` 8192, `allocation`: exclusive physical head |
| `availability` | gateway_observation | Head health and freshness at the moment of the call |
| `application_suitability` | not_assessed_by_gateway | Task suitability is the application's to establish |

A capability is `passed` only for the current digest, runtime version, endpoint and qualified context. Each of the following makes it `invalidated`:
- a new model digest;
- a new runtime version;
- a moved endpoint;
- a smaller requalified context;
- a later failed requalification (that one shows as `failed`).

## 4. Policy and availability

`GET /v1/policy` returns:

```json
{"contract":"shared.inference/1","application_id":"capstone",
 "policy":{"source":"operator_policy","enabled":true,"models":["…"],"max_concurrency":1,"queue_limit":16,"priority":6,
           "priority_aging_seconds":5,"max_output_tokens":8192,"max_deadline_seconds":3600,
           "operations":["/api/chat","/api/embed","/api/generate"],"max_context":null},
 "models":[{"model":"…","operations":{"/api/generate":"available","/api/embed":"not_qualified"}}]}
```

**Availability status values.** Overall statuses, then the per-head reasons in `resources`:

| Status | Meaning |
|---|---|
| `available` | At least one eligible head is idle |
| `busy` | Eligible heads exist; every slot on them is leased now. Occupancy only: whose lease is never disclosed |
| `model_suspended` | *0.4.0.* The model failed on several heads and is suspended on every head for this operation; the Gateway probes it and lifts the suspension when a probe passes |
| `model_suspended_on_resource` | *0.4.0, per head.* The model failed on this head and is suspended there for this operation; the head serves other models |
| `not_permitted` | Model not in this application's policy |
| `operation_not_permitted` | Operation outside this application's policy |
| `context_exceeds_policy` | `num_ctx` above this application's limit |
| `context_exceeds_qualification` | `num_ctx` above the head's qualified context |
| `capability_not_qualified` | A requested capability has no current passed qualification |
| `not_installed` | The head does not report the model |
| `not_qualified` | Installed, not qualified for this operation (or the head is new or moved) |
| `qualified_other_operation` | Qualified only for the other operation |
| `digest_changed` / `runtime_changed` | The head's build or runtime differs from the qualified one |
| `resource_unhealthy` / `resource_stale` / `inventory_stale` | Head offline, held (`quarantined`: uncertain execution), in maintenance or degraded, or not observed recently |
| `no_authorized_resource` | No head is authorized for this application |
| `unavailable` | Heads disagree; read the per-head `resources` map |

## 5. Submission

`infer(model, nodes, operation, payload, *, client_key=None, timeout=120, progress=None, expected_digest=None, required_capabilities=None)`

- **Body fields:** exactly `contract`, `client_key`, `model`, `nodes`, `deadline` (within one hour), `operation`, `payload`, and optional `expected_digest` and `required_capabilities` (0.3). Priority, application and concurrency fields are refused: priority is operator policy.
- **`nodes`:** `None` means `['@authorized']`, any eligible authorized head. `['head-192-0-2-3']` pins one head.
- **`expected_digest`:** pins the build. A changed head fails before inference; nothing is ever silently substituted.
- **`required_capabilities`:** a subset of `chat`, `tools`, `structured_output`, `vision`, `context`. Only heads with current passed qualifications are used; otherwise the request fails before inference.
- **Payload:** `prompt`, `messages`, `options`, `format`, `tools`, `think`, `input`, `truncate`, `images`.
- **Options:**
  - `num_predict` is 1–8192; the default is 600.
  - `num_ctx` is 512–65536, at most the qualified and application limit; the default is the smaller of the two.
  - Also allowed: `temperature`, `seed`, `top_p`, `top_k`, `repeat_penalty`, `stop`.
- **Idempotency:** one `client_key` per logical request. Reusing it returns the same request. Reusing it with any different content (including `expected_digest`) is refused with 400.
- **`unknown` status:** execution may have happened. Never resubmit it under a new key as if it had not.
- **Busy Gateway (SDK 0.4.1).** `infer()` keeps one deadline, `timeout` seconds from the call:
  - submission: each attempt waits up to 10 s; on a timeout, reset, refused connection or 429/502/503 it is sent again, unchanged (same `client_key`, same `deadline`);
  - status: polled at 0.25 s, then ×1.5 up to every 2 s (back to 0.25 s whenever the state changes); a status read that times out, is reset or gets 503 is repeated with the same `request_id`;
  - `Retry-After` is honoured; any other HTTP answer (400, 403, 404, 504) is final and raised at once;
  - at the deadline it cancels (best effort) and raises `TimeoutError`, as before. `client.retries` counts retried attempts.
  - `request()` and `status()` stay single-shot.

## 6. Result and provenance

The envelope: `contract`, `request`, `status`, the lifecycle times below, and `result` or `error`.

**Lifecycle times** (Unix seconds, the Gateway's clock; each key is absent until that step happened):

| Key | Since | Meaning |
|---|---|---|
| `submitted_at` | **0.4.1** | The request was accepted (= `request.created`) |
| `admitted_at` | **0.4.1** | A lease on `request.resource_id` was granted (the latest one if the request was re-placed) |
| `execution_started_at` | 0.2 | The inference call was sent to the head (= `result.started_at`) |
| `execution_finished_at` | **0.4.1** | The head's answer arrived (= `result.finished_at`) |
| `completed_at` | **0.4.1** | The result was durably accepted (status `completed` only) |

There is no `submitted_at`/`admitted_at`/`completed_at` inside `result`; `result` keeps `queued_at`, `started_at`, `finished_at`, `queue_ms`, `duration_ms` (below). **Same-head overlap:** two requests with the same `request.resource_id` overlapped when each one's `execution_started_at` is before the other's `execution_finished_at` (`tests/test_release_0_4_1.py`).
- `request.nodes` is JSON text.
- Status values:
  - invocation states: `active`, `completed`, `failed`, `cancelled`, `unknown`;
  - before an invocation exists: the request states `queued`, `leased`, `cancel_requested`, `expired`.
- Only `completed` carries an answer.

`result` keeps the 0.1 keys:
- `response`, which is the runtime's body with reasoning text removed;
- `resource_id`, `model`, `model_digest`;
- `queued_at`, `started_at`, `finished_at`, `queue_ms`, `duration_ms`;
- `lease_id`, `application_id`.

It adds **`provenance`** (0.3):

```json
{"request_id":"REQ-…","client_key":"exp42-trial7","operation":"/api/generate","requested_model":"m:1","expected_digest":"sha256:…",
 "executed_model":"m:1","executed_digest":"sha256:…","resource_id":"head-192-0-2-3","lease_id":"LEASE-…","runtime_version":"0.12.3",
 "qualification_evidence_id":"EVIDENCE-…","applied_options":{"num_predict":256,"temperature":0,"seed":7,"num_ctx":4096},
 "dispatch_controls":{"keep_alive":"2m","stream":false,"think":null},"required_capabilities":[],
 "capabilities_requested":[],"capabilities_verified":[],
 "timing_ms":{"admission_wait":12.1,"dispatch_checks":9.8,"gateway_queue":21.9,"model_execution":1803.2},
 "runtime_counters":{"prompt_eval_count":41,"eval_count":96,"total_duration":null,"load_duration":null,"prompt_eval_duration":null,"eval_duration":null},
 "unavailable_counters":["eval_duration","load_duration","prompt_eval_duration","total_duration"],
 "scope":"Gateway-measured: model_execution is the upstream call only …"}
```

**Timings.**
- `gateway_queue` is `admission_wait` (waiting for a lease) plus `dispatch_checks` (pre-dispatch verification).
- `model_execution` is the upstream call only. It excludes tool execution and application workflow time.
- Runtime durations are nanoseconds, or `null` when the runtime did not report them; `unavailable_counters` lists those.
- Leases are not hardware utilization.
- *0.4.4, additive keys in `timing_ms`:*
  - `ledger_wait`: time this request's worker waited for the Gateway's ledger, inside `gateway_queue`;
  - `runtime_load_or_runner_wait`: Ollama's `load_duration`, which includes waiting for a busy runner;
  - `runtime_prompt_eval`, `runtime_generation`, `runtime_total`: Ollama's own durations, in ms;
  - `transfer_and_other`: `model_execution` minus `runtime_total`.
  - A key is `null` when the runtime did not report its source. Response delivery to the client is not in the result; the Gateway logs a slow or abandoned delivery (`http.slow`, `http.client_gone`).
- *0.4.4:* `attempt` is this execution's attempt number. `attempts[]` lists `{attempt, resource_id, failure_class}` for every attempt, the last one being this execution (a request is re-placed once after a head or model fault, since 0.4.0). `model_substitution` is always `null`: the Gateway runs exactly the requested model, and refuses a reply that names another.

**Correlation with Arena.** Applications publish their own telemetry. Carry the Gateway `request_id` (and your `client_key`) in it. Arena's observation shows the same `request_id` in leases and events, so Arena can relate the two without merging the data sources.

## 7. Capstone (192.0.2.201)

- **Mode:** gateway mode with the config file above. No direct Ollama.
- **Discovery:** `models()` and `policy()`. Use `availability()` before scheduling an experiment arm.
- **Experiments:**
  - pin `expected_digest` and send explicit options;
  - use `required_capabilities` when an arm depends on tools, JSON schema, vision or long context;
  - record `provenance` per trial;
  - keep `gateway_queue` apart from `model_execution`.
- **Model fallback (0.4.4 rule).** The Gateway never substitutes a model. A busy model waits in the queue, or runs on another qualified head serving the same model. A different-model fallback is the caller's own new request, and it must be:
  - explicitly authorized in the role configuration;
  - checked as capability-qualified (`/v1/models`, `required_capabilities`);
  - recorded with requested model, executed model, reason and attempts;
  - kept out of the original model's results.
- **Concurrency.** Size per-model concurrency from `model_details[m].parallel.limit` summed over heads. Do not cut a campaign to one or two workers for a serial model: the Gateway queues the excess visibly (`queued`, `admitted_at - submitted_at`).
- **Research workload:** the Phase 6 Testing Lab uses its own identity, `capstone-research`. Its limits are the operator's (Agent Arena sets them through the bootstrap). On the lab since 2026-11-13: `capstone` and `capstone-research` both `max_concurrency` 100, `queue_limit` 500; the operator overrode this document's earlier advice of `max_concurrency` 1. Research and operational work then share all heads by priority and aging. Arena shows `capstone-research` as an application unless Arena registers that ID.
- **SDK:** ship `shared_inference_client.py` 0.4.1 (see the top of this document).
- **Suitability:** passing technical qualification is not task suitability. Evaluate clinical reasoning, curation and other tasks with Capstone's own suites, run as ordinary inference.

## 8. Coder (192.0.2.202)

Coder uses the same SDK and config form with `application_id: coder` and its own token file:
- its own model list, operations (for example generate and chat only) and context limit;
- its own concurrency, queue and priority;
- its own request namespace: it cannot read or cancel Capstone's requests, and the reverse holds (tested).

Heads are shared physically: one lease per head, so Coder and Capstone contend through the same queue and priority policy. Model suitability for coding is Coder's to establish.

## 9. Onboarding a future application (no Gateway code change)

1. **Choose an ID.** Pick an `app_id` matching `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`, identical to its Arena `app_id`.
2. **Declare its policy.** Add it to the approved policy with `credential: "provisioned"`, `models`, `max_concurrency`, `queue_limit`, `priority`, and optional `operations` and `max_context`. Add it to the `applications` of the heads it may use.
3. **Issue its token.** On the hub, as an administrator:
   ```text
   service_launcher.py credentials issue --database <ledger> --policy <policy> --app-id <id> --output <file> --reader <account>
   ```
   The token is never printed. Move the file to the application host into a folder only its service account can read, then delete the hub copy.
4. **Apply the policy.**
   ```text
   service_launcher.py bootstrap --database <ledger> --policy <policy>
   ```
5. **Configure the client.** Write the client config from §1 with the CA file.
6. **Test.**
   - `GET /health` and `GET /ready` with the CA;
   - `models()` and `policy()`;
   - one `infer()` on an approved model;
   - another application's request ID returns 403.
7. **Rotate or remove.**
   - Rotate with `credentials rotate --app-id <id> --output <new file>`; the old token stops working at once.
   - Remove with `credentials revoke --app-id <id>`. Queued work is cancelled, the head stays in service, and applying policy never re-enables it.

## 10. Representative sanitized exchange

```text
POST /v1/submit   X-Application-ID: coder
{"contract":"shared.inference/1","client_key":"build-1182-review","model":"m:1","nodes":["@authorized"],
 "deadline":1793750520,"operation":"/api/chat","payload":{"messages":[{"role":"user","content":"<redacted>"}],"options":{"num_predict":256}}}
202 {"contract":"shared.inference/1","status":"active","request":{"request_id":"REQ-9f…","status":"queued",…}}
GET /v1/requests/REQ-9f…   →  200 {"status":"completed","result":{"model_digest":"sha256:…","resource_id":"head-192-0-2-6","provenance":{…}}}
GET /v1/requests/REQ-9f…   (as capstone)  →  403
```
