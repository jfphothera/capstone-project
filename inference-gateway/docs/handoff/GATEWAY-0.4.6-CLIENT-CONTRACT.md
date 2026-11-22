# Inference Gateway 0.4.6 and client library 0.4.2: contract for Capstone

Written 2026-11-19 for the session that changes Capstone (6.0.0-rc.24 and later) at the same time. It covers what Capstone can use for G1 (cheap launch checks), G4 (upstream error detail), G5 (installed, qualified and available as separate states) and Phase 4 (qualified request shapes). Everything here is **additive**. The wire contract is still `shared.inference/1`. No existing route, field or meaning changes.

Sources of truth: `src/shared_inference_client.py` (client 0.4.2), `src/inference_gateway/capacity_gateway.py` and `capacity_gateway_server.py` (Gateway 0.4.6), tests `tests/test_release_0_4_6.py`.

## 1. Versions and compatibility

| Pair | Works | Notes |
|---|---|---|
| Client 0.4.1 with Gateway 0.4.6 | yes | Nothing changes for 0.4.1 calls. `/v1/models`, `/v1/policy` and `/v1/availability` keep their 0.4.5 shapes. `/v1/policy` gains two keys: `availability_included` and `observed_at`. |
| Client 0.4.2 with Gateway 0.4.6 | yes | The new calls below work. |
| Client 0.4.2 with Gateway 0.4.5 or older | yes | 0.4.1 calls are unchanged. `policy(availability=False)` falls back to the full policy. `gateway_info()` returns `release: None`. `fleet()` raises `GatewayFeatureUnavailable`, so use the 0.4.1 calls instead. |

Feature detection: `Client.gateway_info()` (`GET /health`, no extra cost). On 0.4.6 it returns `{'release': '0.4.6', 'features': ['policy_without_availability', 'fleet_snapshot', 'upstream_error_capture', 'structured_chat_qualification']}`. Check a feature name, never a version string comparison.

The client file ships in the Gateway release at `client/shared_inference_client.py` (version `0.4.2`). Capstone vendors it as before.

Rules:
- Treat every field added below as optional. Unknown keys must be ignored, because the Gateway may add more.
- Never treat an absent snapshot field as a negative fact. For example, a missing `upstream_errors` key means no head error was recorded. It does not mean "no error".
- A snapshot is advisory. Admission and dispatch re-check authorization, digest, qualification, health and suspension on every real request. A snapshot can never make a request run. It can only tell the application not to launch one.

## 2. G1: launch checks that do not scale with the catalog

### 2.1 `GET /v1/policy?availability=0`, client `policy(availability=False)`

Returns the operator policy only. No head, inventory or qualification is read. The reply has the same `policy` block as before. `models` is `[{'model': name}, ...]` with no `operations`, and `availability_included` is `false`.

Plain `GET /v1/policy` (client `policy()`) is unchanged in shape. In 0.4.6 its per-model availability comes from one read snapshot instead of 72 full availability calculations. On the laptop fixed workload its p50 fell from 4.8–9.2 s to 0.10–0.17 s. Capstone should stop calling the full policy in the per-worker launch path.

### 2.2 `GET /v1/fleet`, client `fleet(models=None, *, operations=None, num_ctx=None, capabilities=())`

This is the application-scoped campaign snapshot, scoped to what the trial will use. Query parameters, all optional, lists comma-separated:

| Parameter | Default | Meaning |
|---|---|---|
| `models` | every permitted model | the models to report, up to 256 |
| `operations` | `/api/generate,/api/embed` | any of `/api/generate`, `/api/chat`, `/api/embed` |
| `num_ctx` | none | the context the request will ask for (512–65536) |
| `capabilities` | none | required capabilities: `chat`, `tools`, `structured_output`, `vision`, `context`, `structured_chat` |

Reply (abridged):
```json
{"contract": "shared.inference/1", "application_id": "capstone-research",
 "snapshot": {"snapshot_id": "FLEET-…", "generated_at": 1795…, "served_at": 1795…, "valid_until": 1795…+5, "max_age_seconds": 5,
              "scope": "application", "served_from_cache": false, "authoritative": false, "note": "Advisory: …"},
 "request": {"models": ["gemma3:4b"], "operations": ["/api/chat"], "num_ctx": 8192, "capabilities": []},
 "models": [{
   "model": "gemma3:4b", "authorized": true,
   "installed": {"state": "installed", "resources": ["head-192-0-2-2", "…"], "inventory_fresh": true},
   "qualified": {"state": "qualified", "resources": ["…"], "capabilities": ["structured_output"], "max_context": 8192},
   "available": {"status": {"/api/chat": "model_suspended"}, "any": false},
   "resources": {"head-192-0-2-2": {
       "health": "healthy", "fresh": true,
       "inventory": {"installed": true, "digest": "68ceab7e…", "observed_at": 1795…, "fresh": true},
       "qualification": {"listed": true, "operations": ["completion"], "states": {"completion": {"state": "passed", "…": "…"}},
                         "qualified_capabilities": ["…"], "max_context": 8192, "digest": "68ceab7e…"},
       "availability": {"/api/chat": "model_suspended"},
       "parallel": {"limit": 1, "source": "unmeasured_default", "reason": "no_current_evidence"},
       "suspension": "model_suspended"}}}]}
```

The three states are separate (G5):
- `installed.state`: `installed` (present in at least one authorized head's observed `/api/tags`), `not_installed` (every authorized head's inventory is fresh and none has it), or `unknown` (some inventory is stale or there is no authorized head).
- `qualified.state`: `qualified` (an operation passed on the current digest and runtime on at least one head that lists it), `not_qualified` (installed but no current pass), or `not_applicable` (not installed).
- `available.status[operation]` uses the **same vocabulary as `/v1/availability`**, so there is no second status system: `available`, `busy`, `model_suspended`, `model_suspended_on_resource`, `resource_held`, `resource_unhealthy`, `resource_stale`, `inventory_stale`, `not_installed`, `not_qualified`, `qualified_other_operation`, `digest_changed`, `runtime_changed`, `context_exceeds_qualification`, `capability_not_qualified`, `operation_not_permitted`, `context_exceeds_policy`, `no_authorized_resource`, `unavailable` (mixed reasons, see `resources`). `available.any` is true when some operation is `available` or `busy`.
- A model the identity is not permitted to use returns only `{"model": …, "authorized": false, "status": "not_permitted"}`. Nothing about heads or inventory is disclosed. Other applications' work appears only as occupancy (`busy`), never as identities.

The Gateway caches the snapshot per application and scope, for at most `max_age_seconds` (5 s), and only while every input is unchanged. The inputs are: the client's policy row and limits; head health, freshness, applications and endpoint; inventory; qualification evidence; suspensions; occupancy; slots; capacity evidence; and placement policy. So 24 workers launching together get one computation, and any revocation, suspension, digest change or new qualification produces a new `snapshot_id` immediately. `fleet_cached()` additionally reuses the reply inside one process only for what is left of it: `valid_until - served_at` (both on the Gateway's clock).

### 2.3 Recommended Capstone launch path (replaces models + policy + per-model availability)

1. Once per process (or per settings check): `gateway_info()`. Use `fleet` if `'fleet_snapshot' in features`, else the 0.4.1 path.
2. Per trial launch: `fleet(models=[the trial's routed models], operations=[the operations the routes use], num_ctx=<route num_ctx>, capabilities=<route required_capabilities>)`. That is one call, scoped to the trial.
3. Per real request: no extra preflight is needed. Submit with `required_capabilities` and `expected_digest` as today. The Gateway checks again at admission and dispatch. If Capstone keeps `check_availability=True` in `gateway_transport.invoke`, that is one cheap `/v1/availability` per call. It is not required for correctness.
4. Use `policy(availability=False)` where only the operator policy is needed (max_concurrency, models, limits).

## 3. G5 messages Capstone should derive

| Fleet entry | Show |
|---|---|
| `installed.state == 'installed'` and every head's `availability[op]` is a suspension reason | "Installed, temporarily unavailable: suspended after an upstream failure (automatic recovery probe pending)". Not "not installed". |
| `installed.state == 'not_installed'` | "Not installed on any head this identity may use" |
| `installed.state == 'unknown'` | "Inventory not fresh; installation not confirmed" (retry later) |
| `qualified.state == 'not_qualified'` | "Installed but not qualified for this operation on its current build" |
| `available.status[op] == 'busy'` | "Available, all eligible slots busy now (it will queue)" |
| `capability_not_qualified` / `context_exceeds_qualification` | The specific capability or context reason. Do not emit them when the model record is absent for another reason. |
| `authorized == false` | "Not permitted for this identity" |

Do not fabricate qualification or context errors from an empty model record.

## 4. G4: what the head said when it refused a request

When a head answers a request with an HTTP error, the status envelope (`GET /v1/requests/<id>`, and the reply of `infer()`) gains:
```json
"upstream_errors": [{"attempt": 1, "resource_id": "head-192-0-2-10", "status": 500, "phase": "dispatch",
                     "failure_class": "model_on_head", "message": "<the head's error text, ≤1000 chars, redacted>",
                     "body_truncated": 0, "digest": "68ceab7e…", "runtime_version": "0.40.15", "at": 1795…}]
```
- The key is present only on settled requests (not `active`), and only when a head answered with an HTTP error. There is one row per attempt. A request re-placed after a 5xx and then completed elsewhere still lists the first attempt.
- `error` keeps its 0.4.5 text (for example `HTTP Error 500: Internal Server Error`). Do not parse `message` for control flow. Record it as diagnostic evidence next to the call.
- The failure class and its action are unchanged: 4xx is `request_fault` (request only), 5xx is `model_on_head` (that model is suspended on that head; the request is re-placed once if possible).
- The full body (≤4 KiB) and the request shape (fields, message and role counts, sizes, format kind, schema property names and hash, tool names, options; never content) stay in the Gateway's protected ledger table. The operator can read them with `lifecycle upstream-errors`. The observation file carries counts only (`last_hour.upstream_http_errors`).

## 5. Phase 4: qualified request shapes

| Capstone strategy / role shape | Gateway capability to require | Qualified by |
|---|---|---|
| Native tool calling (`/api/chat` with `tools`) | `tools` | existing fixture (one exact tool call) |
| Structured role on chat (`/api/chat` with a JSON-schema `format`): customer_communication, reviewer, QMS/ERP research, and a **mediated** tool strategy that asks for a JSON action | **`structured_chat` (new)** | system plus user messages, closed schema, exact JSON reply |
| Structured generate (`/api/generate` with `format`) | `structured_output` | existing fixture |
| Plain chat | `chat` | existing fixture |

- `structured_chat` is new in 0.4.6. It appears in `qualifications`, `qualified_capabilities`, `required_capabilities` and the fleet. Until the operator runs `qualify --purpose structured_chat` on a head, its state is `not_tested`. Requiring it then makes that head ineligible, and the reason is `capability_not_qualified`. Capstone should therefore send `required_capabilities=['structured_chat']` only after the lab has qualified it, or show the incompatibility at preflight. That decision belongs to the operator and Capstone; the Gateway does not qualify anything by itself.
- Result provenance (`result.provenance.capabilities_requested`) now also names `structured_chat` for chat requests with a `format`. `capabilities_verified` lists it only where it passed.
- A model without native tools (gemma3:4b in c922: 21 native attempts, 0 completed) shows `tools` not passed. Capstone must resolve the strategy before launch (T1). The Gateway never switches strategy or model.
- Passing `structured_chat` on the synthetic fixture does not prove a specific role's real payload works. The c922 Gemma 500s used this shape. See the G4 reproduction tool.

## 6. Request validation tightened in 0.4.6 (400 before any request exists)

- `tools`: a list of at most 64 entries, each `{"type": "function", "function": {"name": str, "description": str, "parameters": object}}` (`type` may be omitted). Anything else is a 400 request fault. In 0.4.5 it went to the head.
- `images`: a list of strings.
- Options: `temperature`, `top_p` and `repeat_penalty` must be finite numbers. `seed` and `top_k` must be whole numbers; `7` and `7.0` are both fine. `stop` must be a list of up to 16 strings. Ollama answers a wrongly typed option with HTTP 500, which would otherwise suspend a healthy model on the head.
- Capstone's current requests (c922: 248 tool entries, all options integers) already satisfy these rules.

## 7. Unchanged

- Submit, status, cancel and retry semantics, idempotency (`client_key`), deadlines, 503 and `Retry-After`.
- `/v1/models` keys and meaning (a suspended model still drops out there; the fleet is where it is visible).
- The observation file format `shared.inference.observation/1`. Additions are listed in `docs/RELEASE-NOTES-0.4.6.md`.
