# Inference Gateway 0.4.5: release notes (2026-11-18)

Built for the Agent Arena lab provisioner 1.6.23. Update from 0.4.4 with the deploy tool's `update` verb. The ledger is kept, and nothing in it changes. The new placement key defaults to the 0.4.4 behaviour.

## Placement: a limit for models with no current evidence on a head

- **New key** `placement_policy.unmeasured_model_limit`, an integer from 0 to 64, default 0. It applies to a model that is not in a serial family and has no current declared or measured limit on a head. With a value above 0, the Gateway runs at most that many leases of the model on that head (source `unmeasured_default`, reason `no_current_evidence`), and the others wait in the Gateway queue. With 0 it keeps 0.4.4's behaviour, which is the head's slots. A current declared or measured value still wins, `model_parallel` still wins over everything, and the limit never exceeds the head's slots.
- **Why.** In the lab on 2026-11-18, the reviewer qwen3:4b had no declaration on the 16 GB heads 3, 4 and 5, so it got their 8 slots. Heads 3 and 5 ran 7 and 8 reviewer calls at once and 11 of those calls timed out. Per-call speed fell from 11.6 to 3.5 tok/s, and the reviewer's memory kept the main model off those heads. The lab sets the key to 1 through its bootstrap (APPLY-MODEL-CAPACITY). A later bootstrap without a `placement_policy` block keeps it.
- **Tests:** `tests/test_release_0_4_5.py` covers validation, precedence, stale evidence, admission (1 running and 4 queued on an 8-slot head; 3 with a declaration; 5 with 0) and persistence across bootstraps.

## Publication ACL script: a file that disappears is skipped

`scripts/Set-ObservationPublicationAcl.ps1` now skips a file that no longer exists between the folder listing and the ACL read. The case is SQLite's `capacity-ledger.db-shm`, which 0.4.4 creates and removes constantly. Any other read failure still fails the check. The lab hub failed on this twice on 2026-11-17. Reproduced against the 0.4.4 script: 3 of 12 runs failed as shipped, 0 of 12 with the fix.

## Observation: which head and model the last hour's failures were on

**What the hub showed (evidence `HUB\evidence-20261118-102700`).**
- The published observation said `last_hour.invocations_updated.failed: 11`.
- The ledger showed all 11 were Capstone reviewer calls (`capstone-research`, `qwen3:4b`) that timed out at their own deadline (`deadline_exceeded`): 6 on `head-192-0-2-5` and 5 on `head-192-0-2-3`. Both heads stayed `healthy` throughout.
- The document had no way to say so. `events` carry no payload, and the counts have no head or model. Arena could only show a lifetime total.

**Added (additive, `shared.inference.observation/1` unchanged).** Two keys inside `last_hour`:

| Field | Meaning |
|---|---|
| `last_hour.invocation_failures` | Invocations whose state is `failed` and that were updated in the window, grouped by `resource_id`, `model`, `app_id` and `class`. Each row also has `count` and `last_at` (the newest failure in the group). Largest `count` first. At most **50** rows (`capacity_observe.FAILURE_ROWS`) |
| `last_hour.invocation_failures_omitted` | Failures in groups beyond the 50-row cap: counted, not listed. 0 normally |

- `resource_id` is the head of the request's last attempt (or the request's head). `null` means the request never reached a head, for example an admission deadline.
- `class` is the recovery failure class: `deadline_exceeded`, `uncertain_execution`, `head_unreachable`, `model_on_head` or `request_fault`. `null` means unclassified.
- A failed request is not a head fault. A head that is down shows in `resources[].health`, and with `head_unreachable` here. `deadline_exceeded` on a `healthy` head is the request running out of its own time.
- The private error string (`capacity_invocations.error`) is still never exported. Neither are endpoints, payloads or client keys.
- On a ledger without `capacity_request_attempts` (before 0.4.0) `class` is `null` and the head comes from the request.

**Cost.** One grouped query on the `capacity_invocations_state` index, with primary-key lookups on attempts and requests. On the 380 MB evidence ledger it takes 0.15 s.

**Compatibility.**
- Top-level keys and every existing `last_hour` key are unchanged.
- Arena releases that predate this ignore the new keys.
- Arena reads them from its Gateway observation adapter: `arena_service/adapters/gateway/observation.py`.

**Not changed.** Placement, capacity, timeouts and qualification.

**Tests.** `tests/test_observation.py::test_last_hour_failures_name_head_model_and_class_without_the_error` covers the grouping, the window, a request that never reached a head, the cap and that the error string stays private. `test_expiry_revocation_failure_and_restart_are_evented` checks the row for a real failed execution.
