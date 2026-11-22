"""Set how many requests at once each head takes, by machine class, on ALL heads (2026-11-12).

SET-PARALLEL-PER-HEAD.cmd on the admin hub. For each class of machine in the lab head list (8 GB, 16 GB, 32 GB, the
GPU head) it shows the heads, what the Gateway gives them now, what the head's own Ollama allows (from the
HEAD-PARALLEL reports on the stick) and what the latest pressure test found, and asks how many at once. The number
is applied to every head of that class through the Gateway's own bootstrap (slots; applications and memory are kept),
never above what that head's Ollama allows, and written to C:\\ProgramData\\AgentArenaLab\\gateway\\head-slots.json so
a later hub > 3 > 2 keeps it. Refuses while a pressure test runs. Deletes nothing.
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pressure_test as pt  # noqa: E402  (the same Gateway reads and bootstrap, rehearsed)

CLASSES = ("8 GB", "16 GB", "32 GB", "64 GB", "GPU")


# ------------------------------------------------------------------ pure helpers (tested)

def machine_class(ram_gb, gpu):
    """The class a head belongs to. Pure."""
    if gpu:
        return "GPU"
    r = float(ram_gb or 0)
    return "8 GB" if r <= 8.5 else "16 GB" if r <= 16.5 else "32 GB" if r <= 33 else "64 GB"


def parse_head_report(text):
    """(address, parallel) from one HEAD-PARALLEL report: parallel = what that head's Ollama allows after the run,
    1 after "off", None when the run did not finish. Pure."""
    m = re.search(r"head (192\.0\.2\.\d{1,3})", text)
    if not m:
        return None, None
    addr = m.group(1)
    # 1.6.10 wording: "OLLAMA_NUM_PARALLEL is N" / "is as before"; up to 1.6.9: "it now runs up to N requests at once".
    if re.search(r"RESULT success: .*(it now runs as before|OLLAMA_NUM_PARALLEL is as before)", text):
        return addr, 1
    r = re.search(r"RESULT success: .*(?:it now runs up to (\d+) requests at once|OLLAMA_NUM_PARALLEL is (\d+))", text)
    if r:
        return addr, int(r.group(1) or r.group(2))
    if r:
        return addr, int(r.group(1))
    if "Already so: nothing to change." in text:
        n = re.search(r"now: up to (\d+) requests at once", text)
        return addr, int(n.group(1)) if n else 1
    return addr, None


def parse_qualify_report(doc):
    """(address, requests at once) from one HEAD-OLLAMA report (1.6.14): OLLAMA_NUM_PARALLEL times the models it keeps
    resident, as the head's own server proved them after the final restart; None when it did not finish. Pure."""
    q = (doc or {}).get("qualification") or {}
    if doc.get("schema") == "agent-arena.ollama-qualify/1" and doc.get("rolled_back"):
        return doc.get("address"), 1   # HEAD-OLLAMA > 0: back to the old Ollama; 1 until it is measured again
    if doc.get("schema") != "agent-arena.ollama-qualify/1" or not q.get("final_proof"):
        return doc.get("address"), None
    a = q.get("adopted") or {}
    return doc.get("address"), max(1, int(a.get("num_parallel") or 1)) * max(1, int(a.get("max_loaded_models") or 1))


def _when(p):
    return "-".join(p.stem.rsplit("-", 2)[-2:])


def latest_caps(reports_root):
    """{address: parallel} from the newest finished HEAD-PARALLEL or HEAD-OLLAMA report of each head on the stick.
    Pure for a folder."""
    found = {}
    files = list(Path(reports_root).glob("*/head-parallel-*.txt")) + list(Path(reports_root).glob("*/ollama-qualify-*.json"))
    for f in sorted(files, key=_when):
        if f.suffix == ".json":
            try:
                addr, n = parse_qualify_report(json.loads(f.read_text(encoding="utf-8-sig")))
            except ValueError:
                continue
        else:
            addr, n = parse_head_report(f.read_text(encoding="utf-8", errors="replace"))
        if addr and n is not None:
            found[addr] = n
    return found


def knees(steps_csv, gain=0.10):
    """{head: (step, out tok/s)} the point of diminishing returns per head in a pressure test's steps.csv. Pure."""
    rows = list(csv.DictReader(open(steps_csv, encoding="utf-8"))) if Path(steps_csv).exists() else []
    out = {}
    for h in sorted({r["head"] for r in rows}):
        pts = [(int(r["step"]), float(r["output_tokens_per_s"])) for r in rows if r["head"] == h and r["filled"] == "True" and r["output_tokens_per_s"]]
        k = pt.knee(pts, gain)
        if k[0]:
            out[h] = k
    return out


def plan_slots(heads, chosen, caps):
    """{resource_id: slots} and notes. heads: [{resource_id, address, cls}]; chosen: {cls: n}; caps: {address: n}.
    A head never gets more than its Ollama allows (1 when HEAD-PARALLEL was not run there). Pure."""
    slots, notes = {}, []
    for h in heads:
        want = chosen.get(h["cls"])
        if want is None:
            continue
        cap = caps.get(h["address"], 1)
        slots[h["resource_id"]] = max(1, min(int(want), int(cap)))
        if want > cap:
            notes.append("%s (%s): %d asked, %d given - its Ollama allows %d%s" % (
                h["resource_id"], h["cls"], want, slots[h["resource_id"]], cap, "" if h["address"] in caps else " (run HEAD-PARALLEL on it)"))
    return slots, notes


# ------------------------------------------------------------------ the run

def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--python", required=True)
    a.add_argument("--release", required=True)
    a.add_argument("--ledger", default=r"C:\ProgramData\InferenceGateway\state\capacity-ledger.db")
    a.add_argument("--config", required=True, help="the stick's config\\lab-network.json")
    a.add_argument("--reports", required=True, help="the stick's reports folder (HEAD-PARALLEL reports)")
    a.add_argument("--pressure-root", default=r"C:\ProgramData\AgentArenaLab\pressure-test")
    a.add_argument("--slots-file", default=r"C:\ProgramData\AgentArenaLab\gateway\head-slots.json")
    a.add_argument("--work", required=True, help="folder for the policy and the report")
    a.add_argument("--choose", default="", help="non-interactive: '8 GB=3,16 GB=4,32 GB=6,GPU=8'")
    args = a.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    lines = []

    def say(t=""):
        print("  " + t, flush=True)
        lines.append(time.strftime("%H:%M:%S ") + t)

    def finish(code):
        (work / "set-parallel.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return code

    cur = Path(args.pressure_root) / "current.txt"
    if cur.exists():
        d = Path(cur.read_text(encoding="utf-8").strip())
        try:
            state = json.loads((d / "status.json").read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            state = None
        if state and state != "done":
            say("A pressure test is running: stop it first (PRESSURE-TEST.cmd > 0). Nothing was changed.")
            return finish(2)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    hub = pt.Hub({"release": args.release, "ledger": args.ledger, "python": args.python})
    res = hub.resources()
    gw = {r["resource_id"]: r for r in res}
    heads = []
    for h in cfg.get("head_inventory", []):
        rid = "head-" + h["address"].replace(".", "-")
        if rid in gw:
            heads.append({"resource_id": rid, "address": h["address"], "cls": machine_class(h.get("ram_gb"), h.get("gpu")),
                          "label": "%s GB%s" % (round(float(h.get("ram_gb") or 0)), ", " + h["gpu"] if h.get("gpu") else "")})
    caps = latest_caps(args.reports)
    runs = sorted((Path(args.pressure_root) / "runs").glob("*/steps.csv")) if (Path(args.pressure_root) / "runs").exists() else []
    tested = knees(runs[-1]) if runs else {}
    say("Every head now (Gateway = requests at once it sends the head; Ollama = what the head itself allows):")
    say("  class  | head                         | Gateway | Ollama | pressure test")
    for h in heads:
        k = tested.get(h["resource_id"])
        say("  %-6s | %-28s | %7s | %6s | %s" % (h["cls"], "%s (%s)" % (h["resource_id"], h["label"]), gw[h["resource_id"]]["slots"],
            caps.get(h["address"], "1 (not set)"), "best at %s at once (%s tok/s)" % k if k else "-"))
    chosen = {}
    given = dict(p.split("=", 1) for p in args.choose.split(",") if "=" in p) if args.choose else None
    say()
    for cls in CLASSES:
        mine = [h for h in heads if h["cls"] == cls]
        if not mine:
            continue
        found = [tested[h["resource_id"]][0] for h in mine if h["resource_id"] in tested]
        default = str(max(found)) if found else str(max(gw[h["resource_id"]]["slots"] for h in mine))
        names = ", ".join(h["resource_id"].rsplit("-", 1)[1] for h in mine)
        if given is not None:
            ans = given.get(cls, default).strip()
        else:
            ans = input("  %s machines (head %s): requests at once? (Enter = %s%s) " % (cls, names, default, ", the pressure test's result" if found else ", as now")).strip() or default
        if not re.fullmatch(r"\d{1,2}", ans) or not 1 <= int(ans) <= 64:
            say("%s: '%s' is not 1-64. Nothing was changed." % (cls, ans))
            return finish(1)
        chosen[cls] = int(ans)
        lines.append("%s: %s" % (cls, ans))
    slots, notes = plan_slots(heads, chosen, caps)
    for n in notes:
        say("note: " + n)
    plan = [dict(r, slots=slots.get(r["resource_id"], r["slots"])) for r in res]
    if all(r["slots"] == gw[r["resource_id"]]["slots"] for r in plan):
        say("RESULT nothing to change: every head already has these numbers.")
        return finish(0)
    try:
        hub.bootstrap(plan, work, "parallel")
    except RuntimeError as exc:
        say("RESULT refused by the Gateway: %s. Nothing was changed." % exc)
        return finish(1)
    after = {r["resource_id"]: r for r in hub.resources()}
    wrong = [rid for rid, n in slots.items() if after[rid]["slots"] != n]
    changed = [rid for rid in after if (after[rid]["applications"], after[rid]["memory_gb"]) != (gw[rid]["applications"], gw[rid]["memory_gb"])]
    sf = Path(args.slots_file)
    sf.parent.mkdir(parents=True, exist_ok=True)
    keep_mem = {}  # 1.6.19: the machine-class memory_gb APPLY-MODEL-CAPACITY wrote stays
    if sf.exists():
        try:
            keep_mem = json.loads(sf.read_text(encoding="utf-8-sig")).get("memory_gb") or {}
        except (OSError, ValueError):
            keep_mem = {}
    doc = {"schema": "agent-arena.head-slots/1", "chosen_at": time.strftime("%Y-%m-%d %H:%M:%S"), "by_class": chosen,
           "slots": {rid: after[rid]["slots"] for rid in after}}
    if keep_mem:
        doc["memory_gb"] = keep_mem
    sf.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    say()
    for h in heads:
        say("%-17s %-6s %s at once" % (h["resource_id"], h["cls"], after[h["resource_id"]]["slots"]))
    if wrong or changed:
        say("RESULT not as asked: %s" % ", ".join(wrong + changed))
        return finish(1)
    held = [n.split(" ", 1)[0] for n in notes]
    if held:
        say("RESULT partly done: the heads above take those numbers, but %s stay below the number asked until HEAD-PARALLEL "
            "is run on them; then run this again." % ", ".join(held))
        return finish(1)
    say("RESULT success: every head takes the requests at once shown above (kept for later hub runs in %s)." % sf)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
