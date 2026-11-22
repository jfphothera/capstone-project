"""Give the Gateway each head's measured per-model capacity (provisioner 1.6.14, 2026-11-17).

APPLY-MODEL-CAPACITY.cmd on the admin hub. It
  1. reads the newest HEAD-OLLAMA report of every head on the stick (reports\\<computer>\\ollama-qualify-*.json);
  2. builds the qualification matrix, agent-arena.model-capacity/1: one entry per head and model with the runtime
     version and backend, the model digest, num_ctx, the parallel setting and what was measured, the resident-model and
     queue settings, memory, throughput, latency and the pass/fail evidence; shows it and writes matrix.csv;
  3. when the running Gateway is 0.4.4 or later, asks APPLY and applies resources[].model_capacity
     ({model: {parallel, digest, runtime_version, num_ctx, evidence}}, parallel = measured_parallel, passed entries
     only) through the Gateway's own bootstrap, as SET-PARALLEL-PER-HEAD applies slots, keeping every head's slots,
     applications and memory; then writes C:\\ProgramData\\AgentArenaLab\\gateway\\model-capacity.json so later hub
     tools carry it again.
The Gateway uses a value only while the head's live /api/version and the model digest match it. Refuses while a
pressure test runs. Deletes nothing.
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pressure-test"))
import pressure_test as pt  # noqa: E402  (the same Gateway reads and bootstrap, rehearsed)

SCHEMA = "agent-arena.model-capacity/1"
ENTRY_KEYS = ("resource_id", "address", "model", "digest", "runtime_version", "backend", "num_ctx", "num_parallel_setting", "measured_parallel",
              "max_loaded_models", "max_queue", "keep_alive", "flash_attention", "kv_cache_type", "memory", "throughput", "latency",
              "isolation_passed", "passed", "evidence")
GATEWAY_MIN = (0, 4, 4)
PLACEMENT_MIN = (0, 4, 5)   # placement_policy.unmeasured_model_limit


# ------------------------------------------------------------------ pure helpers (tested)

def newest_reports(reports_root):
    """{computer: (path, report)} the newest readable HEAD-OLLAMA report of each computer. Pure for a folder."""
    out = {}
    for f in sorted(Path(reports_root).glob("*/ollama-qualify-*.json"), key=lambda p: p.name):
        try:
            doc = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("schema") == "agent-arena.ollama-qualify/1":
            doc["entries"] = entry_list(doc.get("entries"))
            out[f.parent.name] = (f, doc)
    return out


def entry_list(value):
    """1.6.22: the report's entries as a list. HEAD-OLLAMA up to 1.6.21 wrote a head with one qualified model as a single
    object, not a one-element array (heads 3, 4, 5 and 6 on 2026-11-17); anything else that is not an entry is dropped. Pure."""
    if isinstance(value, dict):
        value = [value]
    return [e for e in (value or []) if isinstance(e, dict)]


def aggregate(reports, generated_at=None):
    """The agent-arena.model-capacity/1 document from {computer: (path, report)}. Pure."""
    entries, sources = [], []
    for computer, (path, doc) in sorted(reports.items()):
        sources.append({"computer": computer, "address": doc.get("address"), "report": "reports\\%s\\%s" % (computer, Path(path).name),
                        "generated_at": doc.get("generated_at")})
        for e in doc.get("entries") or []:
            entries.append({k: e.get(k) for k in ENTRY_KEYS})
    entries.sort(key=lambda e: (tuple(int(x) for x in re.findall(r"\d+", e.get("address") or "")), e.get("model") or ""))
    return {"schema": SCHEMA, "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%S"), "sources": sources, "entries": entries}


def head_settings(reports, classes=()):
    """1.6.19: {resource_id: {"slots", "memory_gb", "class"}} for each head whose newest report names its machine class.
    1.6.24: the class's current slots and memory_gb from the provisioner's config win over the values the report
    recorded when the head was qualified, so a corrected class needs no head re-run (head 6: 2 -> 3.4, GPU: 14 -> 16,
    2026-11-18). A report without a class (older, or not proven) leaves that head's slots and memory as they are. Pure."""
    by_name = {c.get("name"): c for c in classes or () if isinstance(c, dict)}
    out = {}
    for _computer, (_path, doc) in sorted(reports.items()):
        mc, rid = doc.get("machine_class"), doc.get("resource_id")
        if not rid or not isinstance(mc, dict):
            continue
        src = by_name.get(mc.get("name")) or mc
        if src.get("gateway_slots") and src.get("gateway_memory_gb"):
            out[rid] = {"slots": int(src["gateway_slots"]), "memory_gb": float(src["gateway_memory_gb"]), "class": mc.get("name")}
    return out


def operator_entries(reports, classes, existing=()):
    """1.6.23: entries for the machine classes' operator_model_limits ({model: {parallel, why}}), for each head whose
    newest report is proven and names that class, when the model is on the head (its digest from the report's own
    inventory) and the head has no measured entry for it. The Gateway treats them like any declaration: used only while
    the head's runtime and the model's digest match. Pure."""
    by_name = {c.get("name"): c for c in classes or [] if isinstance(c, dict)}
    measured = {(e.get("resource_id"), e.get("model")) for e in existing}
    out = []
    for _computer, (_path, doc) in sorted(reports.items()):
        mc, rid = doc.get("machine_class"), doc.get("resource_id")
        q = doc.get("qualification") or {}
        cls = by_name.get((mc or {}).get("name")) if isinstance(mc, dict) else None
        runtime = (doc.get("runtime") or {}).get("version")
        if not cls or not rid or not q.get("final_proof") or not runtime:
            continue
        inv = {}
        for side in ("before", "after"):
            for m in ((doc.get(side) or {}).get("models") or []):
                if isinstance(m, dict) and m.get("name") and m.get("digest"):
                    inv[m["name"]] = m["digest"]
        for model, lim in sorted((cls.get("operator_model_limits") or {}).items()):
            if (rid, model) in measured or model not in inv or not isinstance(lim, dict) or int(lim.get("parallel") or 0) < 1:
                continue
            out.append({"resource_id": rid, "address": doc.get("address"), "model": model, "digest": inv[model], "runtime_version": runtime,
                        "backend": None, "num_ctx": None, "num_parallel_setting": (q.get("adopted") or {}).get("num_parallel"),
                        "measured_parallel": int(lim["parallel"]), "max_loaded_models": (q.get("adopted") or {}).get("max_loaded_models"),
                        "max_queue": None, "keep_alive": None, "flash_attention": None, "kv_cache_type": None, "memory": None, "throughput": None,
                        "latency": None, "isolation_passed": None, "passed": True, "evidence": (lim.get("why") or "operator limit")[:400]})
    return out


def load_config(config_path):
    """The provisioner's lab-network.json; {} when absent or unreadable."""
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def load_classes(config_path):
    """The machine classes from the provisioner's lab-network.json; [] when absent or unreadable."""
    return ((load_config(config_path).get("ollama") or {}).get("server_settings") or {}).get("machine_classes") or []


def plan(resources, policy, reported=(), heads=None):
    """The Gateway resources with model_capacity set from the policy for the heads it names, {} for a head whose newest
    report passed nothing (clears what the Gateway stored before: Gateway 0.4.4), slots and memory_gb from `heads`
    (head_settings) for the heads it names, every other field and every other head as it is. Pure."""
    out = []
    for r in resources:
        r = dict(r)
        if r["resource_id"] in policy:
            r["model_capacity"] = policy[r["resource_id"]]
        elif r["resource_id"] in set(reported):
            r["model_capacity"] = {}
        h = (heads or {}).get(r["resource_id"])
        if h:
            r["slots"], r["memory_gb"] = h["slots"], h["memory_gb"]
        out.append(r)
    return out


def merge_slots_file(current, heads, chosen_at):
    """1.6.19: the head-slots.json document with these heads' slots and memory_gb set, every other head kept, so later
    hub runs (Administration hub > 3 > 2, Get-LabHeadCapacity) keep them. Pure."""
    doc = dict(current or {})
    doc["schema"] = "agent-arena.head-slots/1"
    slots, mem = dict(doc.get("slots") or {}), dict(doc.get("memory_gb") or {})
    for rid, h in heads.items():
        slots[rid], mem[rid] = h["slots"], h["memory_gb"]
    doc.update(slots=slots, memory_gb=mem, chosen_at=chosen_at, chosen_by="APPLY-MODEL-CAPACITY (machine classes)")
    return doc


def matrix_rows(entries):
    """Flat rows of the qualification matrix (matrix.csv). Pure."""
    rows = []
    for e in entries:
        m, t, l = e.get("memory") or {}, e.get("throughput") or {}, e.get("latency") or {}
        rows.append({"head": e.get("resource_id"), "model": e.get("model"), "digest": (e.get("digest") or "")[:19], "runtime": e.get("runtime_version"),
                     "backend": e.get("backend"), "num_ctx": e.get("num_ctx"), "parallel_setting": e.get("num_parallel_setting"),
                     "measured_parallel": e.get("measured_parallel"), "max_loaded": e.get("max_loaded_models"), "max_queue": e.get("max_queue"),
                     "keep_alive": e.get("keep_alive"), "flash_attention": e.get("flash_attention"), "kv_cache": e.get("kv_cache_type"),
                     "ram_gb": m.get("ram_gb"), "model_gb": m.get("model_size_gb"), "min_free_gb": m.get("min_free_gb"),
                     "serial_tok_s": t.get("serial_tokens_per_s"), "parallel_tok_s": t.get("parallel_tokens_per_s"), "gain": t.get("gain"),
                     "p50_s": l.get("p50_s"), "p95_s": l.get("p95_s"), "queue_ms": l.get("queue_ms_avg"), "load_ms": l.get("load_ms"),
                     "isolation": e.get("isolation_passed"), "passed": e.get("passed"), "evidence": e.get("evidence")})
    return rows


def release_version(release_dir):
    """(0, 4, 4) from ...\\releases\\0.4.4; None when the folder name is not a version. Pure."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", Path(str(release_dir)).name)
    return tuple(int(x) for x in m.groups()) if m else None


# ------------------------------------------------------------------ the run

def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--python", required=True)
    a.add_argument("--release", required=True)
    a.add_argument("--ledger", default=r"C:\ProgramData\InferenceGateway\state\capacity-ledger.db")
    a.add_argument("--reports", required=True, help="the stick's reports folder (HEAD-OLLAMA reports)")
    a.add_argument("--pressure-root", default=r"C:\ProgramData\AgentArenaLab\pressure-test")
    a.add_argument("--capacity-file", default=pt.MODEL_CAPACITY_FILE)
    a.add_argument("--slots-file", default=r"C:\ProgramData\AgentArenaLab\gateway\head-slots.json")
    a.add_argument("--config", default=str(Path(__file__).resolve().parents[3] / "config" / "lab-network.json"),
                   help="the provisioner's lab-network.json (machine classes and their operator_model_limits)")
    a.add_argument("--work", required=True, help="folder for the matrix, the policy and the report")
    a.add_argument("--yes", action="store_true", help="apply without asking (rehearsals)")
    args = a.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    lines = []

    def say(t=""):
        print("  " + t, flush=True)
        lines.append(time.strftime("%H:%M:%S ") + t)

    def finish(code):
        (work / "apply-model-capacity.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return code

    reports = newest_reports(args.reports)
    if not reports:
        say("No HEAD-OLLAMA report is on this stick (reports\\<computer>\\ollama-qualify-*.json). Run HEAD-OLLAMA on the heads first. Nothing was changed.")
        return finish(2)
    doc = aggregate(reports)
    ops = operator_entries(reports, load_classes(args.config), doc["entries"])
    doc["entries"] += ops
    for e in ops:
        say("operator limit: %s %s parallel %d (%s)" % (e["resource_id"], e["model"], e["measured_parallel"], e["evidence"][:90]))
    (work / "model-capacity.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    rows = matrix_rows(doc["entries"])
    with open(work / "matrix.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["head"])
        w.writeheader()
        w.writerows(rows)
    say("Qualification matrix (%d head(s), %d entries; full table: %s):" % (len(reports), len(rows), work / "matrix.csv"))
    say("  head             | model       | Ollama   | backend | ctx  | set | measured | resident | tok/s serial -> parallel | p95 s | result")
    for r in rows:
        say("  %-16s | %-11s | %-8s | %-7s | %-4s | %3s | %8s | %8s | %6s -> %-15s | %5s | %s" % (
            r["head"], r["model"], r["runtime"], r["backend"], r["num_ctx"], r["parallel_setting"], r["measured_parallel"], r["max_loaded"],
            r["serial_tok_s"], r["parallel_tok_s"], r["p95_s"], "PASS" if r["passed"] else "not passed"))
    policy = pt.model_capacity_policy(doc["entries"])
    missing = sorted({e["address"] for e in doc["entries"]} - {e["address"] for e in doc["entries"] if e["resource_id"] in policy})
    if missing:
        say("note: no passed entry for %s: the Gateway keeps its conservative limit there." % ", ".join(missing))
    ver = release_version(args.release)
    if not ver or ver < GATEWAY_MIN:
        say("RESULT matrix written, not applied: the running Gateway (%s) takes no model_capacity. Update it first (Administration hub > 3 > 2), then run this again."
            % Path(args.release).name)
        return finish(1)
    cur = Path(args.pressure_root) / "current.txt"
    if cur.exists():
        try:
            state = json.loads((Path(cur.read_text(encoding="utf-8").strip()) / "status.json").read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            state = None
        if state and state != "done":
            say("A pressure test is running: stop it first (PRESSURE-TEST.cmd > 0). Nothing was changed.")
            return finish(2)
    hub = pt.Hub({"release": args.release, "ledger": args.ledger, "python": args.python, "model_capacity_file": str(work / "none.json")})
    res = hub.resources()
    known = {r["resource_id"] for r in res}
    for rid in sorted(set(policy) - known):
        say("note: %s is not a Gateway head: skipped." % rid)
    policy = {k: v for k, v in policy.items() if k in known}
    heads = {k: v for k, v in head_settings(reports, load_classes(args.config)).items() if k in known}
    planned = plan(res, policy, {e["resource_id"] for e in doc["entries"]}, heads)
    say()
    before_res = {r["resource_id"]: r for r in res}
    for rid, h in sorted(heads.items()):
        b = before_res[rid]
        say("%-17s %-6s slots %s -> %s, memory_gb %s -> %s" % (rid, h["class"], b.get("slots"), h["slots"], b.get("memory_gb"), h["memory_gb"]))
    for r in planned:
        if r.get("model_capacity") is not None:
            say("%-17s %s" % (r["resource_id"], ", ".join("%s parallel %d" % (m, c["parallel"]) for m, c in sorted(r["model_capacity"].items()))
                              or "none passed: its earlier per-model limits are cleared (qwen3.5 back to 1)"))
    if not args.yes:
        if input("  Type APPLY to give the Gateway these per-model limits (Enter = stop): ").strip() != "APPLY":
            say("RESULT nothing applied (not confirmed). The matrix is in %s." % work)
            return finish(1)
    # 1.6.23: the lab's placement rule for models without a current measurement on a head (config
    # gateway.placement_policy.unmeasured_model_limit), on Gateway 0.4.5+ (an older one refuses the key).
    placement = dict(((load_config(args.config).get("gateway") or {}).get("placement_policy") or {}))
    placement = {k: v for k, v in placement.items() if not k.startswith("_")}
    if placement and ver < PLACEMENT_MIN:
        say("note: Gateway %s does not take placement_policy.unmeasured_model_limit yet (0.4.5+): models without a measurement on a "
            "head keep the head's slots until the Gateway is updated (Administration hub > 3 > 2)." % Path(args.release).name)
        placement = {}
    elif placement:
        say("placement: every model without a current measurement on a head runs at most %s at once there (measured limits above win)"
            % placement.get("unmeasured_model_limit"))
    try:
        f = hub.bootstrap(planned, work, "model-capacity", placement or None)
    except RuntimeError as exc:
        say("RESULT refused by the Gateway: %s. Nothing was changed." % exc)
        return finish(1)
    after = {r["resource_id"]: r for r in hub.resources()}
    before = {r["resource_id"]: r for r in res}
    want = {r["resource_id"]: r for r in planned}
    changed = [rid for rid in after if (after[rid]["slots"], after[rid]["applications"], after[rid]["memory_gb"]) != (want[rid]["slots"], want[rid]["applications"], want[rid]["memory_gb"])]
    cf = Path(args.capacity_file)
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(dict(doc, applied_at=time.strftime("%Y-%m-%dT%H:%M:%S"), policy_file=str(f)), indent=2), encoding="utf-8")
    if heads:
        sf = Path(args.slots_file)
        cur = None
        if sf.exists():
            try:
                cur = json.loads(sf.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                cur = None
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(merge_slots_file(cur, heads, time.strftime("%Y-%m-%d %H:%M:%S")), indent=2), encoding="utf-8")
        say("slots and memory_gb kept for later hub runs in %s" % sf)
    if changed:
        say("RESULT applied, but the Gateway shows other slots, applications or memory than planned on %s: check them." % ", ".join(changed))
        return finish(1)
    say("RESULT success: the Gateway has the per-model limits above (policy %s; kept for later hub runs in %s). It uses each one only while "
        "the head's /api/version and the model digest match." % (f.name, cf))
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
