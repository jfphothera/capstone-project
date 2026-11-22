"""Hub pressure test: how many requests at once each test head handles best (2026-11-12).

Started with PRESSURE-TEST.cmd > 1 on the admin hub. It
  1. keeps a copy of every head's Gateway settings (applications, slots, memory) in baseline.json;
  2. optionally sends the test application's traffic (Capstone research) only to the test heads, by taking that
     application off the other heads' list through the Gateway's own bootstrap (qualifications are kept);
  3. sets the test heads to 1 slot and waits: the first request of the application on a test head starts the clock;
  4. runs each step for the chosen minutes (1 at a time, then 2, 3, ...), reading the Gateway's own records of every
     finished request (run time, queue time, Ollama's token counters) and sampling how many run at once;
  5. stops a head at the point of diminishing returns (two filled steps in a row that add less than the gain, 10 %),
     at a failure or a Gateway hold, when the traffic does not fill two steps, or at the top step;
  6. puts every head back exactly as in baseline.json and writes steps.csv and summary.txt.
Stopped early with PRESSURE-TEST.cmd > 0. Reads the ledger read-only; changes heads only through the Gateway's
bootstrap; reads no prompt or answer text (only numbers). Deletes nothing.
"""
import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

POLL = 15
CONTRACT = {"configuration_version": 1, "contract": "shared.inference/1"}
RESOURCE_KEYS = ("resource_id", "endpoint", "resource_type", "owner", "applications", "permitted_uses", "slots", "memory_gb")
# 1.6.14: the per-model capacity APPLY-MODEL-CAPACITY gave the Gateway (0.4.4 resources[].model_capacity). Every later
# bootstrap from the hub tools carries it again, so a pressure test or SET-PARALLEL-PER-HEAD never drops it.
MODEL_CAPACITY_FILE = r"C:\ProgramData\AgentArenaLab\gateway\model-capacity.json"
CSV_FIELDS = ["at", "head", "step", "minutes", "filled", "max_running_sampled", "max_running_measured", "completed", "failed",
              "requests_per_min", "output_tokens_per_s", "tokens_per_s_per_request", "run_s_avg", "run_s_p95", "queue_s_avg",
              "prompt_tokens_avg", "output_tokens_avg", "models", "apps", "loaded_mb", "decision"]


# ------------------------------------------------------------------ pure helpers (tested)

def resources_policy(resources, placement=None):
    """A bootstrap document that declares only these heads (no clients); model_capacity only where a head has it ({}
    clears what the Gateway stored; an absent key keeps it: Gateway 0.4.4); placement_policy only when given (1.6.23:
    APPLY-MODEL-CAPACITY on Gateway 0.4.5+; an absent block keeps what the Gateway stored). Pure."""
    return {**CONTRACT, "clients": [], **({"placement_policy": dict(placement)} if placement else {}), "resources": [dict({k: r[k] for k in RESOURCE_KEYS}, **({"model_capacity": r["model_capacity"]} if r.get("model_capacity") is not None else {}))
                                                     for r in resources]}


def model_capacity_policy(entries):
    """{resource_id: {model: {parallel, digest, runtime_version, num_ctx, evidence}}} from agent-arena.model-capacity/1
    entries: parallel = measured_parallel, and only for passed entries with a digest and a runtime version. Pure."""
    out = {}
    for e in entries or []:
        if not e.get("passed") or not e.get("digest") or not e.get("runtime_version") or not e.get("resource_id") or not e.get("model"):
            continue
        n = int(e.get("measured_parallel") or 0)
        if n < 1:
            continue
        out.setdefault(e["resource_id"], {})[e["model"]] = {"parallel": n, "digest": e["digest"], "runtime_version": e["runtime_version"],
                                                            "num_ctx": e.get("num_ctx"), "evidence": (e.get("evidence") or "")[:400]}   # the Gateway takes up to 400
    return out


def load_model_capacity(path):
    """model_capacity_policy of the hub's model-capacity.json; {} when absent or unreadable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("schema") != "agent-arena.model-capacity/1":
        return {}
    entries = doc.get("entries")
    if isinstance(entries, dict):  # 1.6.22: a one-entry document written as a single object
        entries = [entries]
    return model_capacity_policy([e for e in (entries or []) if isinstance(e, dict)])


def plan_heads(baseline, heads, app, slots, funnel):
    """The head declarations for a step: test heads at `slots`; with funnel, `app` off every other head. Pure."""
    out = []
    for r in baseline:
        r = dict(r)
        if r["resource_id"] in heads:
            r["slots"] = int(slots[r["resource_id"]]) if isinstance(slots, dict) else int(slots)
            if app not in r["applications"]:
                r["applications"] = sorted(set(r["applications"]) | {app})
        elif funnel:
            r["applications"] = [a for a in r["applications"] if a != app]
        out.append(r)
    return out


def max_overlap(intervals):
    """Most intervals (start, end) open at the same moment. Pure."""
    points = sorted([(s, 1) for s, e in intervals] + [(e, -1) for s, e in intervals], key=lambda p: (p[0], p[1]))
    best = cur = 0
    for _, d in points:
        cur += d
        best = max(best, cur)
    return best


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def step_metrics(records, failures, seconds):
    """Numbers for one head and step from the Gateway's records of finished requests. Pure.
    records: dicts with started_at, finished_at, queue_ms, model, app, eval_count, eval_duration (ns), prompt_eval_count."""
    run = [r["finished_at"] - r["started_at"] for r in records]
    out_tokens = [r.get("eval_count") or 0 for r in records]
    per_req = [(r["eval_count"] / (r["eval_duration"] / 1e9)) for r in records if r.get("eval_count") and r.get("eval_duration")]
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None
    return {
        "completed": len(records), "failed": len(failures),
        "requests_per_min": round(len(records) / (seconds / 60), 2) if seconds > 0 else None,
        "output_tokens_per_s": round(sum(out_tokens) / seconds, 2) if seconds > 0 else None,
        "tokens_per_s_per_request": round(avg(per_req), 2) if per_req else None,
        "run_s_avg": round(avg(run), 2) if run else None, "run_s_p95": round(pct(run, 0.95), 2) if run else None,
        "queue_s_avg": round(avg([(r.get("queue_ms") or 0) / 1000 for r in records]), 2) if records else None,
        "prompt_tokens_avg": round(avg([r.get("prompt_eval_count") or 0 for r in records]), 1) if records else None,
        "output_tokens_avg": round(avg(out_tokens), 1) if records else None,
        "max_running_measured": max_overlap([(r["started_at"], r["finished_at"]) for r in records]),
        "models": sorted({r["model"] for r in records}), "apps": sorted({r["app"] for r in records}),
    }


def knee(points, gain):
    """The point of diminishing returns in [(step, total tok/s), ...] (filled steps, in order): the last step that added
    at least `gain` (10 %) over the point before it. Returns (step, tps) or (None, None). Pure."""
    best = (None, None)
    for step, tps in points:
        if best[0] is None or (tps or 0) >= (best[1] or 0) * (1 + gain):
            best = (step, tps)
    return best


def decide(head_state, step, m, *, held, top, gain, sampled):
    """Whether this head goes on after `step`. Mutates head_state (points, flat, thin). Returns (go_on, text). Pure.
    Stops at the point of diminishing returns: two filled steps in a row that add less than `gain` over it."""
    filled = max(sampled, m["max_running_measured"]) >= step and m["completed"] > 0
    pts = head_state.setdefault("points", [])
    k = knee(pts, gain)
    if m["failed"] or held:
        return False, "stopped: %s at %d at once (point so far: %s at once)" % ("held by the Gateway" if held else "%d failed" % m["failed"], step, k[0])
    tps = m["output_tokens_per_s"] or 0
    if not filled:
        head_state["thin"] = head_state.get("thin", 0) + 1
        if head_state["thin"] >= 2:
            return False, "stopped: the traffic did not fill %d at once twice (send more work, then run again)" % step
        text = ("not filled: at most %d ran at once" % max(sampled, m["max_running_measured"])) if m["completed"] else "not filled: nothing finished in the step"
    else:
        head_state["thin"] = 0
        head_state["flat"] = 0 if (k[0] is None or tps >= (k[1] or 0) * (1 + gain)) else head_state.get("flat", 0) + 1
        pts.append((step, tps))
        k = knee(pts, gain)
        head_state["best"], head_state["best_step"] = k[1], k[0]
        if head_state["flat"] >= 2:
            top_tps = max(p[1] for p in pts)
            return False, "diminishing returns: %s at once is the point (%s output tokens/s); more adds under %d %% (highest %s)" % (
                k[0], k[1], round(gain * 100), top_tps)
        text = "filled"
    if step >= top:
        return False, "reached the top step %d (point so far: %s at once, %s output tokens/s)" % (top, k[0], k[1])
    return True, text


# ------------------------------------------------------------------ the Gateway and the heads

class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.launcher = Path(cfg["release"]) / "inference_gateway" / "service_launcher.py"

    def db(self):
        c = sqlite3.connect("file:{}?mode=ro".format(Path(self.cfg["ledger"]).as_posix()), uri=True, timeout=20)
        c.row_factory = sqlite3.Row
        return c

    def resources(self):
        with self.db() as c:
            cap = {r["resource_id"]: dict(r) for r in c.execute("SELECT * FROM capacity_resource_capacity")}
            out = []
            for r in c.execute("SELECT * FROM capacity_resources WHERE resource_type='shared_inference' AND health!='retired' ORDER BY resource_id"):
                k = cap.get(r["resource_id"], {})
                out.append({"resource_id": r["resource_id"], "endpoint": r["endpoint"], "resource_type": r["resource_type"], "owner": r["owner"],
                            "applications": json.loads(r["applications"]), "permitted_uses": json.loads(r["permitted_uses"]),
                            "slots": int(k.get("slots") or 1), "memory_gb": k.get("memory_gb"), "health": r["health"]})
        cap = load_model_capacity(self.cfg.get("model_capacity_file", MODEL_CAPACITY_FILE))
        for r in out:
            if r["resource_id"] in cap:
                r["model_capacity"] = cap[r["resource_id"]]
        return out

    def bootstrap(self, resources, folder, label, placement=None):
        f = Path(folder) / ("policy-%s-%s.json" % (label, time.strftime("%Y%m%d-%H%M%S")))
        f.write_text(json.dumps(resources_policy(resources, placement), indent=2), encoding="utf-8")
        p = subprocess.run([self.cfg["python"], "-I", str(self.launcher), "bootstrap", "--database", self.cfg["ledger"], "--policy", str(f)],
                           capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0:
            raise RuntimeError("the Gateway's bootstrap refused %s: %s" % (f.name, (p.stdout.strip() or p.stderr.strip())[-300:]))
        return f

    def first_request(self, heads, app, since):
        with self.db() as c:
            q = "SELECT min(created) FROM capacity_requests WHERE app_id=? AND created>=? AND resource_id IN (%s)" % ",".join("?" * len(heads))
            return c.execute(q, (app, since, *heads)).fetchone()[0]

    def running(self, heads):
        with self.db() as c:
            q = "SELECT resource_id, count(*) FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND executing=1 AND resource_id IN (%s) GROUP BY resource_id" % ",".join("?" * len(heads))
            got = dict(c.execute(q, tuple(heads)).fetchall())
        return {h: got.get(h, 0) for h in heads}

    def held(self, head):
        with self.db() as c:
            try:
                # Gateway 0.4.0 recovery.ACTIVE: the head, or a model on it, is suspended / held / needs attention.
                return bool(c.execute("SELECT 1 FROM capacity_suspensions WHERE resource_id=? AND state IN ('suspended','held','needs_attention')", (head,)).fetchone())
            except sqlite3.OperationalError:
                return False

    def finished(self, head, start, end):
        """Records of requests on `head` that FINISHED in [start, end), and failures that ended then (numbers only).
        By finish time, so a request longer than the window is counted where it ends, never lost."""
        recs, fails = [], []
        with self.db() as c:
            for r in c.execute("SELECT i.app_id, i.state, i.result, i.error, i.updated FROM capacity_invocations i JOIN capacity_requests q ON q.request_id=i.request_id "
                               "WHERE q.resource_id=? AND i.updated>=? AND i.updated<?", (head, start, end + 5)):
                if r["state"] == "completed" and r["result"]:
                    d = json.loads(r["result"])
                    if not (start <= (d.get("finished_at") or 0) < end):
                        continue
                    resp = d.get("response") or {}
                    recs.append({"started_at": d["started_at"], "finished_at": d["finished_at"], "queue_ms": d.get("queue_ms"), "model": d.get("model"),
                                 "app": r["app_id"], "eval_count": resp.get("eval_count"), "eval_duration": resp.get("eval_duration"),
                                 "prompt_eval_count": resp.get("prompt_eval_count")})
                elif r["state"] in ("failed", "unknown") and start <= r["updated"] < end:
                    fails.append({"app": r["app_id"], "state": r["state"], "error": (r["error"] or "")[:200]})
        return recs, fails

    def loaded_mb(self, endpoint):
        try:
            with urllib.request.urlopen(endpoint.rstrip("/") + "/api/ps", timeout=3) as resp:
                ps = json.loads(resp.read().decode("utf-8"))
            return round(sum(m.get("size") or 0 for m in ps.get("models", [])) / 2 ** 20)
        except Exception:
            return None


# ------------------------------------------------------------------ the run

class Run:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.cfg = json.loads((self.dir / "config.json").read_text(encoding="utf-8"))
        self.hub = Hub(self.cfg)

    def log(self, text):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + text
        with open(self.dir / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def status(self, **kw):
        s = self.read_status()
        s.update(kw, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        tmp = self.dir / "status.tmp"
        tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "status.json")

    def read_status(self):
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def stopping(self):
        return (self.dir / "stop.flag").exists()

    def prepare(self):
        if (self.dir / "baseline.json").exists():
            raise RuntimeError("this run already has a baseline")
        base = self.hub.resources()
        heads = self.cfg["heads"]
        missing = [h for h in heads if h not in {r["resource_id"] for r in base}]
        if missing:
            raise RuntimeError("not in the Gateway: " + ", ".join(missing))
        (self.dir / "baseline.json").write_text(json.dumps(base, indent=2), encoding="utf-8")
        self.hub.bootstrap(plan_heads(base, heads, self.cfg["app"], 1, self.cfg["funnel"]), self.dir, "start")
        self.log("baseline kept (%d heads); test heads %s at 1; %s" % (len(base), ", ".join(heads),
                 "%s only on the test heads" % self.cfg["app"] if self.cfg["funnel"] else "the other heads unchanged"))
        self.status(state="waiting", step=None, heads={h: {"step": 1, "decision": "waiting for the first request"} for h in heads})

    def restore(self, why):
        base = json.loads((self.dir / "baseline.json").read_text(encoding="utf-8"))
        self.hub.bootstrap(base, self.dir, "restore")
        self.log("every head restored as in baseline.json (%s)" % why)
        self.status(state="done", finished=why)

    def write_rows(self, rows):
        f = self.dir / "steps.csv"
        new = not f.exists()
        with open(f, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: (";".join(r[k]) if isinstance(r.get(k), list) else r.get(k)) for k in CSV_FIELDS})

    def summary(self):
        rows = []
        if (self.dir / "steps.csv").exists():
            with open(self.dir / "steps.csv", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        lines = ["PRESSURE TEST - %s" % self.cfg["started"], "heads %s, %s min per step, top %s, application %s%s" % (
            ", ".join(self.cfg["heads"]), self.cfg["minutes"], self.cfg["top"], self.cfg["app"], ", funnelled" if self.cfg["funnel"] else ""), ""]
        for h in self.cfg["heads"]:
            mine = [r for r in rows if r["head"] == h]
            lines.append("%s" % h)
            lines.append("  at once | filled | done | failed | out tok/s | tok/s per request | run s avg | run s p95 | queue s | decision")
            for r in mine:
                lines.append("  %7s | %6s | %4s | %6s | %9s | %17s | %9s | %9s | %7s | %s" % (r["step"], r["filled"], r["completed"], r["failed"],
                             r["output_tokens_per_s"], r["tokens_per_s_per_request"], r["run_s_avg"], r["run_s_p95"], r["queue_s_avg"], r["decision"]))
            filled = [r for r in mine if r["filled"] == "True" and r["output_tokens_per_s"] not in ("", None)]
            k_step, k_tps = knee([(int(r["step"]), float(r["output_tokens_per_s"])) for r in filled], float(self.cfg["gain"]))
            first = filled[0] if filled else None
            if k_step and first and float(first["output_tokens_per_s"]) > 0:
                at = next(r for r in filled if int(r["step"]) == k_step)
                lines.append("  POINT OF DIMINISHING RETURNS: %s at once, %s output tokens/s (%.1f x 1 at once); each request then %s tok/s, %s s on average" % (
                    k_step, k_tps, k_tps / float(first["output_tokens_per_s"]), at["tokens_per_s_per_request"], at["run_s_avg"]))
            elif not mine:
                lines.append("  no step finished")
            lines.append("")
        (self.dir / "summary.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    def run(self):
        heads, app, minutes, top = self.cfg["heads"], self.cfg["app"], float(self.cfg["minutes"]), int(self.cfg["top"])
        base = {r["resource_id"]: r for r in json.loads((self.dir / "baseline.json").read_text(encoding="utf-8"))}
        endpoints = {h: base[h]["endpoint"] for h in heads}
        try:
            self.log("waiting for the first %s request on a test head" % app)
            since = self.cfg["prepared_at"]
            while not self.stopping():
                t = self.hub.first_request(heads, app, since)
                if t:
                    break
                time.sleep(POLL)
            if self.stopping():
                return self.finish("stopped before any traffic")
            active = {h: {} for h in heads}
            slots = {h: 1 for h in heads}
            step = 1
            while active and not self.stopping():
                for h in active:
                    slots[h] = step
                plan = plan_heads(list(base.values()), heads, app, {h: (slots[h] if h in active else base[h]["slots"]) for h in heads}, self.cfg["funnel"])
                self.hub.bootstrap(plan, self.dir, "step%d" % step)
                start = time.time()
                self.log("step %d: %s at %d at once, %s min" % (step, ", ".join(active), step, minutes))
                self.status(state="running", step=step, step_started=time.strftime("%H:%M:%S", time.localtime(start)))
                sampled = {h: 0 for h in active}
                loaded = {h: None for h in active}
                while time.time() < start + minutes * 60 and not self.stopping():
                    for h, n in self.hub.running(list(active)).items():
                        sampled[h] = max(sampled[h], n)
                    for h in active:
                        mb = self.hub.loaded_mb(endpoints[h])
                        if mb is not None:
                            loaded[h] = max(loaded[h] or 0, mb)
                    time.sleep(POLL)
                end = time.time()
                if self.stopping() and end - start < 60:
                    break
                # The first fifth of a step (at most 2 minutes) settles: requests started under the step before finish then.
                measured = start + min(120.0, (end - start) * 0.2)
                rows = []
                for h in list(active):
                    recs, fails = self.hub.finished(h, measured, end)
                    m = step_metrics(recs, fails, end - measured)
                    go, text = decide(active[h], step, m, held=self.hub.held(h), top=top, gain=float(self.cfg["gain"]), sampled=sampled[h])
                    if fails:
                        text += "; first failure: " + fails[0]["error"]
                    rows.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "head": h, "step": step, "minutes": round((end - start) / 60, 1),
                                 "filled": max(sampled[h], m["max_running_measured"]) >= step, "max_running_sampled": sampled[h],
                                 "loaded_mb": loaded[h], "decision": text, **m})
                    self.log("  %s step %d: %d done, %d failed, %s out tok/s, %s tok/s per request: %s" % (
                        h, step, m["completed"], m["failed"], m["output_tokens_per_s"], m["tokens_per_s_per_request"], text))
                    if not go:
                        del active[h]
                self.write_rows(rows)
                self.summary()
                st = self.read_status().get("heads", {})
                for r in rows:
                    st[r["head"]] = {"step": r["step"], "decision": r["decision"], "out_tok_s": r["output_tokens_per_s"]}
                self.status(heads=st)
                step += 1
            return self.finish("stopped by the operator" if self.stopping() else "every head reached its end")
        except Exception as exc:
            self.log("ERROR: %s" % exc)
            try:
                return self.finish("error: %s" % exc)
            except Exception as exc2:
                self.log("RESTORE FAILED: %s - run PRESSURE-TEST.cmd > 0" % exc2)
                self.status(state="restore_failed", error=str(exc2))
                return 1

    def finish(self, why):
        self.restore(why)
        self.summary()
        return 0


def main(argv=None):
    a = argparse.ArgumentParser(description=__doc__)
    a.add_argument("verb", choices=["prepare", "run", "stop", "restore", "status", "summary"])
    a.add_argument("--dir", required=True)
    args = a.parse_args(argv)
    r = Run(args.dir)
    if args.verb == "prepare":
        r.prepare()
    elif args.verb == "run":
        return r.run()
    elif args.verb == "stop":
        (r.dir / "stop.flag").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    elif args.verb == "restore":
        if r.read_status().get("state") != "done":
            r.restore("restored by PRESSURE-TEST.cmd > 0")
        r.summary()
    elif args.verb == "summary":
        r.summary()
    print(json.dumps(r.read_status()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
