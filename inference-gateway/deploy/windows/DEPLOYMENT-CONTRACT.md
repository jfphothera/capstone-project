# Inference Gateway 0.4.0: Windows deployment contract for 192.0.2.254

This is the Gateway-owned answer to Agent Arena's `PROMPT-GATEWAY-DEPLOYMENT-CONTRACT.md` and `GATEWAY-INFRASTRUCTURE-MATRIX.md` (F: lab provisioner 1.1.0, `agent-arena-lab-1.1.0+823e182`).

**Machine-readable companion:** `deploy/windows/deployment-manifest.json`, schema `inference-gateway.windows-deployment/1`, which carries every file's SHA-256.

**Ownership.**
- **The Gateway owns:** its installer, service, certificates, authentication, qualified inventory and scheduling.
- **F: owns:** the machine prerequisites. It invokes the Gateway's commands and reads their JSON.
- No Gateway logic is copied into Arena.

**Status.** Software implemented and tested (`verification/test-report.json`). Nothing has been installed on 192.0.2.254. No account, certificate, token, firewall rule or service exists on a real machine.

## 1. Listener

| Item | Decision |
|---|---|
| Bind | `192.0.2.254` only (not `0.0.0.0`, not loopback) |
| Port | **TCP 8443**. Arena uses 8790 and 8791 on the hub |
| Protocol | HTTPS only, TLS 1.2 minimum. Handshakes run per connection under a 10 s timeout, so silent or plaintext clients cannot block the listener (tested) |
| Allowed clients | `192.0.2.201` (Capstone), `192.0.2.202` (Coder) |
| Cutover | `192.0.2.1` **only** while an approved Capstone migration is open. It is a separate, explicitly enabled entry, never in the default rule. Open item O2 |
| Firewall rule (F: creates) | Inbound TCP 8443, local address 192.0.2.254, remote addresses = allowed clients, Private profile, named `InferenceGateway-HTTPS-8443`. **Never "Any".** The hub's own checks use the LAN address and are not filtered |

## 2. TLS

| Item | Decision |
|---|---|
| Issuer | The Gateway's own lab CA: ECDSA P-384, 5 years, made by `scripts/New-GatewayCertificate.ps1` through the installer's `certificate` verb. Pure .NET: no certificate store entry and no public CA |
| Server certificate | ECDSA P-256, SAN `IP:192.0.2.254`, EKU serverAuth, at most 397 days and never beyond the CA. `notBefore` is one hour back, to tolerate clock skew |
| Files | `C:\ProgramData\InferenceGateway\tls\server.pem`, `server.key`, `inference-gateway-ca.pem` |
| Key ACL | SYSTEM and Administrators full; `svc-inference-gw` read on `server.pem` and `server.key`; Arena and Users none. ACLs are set **before** the key is written |
| CA key | `C:\ProgramData\InferenceGateway\ca\ca.key`, SYSTEM and Administrators only. Move it to offline media after issuance; bring it back only to renew |
| Renewal | `certificate --renew` (same CA), then restart the service. Clients need no change. Track `not_after` in the verb's JSON |
| Client trust | `inference-gateway-ca.pem` is public and may travel on F:. Clients name it in `ca_file`. The SDK verifies chain and IP SAN; untrusted or mismatched certificates are refused (tested) |
| Time | The hub serves lab time. Clients and heads must use it. The certificate tolerates one hour of skew |

## 3. Service identity (question 2)

| Item | Decision |
|---|---|
| Account | `.\svc-inference-gw`: a **local standard user**, member of Users only, never of Administrators or Backup Operators. Workgroup, so no gMSA. Not an `NT SERVICE` virtual account |
| Rights | `SeServiceLogonRight` ("Log on as a service"). Recommended denials: interactive and Remote Desktop logon |
| Created by | F: (machine prerequisite) |
| Password | Typed by the operator when F: creates the account, and again at `install`, through `--password-stdin` or a prompt. It goes to the SCM in memory (`CreateServiceW`). It is never stored, logged, written to the manifest or placed on a command line |
| Verified by | `plan` and `install` refuse a missing or privileged account. `verify` re-checks it |

## 4. Supervisor

- **Host.** A **native Windows Service Control Manager host** (`inference_gateway.windows_service`, `ctypes`, no wrapper):
  - it calls `StartServiceCtrlDispatcherW`;
  - it reports START_PENDING, RUNNING, STOP_PENDING with advancing checkpoints, and STOPPED;
  - it accepts STOP and PRESHUTDOWN.
- **Service.** Name `InferenceGateway`, type own-process, start **delayed automatic**, which starts before any login. Depends on `Tcpip`.
- **Command.** `"<python>" -I "C:\Program Files\InferenceGateway\releases\<release>\inference_gateway\service_launcher.py" service --config "C:\ProgramData\InferenceGateway\config\gateway-service.json" --name InferenceGateway`.
- **Pre-stop drain.**
  - New submissions get 503.
  - Admission closes persistently, marked as a *service* drain.
  - Active allocations get up to 90 s to finish.
  - Then the listener closes and ledger ownership is released.
  - The service reopens admission at its next start **only** if it closed it itself. An operator's maintenance drain survives restarts (tested).
- **PRESHUTDOWN.** Timeout 180 s.
- **Recovery.** Restart after 30 s, then 60 s, then every 300 s; the counter resets after one day. Recovery also applies to an error exit: a start failure exits 1, a runtime failure exits 2.
- **Crash.** The kernel releases the ledger ownership lock. At restart, interrupted work becomes `unknown` and is never replayed. Leases that may still be executing are kept until expiry and then hold the head. A held head is probed after its deadline and returned automatically (0.4.0, `docs/FAILURES-AND-PLACEMENT.md`; tested).
- **Single authority.** One service per ledger file (kernel lock). The ledger records host, path and service ID. A copied or moved ledger, or a renamed service, refuses to start until an operator runs `lifecycle adopt` after confirming the old authority is stopped (tested).
- **Logs.** `C:\ProgramData\InferenceGateway\logs\gateway-service.log`, JSON lines, rotating 10 MiB × 10, plus `gateway-fault.log`.
  - Logged: lifecycle, drain, recovery, authority, capacity event types (including qualification and administration) and failures with tracebacks.
  - Never logged: prompts, outputs, tokens or keys (tested).
- **Observation publisher.** It runs **inside the service**: 5 s cadence, atomic replace, overlapping cursor, ledger acceptance. Its freshness therefore also tells Arena that the service runs. Its failures are logged and never stop inference. The Task Scheduler definition (`Register-ObservationPublisherTask.ps1`) remains as an alternative and is not used by default.

**Lifecycle commands.** Every command prints one JSON object and exits **0** on success, **1** on a failed check or refused step, **2** on bad input.

```text
"<python>" -I "<package>\bin\gateway-deploy.py" <verb> --settings "<deployment-settings.json>"
  plan                       checks and planned steps; changes nothing
  certificate [--renew]      lab CA (first time) and server certificate
  install --password-stdin (--new-ledger | --restore-backup <file>) [--no-start]
  verify                     service, config, readiness, publisher, heads, /health and /ready over TLS
  update                     run from the NEW package: backup, new side-by-side release, switch; on failure it switches back automatically
  rollback                   backup, then return to the previous release
  repair [--password-stdin]  re-apply ACLs, reinstall the current release files, re-register; data untouched
  uninstall [--remove-releases]   drain-stop and unregister; data is never deleted
```

## 5. Folders

The installer sets every folder ACL with inheritance removed. Every verb preserves all data.

| Folder | ACL | Content |
|---|---|---|
| `C:\Program Files\InferenceGateway` | SYSTEM F, Administrators F, Users RX | `releases\<release>\` (side by side) |
| `C:\ProgramData\InferenceGateway` | SYSTEM F, Administrators F | data root |
| `…\config` | + svc RX | `gateway-service.json` (paths and policy, no secrets) |
| `…\state` | + svc M | `capacity-ledger.db` |
| `…\tls` | + svc RX | server certificate and key, client CA |
| `…\ca` | SYSTEM, Administrators only | lab CA key (move offline) |
| `…\observation` | + svc M; **Arena accounts `(OI)(IO)R`**, applied by `Set-ObservationPublicationAcl.ps1` | `shared-inference-observation.json` |
| `…\observation-state` | + svc M | publisher cursor and acceptances |
| `…\logs` | + svc M | service logs |
| `…\backups` | SYSTEM, Administrators only | verified ledger backups (`.db` + `.json` hash record) |
| `…\deployment` | SYSTEM, Administrators only | `deployment-state.json` (current and previous release, history) |

## 6. Runtime

- **Python.** CPython **3.12.10**, installed **machine-wide** by F: from the official installer, with its hash verified: `InstallAllUsers=1 TargetDir="C:\Program Files\Python312" PrependPath=0 Include_launcher=0`.
- **No virtual environment.** A venv `python.exe` is a redirector whose child process cannot connect to the SCM. The base interpreter runs `-I` against the isolated release folder.
- **No interpreter in the release.** The release carries no interpreter, and runtime needs no third-party package.
- **PowerShell 7.4+ machine-wide.** Needed for the certificate script; the ACL script also runs under it.

## 7. Ordered install procedure

1. **F: prerequisites** (§12).
2. **`plan`.** All checks are `ok`, except `tls` and `ledger` on a first install.
3. **`certificate`** (first install only). Move the CA key offline afterwards.
4. **`install --password-stdin --new-ledger`**, or `--restore-backup <verified backup>` for a migration. Never bootstrap a fresh ledger over an existing authority.
5. **Operator supplies the policy** (§8, §9), on the hub, with the launcher:
   - `heads --inventory <F: export> --policy <approved policy> --applications capstone,coder --output <review.json>`;
   - review the output;
   - `bootstrap --database <ledger> --policy <review.json>`;
   - `credentials issue` per application. Token files go to the application hosts out of band;
   - `qualify` each head and model, for completion and/or embedding, plus capability purposes where needed.
6. **Tell the Arena operator** the `ledger_id`, then `publisher accept-ledger --operator <id>`. Until then the service runs and serves inference, but publishes nothing, and Arena shows "stale".
7. **Publication ACL.** `Set-ObservationPublicationAcl.ps1 … -ArenaAccount <every Arena account>`; it must return `ok: true`.
8. **`verify`.** It must return `ok: true`.
9. **F: opens the inbound firewall rule** from `listener.firewall_rule`. The Capstone and Coder hosts install the CA and token file and check `/health`.

## 8. Head inventory input (question 5)

The input is F:'s **`agent-arena.verified-head-inventory/1`**, accepted as is (it is no longer provisional).

`service_launcher.py heads` takes `heads[]` entries in which:
- `resource_id == arena_node_id == head-192-0-2-N`;
- `address == 192.0.2.N`, with N in 2–199 (never .254);
- `endpoint == http://192.0.2.N:11434`;
- `verified.ollama_version` is present.

Other entries are refused and reported, and the `excluded` entries are listed. It writes a **reviewable** policy with `shared_inference` resources for the named applications. Nothing is enrolled until `bootstrap`, and nothing is usable until qualification. The real installed inventory and qualification are authoritative. Profiles chosen at install time (CPU8, GPU, …) are never assumed.

## 9. Tokens (question 4)

- **Issue.** `credentials issue` generates 256-bit tokens on the hub. Each is written once to a new file that is ACL-restricted before the token is written, and never printed. The ledger stores SHA-256 only.
- **Rotate or revoke.** `rotate` replaces the token at once. `revoke` disables the application and cancels its queued work. Applying policy never re-enables a revoked application.
- **The one answer for Capstone.** Capstone reads its client config `C:\ProgramData\Capstone\gateway\gateway-client.json`, with `token_file: C:\ProgramData\Capstone\gateway\capstone.token` and `ca_file: …\inference-gateway-ca.pem`.
  - ACL: SYSTEM and Administrators full, `NT AUTHORITY\LOCAL SERVICE` read.
  - None of `CAPSTONE_INFERENCE_TOKEN`, `CAPSTONE_INFRASTRUCTURE_TOKEN` or `APPLICATION_INFERENCE_TOKEN` is used.
  - The SDK's `token_file` overrides any environment variable.
  - Capstone's `Start-Capstone.ps1` and `Configure-Capstone.ps1` adopting this is a **Capstone-side change** (open item O4).
- **Coder.** The same, with its own files.

## 10. Head transport (question 6)

**Decision for 0.3.0:** keep **plain HTTP to `http://192.0.2.N:11434`, reachable only from 192.0.2.254** (plus .1 during an approved cutover), enforced by each head's firewall. F: 1.1.0 implements that rule; it has not been run on any head.

**What it protects against:**
- application hosts (.201, .202) and other lab devices calling raw Ollama;
- any client bypassing Gateway admission.

**What it does not protect against:**
- *Confidentiality.* Prompts and outputs cross the lab LAN in cleartext. Anyone able to capture traffic on that segment (a mirrored switch port, ARP spoofing) can read them.
- *Head authenticity.* A device that impersonates a head's IP on the same L2 segment could answer with fabricated results. Digest and version checks are reported by the head itself.
- *Source authentication.* The firewall trusts the source IP, which is not cryptographic.

**Why this is acceptable for now.** The lab LAN is an isolated, physically controlled segment with no Wi-Fi bridge and no internet. The Gateway already refuses public, DNS-named and link-local upstreams.

**Condition (open item O1).** This holds **only if** the lab LAN is approved for the data Capstone and Coder will send. If restricted customer or regulated data will cross it, an mTLS head proxy is required before that data is used.

**Migration cost if adopted later.** Gateway code is ready: `https` endpoints, plus `upstream_tls.ca`, `.certificate` and `.key`. Required work:
- a TLS proxy on every head (Ollama rebound to `127.0.0.1`; proxy on a new port);
- a per-head server certificate and a Gateway client certificate;
- head firewall changes (close 11434, allow the proxy port from .254);
- F: head-engine changes;
- re-authorizing each resource's endpoint, which invalidates its qualification by design, then requalifying every head and model;
- certificate renewal for every head.

## 11. Health checks and observation

- **Health checks.** `GET https://192.0.2.254:8443/health` and `/ready`, without credentials, with `ca_file`, from .254, .201 and .202.
  - `/ready` returns 200 only when the ledger is usable and admission is open; 503 when drained or stopping.
  - Neither says anything about heads. For that, `verify` and `status` report head health and available models.
- **Observation.** `C:\ProgramData\InferenceGateway\observation\shared-inference-observation.json`, contract `shared.inference.observation/1`.
  - Written every 5 s by `svc-inference-gw` (in-service). Stale after 30 s.
  - Arena `gateway_id`: `gateway-254`. The document carries `service_id` and `ledger_id`.
  - Additive in 0.3.0: an `inventory` array (installed models per head, no endpoints), and events for inventory changes, health changes and qualification invalidation.

## 12. Machine prerequisites F: provides (question 7)

1. Hub on 192.0.2.254/24, Private profile, never sleeps, lab time source. *F: 1.1.0 has this.*
2. **Local account `svc-inference-gw`.** Standard user, `SeServiceLogonRight`, interactive and RDP logon denied, password operator-typed and handed to `install` on stdin.
3. **Every Arena process runs as a standard account** that is not a transitive member of Administrators or Backup Operators, and differs from `svc-inference-gw`. Maintenance uses a separate administrator account. *Arena-side decision, matrix row 10.*
4. Machine-wide CPython 3.12.10 at `C:\Program Files\Python312\python.exe`, and PowerShell 7.4+.
5. The inbound rule from §1, after `verify`. No outbound change. The hub must allow TCP 11434 outbound to the enrolled heads; F: 1.1.0 inspects this.
6. Head firewalls admitting .254 (and .1 only during cutover), refusing .201, .202 and others.
7. The verified head-inventory export (§8), and the Arena `gateway` block (§11).

## 13. Rollback and uninstall

- **Rollback.** `rollback` verifies a ledger backup, then switches the service to the previous side-by-side release.
  - Schema changes in 0.2 and 0.3 are additive: older releases ignore the new tables and rows.
  - **A release older than 0.3.0 would ignore the per-application operation and context limits, `required_capabilities` and the authority binding**, and its bootstrap cannot read `credential: provisioned` clients. Supported rollback targets are therefore 0.3.x releases only. From 0.4.0, rollback to 0.3.0 is supported on the same ledger: `rollback` first waits for every head to be down to one reservation, because 0.3.0 recreates its one-lease-per-head index. 0.3.0 then ignores the 0.4.0 suspension tables.
  - If a later release is ever not backward-compatible, its manifest will say so, and rollback will need the backup made by `update`.
- **Uninstall.** `uninstall` drain-stops and unregisters. Releases are kept unless `--remove-releases`. **Data is never deleted.**
- **Removal order:**
  1. F: removes the inbound rule.
  2. Gateway `uninstall`.
  3. Optionally, F: removes the account.
  4. Ledger and backups follow the approved retention.
  5. Restore Arena's earlier configuration.
- **Ledger migration.** Drain and stop the old authority first; verified `backup`, then `restore`, then `adopt`; tell Arena, then `accept-ledger`. Never run two writable authorities.

## Open items

| ID | Item | Owner |
|---|---|---|
| O1 | Approve the lab LAN for the data classes sent over plain HTTP to heads, or require the mTLS proxy (§10) | Lab/security operator |
| O2 | Whether 192.0.2.1 is needed during cutover, and its end date | Operator, Capstone |
| O3 | The Arena standard account name(s) on the hub, and moving Arena off the administrative account (matrix row 10) | Agent Arena |
| O4 | Capstone reading `gateway-client.json` with `token_file`; retiring the three token variable names | Capstone |
| O5 | Coder's client adoption and its approved models | Coder |
| O6 | Physical acceptance (`docs/ACCEPTANCE_PLAN.md`) | Operator |
