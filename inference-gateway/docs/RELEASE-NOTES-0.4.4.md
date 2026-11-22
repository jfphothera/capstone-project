# Inference Gateway 0.4.4: release notes (2026-11-17)

This release is the same type as 0.4.1 to 0.4.3:
- It updates 0.4.3 in place with `gateway-deploy.py update`.
- It keeps the ledger, with the same `ledger_id`.
- It keeps `shared.inference/1` and `shared.inference.observation/1`. All additions are additive.

Ledger changes are additive only:
- three indexes: `capacity_requests_status`, `capacity_requests_resource_status`, `capacity_invocations_state`;
- one table: `capacity_model_capacity`.

Releases 0.3.0 to 0.4.3 ignore both, so rolling back still works.

**Client library unchanged.** Capstone keeps `client/shared_inference_client.py` **0.4.1** (SHA-256 `e7065e412bdf7e6777714ab51ebafa520c2984e344d964741b9f60f7a4bdce02`).

## What the pilot showed (2026-11-16 20:47 to 11-17 00:30 ET, EXP-7f55845c…, hub on 0.4.3, Ollama 0.40.13)

The evidence is `E:\gateway-service.log`; the fault log was empty. The analysis is [`verification/pilot-log-analysis-0.4.4.json`](../verification/pilot-log-analysis-0.4.4.json).

**What the log shows:**
- **48 `database is locked` events, all on the Gateway's own SQLite ledger:** 27 HTTP 503s (24 on `/v1/submit`), 20 lease-renewal failures and 1 recovery cycle (traceback at `capacity_broker.reconcile_expired`, `BEGIN IMMEDIATE`).
- **The renewal failures lost nothing.** They hit 19 requests, and all 19 completed. The window has no `lease.expired`, and no admitted request without an end.
- **167 `SSLEOFError: EOF occurred in violation of protocol`:** 159 on `/v1/submit`, 8 on status reads.
- **The health cycle (set to 15 s) ran long:** p50 28.6 s, p90 79 s, max 172 s.
  - 98% of the lock events and 94% of the SSLEOF events fall in cycles longer than 40 s, which cover 61% of the pilot.
  - In cycles longer than 90 s (18% of the time) the shares are 65% and 38%.

### Proven causes (code and reproduction)

1. **SQLite's busy handler was the only queue for the write lock, and it is not fair.**
   - Every write opened its own connection with a 30 s busy timeout (`capacity_broker.py` 0.4.3 line 129).
   - A waiting connection polls with sleeps of up to 100 ms, and it loses to any writer that takes the lock between its polls.
2. **The admission scheduler took the write lock back to back.**
   - `Scheduler._loop` (0.4.3 `capacity_gateway.py` 115-136) starts the next `place()` pass at once when an event arrived during the last one, which is normal under load.
   - Reproduced: `scripts/ledger_contention.py`, scenario `scheduler_starvation`, on 0.4.3 code with a pilot-sized ledger. Lease renewals next to a busy scheduler waited p95 2.8 s, and **13 of 103 failed with `database is locked`** (busy timeout forced to 3 s so it runs in 20 s; `verification/ledger-0.4.3.json`).
   - The same scenario on 0.4.4, scheduler still back to back: 320 renewals, none refused, max wait 7 ms (`ledger-0.4.4.json`).
3. **Write transactions grew with the ledger's age.**
   - No index on `capacity_requests.status` or `resource_id`. So every admission pass, expiry pass, slot-guard trigger and status poll (`has_expired`) scanned the whole request history inside or next to the write lock (0.4.3 `capacity_broker.py` 193, 215, 229, 309, 315; triggers 60-70; `placement.py` 44).
   - `_execute` counted active invocations by scanning every stored result inside the write lock (0.4.3 `capacity_gateway.py` 729).
   - The hub ledger held at least 19,092 requests and 59,320 events at the pilot.
   - Measured on a synthetic ledger of 20,000 requests with 8 KB results (`verification/ledger-0.4.3.json` against `ledger-0.4.4.json`, medians on the development laptop): one admission pass 48 → 3.8 ms, the invocation count 9.2 → 0.6 ms, the expiry pass 7.2 → 0.7 ms, a status poll's expiry check 4.9 → 0.6 ms, a submission 7.8 → 2.1 ms. On 0.4.3 these grow with every request the ledger keeps; the hub's disk and CPU are slower than this laptop.
   - Fairness at 92% offered load (24 writers, 30 ms holds): busy-handler waits p99 1.3 s, max 2.3 s; FIFO gate p99 0.22 s, max 0.28 s.
4. **SSLEOF is a symptom: the client had already given up.**
   - The 0.4.1 client gives up on one submit or status attempt after **10 s** (`shared_inference_client.py` line 12) and retries with the same `client_key`. The ledger could make a handler wait **30 s**.
   - The server then wrote its answer into a closed TLS connection. `SSLEOFError` is an `OSError`, not a `ConnectionError`, so 0.4.3 logged it as `http.unavailable` and tried to answer 503 on the dead socket (0.4.3 `capacity_gateway_server.py` 136-140).
   - Reproduced over TLS: `test_a_client_that_gives_up_is_logged_as_client_gone_with_timing_not_as_unavailable`.
   - It is not a certificate problem: verification succeeded, and the handshake had completed long before.
5. **Each submission took three write transactions:** submit, then a separate `record_operation`, then the invocation (0.4.3 `capacity_gateway.py` 554-555, 715). A status poll could add a fourth while a live call was past its deadline: `has_expired` counted it, and `_expire` then changed nothing.

### Remaining hypotheses (not proven by the available logs)

- **Which transaction held the lock in the field.** The 0.4.3 logs do not record it. 0.4.4 records every write's label, wait and hold time. A slow one is logged as `ledger.slow_transaction`, and a refused one as `ledger.wait_timeout`, which names the holder. Expect the scheduler pass to dominate, as in the reproduction.
- **The hub's commit cost.** `synchronous=FULL` fsyncs the WAL on every commit, and the hub's disk sets that cost. It is unmeasured; `ledger.slow_transaction` will show it.
- **CPU and GIL pressure from read paths.** `/v1/models`, `/v1/availability` and dynamic node refresh parse every qualification record. This could lengthen write holds on a busy hub. It is not measured on the hub.
- **The 18 whole-worker timeouts at 27 minutes.** The Gateway ended every request it admitted in the window. The timeouts are consistent with time added across many calls per workflow: queue waits, 10 s abandoned attempts plus retries, and serial qwen3.5 on 0.40.13. They are not shown to be ledger failures or hardware overload. Capstone's per-call records with the new `timing_ms` fields will settle it.
- **The 8 SSLEOF events on status reads.** They fit the same 10 s attempt timeout. 0.4.4 logs each with its timing (`http.client_gone`, `likely`).

## Changes

### 1. Ledger writes: FIFO, bounded, observable (`capacity_broker.py`)

**One write path.** Every ledger write goes through `CapacityBroker.write(label, wait=…)`.
1. A FIFO gate per ledger file in this process hands the lock straight to the next waiter.
2. Then `BEGIN IMMEDIATE`. Only this step can meet another process's lock (WAL), so it alone is retried with a short backoff inside the budget.

**Wait budgets:**
- 5 s for the HTTP handler writes that create nothing on refusal: submit, cancel, a status read's expiry. The invocation row that follows a committed submission uses the background budget, so a refused write never leaves a request without its invocation. This is below the client's 10 s attempt, so the client gets **503 + `Retry-After: 1`** instead of silence.
- 30 s for background writes.
- A finished result (`complete`) and a failure's settlement retry through `LedgerBusy` until the request's deadline (a settlement: until 120 s after it). A busy ledger no longer loses a terminal result.

**What is logged:**
- `ledger.slow_transaction` (hold ≥ 1 s or wait ≥ 2 s): `label`, `wait_ms`, `hold_ms`, `external_busy`.
- `ledger.wait_timeout`: `label`, `waited_ms`, `holder` (`{label, held_ms, thread}`, or `external`).
- Per-label counts, timeouts and maximum waits and holds are in the observation, under `gateway_load.ledger`.

**Other changes:**
- Indexes as above.
- `submit` records the operation in the same transaction.
- `has_expired` ignores calls this process is still running.
- The scheduler waits `MIN_GAP` (50 ms) between passes to coalesce events.
- The service logs `ledger.opened` (row counts, file and WAL size) at start.

Kept: WAL and `synchronous=FULL`. No inference, network call or sleep runs inside a write transaction.

### 2. Transport symptoms separated from causes (`capacity_gateway_server.py`, `capacity_gateway.py`)

**A peer that closed the connection** (`ConnectionError`, `SSLEOFError`, `SSLZeroReturnError`) is logged as `http.client_gone` with:
- `route`;
- `phase` (`reading`, `reading_body`, `handling`, `responding`);
- `status`, `elapsed_ms`, `ledger_wait_ms`;
- `likely`: `client_attempt_timeout` when the handler took ≥ 9 s, else `transport`.

It is counted in `client_disconnected`, never in `unavailable_503`. No answer is attempted on the dead socket. TLS verification is unchanged.

**Other logs:**
- `http.slow`: any handler of 5 s or more, with the same timing.
- `http.unavailable` now carries `phase` and timing.
- `health.slow_cycle` (a cycle of 10 s or more): `total_ms`, `ledger_wait_ms`, `heads_ms` and the slowest heads. It says whether the heads or the ledger took the time.

**Result timing**, additive in `provenance.timing_ms`:
- `ledger_wait`;
- `runtime_load_or_runner_wait` (Ollama's `load_duration`, which includes waiting for a busy runner);
- `runtime_prompt_eval`, `runtime_generation`, `runtime_total`;
- `transfer_and_other`.

`provenance` also gains `attempt`, `attempts[]` (`resource_id`, `failure_class` per attempt) and `model_substitution: null`.

**Requests in flight when a worker dies:**
- A result this process finished, whose status nobody read within 120 s, is logged once as `request.unclaimed` (`request_id`, `app_id`, `state`, `finished_at`, `last_read_at`) and counted as `results_unclaimed`.
- Nothing is replayed.

**A client's cancel no longer takes a head offline.** When the lease ended during the pre-dispatch checks (a client cancellation or an expiry), 0.4.0-0.4.3 raised `TimeoutError`. In the checks phase that classifies as `head_unreachable`, so the head was marked offline until the next health cycle, and the other calls being checked on it failed with `Resource admission withdrawn before dispatch`. It is now `AllocationEnded`, a request fault: nothing ran and no head is touched. `LedgerBusy` is also a request fault, never a head fault. Found by the 20-workflow lifecycle test (about 1 run in 3 to 12); regression test `test_a_client_cancel_before_dispatch_fails_no_head`.

**A graceful stop lets callers collect results that finished during the drain.** After the last allocation ends, the listener stays up for up to 5 s (within the drain budget) while a result finished in the last 2 minutes is unread. Before, a caller polling every 2 s could miss the window and retry a stopped service until its deadline. The result was durable either way. Any results still unread are logged as `service.stop_results_unread`.

**Status reads and revocation.** A status read now checks the client's authorization again after its snapshot. Before, a revocation that committed between the first check and the read could still return a result. This was seen once in a loaded full test run (`test_revocation_releases_capacity_without_degrading_heads`).

### 3. Per-model capacity after the Ollama upgrade (`model_capacity.py`, `placement.py`)

The limit per head and model comes from evidence that names the runtime and the build.

**Declared:** the bootstrap policy's `resources[].model_capacity` (Agent Arena's `agent-arena.model-capacity/1`), keyed by model name:
`{"parallel": 1-64, "digest": str, "runtime_version": str, "num_ctx": int, "evidence": str}`.
- `authorize_resource` stores it. An absent key keeps the stored declarations; `{}` clears them.
- The inventory import (`heads`) carries `capacity.model_capacity` and keeps it on re-import, like `slots` and `memory_gb`.

**Measured:** `capacity_admin … --purpose completion --parallel N` (`Gateway.measure_parallel`).
- It runs after a passing qualification, with the head in maintenance and no allocation on it.
- One warm-up call, then N short generations at once.
- Overlap is judged on the head's own clock from each reply's `created_at` and `prompt_eval_duration + eval_duration` (load and runner wait excluded).
- It is stored with the runtime version, digest and `num_ctx`. Event `model_capacity.measured`.

**A value is used only while** the head's live `/api/version` equals its `runtime_version` and the installed digest equals its `digest` (case and `sha256:` prefix ignored). With both a declared and a measured value, the limit is `min(declared, measured)`.

**Otherwise, the conservative default:**

| Case | Limit | `source` / `reason` |
|---|---|---|
| serial family, runtime below `placement_policy.parallel_runtime_min` (default `0.40.15`) or unknown | 1 | `serial_family` / `runtime_below_parallel_min`, `runtime_version_unknown` |
| serial family, newer runtime, no current evidence | 1 | `serial_family` / `no_current_parallel_evidence` |
| any other model | `slots` | `slots` |
| `placement_policy.model_parallel[model]` (0.4.3 operator override) | that value | `policy` |

Every limit is capped at `slots`. A runtime upgrade withdraws a declared or measured value automatically (`stale_because: runtime_changed`) until Arena re-declares it or the Gateway re-measures.

**Where to see it:**
- the observation: `resources[].capacity.model_limits[]` (`model`, `limit`, `source`, `reason`, `runtime_version`, `digest`, `declared`, `measured`);
- `L lifecycle capacity --database <ledger>`;
- `/v1/models`: `model_details[m].parallel = {limit, source, reason}`.

### 4. Experimental meaning (no substitution)

- A busy request waits visibly in the Gateway queue (`queued`, `submitted_at`, `admitted_at`), or goes to another qualified head serving the **same** model (least-busy placement, 0.4.3).
- The Gateway never runs another model: `provenance.model_substitution` is always `null`, and a reply naming another model is refused.
- A different-model fallback is the caller's decision, as a new request. Capstone must authorize it in the role configuration, check it is capability-qualified (`/v1/models`), and record requested model, executed model, reason and attempts. It must never merge it into the original model's results.

## How it was tested

| What | Result |
|---|---|
| `tests/test_release_0_4_4.py` (21 tests): a client cancel before dispatch fails no head; FIFO hand-off and a refused writer naming its holder; another process's lock retried, then refused as `external`; every write of a request through the gate, one write per submission; index use (query plans); no write on status reads for a live call past its lease; a finished reply recorded through a 1.5 s external lock; renewal through repeated external locks (2 s lease); 503 inside the client's attempt and exactly one execution over HTTP; a client that gives up over TLS logged as `http.client_gone` with timing; policy defaults and validation; the full limit matrix; a current declaration on an upgraded head (3 of 4 slots), then withdrawn by a runtime change; declarations through bootstrap re-apply and inventory re-import; measurement of overlapping and serial timings, `min()` with the declaration; observation, lifecycle and `/v1/models` sources; **12, 16 and 20 workflows** (below); a restart with calls in flight never replays them | 21 passed |
| `tests/test_client_contract.py`: additive keys pinned (`parallel`, `attempt`, `attempts`, `model_substitution`, six `timing_ms` keys) | passed |
| Full suite (`verification/test-report.json`) | See the report. Exceptions: the four ACL-script tests that need expandable local Administrators, as in 0.4.2 and 0.4.3 |

**Lifecycle under concurrency** (`scripts/lifecycle_load.py`, [`verification/lifecycle-0.4.4.json`](../verification/lifecycle-0.4.4.json)). The setup:
- six fake heads with latencies 0.08-1.0 s and slots 1-4;
- `qwen3.5:4b` (serial family) and `gemma3:4b`;
- declared parallel 2 for qwen3.5 on the heads with 2 or more slots;
- 3 s leases, renewed every 0.5 s;
- the recovery and health cycles running every 1 s;
- 5% client cancellations;
- an external connection holding the write lock 0.2-1.2 s every 0.5-1.5 s.

Every run: no exception, no duplicate dispatch, no lost result, no lease expiry, no hold, no `database is locked`, no renewal failure, and peak per-model concurrency within each limit.

| Workflows × calls | Completed / cancelled | Throughput | Latency p50 / p95 / max | Queue p50 / p95 / max | External locks injected | Longest ledger wait / hold |
|---|---|---|---|---|---|---|
| 12 × 8 | 93 / 3 | 9.7 calls/s | 0.84 / 2.13 / 2.79 s | 0.09 / 0.52 / 1.24 s | 5 | 1.16 s / 35 ms |
| 16 × 8 | 125 / 3 | 11.2 calls/s | 1.19 / 2.20 / 2.94 s | 0.16 / 0.57 / 0.76 s | 6 | 1.26 s / 34 ms |
| 20 × 8 (pressure) | 152 / 8 | 10.1 calls/s | 1.54 / 2.75 / 3.89 s | 0.28 / 1.42 / 1.55 s | 8 | 1.29 s / 39 ms |

The longest ledger waits are the injected external locks (up to 1.2 s), absorbed without a refusal. Queue time is visible queueing behind serial qwen3.5 limits and one-slot heads.

**Over the real HTTP listener with client 0.4.1** (`scripts/concurrency_load.py`, nine fake heads, slots 1/2/4, generations 0.3-3 s, `verification/concurrency-0.4.4.json`): 12, 16 and 20 flows × 4 calls. 48/48, 64/64 and 80/80 completed, no failures, no suspensions; the longest call took 4.2, 4.7 and 4.5 s.

**Not tested here:**
- the hub's disk and CPU;
- real Ollama 0.40.15 parallelism (run `qualify --parallel N` on a head);
- a real 12/16/20-workflow Capstone campaign. That campaign is the acceptance test: expect no `database is locked`, `http.client_gone` only as `client_attempt_timeout` during real stalls, and `ledger.slow_transaction` naming any remaining long holder.

## Upgrade notes (0.4.3 → 0.4.4)

1. Run `update` from the 0.4.4 package. Expect `ledger.kept: true` and `held_at_upgrade: []`. The indexes and table are created at the first start (seconds on a 20,000-request ledger).
2. No requalification is needed for the upgrade itself.
3. Policy: a stored `placement_policy` without `parallel_runtime_min` and `serial_families_need_evidence` gets the defaults. Bootstrap refuses:
   - a non-version `parallel_runtime_min`;
   - a non-boolean `serial_families_need_evidence`;
   - a malformed `model_capacity` (unknown keys, `parallel` outside 1-64, missing `digest` or `runtime_version`).
4. Rollback to 0.4.3 or earlier is supported. The indexes and table are ignored, so qwen3.5 returns to one lease per head.

**After the heads move to Ollama 0.40.15 (with Agent Arena):**
1. Requalify each head and model. The runtime changed, so the old qualifications no longer match.
2. Declare `model_capacity` from Arena's matrix, or run `qualify --parallel N`.
3. Until then, qwen3.5 stays at one lease per head.
4. Remove any `placement_policy.model_parallel` entry for a model whose limit should come from evidence: the override still wins.

**Contract decision to confirm:**
- The agreed fallback gives a serial-family model on a ≥ 0.40.15 head without evidence the head's `slots`. 0.4.4 keeps it at 1 by default (`serial_families_need_evidence: true`), because the pilot brief requires conservative limits for unverified combinations.
- Set it to `false` for the literal fallback.


## Known issue found in the lab (2026-11-17), fixed in the source for the next release

`scripts/Set-ObservationPublicationAcl.ps1` lists the ledger folder and then reads each file's ACL. 0.4.4 opens and closes the ledger often, so SQLite's `capacity-ledger.db-shm` can disappear in between, and the script then failed with `Cannot find path ...-shm`. That happened twice on the lab hub. The source now skips a file only when it no longer exists; any other read error still fails the check. Until the next release, the lab provisioner (1.6.21) runs a corrected copy of the installed script. Reproduced against the 0.4.4 script with a file created and deleted in a loop: 3 of 12 runs failed as shipped, 0 of 12 with the fix.

