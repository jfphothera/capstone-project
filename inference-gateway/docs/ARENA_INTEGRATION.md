# Gateway boundary for the independent Agent Arena team

The gateway and Arena share the admin hub **192.0.2.254** but stay independent services, releases, accounts, databases and failure domains. Stopping or restarting Arena does not stop gateway inference. The gateway imports no Arena code, never writes Arena's database, and never asks Arena for scheduling, authorization or workflow decisions. Arena's side is `AgentArena/docs/handoff/GATEWAY-OBSERVATION-INTEGRATION.md`; this document is the gateway's answer to it.

**Arena reads one published file** and nothing else from the gateway:
- no ledger access;
- no exporter execution;
- no gateway HTTP API;
- no gateway credential.

Nothing below is deployed. No machine address, firewall rule, account, scheduled task or Windows service was created or changed.

## 1. Supervised publisher

`The publisher (`inference_gateway.observation_publisher`) runs **inside the `InferenceGateway` Windows service** (0.3.0 default), under the gateway's service account `svc-inference-gw`. It is configured by the `observation` block of `gateway-service.json`. A stopped service therefore stops publication, and Arena then sees the gateway as stale, which is true. Publisher failures are logged and never stop inference.

Every **5 s** it does one cycle:
1. Take one read-only `capacity_observe.snapshot`. This is the same function, with the same output, as `inference-observe --service-id shared-inference`.
2. Write the JSON to a temporary file in the publication folder and `fsync` it.
3. **Atomically replace** the published file with one rename.
4. Persist the cursor.

The publisher never admits, leases, reconciles, qualifies or writes the ledger: it opens it with SQLite `mode=ro` and `query_only`.

- **Reader contention.** Windows refuses the rename while a reader has the file open. The publisher retries for up to one second, then skips that cycle and tries again 5 s later. Arena keeps its last good document meanwhile.
- **Cleanup.** A failed write removes its temporary file. A publisher killed mid-write leaves an orphan, which is deleted on the next start once it is older than 60 s.
- **Single instance.** Each cycle holds a kernel lock on the state file. This is a separate lock from the gateway's ledger service lock.
- **Logging.** Only state transitions are logged (`observation.published`, `observation.blocked`, `observation.failed`), into the service log.
- **Alternative supervisor.** It is not used by default. `scripts/Register-ObservationPublisherTask.ps1` defines a Task Scheduler task:
  - runs at boot, before any login, under the gateway account;
  - a repeating 1-minute trigger with `IgnoreNew` restarts a stopped publisher within a minute and never starts a second one;
  - no time limit.
  - `-PlanOnly` prints the task XML.

  Task Scheduler cannot run as an `NT SERVICE\…` virtual account. The task needs batch-logon rights and a stored credential for `svc-inference-gw`.

## 2. Published file location and ACL on 192.0.2.254

| Item | Path (configurable) | Access |
|---|---|---|
| Published document | `C:\ProgramData\InferenceGateway\observation\shared-inference-observation.json` | Gateway account: modify, the only writer. **Arena console account: read on files in this folder only**: no listing, no write, no subfolders |
| Publisher state (cursors, acceptances) | `C:\ProgramData\InferenceGateway\observation-state\publisher-state.json` | Gateway account, SYSTEM, Administrators. No Arena |
| Service and publisher log | `C:\ProgramData\InferenceGateway\logs\gateway-service.log` | Gateway account, SYSTEM, Administrators. No Arena |
| Ledger | `C:\ProgramData\InferenceGateway\state\capacity-ledger.db` | Gateway account, SYSTEM, Administrators. **Never Arena** |

`scripts/Set-ObservationPublicationAcl.ps1` applies these with `icacls`:
- Inheritance is removed. Grants: SYSTEM and Administrators full; gateway account modify; Arena account `(OI)(IO)R` on the publication folder.
- The ACE is inherit-only and files-only, so each newly renamed publication carries Arena's read ACE and the folder itself grants Arena nothing.
- Transient `.shared-inference-observation.json.*.tmp` files inherit the same read ACE. They hold the same sanitized document.

The script never changes the ledger folder. It **verifies** that the ledger, state and log folders and their files give no allow ACE to:
- the Arena account;
- Everyone, Users, Authenticated Users, Interactive or Guests.

**Account rule.**
- `-ArenaAccount` lists **every account that owns an Arena process reading the file**: admin window, admin console, event service.
- Each must be a standard account, not a member of Administrators or Backup Operators, directly or through nested local groups. An administrator can take ownership of the ledger regardless of ACLs, and a UAC-filtered process does not change that, because the account can elevate.
- Membership that cannot be read is a finding (fail closed).
- The analysis is in `deploy/windows/F-1.1.0-COMPATIBILITY.md`.

Any finding exits 1. Bad input exits 2. `-PlanOnly` prints the plan and changes nothing.

Arena's `aes.config.json`:

```json
"gateway": {
  "observation_file": "C:\\ProgramData\\InferenceGateway\\observation\\shared-inference-observation.json",
  "gateway_id": "gateway-254",
  "stale_seconds": 30,
  "resource_heads": {}
}
```

## 3. Overlapping cursor, persisted per ledger

Each cycle reads events after `max(0, last_next_event_id - 100)`, with `--limit 1000`.
- The persisted cursor is the returned `next_event_id`. It is stored per `ledger_id` and never moves backwards.
- Overlap must be smaller than the limit, or the cursor could not advance; the publisher refuses otherwise.
- A burst of more than 900 new events in 5 s catches up over the following cycles.
- Arena deduplicates by event ID, so the repeated 100 events are free. A publication Arena missed while it was down still reaches it.

## 4. Resource IDs Arena can place

Name every `shared_inference` resource by its Arena head node ID, `head-192-0-2-N`, in the bootstrap policy:

```json
{"resource_id": "head-192-0-2-3", "endpoint": "<origin from operator inventory>", "resource_type": "shared_inference",
 "owner": "infrastructure", "applications": ["capstone", "coder"], "permitted_uses": ["inference"]}
```

- **Do not rename an existing resource.** Renaming loses qualification history. If a head moves to a different address, keep its `resource_id`. Moving it invalidates qualification by design. Give the Arena operator a `resource_heads` entry if the name no longer matches the node.
- **Never enroll 192.0.2.254 as a head.** Infrastructure and exclusive devices keep other resource types. Arena never counts them as heads.
- **Operator IDs.** Use IDs without spaces (`ops-1`, `ops-2`). A qualification lease appears with `app_id` `maintenance:<operator>`, and Arena drops identifiers that contain spaces.

## 5. Same application IDs as Arena

Register Capstone as `capstone` (Arena: Ticketing) and Coder as `coder`, its Arena `app_id`. `config/policy.template.json` already uses both. A future application must use its Arena `app_id` too, or Arena flags it as "not in Arena".

## 6. Reset, replacement and restore procedure

**What the ledger carries.**
- Every ledger has a stable `ledger_id`. It is created once in `capacity_service_policy` the first time this release opens the ledger, and it is preserved by backup and restore.
- The publisher publishes only from the ledger an operator has **accepted**.

**When the publisher blocks.** It stops publishing, and Arena shows the observation as stale after 30 s, when:
- the ledger has no `ledger_id` yet;
- the `ledger_id` differs from the accepted one (the ledger was replaced, or a fresh one was created);
- the ledger's newest event ID is below the persisted cursor (restored from an older backup, or reset).

**Procedure**, including first go-live:
1. The gateway operator checks the state:
   ```powershell
   inference-observation-publisher status --database <ledger> --state <state-file>
   ```
   It shows the accepted ledger, the current `ledger_id`, the cursor and the reason for any block.
2. The gateway operator **tells the Arena operator** before publishing: old and new `ledger_id`, the reason, and the time.
3. The gateway operator accepts the ledger:
   ```powershell
   inference-observation-publisher accept-ledger --database <ledger> --state <state-file> --operator <operator-id>
   ```
   This records the acceptance with the operator and the previous ledger. A restored ledger restarts from the history it actually has.
4. The Arena operator restarts the Arena console. Arena stops merging when the cursor goes backwards. After a restore, event IDs are reused for new events, so Arena's deduplication memory must be dropped.
5. Confirm that `status` shows `"blocked": null` and that the published file updates every 5 s.

## Observation document: `shared.inference.observation/1`

The contract is unchanged. These fields were added; Arena ignores unknown fields:
- `service_id`;
- `ledger_id`;
- `last_hour`;
- `inventory` (0.3.0);
- `suspensions`, `alerts`, `resources[].capacity` (0.4.0). Every earlier field is unchanged in name, type and meaning (`tests/test_failures_and_placement.py::test_observation_keeps_every_0_3_0_field_and_adds_capacity_suspensions_alerts`).

| Export field | Meaning and limitation |
|---|---|
| `observed_at`, `admission_open` | Unix UTC snapshot time and persisted admission policy |
| `service_id`, `ledger_id` | *Additive.* Logical gateway service (`shared-inference`) and the ledger's stable identity. Deduplicate by `ledger_id` + `event_id` |
| `applications` | Registered IDs, enabled authorization, concurrency, queue and priority policy. Enabled does not mean a client process is alive; no application heartbeat exists |
| `resources` | Stable resource IDs and types, qualified model names, health, last seen, freshness. Infrastructure and exclusive resources are distinguishable; never count them as heads |
| `requests` | Lifetime counts by ledger state: queued, leased, cancel_requested, completed, cancelled, expired |
| `invocations` | Lifetime counts by execution state: active, completed, failed, cancelled, unknown |
| `leases` | Active reservations: request, application, resource and lease IDs, deadline, expiry, executing, status. Not a remote CPU proof |
| `qualification_observations` | Most recent bounded evidence IDs, resource, model, digest, passed and time; failures are visible without raw evidence |
| `events`, `next_event_id` | Bounded ordered event IDs with safe metadata only |
| `last_hour` | *Additive.* `window_seconds: 3600`. Counts for the last hour: `requests_created` by current status, `invocations_updated` by current state, and `events` by type. *Additive, 0.4.5 (unreleased):* `invocation_failures[]`, the hour's failed invocations grouped by `resource_id` (null: never reached a head), `model`, `app_id` and `class` (recovery failure class, null when unclassified), each with `count` and `last_at`, largest first, at most 50 rows; and `invocation_failures_omitted`, the failures beyond that cap. The error string is never exported (`docs/RELEASE-NOTES-0.4.5.md`) |
| `inventory` | *Additive, 0.3.0.* Per resource: `resource_id`, `observed_at`, `fresh`, `runtime_version`, and `models[]` with `{name, digest}` exactly as the head reports them. Endpoints are never exported. Installed is not qualified: compare with `resources[].qualified_models` |
| `resources[].capacity` | *Additive, 0.4.0.* `slots`, `slots_in_use` (active leases, including maintenance and probe leases), `memory_gb` (policy; null = unknown), `loaded_models[]` (`name`, `digest`, `size_bytes` from `/api/ps`; null = unknown), `loaded_observed_at`, `loaded_fresh` (read within 30 s) |
| `suspensions` | *Additive, 0.4.0.* Active suspensions and holds. Fields: `suspension_id`, `scope` (`resource_model`, `model`, `resource`), `resource_id` (null for `model`), `model`, `digest`, `operation` (`completion` or `embedding`), `class` (`model_on_head` or `uncertain_execution`), `source`, `since`, `hold_until`, `next_probe`, `probes_failed`, `state` (`suspended`, `held`, `needs_attention`), `waiting`, `heads`. A model suspended on a head still appears in that head's `qualified_models`: qualification is not withdrawn |
| `alerts` | *Additive, 0.4.0.* Items that need a person, for the admin hub. Fields: `alert_id`, `suspension_id`, `kind` (`needs_attention` or `model.suspended`), `scope`, `resource_id`, `model`, `operation`, `since`, `reason_code` (`probes_exhausted`, `not_safe_to_probe`, `model_suspended_everywhere`) and a short `reason`. It disappears when the item recovers or is released |

**Event fields.** Each event has only `event_id`, `app_id`, optional `request_id`, `event_type` and `at`. Payloads are never exported.

**Event types.**
- Existing:
  - `resource.authorized`, `queued`, `admitted`, `completed`;
  - `cancelled`, `cancel_requested`, `authorization.withdrawn`, `overload.rejected`;
  - `qualification.completed`, `service.restart_reconciled`;
  - `service.admission_drained`, `service.admission_resumed`.
- New:
  - `lease.expired`: a lease ran out;
  - `resource.quarantined`: uncertain execution; since 0.4.0 only that (a timeout, reset or truncation after the call was sent, or an executing lease expiring), once per hold, always with `resource.held`. An HTTP error reply no longer quarantines;
  - `request.expired`: a queued request passed its deadline;
  - `authorization.revoked`: a client was revoked. Each queued request it had gets a `cancelled` event;
  - `invocation.failed`;
  - `invocation.unknown`: execution interrupted by a restart.
- New in 0.3.0:
  - `inventory.observed`, `inventory.model_added`, `inventory.model_removed`, `inventory.digest_changed`, `inventory.runtime_changed`: fleet changes reported by heads;
  - `resource.health_changed`, `qualification.invalidated`: a qualified model withdrawn after a digest or runtime change;
  - `resource.reconciled`;
  - `authorization.rotated`;
  - `service.authority_bound`, `service.authority_adopted`, `service.ledger_restored`.
- New in 0.4.0 (see `docs/FAILURES-AND-PLACEMENT.md`):
  - `model.suspended`, `model.recovered`: a model stopped on one head or, by the cascade rule, on every head; and returned after a passed probe;
  - `resource.held`, `resource.recovered`: a head held after uncertain execution; and returned after its probes passed;
  - `probe.passed`, `probe.failed`, `probe.requested` (an operator's probe now);
  - `resource.needs_attention`: probes are exhausted, or a held head never became safe to probe. It also appears in `alerts`;
  - `suspension.released` (operator release or reconcile), `request.replaced` (the same request re-placed on another head after a server error or an unreachable head), `service.schema_upgraded`.

**Health values are unchanged.** `quarantined` now means *held*: uncertain execution only. The Gateway releases it by itself after a passed probe, or an operator does.

Events now cover expiry, revocation and failure. Still reconcile with the snapshot counts: lazy state corrections during status reads are not evented. The observer does not expire leases, so a snapshot may show elapsed leases until the service reconciles them.

**Utilization.** Queue depth comes from queued requests; active head allocations come from leases. Allocation utilization is leased eligible inference heads divided by healthy, fresh, qualified heads. With zero eligible heads it is unavailable, not zero percent. This is allocation, not measured CPU or GPU load.

**Not provided:**
- RAM, VRAM, CPU or GPU measurements;
- latency percentiles;
- application heartbeat.

Per-client token totals exist only in each client's authenticated `/v1/usage`.

**Never published:**
- token hashes or tokens;
- client idempotency keys, prompts, responses, customer identifiers;
- error strings, raw qualification JSON;
- upstream endpoints, owners, certificate paths.

The exporter uses explicit column allowlists. The tests check private-value exclusion and that the ledger stays byte-identical.

## 0.4.6 additions to the observation (additive; `shared.inference.observation/1` unchanged)

| Field | Meaning |
|---|---|
| `last_hour.upstream_http_errors` | HTTP error replies from heads in the window, grouped by `resource_id`, `model`, `operation`, `status`, with `count` and `last_at`; at most 50 rows. Bodies and messages are never exported. |
| `gateway_load.routes` | per HTTP route since the service started: `count`, `ms_total`, `ms_max`, `errors_5xx`, `recent_p50_ms`, `recent_p95_ms` (last 512 calls) |
| `gateway_load.health_cycle` | the last health cycle: `total_ms`, `ledger_wait_ms`, `heads_ms`, `head_failures` (heads that did not answer) and `local_failures` (Gateway bookkeeping stages that failed; such a head keeps its health) |
| `gateway_load.ledger.placement` | placement passes, passes that needed no write, grants |

A head that goes offline because it did not answer now has `stage` and `cause` in its `resource.health_changed` event payload (ledger only). Older Arena releases ignore all of these.

## HTTP contracts and what is not built

The gateway's `GET /health` and `GET /ready` are public status only. `/v1/models`, `/v1/usage` and `/v1/requests/{id}` are application-scoped `shared.inference/1` routes. They are **not** an Arena feed. Never give Arena an application token.

An observer-only authenticated HTTPS endpoint serving the same document was requested for later and is **not built**. Arena does not assume one.

Capstone and Ticketing request correlation (recording the gateway `request_id` in Capstone telemetry) is Capstone's decision. It needs no gateway change.
