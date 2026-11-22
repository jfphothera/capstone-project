# Inference Gateway 0.4.3: release notes (2026-11-14)

This release is the same type as 0.4.1 and 0.4.2:
- It updates 0.4.2 in place with `gateway-deploy.py update`.
- It keeps the ledger, with the same `ledger_id`.
- It keeps `shared.inference/1` and `shared.inference.observation/1`.

One additive ledger change: a nullable column, `capacity_inventory_runtime.families`. Releases 0.4.0–0.4.2 ignore it, so rolling back to 0.4.2 still works.

Everything in [RELEASE-NOTES-0.4.2.md](RELEASE-NOTES-0.4.2.md) and [RELEASE-NOTES-0.4.1.md](RELEASE-NOTES-0.4.1.md) still applies.

**Client library unchanged.** Capstone keeps `client/shared_inference_client.py` **0.4.1** (SHA-256 `e7065e412bdf7e6777714ab51ebafa520c2984e344d964741b9f60f7a4bdce02`).

## What the lab found (2026-11-14)

The run:
- Hub on 0.4.2; nine heads on Ollama 0.40.13.
- Capstone Testing Lab campaign `EXP-7c9689c6`: one model, `qwen3.5:4b`, at most six calls at once, 32 runs.
- Head slots: 10 on heads .2 and .7–.10, 8 on .3–.5, 4 on .6.

Result: 11 of the 32 runs failed, each with one call that reached Capstone's 45 s per-call deadline. The ledger and the run evidence show three causes.

1. **Placement piled every call onto one head.**
   - Placement ranked any head that had the model loaded above every idle head. Busyness only broke ties inside a tier, and a loaded head dropped out only when it was full (`active >= slots`).
   - With 10 slots and 6 calls, it was never full. For four minutes all six calls went to head .8; heads .2–.6 got none during the whole run.
2. **The head ran those calls one at a time.**
   - Ollama serves some architectures one request at a time, whatever `OLLAMA_NUM_PARALLEL` says. The `qwen35` family (every qwen3.5 build) and `qwen35moe` are on that list ([ollama#18938](https://github.com/ollama/ollama/issues/18938), [ollama#14510](https://github.com/ollama/ollama/issues/14510); the fix [ollama#17144](https://github.com/ollama/ollama/pull/17144) is not merged).
   - Measured in the run: none of the 194 completed calls overlapped another on its head. Generation held at about 17 tokens/s, and each call computed for about 5.7 s.
   - The rest of each call's time was spent waiting inside Ollama. The Gateway counted those waiting calls as executing.
3. **A call that reached its own deadline held the whole head.**
   - The transport timeout is the request's remaining deadline. A timeout in dispatch was `uncertain_execution`, which holds the whole head.
   - The probe could start only after the 2-minute keep-alive emptied the head, so each hold lasted about 150 s.
   - The next loaded head then took every call and was held the same way: heads .8 → .9 → .10 → .8 → .7. All five holds recovered with passing probes. The cascade rule never fired, because it counts only `model_on_head`.

## Changes

### 1. Placement spreads load: `placement_policy.strategy`, default `least_busy`
- `least_busy` (new default): the eligible head with the fewest active leases per slot wins.
  - Among equally busy heads, the 0.4.0 tier breaks the tie: loaded, then fits, then evicts.
  - A head that would have to unload idle models (tier 3) comes after every head that need not.
  - Six calls over four free heads now go to four heads, one each.
- `loaded_first`: the 0.4.0–0.4.2 order, kept for operators who want it.
- Code: `placement.View.rank` and `View.choose`.

### 2. Per-model limit per head: `placement_policy.serial_families` and `model_parallel`
- A head takes at most N leases of one model, whatever its `slots`. N is:
  1. `placement_policy.model_parallel[<model name>]` (1–64), if set;
  2. otherwise 1, when the model's family, as the head's own `/api/tags` reports it (`details.family`, `details.families`), is in `placement_policy.serial_families` (default `["qwen35", "qwen35moe"]`);
  3. otherwise the head's `slots`.
- Families are recorded with every health refresh in the new `capacity_inventory_runtime.families` column.
- Other models still share the head's remaining slots.
- A second call of a serial model waits in the Gateway queue, where its wait is measured, or goes to another head, instead of waiting unseen inside Ollama.
- Code: `placement.View.model_limit` and `View.tier`; `Gateway._record_runtime`.

### 3. A deadline on a live head fails only that call: class `deadline_exceeded`, `failure_policy.deadline_liveness_seconds`
- When a dispatched call times out at its own deadline (a plain timeout, at or after the deadline), the Gateway asks the head `/api/version` and waits up to `deadline_liveness_seconds` (default 3 s).
  - **If the head answers:** the request fails with class `deadline_exceeded`. There is no hold, no fault and no replay, and the head keeps serving. Events: `invocation.deadline_exceeded`, then `invocation.failed {class: deadline_exceeded}`.
  - **If the head doesn't answer:** the 0.4.0 path, `uncertain_execution`, which holds the whole head.
- A reset, a truncated reply, or a timeout well before the deadline stays `uncertain_execution`, as before.
- `deadline_liveness_seconds: 0` restores the 0.4.2 behaviour.
- The broker's lease-expiry pass now leaves a call that this process is still running to its own settlement, even at the deadline. Before, it could expire the lease and hold the head before the liveness check ran. A lease that stops being live is expired on a later pass, as before.
- Code: `Gateway._deadline_on_live_head`, `Gateway._settle`, `CapacityBroker` expiry; `recovery.DEADLINE`.

## Unchanged
- `slots` still caps the leases on a head. Set it to the head's `OLLAMA_NUM_PARALLEL` for models Ollama does run in parallel.
- Admission, priorities, `max_concurrency` and `queue_limit`, qualification and the probe test.
- The `2m` keep-alive, the hold and probe timing, and the cascade rule.
- The client library, and both contracts.

## How it was tested

| What | Result |
|---|---|
| `tests/test_release_0_4_3.py`: the field run replayed on fake heads (four heads with 10 slots each, model loaded on one, four calls at once), the serial family, the `model_parallel` override, a serial model sharing a head with another model, a deadline on a live head, on a dead head, with the liveness check off, an early timeout, the policy defaults and validation, and the families column | 12 passed |
| `tests/test_failures_and_placement.py`: the 0.4.0 tier test now runs under `strategy: loaded_first`; everything else unchanged | 15 passed |
| Full suite (`verification/test-report.json`) | See the report: same environmental exceptions as 0.4.2 (the ACL-script tests that need expandable local Administrators) |

**Not tested here:** the lab heads themselves (Ollama 0.40.13), and a real qwen3.5 run through 0.4.3. The next Testing Lab campaign with the same settings is that test: expect at most one qwen3.5 call per head, calls on several heads, and no `resource.held` from deadlines.

## Upgrade notes (0.4.2 → 0.4.3)
- Run `update` from the 0.4.3 package. Expect `ledger.kept: true` and `held_at_upgrade: []`. The `families` column is added when the service starts, and filled at the first health refresh (15 s).
- No requalification needed.
- Policy: a stored `placement_policy` or `failure_policy` without the new keys gets their defaults. Bootstrap refuses unknown values: `strategy` must be `least_busy` or `loaded_first`, `model_parallel` values must be 1–64, and `deadline_liveness_seconds` must be 0–30.
- Rollback to 0.4.2 is supported: the extra column is ignored.
