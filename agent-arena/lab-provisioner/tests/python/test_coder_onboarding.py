"""Coder Onboarding (hub) and the Gateway Models screen's explicit-only guard for coder.

The snapshot below is the shape of the Gateway's observe output, reduced from the hub's evidence of 2026-11-11 15:55
(heads 2, 3, 6 and 10; the real digests of qwen2.5-coder:7b and qwen3.5:4b). With GATEWAY_RELEASE (a folder holding the
Gateway's inference_gateway package), the whole set-up also runs against the Gateway's own code."""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "gateway-models"))
sys.path.insert(0, str(TOOLS / "model-library"))
import gateway_models as gm  # noqa: E402
import fleet_commit as fc  # noqa: E402
import coder_onboarding as co  # noqa: E402

RELEASE = os.environ.get("GATEWAY_RELEASE", "")
CODER7 = "dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364"
CODER14 = "9ec8897f747e" + "0" * 52
QWEN = "4b" * 32


def snapshot(**over):
    res = [{"resource_id": "head-192-0-2-2", "resource_type": "shared_inference", "health": "healthy",
            "qualified_models": ["qwen2.5-coder:14b", "qwen2.5-coder:7b", "qwen3.5:4b", "all-minilm:latest"]},
           {"resource_id": "head-192-0-2-3", "resource_type": "shared_inference", "health": "healthy", "qualified_models": ["qwen3.5:4b"]},
           {"resource_id": "head-192-0-2-6", "resource_type": "shared_inference", "health": "quarantined", "qualified_models": ["qwen2.5-coder:1.5b"]},
           {"resource_id": "head-192-0-2-10", "resource_type": "shared_inference", "health": "healthy", "qualified_models": ["qwen2.5-coder:7b"]}]
    inv = [{"resource_id": "head-192-0-2-2", "fresh": True, "runtime_version": "0.40.12",
            "models": [{"name": "qwen2.5-coder:7b", "digest": CODER7}, {"name": "qwen2.5-coder:14b", "digest": CODER14},
                       {"name": "qwen3.5:4b", "digest": QWEN}, {"name": "all-minilm:latest", "digest": "e" * 64}]},
           {"resource_id": "head-192-0-2-3", "fresh": True, "runtime_version": "0.40.13", "models": [{"name": "qwen3.5:4b", "digest": QWEN}]},
           {"resource_id": "head-192-0-2-6", "fresh": False, "runtime_version": "0.40.13", "models": [{"name": "qwen2.5-coder:1.5b", "digest": "1" * 64}]},
           {"resource_id": "head-192-0-2-10", "fresh": True, "runtime_version": "0.40.13", "models": [{"name": "qwen2.5-coder:7b", "digest": CODER7}]}]
    q = [{"evidence_id": "EVIDENCE-b565", "resource_id": "head-192-0-2-2", "model": "qwen2.5-coder:7b", "digest": CODER7, "passed": 1, "at": 1794250000},
         {"evidence_id": "EVIDENCE-3e7e", "resource_id": "head-192-0-2-10", "model": "qwen2.5-coder:7b", "digest": CODER7, "passed": 1, "at": 1794320000},
         {"evidence_id": "EVIDENCE-c14", "resource_id": "head-192-0-2-2", "model": "qwen2.5-coder:14b", "digest": CODER14, "passed": 1, "at": 1794250000},
         {"evidence_id": "EVIDENCE-q2", "resource_id": "head-192-0-2-2", "model": "qwen3.5:4b", "digest": QWEN, "passed": 1, "at": 1794250000},
         {"evidence_id": "EVIDENCE-q3", "resource_id": "head-192-0-2-3", "model": "qwen3.5:4b", "digest": QWEN, "passed": 1, "at": 1794250000},
         {"evidence_id": "EVIDENCE-c6", "resource_id": "head-192-0-2-6", "model": "qwen2.5-coder:1.5b", "digest": "1" * 64, "passed": 1, "at": 1794250000},
         {"evidence_id": "EVIDENCE-m2", "resource_id": "head-192-0-2-2", "model": "all-minilm:latest", "digest": "e" * 64, "passed": 1, "at": 1794250000}]
    doc = {"ledger_id": "LEDGER-test", "service_id": "shared-inference", "admission_open": 1, "resources": res, "inventory": inv,
           "qualification_observations": q}
    doc.update(over)
    return doc


def test_candidates_are_qualified_now_on_healthy_heads_coding_models_first():
    c = co.candidates(snapshot(), is_chat=gm.is_chat_model)
    assert [(x["model"], x["heads"]) for x in c] == [
        ("qwen2.5-coder:7b", ["head-192-0-2-2", "head-192-0-2-10"]),      # coding, two heads
        ("qwen2.5-coder:14b", ["head-192-0-2-2"]),
        ("qwen3.5:4b", ["head-192-0-2-2", "head-192-0-2-3"])], "embeddings left out; head 6 is quarantined and stale"
    assert c[0]["digest"] == CODER7 and c[0]["coding"] and not c[2]["coding"]
    assert c[0]["evidence"]["head-192-0-2-10"]["evidence_id"] == "EVIDENCE-3e7e"


def test_a_failed_newest_test_or_a_changed_digest_does_not_count():
    s = snapshot()
    s["qualification_observations"].append({"evidence_id": "EVIDENCE-late", "resource_id": "head-192-0-2-10", "model": "qwen2.5-coder:7b",
                                            "digest": CODER7, "passed": 0, "at": 1794400000})
    assert co.candidates(s, is_chat=gm.is_chat_model)[0]["heads"] == ["head-192-0-2-2"]
    s = snapshot()
    s["inventory"][0]["models"][0]["digest"] = "f" * 64          # head 2 now has another build than the one tested
    seven = [x for x in co.candidates(s, is_chat=gm.is_chat_model) if x["model"] == "qwen2.5-coder:7b"]
    assert [x["heads"] for x in seven] == [["head-192-0-2-10"]], "only the head whose installed build was tested"


def test_the_policy_names_only_coder_with_least_privilege():
    p = co.coder_policy("qwen2.5-coder:7b")
    assert [c["app_id"] for c in p["clients"]] == ["coder"] and p["resources"] == []
    c = p["clients"][0]
    assert c == {"app_id": "coder", "credential": "provisioned", "models": ["qwen2.5-coder:7b"], "max_concurrency": 1, "queue_limit": 4,
                 "priority": 5, "operations": ["/api/generate", "/api/chat"], "max_context": 4096}
    assert c["max_context"] < gm.QUALIFY_CONTEXT, "below the context the heads were qualified at"
    with pytest.raises(ValueError):
        co.coder_policy("")


def test_head_changes_keep_every_project_a_head_serves():
    heads = {"head-192-0-2-2": ["capstone", "capstone-research", "lab-traffic"], "head-192-0-2-10": ["capstone", "coder"],
             "head-192-0-2-3": ["capstone"]}
    assert co.head_changes(heads, ["head-192-0-2-2", "head-192-0-2-10"]) == [
        {"op": "head-projects", "address": "192.0.2.2", "applications": ["capstone", "capstone-research", "coder", "lab-traffic"]}]
    assert fc.validate({"schema": fc.REQUEST_SCHEMA, "changes": co.head_changes(heads, ["head-192-0-2-2"])},
                       apps={"capstone", "capstone-research", "lab-traffic", "coder"})[1] == [], "a change Fleet Commit itself accepts"
    with pytest.raises(ValueError):
        co.head_changes(heads, ["head-192-0-2-99"])


def test_arp_says_in_use_only_for_a_real_hardware_address():
    arp = ("\nInterface: 192.0.2.254 --- 0x7\n  Internet Address      Physical Address      Type\n"
           "  192.0.2.202           02-00-00-00-11-22     dynamic\n")
    assert co.arp_verdict(arp) == {"verdict": "in-use", "mac": "02-00-00-00-11-22", "line": "192.0.2.202           02-00-00-00-11-22     dynamic"}
    assert co.arp_verdict("No ARP Entries Found.\n")["verdict"] == "no-answer"
    assert co.arp_verdict("  192.0.2.2022  aa-bb-cc-dd-ee-ff dynamic")["verdict"] == "no-answer", "not a prefix match"
    assert co.arp_verdict("  192.0.2.202  ff-ff-ff-ff-ff-ff static")["verdict"] == "no-answer"


def test_coder_state_and_the_unchanged_checks():
    apps = [{"app_id": "capstone", "enabled": True, "models": ["a"], "max_concurrency": 1, "queue_limit": 16, "priority": 6, "operations": None, "max_context": None}]
    assert co.coder_state(apps, {"head-192-0-2-2": ["capstone"]}) == {"registered": False, "heads": []}
    after = apps + [{"app_id": "coder", "enabled": True, "models": ["m"], "max_concurrency": 1, "queue_limit": 4, "priority": 5,
                     "operations": ["/api/generate", "/api/chat"], "max_context": 4096}]
    s = co.coder_state(after, {"head-192-0-2-10": ["capstone", "coder"], "head-192-0-2-2": ["coder"]})
    assert s["registered"] and s["heads"] == ["head-192-0-2-2", "head-192-0-2-10"] and s["max_context"] == 4096
    assert co.others_unchanged(apps, after)
    assert not co.others_unchanged(apps, [dict(apps[0], models=["a", "b"])] + after[1:])
    assert co.other_heads_unchanged({"h": ["capstone"]}, {"h": ["capstone", "coder"]})
    assert not co.other_heads_unchanged({"h": ["capstone"]}, {"h": ["coder"]})
    assert not co.other_heads_unchanged({"h": ["capstone"]}, {"h": ["capstone", "lab-traffic"]})


# ---------------------------------------------------------------------------- the Gateway Models screen's guard

def view_with_coder():
    apps = [{"app_id": "capstone", "enabled": True, "models": ["qwen3.5:4b"]},
            {"app_id": "coder", "enabled": True, "models": ["qwen2.5-coder:7b"]}]
    return gm.build_view(snapshot(), apps, {}, set(), {"qwen3.5:4b", "gemma3:4b"})


def test_available_to_all_and_apply_never_add_a_model_to_coder():
    v = view_with_coder()
    assert v["explicit_only"] == {"coder": gm.EXPLICIT_ONLY["coder"]}
    # What the page used to send with "Available to all" ticked: every flagged model for every orchestrator.
    desired = {"apps": {"capstone": ["qwen3.5:4b", "gemma3:4b"], "coder": ["qwen2.5-coder:7b", "qwen3.5:4b", "gemma3:4b"]},
               "blocked": [], "flags": ["qwen3.5:4b", "gemma3:4b"]}
    plan = gm.plan_changes(v, desired)
    assert "coder" not in plan["policy"], "coder keeps exactly its one model"
    assert plan["policy"]["capstone"]["add"] == ["gemma3:4b"], "the other projects behave as before"
    assert any("not given to coder" in n for n in plan["notes"])
    assert not plan["problems"]


def test_the_screen_can_still_take_a_model_away_from_coder():
    v = gm.build_view(snapshot(), [{"app_id": "coder", "enabled": True, "models": ["qwen2.5-coder:7b", "qwen3.5:4b"]}], {}, set(), set())
    plan = gm.plan_changes(v, {"apps": {"coder": ["qwen2.5-coder:7b"]}, "blocked": [], "flags": []})
    assert plan["policy"]["coder"] == {"add": [], "remove": ["qwen3.5:4b"], "models": ["qwen2.5-coder:7b"]}


def test_coder_onboarding_refuses_a_screen_without_the_guard(monkeypatch):
    fake = type(sys)("gateway_models")
    fake.EXPLICIT_ONLY = {}
    monkeypatch.setattr(co, "_tools", lambda: (fake, fc, None))
    with pytest.raises(RuntimeError, match="does not keep Coder's models explicit"):
        co.setup(ask=lambda _p: "", say=lambda _t: None)


def test_the_page_skips_explicit_only_projects():
    js = (TOOLS / "gateway-models" / "ui" / "app.js").read_text(encoding="utf-8")
    assert "if (!explicit[a]) d.apps[a].add(m)" in js
    assert "for (const [a, s] of Object.entries(desired.apps)) if (!explicit[a]) s.add(m)" in js
    assert "${locked ? \"disabled\" : \"\"}" in js


# ---------------------------------------------------------------------------- a set-up against stand-ins

class Tools:
    def __init__(self, ledger, snap):
        self.ledger, self.release, self.operator = Path(ledger), Path("inference-gateway-0.3.0"), "tester"
        self.snap, self.calls = snap, []
        self.apps = [{"app_id": "capstone", "enabled": True, "models": ["qwen3.5:4b"], "max_concurrency": 1, "queue_limit": 16, "priority": 6,
                      "operations": ["/api/chat", "/api/embed", "/api/generate"], "max_context": None},
                     {"app_id": "lab-traffic", "enabled": True, "models": ["qwen3.5:4b"], "max_concurrency": 4, "queue_limit": 8, "priority": 1,
                      "operations": ["/api/chat", "/api/generate"], "max_context": 8192}]

    def applications(self):
        return [dict(a) for a in self.apps]

    def snapshot(self):
        return self.snap

    def run(self, *args, timeout=None):
        self.calls.append(args)
        if args[:2] == ("credentials", "issue"):
            pol = json.loads(Path(args[args.index("--policy") + 1]).read_text())
            c = pol["clients"][0]
            self.apps.append({k: c[k] for k in ("app_id", "models", "max_concurrency", "queue_limit", "priority", "operations", "max_context")} | {"enabled": True})
            Path(args[args.index("--output") + 1]).write_text("secret-not-read")
            return {"ok": True, "exit": 0, "json": {}, "error": None}
        return {"ok": False, "exit": 2, "json": None, "error": "unexpected"}

    def heads(self, inv, base, applications, output):
        self.calls.append(("heads", list(applications)))
        h = json.loads(Path(inv).read_text())["heads"][0]
        Path(output).write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [],
                                            "resources": [{"resource_id": h["resource_id"], "applications": sorted(applications)}]}))
        return {"ok": True, "exit": 0, "json": {}, "error": None}

    def bootstrap(self, f):
        doc = json.loads(Path(f).read_text())
        self.calls.append(("bootstrap", [r["resource_id"] for r in doc.get("resources", [])], [c["app_id"] for c in doc.get("clients", [])]))
        db = sqlite3.connect(self.ledger)
        for r in doc.get("resources", []):
            db.execute("UPDATE capacity_resources SET applications=? WHERE resource_id=?", (json.dumps(r["applications"]), r["resource_id"]))
        db.commit()
        db.close()
        return {"ok": True, "exit": 0, "json": {}, "error": None}

    def qualify(self, *a):
        raise AssertionError("Coder Onboarding never runs the Gateway's tests")


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    db = sqlite3.connect(ledger)
    db.execute("CREATE TABLE capacity_resources(resource_id TEXT, resource_type TEXT, applications TEXT)")
    for rid, apps in {"head-192-0-2-2": ["capstone", "lab-traffic"], "head-192-0-2-3": ["capstone"], "head-192-0-2-10": ["capstone", "lab-traffic"]}.items():
        db.execute("INSERT INTO capacity_resources VALUES(?,?,?)", (rid, "shared_inference", json.dumps(apps)))
    db.commit()
    db.close()
    tools = Tools(ledger, snapshot())
    console = gm.Console(tools, tmp_path / "console", scanner=lambda: {})
    scanned = []
    fleet = fc.Fleet(tools, console, None, tmp_path / "fleet",
                     scanner=lambda a: scanned.append(list(a)) or {x: {"version": "0.40.13", "models": {"qwen2.5-coder:7b": "sha256:" + CODER7}} for x in a})
    fleet.publish = lambda last_commit=None: {}
    monkeypatch.setattr(co, "_tools", lambda: (gm, fc, fleet))
    monkeypatch.setattr(co, "DATA", tmp_path / "data")
    monkeypatch.setattr(co, "OUTBOX", tmp_path / "outbox" / "coder")
    monkeypatch.setattr(co, "_restrict", lambda folder: folder.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(co, "probe_address", lambda address=co.ADDRESS: {"verdict": "no-answer", "mac": None, "line": None})
    monkeypatch.setattr(co, "stick_reports", lambda: tmp_path / "stick" / "reports" / "DESKTOP-REDACT10")
    return tools, fleet, tmp_path, scanned


def test_setup_issues_only_coder_and_adds_only_coder_to_its_heads(hub):
    tools, fleet, tmp, scanned = hub
    lines = []
    answers = iter(["1", "APPROVE"])
    r = co.setup(ask=lambda _p: next(answers), say=lines.append)
    issue = next(c for c in tools.calls if c[:2] == ("credentials", "issue"))
    assert issue[issue.index("--app-id") + 1] == "coder" and "--reader" not in issue
    assert [c for c in tools.calls if c[0] == "bootstrap"] == [("bootstrap", ["head-192-0-2-2"], []), ("bootstrap", ["head-192-0-2-10"], [])], \
        "heads only: no client's models are sent"
    assert scanned == [["192.0.2.2", "192.0.2.10"]], "only coder's heads are read; no other head is touched"
    assert fc.ledger_heads(tools.ledger) == {"head-192-0-2-2": ["capstone", "coder", "lab-traffic"], "head-192-0-2-3": ["capstone"],
                                             "head-192-0-2-10": ["capstone", "coder", "lab-traffic"]}
    assert r["coder"]["models"] == ["qwen2.5-coder:7b"] and r["coder"]["max_concurrency"] == 1 and r["coder"]["max_context"] == 4096
    assert r["selected"]["digest"] == CODER7 and r["selected"]["coding_model"] and r["selected"]["qualified_after"]
    assert r["other_projects_unchanged"] and r["other_heads_unchanged"] and r["secrets_included"] is False
    approved = json.loads((tmp / "console" / "gateway-policy-approved.json").read_text())
    assert [c["app_id"] for c in approved["clients"]] == ["capstone", "lab-traffic", "coder"]
    receipts = list((tmp / "stick" / "reports" / "DESKTOP-REDACT10" / "coder-onboarding").glob("setup-*.json"))
    assert len(receipts) == 1 and "secret-not-read" not in receipts[0].read_text(), "the token never reaches a receipt"
    assert any(line.startswith("DONE:") for line in lines)
    assert any("Do NOT make an App code" in line for line in lines)


def test_nothing_changes_without_approve_or_on_an_address_in_use(hub, monkeypatch):
    tools, fleet, tmp, _ = hub
    answers = iter(["1", "no"])
    co.setup(ask=lambda _p: next(answers), say=lambda _t: None)
    assert not [c for c in tools.calls if c[0] in ("credentials", "heads", "bootstrap")]
    monkeypatch.setattr(co, "probe_address", lambda address=co.ADDRESS: {"verdict": "in-use", "mac": "aa-bb-cc-dd-ee-ff", "line": "x"})
    with pytest.raises(RuntimeError, match="in use on the lab cable"):
        co.setup(ask=lambda _p: "1", say=lambda _t: None)
    assert not [c for c in tools.calls if c[0] in ("credentials", "heads", "bootstrap")]


def test_a_second_setup_issues_nothing(hub):
    tools, fleet, tmp, _ = hub
    answers = iter(["1", "APPROVE"])
    co.setup(ask=lambda _p: next(answers), say=lambda _t: None)
    n = len(tools.calls)
    co.setup(ask=lambda _p: pytest.fail("asked nothing"), say=lambda _t: None)
    assert len(tools.calls) == n


@pytest.mark.skipif(not RELEASE, reason="set GATEWAY_RELEASE to a folder with the Gateway's inference_gateway package")
def test_with_the_real_gateway_coder_gets_one_model_and_two_heads_and_nothing_else_changes(tmp_path):
    sys.path.insert(0, RELEASE)
    from inference_gateway.capacity_broker import CapacityBroker
    from inference_gateway.capacity_bootstrap import apply
    from inference_gateway.head_inventory import resources
    from inference_gateway import credentials
    ledger = tmp_path / "ledger.db"
    b = CapacityBroker(str(ledger))
    b.register_client("capstone", "x" * 40, models=["qwen3.5:4b", "qwen2.5-coder:7b"], max_concurrency=1, queue_limit=16, priority=6)
    for rid, n in (("head-192-0-2-2", 2), ("head-192-0-2-10", 10)):
        b.authorize_resource(rid, endpoint=f"http://192.0.2.{n}:11434", resource_type="shared_inference", owner="infrastructure",
                             applications=["capstone"], permitted_uses=["inference"])
        b.publish(rid, ["qwen2.5-coder:7b"], "healthy")
    before = credentials.listing(str(ledger))["applications"]
    pf = tmp_path / "coder-policy.json"
    pf.write_text(json.dumps(co.coder_policy("qwen2.5-coder:7b")))
    credentials.issue(str(ledger), str(pf), "coder", str(tmp_path / "coder.token"))
    for change in co.head_changes(fc.ledger_heads(ledger), ["head-192-0-2-2", "head-192-0-2-10"]):
        inv = gm.head_inventory(change["address"], "0.40.13", [{"name": "qwen2.5-coder:7b", "digest": CODER7}], "TEST")
        accepted, refused = resources(inv, change["applications"])
        assert not refused
        rf = tmp_path / f"r-{change['address']}.json"
        rf.write_text(json.dumps({"configuration_version": 1, "contract": "shared.inference/1", "clients": [], "resources": accepted}))
        apply(str(rf), str(ledger))
    after = credentials.listing(str(ledger))["applications"]
    coder = next(a for a in after if a["app_id"] == "coder")
    assert coder["models"] == ["qwen2.5-coder:7b"] and coder["max_concurrency"] == 1 and coder["queue_limit"] == 4
    assert sorted(coder["operations"]) == ["/api/chat", "/api/generate"] and coder["max_context"] == 4096
    assert co.others_unchanged(before, after)
    assert fc.ledger_heads(ledger) == {"head-192-0-2-2": ["capstone", "coder"], "head-192-0-2-10": ["capstone", "coder"]}
    with b.connect() as db:
        rows = db.execute("SELECT resource_id, qualified_models, health FROM capacity_resources ORDER BY resource_id").fetchall()
    assert [(r[0], json.loads(r[1]), r[2]) for r in rows] == [("head-192-0-2-10", ["qwen2.5-coder:7b"], "healthy"),
                                                              ("head-192-0-2-2", ["qwen2.5-coder:7b"], "healthy")], "qualifications kept"
