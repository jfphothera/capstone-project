"""Lab traffic: realistic TEST work through the Inference Gateway to the heads, shown live in Agent Arena.

    "C:\\Program Files\\Python312\\python.exe" -I lab_traffic.py         (from the "Lab Traffic" shortcut, as LabOperator1)

A menu: 1 set up (once), 2 run, 3 remove, 4 quit.

Set up, through the Gateway's own tools (administrator):
- a Gateway identity of its own, `lab-traffic`, issued with `credentials issue`. The token file stays in this tool's
  administrators-only folder. It is never Capstone's identity. Priority 1 (the lowest), at most 4 requests at a time,
  only models already qualified on the heads;
- every Gateway head serves it too (the heads tool, then bootstrap, as the Fleet Commit does); qualifications are kept;
- Arena: the producer credential for the Arena application `lab-traffic`, from a one-time app code. Add the
  application first in the admin hub: Machines > Machines and codes > Applications.

Run: small TEST support-ticket cases at a realistic pace. A case goes intake -> evidence research -> quality review.
Each step is one real, short Gateway request, pinned to a head that has the model. Arena shows each agent leaving the
orchestrator, waiting at the head, working, and coming back. Every label starts with "TEST:".

Remove: revokes the Gateway identity and takes it off the heads. The Arena application stays; retire it in the
admin hub if wanted.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
DATA = Path(r"C:\ProgramData\AgentArenaLab\lab-traffic")
APP = "lab-traffic"
ORCH = "main"
DISPLAY = "Lab traffic (TEST)"
GATEWAY_URL = "https://192.0.2.254:8443"
GATEWAY_CA = Path(r"C:\ProgramData\InferenceGateway\tls\inference-gateway-ca.pem")
ARENA_URL = "http://192.0.2.254:8790"
SERVICE_ID = "shared-inference"
CONTRACT = "shared.inference/1"
MAX_MODELS = 4
AGENTS = (("lt_intake", "TEST Intake Clerk", "intake", "triage"),
          ("lt_research", "TEST Evidence Researcher", "controlled-evidence", "retrieval"),
          ("lt_review", "TEST Quality Reviewer", "validation", "review"))
TICKETS = (
    "Customer reports the lamp module shows error E-214 after the latest firmware update; the unit restarts every few minutes.",
    "Shipment of 40 filter cartridges arrived with 6 cracked housings; the customer asks for replacement and a root cause.",
    "Clinic says the calibration report is missing from the last service visit and an audit is next week.",
    "Invoice lists the extended warranty twice; the customer wants a corrected invoice before paying.",
    "Field engineer notes the cooling fan is louder than normal on two units from the same production lot.",
    "Distributor asks whether the new power supply is compatible with devices built before 2024.",
    "User manual page for cleaning instructions contradicts the label on the device; which one is right?",
    "Hospital requests the declaration of conformity and the latest risk assessment summary for a tender.",
)
STEPS = (
    ("lt_intake", "triage", "Classify this support ticket into exactly one category (hardware, software, shipping, billing, "
                            "quality complaint, documentation) and give a one-sentence reason.\nTicket: {ticket}"),
    ("lt_research", "retrieval", "List the two most likely causes or the two documents needed for this support ticket, one short "
                                 "line each.\nTicket: {ticket}"),
    ("lt_review", "review", "In two sentences, say whether this ticket can be answered today and what the first action is.\n"
                            "Ticket: {ticket}"),
)


def _load(name, folder=HERE):
    path = folder / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _arena():
    """Agent Arena's own client (standard library), carried beside this tool in arena_service\\."""
    if "arena_service.client" in sys.modules:
        return sys.modules["arena_service.client"], sys.modules["arena_service.secretfile"]
    sys.path.insert(0, str(HERE))       # this administrators-only folder
    from arena_service import client, secretfile   # noqa: E402
    return client, secretfile


# ---------------------------------------------------------------------------- pure helpers (tested)

def pick_models(snapshot, is_chat, limit=MAX_MODELS):
    """Chat models already qualified on a healthy Gateway head, most widely qualified first. Pure."""
    count = {}
    for r in snapshot.get("resources", []):
        if r.get("resource_type") != "shared_inference" or r.get("health") != "healthy":
            continue
        for m in r.get("qualified_models") or []:
            if is_chat(m):
                count[m] = count.get(m, 0) + 1
    return [m for m, _ in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def traffic_policy(models):
    """The approved-policy entry the Gateway's `credentials issue` reads for lab-traffic. Pure."""
    return {"configuration_version": 1, "contract": CONTRACT,
            "clients": [{"app_id": APP, "credential": "provisioned", "models": list(models), "max_concurrency": 4,
                         "queue_limit": 8, "priority": 1, "operations": ["/api/generate", "/api/chat"], "max_context": 8192}],
            "resources": []}


def choose_target(resources, rng, busy=()):
    """(resource_id, model) for the next step: a head this identity may use that lists a model, preferring a head
    that is not already running one of our steps. Pure given rng."""
    pairs = [(r["resource_id"], m) for r in resources for m in r.get("models") or []]
    if not pairs:
        return None
    free = [p for p in pairs if p[0] not in busy]
    return rng.choice(free or pairs)


class Events:
    """arena.v2 events for the TEST cases; every label starts with TEST:."""

    def __init__(self, run=None, clock=None):
        self.run = run or uuid.uuid4().hex[:8]
        self.n = 0
        self.clock = clock
        self.home = f"orch:{APP}/{ORCH}"

    def ev(self, type_, **fields):
        self.n += 1
        from datetime import datetime, timezone
        at = datetime.fromtimestamp(self.clock() if self.clock else time.time(), timezone.utc).isoformat()
        return dict(type=type_, event_id=f"lt-{self.run}-{self.n}", occurred_at=at, orchestrator_id=ORCH, **fields)

    def opening(self):
        return [self.ev("application.updated", display_name=DISPLAY),
                self.ev("orchestrator.heartbeat", display_name="Lab traffic orchestrator (TEST)", heartbeat_seconds=30),
                *[self.ev("agent.registered", agent_id=a, display_name=name, role=role) for a, name, role, _ in AGENTS]]

    def created(self, aid, agent, case, action, step_name):
        return self.ev("assignment.created", assignment_id=aid, agent_id=agent, kind="orchestration", state="executing",
                       workflow_id=f"TEST-{case}", task_id=aid, visual_action=action, label=f"TEST: {case} {step_name}")

    def waiting_gateway(self, aid, model):
        return self.ev("assignment.state", assignment_id=aid, state="waiting", wait_reason="capacity", visual_action="waiting_model",
                       model_id=model, label="TEST: waiting for the Gateway")

    def to_head(self, aid, head, model):
        return self.ev("assignment.moved", assignment_id=aid, from_location=self.home, to_location="head:" + head, kind="inference",
                       state="waiting", wait_reason="model", visual_action="waiting_model", model_id=model, lease_seconds=180,
                       label="TEST: at the head, model loading")

    def working(self, aid, model):
        return self.ev("assignment.state", assignment_id=aid, state="executing", visual_action="inference", model_id=model,
                       lease_seconds=180, label="TEST: model answering")

    def home_again(self, aid, head, action):
        return self.ev("assignment.moved", assignment_id=aid, from_location="head:" + head, to_location=self.home,
                       kind="orchestration", state="executing", visual_action=action, label="TEST: answer back at the orchestrator")

    def done(self, aid, ok, why=""):
        return self.ev("assignment.state", assignment_id=aid, state="completed" if ok else "failed",
                       visual_action="completion" if ok else "error_handling", label=("TEST: done" if ok else f"TEST: failed {why}")[:80])

    def handoff(self, sender, receiver, case, aid):
        return [self.ev("handoff.sent", sender_agent_id=sender, receiver_agent_id=receiver, correlation_id=f"TEST-{case}", assignment_id=aid),
                self.ev("handoff.received", sender_agent_id=sender, receiver_agent_id=receiver, correlation_id=f"TEST-{case}", assignment_id=aid)]

    def heartbeat(self, aid=None):
        return self.ev("assignment.heartbeat", assignment_id=aid, lease_seconds=180) if aid else self.ev("orchestrator.heartbeat")


def safe_reason(text):
    """A Gateway error in Arena's label alphabet (letters, digits, a little punctuation). Pure."""
    import re
    t = re.sub(r"[^A-Za-z0-9 .,:;()/_'-]", " ", str(text or "")).strip()
    t = re.sub(r"\d{7,}", "", t)
    return t[:60]


# ---------------------------------------------------------------------------- the Gateway, as the lab-traffic client

class Gateway:
    def __init__(self, url, app, token, ca, timeout=30):
        self.url, self.app, self.token, self.timeout = url.rstrip("/"), app, token, timeout
        if not self.url.startswith("https://") and not self.url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("the Gateway is reached over HTTPS (plain HTTP only on this computer, for rehearsals)")
        self.ctx = ssl.create_default_context(cafile=str(ca)) if ca else ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.ctx))

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"X-Application-ID": self.app, "X-Infrastructure-Token": self.token,
                                              **({"Content-Type": "application/json"} if data else {})})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if r.headers.get("X-Inference-Service-ID") != SERVICE_ID:
                    raise RuntimeError("the answer did not come from the shared-inference Gateway")
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            try:
                msg = json.loads(exc.read()).get("error")
            except ValueError:
                msg = ""
            raise RuntimeError(f"Gateway {exc.code}: {msg}") from None

    def models(self):
        return self.call("GET", "/v1/models").get("resources", [])

    def submit(self, model, head, prompt, key):
        body = {"contract": CONTRACT, "client_key": key, "model": model, "nodes": [head], "deadline": time.time() + 900,
                "operation": "/api/generate",
                "payload": {"prompt": prompt, "think": False, "options": {"num_predict": 120, "temperature": 0.3, "num_ctx": 2048}}}
        return self.call("POST", "/v1/submit", body)

    def status(self, rid):
        return self.call("GET", f"/v1/requests/{rid}")


# ---------------------------------------------------------------------------- the traffic

class Traffic:
    """Cases in flight, each a chain of three Gateway requests. `pace` scales the waits between cases and steps."""

    def __init__(self, gateway, publish, *, minutes=15.0, cases_at_once=4, pace=1.0, seed=None, out=print, clock=time.time, sleep=time.sleep):
        self.gw, self.publish_raw, self.out = gateway, publish, out
        self.minutes, self.cases_at_once, self.pace = minutes, cases_at_once, pace
        self.rng = random.Random(seed)
        self.clock, self.sleep = clock, sleep
        self.events = Events(clock=clock)
        self.tally = {"steps": 0, "completed": 0, "failed": 0, "cases": 0}
        self.heads_used = {}

    def publish(self, events):
        if events:
            try:
                for r in self.publish_raw(events).get("results", []):
                    if r.get("status") not in ("accepted", "duplicate"):
                        self.out(f"  Arena did not accept {r.get('event_id')}: {r.get('status')} {r.get('reason', '')}")
            except Exception as exc:          # Arena down never stops the Gateway work
                self.out(f"  Arena: {type(exc).__name__}: {exc}")

    def run(self):
        start = self.clock()
        end = start + self.minutes * 60
        self.publish(self.events.opening())
        resources = self.gw.models()
        if not resources:
            raise RuntimeError("the Gateway offers lab-traffic no head and model right now (heads busy, unhealthy, or not set up)")
        self.out(f"heads and models offered: " + "; ".join(f"{r['resource_id']}: {', '.join(r.get('models') or [])}" for r in resources))
        cases, next_case, beat, refreshed = [], start, start, start
        n = 0
        while cases or self.clock() < end:
            now = self.clock()
            if now - refreshed > 120:
                try:
                    resources = self.gw.models() or resources
                except Exception as exc:
                    self.out(f"  Gateway models: {exc}")
                refreshed = now
            if now < end and now >= next_case and len(cases) < self.cases_at_once:
                n += 1
                case = f"C{n:03d}-{self.events.run[:4]}"
                cases.append({"case": case, "ticket": self.rng.choice(TICKETS), "step": 0, "phase": "start", "at": now})
                self.tally["cases"] += 1
                next_case = now + self.rng.uniform(40, 90) * self.pace
                self.out(f"{time.strftime('%H:%M:%S')} new case {case}")
            for c in list(cases):
                self._advance(c, resources, cases)
                if c.get("finished"):
                    cases.remove(c)
            if now - beat > 15:
                self.publish([self.events.heartbeat()] + [self.events.heartbeat(c["aid"]) for c in cases if c.get("aid") and c["phase"] in ("leased", "active")])
                beat = now
            self.sleep(1.0)
        self.out(f"done: {self.tally}; heads used: {self.heads_used}")
        return self.tally

    def _advance(self, c, resources, cases):
        now = self.clock()
        agent, action, template = STEPS[c["step"]]
        if c["phase"] == "start":
            busy = {x.get("head") for x in cases if x is not c and x.get("phase") in ("submitted", "leased", "active")}
            target = choose_target(resources, self.rng, busy)
            if not target:
                c.update(phase="failed-start", finished=True)
                return
            head, model = target
            aid = f"TEST-{c['case']}-{c['step'] + 1}"
            c.update(aid=aid, head=head, model=model)
            self.publish([self.events.created(aid, agent, c["case"], action, ("intake", "research", "review")[c["step"]])])
            try:
                env = self.gw.submit(model, head, template.format(ticket=c["ticket"]), f"{aid}-{self.events.run}")
                c.update(rid=env["request"]["request_id"], phase="submitted", at=now)
                self.tally["steps"] += 1
                self.publish([self.events.waiting_gateway(aid, model)])
                self.out(f"  {aid}: {agent} -> {model} on {head}")
            except Exception as exc:
                self._finish_step(c, False, safe_reason(exc), cases)
            return
        if c["phase"] in ("submitted", "leased", "active"):
            try:
                env = self.gw.status(c["rid"])
            except Exception as exc:
                self.out(f"  {c['aid']}: status {exc}")
                return
            st, req = env.get("status"), env.get("request") or {}
            head = req.get("resource_id") or c["head"]
            if c["phase"] == "submitted" and (st in ("leased", "active", "completed") or req.get("resource_id")):
                c.update(phase="leased", head=head)
                self.heads_used[head] = self.heads_used.get(head, 0) + 1
                self.publish([self.events.to_head(c["aid"], head, c["model"])])
            if c["phase"] == "leased" and (st in ("active", "completed") or env.get("execution_started_at")):
                c["phase"] = "active"
                self.publish([self.events.working(c["aid"], c["model"])])
            if st == "completed":
                self.publish([self.events.home_again(c["aid"], head, action)])
                self._finish_step(c, True, "", cases)
            elif st in ("failed", "cancelled", "expired", "unknown"):
                self._finish_step(c, False, safe_reason(env.get("error") or st), cases)
            elif now - c["at"] > 900:
                self._finish_step(c, False, "no answer in 15 minutes", cases)
            return
        if c["phase"] == "pause" and now >= c["resume"]:
            c["phase"] = "start"

    def _finish_step(self, c, ok, why, cases):
        self.publish([self.events.done(c["aid"], ok, why)])
        self.tally["completed" if ok else "failed"] += 1
        self.out(f"  {c['aid']}: {'done' if ok else 'FAILED ' + why}")
        if ok and c["step"] < len(STEPS) - 1:
            sender = STEPS[c["step"]][0]
            c["step"] += 1
            self.publish(self.events.handoff(sender, STEPS[c["step"]][0], c["case"], c["aid"]))
            c.update(phase="pause", resume=self.clock() + self.rng.uniform(5, 15) * self.pace)
        else:
            c["finished"] = True


# ---------------------------------------------------------------------------- set up, run, remove (on the hub)

def _tools():
    gm = sys.modules.get("gateway_models") or _load("gateway_models")
    fc = sys.modules.get("fleet_commit") or _load("fleet_commit")      # it loads model_library itself
    args = argparse.Namespace(release=None, ledger=None, gateway_root=str(gm.GATEWAY_ROOT), gateway_data=str(gm.GATEWAY_DATA),
                              data=str(gm.CONSOLE_DATA), library=str(fc.LIBRARY_DIR), fleet_dir=str(fc.FLEET_DIR), hub=fc.HUB)
    return gm, fc, fc.build(args)


def _restrict(folder):
    import subprocess
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/Q"],
                   capture_output=True, check=False)


def setup(ask=input):
    gm, fc, fleet = _tools()
    _restrict(DATA)
    token = DATA / "lab-traffic.token"
    apps = {a["app_id"]: a for a in fleet.tools.applications()}
    if APP not in apps:
        models = pick_models(fleet.tools.snapshot(), gm.is_chat_model)
        if not models:
            raise RuntimeError("no chat model is qualified on a healthy Gateway head yet: nothing to send traffic to")
        print(f"The Gateway issues lab-traffic its own login (priority 1, at most 4 at a time) for: {', '.join(models)}")
        pf = DATA / "lab-traffic-policy.json"
        pf.write_text(json.dumps(traffic_policy(models), indent=2), encoding="utf-8")
        token.unlink(missing_ok=True)
        r = fleet.tools.run("credentials", "issue", "--database", str(fleet.tools.ledger), "--app-id", APP, "--policy", str(pf), "--output", str(token))
        if not r["ok"]:
            raise RuntimeError("the Gateway refused the lab-traffic login: " + (r["error"] or f"exit {r['exit']}"))
        print("  issued (the token stays in " + str(DATA) + ")")
    elif not apps[APP].get("enabled") or not token.is_file():
        token.unlink(missing_ok=True)
        r = fleet.tools.run("credentials", "rotate", "--database", str(fleet.tools.ledger), "--app-id", APP, "--output", str(token), "--enable")
        if not r["ok"]:
            raise RuntimeError("the Gateway did not renew the lab-traffic login: " + (r["error"] or f"exit {r['exit']}"))
        print("  lab-traffic login renewed and enabled")
    else:
        print("  the Gateway already has the lab-traffic login")
    head_apps = fc.ledger_heads(fleet.tools.ledger)
    changes = [{"op": "head-projects", "address": gm.head_address(rid), "applications": sorted(set(a) | {APP})}
               for rid, a in head_apps.items() if APP not in a and gm.head_address(rid)]
    if changes:
        print(f"Letting {len(changes)} head(s) serve lab-traffic too (the Gateway's heads tool, then bootstrap; qualifications are kept)")
        status = fc.Status(DATA / "setup-status.json", "setup")
        failures = fleet.run(changes, status)
        for f in failures:
            print("  not done: " + f)
    fleet.publish()
    client, secretfile = _arena()
    cred = DATA / "arena-producer.dpapi"
    if not cred.is_file():
        print()
        print("Arena: in the admin hub (ArenaSvcUser's screen), Machines > Machines and codes... > Applications:")
        print(f"  add the application ID {APP}, name {DISPLAY} (if it is not there), then click App code on its row.")
        code = ask("Type the APP CODE here (or press Enter to do this later): ").strip()
        if code:
            cid, secret, answer = client.enroll_app(ARENA_URL, APP, ORCH, code)
            secretfile.write(str(cred), cid, secret)
            print(f"  Arena accepted it ({answer.get('service_id')})")
        else:
            print("  Arena left for later: run Set up again with an app code. Traffic still runs through the Gateway.")
    print("Set up complete.")


def run(minutes, pace=1.0):
    client, secretfile = _arena()
    token = (DATA / "lab-traffic.token").read_text(encoding="utf-8").strip()
    gw = Gateway(GATEWAY_URL, APP, token, GATEWAY_CA)
    cred = DATA / "arena-producer.dpapi"
    if cred.is_file():
        cid, secret = secretfile.read(str(cred))
        arena = client.Client(ARENA_URL, cid, secret)
        publish = arena.publish
    else:
        print("Arena is not set up for lab-traffic: the work runs through the Gateway but will not show in Arena.")
        publish = lambda events: {"results": []}
    print(f"Lab traffic for {minutes:g} minutes: new TEST case every 40-90 s, at most 4 at a time. Ctrl+C stops.")
    t = Traffic(gw, publish, minutes=minutes, pace=pace, cases_at_once=4, out=lambda s: print(s, flush=True))
    try:
        return t.run()
    except KeyboardInterrupt:
        print("stopped by the operator")
        return t.tally


def remove():
    gm, fc, fleet = _tools()
    head_apps = fc.ledger_heads(fleet.tools.ledger)
    changes = [{"op": "head-projects", "address": gm.head_address(rid), "applications": sorted(set(a) - {APP})}
               for rid, a in head_apps.items() if APP in a and len(set(a) - {APP}) and gm.head_address(rid)]
    if changes:
        fleet.run(changes, fc.Status(DATA / "remove-status.json", "remove"))
    apps = {a["app_id"] for a in fleet.tools.applications()}
    if APP in apps:
        r = fleet.tools.run("credentials", "revoke", "--database", str(fleet.tools.ledger), "--app-id", APP)
        print("Gateway login revoked" if r["ok"] else "revoke refused: " + (r["error"] or ""))
    (DATA / "lab-traffic.token").unlink(missing_ok=True)
    fleet.publish()
    print(f"Removed. The Arena application {APP} stays; retire it in the admin hub if you want it gone.")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=15.0)
    p.add_argument("--pace", type=float, default=1.0, help="scale the waits (2 = half the traffic)")
    p.add_argument("action", nargs="?", choices=("setup", "run", "remove"))
    a = p.parse_args(argv)
    gm = sys.modules.get("gateway_models") or _load("gateway_models")
    if not gm.is_admin():
        print("Lab Traffic needs administrator rights: start it from its shortcut and click Yes.")
        return 1
    action = a.action
    while True:
        if not action:
            print(f"\nLab Traffic {VERSION}: 1 Set up (once)   2 Run for {a.minutes:g} minutes   3 Remove   4 Quit")
            action = {"1": "setup", "2": "run", "3": "remove"}.get(input("Choose: ").strip())
            if not action:
                return 0
        try:
            {"setup": setup, "run": lambda: run(a.minutes, a.pace), "remove": remove}[action]()
        except Exception as exc:
            print(f"STOPPED: {exc}")
        action = None


if __name__ == "__main__":
    raise SystemExit(main())
