# Capstone Head Provisioner 1.0.0

Unattended Windows provisioning for Capstone inference heads: private LAN
address, Ollama, local models, remote serving, and verification — in one run.

---

## Start here

**On a new head:** extract this folder somewhere real (for example
`C:\Capstone-Setup`), then double-click:

```
START-CAPSTONE-SETUP.cmd
```

Approve the Windows UAC prompt, answer one question, and walk away.

**On the coordinator (`192.0.2.1`):**

```
COORDINATOR-CHECK-ALL-HEADS.cmd
```

Read-only. Checks every head from one keyboard.

New to this? Read [`docs/OPERATOR-QUICK-START.md`](docs/OPERATOR-QUICK-START.md)
— one page, everything you need.

---

## What is in here

| Item | Purpose |
|---|---|
| `START-CAPSTONE-SETUP.cmd` | **The main launcher.** Everything else routes through it. |
| `launchers\SETUP-NODE-<N>-<PROFILE>.cmd` | Fully unattended: no questions at all. Every node 3–7 × CPU8/CPU16/CPU32. |
| `VERIFY-ONLY.cmd` | Check everything, change nothing. |
| `DIAGNOSTICS-READ-ONLY.cmd` | Full machine report, no changes. |
| `RECOVER-NETWORK-ROLLBACK.cmd` | Put the network back as it was. |
| `GPU-INSPECT-AND-UPDATE.cmd` | For the existing GPU workstation. Additive only. |
| `COORDINATOR-CHECK-ALL-HEADS.cmd` | Verify every head from the coordinator. |
| `RUN-ALL-TESTS.cmd` | Re-run all 401 checks on this machine. Non-destructive. |
| `capstone.config.sample.json` | Optional: pre-seed node and profile for a zero-question run. |
| `engine\` | The single deployment engine and its seven modules. |
| `engine\catalog\model-catalog.json` | 28 registry-validated tags with measured sizes and digests. |
| `coordinator\` | The coordinator-side verification utility. |
| `tests\` | Static, unit and integration suites. |
| `docs\` | Operator sheet, technical reference, defect matrix, model manifest, security notes, acceptance report. |
| `samples\` | Real example reports: success, partial, failed, resumed. |

---

## Documentation

| Document | Read it for |
|---|---|
| [`OPERATOR-QUICK-START.md`](docs/OPERATOR-QUICK-START.md) | What to do, on one page |
| [`ACCEPTANCE-REPORT.md`](docs/ACCEPTANCE-REPORT.md) | **What is proven and what is not**, plus first-run risks |
| [`DEFECT-MATRIX.md`](docs/DEFECT-MATRIX.md) | Why the previous packages failed and what changed |
| [`TECHNICAL-REFERENCE.md`](docs/TECHNICAL-REFERENCE.md) | Architecture, state machine, policies, exit codes |
| [`MODEL-MANIFEST.md`](docs/MODEL-MANIFEST.md) | Every tag, size, role and profile |
| [`SECURITY-NOTES.md`](docs/SECURITY-NOTES.md) | The trust boundary, honestly, including its limits |

---

## The two configuration dimensions

They are independent. A node number is a physical computer; it says nothing
about that computer's RAM.

```
Node number  ->  3 4 5 6 7        ->  192.0.2.N
Profile      ->  CPU8 CPU16 CPU32 GPU EMBEDDINGS
```

Pick the profile from the RAM actually installed in the machine:

| Installed RAM | Profile | Download | Models |
|---|---|---|---|
| ~8 GB | `CPU8` | 10.1 GB | 12 |
| ~16 GB | `CPU16` | 26.5 GB | 21 |
| ~32 GB | `CPU32` | 54.3 GB | 26 |

---

## Re-running is safe

It is the supported recovery path. A second run reuses the installed Ollama,
keeps every downloaded model, resumes incomplete ones, leaves a working network
configuration alone, and converges the firewall on exactly one rule. It cannot
undo a successful first run.

---

## Where results go

```
C:\ProgramData\CapstoneProvisioner\reports\LATEST-STATUS.txt
```

Plain English. The section headed **WHAT STILL NEEDS ATTENTION** is the only
part you normally need to read.

---

## Requirements

- Windows 10 build 10240 or later; Windows 11 expected. 64-bit.
- Windows PowerShell 5.1 — in-box, nothing to install.
- Administrator rights, requested once via UAC.
- Ethernet to the isolated Capstone switch.
- Wi-Fi internet, for the Ollama and model downloads.
- Free disk: the profile size plus a 10 GB reserve, plus ~2 GB for Ollama.

---

## Before trusting it on a machine that matters

Read [`docs/ACCEPTANCE-REPORT.md`](docs/ACCEPTANCE-REPORT.md) section 5,
"First-run risks". Eight behaviours could not be executed during development
without modifying the coordinator or the working GPU workstation; each is named,
with its mitigation and a recommended first-run order. Section 6 recommends
starting with node `192.0.2.4`, which is already on the LAN with Ollama running
and no models — so the first real test skips the highest-risk stage entirely.
