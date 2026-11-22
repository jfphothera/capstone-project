# Compatibility contract

Wire version: **shared.inference/1** (unchanged). Package release: **0.4.0** (additive over 0.1, 0.2 and 0.3; failure classes and placement in [FAILURES-AND-PLACEMENT.md](FAILURES-AND-PLACEMENT.md); the released 0.1.0 SDK still works, see `tests/test_cross_version.py`). The full client integration contract, with every field's evidence source, is [CLIENT_CONTRACT.md](CLIENT_CONTRACT.md), pinned by `tests/test_client_contract.py`. Python baseline: CPython 3.12.10 on Windows x64. No Capstone or Arena runtime/database dependency exists. The SDK retains `from shared_inference_client import Client`, `from_config`, `infer`, `status`, `cancel`, `models` and `request` without changes. Existing request/result, SQL table, event and authentication names are preserved.

Every private route requires `X-Application-ID` and `X-Infrastructure-Token`. Tokens are checked against hashes in the operator-owned ledger. An application cannot register clients, change resource ownership, increase priority/concurrency, qualify heads, or access another application's request/result. Client registration and maintenance remain host-side administrative functions. TLS certificate verification authenticates remote origins; `X-Inference-Service-ID` additionally detects a misconfigured logical service. The header alone is not cryptographic authentication. Redirects and remote plaintext are refused by the SDK.

| Method / route | Scope |
|---|---|
| GET `/health` | Existing public liveness: `ok`, contract, service ID |
| GET `/ready` | Additive public readiness: `ready`, contract, service ID; 200 when ledger is readable and admission open; 503 when drained or ledger unavailable |
| GET `/v1/models` | Authenticated caller-authorized healthy/fresh heads and qualified models, digests, operation/context policies |
| GET `/v1/policy` | 0.3: the caller's own operator-set policy (models, operations, context, concurrency, queue, priority) and per-model availability |
| GET `/v1/availability` | 0.3: pre-flight check for one request shape (`model`, `operation`, `num_ctx`, `capabilities`), with a reason code per authorized head |
| GET `/v1/usage` | Authenticated caller request counts and completed output tokens/duration; unknown work is not reported as zero usage |
| POST `/v1/submit` | Idempotent asynchronous inference admission; 202 envelope |
| POST `/v1/inference` | Existing synchronous execution interface; asynchronous submit/poll preferred for recovery |
| GET `/v1/requests/{id}` | Owner-only durable request/result envelope |
| POST `/v1/requests/{id}/cancel` | Owner cancellation; never unloads/preempts active remote compute |

Readiness means the process can use its ledger and accept admission. An empty qualified pool can be ready; it does not imply an inference head is available. Check the authenticated model inventory before assigning workload. `/ready` reveals no resource, client, request, credential or result data. Database errors use the existing generic 503 envelope. `/health` remains compatible.

Submission permits only `contract`, `client_key`, `model`, `nodes`, `deadline`, `operation`, `payload`, `expected_digest`, and since 0.3 `required_capabilities`. Per-application `operations` and `max_context` (operator policy) are refused with 403 before any request exists. A refusal caused by the client (context, pin, capability, revocation) never takes a head out of service. `deadline` is Unix UTC time in the next hour. Operations are `/api/generate`, `/api/chat`, `/api/embed`. Payload cannot choose a model, streaming, residency, unloading, arbitrary upstream path or application priority. Generation is bounded to 1–8192 output tokens and an operator-qualified context (default qualification 1024); input is bounded to 1 MiB and upstream response to 16 MiB. See `Gateway.validate` for the preserved sampling-option allowlist.

The SDK's `nodes=None` selects `['@authorized']`, dynamically resolving only caller-authorized, healthy, qualified capacity. Explicit resource IDs remain pinned. No LAN scan or endpoint discovery grants permission. Different payload/model/node-policy reuse of a client key is rejected. Reuse the same key after transport loss and query status; never blindly retry unknown execution with a new identity.

The envelope includes `contract`, durable `request`, `status`, optional `execution_started_at`, private `result` or error. `request.nodes` is the ledger's JSON text, not a list, and `request.executing` is 0/1. Reasoning text (`thinking`) is removed from stored responses. A completed result includes response/model/digest, resource and lease IDs, application ID, queue/start/finish timing. Errors are not successful model answers. HTTP 400 means invalid contract/input or exhausted queue/pending budget; 403 means credentials/namespace/resource denial; 404 unknown route; 503 unavailable; 504 synchronous timeout. Connection exhaustion may close a socket before parsing. Execution errors appear as terminal states in otherwise valid envelopes.

The sole ledger serializes admission with SQLite transactions. Since 0.4.0 a head takes up to its operator-set `slots` concurrent leases (default 1, the 0.3.0 behaviour), enforced in the ledger by slot triggers that replace the 0.3.0 unique active-resource index. Each lease's model must fit in the head's `memory_gb` together with what is loaded, and a load never unloads a model an active lease uses. This is lease and memory accounting from `/api/ps` and `/api/tags`, not CPU/VRAM measurement. Placement is one documented, deterministic order (model already loaded, then fits in free memory, then may evict idle models only; ties by fewest leases, least recently assigned, resource ID), replacing "first free head in rowid order". Application concurrency/queue limits and priority 0–10 are operator-set and unchanged; priority ages every five seconds. No application identity has special scheduling privilege. No workflow orchestration, application tool execution or MCP endpoint is exposed.

Leases renew every ten seconds, at most 60 seconds ahead and never beyond request deadline. Qualification runs under the same head reservation. Matching digest, service version, operation, endpoint and context are required before dispatch. Health refresh polls only enrolled origins every 15 seconds; stale availability older than 120 seconds is ineligible. A moved endpoint invalidates qualification and cannot move while leased. Inventory cannot enroll unknown heads. Exclusive development/test devices and the infrastructure computer must not join the inference pool.

Cancellation preserves executing reservations until the operation returns or expires; late answers are discarded.

**No replay (unchanged principle): work that may have executed is never re-run.** Since 0.4.0 every failure of a dispatched call has exactly one class, and only that class's action is taken:
- **Request fault** (HTTP 4xx, client-policy refusal): the request fails; head and model untouched.
- **Model-on-head fault** (HTTP 5xx, invalid reply): that model is suspended on that head for that operation; the head keeps serving other models.
- **Head unreachable** (no connection before the request was sent): the head is offline until its heartbeat returns.
- **Uncertain execution** (timeout, reset or truncation after sending; an executing lease expiring): the whole head is held. This is the only class that takes a head out.

Any HTTP answer is definite: nothing stays executing. Only in the model-on-head and unreachable classes, where nothing ran, is the same request (same ID, key and deadline) re-placed once on another head. An uncertain request ends failed or unknown and is never replayed.

Suspended and held items recover automatically:
- A held head waits until the original deadline has passed and `/api/ps` shows no loaded model.
- Then the Gateway's own qualification probe runs: pass returns it, failure backs off 1, 5 and 15 minutes, then `needs_attention` with an alert.
- The same model failing on two heads within 10 minutes is suspended everywhere.
- Operators may still release by hand (`lifecycle release --confirmed`) or reconcile and requalify (`reconcile-resource --confirmed-idle`).

Restart marks interrupted invocation ownership unknown, cancels orphaned queued work, and never reconstructs/replays prompt bodies. In-flight leases persist until expiry, which holds the head. Completed responses remain private in the ledger: protect the database and backups accordingly.

Client bootstrap template: `config/client.template.json`. `https://192.0.2.254:REPLACE_WITH_APPROVED_PORT` is an unusable deployment placeholder, not an allocated endpoint or a claim TLS exists. Actual bind, advertised URL, protocol and port are configurable. Only loopback development allows HTTP. Preserve service/application/resource IDs during relocation.
