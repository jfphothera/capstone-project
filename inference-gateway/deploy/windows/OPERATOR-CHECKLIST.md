# Operator checklist: Inference Gateway 0.4.6 on 192.0.2.254

Use an elevated PowerShell 7 session on the hub, signed in with the **maintenance administrator account**, not Arena's account.

Shorthand used below:
- `PY` = `"C:\Program Files\Python312\python.exe"`
- `PKG` = the unpacked release folder
- `S` = your `deployment-settings.json`, made from `config/deployment-settings.template.json`
- `L` = `PY -I "C:\Program Files\InferenceGateway\releases\0.4.6\inference_gateway\service_launcher.py"`
- `D` = `C:\ProgramData\InferenceGateway`

Every command prints one JSON object. Continue only when it shows `"ok": true`.

**Before you start** (F: or by hand):
- [ ] Release zip hash matches `inference-gateway-0.4.4.zip.sha256`.
- [ ] Machine-wide Python 3.12.10 and PowerShell 7.4+ installed.
- [ ] Local account `svc-inference-gw` exists: standard user, "Log on as a service", interactive and RDP logon denied.
- [ ] Arena runs as a standard account. Write its name into `arena_accounts` in `S`.
- [ ] Head firewalls admit 192.0.2.254; .201 and .202 are refused.
- [ ] F: verified head inventory exported.

**Install:**
1. [ ] `PY -I PKG\bin\gateway-deploy.py plan --settings S`. Only `tls` and `ledger` may be missing.
2. [ ] `… certificate --settings S`. Then move `D\ca\ca.key` to offline media.
3. [ ] `… install --settings S --password-stdin --new-ledger`. Type the `svc-inference-gw` password.
   - For a migration, use `--restore-backup <verified backup>` instead, then `L lifecycle adopt …`.
4. [ ] `L heads --inventory <F: export> --policy <approved policy> --applications capstone,coder --output D\config\policy-review.json`. Review the file.
5. [ ] `L bootstrap --database D\state\capacity-ledger.db --policy D\config\policy-review.json`
6. [ ] For each application: `L credentials issue --database … --policy … --app-id capstone --output <file> --reader "NT AUTHORITY\LOCAL SERVICE"`.
   - Carry the file to the application host; delete the hub copy.
7. [ ] For each head and model: `L qualify --database … --resource head-192-0-2-N --model <m> --operator <you> --max-context <n> --output D\backups\qual-<id>.json`.
   - Add `--purpose embedding`, `tools`, `structured_output`, `vision` or `context` as needed.
8. [ ] Tell the Arena operator the `ledger_id` (`L lifecycle status --database …`), then run `L publisher accept-ledger --database … --state D\observation-state\publisher-state.json --operator <you>`.
9. [ ] `pwsh -File PKG\scripts\Set-ObservationPublicationAcl.ps1 -PublishDirectory D\observation -StateDirectory D\observation-state -LogDirectory D\logs -LedgerDirectory D\state -GatewayAccount .\svc-inference-gw -ArenaAccount <arena account(s)>`
10. [ ] `… verify --settings S`. This needs `ok: true`.
11. [ ] F: opens inbound TCP 8443 from 192.0.2.201 and 192.0.2.202 only.
12. [ ] On 192.0.2.201: `GET https://192.0.2.254:8443/ready` with the CA returns 200; `models()` lists the qualified models.

**Update 0.4.5 → 0.4.6 (ledger, policy, `model_capacity`, `unmeasured_model_limit` and credentials kept):**
1. [ ] Release zip hash matches `inference-gateway-0.4.6.zip.sha256`. No experiment is running (the update restarts the service once).
2. [ ] From the 0.4.6 package: `PY -I PKG\bin\gateway-deploy.py update --settings S`. Expect `"ok": true`, `ledger.kept: true`, `held_at_upgrade: []`. The index `capacity_qualification_evidence_resource` and the table `capacity_upstream_errors` are created when the service starts.
3. [ ] Do **not** re-run bootstrap or `credentials` for this update: no configuration key is new or changed. `L lifecycle capacity --database …` must show the same limits and sources as before the update.
4. [ ] `GET https://192.0.2.254:8443/health` shows `"release": "0.4.6"`.
5. [ ] Optional, per head and model whose roles send chat with a JSON schema: `L qualify … --purpose structured_chat --model <m> --operator <you> --output …`.
6. [ ] Watch: `health.local_failure` (the Gateway's own bookkeeping failed; the head keeps its health), `health.head_failed` (the head did not answer, with its stage), `upstream.http_error` (a head refused a call; read it with `L lifecycle upstream-errors --database … [--request <REQ-id>]`).
7. [ ] Capstone ships client 0.4.2 (0.4.1 keeps working).
8. [ ] Lab tools: `lab-tools\README.md` (launch storm before and after, then G4, then G6).

**Update 0.4.3 → 0.4.4 (ledger kept):**
1. [ ] Release zip hash matches `inference-gateway-0.4.4.zip.sha256`.
2. [ ] From the 0.4.4 package: `PY -I PKG\bin\gateway-deploy.py update --settings S`. Expect `"ok": true`, `ledger.kept: true`, `held_at_upgrade: []`. Three indexes and the table `capacity_model_capacity` are created when the service starts. The log line `ledger.opened` gives row counts and the WAL size.
3. [ ] No requalification for the update itself. New policy keys take their defaults (`parallel_runtime_min: "0.40.15"`, `serial_families_need_evidence: true`).
4. [ ] Capstone keeps client 0.4.1.
5. [ ] Watch the service log and `gateway_load.ledger` in the observation:
   - `ledger.slow_transaction` and `ledger.wait_timeout` name any long write and its holder;
   - `http.client_gone` with `likely: client_attempt_timeout` means a handler outlasted the client's 10 s attempt;
   - `health.slow_cycle` says whether the heads or the ledger took the time;
   - `request.unclaimed` lists results no caller collected.
6. [ ] **After a head's Ollama upgrade (Arena):**
   1. Requalify each model on that head.
   2. Then either apply Arena's `model_capacity` with `bootstrap`, or measure: `L qualify … --model <m> --operator <you> --max-context <n> --parallel 4 --output …`.
   3. Check `L lifecycle capacity --database …`: `source` must read `declared` or `measured` before a qwen3.5 model runs more than one call per head.
   4. Remove `placement_policy.model_parallel` entries that should no longer override the evidence.
**Update 0.4.2 → 0.4.3 (ledger kept):**
1. [ ] Release zip hash matches `inference-gateway-0.4.3.zip.sha256`.
2. [ ] From the 0.4.3 package: `PY -I PKG\bin\gateway-deploy.py update --settings S`. Expect `"ok": true`, `ledger.kept: true`, `held_at_upgrade: []`. The column `capacity_inventory_runtime.families` is added when the service starts.
3. [ ] No requalification. The new policy keys take their defaults (`strategy: least_busy`, `serial_families: [qwen35, qwen35moe]`, `model_parallel: {}`, `deadline_liveness_seconds: 3`). Set them in the bootstrap policy only to change them.
4. [ ] Capstone keeps client 0.4.1.
**Update 0.4.1 → 0.4.2 (ledger kept):**
1. [ ] Release zip hash matches `inference-gateway-0.4.2.zip.sha256`.
2. [ ] From the 0.4.2 package: `PY -I PKG\bin\gateway-deploy.py update --settings S`. Expect `"ok": true`, `ledger.kept: true`, `held_at_upgrade: []`. No schema change.
3. [ ] Re-run the completion qualification of the reasoning models that failed under 0.4.1 (deepseek-r1:8b, deepseek-r1:14b, others): `L qualify … --model <m> --operator <you>`. A pass now needs output, not a finished answer; read `measurement.finished_reasoning` and `answer_matches_ready` if you want to know more. On a slow head add `--timeout` (up to 300 s).
4. [ ] Capstone keeps client 0.4.1.
**Update 0.4.0 → 0.4.1 (ledger kept):**
1. [ ] Release zip hash matches `inference-gateway-0.4.1.zip.sha256`.
2. [ ] From the 0.4.1 package: `PY -I PKG\bin\gateway-deploy.py update --settings S`. Expect `"ok": true`, `ledger.kept: true`, `held_at_upgrade: []` (0.4.0 → 0.4.1 changes no head state).
   - If starting 0.4.1 fails (including an SCM error such as 1053), the installer now switches back to 0.4.0 and records `update_failed_rolled_back` with the error. If even that fails it says so (`update_failed_rollback_failed`): run `rollback` or `repair`.
   - The installer resets the new release folder's ACL to inherit (`icacls /reset /T /C`); Arena's manual workaround is no longer needed for 0.4.1 and later.
3. [ ] The ledger switches to SQLite WAL journaling at the first 0.4.1 start: `capacity-ledger.db-wal` and `-shm` appear next to it in `D\state` (same ACL). Back up with `L lifecycle backup` (never a plain file copy of the `.db` alone).
4. [ ] Limits: unless `D\config\gateway-service.json` sets `limits`, 0.4.1 uses `max_connections` 256 and `max_pending` 512 (0.4.0: 64 and 128). If the file sets them, raise them to these values.
5. [ ] Requalify the head/model pairs that 0.4.0 removed (the 49 pairs of 2026-11-12): completion for completion models, `--purpose embedding` for the embedding models. 0.4.1 does not restore them on its own: a model that left a head's list needs a passing test.
6. [ ] Capstone ships `shared_inference_client.py` 0.4.1 (`PKG\client\`, hash in `SHA256SUMS`).
7. [ ] Watch `gateway_load` in the observation (`connections_503`, `pending_budget_503`, `lease_renewal_failures`, `client_disconnected`).
**Day-to-day:**
- **Status:** `L status --config D\config\gateway-service.json --observation-file D\observation\shared-inference-observation.json --publisher-state D\observation-state\publisher-state.json`
- **Maintenance drain:** `L lifecycle drain --database … --operator <you> --wait-seconds 120`. Resume with `lifecycle resume`.
- **Suspensions and held heads (0.4.0):** the service probes them by itself.
  - Watch: `L lifecycle suspensions --database …` or the observation `alerts`.
  - Probe now: `L lifecycle probe --database … --suspension <SUSP-id> --operator <you>`.
- **Per-model limits (0.4.4):** `L lifecycle capacity --database …` lists each head's models with `limit` and `source` (`declared`, `measured`, `serial_family`, `slots`, `policy`). A `stale_because` entry means the runtime or digest changed: requalify, then re-declare or re-measure.
  - Manual override: `L lifecycle release --database … --suspension <SUSP-id> --operator <you> --confirmed`.
- **Quarantined (held) head, by hand:**
  1. Verify the head is idle.
  2. `L lifecycle reconcile-resource --database … --resource head-192-0-2-N --operator <you> --confirmed-idle`
  3. Requalify it.
- **Rotate a token:** `L credentials rotate --app-id <app> --output <file>`. Revoke with `credentials revoke`.
- **Renew the certificate:** bring `ca.key` back, run `certificate --renew`, restart the service, then move the key offline again.
- **Update:** from the new package, `update --settings S`. If the new release does not become healthy, the old one is restored automatically.
  - From 0.3.0: check `ledger.kept` is `true` and read `held_at_upgrade`: those heads are probed from 60 s later.
  - Before the update, put each head's `slots` and `memory_gb` in the policy and apply it with `bootstrap` (example: `config/policy.lab-example.json`).
- **Roll back:** `rollback --settings S`.
- **Repair:** `repair --settings S`.
- **Uninstall:** `uninstall --settings S`. Data stays; ask F: to remove the firewall rule.
