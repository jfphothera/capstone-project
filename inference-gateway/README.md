# Inference Gateway 0.4.6

The Inference Gateway is the shared model-access service for the capstone
system. Applications such as Capstone never call a language model directly.
They send every request (chat, generation, embeddings) to the Gateway, which:

- **authenticates** each application with its own token and only allows the
  models and operations its policy grants;
- **admits and queues** requests fairly, with per-application concurrency,
  queue limits and priorities;
- **places** each request on one of several lab machines ("heads") running
  Ollama, choosing the least busy qualified head that has the memory for the
  model;
- **qualifies** every model on every head before use (it really runs the model
  and records the result), and keeps that qualification tied to the head's
  Ollama version and the model's digest;
- **contains failures:** a 4xx fails only that request, a 5xx suspends only that
  model on that head, an unreachable head goes offline until its heartbeat
  returns, and a request is never replayed after it may have run;
- **publishes a sanitized observation file** every 5 seconds, which Agent Arena
  reads to show the fleet live.

It is one of the three parts of the capstone system, alongside
[capstone-project](../capstone-project/README.md) (the agent application) and
[agent-arena](../agent-arena/README.md) (live visualisation).

It implements the `shared.inference/1` contract. The runtime is the **Python 3.12
standard library only**: no third-party packages.

---

## Architecture

```
 Capstone (operational)   Capstone Testing Lab (research)    other apps
          \                        |                          /
           +------------  HTTPS + bearer token  -------------+
                                   |
                     Inference Gateway (this project)
        admission & queues | placement | qualification | failure policy
                 SQLite ledger (WAL)  |  observation publisher --> Agent Arena
                                   |
          +-----------+------------+------------+-----------+
       head 2      head 3       head 4   ...  head N        (Ollama on lab PCs)
```

| Path | Contents |
|---|---|
| `src/inference_gateway/capacity_gateway_server.py` | HTTP(S) service: `/health`, `/ready`, `/v1/submit`, `/v1/models`, `/v1/policy`, `/v1/availability`, `/v1/fleet`, `/v1/usage` |
| `src/inference_gateway/capacity_broker.py` | Admission, queues, leases and the ledger |
| `src/inference_gateway/placement.py`, `model_capacity.py` | Head selection and per-model capacity |
| `src/inference_gateway/capacity_admin.py` | Model qualification (`inference-qualify`) |
| `src/inference_gateway/recovery.py`, `capacity_lifecycle.py` | Failure classes, suspensions, probes and operator commands |
| `src/inference_gateway/credentials.py` | Issue, rotate and revoke application tokens |
| `src/inference_gateway/observation_publisher.py` | The sanitized fleet observation Agent Arena reads |
| `src/inference_gateway/windows_service.py`, `deployment.py` | Windows service and versioned installer (plan, install, verify, update, rollback) |
| `src/shared_inference_client.py` | The standard-library client SDK that applications (Capstone) use |
| `config/` | Policy and client templates; `policy.lab-example.json` shows a nine-head lab |
| `deploy/windows/` | Installer entry point, deployment manifest and operator checklist |
| `scripts/` | Development setup, certificates, release build, and the load/contention harnesses |
| `lab-tools/` | Capacity, launch-storm and failure-reproduction tools run on the hub |
| `docs/` | API and client contracts, deployment guide, failure and placement rules, release notes |
| `verification/` | Measured results: concurrency, ledger contention, lifecycle and control-plane load |
| `tests/` | Automated test suite (about 230 tests) |

---

## Setup

Requirements: Windows 10/11 and **Python 3.12**. Ollama is needed on at least
one machine to serve models.

```powershell
.\scripts\Setup-Development.ps1
```

This creates `.venv`, installs the pinned test tools from
`requirements-dev.lock`, installs the Gateway in editable mode and runs the
test suite. To run the tests again:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The tests start in-process Gateways with stand-in heads. They need no Ollama,
network or certificates.

---

## Run it on one machine

This runs the Gateway on loopback in front of a local Ollama (plain HTTP is
accepted only on loopback). Run the commands from the project folder with the
`.venv` from Setup activated. Keep `var\` (ledger and tokens) private; it's
ignored by git.

1. **Models.** Install Ollama and pull the models Capstone uses:

   ```powershell
   ollama pull qwen3.5:4b
   ```

   ```powershell
   ollama pull all-minilm
   ```

2. **Policy.** Create `var\policy.json`, starting from `config\policy.template.json`:
   list the applications (`capstone`, `capstone-research`) with the models they
   may use (`qwen3.5:4b`, `all-minilm:latest`), and one resource (head) with
   `"resource_id": "head-127-0-0-1"`, `"endpoint": "http://127.0.0.1:11434"`,
   `"applications": ["capstone", "capstone-research"]`,
   `"permitted_uses": ["inference"]`, `"slots": 1` and its `memory_gb`.
   `config\policy.lab-example.json` shows the exact resource format.

3. **Tokens first, then the policy.** The Gateway refuses a policy for an
   application that has no credential yet:

   ```powershell
   python -m inference_gateway.credentials issue --database var\gateway.db --app-id capstone --output var\capstone.token --policy var\policy.json
   python -m inference_gateway.credentials issue --database var\gateway.db --app-id capstone-research --output var\capstone-research.token --policy var\policy.json
   python -m inference_gateway.capacity_bootstrap --database var\gateway.db --policy var\policy.json
   ```

4. **Qualify each model on the head.** Nothing is placed on a head until its
   models pass:

   ```powershell
   python -m inference_gateway.capacity_admin --database var\gateway.db --resource head-127-0-0-1 --model qwen3.5:4b --operator ops-1 --purpose completion --output var\qualify-qwen.json
   python -m inference_gateway.capacity_admin --database var\gateway.db --resource head-127-0-0-1 --model all-minilm:latest --operator ops-1 --purpose embedding --output var\qualify-minilm.json
   ```

5. **Start the Gateway:**

   ```powershell
   python -m inference_gateway.capacity_gateway_server --database var\gateway.db --host 127.0.0.1 --port 8443
   ```

   It prints `{"event": "service.listening", ...}`. Check
   `http://127.0.0.1:8443/health`.

6. **Point Capstone at it.** Write a client file for each application, based on
   `config\client.template.json`, with `"url": "http://127.0.0.1:8443"`, the
   `application_id` and the absolute path of its `token_file`. Leave out
   `ca_file` on loopback. Then set `CAPSTONE_GATEWAY_CLIENT_CONFIG` and
   `CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG` as described in
   the [capstone-project README](../capstone-project/README.md#run-the-application).

For a quick look with no heads at all, `.\scripts\Run-Development.ps1 -Port 0`
starts a Gateway with an empty development policy on a free loopback port.

## Run it across a lab (multiple heads)

On the hub machine the Gateway runs as a Windows service with TLS from its own
lab CA. Build the release package and follow the operator checklist:

```powershell
python scripts\build_release.py
```

The package's installer is
`python -I bin\gateway-deploy.py <plan|certificate|install|verify|update|rollback|repair|uninstall> --settings deployment-settings.json`,
with settings based on `config\deployment-settings.template.json`. See
`deploy/windows/OPERATOR-CHECKLIST.md`, `docs/DEPLOYMENT.md` and
`deploy/windows/DEPLOYMENT-CONTRACT.md`. The API is documented in
`docs/CONTRACT.md` and `docs/CLIENT_CONTRACT.md`.

---

## Evaluation

`verification/` holds measurements from the lab and from the load harnesses in
`scripts/`: 16–32 parallel flows (`concurrency-*.json`), SQLite ledger
contention (`ledger-*.json`), request lifecycle timing (`lifecycle-0.4.4.json`)
and control-plane read load (`control-plane-*.json`). The release notes in
`docs/RELEASE-NOTES-0.4.1.md` to `0.4.6.md` record what each lab run found and
what was changed.

## Limitations

- Placement and qualification are tuned for a small lab (about nine heads); it
  isn't a general-purpose cluster scheduler.
- Remote clients need HTTPS with the Gateway's own CA; plain HTTP works only on
  loopback.
- Lab network addresses in configuration, scripts and recorded results are
  replaced with documentation addresses (`192.0.2.x`, RFC 5737), and machine
  and user names are redacted. Use your own addresses when deploying.
