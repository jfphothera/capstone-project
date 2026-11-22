"""hub > 3 > 2 qualification (provisioner 1.6.7): test only the head/model pairs that are not usable now.

For each (head, model) the approved policy names and the head has installed: usable now = the model is on the head's
qualified list and the Gateway's newest real test of its purpose (embedding for an embedding model, completion for the
rest) on the head's current endpoint passed. Those are kept. Every other pair is tested with the Gateway Models
screen's run_head_tests: the model is loaded on the head first (so a slow first load never runs out the Gateway's
test time and holds the head), a head an earlier timed-out test locked is unlocked when idle, heads side by side, the
models of one head one after another, then the Gateway's own qualify. Gateway 0.4.1 passes every model that answers,
so the pairs 0.4.0's exact-READY test removed come back here. Reads the ledger read-only. Prints one line per result,
then writes the JSON summary to --out.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_models as gm  # noqa: E402

PRECHECK = ("Metadata does not support qualification operation", "Qualification model not installed",
            "Qualification requires an observed model digest", "Capability qualification requires",
            "Qualification deadline expired")


def purpose_of(model):
    return "completion" if gm.is_chat_model(model) else "embedding"


def refused(evidence):
    """A test the Gateway's own pre-checks refused (0.4.1 flags it; 0.4.0 rows recognised by their error). Pure."""
    if evidence.get("refused"):
        return True
    return not evidence.get("passed") and "response" not in evidence and any(t in (evidence.get("error") or "") for t in PRECHECK)


def usable_now(ledger):
    """{(resource_id, model, purpose)} that serve now. Read-only."""
    db = sqlite3.connect("file:{}?mode=ro".format(Path(ledger).as_posix()), uri=True, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        heads = {r["resource_id"]: (r["endpoint"], set(json.loads(r["qualified_models"] or "[]")))
                 for r in db.execute("SELECT resource_id, endpoint, qualified_models FROM capacity_resources")}
        newest = {}
        for r in db.execute("SELECT resource_id, model, passed, evidence, at FROM capacity_qualification_evidence ORDER BY at"):
            h = heads.get(r["resource_id"])
            e = json.loads(r["evidence"] or "{}")
            if not h or e.get("endpoint") != h[0] or refused(e):
                continue
            newest[(r["resource_id"], r["model"], e.get("purpose"))] = bool(r["passed"])
    finally:
        db.close()
    return {k for k, ok in newest.items() if ok and k[1] in heads[k[0]][1]}


def split(pairs, usable):
    """(to_test, kept): pairs [(resource_id, model)]. Pure."""
    test, kept = [], []
    for rid, model in pairs:
        (kept if (rid, model, purpose_of(model)) in usable else test).append((rid, model))
    return test, kept


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--python", required=True)
    a.add_argument("--release", required=True)
    a.add_argument("--ledger", required=True)
    a.add_argument("--evidence", required=True)
    a.add_argument("--operator", required=True)
    a.add_argument("--pairs", required=True, help="JSON file: [[resource_id, model], ...]")
    a.add_argument("--out", required=True)
    a.add_argument("--plan-only", action="store_true", help="only say which pairs would be tested")
    args = a.parse_args(argv)
    pairs = [tuple(p) for p in json.loads(Path(args.pairs).read_text(encoding="utf-8-sig"))]
    test, kept = split(pairs, usable_now(args.ledger))
    summary = {"ok": True, "kept": [list(p) for p in kept], "to_test": [list(p) for p in test], "results": []}
    if args.plan_only:
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("%d usable now (kept), %d to test" % (len(kept), len(test)), flush=True)
        return 0
    Path(args.evidence).mkdir(parents=True, exist_ok=True)
    tools = gm.GatewayTools(args.python, args.release, args.ledger, args.evidence, args.operator)
    res = gm.run_head_tests(tools, test, http=gm.ollama_request, say=lambda t: print(t, flush=True),
                            again="run Administration hub > 2 again later") if test else {}
    for (rid, model), r in sorted(res.items()):
        summary["results"].append({"resource": rid, "model": model, "purpose": purpose_of(model), "passed": bool(r.get("passed")),
                                   "why": r.get("why") or "", "kind": r.get("kind")})
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
