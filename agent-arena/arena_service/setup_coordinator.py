"""One-time, idempotent setup of the Arena Event Service on the coordinator hardware.

    python -E -s -m arena_service.setup_coordinator --data "%LOCALAPPDATA%\\AgentArenaService"

Creates the service and Ticketing-adapter configuration, registers the
Ticketing application, its orchestrator, the coordinator's orchestrator
display and the enrolled heads (192.0.2.2-.10 by default; add more any time
with --head), and stores the Ticketing producer credential with DPAPI. It does
not start anything, open firewall ports, or touch Capstone or Ollama.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import secretfile
from .admin import head_identity
from .store import Store

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    p = argparse.ArgumentParser(prog="setup_coordinator", description=__doc__)
    p.add_argument("--data", required=True)
    p.add_argument("--host", default="192.0.2.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--capstone-db", required=True, help="Capstone operations.db, read-only (e.g. ...\\PycharmProjects\\Capstone\\data\\operations.db)")
    p.add_argument("--capstone-health", default="http://127.0.0.1:8000/api/health")
    p.add_argument("--head", action="append", default=[], help="extra head address, e.g. 192.0.2.11 (repeatable)")
    args = p.parse_args(argv)
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    url = f"http://{args.host}:{args.port}"
    config = data / "aes.config.json"
    if not config.exists():
        config.write_text(json.dumps({"host": args.host, "port": args.port, "db_path": str(data / "arena-events.db"),
                                      "log_path": str(data / "arena-event-service.log"), "public_endpoints": [url]}, indent=2))
    store = Store(json.loads(config.read_text())["db_path"])
    store.add_application("capstone", "Ticketing")
    store.add_orchestrator("capstone", "main", "Ticketing coordinator")
    store.add_display("orch-capstone", "orchestrator", "orch:capstone/main", args.host)
    heads = [f"192.0.2.{n}" for n in range(2, 11)] + args.head
    for address in heads:
        node, label = head_identity(address)
        label += " GPU" if address == "192.0.2.2" else ""
        store.add_display(node, "head", store.add_head(node, label, address), address)
    credential = data / "ticketing-producer.dpapi"
    if not credential.exists():
        secretfile.write(str(credential), *store.issue_producer("capstone", ["main"]))
    adapter = data / "ticketing-adapter.json"
    if not adapter.exists():
        adapter.write_text(json.dumps({"capstone_db": args.capstone_db, "aes_url": url, "credential_file": str(credential),
                                       "checkpoint_file": str(data / "ticketing-adapter.checkpoint.json"),
                                       "capstone_health_url": args.capstone_health, "start_from": "now",
                                       "heartbeat_seconds": 15, "log_path": str(data / "ticketing-adapter.log")}, indent=2))
    print(f"Arena Event Service configured in {data}")
    print(f"  service URL      {url}  (service_id {store.meta('service_id')})")
    print(f"  heads registered {', '.join(heads)}")
    print(f"  Ticketing reads  {args.capstone_db} (read-only)")
    print("Next: START-ARENA-EVENT-SERVICE.cmd, then START-TICKETING-ADAPTER.cmd, then ENROLL-DISPLAY.cmd <display>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
