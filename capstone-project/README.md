# Capstone Project: Multi-Agent Customer Service for a Regulated Medical-Device Maker

Version **6.0.0-rc.29** (the submission freeze).

Capstone is a multi-agent customer-service system for Phothera, a maker of home
phototherapy medical devices. A customer writes in about a device. The system
works out who the customer is and which device and hardware revision they own.
It finds the controlled quality procedure (QMS) that applies to that exact
revision, screens for safety complaints, drafts a reply, checks the reply against
its source in code, and routes anything risky to a named human reviewer.

The system has three parts, each in its own folder of this repository:

| Folder | Role |
|---|---|
| **capstone-project** (this one) | The multi-agent application, its Testing Lab, the ERP/QMS business services and the lab deployment scripts |
| [inference-gateway](../inference-gateway/README.md) | Authenticated, queued access to the local models running on the lab machines; every model call goes through it |
| [agent-arena](../agent-arena/README.md) | Live visualisation of the agents and the Gateway's model fleet, built from telemetry |

All customer, device, order, ticket and QMS data in this repository is
**synthetic**. No real customer or company data is included.

---

## The problem and the users

- **Problem:** in a regulated device company, a support answer is only correct if
  it matches the customer's exact model **and hardware revision**. Two revisions
  can need opposite instructions. Possible safety complaints also have to be
  caught and escalated, never answered automatically.
- **Users:** customer-service representatives and quality reviewers (the
  representative workspace), and device owners (the customer chat).
- **Goal:** answer routine questions from verified sources, and refuse or
  escalate whenever the evidence isn't good enough. "I don't know, a person will
  follow up" is a correct outcome.

---

## Architecture

```
 Customer chat / Representative workspace  (FastAPI + static web UI)
                     |
                Coordinator  (LangGraph state machine, SQLite checkpoints)
                     |
  +---------+--------+--------+----------+-----------+-------------+
  |         |        |        |          |           |             |
Interpret  ERP    QMS      Safety    Resolution  Communication  Independent
 ticket  research research complaint  planning    review/fix     validation
            |        |                                              |
       ERP service  QMS service  (authenticated business services, MCP tools)
                     |
       Deterministic gates: revision validation, grounding verifier,
       safety screen, named human approval, reviewed memory
                     |
        Inference Gateway  (shared.inference/1)  -->  model "heads" (Ollama)
                     |
        Agent Arena  (live visualisation of agent telemetry)
```

**Workflow agents** (`src/capstone_rag/telemetry.py`): Coordinator, Ticket
Interpretation, ERP Research, QMS Research, Safety/Complaint, Documentation,
Resolution Planning, Operations, Communication Review, Independent Validation,
Communication Correction, Reviewed Learning.

**What is a model and what is code.** Language models draft and interpret.
Every decision that matters is deterministic code: device identification, the
revision-applicability check, the safety screen, character-level checks of serial
numbers and document IDs in a reply, and the approval gate. No model is asked to
judge its own output.

**Inference.** All model calls (chat models such as `qwen3.5:4b` and the
`all-minilm` embedding model) go through the **Inference Gateway**, which spreads
requests across several lab machines ("heads") running Ollama. The Gateway is in
the [inference-gateway](../inference-gateway/README.md) folder.

**Testing Lab.** A built-in evaluation harness (`/pressure-testing` in the web app) runs
scripted and agentic test campaigns on isolated copies of the data. It uses a
simulated customer, a simulated environment and a simulated human reviewer, and
scores the system under test against rubrics. Its own agents are tagged
`system_under_test: false`, so they're never confused with the workflow.

### Key folders

| Path | Contents |
|---|---|
| `run_app.py` | Starts the web application (workspace, chat, activity trace, API, Testing Lab) |
| `src/capstone_rag/` | The application: agents, orchestration, retrieval, validation, safety, Testing Lab |
| `src/capstone_rag/webapp/` | FastAPI app, API routers and the static web UI |
| `src/shared_inference_client.py` | Standard-library client for the Inference Gateway contract |
| `services/service_host.py` | Small authenticated ERP / QMS business service (runs on a lab head) |
| `data/qms/` | Synthetic controlled QMS documents (Markdown with front matter; read by the app) |
| `data/erp/` | Synthetic ERP records: customers, devices, orders, shipments, demo scenarios |
| `data/qms_compatibility.json` | Which procedures are authorised for each model and revision |
| `data/revision_register.json` | Serial number to hardware revision register |
| `data/evaluation/`, `data/phase1/` | Evaluation scenarios and challenge cases |
| `data/seed/operations.db` | Clean synthetic seed database (source tables only, no cases) |
| `config/*.template.json` | Inference Gateway client configuration templates (no secrets) |
| `tests/` | Automated test suite (986 tests) |
| `evaluation/experiments/` | Aggregate results of the three final Testing Lab campaigns (2,000 trials) |
| `scripts/`, `tools/` | Dataset builders, evaluation, benchmark and acceptance tooling |
| `deploy/windows/` | Windows release, install, health-check and rollback scripts |
| `deploy/services/` | Packaging for the ERP and QMS business services on the lab heads |
| `deploy/Capstone-Head-Provisioner/` | Provisioning and verification of the multi-machine model "heads" |

---

## Setup

Requirements: Windows 10/11 (the deployment scripts are PowerShell; the Python
code also runs elsewhere) and **Python 3.12**.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`requirements.runtime.lock` pins the exact versions used for the release.

### Run the tests (no Gateway or model needed)

```powershell
python -m pytest -q
```

The suite uses stand-in models and temporary databases. It doesn't need the
Inference Gateway, Ollama or a network.

### Run the application

The application needs a reachable Inference Gateway, because every model call,
including embeddings, goes through it. It won't start without one, and it
doesn't silently switch to another model source.

1. Start the Inference Gateway (see [its README](../inference-gateway/README.md#run-it-on-one-machine)) with at least one
   head serving `qwen3.5:4b` and `all-minilm`.
2. Copy `config/inference-gateway.client.template.json` to a private location
   and fill in the Gateway URL, the client token file and the Gateway CA file.
   Do the same with the research template for the Testing Lab identity.
3. Point Capstone at the two files and create the starting database:

   ```powershell
   $env:CAPSTONE_GATEWAY_CLIENT_CONFIG = "C:\path\to\inference-gateway.client.json"
   $env:CAPSTONE_RESEARCH_GATEWAY_CLIENT_CONFIG = "C:\path\to\inference-gateway.research-client.json"
   Copy-Item data\seed\operations.db data\operations.db
   ```

4. Start it:

   ```powershell
   python run_app.py
   ```

| URL | What it is |
|---|---|
| `http://127.0.0.1:8000/` | Representative workspace (inbox, case details, approvals) |
| `http://127.0.0.1:8000/chat` | Customer chat |
| `http://127.0.0.1:8000/trace` | Live agent activity and QMS inventory |
| `http://127.0.0.1:8000/api/docs` | API reference |
| `http://127.0.0.1:8000/testing-lab-login` | Testing Lab sign-in (opens `/pressure-testing`) |

Synthetic tickets arrive in the workspace from the built-in arrival simulator.
You can also write one yourself in the customer chat. The Testing Lab is for
administrators: set a developer token before starting, with
`$env:CAPSTONE_DEVELOPER_TOKEN = "<a long random value>"` (or
`CAPSTONE_DEVELOPER_TOKEN_FILE` pointing at a file that holds it), and sign in
with that value.

For a managed multi-machine installation (scheduled task, health checks,
rollback), see `deploy/windows/README.md`. For preparing the lab's model heads,
see `deploy/Capstone-Head-Provisioner/README.md`.

---

## Demonstration scenarios

The six core scenarios are in `data/erp/scenarios.json`:

| ID | Scenario | Expected behaviour |
|---|---|---|
| SCN-01 | Successful grounded retrieval | P4800 Rev A won't power on; the matching procedure is found, validated and cited |
| SCN-02 | Wrong revision | Same words, Rev B unit; the same documents are retrieved and all are rejected on revision, so the agent abstains |
| SCN-03 | No applicable source | Model with no procedures; abstain and escalate |
| SCN-04 | Missing serial number | Account has two devices; ask instead of guessing |
| SCN-05 | Potential safety complaint | Burning smell; the safety gate fires before retrieval and routes to a human |
| SCN-06 | Order and shipment status | Answered from ERP and shipment records; QMS is never searched |

SCN-01 and SCN-02 send exactly the same words and differ only in the serial
number. The hardware revision, not the wording, decides what the system may say.

Further cases cover superseded QMS documents, conflicting revisions and human
approval of returns (`tests/test_rc19_qms_conflict.py`,
`tests/test_rc25_revision_and_review.py`, `tests/test_checkpoint61_safety_suite.py`).

---

## Safety and human oversight

- **Safety screen before retrieval:** possible harm or hazard language stops
  automation and opens a human-review case.
- **Revision gate:** a procedure is used only if `qms_compatibility.json`
  authorises it for the device's confirmed model and revision.
  Superseded or unapproved documents are never served.
- **Grounding verifier:** every instruction sentence in a draft is checked
  against its source. Serial numbers and document IDs are compared character
  for character. A failing draft is rewritten once, then replaced by
  source-only text, or escalated.
- **Named human approval:** returns, replacements and customer emails that
  need approval wait for a named reviewer. The agents can't approve them.
- **Reviewed memory:** past resolutions are reused only after a person approves
  them.
- **No silent fallback:** if the Gateway or a model is unavailable, the failure
  is recorded and the case goes to a person.

---

## Limitations

- The ERP, QMS, ticketing and shipment systems are simulations backed by local
  synthetic data. Nothing connects to a real business system.
- Running the full application needs the Inference Gateway and at least one
  model head; only the automated tests run standalone.
- Small local models (4B parameters) are slow on CPU and sometimes need the
  correction loop. Evaluation showed that adding more agents doesn't on its own
  make the system more reliable.
- Lab network addresses in scripts and samples are replaced with documentation
  addresses (`192.0.2.x`, RFC 5737), and machine names are redacted. Change
  them to match your own network before deploying.
