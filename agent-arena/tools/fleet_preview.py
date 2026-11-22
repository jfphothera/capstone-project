"""Preview the admin hub's Fleet window. Development only.

Runs the admin console on a copy of this machine's Arena data (as tools/admin_console_preview.py does) with a
fleet folder in %TEMP%: the real catalog, and a published Gateway state written by the Fleet Commit helper
(fleet_commit.py --publish --rehearsal) when one is given. The head scan is live: from the lab laptop it reads
the heads' Ollama over the lab cable. Commit is stood in for: it prints the request instead of asking Windows.
    python tools/fleet_preview.py [port] [published fleet-state.json]
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arena_service import admin_console  # noqa: E402

live_config = Path(os.environ["LOCALAPPDATA"]) / "AgentArenaService" / "aes.config.json"
config = json.loads(live_config.read_text(encoding="utf-8"))
folder = Path(tempfile.gettempdir()) / "arena-fleet-preview"
fleet = folder / "fleet"
for d in ("catalog", "published", "requests", "status"):
    (fleet / d).mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "lab-provisioner" / "config" / "model-catalog.json", fleet / "catalog" / "model-catalog.json")
if len(sys.argv) > 2:
    shutil.copy2(sys.argv[2], fleet / "published" / "fleet-state.json")
# The lab's head list, as the provisioner writes it on the hub (Install-LabFleet -Inventory).
lab = json.loads((ROOT / "lab-provisioner" / "config" / "lab-network.json").read_text(encoding="utf-8-sig"))
inv = []
for h in lab.get("head_inventory", []):
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*GB", str(h.get("gpu") or ""))
    inv.append({"address": h["address"], "computer": h.get("computer"), "cpu": h.get("cpu"), "ram_gb": h.get("ram_gb"),
                "gpu": h.get("gpu"), "vram_gb": float(m.group(1)) if m else None})
(fleet / "catalog" / "head-inventory.json").write_text(json.dumps({"schema": "agent-arena.head-inventory/1", "heads": inv}, indent=1), encoding="utf-8")
# A stand-in head scan (FLEET_PREVIEW_SCAN=<json {address: {version, models}}>) when the lab cable is not in.
SCAN = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("FLEET_PREVIEW_SCAN")
copy = folder / "arena-events-copy.db"
source, target = sqlite3.connect(f"file:{config['db_path']}?mode=ro", uri=True), sqlite3.connect(copy)
source.backup(target)
source.close()
target.close()
helper = folder / "fleet_commit.py"
helper.write_text("# preview stand-in: Commit prints the request instead of starting the real helper\n")
config.update(db_path=str(copy), fleet={"dir": str(fleet), "helper": str(helper), "python": sys.executable})
preview_config = folder / "aes.preview.json"
preview_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
port = sys.argv[1] if len(sys.argv) > 1 else "18800"
_build = admin_console.build


def build(cfg, port=8791, token=None):
    server, console = _build(cfg, port, token or "fleet-preview-token")    # open /#token=fleet-preview-token
    console.fleet.launcher = lambda python, script, args: print("PREVIEW Commit would start:", python, script, *args, flush=True)
    if SCAN:
        canned = json.loads(Path(SCAN).read_text(encoding="utf-8"))
        console.fleet.scanner = lambda addresses: {a: v for a, v in canned.items() if a in addresses}
    return server, console


admin_console.build = build
print(f"PREVIEW on a copy of the live data: {copy}; fleet folder {fleet}", flush=True)
sys.exit(admin_console.main(["--config", str(preview_config), "--port", port, "--no-browser"]))
