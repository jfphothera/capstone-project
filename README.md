# Capstone: An Integrated Multi-Agent Support System for a Regulated Device Maker

Final capstone submission. This repository holds the three cooperating
applications that make up one autonomous agent system for Phothera, a maker of
home phototherapy medical devices.

## The problem

A device support answer is only correct if it matches the customer's exact model
**and hardware revision**, and possible safety complaints must be escalated to a
person, never answered automatically. The system takes a customer inquiry,
verifies the customer and device against an ERP, retrieves the controlled QMS
procedure that applies to that exact revision, screens for safety, drafts a
reply, checks it against its source in code, and puts every consequential
action in front of a named human reviewer. All data in this repository is
synthetic.

## The three parts

| Folder | Role |
|---|---|
| [`capstone-project/`](capstone-project/README.md) | **The autonomous agent system.** Twelve cooperating agents (coordinator, ticket interpretation, ERP and QMS research, safety, planning, communication review, independent validation and more), deterministic safety and revision gates, named human approval, and the Testing Lab evaluation harness. |
| [`inference-gateway/`](inference-gateway/README.md) | **Shared model access.** Every model call goes through this authenticated service, which queues requests, qualifies models and spreads the work over several lab machines ("heads") running local models in Ollama. |
| [`agent-arena/`](agent-arena/README.md) | **Observability.** A live view of the agents at work: who is doing what, on which machine, plus the Gateway's view of the model fleet. |

```
  Customer chat / Representative workspace
                    |
           capstone-project  ---- agent telemetry ---->  agent-arena
          (agents, gates,                                    ^
           human approval)                                   |
                    |                                fleet observation
           inference-gateway  ----------------------------+
                    |
     model heads: Ollama on lab machines (qwen3.5, all-minilm, ...)
```

## Getting started

Each folder has its own README with full instructions. The short version
(Windows, Python 3.12):

1. **Run the tests.** Each part has a standalone test suite that needs no
   models, network or lab hardware:
   - `capstone-project`: create a virtual environment, `pip install -r requirements.txt`,
     copy `data\seed\operations.db` to `data\operations.db`, then `python -m pytest`
   - `inference-gateway`: `.\scripts\Setup-Development.ps1` (creates the environment and runs the tests)
   - `agent-arena`: create a virtual environment, `pip install -r requirements-dev.txt`, then `python -m pytest`
2. **Run the system.** Start the Inference Gateway in front of a local Ollama
   ([steps](inference-gateway/README.md#run-it-on-one-machine)), then point
   Capstone at it and start the web app
   ([steps](capstone-project/README.md#run-the-application)). Agent Arena can
   then show the agents live ([Arena README](agent-arena/README.md)).
3. **Deploy across a lab.** The `deploy/` folders and `agent-arena/lab-provisioner`
   hold the Windows install, health-check and provisioning scripts used to run
   the system on several machines.

## Evaluation

- Automated test suites: about 986 tests in `capstone-project`, about 280 in
  `inference-gateway` and 260 in `agent-arena`.
- [`capstone-project/evaluation/experiments`](capstone-project/evaluation/experiments)
  holds the aggregate results of the three final Testing Lab campaigns (2,000
  trials across models, agent team designs and reasoning/retrieval treatments).
- [`inference-gateway/verification`](inference-gateway/verification) holds the
  Gateway's measured concurrency, ledger and load results.

## Data and privacy

- All customer, device, order, ticket and quality-system data is synthetic.
- No credentials, tokens, certificates or private keys are included. The
  configuration files are templates.
- Private network addresses are replaced with reserved documentation addresses
  (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, RFC 5737). Machine
  names, user names and hardware addresses are redacted.
