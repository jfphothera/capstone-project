"""Fleet Commit: the administrator half of the admin hub's Fleet window.

    "C:\\Program Files\\Python312\\python.exe" -I fleet_commit.py --commit <id>    (started by the admin hub)
    "C:\\Program Files\\Python312\\python.exe" -I fleet_commit.py --publish        (installer; after every Commit)

The admin hub (Agent Arena, as the standard account ArenaSvcUser) collects the operator's changes and,
on Commit, writes them to C:\\ProgramData\\AgentArenaLab\\fleet\\requests\\<id>.json and asks Windows to
start this helper as an administrator: one password prompt for the whole list (LabOperator1's). Arena never
holds administrator rights and never touches the Inference Gateway; this helper does, through the
Gateway's own tools, as the Gateway Models screen does.

This helper treats the request as untrusted: it copies it to its own administrators-only folder, checks
every change against the Gateway and the address plan, shows the exact list in its window, and runs
nothing until the operator types APPROVE there. Then, in this order:
  1. models each orchestrator may use     the Gateway's bootstrap (only "models" changes)
  2. which heads serve which project      the Gateway's heads tool, then bootstrap (also adds a new head)
  3. Do not use on one head               taken off that head's qualified list (Gateway Models' method)
  4. uninstall                            out of the Gateway first, then Ollama's /api/delete on the head
  5. install                              the hub's model library downloads once (hub Wi-Fi), then the head
                                          pulls the identical copy over the lab cable and is checked by digest
  6. tests                                the Gateway's qualify for every allowed model the changes touched: the heads
                                          side by side, one model at a time on each; each model is loaded on its head
                                          first (a cold load can outlast the test's fixed 90 s), a head an earlier
                                          timed-out test locked is unlocked with the Gateway's own reconcile once idle
Progress goes to fleet\\status\\<id>.json, which the admin hub shows. Afterwards it publishes
fleet\\published\\fleet-state.json: what the Gateway allows (models per project, projects per head, Do not
use, the library), with no credential, token or endpoint, so the admin hub can show it without reading the
Gateway's ledger.
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.2.0"
HERE = Path(__file__).resolve().parent
FLEET_DIR = Path(r"C:\ProgramData\AgentArenaLab\fleet")
LIBRARY_DIR = Path(r"C:\ProgramData\AgentArenaLab\model-library")
HUB = "192.0.2.254"
LIBRARY_PORT = 11500
FIREWALL_RULE = "Agent Arena Lab - model library (only during a Commit)"
REQUEST_SCHEMA = "agent-arena.fleet-request/1"
STATE_SCHEMA = "agent-arena.fleet-state/1"
REQUEST_ID = re.compile(r"^[a-f0-9]{8,32}$")
MAX_CHANGES = 200
ORDER = {"allow": 0, "disallow": 0, "head-projects": 1, "do-not-use": 2, "uninstall": 3, "install": 4, "test-missing": 5}


def _load(name):
    """Sibling modules, from this administrators-only folder (python -I does not put it on sys.path)."""
    path = next((p for p in (HERE / f"{name}.py", HERE.parent / "model-library" / f"{name}.py") if p.is_file()), HERE / f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gm = sys.modules.get("gateway_models") or _load("gateway_models")
ml = sys.modules.get("model_library") or _load("model_library")


# ---------------------------------------------------------------------------- pure helpers (tested)

def head_address(value):
    """'192.0.2.N' for a head address .2-.199, else None."""
    try:
        ip = ipaddress.IPv4Address(str(value))
    except ValueError:
        return None
    if ip not in gm.LAN:
        return None
    n = int(str(ip).rsplit(".", 1)[1])
    return str(ip) if n in gm.HEAD_RANGE else None


def short(address):
    return "head " + address.rsplit(".", 1)[1]


def validate(request, *, apps):
    """The request's changes, checked and in execution order, plus what is wrong with it. Pure."""
    problems, out, seen = [], [], set()
    if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
        return [], ["not a fleet request from the admin hub"]
    changes = request.get("changes")
    if not isinstance(changes, list) or not changes:
        return [], ["the request lists no changes"]
    if len(changes) > MAX_CHANGES:
        return [], [f"more than {MAX_CHANGES} changes in one Commit"]
    for c in changes:
        if not isinstance(c, dict):
            problems.append("a change is not an object")
            continue
        op = c.get("op")
        model = c.get("model")
        address = head_address(c.get("address")) if "address" in c else None
        if op == "test-missing":
            clean = {"op": op}
        elif op in ("allow", "disallow"):
            if c.get("app") not in apps:
                problems.append(f"{c.get('app')!r} is not a project the Gateway knows")
                continue
            if not isinstance(model, str) or not gm.MODEL_NAME.match(model):
                problems.append(f"{model!r} is not a model name")
                continue
            clean = {"op": op, "app": c["app"], "model": model}
        elif op == "head-projects":
            wanted = c.get("applications")
            if not address:
                problems.append(f"{c.get('address')!r} is not a head address (192.0.2.2-199)")
                continue
            if not isinstance(wanted, list) or not wanted or any(a not in apps for a in wanted):
                problems.append(f"{short(address)}: choose one or more projects the Gateway knows ({', '.join(sorted(apps))})")
                continue
            clean = {"op": op, "address": address, "applications": sorted(set(wanted))}
        elif op in ("do-not-use", "install", "uninstall"):
            if not address:
                problems.append(f"{c.get('address')!r} is not a head address (192.0.2.2-199)")
                continue
            if not isinstance(model, str) or not gm.MODEL_NAME.match(model):
                problems.append(f"{model!r} is not a model name")
                continue
            if op == "install":
                try:
                    ml.parse_name(model)
                except ValueError as exc:
                    problems.append(str(exc))
                    continue
            clean = {"op": op, "address": address, "model": model}
            if op == "do-not-use":
                if not isinstance(c.get("on"), bool):
                    problems.append(f"Do not use for {model} on {short(address)}: on must be true or false")
                    continue
                clean["on"] = c["on"]
        else:
            problems.append(f"{op!r} is not a change this helper makes")
            continue
        key = json.dumps({k: v for k, v in clean.items() if k not in ("on", "applications")}, sort_keys=True)
        if key in seen:
            problems.append("the same change is listed twice: " + describe(clean))
            continue
        seen.add(key)
        out.append(clean)
    pairs = {}
    for c in out:
        if c["op"] in ("install", "uninstall"):
            pairs.setdefault((c["address"], c["model"]), set()).add(c["op"])
        if c["op"] in ("allow", "disallow"):
            pairs.setdefault((c["app"], c["model"], "policy"), set()).add(c["op"])
    for key, ops in pairs.items():
        if len(ops) > 1:
            problems.append(f"{key[1]} is both added and removed in one Commit")
    return sorted(out, key=lambda c: ORDER[c["op"]]), problems


def describe(c):
    op = c["op"]
    if op == "allow":
        return f"Allow {c['model']} for {c['app']}"
    if op == "disallow":
        return f"Stop allowing {c['model']} for {c['app']}"
    if op == "head-projects":
        return f"{short(c['address'])} ({c['address']}) serves: {', '.join(c['applications'])}"
    if op == "do-not-use":
        return (f"Do not use {c['model']} on {short(c['address'])}" if c.get("on")
                else f"Use {c['model']} on {short(c['address'])} again")
    if op == "install":
        return f"Install {c['model']} on {short(c['address'])} (an exact copy from the hub's library)"
    if op == "uninstall":
        return f"Uninstall {c['model']} from {short(c['address'])} (out of the Gateway first)"
    if op == "test-missing":
        return "Test every allowed model that is not usable yet, on each Gateway head that has it"
    return op


def models_after(apps, changes):
    """{app: [models]} after the allow/disallow changes, and the apps that changed. Pure."""
    models = {a["app_id"]: list(a.get("models") or []) for a in apps}
    changed = set()
    for c in changes:
        if c["op"] == "allow" and c["model"] not in models[c["app"]]:
            models[c["app"]].append(c["model"])
            changed.add(c["app"])
        elif c["op"] == "disallow" and c["model"] in models[c["app"]]:
            models[c["app"]].remove(c["model"])
            changed.add(c["app"])
    return {a: sorted(m) for a, m in models.items()}, sorted(changed)


def tests_wanted(changes, *, models, head_apps, installed, qualified, blocked, failed_before):
    """(resource, model) pairs to test after the changes: each allowed model, on each Gateway head that has
    it installed and serves a project allowing it, that the changes touched, unless Do not use, already
    qualified, or failed before and not reinstalled now. Pure.

    models: {app: [models]} after; head_apps: {resource: [apps]} after (Gateway heads only);
    installed: {resource: set(models)} after; qualified: {resource: set}; blocked: set((resource, model)).
    """
    touched = set()
    for c in changes:
        op = c["op"]
        if op in ("allow",):
            touched.update((rid, c["model"]) for rid in head_apps)
        elif op == "head-projects":
            rid = gm.resource_for(c["address"])
            touched.update((rid, m) for app in c["applications"] for m in models.get(app, []))
        elif op == "do-not-use" and not c.get("on"):
            touched.add((gm.resource_for(c["address"]), c["model"]))
        elif op == "install":
            touched.add((gm.resource_for(c["address"]), c["model"]))
        elif op == "test-missing":
            touched.update((rid, m) for rid, apps in head_apps.items() for app in apps for m in models.get(app, []))
    reinstalled = {(gm.resource_for(c["address"]), c["model"]) for c in changes if c["op"] == "install"}
    out = []
    for rid, model in sorted(touched):
        if rid not in head_apps or (rid, model) in blocked:
            continue
        if model not in installed.get(rid, set()) or model in qualified.get(rid, set()):
            continue
        if not any(model in models.get(app, []) for app in head_apps[rid]):
            continue
        if (rid, model) in failed_before and (rid, model) not in reinstalled:
            continue
        # Embedding models are tested too (the Gateway's purpose "embedding": a real /api/embed); Capstone 6.0.0-rc.4
        # needs one for its retrieval. Until 1.4.9 they were skipped.
        out.append((rid, model))
    return out


def digest_of(value):
    v = (value or "").strip().lower()
    return v if v.startswith("sha256:") else ("sha256:" + v if v else "")


# ---------------------------------------------------------------------------- the heads' Ollama

def ollama(address, path, body=None, timeout=30, method=None):
    return gm.ollama_request(address, path, body, timeout=timeout, method=method)


def head_models(address):
    return {m["name"]: digest_of(m.get("digest")) for m in ollama(address, "/api/tags", timeout=10).get("models", []) if m.get("name")}


# ---------------------------------------------------------------------------- the ledger (read only)

LASTING_REFUSALS = ("Metadata does not support", "model not installed", "requires an observed model digest")


def judged(evidence):
    """Did this failed test judge the model? Only then is it "failed before" and skipped by later Commits. Pure.

    The Gateway judged it when the model answered (the evidence keeps the response) or when the Gateway refused the
    model for a lasting reason. A test that ran out of time while the model was loading (provisioner 1.5.1 and
    earlier, before the warm-up) never judged the model, so it is tested again."""
    if not isinstance(evidence, dict):
        return False
    if "response" in evidence:
        return True
    return any(t.lower() in str(evidence.get("error") or "").lower() for t in LASTING_REFUSALS)


def failure_detail(model, evidence, error=""):
    """(kind, reason) for a failed Gateway test, for the Fleet window. Pure.
      rule    - the model works, but the Gateway's own check refused its answer (Gateway 0.3.0: exactly READY);
      lasting - the Gateway refused it for a reason that a retest does not change;
      retest  - the test never judged the model (ran out of time, head locked or busy): "Test models not usable yet"
                tests it again."""
    ev = evidence if isinstance(evidence, dict) else {}
    err = str(ev.get("error") or error or "")
    if "response" in ev:
        ok, why = gm.judge_response(model, ev.get("response"))
        return ("rule", "works, but the Gateway refused its answer: " + why) if not ok else ("lasting", "the Gateway refused it: " + gm.one_line(err or "no reason recorded", 200))
    if any(t.lower() in err.lower() for t in LASTING_REFUSALS):
        return "lasting", gm.qualify_reason({"evidence_error": err})
    if gm.QUARANTINED_TEXT in err:
        return "retest", "not judged: the Gateway had locked this head after an earlier timed-out test. Test models not usable yet tests it again"
    if gm.BUSY_TEXT in err:
        return "retest", "not judged: the head was busy. Test models not usable yet tests it again"
    if err:
        return "retest", "not judged: " + gm.qualify_reason({"evidence_error": err}) + ". Test models not usable yet tests it again"
    return "retest", "not judged: the test ran out of time before the model answered (tests before 2026-11-08 did not load the model first). Test models not usable yet tests it again"


def ledger_evidence(ledger, evidence_ids):
    """{evidence_id: evidence dict} from the Gateway's ledger, read-only."""
    ids = [i for i in evidence_ids if i]
    if not ids:
        return {}
    db = sqlite3.connect(Path(ledger).resolve().as_uri() + "?mode=ro", uri=True)
    out = {}
    try:
        db.execute("PRAGMA query_only=ON")
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = "SELECT evidence_id, evidence FROM capacity_qualification_evidence WHERE evidence_id IN (%s)" % ",".join("?" * len(chunk))
            for eid, raw in db.execute(q, chunk).fetchall():
                try:
                    out[eid] = json.loads(raw)
                except (TypeError, ValueError):
                    out[eid] = None
    finally:
        db.close()
    return out


def ledger_heads(ledger):
    """{resource_id: [applications]} for the Gateway's inference heads, read-only (as the Gateway's own observe)."""
    db = sqlite3.connect(Path(ledger).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT resource_id, applications FROM capacity_resources WHERE resource_type='shared_inference'").fetchall()
    finally:
        db.close()
    return {rid: sorted(json.loads(apps or "[]")) for rid, apps in rows}


# ---------------------------------------------------------------------------- status the admin hub reads

class Status:
    def __init__(self, path, request_id):
        self.path, self.doc = Path(path) if path else None, {"id": request_id, "state": "checking", "helper": VERSION,
                                                              "started_at": time.time(), "lines": [], "results": []}
        self.lock = threading.RLock()                    # the heads' tests report from one thread per head
        self.write()

    def write(self):
        if not self.path:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.doc, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def say(self, text):
        with self.lock:
            line = time.strftime("%H:%M:%S ") + text
            print(line, flush=True)
            self.doc["lines"].append(line)
            self.doc["lines"] = self.doc["lines"][-400:]
            self.write()

    def result(self, change, ok, detail):
        with self.lock:
            self.doc["results"].append({"change": describe(change), "ok": bool(ok), "detail": detail})
            self.write()

    def state(self, value, **extra):
        with self.lock:
            self.doc.update(state=value, **extra)
            if value in ("done", "failed", "declined", "refused"):
                self.doc["finished_at"] = time.time()
            self.write()


# ---------------------------------------------------------------------------- the Commit

class Fleet:
    """The Gateway (its tools), the heads (their Ollama), the library and the published state."""

    def __init__(self, tools, console, library, fleet_dir, *, scanner=gm.scan_heads, http=ollama, tags=None, serve=None, firewall=None, hub=HUB):
        self.tools, self.console, self.library = tools, console, library
        self.fleet_dir = Path(fleet_dir)
        self.scanner, self.http, self.tags, self.hub = scanner, http, tags or head_models, hub
        self.serve = serve or self._serve
        self.firewall = firewall or self._firewall

    # -- library serving, only while installs run
    def _firewall(self, addresses, on):
        if on:
            subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in", "action=allow",
                            "protocol=TCP", f"localport={LIBRARY_PORT}", f"localip={self.hub}", "remoteip=" + ",".join(sorted(addresses)),
                            "profile=any"], capture_output=True, check=False)
        else:
            subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"], capture_output=True, check=False)

    def _serve(self, addresses):
        server = ml.build_server(self.library, self.hub, LIBRARY_PORT, set(addresses))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    # -- published state
    def publish(self, last_commit=None):
        snap = self.tools.snapshot()
        apps = self.tools.applications()
        try:
            per_head = ledger_heads(self.tools.ledger)
        except sqlite3.Error:
            per_head = {}
        heads = []
        for r in snap.get("resources", []):
            if r.get("resource_type") != "shared_inference" or not gm.head_address(r["resource_id"]):
                continue
            heads.append({"resource_id": r["resource_id"], "address": gm.head_address(r["resource_id"]),
                          "applications": per_head.get(r["resource_id"], []), "health": r.get("health"),
                          "qualified": list(r.get("qualified_models") or [])})
        evidence = gm.latest_evidence(snap.get("qualification_observations"))
        failed = sorted({f"{rid} {m}" for (rid, m), q in evidence.items() if not q.get("passed")})
        # Why and when each one failed, so the Fleet window can say it (and an old, never-judged test is not shown as
        # a verdict on the model).
        bad = {(rid, m): q for (rid, m), q in evidence.items() if not q.get("passed")}
        try:
            detail = ledger_evidence(self.tools.ledger, [q.get("evidence_id") for q in bad.values()])
        except (sqlite3.Error, OSError):
            detail = {}
        failures = []
        for (rid, m), q in sorted(bad.items()):
            kind, reason = failure_detail(m, detail.get(q.get("evidence_id")))
            failures.append({"resource_id": rid, "model": m, "kind": kind, "reason": reason, "at": q.get("at")})
        try:
            library = [{k: i[k] for k in ("name", "digest", "size", "complete", "fetched_at")} for i in self.library.index()]
        except (OSError, ValueError):
            library = []
        previous = self._read_state()
        doc = {"schema": STATE_SCHEMA, "written_at": time.time(), "written_by": f"Fleet Commit {VERSION} as {self.tools.operator}",
               "gateway": {"release": self.tools.release.name, "ledger_id": snap.get("ledger_id"), "admission_open": snap.get("admission_open")},
               "applications": [{"app_id": a["app_id"], "enabled": bool(a.get("enabled")), "models": sorted(a.get("models") or [])} for a in apps],
               "heads": sorted(heads, key=lambda h: int(h["address"].rsplit(".", 1)[1])),
               "do_not_use": [{"resource_id": rid, "model": m} for rid, m in sorted(self.console.blocked())],
               "failed_tests": failed, "failures": failures, "library": library,
               "last_commit": last_commit or previous.get("last_commit")}
        target = self.fleet_dir / "published" / "fleet-state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        return doc

    def _read_state(self):
        try:
            return json.loads((self.fleet_dir / "published" / "fleet-state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- the work
    def run(self, changes, status):
        failures = []
        apps = self.tools.applications()
        snap = self.tools.snapshot()
        qualified = {r["resource_id"]: set(r.get("qualified_models") or []) for r in snap.get("resources", [])
                     if r.get("resource_type") == "shared_inference"}
        evidence = gm.latest_evidence(snap.get("qualification_observations"))
        failed = {(rid, m): q for (rid, m), q in evidence.items() if not q.get("passed")}
        try:
            detail = ledger_evidence(self.tools.ledger, [q.get("evidence_id") for q in failed.values()])
        except Exception:                                # unreadable: keep the old rule (every failure counts)
            detail = None
        failed_before = {p for p, q in failed.items() if detail is None or judged(detail.get(q.get("evidence_id")))}
        if len(failed_before) < len(failed):
            status.say(f"{len(failed) - len(failed_before)} earlier failed test(s) never judged their model (they ran out of time loading it): tested again")
        head_apps = ledger_heads(self.tools.ledger)
        addresses = sorted({c["address"] for c in changes if "address" in c} |
                           {gm.head_address(rid) for rid in head_apps if gm.head_address(rid)}, key=lambda a: int(a.rsplit(".", 1)[1]))
        status.say(f"reading {len(addresses)} head(s) over the lab cable")
        scan = self.scanner(addresses)

        # 1. models per project
        models, changed = models_after(apps, changes)
        if changed:
            for app in changed:
                status.say(f"{app} may use: {', '.join(models[app])}")
                if not models[app]:
                    raise RuntimeError(f"{app} would have no model left: the Gateway needs at least one per project; nothing was changed")
            doc = gm.bootstrap_policy(apps, {app: models[app] for app in changed})
            pf = self.console.data / "evidence" / f"policy-{time.strftime('%Y%m%d-%H%M%S')}.json"
            pf.write_text(json.dumps(doc, indent=2), encoding="utf-8")
            r = self.tools.bootstrap(pf)
            for c in changes:
                if c["op"] in ("allow", "disallow"):
                    status.result(c, r["ok"], "the Gateway accepted it" if r["ok"] else (r["error"] or f"exit {r['exit']}"))
            if not r["ok"]:
                raise RuntimeError("the Gateway refused the model change: " + (r["error"] or f"exit {r['exit']}") + " (nothing else was done)")
            self.console._write_approved(models, apps)
            self.console.log({"action": "allowed models (Fleet Commit)", "models": {a: models[a] for a in changed}})
        else:
            for c in changes:
                if c["op"] in ("allow", "disallow"):
                    status.result(c, True, "already so; nothing to change")

        # 2. projects per head (this also adds a head the Gateway does not have yet)
        for c in [c for c in changes if c["op"] == "head-projects"]:
            address, rid = c["address"], gm.resource_for(c["address"])
            live = scan.get(address)
            if not live:
                status.result(c, False, "the head did not answer over the lab cable; nothing changed for it")
                failures.append(describe(c))
                continue
            if head_apps.get(rid) == c["applications"]:
                status.result(c, True, "already so")
                continue
            stamp = time.strftime("%Y%m%d-%H%M%S")
            ev = self.console.data / "evidence"
            inv = ev / f"inventory-{address}-{stamp}.json"
            inv.write_text(json.dumps(gm.head_inventory(address, live["version"], [{"name": n, "digest": d} for n, d in live["models"].items()],
                                                         self.console.computer), indent=2), encoding="utf-8")
            base = ev / f"base-{stamp}.json"
            base.write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [], "resources": []}), encoding="utf-8")
            review = ev / f"head-review-{address}-{stamp}.json"
            r = self.tools.heads(inv, base, c["applications"], review)
            if r["ok"]:
                r = self.tools.bootstrap(review)
            if r["ok"]:
                was = head_apps.get(rid)
                head_apps[rid] = c["applications"]
                qualified.setdefault(rid, set())
                status.result(c, True, ("added to the Gateway" if was is None else f"was {', '.join(was) or 'none'}"))
                self.console.log({"action": "head projects (Fleet Commit)", "address": address, "applications": c["applications"], "was": was})
            else:
                status.result(c, False, "the Gateway refused it: " + (r["error"] or f"exit {r['exit']}"))
                failures.append(describe(c))

        # 3. Do not use
        for c in [c for c in changes if c["op"] == "do-not-use"]:
            rid, model = gm.resource_for(c["address"]), c["model"]
            entries = self.console._read("do-not-use.json", {"entries": []})["entries"]
            if c["on"]:
                if not any(e["resource_id"] == rid and e["model"] == model for e in entries):
                    entries.append({"resource_id": rid, "model": model, "since": time.strftime("%Y-%m-%d %H:%M"), "by": self.tools.operator})
                    self.console._write("do-not-use.json", {"entries": entries})
                withdrawn = self.tools.withdraw(rid, model) if rid in head_apps else False
                qualified.get(rid, set()).discard(model)
                status.result(c, True, "taken out of use there" if withdrawn else "marked (it was not in use there)")
            else:
                self.console._write("do-not-use.json", {"entries": [e for e in entries if not (e["resource_id"] == rid and e["model"] == model)]})
                status.result(c, True, "cleared; tested again below if a project allows it")
            self.console.log({"action": "do not use (Fleet Commit)", "resource_id": rid, "model": model, "on": c["on"]})

        # 4. uninstall: out of the Gateway first, then off the head
        installed = {gm.resource_for(a): set(v["models"]) for a, v in scan.items()}
        for c in [c for c in changes if c["op"] == "uninstall"]:
            address, rid, model = c["address"], gm.resource_for(c["address"]), c["model"]
            try:
                if rid in head_apps:
                    self.tools.withdraw(rid, model)
                    qualified.get(rid, set()).discard(model)
                self.http(address, "/api/delete", {"model": model}, timeout=60, method="DELETE")
                left = self.tags(address)
                gone = model not in left
                installed.setdefault(rid, set()).discard(model)
                status.result(c, gone, "removed from the head" if gone else "the head still lists it")
                if not gone:
                    failures.append(describe(c))
            except Exception as exc:                     # one head's trouble does not stop the others
                status.result(c, False, f"{type(exc).__name__}: {exc}")
                failures.append(describe(c))
            self.console.log({"action": "uninstall (Fleet Commit)", "address": address, "model": model})

        # 5. install: the library once, then each head copies it
        installs = [c for c in changes if c["op"] == "install"]
        if installs:
            kept = {i["name"]: i for i in self.library.index() if i["complete"]}
            for model in sorted({c["model"] for c in installs}):
                if model in kept:
                    status.say(f"{model}: in the hub's library already ({kept[model]['size'] / 1e9:.2f} GB)")
                    continue
                status.say(f"{model}: downloading once to the hub's library (hub Wi-Fi)")
                try:
                    self.library.fetch(model, say=lambda t: status.say("  " + t))
                except Exception as exc:
                    status.say(f"{model}: download failed: {type(exc).__name__}: {exc}")
            kept = {i["name"]: i for i in self.library.index() if i["complete"]}
            targets = sorted({c["address"] for c in installs if c["model"] in kept}, key=lambda a: int(a.rsplit(".", 1)[1]))
            server = None
            if targets:
                self.firewall(targets, True)
                server = self.serve(targets)
            try:
                for c in installs:
                    address, rid, model = c["address"], gm.resource_for(c["address"]), c["model"]
                    if model not in kept:
                        status.result(c, False, "not in the hub's library (the download failed: is the hub's Wi-Fi connected?)")
                        failures.append(describe(c))
                        continue
                    item = kept[model]
                    host, ns, name, tag = ml.parse_name(model)
                    source = f"{self.hub}:{LIBRARY_PORT}/{ml.served_path(host, ns, name)}:{tag}"
                    try:
                        status.say(f"{short(address)}: copying {model} ({item['size'] / 1e9:.2f} GB) over the lab cable")
                        r = self.http(address, "/api/pull", {"model": source, "insecure": True, "stream": False},
                                      timeout=max(600, int(item["size"] / 4e6)))
                        if (r or {}).get("status") != "success":
                            raise RuntimeError(f"the head's pull answered {r!r}")
                        self.http(address, "/api/copy", {"source": source, "destination": model}, timeout=60)
                        self.http(address, "/api/delete", {"model": source}, timeout=60, method="DELETE")
                        now = self.tags(address)
                        same = digest_of(now.get(model)) == digest_of(item["digest"])
                        if same:
                            installed.setdefault(rid, set()).add(model)
                        status.result(c, same, ("installed; digest " + item["digest"][7:19] + " matches the library") if same
                                      else f"installed, but its digest {now.get(model)} is not the library's {item['digest']}")
                        if not same:
                            failures.append(describe(c))
                    except Exception as exc:
                        status.result(c, False, "the head refused the copy: " + gm.warmup_error(exc))
                        failures.append(describe(c))
                    self.console.log({"action": "install (Fleet Commit)", "address": address, "model": model})
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                if targets:
                    self.firewall(targets, False)

        # 6. the Gateway's tests
        pairs = tests_wanted(changes, models=models, head_apps=head_apps, installed=installed, qualified=qualified,
                             blocked=self.console.blocked(), failed_before=failed_before)
        if pairs:
            status.say(f"the Gateway tests {len(pairs)} head/model pair(s) on {len(gm.plan_head_tests(pairs))} head(s), the heads side by side: "
                       "each model is first loaded on its head (a large model can take several minutes), then the Gateway tests it")

            def log(rid, model, r):
                self.console.log({"action": "qualify (Fleet Commit)", "resource_id": rid, "model": model, "passed": r["passed"],
                                  "error": None if r["passed"] else r["why"],
                                  "load_seconds": None if r["load_seconds"] is None else round(r["load_seconds"], 1)})
            # Same steps as the Gateway Models screen (gm.run_head_tests): warm-up on the head, unlock a locked head, test.
            results = gm.run_head_tests(self.tools, pairs, http=self.http, say=status.say, log=log, again="Commit again later")
            failures.extend(f"test of {model} on {short(gm.head_address(rid))}" for rid, model in pairs if not results[(rid, model)]["passed"])
        else:
            results = {}
        for c in [c for c in changes if c["op"] == "test-missing"]:
            usable = sum(1 for r in results.values() if r["passed"])
            status.result(c, True, (f"{len(pairs)} head/model pair(s) tested, {usable} usable now; the lines above say why each other one is not"
                                    if pairs else "every allowed model on the Gateway's heads is usable already, or failed a real test before"))
        return failures


def commit(fleet, request_id, *, confirm=input, status_dir=None):
    """Read, check, show, ask, run, publish. Returns the process exit code."""
    if not REQUEST_ID.match(request_id or ""):
        print("Not a request from the admin hub.")
        return 2
    status = Status((status_dir or fleet.fleet_dir / "status") / f"{request_id}.json", request_id)
    src = fleet.fleet_dir / "requests" / f"{request_id}.json"
    try:
        raw = src.read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError("too large")
        request = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        status.state("refused", error=f"the request could not be read: {exc}")
        print(status.doc["error"])
        return 2
    # From here on only this copy counts; the requests folder is writable by the Arena account.
    keep = fleet.console.data / "commits" / f"{request_id}.json"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(raw)
    apps = {a["app_id"] for a in fleet.tools.applications()}
    changes, problems = validate(request, apps=apps)
    if problems:
        status.state("refused", error="; ".join(problems))
        print("This Commit was refused; nothing was changed:\n  - " + "\n  - ".join(problems))
        return 2
    print()
    print(f"Fleet Commit {VERSION} - {len(changes)} change(s) from the admin hub ({request.get('created_by', 'Arena')}):")
    for i, c in enumerate(changes, 1):
        print(f"  {i:>2}. {describe(c)}")
    print()
    status.state("waiting-approval", changes=[describe(c) for c in changes])
    answer = confirm("Type APPROVE and press Enter to make these changes (anything else cancels): ").strip()
    if answer != "APPROVE":
        status.state("declined")
        print("Cancelled; nothing was changed.")
        return 1
    status.state("running")
    try:
        failures = fleet.run(changes, status)
        state, ok = "done", not failures
        summary = "all changes made" if ok else f"{len(failures)} did not complete: " + "; ".join(failures)
    except Exception as exc:
        state, ok, summary = "failed", False, f"stopped: {exc}"
        status.say("STOPPED: " + str(exc))
    last = {"id": request_id, "finished_at": time.time(), "ok": ok, "summary": summary, "changes": len(changes)}
    try:
        fleet.publish(last_commit=last)
        status.say("the admin hub's view of the Gateway is updated")
    except Exception as exc:
        status.say(f"could not update the admin hub's view: {exc}")
    status.state(state, ok=ok, summary=summary)
    print()
    print(("DONE: " if ok else "FINISHED WITH PROBLEMS: ") + summary)
    return 0 if ok else 1


def build(args):
    release = Path(args.release) if args.release else gm.find_release(args.gateway_root, args.gateway_data)
    ledger = Path(args.ledger) if args.ledger else gm.find_ledger(args.gateway_data)
    if not ledger.is_file():
        raise SystemExit(f"The Gateway's ledger is not at {ledger} (Administration hub > 2 installs the Gateway).")
    tools = gm.GatewayTools(sys.executable, release, ledger, Path(args.data) / "evidence", os.environ.get("USERNAME", "operator"))
    console = gm.Console(tools, args.data)
    return Fleet(tools, console, ml.Library(args.library), args.fleet_dir, hub=args.hub)


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--commit", metavar="ID", help="run the admin hub's request with this ID")
    p.add_argument("--publish", action="store_true", help="publish the Gateway's state for the admin hub and stop")
    p.add_argument("--self-check", action="store_true", help="read the Gateway once and print a JSON summary (installer)")
    p.add_argument("--fleet-dir", default=str(FLEET_DIR))
    p.add_argument("--library", default=str(LIBRARY_DIR))
    p.add_argument("--data", default=str(gm.CONSOLE_DATA), help="the Gateway Models folder (administrators only)")
    p.add_argument("--gateway-root", default=str(gm.GATEWAY_ROOT))
    p.add_argument("--gateway-data", default=str(gm.GATEWAY_DATA))
    p.add_argument("--release")
    p.add_argument("--ledger", help="(rehearsal) a ledger other than the installed Gateway's")
    p.add_argument("--rehearsal", action="store_true", help="allow running without administrator rights against --ledger")
    p.add_argument("--hub", default=HUB, help="(rehearsal) the address the library serves heads from (default the hub, 192.0.2.254)")
    p.add_argument("--yes", action="store_true", help="(rehearsal only) answer APPROVE without asking")
    a = p.parse_args(argv)
    if not a.rehearsal and not gm.is_admin():
        print("Fleet Commit needs administrator rights; the admin hub starts it through Windows' administrator prompt.")
        return 1
    fleet = build(a)
    if a.self_check or a.publish:
        try:
            doc = fleet.publish()
            print(json.dumps({"ok": True, "version": VERSION, "published": str(fleet.fleet_dir / "published" / "fleet-state.json"),
                              "applications": {x["app_id"]: len(x["models"]) for x in doc["applications"]},
                              "heads": [h["address"] for h in doc["heads"]], "library": len(doc["library"])}))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
    if not a.commit:
        p.print_help()
        return 2
    title = "Fleet Commit - keep this window open until it says DONE"
    try:
        os.system(f"title {title}")
    except Exception:
        pass
    if a.yes and not a.rehearsal:
        print("--yes is for rehearsals only.")
        return 2
    code = commit(fleet, a.commit, confirm=(lambda _: "APPROVE") if a.yes else input)
    if not a.rehearsal:
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
