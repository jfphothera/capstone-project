"""Rehearse Lab Traffic end to end on the lab laptop: the real Gateway 0.3.0 code (a throwaway ledger and its own
server on 127.0.0.1) sending real requests to real heads, and a throwaway Agent Arena event service receiving the
TEST events. Nothing on the hub is touched. The heads must answer this laptop (the cutover firewall rule).

    python -I rehearse_lab_traffic.py --release <Gateway 0.3.0 release folder> --work <new folder> [--heads 3,4] [--minutes 3]
"""
import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TOOLS = REPO / "lab-provisioner" / "engine" / "hub-tools"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--release", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--heads", default="3,4")
    p.add_argument("--model", default="qwen3.5:4b")
    p.add_argument("--minutes", type=float, default=3.0)
    p.add_argument("--pace", type=float, default=0.3)
    p.add_argument("--cases", type=int, default=2, help="cases in flight at once")
    p.add_argument("--models", default="", help="comma list of models; default --model")
    p.add_argument("--hold", type=float, default=0, help="keep Arena running this many minutes after the traffic, to look at it")
    a = p.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=False)
    release = Path(a.release)
    sys.path[:0] = [str(release), str(REPO), str(TOOLS / "gateway-models"), str(TOOLS / "model-library")]
    from inference_gateway.capacity_broker import CapacityBroker
    from inference_gateway.capacity_bootstrap import apply
    import lab_traffic as lt
    from arena_service.store import Store
    from arena_service.client import enroll_app, Client

    # 1. A Gateway ledger with the chosen heads, serving lab-traffic, then the Gateway's own issue and qualify.
    ledger = work / "ledger.db"
    CapacityBroker(str(ledger))
    heads = [f"head-192-0-2-{n.strip()}" for n in a.heads.split(",")]
    res = [{"resource_id": h, "endpoint": "http://" + h[5:].replace("-", ".") + ":11434", "resource_type": "shared_inference",
            "owner": "infrastructure", "applications": ["lab-traffic"], "permitted_uses": ["inference"]} for h in heads]
    (work / "resources.json").write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [], "resources": res}))
    apply(str(work / "resources.json"), str(ledger))
    models = [m.strip() for m in (a.models or a.model).split(",") if m.strip()]
    pol = lt.traffic_policy(models)
    pol["clients"][0]["max_concurrency"] = max(2, a.cases)
    (work / "policy.json").write_text(json.dumps(pol))
    launcher = release / "inference_gateway" / "service_launcher.py"
    def tool(*args, timeout=240):
        r = subprocess.run([sys.executable, "-I", str(launcher), *args], capture_output=True, text=True, timeout=timeout)
        print("  gateway", args[0], "->", (r.stdout.strip().splitlines() or [""])[-1][:200], flush=True)
        return r
    tool("credentials", "issue", "--database", str(ledger), "--app-id", "lab-traffic", "--policy", str(work / "policy.json"), "--output", str(work / "lab-traffic.token"))
    for h in heads:
        for m in models:
            tool("qualify", "--database", str(ledger), "--resource", h, "--model", m, "--operator", "rehearsal", "--max-context", "8192",
                 "--output", str(work / f"qual-{h}-{m.replace(':', '_')}.json"))
    gport, aport = free_port(), free_port()
    gw_proc = subprocess.Popen([sys.executable, "-I", "-c", f"import sys;sys.path.insert(0,{str(release)!r});sys.argv=['x','--database',{str(ledger)!r},'--port','{gport}'];"
                                "from inference_gateway.capacity_gateway_server import main;main()"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print("  gateway server:", gw_proc.stdout.readline().strip(), flush=True)

    # 2. A throwaway Arena event service with the lab-traffic application and an app code.
    db = work / "arena.db"
    store = Store(str(db))
    # The heads as the hub's Arena has them (registered when it was installed: head:head-192-0-2-N); Arena refuses a
    # move to a head it does not know.
    from arena_service.admin import head_identity
    for h in heads:
        address = h[5:].replace("-", ".")
        node, label = head_identity(address)
        store.add_display(node, "head", store.add_head(node, label, address), address)
    store.add_application("lab-traffic", "Lab traffic (TEST)")
    store.add_orchestrator("lab-traffic", "main", "Lab traffic orchestrator (TEST)")
    code = store.issue_app_code("lab-traffic", "main", 1)
    store.db.close()
    cfg = work / "aes.json"
    cfg.write_text(json.dumps({"host": "127.0.0.1", "port": aport, "db_path": str(db), "log_path": str(work / "aes.log")}))
    ar_proc = subprocess.Popen([sys.executable, "-I", "-c", f"import sys;sys.path.insert(0,{str(REPO)!r});sys.argv=['x','--config',{str(cfg)!r}];"
                                "from arena_service.__main__ import main;main()"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{aport}"
    for _ in range(50):
        try:
            cid, secret, _ = enroll_app(url, "lab-traffic", "main", code)
            break
        except Exception:
            time.sleep(0.2)
    arena = Client(url, cid, secret)

    # 3. The traffic itself.
    token = (work / "lab-traffic.token").read_text().strip()
    gw = lt.Gateway(f"http://127.0.0.1:{gport}", "lab-traffic", token, None, timeout=60)
    try:
        tally = lt.Traffic(gw, arena.publish, minutes=a.minutes, pace=a.pace, cases_at_once=a.cases, seed=7, out=lambda s: print(s, flush=True)).run()
        if a.hold:
            print(f"traffic finished; Arena stays up {a.hold:g} more minutes to look at", flush=True)
            time.sleep(a.hold * 60)
    finally:
        time.sleep(1)
        gw_proc.terminate()
        ar_proc.terminate()
    # 4. What each side recorded.
    import sqlite3
    g = sqlite3.connect(str(ledger))
    done = g.execute("SELECT resource_id, status, COUNT(*) FROM capacity_requests WHERE app_id='lab-traffic' GROUP BY resource_id, status").fetchall()
    s = sqlite3.connect(str(db))
    accepted = s.execute("SELECT COUNT(*) FROM events WHERE app_id='lab-traffic' AND applied=1").fetchone()[0]
    at_heads = s.execute("SELECT COUNT(*) FROM events WHERE app_id='lab-traffic' AND loc_to LIKE 'head:%'").fetchone()[0]
    print(json.dumps({"tally": tally, "gateway_requests": done, "arena_events_applied": accepted, "arena_moves_to_heads": at_heads}, indent=1))
    ok = tally["completed"] > 0 and tally["failed"] == 0 and at_heads > 0
    print("REHEARSAL", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
