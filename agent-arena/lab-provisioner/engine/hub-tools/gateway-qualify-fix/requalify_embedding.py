"""Put back the embedding models the hub's qualification step took off the heads (2026-11-12).

Why: Administration hub > 3 > 2 ("Gateway: qualification") asked the Gateway to test every model in the policy with
the text test (purpose "completion"), embedding models too. An embedding model cannot pass a text test, and a failed
test of any kind takes the model off that head's qualified list (Gateway capacity_gateway.qualify). So all-minilm,
which Capstone uses for lookups, was taken off all nine heads at 14:13-14:37 although it had passed the embedding
test on every one of them before.

What it does: for each head and embedding model that passed the Gateway's embedding test before and is not on the
head's qualified list now, it runs the Gateway's own embedding test once (qualify --purpose embedding). It skips
pairs marked Do not use in the Gateway Models screen. It reads the ledger read-only and changes it only through the
Gateway's qualify command. It deletes nothing. Exit 0 all put back, 1 some not, 2 could not run.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

NOT_CHAT = re.compile(r"(?i)embed|minilm|(^|[/:-])bge|(^|[/:-])gte")   # the same rule as the Gateway Models screen
QUALIFY_CONTEXT = 8192


def read_state(ledger):
    """(heads, pairs): heads = {resource_id: {health, endpoint, qualified}}; pairs = embedding (resource, model) that
    passed the embedding test on the head's current endpoint before and are not qualified there now. Read-only."""
    db = sqlite3.connect("file:{}?mode=ro".format(Path(ledger).as_posix()), uri=True)
    db.row_factory = sqlite3.Row
    try:
        heads = {r["resource_id"]: {"health": r["health"], "endpoint": r["endpoint"], "qualified": json.loads(r["qualified_models"] or "[]")}
                 for r in db.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference' AND health!='retired'")}
        passed = set()
        for r in db.execute("SELECT resource_id, model, evidence FROM capacity_qualification_evidence WHERE passed=1"):
            e = json.loads(r["evidence"] or "{}")
            h = heads.get(r["resource_id"])
            if h and e.get("purpose") == "embedding" and e.get("endpoint") == h["endpoint"] and NOT_CHAT.search(r["model"] or ""):
                passed.add((r["resource_id"], r["model"]))
    finally:
        db.close()
    pairs = sorted(p for p in passed if p[1] not in heads[p[0]]["qualified"])
    return heads, pairs


def do_not_use(folder):
    f = Path(folder) / "do-not-use.json"
    if not f.exists():
        return set()
    return {(e["resource_id"], e["model"]) for e in json.loads(f.read_text(encoding="utf-8-sig")).get("entries", [])}


def head_order(resource_id):
    m = re.search(r"(\d+)$", resource_id)
    return int(m.group(1)) if m else 0


def qualify(python, launcher, ledger, resource_id, model, operator, evidence_dir):
    out = Path(evidence_dir) / "qual-{}-{}-{}.json".format(resource_id, re.sub(r"[^\w.-]", "_", model), time.strftime("%Y%m%d-%H%M%S"))
    p = subprocess.run([python, "-I", str(launcher), "qualify", "--database", str(ledger), "--resource", resource_id, "--model", model,
                        "--purpose", "embedding", "--operator", operator, "--max-context", str(QUALIFY_CONTEXT), "--output", str(out)],
                       capture_output=True, text=True, timeout=200, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    reason = ""
    try:
        e = json.loads(out.read_text(encoding="utf-8"))
        passed = bool(e.get("passed"))
        reason = e.get("error") or ""
    except (OSError, ValueError):
        passed = False
    if not passed and not reason:
        text = (p.stdout.strip() or p.stderr.strip()).splitlines()
        reason = (text[-1] if text else "exit {}".format(p.returncode))[:300]
    return passed, reason


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--python", required=True)
    a.add_argument("--release", required=True, help="the running Gateway release folder")
    a.add_argument("--ledger", default=r"C:\ProgramData\InferenceGateway\state\capacity-ledger.db")
    a.add_argument("--evidence", default=r"C:\ProgramData\InferenceGateway\backups")
    a.add_argument("--models-console", default=r"C:\ProgramData\AgentArenaLab\gateway\models-console")
    a.add_argument("--operator", required=True)
    a.add_argument("--report", required=True)
    a.add_argument("--dry-run", action="store_true")
    args = a.parse_args(argv)
    lines = []

    def say(t):
        print("  " + t, flush=True)
        lines.append(time.strftime("%H:%M:%S ") + t)

    def finish(code, summary):
        Path(args.report).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        Path(args.report).with_suffix(".txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return code

    launcher = Path(args.release) / "inference_gateway" / "service_launcher.py"
    if not launcher.exists() or not Path(args.ledger).exists():
        say("the Gateway's release or ledger is missing ({} / {}): nothing was changed".format(launcher, args.ledger))
        return finish(2, {"ok": False, "error": "missing release or ledger"})
    heads, pairs = read_state(args.ledger)
    blocked = do_not_use(args.models_console)
    skipped = [p for p in pairs if p in blocked]
    pairs = sorted((p for p in pairs if p not in blocked), key=lambda p: (head_order(p[0]), p[1]))
    say("{} embedding model(s) to put back on the heads (passed the embedding test before, off the list now){}".format(
        len(pairs), "; {} skipped, marked Do not use".format(len(skipped)) if skipped else ""))
    results = []
    for rid, model in pairs:
        if args.dry_run:
            say("would test {} on {}".format(model, rid))
            results.append({"resource_id": rid, "model": model, "passed": None})
            continue
        ok, reason = qualify(args.python, launcher, args.ledger, rid, model, args.operator, args.evidence)
        if not ok and "Quarantined" in reason:
            reason = "the head is held by the Gateway (it tests it again itself within about 25 minutes); run this again after"
        elif not ok and "allocated capacity" in reason:
            reason = "the head was busy with a request; run this again in a few minutes"
        say("{:<17} {:<26} {}".format(rid, model, "back" if ok else "NOT back: " + reason))
        results.append({"resource_id": rid, "model": model, "passed": ok, "reason": reason})
    heads, left = read_state(args.ledger)
    left = [p for p in left if p not in blocked]
    say("")
    for rid in sorted(heads, key=head_order):
        h = heads[rid]
        say("{:<17} {:<12} all-minilm {}".format(rid, h["health"], "qualified" if "all-minilm:latest" in h["qualified"] else "NOT qualified"))
    failed = [r for r in results if r["passed"] is False]
    summary = {"ok": not failed and not (left and not args.dry_run), "dry_run": args.dry_run, "results": results,
               "still_off": [{"resource_id": r, "model": m} for r, m in left], "skipped_do_not_use": [{"resource_id": r, "model": m} for r, m in skipped]}
    return finish(0 if summary["ok"] else 1, summary)


if __name__ == "__main__":
    sys.exit(main())
