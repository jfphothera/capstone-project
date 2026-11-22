# Agent Arena Lab Provisioner

One guided USB for the whole 192.0.2.0/24 lab: inference heads (CPU and GPU), the
administration hub, the Capstone and Coder laptops, and maintenance of existing
machines. Operators read [docs/OPERATOR-GUIDE.md](docs/OPERATOR-GUIDE.md). What is
proven and what still needs the field is in
[docs/FIELD-ACCEPTANCE-CHECKLIST.md](docs/FIELD-ACCEPTANCE-CHECKLIST.md).

## Layout

| Path | What |
|---|---|
| `START-LAB-PROVISIONER.cmd` | The one entry point (elevates, then the role menu) |
| `engine/Start-LabProvisioner.ps1` | Menu and the six stages per role |
| `engine/modules/Lab.*.psm1` | Core (runs, approvals, change journal, redaction); Inspect; Plan; Firewall; Head; Hub (admin host, Arena identity, account migration); Accounts (local accounts, user rights, transitive membership); Gateway (release intake, settings, the Gateway's commands, listener rule, TLS checks, runtimes); Models; Arena; Apps; Report |
| `engine/Invoke-LabGatewayFlow.ps1` | The guided Inference Gateway installation (Administration hub > 2), in the manifest's order |
| `engine/head-engine/` | **Derived** from provisioner 1.0.3 (commit `5574901`): Core, Network, Ollama, Models, Preflight and the model catalog. Two targeted changes, marked `LAB FIX`: the no-internet classification (defect 1) and an optional signed local Ollama installer; the release ships `docs/CHANGES-FROM-1.0.3.diff` |
| `config/lab-network.json` | The fixed architecture: addresses, ranges, protected addresses, Ollama client modes, Arena, Gateway and Capstone paths |
| `packages/` | Other projects' releases, carried unchanged: Capstone 6.0.0-rc.2 (the approved archive, by SHA-256), Inference Gateway 0.3.0, the official Python 3.12.10 and PowerShell 7.4.22 installers (Coder: not supplied) |
| `tests/` | `Invoke-LabTests.ps1` (PowerShell 5.1, the 1.0.3 harness) and `test_lab_release.py` (builder) |
| `build/` | `build_lab_release.py` (release) and `Publish-LabUsb.ps1` (verified USB copy) |

**What is reused unchanged and run as is:**
- the Arena head viewer toolset 2.1.2;
- `AGENT-ARENA-HUB`;
- `AGENT-ARENA-TICKETING`;
- Capstone's own install scripts.

**What replaces something defective:**

| Replaced in 1.0.3 | Replacement | Why |
|---|---|---|
| The firewall rule | `Lab.Firewall` | 1.0.3 admitted only .1 and audited nothing |
| The boot task | `Lab.Head` | 1.0.3 set no `OLLAMA_MODELS` |
| The `OLLAMA_HOST` backup | `Lab.Head` | 1.0.3 overwrote it on every run |
| The failure classification | `Lab.Models` | No internet was labelled as invalid tags |

**What is kept only as evidence:**
- `D:\FIX-EXISTING-HEADS`, in `provisioner/original/`;
- the 1.0.3 originals, in `provisioner/`.

## Build, test, publish

Commands run from the repository root.

1. Run the provisioner tests (Windows PowerShell 5.1):

   ```
   powershell -File lab-provisioner\tests\Invoke-LabTests.ps1 -ResultPath lab-provisioner\docs\test-results\lab-tests.json
   ```

2. Run the release builder tests:

   ```
   .venv\Scripts\python.exe -m pytest lab-provisioner\tests\test_lab_release.py
   ```

3. Build the release into `dist\lab-provisioner-<version>\`:

   ```
   .venv\Scripts\python.exe lab-provisioner\build\build_lab_release.py
   ```

4. Copy it to the USB. Use `-CheckOnly` first; the publisher writes nothing unless every destination check passes:

   ```
   powershell -File lab-provisioner\build\Publish-LabUsb.ps1 -Drive F: -Release dist\lab-provisioner-1.3.8 -ExpectSerial <serial> [-CheckOnly]
   ```
