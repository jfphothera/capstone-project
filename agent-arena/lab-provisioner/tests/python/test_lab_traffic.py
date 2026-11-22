"""Lab Traffic: TEST work through the Gateway, shown in Arena. Every event is checked against Arena's own contract,
and a whole run is driven against a Gateway stand-in with a simulated clock."""
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "lab-provisioner" / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "gateway-models"))
sys.path.insert(0, str(TOOLS / "model-library"))
sys.path.insert(0, str(ROOT))
import gateway_models as gm  # noqa: E402
import lab_traffic as lt  # noqa: E402
from arena_service import contract  # noqa: E402


def test_models_are_those_already_qualified_on_healthy_heads():
    snap = {"resources": [
        {"resource_id": "head-192-0-2-3", "resource_type": "shared_inference", "health": "healthy", "qualified_models": ["qwen3.5:4b", "gemma3:4b", "nomic-embed-text:latest"]},
        {"resource_id": "head-192-0-2-4", "resource_type": "shared_inference", "health": "healthy", "qualified_models": ["qwen3.5:4b"]},
        {"resource_id": "head-192-0-2-6", "resource_type": "shared_inference", "health": "unqualified", "qualified_models": ["llama3.2:1b"]}]}
    assert lt.pick_models(snap, gm.is_chat_model) == ["qwen3.5:4b", "gemma3:4b"], "no embedding model, no unhealthy head"
    p = lt.traffic_policy(["qwen3.5:4b"])["clients"][0]
    assert p["app_id"] == "lab-traffic" and p["priority"] == 1 and p["max_concurrency"] == 4 and p["credential"] == "provisioned"


def test_targets_spread_over_free_heads():
    res = [{"resource_id": "head-192-0-2-3", "models": ["qwen3.5:4b"]}, {"resource_id": "head-192-0-2-4", "models": ["qwen3.5:4b"]}]
    rng = random.Random(1)
    assert lt.choose_target(res, rng, busy={"head-192-0-2-3"}) == ("head-192-0-2-4", "qwen3.5:4b")
    assert lt.choose_target([], rng) is None


def test_every_event_passes_arenas_contract():
    e = lt.Events(run="abcd1234")
    events = e.opening() + [e.created("TEST-C001-1", "lt_intake", "C001", "triage", "intake"), e.waiting_gateway("TEST-C001-1", "qwen3.5:4b"),
                            e.to_head("TEST-C001-1", "head-192-0-2-3", "qwen3.5:4b"), e.working("TEST-C001-1", "qwen3.5:4b"),
                            e.home_again("TEST-C001-1", "head-192-0-2-3", "triage"), e.done("TEST-C001-1", True),
                            e.done("TEST-C001-2", False, lt.safe_reason("Gateway 403: Model is not permitted for this application (#42)")),
                            *e.handoff("lt_intake", "lt_research", "C001", "TEST-C001-1"), e.heartbeat(), e.heartbeat("TEST-C001-1")]
    for ev in events:
        contract.validate(ev)
    labels = [ev["label"] for ev in events if "label" in ev]
    assert labels and all(lab.startswith("TEST:") for lab in labels)


class FakeGateway:
    """Each request: queued 2 s, leased (head known) 3 s, active 8 s, then completed."""

    def __init__(self, clock):
        self.clock, self.reqs, self.n = clock, {}, 0

    def models(self):
        return [{"resource_id": "head-192-0-2-3", "models": ["qwen3.5:4b"]}, {"resource_id": "head-192-0-2-4", "models": ["gemma3:4b"]}]

    def submit(self, model, head, prompt, key):
        assert "Ticket:" in prompt and len(prompt) < 2000
        self.n += 1
        rid = f"REQ-{self.n}"
        self.reqs[rid] = (self.clock(), head)
        return {"request": {"request_id": rid}, "status": "queued"}

    def status(self, rid):
        t0, head = self.reqs[rid]
        dt = self.clock() - t0
        st = "queued" if dt < 2 else "leased" if dt < 5 else "active" if dt < 13 else "completed"
        req = {"request_id": rid, "resource_id": head if dt >= 2 else None}
        env = {"status": st, "request": req}
        if dt >= 5:
            env["execution_started_at"] = t0 + 5
        return env


def test_a_run_sends_real_requests_and_shows_each_trip_in_arena():
    t = [__import__("time").time() - 3600]    # a simulated hour ago: Arena refuses future times
    clock = lambda: t[0]
    def sleep(s):
        t[0] += s
    gw, sent = FakeGateway(clock), []
    tr = lt.Traffic(gw, lambda evs: (sent.extend(evs), {"results": [{"status": "accepted", "event_id": e["event_id"]} for e in evs]})[1],
                    minutes=4, seed=3, out=lambda s: None, clock=clock, sleep=sleep)
    tally = tr.run()
    assert tally["cases"] >= 3 and tally["failed"] == 0 and tally["completed"] == tally["steps"] == tally["cases"] * 3
    for ev in sent:
        contract.validate(ev)
    moves = [e for e in sent if e["type"] == "assignment.moved"]
    assert {m["to_location"] for m in moves} >= {"head:head-192-0-2-3", "head:head-192-0-2-4"}, "work visits both heads"
    aid = next(e["assignment_id"] for e in sent if e["type"] == "assignment.created")
    order = [(e["type"], e.get("state"), e.get("to_location", "")) for e in sent if e.get("assignment_id") == aid and e["type"] != "handoff.sent" and e["type"] != "handoff.received"]
    kinds = [o[0] for o in order]
    assert kinds[:5] == ["assignment.created", "assignment.state", "assignment.moved", "assignment.state", "assignment.moved"], order
    assert order[2][2].startswith("head:") and order[4][2] == "orch:lab-traffic/main" and order[-1][1] == "completed"
    assert any(e["type"] == "handoff.sent" for e in sent), "intake hands the case to research"


def test_a_gateway_refusal_shows_as_a_failed_step_and_the_run_goes_on():
    t = [__import__("time").time() - 3600]    # a simulated hour ago: Arena refuses future times
    clock = lambda: t[0]
    gw = FakeGateway(clock)
    calls = {"n": 0}
    real = gw.submit

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Gateway 403: Model is not permitted for this application")
        return real(*a)
    gw.submit = flaky
    sent = []
    tr = lt.Traffic(gw, lambda evs: (sent.extend(evs), {"results": []})[1], minutes=3, seed=5, out=lambda s: None, clock=clock,
                    sleep=lambda s: t.__setitem__(0, t[0] + s))
    tally = tr.run()
    assert tally["failed"] == 1 and tally["completed"] >= 3
    failed = [e for e in sent if e.get("state") == "failed"]
    assert failed and "not permitted" in failed[0]["label"]


def test_the_shortcut_and_arena_client_are_installed_with_the_screen():
    import subprocess
    src = (TOOLS / "gateway-models" / "Lab Traffic.cmd").read_text()
    assert "lab_traffic.py" in src and "RunAs" in src
    gw = (ROOT / "lab-provisioner" / "engine" / "modules" / "Lab.Gateway.psm1").read_text(encoding="utf-8")
    assert "'Lab Traffic.lnk'" in gw and "'__init__.py', 'client.py', 'crypto.py', 'secretfile.py'" in gw
