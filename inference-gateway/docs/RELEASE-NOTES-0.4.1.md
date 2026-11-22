# Inference Gateway 0.4.1: release notes (2026-11-13)

Same release type as 0.4.0. It updates 0.4.0 in place with `gateway-deploy.py update`, keeps the ledger (same `ledger_id`), and keeps the contracts `shared.inference/1` and `shared.inference.observation/1` (both additive only). No change to Agent Arena or Capstone code.

**Ship to Capstone:** `client/shared_inference_client.py` from this package, version **0.4.1** (`shared_inference_client.__version__`), SHA-256 `e7065e412bdf7e6777714ab51ebafa520c2984e344d964741b9f60f7a4bdce02` (also in `SHA256SUMS`). The file is also inside the wheel.

**Where it was tested.** On the development laptop only: the unit and integration suite, a three-process load harness (`scripts/concurrency_load.py`: nine fake Ollama heads on loopback, the real Gateway service runtime and listener, real client flows), and one real Ollama (0.40.16, qwen3:4b) on 127.0.0.1. **No lab head and not the hub were contacted.** The acceptance run through the hub against the nine heads is Agent Arena's (section "For Agent Arena").

| Item | Status | How tested |
|---|---|---|
| A1 admission spin | **Fixed** | `test_waiting_requests_cost_one_admission_pass…`, `test_a_status_read_takes_no_write_lock`, `test_a_freed_slot_is_given…`; load harness |
| A2 renewal stops on first error | **Fixed** | `test_renewal_retries_every_failure…`, `test_a_busy_ledger_during_generation…`, `test_a_lease_whose_executor_is_gone…` |
| A3 connections dropped over the limit | **Fixed** (503 + Retry-After; limit 256 recommended) | `test_over_the_connection_limit…` (HTTP and TLS), `test_a_full_pending_budget_answers_503…`; load harness with `max_connections` 4 |
| A4 client gives up after one 5 s read | **Fixed** in client 0.4.1 | `test_status_reads_that_time_out…`, `test_a_submit_whose_answer_was_lost…`, `test_final_answers_are_not_retried`; load harness |
| A5 lifecycle timestamps | **Fixed** (documented and added) | `test_status_replies_carry_lifecycle_times…`, `test_client_contract.py` |
| A acceptance (32 flows through the hub, 9 heads, slots ≤ 10) | **Not run on the lab**; passed on the local harness | `verification/concurrency-0.4.1.json`, `tests/test_concurrency_load.py` |
| B1 a failed test removes the model for every purpose | **Fixed** | `test_a_failed_test_withdraws_only_the_purpose…`, `test_an_embedding_model_given_the_completion_test…`, `test_a_refused_qualification_changes_nothing`, `test_a_0_4_0_refusal_row_is_ignored…` |
| B2 one failed model degrades the head | **Fixed** | same tests (head stays `healthy`) |
| B3 completion test is an instruction-following test | **Fixed** | `test_completion_qualification_passes_every_model_that_answers` (field answers), `test_completion_rule_on_the_field_answers`, live Ollama qwen3:4b |
| B acceptance on the lab pairs | **Not run on the lab** (requalification is Arena's step) | as above |
| C1 update exception skips the switch-back | **Fixed** | `test_update_rolls_back_when_starting_the_new_release_raises`, `test_update_reports_when_the_switch_back_fails_too` |
| C2 release files owner-only after pip --target | **Fixed** (`icacls /reset /T /C`) | `test_release_folder_acl_is_reset_to_inherit` (real icacls on an owner-only file) |
| D answers O8, O9, client limits | Recorded | docs updated; O8 and O9 closed |

---

## A. 16-32 requests at once (top priority)

### What 0.4.0 did, reproduced here

The load harness reproduced the field symptom on the unchanged 0.4.0 code: 32 flows × 4 calls started within 4 ms, nine heads with one slot each, generations of 2-6 s. **10 of 128 calls completed for the clients**; 118 failed with client read timeouts, 7 of them before a `request_id` came back. The ledger shows 95 calls completed by the Gateway.

The Gateway itself had completed most of those calls; the clients had given up (5 s status read, no retry) while status reads waited behind the admission spin. That matches the field: read timeouts, four of them before a `request_id` came back.

### A1. One scheduler instead of a write-lock spin — fixed

- `CapacityBroker.place()` is the one admission pass (expiry, then placement of every queued request, in one `BEGIN IMMEDIATE`). `admit()` remains for direct callers and runs one pass.
- `Gateway.Scheduler` runs that pass once per **event**: a submission, a release (completion, failure, cancel), a health, qualification or recovery change; plus once a second while anything waits (deadlines, other processes). Events during a pass coalesce into one more pass. The thread stops after 5 s with nothing waiting.
- A waiting request blocks on its own event and reads its row (a plain read). A dynamic (`@authorized`) request recomputes its eligible heads only when health or qualification changed, or every 10 s, and writes them only when they changed.
- **Status reads take no write lock.** 0.4.0's `result()` ran `reconcile_expired()` (a `BEGIN IMMEDIATE`) on every status poll. 0.4.1 reads first and expires only when something is actually past its time.
- **WAL journaling** (found by the 90-flow stress run): in rollback-journal mode a committing writer waits for every concurrent reader, and waiting threads sleep in SQLite's busy handler. The ledger now uses WAL (readers and the writer no longer block each other); `synchronous` stays FULL, so an accepted result is still durable on power loss. 90 simultaneous submissions in one process: p50 2.1 s → 1.1 s, max 8.3 s → 3.3 s.
- The listener's accept backlog is 256 (socketserver's default of 5 was smaller than one burst of flows).
- Measured: 20 waiting requests, 3 s with nothing changing: at most 8 `BEGIN IMMEDIATE` (0.4.0 by construction: 20 requests × 20 a second × 3 s ≈ 1,200). 50 status reads: 0 writes.

### A2. Lease renewal — fixed

- `renew_until_stopped` (Gateway and the coordinator adapter `shared_slot`): every failure is logged (`lease.renewal_failed`, service log) and counted (`lease_renewal_failures`), then retried after 0.5 s, doubling to the 10 s interval, until the call ends. Only a definite "lease has ended" stops it early.
- **A renewal failure alone never holds a head.** The Gateway keeps the set of calls it is executing. Expiry in that process extends such a lease instead of expiring it (event `lease.extended`, reason `renewal_late`). A renewal is accepted while the lease is still `leased`, even after its `expires` time. Result acceptance checks "still `leased` and before the deadline" (0.4.0 also required `expires > now`, which is what turned one late renewal into "Allocation ended before durable result acceptance" and a held head).
- Kept: a lease whose executor is gone (process crash, or another process expiring it) still expires and holds the head as uncertain execution.

### A3. Over the connection limit — fixed

- A connection over `max_connections` gets **`503 Service Unavailable`** with `Retry-After: 1`, `X-Inference-Service-ID` and a JSON body, over TLS too. The answer is written after the request (and up to 1 MiB of body) is read, so Windows does not turn it into a reset. Up to 64 such answers are in progress at once; beyond that a connection is closed (counted).
- A full **pending budget** (`max_pending`) is now 503 with `Retry-After: 2` (0.4.0: 400). A submission retried with the same `client_key` is queued again under the same request ID (event `request.resubmitted`).
- An application's own `queue_limit` stays 400 (operator policy, not a Gateway fault).
- Counted in the observation under the new, additive key **`gateway_load`** (published each health interval, no write per request): `connections_503`, `connections_closed_over_limit`, `pending_budget_503`, `unavailable_503`, `client_disconnected`, `lease_renewal_failures`, `scheduler_pass_failures`, `scheduler_passes`, `executing_now`, and the limits.
- **Recommended limits for 9 heads × up to 10 slots (90 calls at once): `max_connections` 256, `max_pending` 512.** These are now the defaults (0.4.0: 64 and 128); they apply after the update unless `gateway-service.json` sets `limits`. Reasoning: with submit-and-poll a flow holds a connection only per HTTP exchange, so 256 covers a burst of 90+ submissions plus their status reads with room for synchronous `/v1/inference` callers, which hold one connection for the whole call. `max_pending` counts queued plus executing invocations; 512 covers 90 executing plus a deep queue. Each pending invocation is one waiting thread. Note `capstone` and `capstone-research` each allow `queue_limit` 500, so the 512 budget can refuse first; that refusal is a 503 the 0.4.1 client retries.

### A4. Client library 0.4.1 — fixed

- `infer()` keeps one deadline (`timeout`). Submission attempts wait up to 10 s and are re-sent unchanged (same `client_key` and `deadline`) on a timeout, reset, refused connection or 429/502/503. Status reads are repeated with the same `request_id` on the same errors, until the request's own deadline.
- Polling: 0.25 s, ×1.5 up to 2 s, back to 0.25 s whenever the state changes. `Retry-After` is honoured. 400, 403, 404 and 504 are final.
- A retried submission is idempotent on the Gateway: the same `client_key` returns the same request; a lost submit answer runs once (`test_a_submit_whose_answer_was_lost…`).
- `request()`, `status()` and the 0.1.0-era API are unchanged. `client.retries` counts retried attempts.
- **Capstone ships `client/shared_inference_client.py` 0.4.1.** The 0.4.0 client against the 0.4.1 Gateway also completed every call in the harness (128 of 128, slots 1), but it still fails a flow on the first 503 or busy-moment timeout.

### A5. Lifecycle times — fixed

Every status reply now carries (absent until it happened): `submitted_at`, `admitted_at`, `execution_started_at` (existing), `execution_finished_at`, `completed_at`. The result record keeps `queued_at`, `started_at`, `finished_at`, `queue_ms`, `duration_ms`. Documented in `docs/CLIENT_CONTRACT.md` §6 with the same-head overlap rule. Capstone rc.11's names `submitted_at`, `admitted_at`, `completed_at` now exist at the top level of the status reply.

### A acceptance — local harness passed; lab run is Agent Arena's

32 flows × 4 calls each (a flow's calls are sequential), all flows started within 3-7 ms, nine heads, generations 2-6 s (1-3 s for the 503 run). Columns: calls completed; failures before a `request_id`; `lease.expired` events; `resource.held` events; suspensions; 503 answers; most generations at once on one head; slowest call.

| Scenario | Completed | Before ID | Lease expired | Held | Suspensions | 503 | Max on one head | Slowest |
|---|---|---|---|---|---|---|---|---|
| 0.4.0 Gateway + 0.4.0 client, slots 1 (field reproduction) | 10/128 | 7 | 0 | 0 | 0 | – | 1 | 46.8 s |
| 0.4.1, slots 1 | 128/128 | 0 | 0 | 0 | 0 | 0 | 1 | 18.1 s |
| 0.4.1, slots 4 and 10 alternating (acceptance shape) | 128/128 | 0 | 0 | 0 | 0 | 0 | 4 | 9.1 s |
| 0.4.1, `max_connections` 4 (503 path) | 128/128 | 0 | 0 | 0 | 0 | 174 | 3 | 19.1 s |
| 0.4.1 Gateway + 0.4.0 client, slots 1 | 128/128 | 0 | 0 | 0 | 0 | 0 | 1 | 18.8 s |
| 0.4.1 stress: 90 flows × 3 calls, slots 10 | 270/270 | 0 | 0 | 0 | 0 | 0 | 10 | 11.8 s |

Full reports: `verification/concurrency-0.4.1.json`. A short version runs in the suite (`tests/test_concurrency_load.py`). With 32 flows spread over nine heads, at most 4 ran on one head at a time; the 90-flow run fills all 10 slots on a head.

Not covered locally: real head generation times and memory pressure, the hub's disk, TLS from 192.0.2.201, and Capstone's own flow logic. I did not need the hub ledger copy for these fixes; it would be useful from the acceptance run to confirm `gateway_load` and the absence of `lease.expired` / `resource.held` events.

## B. Qualification — fixed

- **A failed test withdraws only the purpose it tested.** A purpose counts while its newest test passed (digest, runtime and endpoint current). A model stays in a head's list while any operation purpose's newest test passed.
- **A test the Gateway's own pre-checks refuse changes nothing** (wrong purpose for the model's declared capabilities, not installed, no digest, no current completion base for a capability test, deadline before sending). It is recorded with `refused: true` and ignored by every view. 0.4.0's rows of such refusals (no flag, no response, one of those errors) are recognised and ignored as well.
- **Head health is not degraded by one model**: after a qualification the head is `healthy` while it serves any qualified model.
- **Completion test: "does the model answer".** Same prompt, `num_predict` **128**, temperature 0, seed 42, `think` false. Pass when `done`, `done_reason` `stop` (or absent), and non-empty text after removing a reasoning block (`<think>…</think>`, reasoning printed before a lone `</think>` as qwen3:4b does despite `think:false`, or an unclosed `<think>`). Whether the answer is READY (case and punctuation ignored) is recorded in `measurement.answer_matches_ready`, not required. The recovery probe uses the same test.
  - On the field answers (fake head): gemma3:1b and qwen3:0.6b (`Ready.`), qwen3:4b, deepseek-r1:8b/14b pass; smollm2:135m and granite4:350m (`I'm sorry…`; smollm2:360m answers the same way) also pass, as working models that answered; a model that never finishes within 128 tokens fails.
  - On a real Ollama 0.40.16 with qwen3:4b: passed (54 tokens, `done_reason` stop, reasoning removed, answer READY). The 0.4.0 rule fails the same reply.
- **The 49 pairs are not restored automatically.** A model that left a head's list needs a passing test (the 0.4.0 rule, kept). After the update, re-run qualification of those pairs: completion for the completion models, `--purpose embedding` for the embedding models. With 0.4.1 a later failed completion test on an embedding model no longer touches its embedding qualification.
- Interpretation to confirm: "compare case-insensitively without punctuation" is implemented as a recorded comparison, not a pass condition, following "leave answer quality to the applications". If the operator wants a match required, it is one line in `completion_test`.

## C. Update — fixed

- `Installer.update()`: an exception from switching to the new release (for example `StartServiceW` error 1053) is now a failed start: the installer switches back and records `update_failed_rolled_back` with the error. If switching back fails too, it records `update_failed_rollback_failed` and says to run `rollback` or `repair`; `current` stays the previous release.
- `_release()`: after `os.replace`, the release folder's ACL is reset to inherit (`icacls <release> /reset /T /C /Q`), so the service account reads it through the install root's `Users:RX`. A failure of that step refuses the install/update. Arena's workaround is no longer needed from 0.4.1 on (harmless if kept).

## D. Answers recorded

- **O8** closed: row 20 PASSED on head 192.0.2.5, Ollama 0.40.13 (`test_live_ollama.py` unchanged, 25 s and 4 s).
- **O9** closed: `HTTP Error 400: Bad Request` on heads 3 and 5 (2026-11-11 18:33:45 / 18:35:07), then `Admission deadline exceeded`. A 400 is a request fault: since 0.4.0 it fails only that request (`test_a_4xx_reply_fails_only_the_request`); 0.3.0 quarantined the head.
- **Client limits** are the operator's: `capstone` and `capstone-research` `max_concurrency` 100, `queue_limit` 500, set by Agent Arena through the bootstrap. The contract's earlier "research should be 1" is marked as overridden.

## Upgrade notes (0.4.0 → 0.4.1)

- Run `update` from the 0.4.1 package; expect `ledger.kept: true`, `held_at_upgrade: []`.
- Schema: one new table, `capacity_request_times`. The ledger switches to WAL at first start (`capacity-ledger.db-wal`, `-shm` beside it, same folder ACL). Back up with `lifecycle backup`, not a file copy.
- Rollback to 0.4.0 is supported (it ignores the new table and opens the WAL ledger unchanged).
- Then: requalify the removed pairs; ship client 0.4.1 to Capstone; watch `gateway_load`.

## Test record

`verification/test-report.json`, `verification/standalone.xml` (CPython 3.12.10, Windows 11, 2026-11-13):
- **162 tests: 155 passed, 3 skipped, 4 failed.** The 3 skipped are the opt-in live-Ollama tests, run separately: 3 passed against a local Ollama 0.40.16 with qwen3:4b (`verification/live-ollama-0.4.1.txt`).
- **The 4 failures are environmental**, all in `tests/test_publication_scripts.py` (the observation ACL script). This laptop's local Administrators group holds domain groups that could not be expanded today, so the script reports "Administrators membership could not be read". The script is byte-identical to 0.4.0, and the unchanged 0.4.0 sources fail the same 4 tests on this machine today (they passed on 2026-11-12). Please re-run them on the hub.
- New: `tests/test_release_0_4_1.py` (A and B), `tests/test_concurrency_load.py` (A end to end), three item-C tests in `tests/test_deployment.py`. Changed pins: the status envelope (`test_client_contract.py`) and observation keys (`test_failures_and_placement.py`) for the additive keys.
