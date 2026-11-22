# Independent deployment, Windows service and rollback (release 0.4.0)

**0.4.0** changes failure handling and placement ([FAILURES-AND-PLACEMENT.md](FAILURES-AND-PLACEMENT.md)). The deployment mechanics below are unchanged. The release adds:
- per-head `slots` and `memory_gb`, and the `failure_policy` and `placement_policy` blocks, in the bootstrap policy;
- a recovery thread in the service;
- the operator commands `lifecycle suspensions`, `probe` and `release`.

**Status.** The software for the managed Windows deployment is implemented and tested in isolation:
- the Service Control Manager host;
- persistent configuration;
- the installer verbs;
- TLS issuance;
- credentials;
- authority binding;
- backup and restore;
- the in-service observation publisher.

**192.0.2.254** is the intended administration host, never an inference head. Nothing was installed, bound, issued or changed on a real machine. Gateway and Agent Arena are separately installed and separately supervised. Neither needs the other's application or database. PyCharm and the development laptop are not runtime dependencies.

**The binding deployment interface** is [deploy/windows/DEPLOYMENT-CONTRACT.md](../deploy/windows/DEPLOYMENT-CONTRACT.md), with [deployment-manifest.json](../deploy/windows/deployment-manifest.json).
- The F: 1.1.0 mapping is [F-1.1.0-COMPATIBILITY.md](../deploy/windows/F-1.1.0-COMPATIBILITY.md).
- The operator's sequence is [OPERATOR-CHECKLIST.md](../deploy/windows/OPERATOR-CHECKLIST.md).
- Physical evidence is required by [ACCEPTANCE_PLAN.md](ACCEPTANCE_PLAN.md).

## Development and packaging

- **Development environment.** Use a fresh CPython 3.12 (tested 3.12.10) development venv: `scripts/Setup-Development.ps1`. Runtime has no third-party dependency.
- **Release package.** `python scripts/build_release.py` produces `release/inference-gateway-<version>/`:
  - wheel, `bin/gateway-deploy.py`, scripts, templates, docs, test report;
  - `deployment-manifest.json` with every file's SHA-256, and `SHA256SUMS`;
  - a hashed zip.
- **What is not packaged.** No ledger, key, certificate, token or environment. The builder refuses secret-like files.
- **The service does not use a venv.** It runs the **machine-wide base interpreter** against a side-by-side release folder (`pip --target`) through `inference_gateway/service_launcher.py -I`. A venv `python.exe` is a redirector, and its child process cannot connect to the SCM.
- **Not delivered:** a code-signed installer and a hash-locked offline Python installer. The operator verifies python.org's published hash.

## Enrollment

All administrative tools run through the release launcher: `L <tool> …`, where `L` is `"<python>" -I "<release>\inference_gateway\service_launcher.py"`.
- Tools: `bootstrap`, `credentials`, `heads`, `qualify`, `lifecycle`, `publisher`, `observe`.
- Development equivalents: `python -m inference_gateway.<module>`.

**Policy.**
- Explicit declarations only. A `credential: "provisioned"` client takes its token from `credentials issue` and its policy from bootstrap.
- Applying policy never re-enables a revoked client. Revocation is explicit (`credentials revoke`).
- Optional per-client `operations` and `max_context` restrict an application below the service bounds.
- *0.4.0.* Each resource may carry `slots` (concurrent leases, set to the head's `OLLAMA_NUM_PARALLEL`; default 1) and `memory_gb` (GiB its models may occupy; absent = unknown).
  - Optional top-level `failure_policy` and `placement_policy` set the probe backoff, the cascade window and the freshness limits.
  - Example for the nine lab heads: `config/policy.lab-example.json` (no clients, so it never changes client policy).
  - `heads` keeps a head's `slots` and `memory_gb` when it re-imports Arena's inventory.

**Heads.**
- Imported from F:'s verified inventory with `heads`, then reviewed, then bootstrapped.
- Endpoints must be private **IP literals**: `http://192.0.2.N:11434`.
- Never enroll 192.0.2.254.

**Qualification.** It performs real synthetic model execution under a lease and produces immutable evidence.
- Operation purposes `completion` and `embedding` gate dispatch, as in 0.2.0.
- Capability purposes `chat`, `tools`, `structured_output`, `vision` and `context` add measured evidence to a current completion qualification:
  - exact-match fixtures;
  - a failure never withdraws completion capacity;
  - *0.4.0:* only uncertain execution holds the head: a dispatched call that times out, is reset or is truncated after it was sent. The head is then held until the call's deadline has passed, then probed and returned automatically. An HTTP error reply is an answer, not uncertain: the qualification fails and the head stays in service (`degraded` until the next heartbeat). This replaces 0.3.0's "an unanswered dispatched call quarantines the head", which also caught immediate error replies.
- See [CLIENT_CONTRACT.md](CLIENT_CONTRACT.md) §3.

## Startup, shutdown and recovery

- **Start.** The service (`windows_service`) or console mode (`console --config`) runs the same runtime:
  - exclusive ledger lock;
  - **authority claim**: host, ledger path and service ID are bound in the ledger; a copied, moved or renamed authority refuses to start until `lifecycle adopt`;
  - resumption of a drain the service itself made;
  - restart reconciliation without replay;
  - TLS listener; health refresh; in-service publisher.
- **Stop (SCM STOP or PRESHUTDOWN, Ctrl+Break in console).**
  - New submissions get 503.
  - A persistent service-stop drain begins.
  - Active allocations get up to `shutdown.drain_seconds` (default 90).
  - Then the listener closes.
  - Anything still allocated becomes `unknown` at the next start and is never replayed.
- **Maintenance drain.** `lifecycle drain` survives restarts until `lifecycle resume`.
- **Recovery.** SCM restarts after 30 s, 60 s, then every 300 s, including after an error exit.
- **Failures (0.4.0).** A failed call never takes a whole head out unless execution is uncertain.
  - **4xx reply:** the request fails.
  - **5xx or invalid reply:** that model is suspended on that head, and the request is re-placed once.
  - **Unreachable:** the head goes offline until its heartbeat returns, and the request is re-placed once.
  - **Uncertain:** the head is held (health `quarantined`).
  - **One model failing on two heads within 10 minutes** is suspended everywhere.
  - The service's recovery thread probes every suspension and hold with the qualification test, then releases it or backs off 1, 5 and 15 minutes, then sets `needs_attention` with an alert in the observation file.
- **Held head.** It keeps its uncertain lease until expiry. It is probed only after the request's deadline has passed and `/api/ps` shows no loaded model.
  - Watch: `lifecycle suspensions`.
  - Probe now: `lifecycle probe --suspension <id> --operator <you>`.
  - Manual override: `lifecycle release --suspension <id> --operator <you> --confirmed`.
  - Still supported: `lifecycle reconcile-resource --confirmed-idle` after verifying the head is physically idle, then requalify.
- **Logs.** `logs\gateway-service.log` (JSON lines, 10 MiB × 10) and `gateway-fault.log`. They hold lifecycle metadata and sanitized capacity event types; never prompts, outputs, tokens or keys.

## Arena observation publication

- **Where it runs.** In the Gateway service (`observation` block in `gateway-service.json`): every 5 s, atomic, overlapping cursor, ledger acceptance (see [ARENA_INTEGRATION.md](ARENA_INTEGRATION.md)). Freshness therefore also means the service is running.
- **Alternative.** `scripts/Register-ObservationPublisherTask.ps1` remains for an operator who wants it separate. It then needs batch-logon rights and a stored task credential.
- **ACL.** `scripts/Set-ObservationPublicationAcl.ps1` gives each Arena account read on publication files only, and fails closed unless every Arena account is a standard account. The rule is analysed in F-1.1.0-COMPATIBILITY.md.

## Controlled migration and rollback

1. **Choose one authority.** Decide the single authoritative ledger. Never bootstrap a fresh production ledger over existing identity or history. Capstone's original gateway modules were removed during Capstone's cleanup and are **not** a recovery path. The recovery checkpoints are this project's `checkpoints/` archives and verified ledger backups.
2. **Back up the current authority.** Drain and wait, verify the heads are actually idle, record unresolved work, stop it and block restarts. Then `lifecycle backup`: SQLite backup API, integrity check, SHA-256 record. Backups contain private results and credential hashes; keep them in `backups\`.
3. **Restore on 192.0.2.254.**
   - `install --restore-backup <file>`, or `lifecycle restore --replace`, which keeps the replaced file aside.
   - Then `lifecycle adopt` (new host and path), keeping service, client and resource IDs.
   - Then tell Arena, then `publisher accept-ledger`.
   - Exactly one writable authority may be enabled.
4. **Head prerequisites (owned by the head provisioner).**
   - Heads admit 192.0.2.254 (and 192.0.2.1 only during an approved cutover), and refuse application hosts.
   - F: 1.1.0 implements this; it has not been run on any head.
   - Requalify after any endpoint change. The head transport decision is in the contract §10.
5. **Clients.** Distribute `gateway-client.json`, the token file and the CA. Capstone uses `token_file` (contract §9). Disable any local-ledger publisher or administrative route in applications, and remove application OS access to the ledger.
6. **Acceptance.** Run [ACCEPTANCE_PLAN.md](ACCEPTANCE_PLAN.md) with real Capstone and Coder traffic. Resume admission only after the operator accepts the evidence.
7. **Rollback.** `rollback`, or `update`'s automatic switch-back, verifies a backup and returns to the previous side-by-side release against the current ledger. Never restore an old ledger and silently lose leases or history. Never run old and new authorities concurrently on independent writable ledgers.
8. **0.3.0 → 0.4.0.** `update` keeps the ledger and its `ledger_id` (printed as `ledger.kept`).
   - The schema change is additive: new tables and slot triggers replace the one-lease-per-head index.
   - Every head quarantined at upgrade time becomes a held head on the probe schedule, listed in `held_at_upgrade`.
   - Rollback to 0.3.0 still works. It waits up to 75 s for leftover leases to expire, and refuses while a head holds two reservations.
   - 0.3.0 then ignores the 0.4.0 tables: held heads stay quarantined (operator reconcile), model suspensions stop applying, one lease per head.
   - See [FAILURES-AND-PLACEMENT.md](FAILURES-AND-PLACEMENT.md) §7.
