"""Gateway Models console: pure logic, the HTTP guard, and (when the Gateway 0.3.0 release folder is
given in GATEWAY_RELEASE) Do not use and bootstrap against the real Gateway code on a throwaway ledger."""
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine" / "hub-tools" / "gateway-models"))
import gateway_models as gm  # noqa: E402

RELEASE = os.environ.get("GATEWAY_RELEASE", "")


def snap(resources=(), inventory=(), evidence=()):
    return {"resources": list(resources), "inventory": list(inventory), "qualification_observations": list(evidence)}


def res(n, health="healthy", qualified=()):
    return {"resource_id": f"head-192-0-2-{n}", "resource_type": "shared_inference", "health": health, "qualified_models": list(qualified), "fresh": health == "healthy"}


def inv(n, models, fresh=True):
    return {"resource_id": f"head-192-0-2-{n}", "fresh": fresh, "runtime_version": "0.40.13", "models": [{"name": m, "digest": "d"} for m in models]}


APPS = [{"app_id": "capstone", "enabled": True, "models": ["qwen3.5:4b", "gemma3:4b"], "max_concurrency": 1, "queue_limit": 16, "priority": 6,
         "operations": ["/api/chat", "/api/embed", "/api/generate"], "max_context": None},
        {"app_id": "capstone-research", "enabled": True, "models": ["qwen3.5:4b"], "max_concurrency": 1, "queue_limit": 32, "priority": 2,
         "operations": ["/api/chat", "/api/embed", "/api/generate"], "max_context": None}]


def test_cell_state_order():
    assert gm.cell_state(installed=True, qualified=True, passed_last=True, tested=True, head_live=True, blocked=True) == "blocked"
    assert gm.cell_state(installed=False, qualified=True, passed_last=True, tested=True, head_live=True, blocked=False) == "absent"
    assert gm.cell_state(installed=True, qualified=True, passed_last=True, tested=True, head_live=False, blocked=False) == "offline"
    assert gm.cell_state(installed=True, qualified=False, passed_last=False, tested=True, head_live=True, blocked=False) == "failed"
    assert gm.cell_state(installed=True, qualified=False, passed_last=True, tested=True, head_live=True, blocked=False) == "lapsed"
    assert gm.cell_state(installed=True, qualified=False, passed_last=None, tested=False, head_live=True, blocked=False) == "untested"


def test_view_degraded_head_counts_as_online_from_its_inventory():
    # The Gateway keeps last_seen 0 (fresh false) for a head with no qualified model; its inventory is fresh.
    v = gm.build_view(snap([res(6, "degraded")], [inv(6, ["llama3.2:1b"])]), APPS, {}, set(), set())
    h = v["heads"][0]
    assert h["online"] and h["health"] == "degraded"
    row = next(r for r in v["models"] if r["model"] == "llama3.2:1b")
    assert row["cells"]["head-192-0-2-6"] == "untested"


def test_view_scan_adds_unenrolled_head_and_hides_embedding_models():
    v = gm.build_view(snap([res(3, qualified=["qwen3.5:4b"])], [inv(3, ["qwen3.5:4b", "nomic-embed-text:latest"])]), APPS,
                      {"192.0.2.6": {"version": "0.40.13", "models": {"llama3.2:1b": "d"}}}, set(), set())
    assert [h["address"] for h in v["heads"]] == ["192.0.2.3", "192.0.2.6"]
    assert v["heads"][1]["in_gateway"] is False
    names = {r["model"]: r for r in v["models"]}
    assert names["nomic-embed-text:latest"]["chat"] is False
    assert names["llama3.2:1b"]["cells"]["head-192-0-2-6"] == "not-enrolled"
    assert names["qwen3.5:4b"]["cells"]["head-192-0-2-3"] == "usable"
    assert names["qwen3.5:4b"]["allowed"] == ["capstone", "capstone-research"]


def base_view(**kw):
    s = snap([res(3, qualified=["qwen3.5:4b", "gemma3:4b"]), res(4, qualified=["qwen3.5:4b"]), res(5, "offline", ["qwen3.5:4b"])],
             [inv(3, ["qwen3.5:4b", "gemma3:4b", "llama3.2:3b", "qwen3:4b"]), inv(4, ["qwen3.5:4b", "gemma3:4b", "qwen3:4b"]), inv(5, ["qwen3.5:4b", "llama3.2:3b"], fresh=False)],
             [{"resource_id": "head-192-0-2-3", "model": "qwen3:4b", "passed": 0, "at": 1}, {"resource_id": "head-192-0-2-4", "model": "qwen3:4b", "passed": 0, "at": 1}])
    return gm.build_view(s, APPS, {}, kw.get("blocked", set()), set())


def desired(view, apps=None, blocked=None, flags=None):
    d = {"apps": {a["app_id"]: list(a["models"]) for a in view["orchestrators"]}, "blocked": [], "flags": flags or []}
    d["apps"].update(apps or {})
    d["blocked"] = [list(b) for b in (blocked or [])]
    return d


def test_plan_add_model_tests_it_where_installed_and_online_only():
    v = base_view()
    p = gm.plan_changes(v, desired(v, apps={"capstone": ["qwen3.5:4b", "gemma3:4b", "llama3.2:3b"]}))
    assert p["policy"] == {"capstone": {"add": ["llama3.2:3b"], "remove": [], "models": ["gemma3:4b", "llama3.2:3b", "qwen3.5:4b"]}}
    # gemma3:4b on .4 is allowed, installed, never tested: tested too. .5 is offline: skipped, not failed.
    assert ["head-192-0-2-3", "llama3.2:3b"] in p["qualify"] and ["head-192-0-2-4", "gemma3:4b"] in p["qualify"]
    assert not any(q[0] == "head-192-0-2-5" for q in p["qualify"])
    assert any(s["resource_id"] == "head-192-0-2-5" and "offline" in s["reason"] for s in p["skipped"])
    assert not p["problems"]


def test_plan_failed_model_is_skipped_unless_retest():
    v = base_view()
    d = desired(v, apps={"capstone": ["qwen3.5:4b", "gemma3:4b", "qwen3:4b"]})
    p = gm.plan_changes(v, d)
    assert not any(q[1] == "qwen3:4b" for q in p["qualify"])
    assert sum(1 for s in p["skipped"] if s["model"] == "qwen3:4b") == 2
    p = gm.plan_changes(v, d, retest=[("head-192-0-2-3", "qwen3:4b")])
    assert ["head-192-0-2-3", "qwen3:4b"] in p["qualify"] and ["head-192-0-2-4", "qwen3:4b"] not in p["qualify"]


def test_plan_block_and_unblock():
    v = base_view()
    p = gm.plan_changes(v, desired(v, blocked=[("head-192-0-2-3", "gemma3:4b")]))
    assert p["block"] == [["head-192-0-2-3", "gemma3:4b"]]
    assert ["head-192-0-2-3", "gemma3:4b"] not in p["qualify"]
    vb = base_view(blocked={("head-192-0-2-3", "gemma3:4b")})
    assert next(r for r in vb["models"] if r["model"] == "gemma3:4b")["cells"]["head-192-0-2-3"] == "blocked"
    p = gm.plan_changes(vb, desired(vb, blocked=[]))
    assert p["unblock"] == [["head-192-0-2-3", "gemma3:4b"]]
    assert ["head-192-0-2-3", "gemma3:4b"] in p["qualify"]          # tested again before use


def test_plan_refuses_empty_orchestrator_unknown_app_and_bad_names():
    v = base_view()
    p = gm.plan_changes(v, desired(v, apps={"capstone-research": [], "nobody": ["x"], "capstone": ["bad name!"]}))
    text = " ".join(p["problems"])
    assert "capstone-research would have no model" in text and "'nobody'" in text and "'bad name!'" in text


def test_plan_no_change_is_empty():
    v = base_view()
    p = gm.plan_changes(v, desired(v))
    # gemma3:4b on .4 is allowed and installed but never tested, so the plan still offers to test it.
    assert p["qualify"] == [["head-192-0-2-4", "gemma3:4b"]]
    assert not p["policy"] and not p["block"] and not p["unblock"]


def test_bootstrap_policy_keeps_every_other_field_and_no_resources():
    doc = gm.bootstrap_policy(APPS, {"capstone": ["qwen3.5:4b", "llama3.2:3b"]})
    assert doc["resources"] == [] and doc["configuration_version"] == 1 and doc["contract"] == "shared.inference/1"
    assert doc["clients"] == [{"app_id": "capstone", "credential": "provisioned", "models": ["qwen3.5:4b", "llama3.2:3b"], "max_concurrency": 1,
                               "queue_limit": 16, "priority": 6, "operations": ["/api/chat", "/api/embed", "/api/generate"], "max_context": None}]


def test_install_note_and_inventory():
    note = gm.install_note("llama3.2:3b", [".3", ".4"])
    assert "ollama pull llama3.2:3b" in note and ".3, .4" in note and "\r\n" in note
    i = gm.head_inventory("192.0.2.6", "0.40.13", [{"name": "a:1", "digest": "x"}], "HUB")
    h = i["heads"][0]
    assert i["schema"] == "agent-arena.verified-head-inventory/1" and h["resource_id"] == h["arena_node_id"] == "head-192-0-2-6"
    assert h["endpoint"] == "http://192.0.2.6:11434" and h["verified"]["ollama_version"] == "0.40.13"


class FakeTools:
    operator = "tester"
    release = Path("0.3.0")

    def snapshot(self):
        return snap([res(3, qualified=["qwen3.5:4b"])], [inv(3, ["qwen3.5:4b"])])

    def applications(self):
        return APPS


@pytest.fixture()
def server(tmp_path):
    console = gm.Console(FakeTools(), tmp_path, scanner=lambda: {})
    srv, token = gm.build_server(console, 0, "t0ken")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], token, console
    srv.shutdown()
    srv.server_close()


def call(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


def test_http_guard(server):
    port, token, _ = server
    own = {"Host": f"127.0.0.1:{port}"}
    assert call(port, "GET", "/api/state", {**own})[0] == 401
    assert call(port, "GET", "/api/state", {"Host": "evil.example", "X-Console-Token": token})[0] == 403
    assert call(port, "POST", "/api/audit", {**own, "X-Console-Token": token, "Origin": "http://evil.example"}, {})[0] == 403
    status, data = call(port, "GET", "/api/state", {**own, "X-Console-Token": token})
    assert status == 200 and json.loads(data)["orchestrators"][0]["app_id"] == "capstone"
    assert call(port, "GET", "/../gateway_models.py", {**own})[0] == 404
    assert call(port, "GET", "/", {**own})[0] == 200


def test_install_request_is_validated_and_tracked(server):
    port, token, console = server
    h = {"Host": f"127.0.0.1:{port}", "X-Console-Token": token, "Content-Type": "application/json"}
    assert call(port, "POST", "/api/install", h, {"model": "rm -rf", "heads": ["192.0.2.3"]})[0] == 400
    assert call(port, "POST", "/api/install", h, {"model": "llama3.2:3b", "heads": ["192.0.2.254"]})[0] == 400
    status, data = call(port, "POST", "/api/install", h, {"model": "qwen3.5:4b", "heads": ["192.0.2.3"]})
    assert status == 200 and "ollama pull qwen3.5:4b" in json.loads(data)["note"]
    v, _ = console.view()
    assert v["installs"][0]["status"] == {"192.0.2.3": "installed"} and v["installs"][0]["done"]


class LockedHeadTools(FakeTools):
    """Two heads with gemma3:4b installed and untested; head 4 was locked by an earlier timed-out test."""

    def __init__(self):
        self.calls, self.health = [], {"head-192-0-2-3": "healthy", "head-192-0-2-4": "quarantined"}

    def snapshot(self):
        return snap([res(3, self.health["head-192-0-2-3"], ["qwen3.5:4b"]), res(4, self.health["head-192-0-2-4"], ["qwen3.5:4b"])],
                    [inv(3, ["qwen3.5:4b", "gemma3:4b"]), inv(4, ["qwen3.5:4b", "gemma3:4b"])])

    def reconcile(self, rid):
        self.calls.append(("reconcile", rid))
        self.health[rid] = "unqualified"
        return {"ok": True, "exit": 0, "json": {}, "error": None}

    def qualify(self, rid, model):
        self.calls.append(("qualify", rid, model))
        if self.health[rid] == "quarantined":
            return {"ok": False, "exit": 1, "json": None, "error": "PermissionError: Quarantined execution must be reconciled before qualification"}
        return {"ok": False, "exit": 1, "json": {"passed": False}, "error": "", "evidence_error": None}


def test_the_screens_tests_load_each_model_first_and_say_why_one_fails(tmp_path):
    """Gateway Models' Apply uses the same steps as Fleet Commit: warm-up, unlock a locked head, test, real reason."""
    tools, warmed = LockedHeadTools(), []

    def http(address, path, body, timeout=30, method=None):
        warmed.append((address, path, body["model"], body["options"]["num_ctx"]))
        return {"response": "READY.", "done": True}
    console = gm.Console(tools, tmp_path, scanner=lambda: {}, http=http)
    view, _ = console.view()
    plan = gm.plan_changes(view, {})
    assert plan["qualify"] == [["head-192-0-2-3", "gemma3:4b"], ["head-192-0-2-4", "gemma3:4b"]] and not plan["skipped"], \
        "a locked head is tested too (the run unlocks it)"
    console.apply({}, [])
    for _ in range(200):
        if not console.job["running"]:
            break
        time.sleep(0.02)
    job = console.job_status()
    assert job["result"] == {"ok": False, "failures": ["gemma3:4b on 192.0.2.3", "gemma3:4b on 192.0.2.4"]}
    assert sorted(warmed) == [("192.0.2.3", "/api/generate", "gemma3:4b", 8192), ("192.0.2.4", "/api/generate", "gemma3:4b", 8192)]
    assert tools.calls.index(("reconcile", "head-192-0-2-4")) < tools.calls.index(("qualify", "head-192-0-2-4", "gemma3:4b"))
    text = "\n".join(job["lines"])
    assert 'gemma3:4b on head 3: FAILED: it answered "READY." and the Gateway accepts only exactly READY' in text
    assert "head 4 was locked by an earlier timed-out test; unlocked after checking it is idle" in text


@pytest.mark.skipif(not (RELEASE and Path(RELEASE, "inference_gateway").is_dir()), reason="set GATEWAY_RELEASE to the Gateway 0.3.0 release folder")
def test_do_not_use_and_bootstrap_against_the_real_gateway(tmp_path):
    sys.path.insert(0, RELEASE)
    from inference_gateway.capacity_broker import CapacityBroker
    from inference_gateway.capacity_bootstrap import apply
    ledger = tmp_path / "ledger.db"
    b = CapacityBroker(str(ledger))
    b.register_client("capstone", "x" * 40, models=["qwen3.5:4b"], max_concurrency=1, queue_limit=16, priority=6)
    b.set_client_limits("capstone", operations=["/api/generate"], max_context=8192)
    b.authorize_resource("head-192-0-2-3", endpoint="http://192.0.2.3:11434", resource_type="shared_inference", owner="infrastructure", applications=["capstone"], permitted_uses=["inference"])
    b.publish("head-192-0-2-3", ["qwen3.5:4b", "gemma3:4b"], "healthy")
    tools = gm.GatewayTools(sys.executable, RELEASE, ledger, tmp_path, "tester")
    assert tools.withdraw("head-192-0-2-3", "gemma3:4b") is True
    assert tools.withdraw("head-192-0-2-3", "gemma3:4b") is False
    with b.connect() as db:
        row = db.execute("SELECT qualified_models,health FROM capacity_resources WHERE resource_id='head-192-0-2-3'").fetchone()
        ev = db.execute("SELECT event_type,payload FROM capacity_events ORDER BY event_id DESC LIMIT 1").fetchone()
    assert json.loads(row[0]) == ["qwen3.5:4b"] and row[1] == "healthy"
    assert ev[0] == "qualification.invalidated" and json.loads(ev[1])["reason"] == "operator_do_not_use"
    # Bootstrap of the console's document changes only models; limits and the head stay as they are.
    lim = b.client_limits("capstone")
    doc = gm.bootstrap_policy([{"app_id": "capstone", "max_concurrency": 1, "queue_limit": 16, "priority": 6, **lim}], {"capstone": ["qwen3.5:4b", "llama3.2:3b"]})
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps(doc))
    apply(str(pf), str(ledger))
    with b.connect() as db:
        client = db.execute("SELECT * FROM capacity_clients WHERE app_id='capstone'").fetchone()
        head = db.execute("SELECT qualified_models FROM capacity_resources WHERE resource_id='head-192-0-2-3'").fetchone()
    assert json.loads(client["models"]) == ["qwen3.5:4b", "llama3.2:3b"] and client["enabled"] == 1 and client["priority"] == 6
    assert b.client_limits("capstone") == {"operations": ["/api/generate"], "max_context": 8192}
    assert json.loads(head[0]) == ["qwen3.5:4b"]
    with pytest.raises(RuntimeError):
        tools.withdraw("head-192-0-2-9", "x")


def test_model_names_from_hugging_face_keep_their_case():
    for ok in ("hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M", "hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:Q4_K_M", "qwen3:4b-instruct-2507-q4_K_M", "llama3.2:3b"):
        assert gm.MODEL_NAME.match(ok), ok
    for bad in ("rm -rf", "a b", ":x", "x;y", ""):
        assert not gm.MODEL_NAME.match(bad), bad
    v = base_view()
    p = gm.plan_changes(v, desired(v, apps={"capstone": ["qwen3.5:4b", "hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M"]}))
    assert not p["problems"] and p["policy"]["capstone"]["add"] == ["hf.co/unsloth/SmolLM3-3B-GGUF:Q4_K_M"]


# ---- hub > 3 > 2 (1.6.7): only the pairs not usable now are tested, with the right purpose
def test_qualify_pairs_keeps_what_serves_and_tests_the_rest(tmp_path):
    import json as _json
    import sqlite3 as _sq
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine" / "hub-tools" / "gateway-models"))
    import qualify_pairs as qp
    f = tmp_path / "l.db"
    db = _sq.connect(f)
    db.execute("CREATE TABLE capacity_resources(resource_id TEXT, endpoint TEXT, qualified_models TEXT)")
    db.execute("CREATE TABLE capacity_qualification_evidence(resource_id TEXT, model TEXT, passed INT, evidence TEXT, at REAL)")
    db.execute("INSERT INTO capacity_resources VALUES('head-192-0-2-7','http://192.0.2.7:11434',?)", (_json.dumps(["gemma3:12b", "all-minilm:latest", "qwen3:4b"]),))
    ev = lambda p, purpose, at, **k: (p, _json.dumps({"endpoint": "http://192.0.2.7:11434", "purpose": purpose, "passed": bool(p), **k}), at)
    rows = [("gemma3:12b",) + ev(1, "completion", 1),
            ("all-minilm:latest",) + ev(1, "embedding", 1), ("all-minilm:latest",) + ev(0, "completion", 2, error="Metadata does not support qualification operation"),
            ("qwen3:4b",) + ev(1, "completion", 1), ("qwen3:4b",) + ev(0, "completion", 3, response={"response": "x"}),
            ("gemma3:1b",) + ev(0, "completion", 1, response={"response": "Ready."})]
    for m, p, e, at in rows:
        db.execute("INSERT INTO capacity_qualification_evidence VALUES('head-192-0-2-7',?,?,?,?)", (m, p, e, at))
    db.commit()
    db.close()
    pairs = [("head-192-0-2-7", m) for m in ("gemma3:12b", "all-minilm:latest", "qwen3:4b", "gemma3:1b", "llama3.2:3b")]
    test, kept = qp.split(pairs, qp.usable_now(f))
    assert kept == [("head-192-0-2-7", "gemma3:12b"), ("head-192-0-2-7", "all-minilm:latest")], "a refused wrong-purpose test changes nothing"
    assert test == [("head-192-0-2-7", "qwen3:4b"), ("head-192-0-2-7", "gemma3:1b"), ("head-192-0-2-7", "llama3.2:3b")], "the newest real test decides"
    assert qp.purpose_of("all-minilm:latest") == "embedding" and qp.purpose_of("gemma3:1b") == "completion"
