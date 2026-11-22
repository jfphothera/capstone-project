# Physical acceptance plan: Gateway 0.4.0 on the lab cluster

Software tests use synthetic ledgers, a fake SCM and simulated or loopback inference. **They prove none of the items below.**
- Run each step on the real machines, under a separately authorized change.
- Record measured evidence: command JSON, timestamps, logs, packet or connection results.
- A step passes only with real evidence. Mocks, fixtures and synthetic ledger events never count.

| # | Demonstrate | Procedure | Evidence and pass criterion |
|---|---|---|---|
| 1 | Gateway starts on .254 before login | Reboot the hub; do not sign in. From .201, poll `/ready` with the CA | 200 within 5 min of boot, before any interactive logon (Security log 4624 type 2/10 absent); `gateway-service.log` `service.listening` |
| 2 | Arena starts independently and reads the observation | Sign in as the Arena standard account; open the console | The Gateway line is live; observation age is under 30 s; stopping Arena does not change `/ready`; stopping the Gateway makes Arena show stale within 30 s |
| 3 | Gateway reaches approved heads | `L status` and `verify` | Every enrolled head shows `healthy` and `fresh`; `inventory.observed` events present |
| 4 | Accurate inventory discovery | Compare `/api/tags` on each head (from .254) with the observation `inventory` | Names and digests are identical for every head; no assumed profile models |
| 5 | Models qualify for intended operations | `qualify` per head, model and purpose | Evidence JSON `passed: true` with `actual_execution: true`; failures recorded, not retried silently |
| 6 | Capstone uses several eligible models | Capstone Phase 6 runs arms on 2+ models, pinning `expected_digest` | Each result's `provenance.executed_digest` equals the pin; `resource_id` matches a head that holds it |
| 7 | Coder submits at the same time | Coder and Capstone load together for 10 min | No head ever holds more leases than its `slots` (observation `leases` and `resources[].capacity.slots_in_use`, sampled every 5 s); both complete work |
| 8 | Queue, capacity and priority enforced | Flood from `capstone-research`; measure `capstone` latency. Run once with `slots` 1 and once with the policy's per-head `slots` | Research holds at most `max_concurrency` leases, and so at most that many heads, whatever the heads' `slots`; operational requests keep the remaining heads and slots; queue limit returns 400 |
| 9 | Arena relationships correct | Compare Arena's view with application telemetry by `request_id` | Application, request and executing-head relationships match the leases |
| 10 | Unavailable or revoked model not selected | Remove a model from a head; revoke a test client | `availability` shows `not_installed` or `not_permitted`; no dispatch to that head or model; revoked token returns 403 |
| 11 | Model change requires requalification | `ollama pull` a new digest on one head (Arena's workflow) | `inventory.digest_changed`, `qualification.invalidated`; `digest_changed` in availability; usable again only after `qualify` |
| 12 | Failed heads and Gateway restarts recover correctly | Unplug a head mid-request; restart the service mid-request; then reconnect the head | Request `failed` or `unknown`, never `completed`; no replay (head-side request count). The head is **held** (`suspensions[]` state `held`, health `quarantined`), never probed before the request's deadline, and not before two `/api/ps` reads show no loaded model. Then the probe returns it (`resource.recovered`) with no operator step; if it never becomes safe or its probes fail three times, `needs_attention` and an `alerts[]` entry. `reconcile-resource --confirmed-idle` and `release --confirmed` still work by hand |
| 13 | Raw Ollama blocked from application machines | From .201 and .202: `Test-NetConnection 192.0.2.N -Port 11434` per head | Refused or timed out on every head; success from .254 |
| 14 | Histories, credentials and state isolated | As the Arena account: open the ledger, state and logs. As Capstone: request Coder's request IDs | Access denied; HTTP 403 |
| 15 | Update and rollback preserve state | `update` 0.3.0 → 0.4.0, then `rollback` | `ledger_id`, request history and qualifications unchanged; backups verified; `verify` ok after each step |
| 16 | Least privilege | `whoami /groups` and rights for `svc-inference-gw`; ACL script JSON | Not in Administrators or Backup Operators; only service logon; ACL script `ok: true` |
| 17 | GPU workstation .2 | Separate inspection: its earlier setup differed | Ollama bind address, firewall and version match the head standard before enrollment |
| 18 | TLS from clients | From .201: SDK with the CA; then without it; then with a wrong-SAN certificate | Only the first succeeds |
| 19 | Work spreads across healthy heads (0.4.0) | Capstone and research load for 10 min with the per-head `slots` and `memory_gb` in the policy | `admitted` events name several heads; a model already loaded is reused (`placement: loaded`) while its head has a free slot; an idle head where the model fits is used before any request queues behind a full head; no head exceeds `slots`; no `/api/ps` shows a model unloaded while a lease used it |
| 20 | Failure classes on real heads (0.4.0) | On one head, under a separately authorized change: run `tests/test_live_ollama.py` against it from .254 | Ollama 0.40.13's replies match the table in `docs/FAILURES-AND-PLACEMENT.md` §1 (4xx request fault, 5xx model-on-head, refused unreachable, timeout after send uncertain); a busy runner stays listed in `/api/ps` past its `expires_at` |
| 21 | One model failing does not take heads down (0.4.0) | A model that fails with 5xx on two heads (a deliberately broken model build, never production work) | Only that model is suspended per head, then everywhere (`model.suspended`, alert); both heads keep serving other models; the same request completes once elsewhere after the first failure |
| 22 | Upgrade keeps the ledger and recovers held heads (0.4.0) | `update` 0.3.0 → 0.4.0 on the hub with heads still quarantined; later `rollback` and `update` again | `ledger.kept: true`; `held_at_upgrade` lists every quarantined head; each is probed after its wait and returns or reaches `needs_attention`; `rollback` succeeds and 0.3.0 serves on the same ledger |

**Operator checklist:** `deploy/windows/OPERATOR-CHECKLIST.md`.

**Acceptance owner:** the lab operator. Gateway acceptance is complete only when all 22 rows have evidence.
