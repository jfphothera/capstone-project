"""Rehearsal for the Gateway Models console: the real Gateway 0.3.0 code, a fresh ledger set up like
DESKTOP-REDACT10's (capstone and capstone-research issued from the approved policy, heads enrolled with the
Gateway's own heads tool and bootstrap, qualified at 8192), the live heads, and the console itself.

    python -I rehearse_models_console.py setup   --release <Gateway release folder> --work <scratch folder> --policy <approved policy> [--heads 192.0.2.3,...]
    python -I rehearse_models_console.py monitor --release ... --work ...      (the service's health refresh, every 15 s)

Stood in: the Windows service (monitor runs the Gateway's own refresh_health in a loop) and
administrator rights (the scratch ledger belongs to the user running this).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "engine" / "hub-tools" / "gateway-models"))
import gateway_models as gm  # noqa: E402


def launcher(release, *args):
    p = subprocess.run([sys.executable, "-I", str(Path(release) / "inference_gateway" / "service_launcher.py"), *args], capture_output=True, text=True)
    print(" ".join(args[:2]), "->", p.returncode, p.stdout.strip()[:300], p.stderr.strip()[-300:])
    return p


def setup(a):
    work = Path(a.work)
    (work / "state").mkdir(parents=True, exist_ok=True)
    (work / "tokens").mkdir(exist_ok=True)
    ledger = work / "state" / "capacity-ledger.db"
    if ledger.exists():
        raise SystemExit(f"{ledger} exists: use a new --work folder")
    subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, r'{a.release}'); from inference_gateway.capacity_gateway import Gateway; Gateway(r'{ledger}')"], check=True)
    for app in ("capstone", "capstone-research"):
        launcher(a.release, "credentials", "issue", "--database", str(ledger), "--policy", a.policy, "--app-id", app, "--output", str(work / "tokens" / (app + ".token")))
    heads = [h for h in a.heads.split(",") if h]
    found = gm.scan_heads(heads)
    inv = {"schema": "agent-arena.verified-head-inventory/1", "heads": [], "excluded": []}
    for h in heads:
        if h in found:
            one = gm.head_inventory(h, found[h]["version"], [{"name": n, "digest": d} for n, d in found[h]["models"].items()], "REHEARSAL")
            inv["heads"] += one["heads"]
    (work / "inventory.json").write_text(json.dumps(inv, indent=2), encoding="utf-8")
    launcher(a.release, "heads", "--inventory", str(work / "inventory.json"), "--policy", a.policy, "--applications", "capstone,capstone-research", "--output", str(work / "review.json"))
    launcher(a.release, "bootstrap", "--database", str(ledger), "--policy", str(work / "review.json"))
    for h in heads:
        if h not in found:
            continue
        for m in a.qualify.split(","):
            if m in found[h]["models"]:
                launcher(a.release, "qualify", "--database", str(ledger), "--resource", gm.resource_for(h), "--model", m, "--operator", "rehearsal",
                         "--max-context", "8192", "--output", str(work / f"qual-{h}-{m.replace(':', '_')}.json"))
    print("ledger ready:", ledger)


def monitor(a):
    ledger = Path(a.work) / "state" / "capacity-ledger.db"
    sys.path.insert(0, a.release)
    from inference_gateway.capacity_gateway import Gateway
    g = Gateway(str(ledger))
    while True:
        try:
            g.refresh_health()
        except Exception as exc:  # keep refreshing like the service does
            print("refresh failed:", exc, flush=True)
        time.sleep(15)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["setup", "monitor"])
    p.add_argument("--release", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--policy")
    p.add_argument("--heads", default="192.0.2.3,192.0.2.4,192.0.2.5")
    p.add_argument("--qualify", default="qwen3.5:4b,gemma3:4b")
    a = p.parse_args()
    setup(a) if a.action == "setup" else monitor(a)
