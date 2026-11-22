# Agent Arena

Agent Arena is a live view of AI agents at work across a small inference cluster.
It shows the agents of the **Capstone** application ("Ticketing" in the Arena) as
animated robots. They wait in their orchestrator's room, walk to the inference head
that runs their model call, work there, and walk back. The admin hub also shows what
the **Inference Gateway** reports about the heads: health, leases, qualified models
and failures.

Agent Arena only observes. It never schedules work, runs inference, or changes how an
application behaves.

It is one of the three parts of the capstone system, alongside
[capstone-project](../capstone-project/README.md) (the agent application) and
[inference-gateway](../inference-gateway/README.md) (shared model access).

This folder is the submission copy of the current version:

| Part | Version | Where |
|---|---|---|
| Arena Event Service (Python) | 2.5.1 | `arena_service/__init__.py` |
| Agent Arena desktop app (Windows) | 2.3.0 | `arena_service/hub_usb.py` (`DESKTOP_VERSION`) |
| Lab provisioner (USB setup for the lab machines) | 1.6.33 | `lab-provisioner/VERSION` |

## Architecture

```
 Capstone (Ticketing)                       Inference Gateway
   operations.db: arena_events table          publishes shared.inference.observation/1
        │  read-only (SQLite mode=ro)            to a JSON file (atomic replace)
        ▼                                           │
 Ticketing adapter ──── arena.v2 events ──►  Arena Event Service  ◄── read by ── Admin console
 (arena_service/adapters/capstone)  signed    (arena_service, :8790)             (arena_service/admin_console.py,
                                                  │  snapshot + live deltas,       127.0.0.1:8791, token-protected;
                                                  │  signed                        adapters/gateway/observation.py)
                                                  ▼
                       Agent Arena desktop app on each head and orchestrator display
                       (desktop/ = C# WinForms + WebView2, renders frontend/)
```

| Path | What it is |
|---|---|
| `arena_service/` | The event service: the arena.v2 contract (`contract.py`), the signed HTTP API (`server.py`), the SQLite state machine for locations and assignments (`store.py`), the local admin CLI (`admin.py`), the admin console (`admin_console.py`) and installers for the hub and the Ticketing adapter. **Python standard library only.** |
| `arena_service/adapters/capstone/` | Ticketing adapter: tails Capstone's `arena_events` table and publishes arena.v2 |
| `arena_service/adapters/gateway/` | Reads and summarizes the Inference Gateway's observation file for the admin console |
| `arena_service/clients/synthetic_coder.py` | A labelled synthetic second producer ("Coder (SYNTHETIC TEST)") for multi-application tests |
| `arena_service/devbridge.py`, `visual_harness.py` | Workstation tools: serve the viewer with a signed read-only bridge to a local service, and run a full live demo |
| `frontend/` | The viewer: `agent_arena_desktop.html` (cluster and location views), `arena-room.html` (one live room), `admin.html` and `admin-room.html` (admin hub). Plain ES modules, no build step. `frontend/assets/` holds the room and character art the pages load. |
| `desktop/` | Windows app (C# on .NET Framework 4.8 with WebView2): source, build script, installer launchers, USB tools for the heads, and its Windows tests |
| `coordinator/` | Double-click launchers: set up and start the service, start the Ticketing adapter, enroll a display, prepare the hub and Ticketing USB folders |
| `lab-provisioner/` | The guided USB provisioner for the lab: inference heads (Ollama), the admin hub, the Capstone and Coder laptops. PowerShell engine and modules, config, release builder, tests |
| `provisioner/engine/` | The earlier head provisioner (1.0.3). The lab release builder diffs its derived copy in `lab-provisioner/engine/head-engine` against this. |
| `tools/` | `serve_frontend.py` (static preview server), `room_assets.mjs` (lists every asset the live rooms load), admin-console and fleet previews, `analyze_lab_evidence.py` |

### How the event service works

- **Producers publish.** Each application has its own producer credential. It POSTs arena.v2
  events to `/v2/events`, such as `agent.registered`, `assignment.created`,
  `assignment.moved`, `assignment.state` and `handoff.sent`. The service validates every event
  against `contract.py`. It takes the application identity from the credential, never from the
  payload, so namespaces stay isolated.
- **Displays read.** Each display (a head or an orchestrator screen) is enrolled with a
  one-time code bound to its address. It then reads `/v2/snapshot` and long-polls `/v2/events`
  for deltas. A snapshot is applied without animation and only newer deltas animate, so history
  is never replayed as live work.
- **Location rules.** An agent belongs to one orchestrator, which is its home. Each assignment
  is at exactly one location at a time and moves only through explicit `assignment.moved`
  events. A head view shows only the work currently on that head.
- **Signing.** Requests are HMAC-signed and replay-protected (nonce window and clock-skew
  check). Responses are signed too, so a display can tell the real service from an impostor.
  Traffic is **not encrypted**. It is meant for an isolated lab LAN.
- **Administration is local.** There is no network admin API. Administration happens on the
  service host, through the CLI (`python -m arena_service.admin`) or the admin console, which
  listens on 127.0.0.1 only and needs the token it prints at start.

### Connection to Capstone (`arena_service/adapters/capstone`)

Capstone writes its own agent activity to an `arena_events` table (schema 1.0) in its SQLite
database, `data/operations.db`. The adapter:

- opens that database **read-only** (`mode=ro`) and never writes to Capstone;
- translates each row into arena.v2 in `translate.py`. The translation is deterministic, so a
  restart re-publishes the same event IDs and the service ignores the duplicates. Workarounds
  for known gaps in Capstone's instrumentation are named in each event's `source_ref`;
- keeps a durable checkpoint file, publishes in batches of up to 100, and heartbeats only while
  Capstone's health URL answers (default `http://127.0.0.1:8000/api/health`).

Its configuration (written by `setup_coordinator.py` as `ticketing-adapter.json`) has these
keys: `capstone_db`, `aes_url`, `credential_file` (a DPAPI-protected producer credential),
`checkpoint_file`, `capstone_health_url`, `start_from`, `heartbeat_seconds`, `poll_seconds`
and `log_path`. To run it:

```powershell
py -3.12 -E -s -m arena_service.adapters.capstone --config <path>\ticketing-adapter.json
```

The service never imports Capstone code, and a test checks this.

### Connection to the Inference Gateway (`arena_service/adapters/gateway`)

The Gateway's operator runs the Gateway's `inference-observe` exporter and publishes its output,
a `shared.inference.observation/1` document, by atomic file replacement. Arena reads only that
file. `observation.py` checks the contract name and a 4 MB size limit, and copies every record
through an allowlist, so a bad record is dropped rather than guessed at. The admin console then
shows heads, leases, qualified models and recent failures in the Gateway's own terms. To turn it
on, add this to the service configuration that the admin console reads:

```json
"gateway": {"observation_file": "C:\\path\\to\\observation.json", "gateway_id": "gateway"}
```

A Gateway resource is placed on an Arena head when its ID matches the Arena head ID
(`head-192-0-2-N`), or through an optional `resource_heads` mapping.

## Setup

- **Python 3.12** for the event service, the adapters and the tests. The service itself needs
  only the standard library.
- **pytest 9** for the tests:

  ```powershell
  py -3.12 -m venv .venv
  ```
  ```powershell
  .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  ```

- **Node.js** (optional): the frontend tests run `node`. They are skipped if Node is not installed.
- **Windows** for the desktop app, the coordinator `.cmd` launchers, DPAPI credential storage
  and the lab provisioner (Windows PowerShell 5.1).

## Run it

### 1. Live demo on one workstation (recommended)

This starts a real event service on a temporary database, the real Ticketing adapter replaying
a recorded Capstone trace (`arena_service/tests/fixtures/distributed_case_trace.json`), the
synthetic Coder producer, the dev bridge, and (with `--admin-port`) the admin console fed by a
recorded Gateway observation. Everything binds to 127.0.0.1.

```powershell
.venv\Scripts\python.exe -E -s -m arena_service.visual_harness --port 18791 --admin-port 18798
```

- Viewer: <http://127.0.0.1:18791/>. It opens as display `head-192-0-2-3`. Use
  `--display orch-capstone` for the Ticketing orchestrator room.
- Admin console: <http://127.0.0.1:18798/#token=harness>
- The event service runs on port 18790 (`--service-port`). Add `--outage-every 35 --outage-for 15`
  to kill and restart it on a timer and watch the viewer reconnect.

### 2. Static preview of the pages

```powershell
.venv\Scripts\python.exe tools\serve_frontend.py 18794
```

This serves `frontend/` on <http://127.0.0.1:18794/> with caching off. With no bridge behind
it, the viewer pages have no live data. <http://127.0.0.1:18794/agent_arena_desktop.html?demo=1>
runs the viewer's built-in demo script.

### 3. The event service on its own

Write a configuration file, for example `aes.json`:

```json
{"host": "127.0.0.1", "port": 8790, "db_path": "C:\\ArenaData\\arena-events.db", "log_path": "C:\\ArenaData\\aes.log"}
```

```powershell
.venv\Scripts\python.exe -I -m arena_service --config aes.json
```

`GET http://127.0.0.1:8790/v2/health` answers without a credential. Register applications,
heads and displays with the admin CLI (`app-add`, `orchestrator-add`, `producer-issue`,
`head-add`, `orchestrator-display-add`, `enroll`, `list` and others):

```powershell
.venv\Scripts\python.exe -m arena_service.admin --help
```

On the coordinator machine, `coordinator\SETUP-ARENA-EVENT-SERVICE.cmd` does this setup in one
step. It registers Ticketing and heads .2 to .10 and stores the Ticketing credential with DPAPI.
`START-ARENA-EVENT-SERVICE.cmd` and `START-TICKETING-ADAPTER.cmd` then run the two processes.

### 4. The admin console

```powershell
.venv\Scripts\python.exe -I -m arena_service.admin_console --config aes.json --port 8791
```

It opens a browser on `http://127.0.0.1:8791/` with a one-time token in the URL.

### 5. Build the Windows desktop app

The build uses the .NET Framework compiler that ships with Windows (`csc.exe`) and the Microsoft
WebView2 SDK. The WebView2 binaries are **not included** in this repository. Get them from the
NuGet package **Microsoft.Web.WebView2 1.0.4314.35**
(<https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4314.35>). A `.nupkg` file is a zip
archive. Copy these four files from it into `desktop\third_party\webview2\1.0.4314.35\`:

| File in the package | SHA-256 of the file used for this build |
|---|---|
| `lib\net462\Microsoft.Web.WebView2.Core.dll` | `acdd517b951829f763d1255f32df1e6a936cb8623a7ebff6b8b24172dede6bd6` |
| `lib\net462\Microsoft.Web.WebView2.WinForms.dll` | `c66437f80886aecf074804b61af4cc10ad90a95310c8e0a717f31ea377c26b03` |
| `runtimes\win-x64\native\WebView2Loader.dll` | `d3fce90bdeecb08596e377dea6a316354dd8949e32ff09b89d51be00c14e21b6` |
| `LICENSE.txt` | `0af8f1b807512aae39c2ac1aa4d0cae65cabecb6fd554b8439a5162a0d6eca55` |

The build script refuses DLLs that are not validly signed by Microsoft. Then:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\build\Build-AgentArena.ps1 -Version 2.3.0
```

It writes `dist\Agent-Arena-2.3.0\` and its `.zip`, with a SHA-256 manifest. The package carries
only the viewer pages, their scripts and styles, and exactly the room assets listed in
`frontend\assets\room-assets.txt`. If you change rooms or art kits, regenerate that list with
`node tools\room_assets.mjs`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```

`pytest.ini` collects `arena_service/tests`, `desktop/tests` and `lab-provisioner/tests/python`.
On this copy (Windows 11, Python 3.12.10, Node 26, no `dist\` package built), all tests pass
except for skips:

- `desktop/tests/test_desktop_windows.py` runs the built `AgentArena.exe` and is skipped
  until the desktop package is built.
- The hub USB tests in `arena_service/tests/test_hub_install.py` copy the admin window from the
  built desktop package and are skipped until it is built.
- Three lab-provisioner tests need the Inference Gateway's release folder (set `GATEWAY_RELEASE`).

The lab provisioner's own PowerShell suite (Windows PowerShell 5.1) uses stand-ins for the
firewall, Ollama and the network, and changes nothing on the machine:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File lab-provisioner\tests\Invoke-LabTests.ps1
```

On this copy, 240 of its 242 checks pass. The other two need things that are not in this
repository: the Inference Gateway's release archive, and the git history of the original
repository (one check compares the 1.0.3 engine with a specific commit).

## Limitations

- **Addresses are redacted.** The real lab subnet was replaced with the documentation range
  `192.0.2.0/24` (hub `192.0.2.254`, heads `.2` to `.199`, application laptops `.200` to `.249`).
  The address plan is fixed in code (`hub_install.py`, `admin_console.py`, `store.py`,
  `fleet.py`) and in `lab-provisioner/config/lab-network.json`, so a real deployment must change
  it in those places. Machine names, accounts, MAC addresses and drive serials in the tests and
  recorded lab evidence were also replaced with placeholders.
- **Windows-centric.** The desktop app, the DPAPI credential storage, the coordinator launchers
  and the lab provisioner are Windows-only. The event service and the viewer pages run anywhere
  Python 3.12 and a modern browser do.
- **Not encrypted.** Traffic is signed, not encrypted. The design assumes an isolated lab LAN.
- **Builds that need outside inputs.** The hub USB folder (`arena_service/hub_usb.py`) needs the
  built desktop package. The lab release (`lab-provisioner/build/build_lab_release.py`) also
  needs the other projects' release archives (Capstone, the Inference Gateway, the pinned Ollama
  and Python installers). They are looked for in sibling folders (`..\Capstone`,
  `..\Inference Gateway`) and are not included here.
- **Art sources are not included.** `frontend/assets` holds the finished art the pages load. The
  source art, previews and the scripts that built the art kits are not part of this copy.
- **Observation only.** The Arena shows what the applications and the Gateway report. It cannot
  show work they do not report. The Capstone adapter's workarounds for gaps in Capstone's
  instrumentation are labelled in `source_ref`.
- Some code comments and admin-console messages point to design notes and hand-off documents
  that are not part of this copy.
