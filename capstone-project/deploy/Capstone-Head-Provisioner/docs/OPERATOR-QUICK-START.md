# Capstone Head Provisioner — Operator Quick Start

**Version 1.0.0**

---

## What to do on each new computer

1. Connect the computer's **Ethernet port to the isolated Capstone switch**.
2. Make sure it has **Wi-Fi internet** (needed to download Ollama and the models).
3. Copy `Capstone-Head-Provisioner-v1.0.0.zip` to the computer — a USB stick is fine.
4. **Extract it to a real folder**, for example `C:\Capstone-Setup`.
   Do not run anything from inside the ZIP preview window.
5. Double-click **`START-CAPSTONE-SETUP.cmd`**.
6. Approve the **Windows UAC prompt** (once).
7. Answer **one question**: the node number, and the model profile.
8. **Walk away.** Come back later and read the result in the window.

That is the whole procedure. There is nothing to type, no second program
to run afterwards, and no PowerShell commands to copy.

---

## Making it ask nothing at all

If you would rather not answer even that one question, use a preconfigured
launcher from the `launchers\` folder instead of step 5. Right-click it and
choose **Run as administrator**:

```
launchers\SETUP-NODE-4-CPU16.cmd
```

The name says exactly what it does: node 4 (so `192.0.2.4`) with the CPU16
model set. Every combination of node 3–7 and CPU8/CPU16/CPU32 is provided.

**The node number and the model profile are independent.** Node 6 is not
assumed to be an 8 GB machine. Pick the profile from the RAM actually
installed in the computer in front of you:

| Installed RAM | Use      | Download size | Models |
|---------------|----------|---------------|--------|
| about 8 GB    | `CPU8`   | 10.1 GB       | 12     |
| about 16 GB   | `CPU16`  | 26.5 GB       | 21     |
| about 32 GB   | `CPU32`  | 54.3 GB       | 26     |

The GPU workstation at `192.0.2.2` uses `GPU-INSPECT-AND-UPDATE.cmd`, not
these launchers.

---

## How the profile is remembered

The first run saves your choice to
`C:\ProgramData\CapstoneProvisioner\config\capstone.config.json`.
Every later run on that machine reuses it without asking again.

To pre-seed the choice before the first run, copy
`capstone.config.sample.json` to `capstone.config.json` next to
`START-CAPSTONE-SETUP.cmd` and edit the two values.

---

## What the program changes

On the computer you run it on, and nothing else:

- Sets the **Ethernet** adapter to `192.0.2.N`, `255.255.255.0`, **no
  gateway, no IPv4 DNS**. Wi-Fi and IPv6 are not touched.
- Installs **Ollama** from `https://ollama.com` — only if it is not already
  installed. An existing installation is detected and reused as-is.
- Downloads the model set — skipping anything already present.
- Sets `OLLAMA_HOST` so Ollama serves on the private LAN after a reboot.
- Adds **one** Windows Firewall rule: inbound TCP 11434, **from `192.0.2.1`
  only**.

It never disables the firewall, never removes a model, never reinstalls a
working Ollama, and never reconfigures an adapter other than the one wired
to the Capstone switch.

---

## Where the result appears

```
C:\ProgramData\CapstoneProvisioner\reports\LATEST-STATUS.txt
```

Plain English, at the top of the file:

```
RESULT: FULL_SUCCESS
This head is ready for Phase 4 integration.
```

The three possible results:

| Result            | Meaning                                                      |
|-------------------|--------------------------------------------------------------|
| `FULL_SUCCESS`    | Everything worked. Nothing for you to do.                     |
| `PARTIAL_SUCCESS` | Most of it worked. Read **WHAT STILL NEEDS ATTENTION**.       |
| `FAILED`          | Something important did not work. Same section explains what. |

The **WHAT STILL NEEDS ATTENTION** section is the only part you normally
need to read. If it says "Nothing", you are done.

Supporting files, all under `C:\ProgramData\CapstoneProvisioner\`:

| Folder         | What is in it                                     |
|----------------|---------------------------------------------------|
| `reports\`     | The summary above, plus one per run               |
| `logs\`        | The full transcript of each run                   |
| `diagnostics\` | What the machine looked like before any change    |
| `backups\`     | The network settings, for rollback                |
| `inventory\`   | The machine-readable head record for Phase 4      |
| `state\`       | Resume point and the model queue                  |

---

## Checking every head from one keyboard

On the **coordinator** (`192.0.2.1`), double-click:

```
COORDINATOR-CHECK-ALL-HEADS.cmd
```

It queries `192.0.2.2` through `192.0.2.7`, lists what models each head
has, and asks one model on each head for a short reply — proving real
remote inference end to end. It needs no administrator rights, changes
nothing, and means you never have to move a keyboard to a head to find
out whether it is working.

Note: a head's own report says
`REMOTE_COORDINATOR_VERIFICATION_PENDING` until this check is run. That is
correct and expected — a head genuinely cannot observe whether the
coordinator reached it, so it does not claim that it did.

---

## If something goes wrong

**Run it again.** The program is built to be re-run. It reuses the existing
Ollama, keeps every model already downloaded, resumes incomplete ones, and
leaves a working network configuration alone. A second run cannot undo a
successful first run.

| Situation | What to do |
|-----------|------------|
| Want to check without changing anything | `VERIFY-ONLY.cmd` |
| Want a full machine report, no changes   | `DIAGNOSTICS-READ-ONLY.cmd` |
| Network needs to go back as it was       | `RECOVER-NETWORK-ROLLBACK.cmd` |
| Report says `AMBIGUOUS_NETWORK_ADAPTER`  | The machine has two wired ports. Put the switch-side MAC in `ethernet_mac` in the config file, then re-run. |
| Report says `ETHERNET_IS_SOLE_INTERNET_PATH` | The Ethernet port is the only internet. Connect Wi-Fi first, then re-run. |
| Report says `DUPLICATE_IP_OR_NETWORK_CONFLICT` | Another device already has that address. Pick a different node number. |
| Report names a failed model tag          | Re-run; transient registry errors usually clear. Everything else is kept. |

`RECOVER-NETWORK-ROLLBACK.cmd` restores the adapter exactly as it was. A
port that was on DHCP goes back to DHCP — its old lease address is **not**
re-applied as a static address. Ollama and the downloaded models are not
touched by a rollback.

---

## How long it takes

| Stage | Time |
|-------|------|
| Discovery and network | under a minute |
| Installing Ollama (first time only) | 5–15 min, 1.6 GB download |
| Models, CPU8 | roughly 30–60 min |
| Models, CPU16 | roughly 1–3 hours |
| Models, CPU32 | roughly 2–6 hours |

Mostly download time. The window reports progress continuously and
reports a heartbeat even during a long single layer, so it never looks
frozen. You can leave several machines running at once.

---

## Disk space

Each head needs the profile's download size plus a **10 GB reserve**, plus
about 2 GB for the Ollama installation itself. The program measures free
space before it starts and stops safely if there is not enough — it never
deletes an existing model to make room.
