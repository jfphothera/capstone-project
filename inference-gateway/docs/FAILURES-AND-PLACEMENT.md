# Failures and placement (Gateway 0.4.0, amended in 0.4.1, 0.4.2, 0.4.3 and 0.4.4)

> **0.4.4 (2026-11-17), see [RELEASE-NOTES-0.4.4.md](RELEASE-NOTES-0.4.4.md):**
> - Every ledger write goes through one FIFO, bounded, timed path (`CapacityBroker.write`). HTTP writes wait at most 5 s, then answer 503 with `Retry-After`. A finished result and a failure's settlement are retried through a busy ledger.
> - Indexes end the history scans inside the write lock.
> - The per-model limit comes from evidence that names the runtime and the build: Arena's declared `model_capacity`, or the Gateway's measurement (`qualify --parallel N`). Each counts only while the head's runtime version and the model's digest match. Without it: serial families 1, other models `slots`.
> - A client that gave up is logged as `http.client_gone`, not `http.unavailable`.

> **0.4.3 (2026-11-14), see [RELEASE-NOTES-0.4.3.md](RELEASE-NOTES-0.4.3.md):**
> - Placement is least-busy first (`placement_policy.strategy`).
> - A head takes at most one lease of a model Ollama runs one call at a time (`serial_families`, default qwen35 and qwen35moe; or `model_parallel`).
> - A call that reaches its own deadline on a head that still answers fails alone, with class `deadline_exceeded`; the head is not held.
>
> The sections below are amended where they changed.

> **0.4.1 changes** (field evidence 2026-11-12/13, [RELEASE-NOTES-0.4.1.md](RELEASE-NOTES-0.4.1.md)): admission is one scheduler pass per event, not each waiting request's 50 ms write-lock spin (section 3); a renewal failure never expires a lease this process is still running; the completion test asks whether the model answers (section 2, step 3; 0.4.2: whether it produced output, reasoning included); a failed qualification withdraws only the purpose it tested and never degrades the head, and a test that was never sent changes nothing (section 1).

0.4.0 changes two things:
- **What counts as a failure.** One failed call no longer takes a whole head out of service.
- **How work is spread across heads.** "First free head in rowid order" is gone.

Everything else is unchanged:
- the wire contract `shared.inference/1`;
- the observation contract `shared.inference.observation/1`;
- request states and error codes;
- application `max_concurrency`, queue limits, priority and aging;
- the no-replay rule.

Code: `recovery.py` (classes, suspensions, recovery, operator commands), `placement.py` (head choice), `capacity_gateway.py` (`_attempt`, `_settle`, `run_recovery`), `capacity_broker.py` (`admit`, `_expire`, slot triggers, upgrade). Tests: `tests/test_failures_and_placement.py`, `tests/test_live_ollama.py` (opt-in, real Ollama).

**Why.** On 2026-11-11 (Arena evidence `gateway-routing-20261111/report.txt`), gemma3:270m answered a request on head 3 within a second, and 0.3.0 quarantined the head. The lease then expired and quarantined it a second time. The same happened on head 5. Every request had gone to head 3, the first head in rowid order, while heads 7–10 and 2 sat idle. The common factor was the model, not the heads.

## 1. Failure classes, as built

Every failure of an execution attempt is put in exactly one class by `recovery.classify(exception, phase)`. Only that class's action is taken.

The phases are:
- `admission`: no head yet.
- `checks`: leased; the read-only `/api/tags`, `/api/show` and `/api/version` calls before dispatch.
- `dispatch`: the inference call is being sent or awaited.
- `answered`: a reply came back.

urllib wraps every error raised while connecting or sending in `URLError`. Errors after the request was written are raised raw. `HTTPError` is raised for every 4xx/5xx reply, and it is both a `URLError` and an `OSError`, so it is tested first.

| Exception or reply (phase) | Class | Action |
|---|---|---|
| `ClientPolicyError`, any phase (digest pin, context, capability, model authorization withdrawn) | request fault | Fail this request only. Head and model untouched |
| `HTTPError` 400–499 (dispatch) | request fault | Same. `executing=0` at once |
| `HTTPError` 500–599, and any other non-4xx code (dispatch) | model-on-head | Suspend this model (name + digest) on this head for this operation. `executing=0` at once. The head keeps serving its other models. Re-place the request once |
| A reply that fails `validate_response`, or names another model (`InvalidReply`, answered) | model-on-head | Same |
| A complete reply that is not JSON, or exceeds 16 MiB (`ValueError`, dispatch) | model-on-head | Same |
| `URLError` that is not an `HTTPError`, or `ConnectionRefusedError` (dispatch): refused, no route, or reset while sending | head unreachable | Health `offline` (event `resource.health_changed`). The next heartbeat that answers brings it back. `executing=0`. Re-place the request once |
| `URLError`, `ConnectionError`, `TimeoutError`, `http.client.HTTPException` on the read-only pre-dispatch calls (checks) | head unreachable | Same |
| *0.4.3:* `TimeoutError` at or after the request's own deadline (dispatch), and the head answers `/api/version` within `deadline_liveness_seconds` (3 s) | deadline exceeded | Fail this request only. No hold, no fault, no replay. `executing=0`. Events `invocation.deadline_exceeded`, `invocation.failed {class: deadline_exceeded}`. `deadline_liveness_seconds: 0` turns this off |
| `TimeoutError`, `ConnectionResetError`, `http.client.IncompleteRead`, other `OSError` or unknown exception after the request was sent (dispatch); *0.4.3:* including a deadline timeout on a head that does not answer | uncertain execution | Hold the whole head (health `quarantined`) until the request's deadline has passed, then recover as in §2. `executing` stays 1, so the lease keeps its slot until it expires. Never replayed |
| An executing lease expires with no answer (`_expire`) | uncertain execution | Hold. An existing hold is extended, never re-raised or shortened. *0.4.3:* not while this process is still running the call; that call settles itself at its deadline (row above) |
| Any other failure in checks (e.g. model no longer installed, version changed), or admission timeout | none (as 0.3.0) | Request fails. A failed pre-dispatch check marks the head `degraded`, and the heartbeat re-validates it within one interval |
| A late or cancelled result after an answer (answered) | none | Request ends as 0.3.0. Head untouched, `executing=0` |

Rules for all classes:
- **Any HTTP answer is a definite answer.** `executing` is set to 0 in the same transaction that records the failure. The lease is released by `finish`, so it cannot expire into a hold afterwards. On 2026-11-11 the lease expiry caused the second quarantine; that path is closed.
- **A hold is never downgraded or skipped** by a later event. A second uncertain event on a held head only extends `hold_until`. `needs_attention` stays `needs_attention`.
- **Only uncertain execution holds a whole head.** *0.4.3:* a call that only ran out of its own time on a head that still answers is not uncertain execution.
- **Qualification uses the same classes.** Only an uncertain dispatched qualification call holds the head. *0.4.1:* a test refused before it is sent (wrong purpose for the model, not installed, no digest, no current completion base, deadline) is recorded with `refused: true` and changes nothing: not the model list, not the head's health, not the other purpose. A test that ran and failed withdraws only that purpose; the model stays listed while its other operation purpose's newest test passed on the same digest and runtime. Head health after a qualification is `healthy` while the head serves any qualified model (0.4.0: `degraded` after any failed test). A purpose counts while its **newest** test passed: a later failure withdraws it until requalified. 0.4.0 rows of refused tests (no flag, no response, a pre-check error) are recognised and ignored too.

**Re-placing the request.** For model-on-head and head-unreachable faults, nothing ran, so the same request may run on another head. It keeps the same request ID, client key and deadline. This happens once, on attempt 1 only. The conditions:
- the request is still leased (not cancelled) and before its deadline;
- the client is not revoked;
- at least one other authorized, healthy, fresh, qualified and unsuspended head exists. It may be busy; the request then waits in the queue as today.

The ledger records the attempt (`capacity_request_attempts`) and the request never returns to a head it failed on. Event `request.replaced` is emitted, and the request is briefly `queued` again. Capstone sees one request ID and one completion.

Request faults are never retried. Uncertain execution is never replayed; the request ends `failed` (or `unknown` after a restart), as today.

### Real Ollama replies (verified locally, not on the lab version)

`tests/test_live_ollama.py` ran through the Gateway's real urllib transport against a local **Ollama 0.40.16** (the lab runs 0.40.13; head 2 runs 0.40.12). Agent Arena must repeat it on a head (open item O8).

| Request | Reply | Class |
|---|---|---|
| `/api/generate`, unknown model | 404 `model '…' not found` | request fault |
| `/api/chat`, `messages` not a list | 400 `json: cannot unmarshal …` | request fault |
| `/api/generate`, `options.num_ctx: "abc"` | **500** `option "num_ctx" must be of type integer` | model-on-head |
| `/api/embed` on a completion-only model | **501** `This server does not support embeddings…` | model-on-head |
| Connection refused (`127.0.0.1:9`) | `URLError(ConnectionRefusedError)` | head unreachable |
| Generation still running after a 1.5 s timeout | raw `TimeoutError` from the reply read | uncertain |

Two mismatches with "4xx = request, 5xx = model":
- Ollama answers a wrong option type with 500. The Gateway's `validate()` refuses non-integer `num_ctx`, unknown options and out-of-range values before any request exists, so this cannot reach a head through the Gateway. The live test asserts that too.
- An embedding call on a model without embedding support gets 501. The Gateway checks `/api/show` capabilities before dispatch, so this is a model or head inconsistency, and model-on-head is right.

A 404 "model not found" at dispatch means the model vanished between the `/api/tags` check and the call. It is classed as a request fault, as the rule says. The next heartbeat withdraws the model from that head (`inventory.model_removed`, `qualification.invalidated`).

## 2. Suspensions, holds and automatic recovery

A **suspension** (`capacity_suspensions`) has one of three scopes:

| Scope | Created by | Stops | Head |
|---|---|---|---|
| `resource_model` | model-on-head fault | that model+operation on that head | serves everything else |
| `model` | cascade rule | that model (name + digest) + operation on every head | every head serves everything else |
| `resource` (a **hold**) | uncertain execution, upgrade | the whole head (health `quarantined`) | out of service |

A suspension's state is `suspended` (model scopes), `held` (head), or `needs_attention`. It ends as `recovered` (probe passed), or `released` (operator release, or `reconcile-resource`). Operations are the qualification purposes: `completion` (generate and chat) and `embedding`.

**1. Stop normal traffic.** Placement, `/v1/models`, `/v1/availability` and the re-placement check all skip suspended items. Other heads keep serving Capstone and Research Lab.

**2. Wait until it is safe (held heads only).** All of these must hold:
- the original request's deadline (`hold_until`) has passed;
- no reservation is active on the head;
- the head answers `/api/version`;
- **two `/api/ps` reads `ps_settle_seconds` (2 s) apart both list no loaded model.**

Why this signal is safe. Ollama has no in-flight counter. These facts were measured on 0.40.16:
- A runner that is serving a request is never unloaded. A model loaded with `keep_alive: 2s` stayed in `/api/ps` about 30 s past its `expires_at` while a generation ran, and disappeared 2 s after it finished.
- An idle runner is unloaded when its keep_alive ends. The Gateway sends `keep_alive: 2m` on every call, qualification and probe, so an idle held head empties within about 2 minutes of its last request.
- When the client gives up (the Gateway's call reached its deadline), Ollama abandons the request: the runner went idle at the moment of disconnect.

So an empty `/api/ps` means no runner, and no runner means no request executing. No clock comparison between hub and head is needed.

A signal that does **not** work: `expires_at` does not move while an already-loaded runner serves a request. It only reads now + keep_alive while a runner is busy with the request that loaded it.

Limits of the signal:
- A model that is still loading is not listed in `/api/ps`. The deadline rule covers that case, because the Gateway closed its connection at the deadline.
- A head configured to keep models loaded indefinitely never looks safe. It escalates (below).

While not safe, the suspension records `waiting` (`deadline`, `active_reservation`, `runner_loaded`, `head_unreachable`) and the head is looked at again after `safe_recheck_seconds` (15 s). A held head that is still not safe `hold_max_wait_seconds` (30 min) after its deadline becomes `needs_attention`, with reason `not_safe_to_probe`.

**3. Test it.** The Gateway runs its own probe, which is the existing qualification test:
- completion (0.4.2, also the qualification test): `/api/generate` "Return exactly the word READY.", `num_predict` 128, temperature 0, seed 42, `think` false, `num_ctx` = the qualified context. It passes when the model **produced output for the prompt**: `done` and `eval_count` > 0 (non-empty output when a runtime reports no `eval_count`), whether `done_reason` is `stop` or `length` and whether the output is still inside a reasoning block. The operator's rule: a model that is reasoning is a working model. Recorded in `measurement`, never required: `answered` (text after the reasoning), `reasoning`, `finished_reasoning`, `answer_matches_ready` (case and punctuation ignored), `done_reason`, `eval_count`. 128 tokens keep a CPU head inside the 90 s deadline (deepseek-r1:14b from a cold load: 62 s on the development laptop). History: 0.4.0 required exactly `READY` within 24 tokens; 0.4.1 required `done_reason` `stop` and an answer after the reasoning, which failed DeepSeek-R1 (still reasoning at 128 tokens).
- embedding: the qualification embedding fixture, passing a valid finite embedding.

The probe runs on that head and model, for the suspended operation. It runs under a maintenance lease (`app_id` `maintenance:gateway-recovery`, request ID `PROBE-…`) that takes one slot, so no client traffic is placed in that slot meanwhile.

Before calling, the probe checks that a passed qualification still matches the head's current digest, runtime version and endpoint. If not, the probe fails with `qualification_not_current`, and requalification is the operator's step.

For a **held head**:
1. Probe the smallest model (by `/api/tags` size) that has a current qualification on that head, other than the failed model.
2. Then probe the failed model, for its failed operation.

The outcomes:
- Both pass: the head returns (`resource.recovered`). Its qualified models are re-checked against the head's digests and runtime.
- The small model passes and the failed model fails with an answer: the head returns, and only that model is suspended on it. This counts toward the cascade rule.
- The small model fails, or a probe call is uncertain: the hold stays, and the probe counts as failed.

**4. Pass.** `probe.passed`, then `model.recovered` (model scopes) or `resource.recovered` (holds). Traffic returns automatically.

**5. Fail.** `probe.failed`. The next probe follows the backoff `probe_backoff_seconds` = **[60, 300, 900]**:
- probe 1 runs 60 s after the suspension begins (for a hold: 60 s after `hold_until`);
- probe 2 runs 5 min after probe 1 fails;
- probe 3 runs 15 min after probe 2 fails.

If probe 3 also fails (three failed probes by default), the state becomes `needs_attention` (`resource.needs_attention`). It stays out and appears in `alerts[]`. No more automatic probes run.

A probe that cannot run is not counted as failed. It waits: head unreachable, no free slot, or no head to probe a model-wide suspension on.

**6. Stop the cascade.** If the same model (name **and** digest) has model-on-head faults for the same operation on `cascade_heads` (2) different heads within `cascade_window_seconds` (600 s):
- it is suspended for that operation on every head (scope `model`, `model.suspended`), and appears in `alerts[]`;
- the heads stay in service for their other models;
- it is probed on one eligible head on the same schedule;
- the per-head suspensions keep their own schedules.

On 2026-11-11 this rule alone would have kept heads 3 and 5 in service; `test_replay_2026_11_11_…` replays it.

**Operator.** `lifecycle reconcile-resource --confirmed-idle` still works. It also closes the head's hold as `released`. The new commands are in §6. A manual release still works from `needs_attention`.

## 3. Placement

`placement.View.choose`, inside the broker's `BEGIN IMMEDIATE` admission transaction (`CapacityBroker.place`). *0.4.1:* one `Scheduler` thread per Gateway runs that pass once per event (a submission, a release, a health/qualification/recovery change) and once a second while anything waits; waiting requests block on their own event and read their row outside the write lock. 0.4.0 had every waiting request call `admit()` every 50 ms (30 waiting = about 600 exclusive locks a second).

**Eligible heads:**
- healthy and fresh: `last_seen` within 120 s, as today;
- the application authorized; the model qualified for operation, digest and context, as today. For `@authorized` requests the Gateway filters by operation, digest pin, capability and context;
- not held; that model not suspended on that head, nor everywhere;
- not tried before by this request;
- a free slot: active leases (including maintenance and probe leases) below `slots`.

**Per-model limit (0.4.3).**
- A head is also not eligible when it already holds N leases of the requested model:
  - N = `placement_policy.model_parallel[model]`, if set;
  - otherwise 1, when the model's family (the head's `/api/tags` `details.family` / `details.families`, stored in `capacity_inventory_runtime.families`) is in `placement_policy.serial_families` (default `qwen35`, `qwen35moe`);
  - otherwise no per-model limit beyond `slots`.
- *0.4.4:* the limit is `model_capacity.effective`. In order:
  1. `model_parallel[model]` (`source: policy`);
  2. else `min(declared, measured)` over the values whose `runtime_version` equals the head's live `/api/version` and whose `digest` equals the installed digest (`declared` / `measured`);
  3. else 1 for a serial family on a runtime below `parallel_runtime_min` (default `0.40.15`), or without current evidence while `serial_families_need_evidence` (default true) holds (`serial_family`);
  4. else `slots` (`slots`).
  Always capped at `slots`. Shown in the observation `resources[].capacity.model_limits[]`, `lifecycle capacity` and `/v1/models` `model_details[m].parallel`.
- Why: Ollama runs those families one request at a time whatever `OLLAMA_NUM_PARALLEL` says ([ollama#18938](https://github.com/ollama/ollama/issues/18938)). In the 2026-11-14 field run, none of 194 qwen3.5:4b calls overlapped on a head.
- The extra calls now wait in the Gateway queue or go to another head, instead of waiting unseen inside Ollama, where they ran into their deadlines.

**Order (0.4.3, `placement_policy.strategy`).**
- `least_busy` (default): the head with the fewest active leases per slot wins.
  - The tier below breaks the tie between equally busy heads.
  - A tier-3 (evicting) head comes after every head that need not evict.
- `loaded_first`: the 0.4.0–0.4.2 order. The first non-empty tier wins, then the order within the tier.
- In the 0.4.2 field run, `loaded_first` sent six calls at once to the one head with the model loaded (10 free slots), while three idle heads waited.

The tiers (0.4.0). Ties end with `resource_id`, so the choice is deterministic.

| Tier | Condition | Order within the tier |
|---|---|---|
| 1 loaded | The model is in the head's fresh `/api/ps`, or an active lease on the head uses it | fewest active leases, then least recently assigned |
| 2 fits | Not loaded; fits in free memory without unloading anything | most free memory, then least recently assigned |
| 3 evicts | Fits only if idle models unload. No active lease uses a model that would have to go | fewest active leases, then least recently assigned |

The `admitted` event payload records the tier (`placement`: `loaded`, `fits`, `evicts`).

**Never:**
- a head with no free slot. This is also enforced by the ledger triggers `capacity_slot_guard_*`, which replace 0.3.0's one-lease-per-head unique index;
- a load that would unload a model an active lease is using. If no head qualifies, the request stays queued, as today.

**Memory accounting.**
- Free memory = `memory_gb` × 2³⁰ − Σ size of the models loaded or in use on the head.
- A loaded model counts at its `/api/ps` `size`. A model in use but not yet listed counts at its estimate.
- Estimate = the largest `/api/ps` size seen for that name+digest on any head. Without one: `/api/tags` size × `model_size_factor` (1.2).
- Measured: qwen3:4b at `num_ctx` 1024 used 1.093 × its file size. Larger contexts, or `OLLAMA_NUM_PARALLEL` > 1, need more; observed sizes take over as soon as they are seen.
- An idle head that must load the model beats waiting for a busy head that has it.

**Several requests per head.** `slots` (default 1, which is 0.3.0's behaviour) leases at once, each with a model that fits together with what is loaded.

**Freshness.**
- `/api/ps` is read with every health refresh (`health_interval_seconds`, 15 s). Each completed call also marks its model loaded on that head until the next read.
- `/api/ps` data older than `loaded_fresh_seconds` (30 s), or a head that does not answer `/api/ps`, means **loaded unknown**. This is not a failure. Such a head (and one with no `memory_gb`) can only be tier 3, and only when every active lease on it uses the same model or none.
- Head freshness (120 s) and inventory freshness (120 s, `/v1/availability`) are unchanged.

**Unchanged:**
- application `max_concurrency`: research still holds at most `max_concurrency` leases, and so heads (`test_research_identity_stays_capped_on_multi_slot_heads`, and `test_multi_client.py`);
- priority, and aging (+1 per 5 s waited);
- queue limits.

## 4. Where each value is configured

| Value | Default | Where |
|---|---|---|
| `slots` per head | 1 | policy `resources[]` (bootstrap); set it to the head's `OLLAMA_NUM_PARALLEL` |
| `memory_gb` per head | absent = unknown | policy `resources[]`. GiB that the head's models may occupy |
| `probe_backoff_seconds` | [60, 300, 900] | policy `failure_policy` |
| `cascade_window_seconds`, `cascade_heads` | 600, 2 | policy `failure_policy` |
| `ps_settle_seconds` | 2 | policy `failure_policy` |
| `safe_recheck_seconds` | 15 | policy `failure_policy` |
| `hold_max_wait_seconds` | 1800 | policy `failure_policy` |
| `probe_timeout_seconds` | 90 | policy `failure_policy` |
| `loaded_fresh_seconds` | 30 | policy `placement_policy` |
| `strategy` (0.4.3) | `least_busy` | policy `placement_policy`; `least_busy` or `loaded_first` |
| `serial_families` (0.4.3) | `["qwen35", "qwen35moe"]` | policy `placement_policy`; families Ollama runs one call at a time: at most 1 lease of such a model per head |
| `model_parallel` (0.4.3) | `{}` | policy `placement_policy`; `{model name: 1-64}` leases of that model per head, wins over the family (and, 0.4.4, over declared and measured values) |
| `parallel_runtime_min` (0.4.4) | `"0.40.15"` | policy `placement_policy`; serial families are forced to 1 only on runtimes below this version |
| `serial_families_need_evidence` (0.4.4) | `true` | policy `placement_policy`; on newer runtimes a serial family stays at 1 until a declared or measured value is current; `false` = `slots` |
| `model_capacity` per head (0.4.4) | absent (kept) | policy `resources[]`; `{model: {parallel 1-64, digest, runtime_version, num_ctx, evidence}}` from Arena's matrix; `{}` clears |
| measured parallelism (0.4.4) | none | `capacity_admin --purpose completion --parallel N` after a passing qualification |
| `deadline_liveness_seconds` (0.4.3) | 3 | policy `failure_policy`; 0-30, 0 = every deadline timeout is uncertain execution (0.4.2) |
| `model_size_factor` | 1.2 | policy `placement_policy` |
| Health and `/api/ps` refresh, recovery cycle | 15 s | `gateway-service.json` `health_interval_seconds` (unchanged key) |
| Head freshness | 120 s | code (`FRESH_SECONDS`), unchanged |
| keep_alive sent to heads | 2m | code, unchanged |

Policy rules:
- The blocks are validated: unknown keys and out-of-range values are refused before anything changes.
- An absent block keeps the ledger's current values (the defaults until one is applied).
- A resource without `slots` or `memory_gb` gets 1 and unknown when the policy is applied.
- `heads` (the inventory import) keeps a head's existing `slots` and `memory_gb` unless Arena's inventory supplies `capacity: {slots, memory_gb}`. *0.4.4:* the same for `capacity.model_capacity`. `authorize_resource` keeps stored declarations when the key is absent.

**Example policy for the nine lab heads:** [`config/policy.lab-example.json`](../config/policy.lab-example.json).
- `slots` is 1 and `memory_gb` is the RAM in the lab head list. Agent Arena replaces both (open items O7, O10).
- `clients` is empty, so applying it never changes client models or credentials.
- The applications are the ones the lab ledger has today.
- The file holds no tokens.

```json
{"resource_id": "head-192-0-2-7", "endpoint": "http://192.0.2.7:11434", "resource_type": "shared_inference",
 "owner": "infrastructure", "applications": ["capstone", "capstone-research", "lab-traffic"],
 "permitted_uses": ["inference"], "slots": 1, "memory_gb": 31.4}
```

## 5. What Arena and Capstone see

**Clients: no change required.**
- Routes, request states, error codes and envelopes are unchanged. `request` keys are pinned by `test_client_contract.py`.
- A re-placed request moves leased → queued → leased under the same ID, and its `resource_id` changes. The released 0.1.0 SDK handles that (`test_cross_version.py`).
- `/v1/models` drops a suspended model and operation from that head; a model suspended everywhere drops out as an unqualified one does.
- `/v1/availability` adds two reason codes: `model_suspended` (every head) and `model_suspended_on_resource`. A held head is `resource_unhealthy`, as before. `busy` now means every slot is in use.

**Observation: additive only.** The contract stays `shared.inference.observation/1`. Every field Arena reads today is unchanged in name, type and meaning:
- `contract`, `observed_at`, `service_id`, `ledger_id`, `admission_open`;
- `applications`;
- `resources[]` (`resource_id`, `resource_type`, `qualified_models`, `health`, `last_seen`, `fresh`);
- `requests`, `invocations`, `leases`, `qualification_observations`;
- `events`, `next_event_id`, `last_hour`, `inventory`, `scope`.

`test_observation_keeps_every_0_3_0_field_…` pins this. Two meanings narrow:
- `health: quarantined` now means **held** (uncertain execution only). It is released by a probe or an operator.
- `resource.quarantined` is emitted only for uncertain execution, once per hold, together with `resource.held`.

`qualified_models` still lists a model that is suspended on that head, because qualification is not withdrawn. Subtract `suspensions[]` to know what runs now.

Added fields:
- `suspensions[]`:
  - `suspension_id`, `scope`, `resource_id` (null for scope `model`), `model`, `digest`, `operation`;
  - `class` (`model_on_head` | `uncertain_execution`), `source`;
  - `since`, `hold_until`, `next_probe`, `probes_failed`;
  - `state` (`suspended` | `held` | `needs_attention`), `waiting`, `heads` (cascade).
- `alerts[]`: the items that need a person.
  - `alert_id`, `suspension_id`, `kind` (`needs_attention` | `model.suspended`), `scope`, `resource_id`, `model`, `operation`, `since`;
  - `reason_code` (`probes_exhausted` | `not_safe_to_probe` | `model_suspended_everywhere`) and a short `reason`.
- `resources[].capacity`: `slots`, `slots_in_use`, `memory_gb`, `loaded_models[]` (`name`, `digest`, `size_bytes`; null when unknown), `loaded_observed_at`, `loaded_fresh`.
- Events (type only, as before):
  - `model.suspended`, `model.recovered`, `resource.held`, `resource.recovered`;
  - `probe.passed`, `probe.failed`, `resource.needs_attention`;
  - also `request.replaced`, `probe.requested`, `suspension.released`, `service.schema_upgraded`.

There are still no endpoints, error strings, prompts or payloads.

**Example**, generated by a real run: [`examples/observation-0.4.0.json`](examples/observation-0.4.0.json). It shows:
- head 7 held and then `needs_attention` after three failed probes;
- gemma3:270m suspended on heads 3 and 5 after the upgrade probes, then everywhere by the cascade;
- `capacity` per head;
- the new events.

## 6. Operator commands

All run through the release launcher: `L lifecycle …`, with `--database C:\ProgramData\InferenceGateway\state\capacity-ledger.db`. Each prints one JSON object. Exit codes: 0 ok, 1 refused, 2 bad arguments. Real outputs: [`examples/operator-commands-0.4.0.json`](examples/operator-commands-0.4.0.json).

| Command | Effect |
|---|---|
| `L lifecycle suspensions --database <ledger> [--all]` | `{"suspensions": [...], "alerts": [...], "at": …}`. Each item is the observation view plus `last_probe` (`probe_id`, `passed`, `detail`, `at`). `--all` adds resolved items with `resolved_at`, `resolved_by` and `resolution` |
| `L lifecycle probe --database <ledger> --suspension <SUSP-id> --operator <id> [--wait-seconds 120]` | Asks the running service to probe now: sets `next_probe` to now and emits `probe.requested`. The service's recovery thread probes within one cycle (15 s); the command then waits for the result. A held head still waits for its deadline and the safe signal. A pass from `needs_attention` recovers the item; a fail keeps it in `needs_attention` |
| `L lifecycle release --database <ledger> --suspension <SUSP-id> --operator <id> --confirmed` | Manual override. Without `--confirmed`: exit 2. A held head is refused while a reservation is active (as `reconcile-resource`). It is set to `degraded`, so the next health refresh re-validates its qualifications and returns it to `healthy`. Model suspensions return to placement at once. Event `suspension.released` |
| `L lifecycle reconcile-resource --database <ledger> --resource <head> --operator <id> --confirmed-idle` | Unchanged: quarantined → unqualified (requalify). Also closes the head's hold |
| `L lifecycle status --database <ledger>` | Unchanged keys plus `suspensions` (counts by state) |
| `L lifecycle capacity --database <ledger>` | *0.4.4, read-only.* Per head: `slots` and `model_limits[]` (`limit`, `source`, `reason`, the declared and measured evidence with `current` / `stale_because`) |

`probe` output, from the example run:

```json
{"requested_at": 1794497227.99, "pending": false, "note": null,
 "suspension": {"suspension_id": "SUSP-0c51…", "scope": "resource_model", "resource_id": "head-192-0-2-3",
                "model": "gemma3:270m", "operation": "completion", "class": "model_on_head", "state": "recovered", "...": "..."},
 "probe": {"probe_id": "PROBE-71ae…", "passed": true, "detail": null, "at": 1794497228.04}}
```

`release` output:

```json
{"released": {"suspension_id": "SUSP-5039…", "scope": "resource", "resource_id": "head-192-0-2-7",
              "class": "uncertain_execution", "probes_failed": 3, "state": "released", "...": "..."},
 "resource_health": "degraded",
 "next": "the next health refresh re-validates qualification and returns the head to healthy"}
```

## 7. Upgrade and rollback

**`update` from 0.3.0** (run from the 0.4.0 package), on the existing ledger:
1. It takes a verified backup, stops 0.3.0 (drain), installs 0.4.0 side by side, and starts it.
2. At the first open, 0.4.0 makes these schema changes, all additive:
   - new tables `capacity_resource_capacity`, `capacity_inventory_runtime`, `capacity_suspensions`, `capacity_faults`, `capacity_probes`, `capacity_request_routing` and `capacity_request_attempts`;
   - two slot triggers;
   - the 0.3.0 index `one_active_resource` is replaced by those triggers.
3. No existing table or column changes. The ledger file and its `ledger_id` are kept; `update` prints `ledger.before`, `ledger.after` and `ledger.kept`.

**Heads quarantined at upgrade time** (today 3, 4, 5 and 6). Each becomes a **held** head (`source: upgrade`) with no active lease:
- `hold_until` = upgrade time. Their leases ended on 2026-11-09 and 2026-11-11.
- The failed model is the model of the request behind that head's newest `resource.quarantined` event. For heads 3 and 5 the evidence says gemma3:270m (completion).
- The list is stored once in the ledger (`upgrade_report:0.4.0`) and printed by `update` as `held_at_upgrade`, one entry per head: `resource_id`, `suspension_id`, `failed_model`, `failed_operation`, `active_reservations`, `hold_until`, `first_probe_not_before`.
- Example: [`examples/upgrade-report-0.4.0.json`](examples/upgrade-report-0.4.0.json).

From 60 s after the upgrade, each held head is checked for the safe signal (§2), then probed: its smallest current model, then the failed model. Expected outcomes:
- **The head answers and both probes pass:** back in service.
- **gemma3:270m still fails on 3 and 5 with an answer:** heads 3 and 5 return for every other model, and gemma3:270m is suspended on each. Two heads within 10 min trigger the cascade: suspended everywhere, with a `model.suspended` alert. The example run shows exactly this.
- **A head does not answer** (head 6 shows `last_seen` 0 in the evidence, so it was not healthy at its last refresh): it waits. After 30 min it becomes `needs_attention` with an alert.
- **A head's models changed digest or runtime since qualification:** the probe fails `qualification_not_current`. After three probes (about 21 minutes): `needs_attention`. Requalify it.

**Rollback to 0.3.0 still works.**
- `rollback` (and `update`'s automatic switch-back) expires leftover leases with the service stopped. It refuses while any head still holds two reservations, because 0.3.0 recreates its one-lease-per-head index at start. Every lease ends within 60 s, so it waits up to 75 s.
- 0.3.0 then runs on the same ledger and ignores the 0.4.0 tables (`test_upgrade_from_a_0_3_0_ledger_…` starts 0.3.0 code on the upgraded ledger and admits work). Under 0.3.0:
  - held heads stay `quarantined` (manual reconcile, as 0.3.0);
  - model suspensions stop applying, so 0.3.0 could place gemma3:270m on 3 and 5 again;
  - every head takes one lease, in rowid order.
- A later update to 0.4.0 resumes the suspensions it finds and holds any head quarantined meanwhile.

**Packaging** is the same as 0.3.0:
- `inference-gateway-0.4.0.zip` plus `.sha256`;
- `SHA256SUMS` over every file;
- `deployment-manifest.json` (`inference-gateway.windows-deployment/1`).

The builder refuses secret-like files.

## 8. Open items

| # | Item | Owner | Why not decided here |
|---|---|---|---|
| O7 | Each head's `slots` (its `OLLAMA_NUM_PARALLEL`) and `memory_gb` | Agent Arena | Arena holds the lab head list and the heads' Ollama settings |
| O8 | *Closed 2026-11-12 (Arena): row 20 PASSED on head 192.0.2.5, Ollama 0.40.13, `test_live_ollama.py` unchanged (25 s and 4 s).* | Agent Arena | – |
| O9 | *Closed (Arena): `HTTP Error 400: Bad Request` on head 3 (18:33:45) and head 5 (18:35:07), then `Admission deadline exceeded` (18:36:01). A 400 is a request fault: under 0.4.0/0.4.1 that request fails and neither head nor model is suspended (`test_a_4xx_reply_fails_only_the_request`). 0.3.0 quarantined the whole head for it.* | Agent Arena | – |
| O10 | Whether `memory_gb` is total RAM (as in the example) or RAM minus an OS reserve; and for head 2 (RTX 4060 Ti 16 GB + 63.9 GB RAM) whether VRAM is counted | Agent Arena, lab operator | A head-sizing decision; the Gateway takes one figure per head and never unloads a model in use either way |
| O11 | Whether `cascade_heads` 2 / 600 s suits the lab once more heads share traffic | lab operator | Configurable; defaults are as requested |
