# Agent Arena Lab Provisioner 1.3.8: operator guide

One USB for every machine in the 192.0.2.0/24 lab. Insert it, double-click
**START-LAB-PROVISIONER.cmd**, approve the administrator prompt, and choose the
machine's role.

## The lab

| Role | Address | What runs there |
|---|---|---|
| Administration hub | 192.0.2.254 | Agent Arena (event service, admin window) and, separately, the Inference Gateway |
| Capstone laptop | 192.0.2.201 | Capstone (its own release), the Arena Ticketing adapter |
| Coder laptop | 192.0.2.202 | Coder (no release yet) |
| Inference heads | 192.0.2.2-.199 | Ollama, Arena head viewer |
| Development workstation | 192.0.2.1 | temporary development and migration access |

**Protected addresses.** A new head can never take:
- .1 (dev workstation);
- .2 (the hand-built GPU workstation);
- .105 (DESKTOP-REDACT12, Phothera Head);
- .201 and .202 (the application laptops);
- .254 (the hub).

**Who may reach a head's Ollama port 11434:**
- **cutover:** .254 (the Gateway) and .1 (the documented development exception);
- **gateway-only:** .254 alone, once the cutover is complete.

The Capstone and Coder laptops are never given direct Ollama access; they use
the Gateway.

## Every role, the same six stages

| Stage | What happens | Changes the machine? |
|---|---|---|
| A Inspect | Reads hardware, Windows, network, Ollama and models, firewall rules and profiles, tasks and services, Arena, Gateway and Capstone state, backups. Every fact is marked *observed*, *inferred*, *missing* or *inaccessible*. | No |
| B Plan | Proposes the steps for this machine: address and conflicts, hardware fit, software, configuration, credentials needed from others, model profile, security, approvals, backups and rollback. Stops if anything is blocked. | No |
| C Configure | Runs only the approved steps, in order. Disruptive steps (address change, Ollama restart, disabling a firewall rule that is not the provisioner's own) ask again. A failed step stops the stage. | Yes, each change recorded with its undo |
| D Register | Uses only existing Arena contracts: the address claim code and the display enrollment code from the admin console, the existing Ticketing installer and its app code. A service that does not answer leaves registration **pending**, never faked. | Arena records only |
| E Verify | Tests what this machine can test, and lists what must be tested from another machine (labelled *lan* or *external*). | No |
| F Report | Writes `C:\ProgramData\AgentArenaLab\reports\<computer>-<run>.txt` and `.json`, sanitized. Offers to copy them to the USB's `reports\` folder. | No |

**Final status:**
- **verified:** everything required passed;
- **partially verified:** nothing failed, but something is pending (a reboot, another machine, another project);
- **blocked:** the plan could not be carried out;
- **failed:** a step or a required check failed.

**Codes and credentials are typed when asked and never stored on the USB.**
Get them from the hub's admin console:
- **address claim code:** Machines > Addresses;
- **display enrollment code:** Machines > issue code;
- **app code:** Machines > Applications.

Gateway tokens, the CA file and Capstone's developer token come from their
owners and are never read; the provisioner only checks that they exist.

**Rollback.**
- Every run keeps `C:\ProgramData\AgentArenaLab\runs\<run>\`, containing:
  - `changes.json`;
  - `backups\`:
    - network JSON;
    - time and power values;
    - the earlier Ollama task XML and variables;
    - the firewall rules as JSON plus a full `netsh advfirewall export`.
- To undo a run: **Existing machine > 9 Roll back a previous run's changes**. Each change is confirmed one by one (type UNDO).

## 1 and 2: New CPU or GPU inference head

Before you start:
- reserve the address in the admin console (Machines > Addresses, role head) and note the claim code;
- connect the head's Ethernet to the lab switch, and give it internet (Wi-Fi) if it should download Ollama or models.

Steps:
1. **Choose the role, then answer the prompts:**
   - the head number N (192.0.2.N);
   - cutover or gateway-only;
   - model profiles. The proposal comes from the hardware:
     - GPU with 8 GB+ VRAM: GPU;
     - under 11 GB RAM: CPU8;
     - under 24 GB: CPU16;
     - otherwise: CPU32.

   You can add catalog tags or leave some out.
2. **Plan.** It is refused if:
   - the machine is already a head, the hub, an application laptop or the GPU workstation (use *Existing machine*);
   - the address is protected or answers on the network;
   - a GPU head has no qualifying NVIDIA GPU.
3. **Configure**, in order:
   1. **Address:** static, no gateway, using the 1.0.3 adapter choice with a backup.
   2. **Time:** Eastern time zone, Windows Time from the hub.
   3. **Power:** no sleep on AC power, Ethernet power saving off.
   4. **Ollama:** installed if missing. The signed installer is downloaded, or taken from `packages\ollama\OllamaSetup.exe`.
   5. **Boot task** "Capstone Ollama Server":
      - runs as SYSTEM through a wrapper that sets `OLLAMA_HOST=0.0.0.0:11434` and an explicit `OLLAMA_MODELS`;
      - the model folder is the one that already holds the models, or `C:\ProgramData\Ollama\models` on a fresh head;
      - no model is ever moved or deleted.
   6. **Ollama start:** started from the task. The models are listed before and after; if any disappears, the step fails.
   7. **Firewall:**
      - one managed rule admits only the approved clients;
      - the 1.0.3 rule is disabled;
      - any other Ollama rule that admits other machines is offered for disabling, one by one (type DISABLE);
      - broad or Group Policy rules are only reported.
4. **Register:**
   1. Type the claim code: the head is registered in Arena with its display.
   2. Type the display enrollment code: the Arena viewer 2.1.2 installer opens as you. Click Install, then Launch Arena. The provisioner waits for its result.
5. **Verify:**
   - Ollama on the head address;
   - the boot task;
   - the served models match the disk;
   - the firewall analysis (.254 admitted, laptops refused);
   - the clock against the hub;
   - Arena reachable and the head registered;
   - the viewer.
6. **Models** (separate stage, own approval):
   - each model is pulled, then checked in `/api/tags`, its digest compared with the catalog, and a real generate or embed request made;
   - without internet nothing is attempted and the report says **not downloaded (offline)**; a tag is never called invalid because the internet is missing;
   - Gateway qualification is never claimed here.
7. **Afterwards:** reboot, sign in, run **Existing machine > 6 Post-reboot verification**, then on the hub run **Administration hub** to prove the Gateway host reaches the head.

## 3: Administration hub (192.0.2.254)

The hub role asks what to do:

| Choice | What it does |
|---|---|
| 1 | Machine prerequisites and Agent Arena |
| 2 | Inference Gateway 0.3.0: guided installation |
| 3 | Move Arena to a standard Windows account |
| 4 | Verify only (read-only) |

Do them in the order 1, 3, 2.

**Staged rollout on 192.0.2.254.** Each stage is approved separately; nothing below is reset by stopping between stages.

| # | Stage | Menu | Changes |
|---|---|---|---|
| 0 | **Inspect the hub** (first visit) | START-LAB-PROVISIONER > **7 Inspect only** | none. It writes only its run and report files under `C:\ProgramData\AgentArenaLab`, and, if you accept, a sanitized report copy on the USB |
| 1 | Machine prerequisites and Arena | hub > 1 | yes |
| 2 | Arena on a standard account; then verify the migrated Arena | hub > 3, then hub > 4 | yes, then none |
| 3 | Gateway, up to install | hub > 2 | yes. With no head, it **stops safely** after install |
| 4 | One CPU head prepared (GPU .2 stays out of the initial rollout) | on the head: role 1, or Existing machine > Ollama firewall | on the head |
| 5 | Resume: inventory, review, credentials, bootstrap, qualification, ledger acceptance, ACL, verify | hub > 2 again | yes. Install, certificate, credentials and acceptance are not repeated |
| 6 | Listener rule after `verify` | same run | yes |
| 7 | Arena observation | hub > 4 | none |
| 8 | Capstone on .201 (section 4) | on .201 | yes |

**What Inspect reads on the hub:** the lab address and adapter, the network profile and the firewall (rules, profiles, the outbound verdict to each head's 11434); the Arena install, its service identity and data; the account Arena runs as, and its Administrators and Backup Operators membership through nested groups; the elevation of the session; machine-wide Python 3.12.10 and PowerShell 7.4; services and tasks; the Gateway state; and the migration blockers. It reads this machine as it is. It assumes nothing from the development laptop.

**Reading the hub's Verify (1.3.1).** Verify knows the staged rollout. A check on something a later stage installs is **PENDING** until that stage is complete, and **FAIL** only once it is complete and the check still does not hold:

| Check | PENDING while | FAIL when |
|---|---|---|
| Arena configured to read the Gateway observation | hub > 1 has not run its Arena Gateway configuration step | that step ran (or the Gateway is complete) and a live Arena account is still not configured |
| Arena accounts meet the Gateway account rule | the Gateway is not installed yet: hub > 2 refuses to start until hub > 3 has moved Arena | the Gateway service exists and an Arena account is an administrator |
| Gateway observation (file, ACL, fresh) and Arena observation | the Gateway sequence has not reached its listener step, or Arena is not configured | both are complete and the observation is not live |
| Outbound to the heads (rule analysis) | a block rule Windows decides by program path or network keyword may apply: the Gateway's own `heads` command is the proof | a block rule covers the Gateway's runtime (`C:\Program Files\Python312\python.exe`), its service, or any program |

- **Arena's local health** is probed where Arena's own `aes.config.json` binds it. The hub installer binds 192.0.2.254 only, so `127.0.0.1:8790` is refused there by design.
- **Outbound rules for other programs** (spoolsv, Teams, Wi-Fi Direct and similar) are not counted against the Gateway, nor are **Store-app rules** (`…-In-Allow-ServerCapability`, bound to one app package). Neither applies to the Gateway or to Ollama.
- **The report** shows the hub choice, every approval with its answer, and the number of changes recorded. If an approved plan started no step at all, the report says so.

### 3.1 Machine prerequisites and Agent Arena

**Machine preparation.** The settings are those of the field-verified `SET-UP-THIS-COMPUTER`, but each one is its own step with a backup and undo:
1. the address, 192.0.2.254/24 (asks again before changing it);
2. the lab adapter's network profile, **Private**. The Gateway's listener rule applies to the Private profile only;
3. stay awake: no sleep or hibernate, screen on, the lid does nothing, Ethernet power saving off;
4. Eastern time, with this computer serving time to the lab;
5. inbound time (UDP 123) and ping from 192.0.2.0/24, and Remote Desktop from 192.0.2.1 only;
6. Python 3.12+ for this user, which the Arena installer needs.

**Arena:**
- **`INSTALL-ADMIN-HUB`**, unchanged. It runs as you, checks first, and keeps the existing data and service identity `aes-a1e79a8b3902`.
- **Arena's Gateway configuration.** Only the `gateway` block of `aes.config.json` is written, in the Arena account's own profile. Restart the admin console afterwards (tray > Restart service).

### 3.2 Move Arena to a standard Windows account (Gateway open item O3)

**Why.** The Gateway refuses to share the hub with an Arena that runs as an administrator. An administrator can take ownership of the Gateway's private ledger regardless of permissions. The rule covers every account that runs an Arena process (window, console, event service), and it counts Administrators and Backup Operators membership through nested groups too.

**This changes how the hub is operated:**
- Arena runs as a standard account;
- maintenance uses a separate administrator account.

That decision is yours. F: never creates, removes, demotes or switches an account on its own.

**Before:**
- create the standard local account (on DESKTOP-REDACT10: `ArenaSvcUser`) with **Settings > Accounts** or `net user`. It must not be in Administrators or Backup Operators;
- do it in a quiet period: events that arrive between the export and the new account's first start are not carried over.

**Procedure:**
1. **As the maintenance administrator:** START-LAB-PROVISIONER > Administration hub > **3**, and type the account name: `ArenaSvcUser`, `.\ArenaSvcUser` or `DESKTOP-REDACT10\ArenaSvcUser` (another computer or a domain is refused). The plan checks:
   - exactly one live Arena install exists. A copy already exported (marked moved) is reported, never exported twice;
   - the target exists, is a standard account (transitively), and isn't the Gateway account.

   Type APPROVE, then MIGRATE. To run Arena's migration helper, F: uses the machine-wide Python if present, otherwise the Python that Arena itself runs on, which may not be on the administrator's PATH. F: stops the current Arena, then exports into a folder only that account can open:
   - the hub program;
   - a consistent copy of its database, with the same service identity, registrations, enrollments and history.

   The old copy is marked moved and **refuses to start** from then on, so two Arena services never run on one identity.
2. **Sign out; sign in as the new account.** If `py -3 --version` fails, first run `INSTALL-PYTHON-FOR-THIS-USER.cmd` from the export folder. It installs the same verified Python for that user only, because the Gateway's machine-wide Python is deliberately not on PATH. Then run the exported `INSTALL-ADMIN-HUB.cmd` (the path is in the report). It is the unchanged installer in "moved data" mode, the same mechanism that moved Arena from the development laptop to the hub. It creates the start-at-sign-in entry and shortcut for that account and deletes the copied data from the export folder.
3. **Sign back in as the maintenance administrator.** Administration hub > **4 Verify**. Arena must:
   - answer as `aes-a1e79a8b3902`;
   - run as the new account;
   - be reported "standard".

   The old account's copy is listed as a rollback copy that does not start. It does not count as an Arena account unless it runs again.
4. **Administration hub > 1:** only the Arena Gateway configuration step changes anything; it writes into the new account's profile. Its Arena installer step reports PENDING, because Arena now belongs to the new account. To update Arena later, sign in as that account and run `AGENT-ARENA-HUB\INSTALL-ADMIN-HUB.cmd` from the USB. F: never installs a second Arena for the administrator.
5. **When satisfied:** as the old account, run `UNINSTALL-ADMIN-HUB.cmd`. It keeps data and removes its start entry. The marked data stays as the rollback copy.

**Rollback:** Existing machine > 9 Roll back, choosing the migration run. F::
- stops the new account's Arena;
- removes the marker it wrote (never the original move-to-hub marker);
- deletes the export folder.

Then sign in as the old account: its admin window starts the old service with its data as it was at the export. As the new account, run `UNINSTALL-ADMIN-HUB.cmd`.

### 3.3 Inference Gateway 0.3.0: guided installation

**Setup:**
- Sign in with the **maintenance administrator** account, not Arena's.
- The Gateway release travels unchanged in `packages\gateway\`, with its published `.sha256`.
- **F: only prepares the machine:** the runtimes, the service account and its rights, the listener firewall rule, and Arena's own settings. Every other step runs **the Gateway's own command**, and continues only when it prints `"ok": true`.
- **Exit codes:** 0 success, 1 a failed check or refused step, 2 bad input. A failure stops the sequence and nothing after it runs.
- **Resuming:** run choice 2 again after a physical gate (moving the CA key, carrying a token, qualifying). Steps already done report UNCHANGED. A resume never resets anything:
  - the service, the certificate and the ledger are left as they are (install is UNCHANGED);
  - an application that already holds a credential keeps it, unless you type ROTATE and then approve ROTATE;
  - each `heads` run writes a new `policy-review-<time>.json` (the Gateway never overwrites a review); pressing Enter at the policy prompt reuses the newest one;
  - an accepted ledger is not accepted again.
- **Safe stop with no head.** If no head admits the hub yet, the run stops after install. Step 9 is PENDING, the later steps are PENDING, and the run is not failed. Prepare one CPU head (role 1, or Existing machine > Ollama firewall on a 1.0.3 head), then run choice 2 again.
- **Credentials come before bootstrap.** This differs from the manifest's `order`. The Gateway's `bootstrap` only updates a client declared `"credential": "provisioned"` and refuses one that has no credential yet. So F: issues first. For the same reason, the approved policy must not declare `coder` until O5 is decided: F: reports it and holds bootstrap rather than sending it.

**Steps:**

| # | Step | Whose command | Gate |
|---|---|---|---|
| 1 | Release intake: archive against `.sha256`; 16 files against `SHA256SUMS` and the manifest; the manifest against the lab | F: (read-only) | refuses any mismatch |
| 2 | Machine-wide Python 3.12.10 (`C:\Program Files\Python312`) and PowerShell 7.4.22, from `packages\runtime`. Python's installer runs **as SYSTEM** from a re-checked copy in the run folder. Run as the administrator, it collided with that administrator's own per-user Python 3.12.10 (DESKTOP-REDACT10: exit 1603). Per-user copies (LabOperator1's, ArenaSvcUser's) are left as they are. Every installer writes a log; a failure quotes its error lines and copies the logs to `reports\<computer>\<run>-installer-logs` on the USB | F: | publisher hash and Authenticode signature first; APPROVE |
| 3 | Account `svc-inference-gw`: standard, Users only, *Log on as a service*; interactive and Remote Desktop logon denied | F: | CREATE-ACCOUNT; you type the password (twice) |
| 4 | Every Arena account is standard (transitive), not the Gateway account | F: check | stops here until 3.2 is done |
| 5 | `deployment-settings.json` from the Gateway template. `arena_accounts` is filled in, and every account is written `<computer>\name`: Windows' name lookup, which the Gateway uses, cannot resolve the template's `.\svc-inference-gw` | F: | – |
| 6 | `plan` (read-only; on a first install only `tls` and `ledger` may be missing) | Gateway | – |
| 7 | `certificate`. Then **move `C:\ProgramData\InferenceGateway\ca\ca.key` to offline media** (type MOVED, or LATER: it stays pending). The public CA is copied to the provisioner stick's `gateway-ca` folder for the clients | Gateway | APPROVE |
| 8 | `install --password-stdin --new-ledger` (or `--restore-backup <file>` for a migration; never a fresh ledger over an existing one) | Gateway | INSTALL; you type the password again, twice (stdin only, in the ANSI code page Python reads) |
| 9 | Verified head inventory export. **No head: safe stop here** | F: | needs the heads' firewall cutover |
| 10 | `heads --inventory … --policy <approved policy> --output policy-review-<time>.json`, then you read the review. The model menu comes first (see **Choosing the Gateway's models** below) | Gateway | the policy is checked: `capstone` and `capstone-research` separate; research lower priority, `max_concurrency` 1, no placeholders; a declared `coder` is flagged |
| 11 | `credentials issue` for `capstone` and `capstone-research` (from the review) into an administrators-only outbox, readable on the target by LOCAL SERVICE. **Carry each file on a second USB stick (not the provisioner stick) and delete the hub copy.** The provisioner never reads them. Existing credentials are kept (ROTATE to replace). Coder: later (O5) | Gateway | one ISSUE for both |
| 12 | `bootstrap --policy <review>`, only when every client it declares holds a credential | Gateway | APPLY |
| 13 | `qualify` every model the policy names on every verified head that has it, at 8192 tokens (what Capstone sends; the Gateway refuses a request above the qualified context). Real inference, recorded evidence. `qwen3:4b` cannot pass (it reasons despite `think: false`) | Gateway | one QUALIFY for the list |
| 14 | Tell the Arena operator the `ledger_id`; `publisher accept-ledger` | Gateway | ACCEPT |
| 15 | `Set-ObservationPublicationAcl.ps1` with every Arena account (PlanOnly first) | Gateway (PowerShell 7) | must print `ok: true` |
| 16 | `verify` | Gateway | must print `ok: true` |
| 17 | Inbound rule `InferenceGateway-HTTPS-8443`: TCP 8443 on 192.0.2.254 from .201 and .202 only, Private profile | F: | only after `verify` |
| 18 | Optional: a **separate** temporary rule for 192.0.2.1, with an end date (O2) | F: | CUTOVER |
| 19 | `/health` and `/ready` over TLS with the Gateway CA; Arena's observation checked separately | F: (read-only) | – |

**Choosing the Gateway's models (step 10).**
- The menu lists every chat model installed on the verified heads, numbered, with the heads that have it. `[x]` marks the allowed ones.
- Enter keeps the list; numbers (for example `3,7,12`) replace it.
- The policy on the USB (`reports\GATEWAY\gateway-policy-approved.json`) is rewritten, and the previous one is kept beside it as `.before-<time>.json`.
- Capstone gets the chosen models. Capstone-research keeps those of its own that are still chosen, otherwise the first chosen.
- Qualification then tests exactly the allowed models.

To change models and heads later, use the **admin hub's Fleet window** (below). The Gateway Models screen stays as the health view and a fallback. Hub > 2 also still works: finished steps are skipped, and existing rules are not asked again.

**The admin hub's Fleet window (Arena 2.3.4, set up by Administration hub > 5).** The **Agent Arena Admin Hub** window
runs only under **ArenaSvcUser**: sign in as ArenaSvcUser (Start > account picture > Switch user). It opens by itself
at sign-in, or from **Agent Arena Admin Hub** on the desktop. Open the **Fleet** menu > **Heads and models…**:

| Tab | What it shows and does |
|---|---|
| **Heads** | Every head by hardware tier. The tier comes from the head's own report; what the report lacks comes from the lab's head list (1.5.7: heads 2 and 10 have no Arena viewer, and head 2's report has no GPU); the **Tier** list in its details overrides both. Click a head's name (**Head 2**) to open its details: **Models on head N** with **State** and **Why** for each model; the projects it serves (this also adds a new head to the Gateway); **Do not use**, **Uninstall**; **Install a model** with **Add to changes** for one model and **Add all N planned** for every model the plan wants for its tier that is not installed (when nothing is missing, or the tier is unknown, a line says so instead). The heads are scanned every minute; **Scan the heads now** does it at once |
| (model states) | **usable**: passed the Gateway's test on this head. **Gateway rule**: works, but Gateway 0.3.0's test refused its answer (it wants exactly `READY`); only the Gateway project can change that. **retest**: the test never judged it (it ran out of time, or the head was locked); **Test models not usable yet** tests it again. **failed**: refused for a reason a retest does not change (the Why column says it). **installed**: not tested by the Gateway. Each says the date of the test |
| **Projects** | For `capstone`, `capstone-research` and `coder`: the models it may use (× removes one, **Allow** adds one) and the heads that serve it. Coder shows "not connected yet" until it has a Gateway login |
| **Catalog** | The plan spreadsheet: each model, its size and the tiers it is planned for. **Install on…** picks heads |
| **Changes** | Everything waiting. **Commit…** applies it |

**Commit.** Windows asks for an administrator's password (LabOperator1) once. A **Fleet Commit** window then lists the changes. Type **APPROVE** there and it makes them in this order:
1. allowed models (the Gateway's `bootstrap`);
2. projects per head (its heads tool, then `bootstrap`);
3. Do not use;
4. uninstall: out of the Gateway, then Ollama's delete on the head;
5. install: the hub downloads the model once over its Wi-Fi into `C:\ProgramData\AgentArenaLab\model-library`, and each head pulls the identical copy over the lab cable. The digest is checked. Port 11500 is open to those heads only while this runs;
6. the Gateway's tests for the allowed models the changes touched. (1.5.2) Each model is first loaded on its head
   directly (a cold load can take minutes; the Gateway's own test allows 90 s in all), a head the Gateway locked after
   an earlier timed-out test is unlocked once it is idle, heads test side by side, and every failure says why. A model
   that answers `Ready.` or `READY.` instead of exactly `READY` works but is refused by the Gateway's test; the line
   says so.

**Test models not usable yet** (Changes tab, Arena 2.3.3) adds one change: test every allowed model, on every Gateway
head that has it, that is not usable yet. Use it after adding heads, or when tests ran out of time before.

Progress shows in the admin hub (Changes tab). What completed leaves the list; what failed stays, with the reason. Arena never holds administrator rights and never reads the Gateway's ledger: after each Commit the helper publishes the Gateway's state to `C:\ProgramData\AgentArenaLab\fleet\published`.

**Updating Arena on the hub** (for the Fleet menu): sign in as ArenaSvcUser (Switch user) and double-click `AGENT-ARENA-LAB-<version>\payload\AGENT-ARENA-HUB\INSTALL-ADMIN-HUB.cmd` on the stick. No administrator password is needed; it keeps Arena's data and service identity.

**The Gateway Models screen (Administration hub > 5, once; 1.1.0 opens on the system health view).** Installs the screen in `C:\Program Files\AgentArenaLab\GatewayModels`, with a **Gateway Models** shortcut on the administrator's desktop. The Fleet Commit helper and the model library are installed beside it.
- Every file is checked against the USB.
- Standard accounts cannot change it, because it runs as administrator.
- Its settings folder `C:\ProgramData\AgentArenaLab\gateway\models-console` is SYSTEM and Administrators only.
- Its self-check then reads the Gateway once (read-only).

After that the USB is not needed to change models. On the hub, as the maintenance administrator, double-click **Gateway Models** and click **Yes**: the screen opens in the browser. Keep its black window open while you use it.

| On the screen | What it does |
|---|---|
| A tick under an orchestrator (`capstone`, `capstone-research`) | That orchestrator may use the model |
| **All** | The model is available to every orchestrator, including ones added later |
| A head's square | ✓ usable · ○ installed, not tested yet · ✗ failed the Gateway's test · ↻ needs a new test · ⛔ Do not use · – not installed. Click it to mark the model **Do not use** on that head, and again to allow it there again |
| **Review and apply…** | Shows exactly what will change, then **Apply now** does it in the background (see below) |
| **Audit heads** | Asks every address .2–.199 for its models, now. A head that answers but is not in the Gateway gets **Add to Gateway** |
| **Install on heads…** | Makes the install note: the one line to paste at each head while it is on Wi-Fi (`ollama pull <model>`) |
| **Check installs** | Looks at the heads again and shows which of them now have the models you asked for |

What Apply runs, through the Gateway's own tools:
- **Allowed models:** the Gateway's `bootstrap`, with only `models` changed. Every other client setting comes from the Gateway's own `credentials list` and is kept as it is.
- **Tests:** the Gateway's `qualify` at 8192 tokens, for every allowed model on every online head that has it and has not passed there yet. Each head pauses for its own test, about 10 s per model.
- **Skipped tests:** a model whose last test failed is skipped. Tick **Retest** in the review to try it again.
- **Do not use:** the model is taken off that head's qualified list at once, the same change the Gateway makes when a test fails, recorded as its `qualification.invalidated` event. Gateway 0.3.0 has no command for this, so the screen makes it in the ledger (reported in `docs/handoff/GATEWAY-0.3.0-FIELD-FINDINGS.md`). Allowing it again runs a new test.
- **The record:** the screen keeps the policy it applied in its settings folder. Hub > 2 uses that copy instead of the USB's, and never qualifies a Do not use pair, so a USB re-run does not undo the screen. Every change is logged in `changes.log` there, with the administrator's name.

**Installing a model on heads without internet.**
1. In Gateway Models, click **Install on heads…**, type the model (for example `llama3.2:3b`), tick the heads and click **Make the install note**. The note is also saved on the hub in `install-notes`.
2. At each head: unplug the lab cable and connect to Wi-Fi. Open PowerShell and paste `ollama pull llama3.2:3b`; no administrator rights are needed. Wait for `success`, disconnect Wi-Fi and plug the lab cable back in.
3. Back on the hub: click **Check installs**. To let an orchestrator use the model, tick it and **Apply**: it is tested on those heads.

The hub cannot put the note into a head's Documents folder. That would need file sharing on the heads, which the head firewall deliberately does not allow.

**Monitoring.** In 0.3.0 the observation publisher runs **inside the `InferenceGateway` service**; there is no separate publisher task. Verify reports:
- the service (running, delayed automatic, as `svc-inference-gw`);
- the Gateway's `status` (including ledger acceptance);
- TLS `/health` and `/ready`;
- the listener rule (.201 and .202 admitted, .1 refused unless the cutover rule exists);
- **separately,** Arena's observation: configured, file present, Arena can read it and nothing private, fresh, service running. "Live" is reported only when all hold;
- a token left in the outbox, or the CA key still on the disk.

### 3.4 How the Gateway spreads calls over the heads (0.4.3, stick 1.6.10)

**Slots and Ollama's real parallelism.**
- A head's Gateway `slots` (SET-PARALLEL-PER-HEAD) is how many calls the Gateway sends that head at once.
- Its `OLLAMA_NUM_PARALLEL` (HEAD-PARALLEL) is a setting, not a guarantee. Ollama ignores it for some model families and runs them **one call at a time**: every qwen3.5 build (family `qwen35`) and `qwen35moe` ([ollama#18938](https://github.com/ollama/ollama/issues/18938)).
- Measured on 2026-11-14: with 10 slots on heads 7–10 and Gateway 0.4.2, none of 194 qwen3.5:4b calls overlapped on a head.

**What 0.4.3 does:**
- **Least busy first.** Each call goes to the head with the fewest calls per slot. A head that already has the model loaded wins only a tie, and a head that would have to unload other models comes last. 0.4.2 sent every call to the one head with the model loaded until its slots were full.
- **One qwen3.5 call per head.** A head takes at most one call of a `qwen35` / `qwen35moe` model, whatever its slots; other models still use the remaining slots. Change it with `placement_policy.serial_families` or `model_parallel` in the bootstrap policy (Gateway `docs/FAILURES-AND-PLACEMENT.md` §4).
- **A late call fails alone.** When a call reaches its own deadline and the head still answers, only that call fails (`deadline_exceeded`). In 0.4.2 every such call held the whole head for about 2½ minutes, and the next head then took every call.

Keep the slots at what you chose per machine class; the per-model limit is applied on top. The Testing Lab's per-call limit is 5 minutes in Capstone rc.15 (45 s before).

### 3.5 Pinned Ollama 0.40.15 and measured parallel inference per head (stick 1.6.14)

**Why.**
- Ollama 0.40.13 ran qwen3.5 models one request at a time, whatever `OLLAMA_NUM_PARALLEL` said.
- 0.40.15 can run several at once ([release](https://github.com/ollama/ollama/releases/tag/v0.40.15), [PR 19545](https://github.com/ollama/ollama/pull/19545)).
- The heads get exactly 0.40.15, never "latest". The pin is `config\lab-network.json` `ollama.runtime`: the official `ollama-windows-amd64.zip` with SHA-256 `9ca74a7a...0221c6`.

**No internet is needed.**
- The archive is on the stick (`packages\ollama`).
- HEAD-OLLAMA checks its SHA-256 and the Ollama Inc. signature of `ollama.exe`, then unpacks it to `C:\ProgramData\AgentArenaLab\ollama\runtime\0.40.15`.
- The head's lab cable (192.0.2.x) and its firewall rules stay as they are. The tool compares the firewall rules before and after and says so on screen.

**On each head: HEAD-OLLAMA.cmd** (`engine\head-tools\head-ollama`), at the head's own keyboard, as its administrator.
- **1 = update and qualify** (about 1 to 1½ hours):
  - **Record.** It records the head: hardware, OS, the boot task, the wrapper, the Ollama processes and their owner, the listener, the server's `/api/version`, models and digests, loaded models, and the server's own config line.
  - **Keep for rollback.** It unpacks the pinned runtime. It keeps the wrapper as it was, plus `rollback-<time>.json`, in `C:\ProgramData\AgentArenaLab\ollama\backups`.
  - **Each level** (`OLLAMA_NUM_PARALLEL` 2, then 3, then 4):
    - It writes the settings into `ollama-serve.cmd` and restarts the boot task.
    - It proves what runs: `/api/version` must say 0.40.15, the listening program must be the pinned `ollama.exe`, and the server's `server config` log line must show the settings.
    - It sends qwen3.5:4b and the reviewer qwen3:4b a serial baseline first, then N simultaneous conversations. Each conversation has its own ticket code, and all run at num_ctx 8192. They cover:
      - structured JSON;
      - a tool call;
      - a review with a 1024-token output budget (output only: the context stays 8192).
  - **Pass.** A level passes when:
    - every answer is valid;
    - no answer carries another conversation's ticket code;
    - the compute windows really overlapped N times (`created_at` minus the prompt and generation times);
    - free memory stayed above 1 GB.

    Requests that only waited in Ollama's queue do not count.
  - **Kept setting.** The highest level that passed and still added 10 % throughput.
  - **Resident models.**
    - `OLLAMA_MAX_LOADED_MODELS` is 2 when both models fit and a mixed test passes, otherwise 1. One model serves many conversations; a second resident copy is not needed for that.
    - Head 6 (8 GB) keeps 1. Send the reviewer model to other heads.
  - **Report.** It writes `reports\<computer>\ollama-qualify-<time>.json` on the stick. The report holds every answer, every timing and the per-model entries for the Gateway.
  - **Reprovisioning.** It writes `ollama-settings.json`, so reprovisioning keeps the pinned runtime and these settings.
- **2 = qualify again**, on a head that already runs 0.40.15.
- **0 = roll back.**
  - Puts back the wrapper kept before the first update, which runs the old per-user `ollama.exe` again.
  - Marks the settings `rolled_back`, restarts, and checks `/api/version`.
  - Nothing is deleted: 0.40.15 stays unpacked.
- **Head 2** (GPU, protected in the lab plan) is refused unless a separately approved plan runs it with `HEAD-OLLAMA.cmd -AllowProtected`.

**The settings and what they mean.**

| Setting | Value | Note |
|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | 8192 | Initial diagnostic default. A request's own `num_ctx` still wins; the report records the context the server shows (`/api/ps`). |
| `OLLAMA_NUM_PARALLEL` | measured 1–4 | Per model, not Capstone's worker count. |
| `OLLAMA_MAX_LOADED_MODELS` | 1 or 2 | From memory and the mixed test. |
| `OLLAMA_MAX_QUEUE` | 16 | Ollama's own waiting line. A full queue answers 503 at once, so the Gateway sees it. A longer queue adds no capacity. The Gateway sends a head at most its slots, and each call carries its own deadline. |
| `OLLAMA_KEEP_ALIVE` | 30m | No loading and unloading between steps or bursts. |
| Flash attention, KV-cache type | not set | Runtime defaults, recorded as the server shows them. Evaluate memory optimisations separately, one at a time. |
| `OLLAMA_HOST`, `OLLAMA_MODELS` | unchanged | As provisioned. The report checks them before and after. |

**At the hub: APPLY-MODEL-CAPACITY.cmd** (`engine\hub-tools\model-capacity`), as LabOperator1, after the heads.
- It shows the qualification matrix: head, model, Ollama version and backend, digest, context, setting, measured parallel, resident models, memory, tokens/s serial and parallel, p95 latency, and the result. The full table is written to `matrix.csv` on the stick.
- With Gateway 0.4.4 or later, after you type APPLY, it gives the Gateway `resources[].model_capacity` through the Gateway's own bootstrap: `parallel` = measured, passed entries only. Slots, applications and memory are kept.
- The Gateway uses each value only while the head's live `/api/version` and the model digest match. Otherwise it keeps qwen35 at 1.
- The pressure test and SET-PARALLEL-PER-HEAD carry these limits from then on.
- Run SET-PARALLEL-PER-HEAD afterwards if a head's slots should rise. A head can run up to parallel × resident models calls at once.

**Then through the Gateway:**
- Run Capstone at 12, then 16 concurrent workflows, then 20 as a pressure test.
- Then run COLLECT-LAB-EVIDENCE on the hub and on the Capstone laptop.
- Report timeouts by phase: Gateway queue, load, prompt, generation and deadline. Do not assume that model size is the cause.

## 4: Capstone laptop (192.0.2.201)

**Capstone 6.0.0-rc.4** travels unchanged in `packages\capstone`. It is the approved release: the builder refuses any other archive, and the provisioner checks the archive's SHA-256 (`205879781df7…9ed6f7`) and then every file against Capstone's own manifest before running anything. It unpacks into `C:\ProgramData\AgentArenaLab\cs` after checking every path fits Windows' limit (1.4.4 used a deeper folder and stopped there). The provisioner stick may get any drive letter on this laptop (E: on DESKTOP-REDACT11); the provisioner finds its files where it was started. It runs only Capstone's own scripts, each after approval:

| Step | What runs | Gate |
|---|---|---|
| Lab address | The Ethernet adapter is set to 192.0.2.201/24, no gateway, no DNS (as for heads). Not needed when the laptop already holds it. No Windows Settings step | APPROVE |
| Clock | Time zone Eastern, Windows Time from the hub (as heads) | – |
| Smart App Control | Offered off when it is on or in evaluation (it can block unsigned parts of Capstone's packages and the Ticketing adapter) | TURN-OFF |
| Visual C++ runtime | Microsoft's Visual C++ 2015–2022 Redistributable (x64) from the stick, only when its DLLs are missing. Capstone's torch cannot load without it | APPROVE |
| Release | `Install-Capstone.ps1` on a laptop without Capstone. On an older Capstone: `Update-Capstone.ps1 -PackageRoot` (see below). The same version: UNCHANGED | APPROVE / UPDATE |
| Gateway client folder | The provisioner makes `C:\ProgramData\Capstone\gateway\` (SYSTEM and Administrators full, `NT AUTHORITY\LOCAL SERVICE` read) and copies the Gateway CA from the stick's `gateway-ca` folder, checked against its hash. No token and no client configuration | APPROVE |
| QMS and ERP tokens | (1.5.0) The tokens heads 3 and 4 put on the stick (Existing machine > 10 there) go into `C:\ProgramData\Capstone\services` and are deleted from the stick; then each service is called with its token from this laptop | APPROVE each |
| Configure | `Configure-Capstone.ps1 -OperationalGatewayBootstrap -ResearchGatewayBootstrap -DeveloperTokenFile -VerifyLocalAddress` (and `-ArenaAdapterRegistered` once the Ticketing adapter is enrolled; and `-QmsServiceConfig` / `-ErpServiceConfig` for each service that accepted its token, so Capstone uses head 3 and head 4 instead of its own copy; a running Capstone is restarted to use it). The two bootstraps are **Capstone's own templates** from its installed release (`config\inference-gateway.client.template.json`, `…research-client.template.json`), checked first against the Gateway manifest: URL `https://192.0.2.254:8443`, the CA path, token files in the protected folder, `token_file` only, and distinct tokens. Capstone copies them to `C:\ProgramData\Capstone\config\` and applies its LOCAL SERVICE ACLs | APPROVE |
| Startup task | `Register-CapstoneTask.ps1`: at machine startup, as LOCAL SERVICE | APPROVE |

**Two identities, two token files.** Capstone uses `capstone` for operations and `capstone-research` for the Testing Lab. Each has its own bootstrap and its own token file, issued separately on the hub:
- `C:\ProgramData\Capstone\gateway\capstone.token`
- `C:\ProgramData\Capstone\gateway\capstone-research.token`

Copy both from a second USB stick (never the provisioner stick) into that folder; they inherit its ACL. **The provisioner never writes or reads a token.** Until both are there, Configure stays PENDING and says which file is missing.

**Token environment variables are retired.** `CAPSTONE_INFERENCE_TOKEN`, `CAPSTONE_INFRASTRUCTURE_TOKEN` and `APPLICATION_INFERENCE_TOKEN` are not used. F: and Capstone both refuse a bootstrap that names one. If one is still set machine-wide, verify reports it by name; its value is never read.

**Developer password.** When `C:\ProgramData\Capstone\secrets\developer.token` is missing, the provisioner asks for it (APPROVE, then typed twice, 12 or more characters) and writes it without a byte-order mark. It is the login for Capstone's developer pages.

**Update and rollback (Capstone's own).** `Update-Capstone.ps1` keeps `C:\ProgramData\Capstone` (`operations.db`, `ai_configuration.db`, Testing Lab history, Candidate A, QMS, experiments). It backs up both databases, switches its pointer, and runs its own health check. If that check fails, it restores the previous release and both databases itself. Its health check needs both Gateway identities to authenticate. So F: runs the update only where the installed Capstone already has its startup task and both identities configured; otherwise it stays PENDING. Moving an older installation to RC.2 is the Capstone owner's procedure for a maintenance window. To go back: `C:\Program Files\Capstone\tools\Rollback-Capstone.ps1 -Version <previous>`.

**Verify:**
- Capstone installed at the approved version, in gateway mode;
- both identities: own bootstrap, own token file (LOCAL SERVICE read, nobody broad), CA, `token_file` only;
- the CA identical to the hub's; TLS to the Gateway with it, service `shared-inference`;
- **Capstone's own `Test-CapstoneHealth.ps1`:** `/api/health`, and both identities authenticated by its `Test-CapstoneGateway.ps1` (`models`, `policy`, `availability`; no secret printed). An unreachable Gateway is `offline_or_unverified`, never a local success;
- the external steps Capstone still reports, and its record of the Arena adapter registration;
- **a real connection to every head's port 11434 fails** from this laptop;
- (1.5.0) QMS on head 3 and ERP on head 4 answer this laptop and accept its token; Capstone's settings name them; no
  token is left on the stick; this laptop's own QMS/ERP data is the same as the services' (nothing to move);
- (1.5.0) **Capstone's own `Test-CapstoneEndToEnd.ps1`**: an operational generation, an operational embedding and a
  research generation through the Gateway, each with its request ID, head and model. The embedding needs
  **all-minilm:latest** allowed for `capstone` (the model of Capstone's existing index). Allow it in the hub's
  Gateway Models screen; allow no other embedding model for Capstone.
- (1.5.2) **Capstone 6.0.0-rc.5's own `Test-CapstoneLabHeads.ps1`** (asked first): one Research Lab Smoke case pinned
  to each head the Gateway serves to `capstone-research`, one after another; each must complete and write rows to the
  live `arena_events` (the hub's board shows them). One line per head. A head the Gateway serves without a usable
  model fails here: fix it in the hub's Fleet window (Test models not usable yet).

(1.5.2) The update to rc.5 runs Capstone's own `Update-Capstone.ps1` from the run folder with one exact edit: its
health wait asks Capstone's own address (`appsettings.json`), because the shipped one asks 127.0.0.1, where Capstone
does not listen. The step `capstone-lab-heads` writes `C:\ProgramData\Capstone\config\lab-heads.json` (heads 2-10,
names and hardware) for the Research Lab's Heads screen.

(1.5.3) The update to **rc.6** runs Capstone's own `Update-Capstone.ps1` **as shipped**: rc.6 waits at Capstone's own
address and its `Stop-Capstone.ps1` stops every process under the install folder. The approval prompt then has no
"(the lab points that wait …)" note, and the log says "Capstone's own updater, as shipped". A fresh install (a laptop
with no Capstone yet) first makes the empty `C:\ProgramData\Capstone\models\huggingface`, which Capstone's installer
checks for.

**Health.** After the startup task starts Capstone, the run waits up to 3 minutes for `/api/health`. If it does not answer, the report carries the task's result and the end of Capstone's logs, also copied to the stick (`reports\<computer>\<run>-capstone-diagnostics.txt`). Running stick > 4 again changes nothing and verifies again.

**The two codes, both from the admin hub (ArenaSvcUser's screen), Machines > Machines and codes…:**
- **Claim code:** Addresses: role *Application laptop*, address 192.0.2.201, application *Ticketing (capstone/main)*, then **Reserve and get code**. The provisioner asks for it at "Arena CLAIM CODE".
- **App code:** Applications, the *Ticketing (capstone)* row, then **App code**. The Ticketing adapter's installer window asks for it.

Each code is shown once, works once, and lasts 72 h (claim) or 24 h (app).

**Ticketing adapter.** The address claim for `capstone/main`, then the existing installer with the app code. When the signed-in user has no Python 3.12, the run offers to install Python 3.12.10 for that user from the stick (APPROVE). It reads `operations.db` read-only. RC.2's Phase 6 experiment and research event kinds are skipped and counted, not shown. Once the adapter is enrolled, F: runs Capstone's Configure again with `-ArenaAdapterRegistered` so that Capstone records the registration.

## 5: Coder laptop (192.0.2.202)

- **Installation stays PENDING:** Coder has no Windows release, Gateway client or Arena publisher yet (O5).
- **Still run:** Inspect, the address claim (once Coder is onboarded), the direct-Ollama refusal test, and, when the Gateway CA is on F:, a TLS check to the Gateway from this laptop.
- **Coder's client file layout isn't declared yet,** so nothing is written for it.

## 6: Existing machine

The machine's role is detected from its address. For the hub, Capstone or Coder,
use their own menu entry.

For a head, pick one or more actions:

| No. | Action | Notes |
|---|---|---|
| 1 | Verify only | Read-only. |
| 2, 3 | Ollama firewall: cutover, or Gateway only | Audit, backup, managed rule, exact undo. Replaces the old `D:\FIX-EXISTING-HEADS` script, which must not be run as it is. |
| 4 | Time sync with the hub, and power | Each with backup and undo. |
| 5 | Ollama boot task with an explicit model directory | Fixes a SYSTEM server that sees no models. Ollama is restarted only on approval, and never while a model is loaded unless you type RESTART. |
| 6 | Post-reboot verification | Did the boot task serve the same models after the reboot? |
| 7 | Install or update models | |
| 8 | Repair or update the Arena viewer | Keeps its credential. |
| 9 | Roll back a previous run | |
| 10 | **Head 3 and head 4 only:** install or update Capstone's business service. CapstoneQMS (quality records) runs on head 3, `192.0.2.3:8611`; CapstoneERP (orders, customers, shipments) runs on head 4, `192.0.2.4:8612` | See below. |

Installed models, local configuration and the Arena enrollment are always kept.

**10: Capstone's QMS or ERP service** (Capstone 6.0.0-rc.4; packages in `packages\services`, pinned by SHA-256 in
`config\lab-network.json` `business_services`). Each part is asked for separately; Ollama, its models and the viewer
are not touched:

| Step | What runs | Gate |
|---|---|---|
| Visual C++ runtime | From the stick, only when its DLLs are missing (the service's installer refuses without it) | APPROVE |
| Release | The service's own `Install-CapstoneQMS.ps1` / `Install-CapstoneERP.ps1` (program in `C:\Program Files\CapstoneQMS`, data in `C:\ProgramData\CapstoneQMS`; ERP likewise). An older release: its own `Update-*.ps1` | APPROVE / UPDATE |
| Token | A new token (64 hex characters) made on the head and given to the service's own `Configure-*.ps1`. The token and the service's client file then go on the provisioner stick (`handoff\qms.token`, `handoff\qms-capstone-client.json`; ERP likewise) for the Capstone laptop, which takes them and deletes them from the stick. Already configured: Enter keeps the token; type NEW-TOKEN only when the Capstone laptop's report says the token was refused | APPROVE |
| Startup task | The service's own `Register-*Task.ps1`: at boot, as LOCAL SERVICE | APPROVE |
| Firewall | One rule, "Agent Arena Lab: CapstoneQMS 8611 from 192.0.2.201": TCP on the head's address, from the Capstone laptop only | APPROVE |

Verify: the approved release; the task running as LOCAL SERVICE; `/api/health` answering; only the Capstone laptop
admitted on the port (rule analysis, other rules that open the port are named); the token still waiting on the stick.

The services speak plain HTTP on the lab cable, so the token keeps other lab machines out. It does not protect
against someone listening on the cable.

**The GPU workstation (192.0.2.2)** is detected and protected. Verify-only is
free; any change needs you to type GPU-WORKSTATION, confirming that a separate,
approved plan for it exists.

## Other entry points on the USB

- **WATCH-LAB.cmd (1.6.29):** a live, read-only window of the machine it runs on, for watching (and showing) the lab at
  work. Double-click it on the hub, the Capstone laptop or a head; Windows asks for administrator rights once (to read
  the service folders). It knows the machine by its lab address. Press Enter to watch; type SHORTCUT (capitals) to also
  put a "Watch the lab" icon on the desktop, which then opens the window without the drive. Ctrl+C or closing the window
  stops it. It changes nothing and does not slow a run.
  - Capstone laptop: every running Testing Lab trial (and cases worked in Capstone's own screens), step by step: intake,
    identity and safety checks, RAG retrieval (every candidate passage, kept or rejected and why), tool and ERP calls, each
    model call with its role, model, head, time and tokens and its full written output (plans and their reasons, reviewer
    decisions, critiques, validator verdicts), rule decisions and each draft reply. A step appears when its model call
    finishes. Models run with thinking off, so there is no private reasoning to show. An answer cut off by its output
    limit is marked CUT OFF.
  - Hub: the Inference Gateway's traffic from its published observation file: each request queued, placed on a head,
    finished or failed; the last hour's failures by model and head; every 20 s a table of the heads.
  - A head: its Ollama at work from Ollama's own log: each model call (chat, generate, embedding), how long it took and
    who asked (the Gateway), model loads, and which models are in memory.
- **COLLECT-LAB-EVIDENCE.cmd (1.5.7):** double-click it on any lab computer when something does not work. Windows asks
  for administrator rights once (on the hub: LabOperator1's password); then it asks nothing. It finds out which computer it
  is from its lab address and saves everything needed to diagnose it to `reports\<computer>\evidence-<date>` on the
  stick, changing nothing on the computer:
  - every computer: network, firewall, listening ports, services, tasks, processes, Windows errors, Smart App Control;
  - the hub: the Inference Gateway's logs, observation and ledger database, the Fleet folder, the Gateway Models
    screen's records, the model library's list, Arena's logs, settings and event database, and every head's Ollama
    (version, models, loaded models) read over the lab cable;
  - the Capstone laptop: Capstone's release, settings, logs and databases, the Research Lab's experiments with each
    run's own database, and (through Capstone's own API, with its developer token used on the laptop and never copied)
    the Research Lab state, results and each recent run's trace;
  - a head: Ollama's logs and settings, its Arena viewer's logs, and QMS or ERP logs on heads 3 and 4.
  - (1.5.8) the hub also measures every head's speed over the lab cable (one cold and one warm answer of qwen3.5:4b,
    the model then unloaded); a head also records a 60-second performance sample, its top processes, page file, power
    plan, temperatures, Windows Defender scanning and exclusions, recent Windows updates, disk health, every Ollama
    setting, how Ollama is started, Ollama's own `server.log`, and the same speed test locally. The heads' speed test
    makes the hub's run take up to 15 minutes more.

  Token, key, password and credential files are never copied, and secret values in copied settings are masked. It
  takes 2 to 10 minutes. It needs the lab cable in; no internet.
- **VERIFY-USB.cmd:** checks every file against `SHA256SUMS.txt` (read-only).
- **`AGENT-ARENA-LAB-1.3.8\RUN-LAB-TESTS.cmd`:** the provisioner's own tests, which change nothing.
- **Dry run without changes:**

  ```
  powershell -File AGENT-ARENA-LAB-1.3.8\engine\Start-LabProvisioner.ps1 -Role <role> -Through Plan
  ```
