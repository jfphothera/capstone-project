"""Let Capstone run on several heads at once, and test again the models head 10 never got to test (2026-11-12).

1. Capstone's limit. The Gateway gives an application at most max_concurrency requests at a time. Our policy had
   capstone at 1, so with Gateway 0.4.0's placement Capstone still used one head at a time. This sets it (and
   optionally capstone-research) through the Gateway's own bootstrap with a clients-only policy: every other field
   (models, queue limit, priority, operations, context) is copied from the Gateway's own credentials list, and no
   resources are named, so heads and their qualifications are untouched. The hub's copy of the approved policy
   (models-console\\gateway-policy-approved.json, which hub > 3 > 2 applies) gets the same numbers, after a dated copy
   of it is kept, so a later run does not set it back to 1.
2. Head 10. At 14:37 the Gateway did not test five models there (refused) and qwen3:14b ran out of time. They are
   tested again with the Gateway Models screen's own run_head_tests: the model is loaded on the head first, then the
   Gateway's test (embedding test for embedding models).
Nothing is deleted. Exit 0 all done, 1 something not, 2 could not run.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HEAD10 = [("head-192-0-2-10", m) for m in ("qwen3:14b", "qwen3:4b", "qwen3:8b", "smollm2:1.7b", "smollm2:135m", "smollm2:360m")]


def clients_policy(applications, limits, queue=None):
    """The bootstrap document that changes only these clients' max_concurrency (and queue_limit when given). Pure."""
    clients = []
    for a in applications:
        if a["app_id"] in limits:
            clients.append({"app_id": a["app_id"], "credential": "provisioned", "models": list(a["models"]),
                            "max_concurrency": int(limits[a["app_id"]]), "queue_limit": int(queue) if queue else a["queue_limit"], "priority": a["priority"],
                            "operations": a.get("operations"), "max_context": a.get("max_context")})
    return {"configuration_version": 1, "contract": "shared.inference/1", "clients": clients, "resources": []}


def changed_fields(before, after, limits):
    """Fields that differ other than the max_concurrency we set, per app (must be empty). Pure."""
    b = {a["app_id"]: a for a in before}
    out = {}
    for a in after:
        if a["app_id"] in b:
            diff = sorted(k for k in set(a) | set(b[a["app_id"]]) if k not in ("max_concurrency", "queue_limit") and a.get(k) != b[a["app_id"]].get(k))
            if diff:
                out[a["app_id"]] = diff
    return out


def update_approved_copy(path, limits, stamp, queue=None):
    """Set the same limits in the hub's approved policy copy, keeping a dated copy first. Returns a note."""
    p = Path(path)
    if not p.exists():
        return "no hub copy of the approved policy (hub > 3 > 2 uses the stick's): nothing to change"
    doc = json.loads(p.read_text(encoding="utf-8-sig"))
    shutil.copy2(p, p.with_name("gateway-policy-approved.before-concurrency-%s.json" % stamp))
    for c in doc.get("clients", []):
        if c.get("app_id") in limits:
            c["max_concurrency"] = int(limits[c["app_id"]])
            if queue:
                c["queue_limit"] = int(queue)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(p)
    return "hub copy of the approved policy updated (the old one kept as gateway-policy-approved.before-concurrency-%s.json)" % stamp


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("--python", required=True)
    a.add_argument("--release", required=True)
    a.add_argument("--engine", required=True, help="the stick's engine\\hub-tools\\gateway-models folder")
    a.add_argument("--ledger", default=r"C:\ProgramData\InferenceGateway\state\capacity-ledger.db")
    a.add_argument("--config", default=r"C:\ProgramData\InferenceGateway\config")
    a.add_argument("--evidence", default=r"C:\ProgramData\InferenceGateway\backups")
    a.add_argument("--models-console", default=r"C:\ProgramData\AgentArenaLab\gateway\models-console")
    a.add_argument("--operator", required=True)
    a.add_argument("--capstone", type=int, required=True)
    a.add_argument("--research", type=int, default=None)
    a.add_argument("--queue", type=int, default=None, help="queue_limit for the same clients (waiting requests)")
    a.add_argument("--report", required=True)
    a.add_argument("--skip-retest", action="store_true")
    args = a.parse_args(argv)
    sys.path.insert(0, args.engine)
    import gateway_models as gm  # the Gateway Models screen's own, tested code
    lines, summary = [], {"ok": False}

    def say(t):
        print("  " + t, flush=True)
        lines.append(time.strftime("%H:%M:%S ") + t)

    def finish(code):
        Path(args.report).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        Path(args.report).with_suffix(".txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return code

    stamp = time.strftime("%Y%m%d-%H%M%S")
    tools = gm.GatewayTools(args.python, args.release, args.ledger, args.evidence, args.operator)
    limits = {"capstone": args.capstone}
    if args.research is not None:
        limits["capstone-research"] = args.research
    if not all(1 <= n <= 1024 for n in limits.values()):
        say("a limit must be 1-1024 (the Gateway's range): nothing was changed")
        return finish(2)

    # 1. Capstone's limit, through the Gateway's own bootstrap.
    before = tools.applications()
    have = {x["app_id"]: x for x in before}
    missing = [k for k in limits if k not in have]
    if missing:
        say("the Gateway has no client %s: nothing was changed" % ", ".join(missing))
        return finish(2)
    for k in limits:
        say("%s: %s at a time now, %s may wait" % (k, have[k]["max_concurrency"], have[k]["queue_limit"]))
    policy = Path(args.config) / ("policy-concurrency-%s.json" % stamp)
    if args.queue is not None and not 1 <= args.queue <= 10000:
        say("the queue must be 1-10000: nothing was changed")
        return finish(2)
    policy.write_text(json.dumps(clients_policy(before, limits, args.queue), indent=2), encoding="utf-8")
    r = tools.bootstrap(policy)
    if not r["ok"]:
        say("the Gateway's bootstrap refused it: %s. Nothing else was changed." % (r["error"] or "exit %s" % r["exit"]))
        summary.update(bootstrap=r)
        return finish(1)
    after = tools.applications()
    now = {x["app_id"]: x["max_concurrency"] for x in after}
    other = changed_fields(before, after, limits)
    ok1 = all(now.get(k) == v for k, v in limits.items()) and not other
    queues = {x["app_id"]: x["queue_limit"] for x in after}
    for k, v in limits.items():
        say("%s: %s at a time %s, %s may wait" % (k, now.get(k), "(set)" if now.get(k) == v else "(NOT set: still %s)" % now.get(k), queues.get(k)))
    if args.queue is not None and any(queues.get(k) != args.queue for k in limits):
        ok1 = False
        say("the queue limit was NOT set to %s" % args.queue)
    if other:
        say("UNEXPECTED: other fields changed: %s" % other)
    try:
        say(update_approved_copy(Path(args.models_console) / "gateway-policy-approved.json", limits, stamp, args.queue))
    except (OSError, ValueError) as exc:
        ok1 = False
        say("the hub copy of the approved policy was not updated (%s): a later hub > 3 > 2 would set the limit back" % exc)
    summary.update(limits=limits, now=now, other_changes=other, policy=str(policy))

    # 2. Head 10's untested models, with the screen's warm-up first.
    if not ok1:
        say("the limits are not set: head 10 is not tested now")
    elif not args.skip_retest:
        say("")
        say("head 10: testing again the 6 models it did not get to test at 14:37 (each is loaded first; a few minutes)")
        res = gm.run_head_tests(tools, HEAD10, http=gm.ollama_request, say=say, again="run this again later")
        summary["head10"] = {"%s %s" % k: v for k, v in res.items()}
        for (rid, m), v in sorted(res.items()):
            say("%-17s %-14s %s" % (rid, m, "passed" if v.get("passed") else "not usable: " + str(v.get("why") or "")))
        summary["head10_passed"] = sum(1 for v in res.values() if v.get("passed"))
    # The result is the limits; head 10's retest is reported on its own lines and never turns a set limit into a failure.
    summary["ok"] = ok1
    return finish(0 if ok1 else 1)


if __name__ == "__main__":
    sys.exit(main())
