# Inference Gateway 0.4.6: release notes (2026-11-19)

This is the Gateway part of the final engineering iteration (`AgentArena/docs/handoff/FINAL-ITERATION-20261119.md`, Phase 5 G1–G6, the Gateway side of Phase 4, and the Gateway rows of Phases 0 and 8). Update from 0.4.5 with the deploy tool's `update` verb. The ledger, operator capacity policy, `model_capacity` declarations, `placement_policy.unmeasured_model_limit` and credentials are all kept. There are no new configuration keys. Client library 0.4.2 is additive (see `docs/handoff/GATEWAY-0.4.6-CLIENT-CONTRACT.md`).

Evidence states used below: **confirmed defect** (reproduced here and repaired), **reproduced, cause unresolved**, **hypothesis** (not yet tested).

## G2: `/v1/models` capacity map overwritten (confirmed defect, repaired)

- **Cause.** In `Gateway.models`, `evidence` first held the capacity map from `model_capacity.load(db)`. Inside the model loop, it was reassigned to the qualification document of the model just read. Every later head then got a dictionary with string keys as its capacity map. `head_limits` found no declared or measured row there, so it reported the default.
- **Reproduced on the unchanged 0.4.5 source.** Two 8-slot heads, `qwen3.5:4b` declared 4 on both, unmeasured limit 1. In both enrollment orders, the first head reported `4 declared` and the second reported `1 serial_family`. 0.4.6 reports `4 declared` on both.
- **Scope.** Only `/v1/models` (and so Capstone's displayed limits) was wrong. The scheduler (`placement.View`) and the observation file read the map themselves and were right. This defect alone does not explain any scheduling behaviour.
- **Repair.** The two values have separate names: `capacity_evidence` and `qualification_evidence`. `model_capacity.head_limits` now refuses anything that is not the `(resource_id, model)` map (TypeError), so a regression cannot pass silently.
- **Tests.** `test_g2_every_head_reports_its_declared_limit…` runs two heads, limit 4, both traversal orders. It checks `/v1/models`, the observation and `placement.View` for the same head and model, and that the scheduler runs exactly 4 per head with the ninth call queued. `test_g2_head_limits_refuses…` covers the guard.

## G1: cheap policy and discovery reads (confirmed mechanism, repaired; lab latency to be measured)

- **Cause, confirmed in code.** `policy()` called `availability()` twice for every authorized model. With 36 models that is 72 full calculations, and each one re-read every head's inventory, evidence rows (JSON-parsed), suspensions and occupancy. `models()` and each `/v1/availability` repeated the same evidence scans. Every waiting dynamic request also rebuilt the whole `/v1/models` view after each health cycle.
- **Repair.**
  - One read snapshot per call (`_context`). Per-head qualification facts are cached (`_facts`). Evidence is immutable (insert-only triggers), so the cache key `(endpoint, row count, highest rowid)` is exact and a new test invalidates it immediately. Health, inventory, suspensions, occupancy and authorization are never cached.
  - New additive index `capacity_qualification_evidence(resource_id, model, at)`.
  - `GET /v1/policy?availability=0` reads no head at all.
  - New `GET /v1/fleet`: an application-scoped snapshot, scoped to models, operations, context and capabilities. It is shared by every caller of that application for at most 5 s while its inputs are identical. Its `snapshot_id` is a hash of all inputs, so any revocation, suspension, digest change or new qualification gives a new snapshot at once.
  - Dynamic-node hints are shared per scheduler generation (at most 5 s).
- **Authoritative checks unchanged.** Dispatch still reads proofs from the ledger, re-reads `/api/tags`, `/api/show` and `/api/version`, and re-checks authorization, model permission, head health and lease before sending.
- **Tests.**
  - `test_g1_policy_reads_one_snapshot…`: 36 models, one snapshot per policy read, zero for policy-only, and statuses equal to `/v1/availability`.
  - `test_g1_qualification_facts_are_rebuilt…`
  - `test_g1_fleet_snapshot_is_shared_scoped_and_never_stale…`: cache hit, other application, suspension, digest change, revocation.
  - `test_g1_a_cached_discovery_never_authorizes_dispatch…`
- **Measured (fixed workload, `scripts/control_plane_load.py`, 3 runs per tree, this laptop).** In the table, "rc.24 launch" means 24 concurrent launches of Capstone rc.24's sequence (models, policy, availability per advertised model):

  | rc.24 launch | 0.4.5 | 0.4.6 |
  |---|---|---|
  | `/v1/policy` p50 | 4,820–9,226 ms | 104–167 ms |
  | `/v1/models` p50 | 903–1,248 ms | 219–435 ms |
  | whole storm wall time | 6.9–12.5 s | 0.93–1.36 s |
  | 0.4.6 scoped path (policy-only + fleet) | n/a | 2 calls per launch instead of 9; 171 ms wall |

  On the 0.4.6 scoped path, work per launch no longer grows with the catalog. On the rc.24 path, the same number of per-head reasons is evaluated, but from one snapshot. Files: `verification/control-plane-0.4.5.json`, `control-plane-0.4.6.json`, `control-plane-repeats-0.4.6.json`.
- **Remaining.** Hub latency is unmeasured until `lab-tools/launch-storm` runs there. Capstone must adopt the scoped path to remove the per-worker storm. The Gateway cannot stop a client from calling the full policy.

## G3: ledger contention and health refresh (repaired; lab effect to be measured)

- **Cause, confirmed in code.**
  - `place()` built the full placement view, parsed every head's JSON columns for every waiting request and queried attempts per request, all inside the write lock, on every scheduler event, including passes that could grant nothing.
  - The health cycle took three write transactions per head.
  - One `except Exception` covered both the head's version/tags answer and the Gateway's own ledger writes. A `LedgerBusy` while recording inventory therefore marked a head that had just answered as **offline**, and a refused health write ended the cycle for the remaining heads.
- **Repair.**
  - **Placement.** Each pass is first planned on a read snapshot outside the lock. With nothing to grant, withdraw or expire, no write is taken. Otherwise the plan is made again inside the write transaction on current state (the safe recheck) and applied. Within a pass, a `(model, candidate heads, operation)` that found no head is skipped for the rest of the pass. This is safe because later grants only add leases. Heads' JSON is parsed once per pass and attempts are read with one query. The scheduler's gap after a pass is at least the pass's own duration (max 0.5 s), so wakeups coalesce. Fair order with aging, per-application limits, per-model limits, idempotency and the uncertain-execution rules are unchanged.
  - **Health.** Network stages (version, tags, ps) are separate from local stages (qualification read, head record). Only a failed head answer marks a head offline, and its event carries `stage` and `cause`. A local failure leaves the head's health as it was, is logged as `health.local_failure` with stage, head and `ledger_busy`, and counted (`health_local_failures`). The cycle continues with the other heads. Inventory, runtime and health go into one write per head.
- **Tests.**
  - `test_g3_a_full_fleet_pass_takes_no_write_lock…`: no `place` write while nothing can be granted; app limit holds; all requests complete in turn.
  - `test_g3_unplaceable_signatures…`
  - `test_g3_a_local_ledger_failure…`: an injected LedgerBusy leaves an answering head healthy; a refused head goes offline with stage `version`.
  - `test_g3_one_write_transaction_per_head…`
  - All earlier placement, fairness and recovery tests still pass.
- **Measured (same workload, 24 callers × 45 s, 3 runs):**

  | Admission under load | 0.4.5 | 0.4.6 |
  |---|---|---|
  | placement write transactions | 341–479 | 201–323 |
  | placement lock-hold total | 6.1–8.2 s | 1.8–2.2 s |
  | placement hold p95 / max | 43–87 ms / 221–568 ms | 18–34 ms / 40–76 ms |
  | health write transactions | 324–405 | 153–189 |
  | health cycle p50 | 0.91–1.72 s | 0.18–0.68 s |
  | admission p95 | 0.93–2.05 s | 0.52–0.92 s |
  | completions in 45 s | 444–576 | 522–700 |
  | request-path write waits (submit/complete p95) | 127–219 ms | 77–238 ms: **no clear change** |

  The laptop workload uses fake heads, so it is not the hub's 789 slow transactions. It shows the work removed from inside the lock, not the hub's numbers.
- **Not established.** Heads 7, 9 and 10's brief offline periods in c922 have no retained cause. A LedgerBusy during inventory recording is one mechanism that would have produced them under 0.4.5 (**hypothesis**). 0.4.6 logs the stage, so the next occurrence will say which.

## G4: upstream error bodies; Gemma 500 (capture repaired; 500 reproduced in evidence, cause unresolved)

- **Repair.**
  - `Gateway.upstream` reads an HTTP error's body once, bounded to 4 KiB, decoding with replacement. It keeps the body on the exception and re-raises the same `HTTPError`, so `recovery.classify` still gives 4xx `request_fault` and 5xx `model_on_head`.
  - `_settle` records one row per failed attempt in the new protected table `capacity_upstream_errors`: request ID, attempt, application, head, model, digest, runtime version, operation, phase, class, status, message, body, truncation, decode errors, and request shape. The shape has fields, message and role counts, sizes, format kind, schema property names and hash, tool names and numeric options, and never content. The app's own token and credential patterns are redacted.
  - The application's status envelope gets `upstream_errors` with a message of at most 1,000 characters. The 0.4.5 `error` string is unchanged.
  - The operator reads full rows with `lifecycle upstream-errors [--request ID] [--since-seconds N]`.
  - The observation file gets counts only (`last_hour.upstream_http_errors`).
  - A failed qualification test keeps `upstream_error` in its evidence.
- **Tests.** `test_g4_a_500_keeps_its_body…` (class, suspension, message, redaction, shape without content, observation without body), `test_g4_a_400_stays_a_request_fault…` (bounded, undecodable, missing body), `test_g4_the_real_upstream_path…` (a real HTTP 500 through `urllib`), and `test_g4_request_shape_has_no_content`.
- **Real reply check (local Ollama 0.40.16, qwen3:4b; never a lab head; `verification/live-ollama-0.4.6.json`).**
  - A wrongly typed option is answered with **HTTP 500** `option "temperature" must be of type float32`. The capture kept it as status, message, body and shape, and the class stayed `model_on_head`.
  - This exposed a defect (**confirmed**): a client's malformed option suspended a healthy model on a head, and on two heads it would cascade to every head. 0.4.5 checked only `num_predict` and `num_ctx`.
  - Repair: `temperature`, `top_p` and `repeat_penalty` must be finite numbers, `seed` and `top_k` integers, and `stop` a list of up to 16 strings. Anything else is refused with 400 before a request exists. Capstone's and c922's requests all send numbers.
  - Test: `test_g4_a_wrongly_typed_option_is_refused…`.
  - This does not explain the Gemma 500s: their options are `num_ctx 8192, num_predict 320, seed 7, temperature 0`, all correctly typed.
- **What c922 shows (read from `calls.json`, not a lab run).** The 18 saved gemma3:4b customer-communication requests are only four distinct bodies (temperature 0, seed 7, the same `{"reply": string}` schema, num_ctx 8192, num_predict 320):
  - the target body (`REQ-90426799…`): 3 of 3 failed, on heads 10, 3 and 9;
  - another body: 5 of 5 failed, on heads 2, 8 and 4;
  - a third: 4 of 5 failed;
  - the fourth: 0 of 5 failed, on heads 2, 10 and 8.

  The failure follows the request body across heads, which points at the request content or shape rather than a head (**hypothesis**). The body text that would explain it was never recorded.
- **Remaining.** `lab-tools/g4-gemma500-repro` replays the four bodies and five variants of the target (no format, format "json", num_predict 1024, seed 8, num_ctx 4096) on one idle head and keeps the bodies. Until it runs, the Gemma 500 is **unresolved**. No targeted repair is made, and none is claimed.

## G5: installed, qualified and available (repaired, additive)

- `/v1/fleet` reports `installed` (observed inventory with freshness), `qualified` (current evidence per operation and capability, with context) and `available` (now: the same reasons as `/v1/availability`, per head), plus per-head parallel limit and suspension.
- An installed, suspended model is "installed, qualified, model_suspended", never "not installed". Absent, unpermitted, unqualified, busy, stale, digest-changed and runtime-changed each have their own state.
- `/v1/models` keeps its exact 0.2.0 shape (`tests/test_client_contract.py` pins it), so a suspended model still drops out there.
- **Test.** `test_g5_installed_qualified_and_available_are_separate_states`.

## Phase 4 (Gateway side): qualified request shapes

- New capability purpose **`structured_chat`**: `/api/chat` with system and user messages and a closed JSON schema, which is the shape of Capstone's structured roles and of a mediated tool strategy. Qualify with `qualify --purpose structured_chat`.
- `tools` (native), `structured_output` (generate with schema) and `chat` are unchanged.
- Provenance names `structured_chat` for chat requests with `format`.
- **Test.** `test_structured_chat_is_qualified_on_its_own_shape…`.
- Passing the fixture does not prove a particular role's real payload works (see G4).

## G6: capacity

There is no Gateway code change for G6. `lab-tools/g6-capacity-check` runs the same admitted request mix (39 completed c922 requests, qwen3.5:4b roles plus the qwen3:4b reviewer) at 16, 18 and 24 callers. It reports useful completions, failures, queue/dispatch/execution p50/p95, ledger and health overhead, per-head overlap and reviewer share. It chooses nothing.

## Found and fixed while verifying (confirmed defects, present since 0.4.4)

- **Graceful stop could lose a just-finished `/v1/submit` result.**
  - Cause 1: the background execution's own final status read marked the result as collected by its caller. So `unread_results()` was always 0 for submitted requests, and the stop never waited for them. The same cause meant `request.unclaimed` never fired for `/v1/submit` requests, so a dead worker's finished result went unreported.
  - Cause 2: a status read counts as collected when its handler starts. The listener could close before that handler sent its reply.
  - Evidence: `tests/test_windows_service.py::test_graceful_stop_drains_in_flight_work_then_resumes_on_start` failed 3 of 4 runs on the unchanged 0.4.5 code and 5 of 6 with the first 0.4.6 build on this laptop. The ledger showed the request completed 0.7 s before the stop.
  - Repair: the internal read is `result(..., claim=False)`, and the stop waits up to 2 s (`HANDLER_GRACE_SECONDS`) for running handlers to send their replies.
  - Result: 8 of 8 runs passed afterwards. Regression test: `test_a_background_result_counts_as_collected_only_when_the_caller_reads_it`.
  - Effect on the hub: the update and any service stop no longer cut off a caller's last result. `request.unclaimed` now reports finished results that no caller read within 120 s (expected after a worker is killed). This is a log line and a counter, not a failure.

## Fixed after the coordinator's review (same release number, rebuilt 2026-11-19)

- **HIGH, confirmed defect in the first 0.4.6 build: a malformed `tools` entry could quarantine a healthy head.**
  - Cause: `request_shape` raised `AttributeError` on `{"function": "record_value"}`. It ran in `_settle` before the settlement, so the attempt was never recorded, the lease stayed `executing`, the worker died and renewals stopped. When the lease expired, `_expire` held the whole head as uncertain execution. 0.4.5 settled the same request as a plain `request_fault`.
  - Repair, in four layers:
    1. `validate()` refuses malformed `tools` (each must be `{"type": "function", "function": {"name": str, "description": str, "parameters": object}}`, at most 64) and non-string `images` with a 400, before any request exists. All 248 tool entries in c922 have this form.
    2. `request_shape` never raises; on malformed input it names what it saw, for example `tool_names: ["<str>"]`.
    3. The whole upstream capture (`_upstream_capture`) never raises. The diagnostic row is written inside its own savepoint, so a failed insert rolls back only that row and never the settlement.
    4. If `_settle` itself raises, `_settle_fallback` records the attempt and fails the request. It clears `executing` unless the failure really is uncertain execution, takes no suspension or hold, and never raises. A status read whose diagnostic table cannot be read still answers.
  - Audit of everything between a reply and the settlement:
    - `classify` is pure;
    - `_deadline_on_live_head` catches transport errors;
    - `authenticate` is 0.4.5 code;
    - the 0.4.6 diagnostics are guarded as above;
    - any other exception now reaches the fallback.
  - Tests:
    - `test_review_malformed_tools_are_refused_before_any_request_exists` (8 shapes);
    - `test_review_the_reported_scenario_cannot_hold_a_head…` (the reviewer's repro, with the old lenient validation forced back on);
    - `test_review_request_shape_never_raises` (11 malformed payloads);
    - `test_review_a_failing_capture_or_record_never_stops_the_settlement`;
    - `test_review_if_settle_itself_raises_the_fallback…`.
- **LOW 1.** Integer options (`seed`, `top_k`) accept whole-number floats (`7.0`, `40.0`), as Capstone's settings and Ollama do. `7.5`, booleans and infinities are still refused.
- **LOW 2.** `capacity_upstream_errors` is bounded to 14 days (`UPSTREAM_ERROR_DAYS`) and 20,000 rows (`UPSTREAM_ERROR_ROWS`, newest kept). It is pruned from the health cycle at most every 10 minutes, and a failed prune is a logged local failure.
- **LOW 3.** The row's `operation` is now the call that failed (`/api/tags`, `/api/show`, `/api/version` or the inference operation); `shape.operation` is the request's own operation.
- **LOW 4.** `/v1/fleet` snapshots carry `served_at`. SDK `fleet_cached` reuses a reply only for `valid_until - served_at` (both on the Gateway's clock). Before, it could reuse a snapshot served from the Gateway's cache for up to about twice its validity.
- **LOW 5, hub visibility.** Since `request.unclaimed` now works for `/v1/submit`, the hub log and `gateway_load.counters.results_unclaimed` will show counts that 0.4.4 and 0.4.5 never produced. Each is a finished result that no caller read within 120 s, typically a killed or timed-out worker. It is not a Gateway failure and not a lost inference. Arena and the provisioner should not alert on it as a fault.
- **Publisher test.** `test_publication_scripts::test_task_plan_defines_a_supervised_five_second_publisher` runs `python -I -m inference_gateway.observation_publisher` in a subprocess. `-I` ignores `PYTHONPATH`, so it passes only where the package is installed in that interpreter. Here it is installed editable in `.venv`. It is environment-only, the file is unchanged since 0.4.5, and on the hub the release is installed with `pip --target`.

- **Re-review (three low items).**
  - The fallback writes `invocation.failed` only when it settles a still-active invocation, so a failure `_settle` already recorded is not counted twice.
  - For uncertain execution the fallback now takes the normal path's immediate action. It checks the deadline on a live head first, as `_settle` does, and then holds the head until the request's deadline, instead of waiting for the lease to expire. It takes no other action.
  - A failed retention prune is listed in that cycle's `local_failures`.
  - Tests: `test_rereview_*` (3).
- **Temporary lab identity (operator decision, 2026-11-19).** Three new `credentials` verbs, all operator-only, each printing one JSON object:
  - `reissue --app-id X --policy <clients-only file> --output <token>`: issue, or for an existing row revoke it, apply its policy entry and give it a new token (enabled). It changes only X's `capacity_clients` row (token hash, enabled, models, max_concurrency, queue_limit, priority) and its `capacity_client_limits` row (operations, max_context). No bootstrap runs.
  - `grant-heads --app-id X --like T`: adds X to `capacity_resources.applications` on exactly the heads where T is listed, with one `resource.application_granted` event per head. It refuses an unknown or disabled X, an unknown T, or X already on a head T is not on.
  - `withdraw-heads --app-id X`: removes X from every head's list, with an event per head. It refuses while X is enabled and has work.
  - A test compares, before and after a full issue → grant → use → revoke → withdraw cycle: every head's slots, memory_gb, health and qualified_models, `capacity_model_capacity`, the policy blocks, qualification evidence, suspensions, inventory, and other clients' rows and limits. All are identical; the head lists return exactly as they were. `credentials revoke` leaves X as a disabled row.
  - G6 and the launch storm use these to issue `lab-capacity-check` (qwen3.5:4b and qwen3:4b only, 24 at once, priority 0, the heads of `capstone-research`) and revoke it in a `finally`. The token is kept under `C:\ProgramData\AgentArenaLab\lab-capacity-check` and deleted afterwards. A crashed run is cleaned up at the next start. `--client-config` remains.
- **Third review (HIGH, confirmed defect in the 70b899cf build): the temporary verbs accepted any application.** `withdraw-heads --app-id capstone` removed capstone from every head; `reissue --app-id capstone` revoked capstone and rotated its token; `grant-heads --app-id capstone-research --like capstone` widened research. Repaired:
  - `reissue`, `grant-heads` and `withdraw-heads` act only on `lab-<lowercase letters, digits, hyphens>` names;
  - they act only on applications the Gateway itself issued as temporary (the ledger's `temporary_clients` service-policy key, written by `reissue`; older releases ignore it);
  - `reissue` accepts only a clients-only policy declaring exactly that application.
  - Anything else is refused with the reason, and nothing changes. `--like` may name any application and is only read.
  - Tests: `test_round3_the_temporary_verbs_refuse_every_real_or_lookalike_application_and_change_nothing` (capstone, capstone-research, a lookalike `capstone-researchx`, the provisioned `lab-traffic`, and four malformed names; the whole ledger compared before and after; every real token still works) and `test_round3_reissue_refuses_the_infrastructure_policy…`.
- **Fourth review (confirmed defect in the bdc15737 build): a temporary mark could land on a real application.**
  - Cause: `reissue` wrote the mark before issuing, and nothing cleared it. A reissue that failed (for example, its token file already existed) left `lab-traffic` marked. A later ordinary `credentials issue` inherited the mark, and the stale expiry then revoked the identity.
  - Repair:
    - `reissue` creates the token file first, then does everything in one write transaction (`CapacityBroker.issue_temporary`): it revokes an earlier row of the identity, writes the client row and limits, and sets the mark last. Inside that transaction it re-checks that an existing row is temporary. A failure leaves nothing.
    - `credentials issue`, `rotate` and bootstrap clear any mark in their own transaction (event `temporary_client.cleared`).
    - The expiry path re-checks inside its write transaction that the identity is still temporary, still past that expiry and not yet cleaned, before it revokes and withdraws. So a reissue that wins the race keeps its fresh expiry.
    - A handled expiry is marked `cleaned_at`, never handled twice, and the entry is kept so the tool can reissue the identity.
  - Tests: `test_round4_a_failed_reissue_leaves_no_mark…`, `test_round4_an_ordinary_issue_rotate_or_bootstrap_clears_a_stale_mark`, `test_round4_an_expired_then_reissued_identity_is_not_revoked_by_the_old_expiry`.
- **Temporary identities expire** (`reissue --expires-in-seconds`, 60 s to 24 h, default 4 h). At the expiry the Gateway refuses the token, and its health cycle revokes the identity and withdraws it from the heads (event `temporary_client.expired`). Their queued requests never age in priority and always come after every real application's (placement). Tests: `test_round3_a_temporary_identity_expires…`, `test_round3_a_temporary_identity_never_outranks…`.
- **Lab tools (third review):**
  - the token folder's owner and permissions, its contents and its parent are checked before use, and a new folder is created with a verified private ACL;
  - `--cleanup`, and an `IF-INTERRUPTED.txt` note in the results;
  - G6 stops on outside queued or running work (from the ledger, every 2 s) and revokes at once;
  - the launch storm watches during every round;
  - the queue limit equals the concurrency (24).
  - Tests: `test_a_token_folder_someone_else_may_use_is_refused…`, `test_cleanup_mode…`, `test_g6_stops_at_once_on_outside_queued_work…`.
- **G6 levels.** The defaults are 1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22 and 24 callers (30 s warm-up + 4 min each, about 60–65 min). G6 prints progress, and `report.json` and `summary.txt` are rewritten after every level.
- **Lab tools, found while re-testing:**
  - The G6 request mix was grouped by role with the reviewer requests last, so a short or slow window under-sent the reviewer (one test run measured a reviewer share of 0). The mix is now interleaved by a fixed seeded shuffle, and callers start at evenly spread offsets.
  - All tools retry an observation read that collides with the publisher's atomic replace (about 2 s) before treating the file as unreadable.

## Other additions

- `GET /health` adds `release` and `features`.
- Per-route counts and times (`gateway_load.routes`), `gateway_load.health_cycle` and `gateway_load.ledger.placement` are published in the observation's `gateway_load`.

## Compatibility and upgrade

- **Ledger.** Additive only: one index and one table. Older releases ignore both. Rollback to 0.4.5 and earlier works.
- **Configuration.** Nothing new. `gateway-service.json`, deployment settings, the bootstrap policy and credentials are untouched. Do not re-run bootstrap or credentials for the update.
- **Service.** `update` restarts it once. On first start 0.4.6 creates the index. On a large evidence table this takes seconds, inside the start.
- **Observation** `shared.inference.observation/1`, additive keys only:
  - `last_hour.upstream_http_errors`: rows of `resource_id`, `model`, `operation`, `status`, `count`, `last_at`;
  - `gateway_load.routes`, `gateway_load.health_cycle`, `gateway_load.ledger.placement`;
  - event payloads (`resource.health_changed` gains `stage` and `cause`), which are not exported.
- **Client.** 0.4.1 clients work unchanged. Client 0.4.2 ships in `client/`.

## Tests

`tests/test_release_0_4_6.py` (19), `tests/test_lab_tools.py` (5). Four earlier tests were adjusted for additive changes: two client-version pins, the capability-state set (adds `structured_chat: not_tested`), and the concurrency client version. Full results are in `verification/test-report.json`.
