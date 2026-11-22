"""Workstation visual harness: live service + Ticketing adapter + synthetic Coder + dev bridge.

    python -E -s -m arena_service.visual_harness --port 18791

Everything binds to 127.0.0.1 and uses a temporary database. The Ticketing
stream is the real Phase 4.2 trace re-timed to now and replayed at its
recorded pace; the Coder stream is the labelled synthetic producer. This is a
rendering harness, not deployment evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .adapters.capstone.runner import Adapter
from .client import Client, enroll
from .clients.synthetic_coder import Script
from .devbridge import serve as serve_bridge
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "frontend"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--display", default="head-192-0-2-3")
    parser.add_argument("--service-port", type=int, default=18790)
    parser.add_argument("--outage-every", type=float, default=0, help="stop the service every N seconds (0 = never)")
    parser.add_argument("--outage-for", type=float, default=15)
    parser.add_argument("--admin-port", type=int, default=0, help="also run the admin console on this harness's data")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="arena-harness-"))
    config = {"host": "127.0.0.1", "port": args.service_port, "db_path": str(tmp / "aes.db")}
    (tmp / "aes.json").write_text(json.dumps(config), encoding="utf-8")
    store = Store(config["db_path"])          # setup through the admin path; the service is a separate process
    url = f"http://127.0.0.1:{args.service_port}"
    store.add_application("capstone", "Ticketing")
    store.add_orchestrator("capstone", "main", "Ticketing coordinator")
    store.add_application("coder-synthetic-test", "Coder (SYNTHETIC TEST)")
    store.add_orchestrator("coder-synthetic-test", "test", "Coder test orchestrator")
    for n in (3, 6, 7):
        location = store.add_head(f"head-192-0-2-{n}", f"Head {n} (192.0.2.{n})")
        store.add_display(f"head-192-0-2-{n}", "head", location)
    store.add_display("orch-capstone", "orchestrator", "orch:capstone/main")
    root = str(ROOT)
    start = lambda: subprocess.Popen([sys.executable, "-E", "-s", "-m", "arena_service", "--config", str(tmp / "aes.json")], cwd=root)
    proc = start()
    time.sleep(1.5)
    credential_id, secret, _ = enroll(url, args.display, store.issue_enrollment(args.display), "harness-machine-hash")
    viewer = Client(url, credential_id, secret)
    serve_bridge(WEB, lambda: viewer, args.port)
    ticketing = Client(url, *store.issue_producer("capstone", ["main"]))
    coder = Client(url, *store.issue_producer("coder-synthetic-test", ["test"]))

    def ticketing_loop():
        from .tests.test_capstone_adapter import capstone_db, trace_rows
        while True:
            stamp = str(int(time.time()))
            run = tmp / f"ops-{stamp}.db"
            rows = trace_rows()
            for row in rows:                # fresh identities per loop so the service sees new work
                for field in ("event_id", "task_id", "workflow_id", "correlation_id"):
                    if row.get(field) and row["event_type"] not in ("agent.registered",) or field == "event_id":
                        row[field] = f"{row[field]}-{stamp}" if row.get(field) else row.get(field)
            capstone_db(run, rows)
            adapter = Adapter({"capstone_db": str(run), "checkpoint_file": str(run) + ".cp", "start_from": 0,
                               "heartbeat_seconds": 30}, ticketing)
            while True:
                try:
                    adapter.sync_registry()
                    adapter.heartbeat()
                    break
                except OSError:
                    time.sleep(2)
            while True:
                try:
                    if not adapter.step(limit=2):
                        break
                    adapter.heartbeat()
                except OSError:
                    pass                    # service outage: keep the checkpoint and retry
                time.sleep(1.5)
            time.sleep(10)

    def coder_loop():
        while True:
            for step in Script("head-192-0-2-3", "head-192-0-2-6").steps():
                while True:
                    try:
                        coder.publish(step)
                        break
                    except OSError:
                        time.sleep(2)
                time.sleep(4)
            time.sleep(6)

    def outage_loop():
        nonlocal proc
        while True:
            time.sleep(args.outage_every)
            proc.kill()                         # real process death, as on a coordinator stop
            proc.wait()
            print("harness: event service KILLED", flush=True)
            time.sleep(args.outage_for)
            proc = start()
            print("harness: event service RESTARTED", flush=True)

    if args.outage_every:
        threading.Thread(target=outage_loop, daemon=True).start()
    threading.Thread(target=ticketing_loop, daemon=True).start()
    threading.Thread(target=coder_loop, daemon=True).start()
    if args.admin_port:
        from .admin_console import build as build_console
        observation = tmp / "gateway-observation.json"

        def gateway_loop():
            # The real gateway exporter's output (tests/fixtures), re-timed to now and published the
            # way the gateway's operator would: write aside, then replace atomically.
            fixture = json.loads((ROOT / "arena_service/tests/fixtures/gateway_observation.json").read_text(encoding="utf-8"))
            while True:
                shift = time.time() - fixture["observed_at"]
                doc = json.loads(json.dumps(fixture))
                doc["observed_at"] += shift
                for r in doc["resources"]:
                    r["last_seen"] += shift if r["last_seen"] else 0
                for item in doc["leases"]:
                    item["expires"] += shift
                    item["deadline"] += shift
                for item in doc["qualification_observations"] + doc["events"]:
                    item["at"] += shift
                (tmp / "gateway-observation.tmp").write_text(json.dumps(doc), encoding="utf-8")
                os.replace(tmp / "gateway-observation.tmp", observation)
                time.sleep(3)

        threading.Thread(target=gateway_loop, daemon=True).start()
        console_server, _ = build_console(dict(config, gateway={"observation_file": str(observation), "gateway_id": "harness-gateway"}),
                                          args.admin_port, token="harness")
        threading.Thread(target=console_server.serve_forever, daemon=True).start()
        print(f"harness: admin console http://127.0.0.1:{args.admin_port}/#token=harness", flush=True)
    print(f"harness: bridge http://127.0.0.1:{args.port}/ (display {args.display}); service {url}; data {tmp}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
