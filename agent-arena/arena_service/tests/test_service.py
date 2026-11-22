"""Security, isolation and state-machine tests for the Arena Event Service.

Every test runs a real HTTP service on 127.0.0.1 with its own temporary
database. These are automated workstation tests, not head evidence.
"""
import json
import sys
import threading
import time
import urllib.request

import pytest

from arena_service import client as aclient, crypto
from arena_service.client import ArenaError, ServiceIdentityError


def statuses(response):
    return [r["status"] if r["status"] != "rejected" else "rejected:" + r["reason"] for r in response["results"]]


def setup_ticketing(env):
    producer = env.app("capstone", "Ticketing")
    env.head("head-192-0-2-3")
    env.head("head-192-0-2-6")
    producer.publish([env.ev("agent.registered", agent_id="independent_validation",
                             display_name="Ticketing Independent Validator", role="validation")])
    return producer


def test_service_does_not_import_capstone():
    import arena_service.server  # noqa: F401
    assert not any(name.startswith("capstone_rag") for name in sys.modules)


def test_health_is_public_and_signed_when_credential_present(env):
    with urllib.request.urlopen(env.url + "/v2/health") as r:
        body = json.load(r)
        assert body["contracts"] == ["arena.v2"] and body["service_id"].startswith("aes-")
        assert "X-Arena-Response-Signature" not in r.headers
    env.head("head-192-0-2-3")
    viewer = env.viewer("head-192-0-2-3")
    assert viewer.request("GET", "/v2/health")["service_id"] == body["service_id"]


def test_dispatch_lifecycle_and_location_views(env):
    producer = setup_ticketing(env)
    viewer3 = env.viewer("head-192-0-2-3")
    results = producer.publish([
        env.ev("assignment.created", assignment_id="T1", agent_id="independent_validation", kind="inference",
               state="executing", workflow_id="GRAPH-1", task_id="T1"),
        env.ev("assignment.moved", assignment_id="T1", from_location="orch:capstone/main",
               to_location="head:head-192-0-2-3", state="waiting", wait_reason="model", visual_action="waiting_model"),
    ])
    assert statuses(results) == ["accepted", "accepted"]
    head3 = viewer3.snapshot("location:head:head-192-0-2-3")
    assert [(a["agent_key"], a["state"], a["wait_reason"]) for a in head3["assignments"]] == \
        [("capstone/independent_validation", "waiting", "model")]
    assert [a["agent_key"] for a in head3["agents"]] == ["capstone/independent_validation"]
    assert viewer3.snapshot("location:head:head-192-0-2-6")["assignments"] == []
    assert viewer3.snapshot("location:head:head-192-0-2-6")["agents"] == []       # idle head: no invented agents
    orch = viewer3.snapshot("location:orch:capstone/main")
    assert orch["assignments"] == [] and [a["agent_key"] for a in orch["agents"]] == ["capstone/independent_validation"]
    producer.publish([env.ev("assignment.state", assignment_id="T1", state="executing", visual_action="inference"),
                      env.ev("assignment.moved", assignment_id="T1", to_location="orch:capstone/main", state="executing"),
                      env.ev("assignment.state", assignment_id="T1", state="completed")])
    events = viewer3.events(0, view="location:head:head-192-0-2-3")["events"]
    moves = [(e["from_location"], e["to_location"]) for e in events if e["type"] == "assignment.moved"]
    assert moves == [("orch:capstone/main", "head:head-192-0-2-3"), ("head:head-192-0-2-3", "orch:capstone/main")]
    final = viewer3.snapshot("location:orch:capstone/main")["assignments"][0]
    assert final["state"] == "completed" and final["render_state"] == "completed"


def test_tampered_replayed_and_skewed_requests_are_refused(env):
    producer = setup_ticketing(env)
    body = json.dumps({"contract": "arena.v2", "events": []}).encode()
    stamp, nonce = str(int(time.time())), "n-fixed-0000000001"
    good = crypto.sign(producer.key, crypto.canonical_request("POST", "/v2/events", stamp, nonce, body))

    def post(data, signature, stamp=stamp, nonce=nonce):
        req = urllib.request.Request(env.url + "/v2/events", data=data, method="POST", headers={
            "X-Arena-Credential": producer.credential_id, "X-Arena-Timestamp": stamp,
            "X-Arena-Nonce": nonce, "X-Arena-Signature": signature})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    tampered = body.replace(b"[]", b"[{}]")
    assert post(tampered, good)[0] == 403
    assert post(body, good)[0] == 400             # authenticated; empty batch is a bad request
    assert post(body, good)[1]["error"] == "replayed"
    old = str(int(time.time()) - 600)
    skewed = crypto.sign(producer.key, crypto.canonical_request("POST", "/v2/events", old, "n2", body))
    assert post(body, skewed, stamp=old, nonce="n2")[1]["error"] == "clock_skew"


def test_role_separation(env):
    producer = setup_ticketing(env)
    viewer = env.viewer("head-192-0-2-3")
    with pytest.raises(ArenaError) as denied:
        viewer.publish([env.ev("orchestrator.heartbeat")])
    assert denied.value.status == 403
    with pytest.raises(ArenaError) as denied:
        producer.snapshot()
    assert denied.value.status == 403
    with pytest.raises(ArenaError):
        aclient.Client(env.url, producer.credential_id, "wrong-secret").publish([env.ev("orchestrator.heartbeat")])


def test_cross_application_isolation(env):
    ticketing = setup_ticketing(env)
    coder = env.app("coder", "Coder")
    reg = dict(agent_id="independent_validation", display_name="Coder Validator", role="validation")
    assert statuses(coder.publish([env.ev("agent.registered", **reg)])) == ["accepted"]
    assert statuses(coder.publish([env.ev("orchestrator.heartbeat", orch="main", display_name="spoof")])) == ["accepted"]
    attempt = coder.publish([
        env.ev("assignment.created", assignment_id="X", agent_id="independent_validation", kind="tool",
               location="orch:capstone/main"),
    ])
    assert statuses(attempt) == ["rejected:location:other_application"]
    other_orch = ticketing.publish([env.ev("orchestrator.heartbeat", orch="evil")])
    assert statuses(other_orch) == ["rejected:orchestrator:not_permitted"]
    agents = {a["agent_key"]: a for a in env.viewer("head-192-0-2-3").request("GET", "/v2/registry")["agents"]}
    assert set(agents) == {"capstone/independent_validation", "coder/independent_validation"}
    assert agents["capstone/independent_validation"]["color"] != agents["coder/independent_validation"]["color"]
    assert agents["capstone/independent_validation"]["display_name"] == "Ticketing Independent Validator"
    orchs = {(o["app_id"], o["orchestrator_id"]): o["display_name"]
             for o in env.store.registry()["orchestrators"]}
    assert orchs[("capstone", "main")] == "Ticketing main"           # coder's heartbeat changed only its own


def test_two_applications_share_a_head_with_distinct_queue_states(env):
    ticketing = setup_ticketing(env)
    coder = env.app("coder", "Coder")
    coder.publish([env.ev("agent.registered", agent_id="python_validator", display_name="Coder Python Validator",
                          role="validation")])
    ticketing.publish([env.ev("assignment.created", assignment_id="A", agent_id="independent_validation",
                              kind="inference", location="head:head-192-0-2-3", state="executing")])
    coder.publish([env.ev("assignment.created", assignment_id="A", agent_id="python_validator", kind="inference",
                          location="head:head-192-0-2-3", state="waiting", wait_reason="capacity")])
    snap = env.viewer("head-192-0-2-3").snapshot("location:head:head-192-0-2-3")
    got = sorted((a["assignment_key"], a["state"]) for a in snap["assignments"])
    assert got == [("capstone/A", "executing"), ("coder/A", "waiting")]


def test_head_permission_and_unknown_agent(env):
    producer = env.app("capstone", "Ticketing")
    env.head("head-192-0-2-9", apps=("coder",))
    producer.publish([env.ev("agent.registered", agent_id="a", display_name="A", role="r")])
    r = producer.publish([env.ev("assignment.created", assignment_id="1", agent_id="a", kind="inference",
                                 location="head:head-192-0-2-9"),
                          env.ev("assignment.created", assignment_id="2", agent_id="ghost", kind="tool"),
                          env.ev("assignment.created", assignment_id="3", agent_id="a", kind="inference",
                                 location="head:head-unknown")])
    assert statuses(r) == ["rejected:location:head_not_permitted", "rejected:agent_id:unknown_agent",
                           "rejected:location:unknown_head"]


def test_duplicates_out_of_order_and_late_completion(env):
    producer = setup_ticketing(env)
    create = env.ev("assignment.created", -30, assignment_id="T", agent_id="independent_validation", kind="inference")
    assert statuses(producer.publish([create])) == ["accepted"]
    assert statuses(producer.publish([create])) == ["duplicate"]
    to6 = env.ev("assignment.moved", -5, assignment_id="T", to_location="head:head-192-0-2-6", state="executing")
    late_done_on_3 = env.ev("assignment.state", -10, assignment_id="T", state="completed")
    r = producer.publish([to6, late_done_on_3])
    assert [x["status"] for x in r["results"]] == ["accepted", "recorded"] and r["results"][1]["note"] == "late"
    a = env.store.snapshot()["assignments"][0]
    assert (a["location_id"], a["state"]) == ("head:head-192-0-2-6", "executing")


def test_terminal_assignments_do_not_resurrect(env):
    producer = setup_ticketing(env)
    producer.publish([env.ev("assignment.created", assignment_id="T", agent_id="independent_validation", kind="tool"),
                      env.ev("assignment.state", assignment_id="T", state="failed", visual_action="error_handling")])
    r = producer.publish([env.ev("assignment.state", 1, assignment_id="T", state="executing")])
    assert r["results"][0]["note"] == "terminal"
    assert env.store.snapshot()["assignments"][0]["state"] == "failed"


def test_stale_is_a_render_state_and_abandoned_leases_expire(env):
    producer = setup_ticketing(env)
    producer.publish([env.ev("orchestrator.heartbeat", heartbeat_seconds=300),
                      env.ev("assignment.created", assignment_id="L", agent_id="independent_validation",
                             kind="inference", state="executing", lease_seconds=5)])
    assert env.store.snapshot()["assignments"][0]["render_state"] == "executing"
    env.store.db.execute("UPDATE assignments SET lease_expires_at=?", (time.time() - 1,))
    a = env.store.snapshot()["assignments"][0]
    assert (a["state"], a["render_state"]) == ("executing", "stale")             # not failed
    assert env.store.maintain(now=time.time() + 4000) == 1
    assert env.store.snapshot()["assignments"] == []
    assert env.store.events_after(0)["events"][-1]["note"] == "service:lease_abandoned"


def test_producer_offline_render_state(env):
    producer = setup_ticketing(env)
    producer.publish([env.ev("assignment.created", assignment_id="P", agent_id="independent_validation",
                             kind="orchestration", state="executing")])
    env.store.db.execute("UPDATE orchestrators SET last_seen=?", (time.time() - 1000,))
    assert env.store.snapshot()["assignments"][0]["render_state"] == "producer_offline"


def test_concurrent_instances_of_one_agent(env):
    producer = setup_ticketing(env)
    producer.publish([env.ev("assignment.created", assignment_id=f"C{n}", agent_id="independent_validation",
                             kind="inference", location=f"head:head-192-0-2-{n}", state="executing") for n in (3, 6)])
    snap = env.store.snapshot()
    assert len(snap["agents"]) == 1 and len(snap["assignments"]) == 2
    assert {a["location_id"] for a in snap["assignments"]} == {"head:head-192-0-2-3", "head:head-192-0-2-6"}


def test_snapshot_matches_last_applied_event_results(env):
    producer = setup_ticketing(env)
    batch = [env.ev("assignment.created", assignment_id=f"S{i}", agent_id="independent_validation", kind="tool")
             for i in range(5)]
    batch += [env.ev("assignment.state", assignment_id="S1", state="executing"),
              env.ev("assignment.moved", assignment_id="S2", to_location="head:head-192-0-2-3"),
              env.ev("assignment.state", assignment_id="S3", state="canceled")]
    producer.publish(batch)
    last = {}
    for e in env.store.events_after(0)["events"]:
        if e["applied"] and e["result"] and "assignment_key" in e["result"]:
            last[e["result"]["assignment_key"]] = (e["result"]["location_id"], e["result"]["state"])
    snap = {a["assignment_key"]: (a["location_id"], a["state"]) for a in env.store.snapshot()["assignments"]}
    assert snap == last


def test_retention_forces_resnapshot(tmp_path):
    from arena_service.tests.conftest import Env
    e = Env(tmp_path, retention_days=1)
    try:
        producer = setup_ticketing(e)
        producer.publish([e.ev("orchestrator.heartbeat") for _ in range(3)])
        viewer = e.viewer("head-192-0-2-3")
        e.store.maintain(now=time.time() + 2 * 86400)
        producer.publish([e.ev("orchestrator.heartbeat")])
        with pytest.raises(ArenaError) as gone:
            viewer.events(1)
        assert gone.value.status == 410
    finally:
        e.close()


def test_rate_limit(tmp_path):
    from arena_service.tests.conftest import Env
    e = Env(tmp_path, producer_events_per_second=1, producer_event_burst=3)
    try:
        producer = setup_ticketing(e)                     # uses 1 token
        producer.publish([e.ev("orchestrator.heartbeat"), e.ev("orchestrator.heartbeat")])
        with pytest.raises(ArenaError) as limited:
            producer.publish([e.ev("orchestrator.heartbeat")] * 2)
        assert limited.value.status == 429
    finally:
        e.close()


def test_contract_and_payload_validation(env):
    producer = setup_ticketing(env)
    with pytest.raises(ArenaError) as old:
        producer.request("POST", "/v2/events", {"contract": "arena.v1", "events": [env.ev("orchestrator.heartbeat")]})
    assert old.value.status == 426
    r = producer.publish([
        env.ev("orchestrator.heartbeat", prompt="patient name ..."),
        env.ev("assignment.created", assignment_id="Z", agent_id="independent_validation", kind="tool",
               label="see https://example.test/x"),
        env.ev("assignment.created", assignment_id="Z", agent_id="independent_validation", kind="tool",
               label="serial 123456789 lookup"),
        env.ev("assignment.created", assignment_id="Z", agent_id="independent_validation", kind="tool",
               label="contact bob@example.test"),
        env.ev("assignment.created", assignment_id="Z", agent_id="independent_validation", kind="tool", state="done"),
        dict(env.ev("orchestrator.heartbeat"), occurred_at="2026-01-01T00:00:00"),
    ])
    assert statuses(r) == ["rejected:field:unknown:prompt", "rejected:label:sensitive_pattern",
                           "rejected:label:sensitive_pattern", "rejected:label:invalid_text",
                           "rejected:state:unknown", "rejected:occurred_at:timezone_required"]


def test_enrollment_binding_reuse_address_and_revocation(env):
    env.head("head-192-0-2-3", address="127.0.0.1")
    env.head("head-192-0-2-4", address="192.0.2.4")
    code = env.store.issue_enrollment("head-192-0-2-3")
    cid, secret, response = aclient.enroll(env.url, "head-192-0-2-3", code.lower().replace("-", " "), "m" * 32)
    assert response["role"] == "head" and response["location_id"] == "head:head-192-0-2-3"
    with pytest.raises(ArenaError) as reused:
        aclient.enroll(env.url, "head-192-0-2-3", code, "m" * 32)
    assert reused.value.status == 403
    with pytest.raises(ArenaError) as wrong_place:
        aclient.enroll(env.url, "head-192-0-2-4", env.store.issue_enrollment("head-192-0-2-4"), "m" * 32)
    assert wrong_place.value.code == "address_mismatch"
    with pytest.raises(ArenaError):
        aclient.enroll(env.url, "head-192-0-2-3", "ARENA-AAAA-BBBB-CCCC-DDDD", "m" * 32)
    viewer = aclient.Client(env.url, cid, secret)
    assert viewer.request("GET", "/v2/config")["display_id"] == "head-192-0-2-3"
    env.store.revoke(cid)
    with pytest.raises(ArenaError) as revoked:
        viewer.request("GET", "/v2/config")
    assert revoked.value.status == 403


def test_viewer_rotation_delivered_through_config(env):
    env.head("head-192-0-2-3")
    viewer = env.viewer("head-192-0-2-3")
    new_id, _ = env.store.rotate(viewer.credential_id)
    rotation = viewer.request("GET", "/v2/config")["rotation"]
    assert rotation["credential_id"] == new_id
    new_secret = crypto.unseal(viewer.key, rotation["sealed_secret"]).decode()
    assert aclient.Client(env.url, new_id, new_secret).request("GET", "/v2/config")["credential_id"] == new_id


def test_service_impersonation_is_detected(env, tmp_path):
    env.head("head-192-0-2-3")
    viewer = env.viewer("head-192-0-2-3")
    (tmp_path / "impostor").mkdir()
    impostor = type(env)(tmp_path / "impostor")
    try:
        fake = aclient.Client(impostor.url, viewer.credential_id, "not-the-secret")
        fake.key = viewer.key                                     # same credential, different service
        with pytest.raises(ServiceIdentityError):
            fake.request("GET", "/v2/health")
    finally:
        impostor.close()


def test_long_poll_wakes_on_new_event(env):
    producer = setup_ticketing(env)
    viewer = env.viewer("head-192-0-2-3")
    start = viewer.events(0)["next"]
    threading.Timer(0.4, lambda: producer.publish([env.ev("orchestrator.heartbeat")])).start()
    began = time.monotonic()
    got = viewer.events(start, wait=10)
    assert got["events"] and time.monotonic() - began < 5


def test_telemetry_scope(env):
    env.head("head-192-0-2-3")
    producer = env.app("capstone", "Ticketing")
    env.store.add_display("orch-display", "orchestrator", "orch:capstone/main")
    head = env.viewer("head-192-0-2-3")
    sample = {"sampled_at": time.time(), "cpu_percent": 12.5, "gpus": [],
              "validity": {"gpu": "unavailable:no_nvidia_smi"}}
    assert head.request("POST", "/v2/telemetry", sample)["node_id"] == "head-192-0-2-3"
    with pytest.raises(ArenaError) as denied:
        env.viewer("orch-display").request("POST", "/v2/telemetry", sample)
    assert denied.value.status == 403
    with pytest.raises(ArenaError):
        head.request("POST", "/v2/telemetry", dict(sample, raw_prompt="x"))
    assert env.store.registry()["heads"][0]["telemetry"]["validity"]["gpu"].startswith("unavailable")
    assert producer


def test_coordinator_setup_is_idempotent_and_enrolls_heads(tmp_path):
    import json as _json
    from arena_service import secretfile
    from arena_service.setup_coordinator import main as setup
    from arena_service.store import Store
    args = ["--data", str(tmp_path), "--host", "127.0.0.1", "--port", "0", "--head", "192.0.2.11",
            "--capstone-db", str(tmp_path / "operations.db")]
    assert setup(args) == 0 and setup(args) == 0                       # second run changes nothing
    store = Store(_json.loads((tmp_path / "aes.config.json").read_text())["db_path"])
    heads = [h["node_id"] for h in store.registry()["heads"]]
    assert heads[0] == "head-192-0-2-10" and "head-192-0-2-11" in heads and len(heads) == 10
    assert store.display("head-192-0-2-3")["address"] == "192.0.2.3"
    assert store.display("orch-capstone")["location_id"] == "orch:capstone/main"
    cid, secret = secretfile.read(str(tmp_path / "ticketing-producer.dpapi"))
    assert store.credential(cid)["app_id"] == "capstone" and len([c for c in store.list_credentials() if c["kind"] == "producer"]) == 1
    adapter = _json.loads((tmp_path / "ticketing-adapter.json").read_text())
    assert adapter["start_from"] == "now" and adapter["capstone_db"].endswith("operations.db")


def test_whoami_proposes_identity_from_the_registry_only(env):
    with urllib.request.urlopen(env.url + "/v2/whoami") as r:
        assert json.load(r)["displays"] == []                      # nothing registered here: no guess
    env.head("head-192-0-2-3", address="192.0.2.3")
    env.head("head-192-0-2-11", address="127.0.0.1")              # any address, not only .2-.10
    env.app("coder", "Coder")
    env.store.add_display("orch-coder-main", "orchestrator", "orch:coder/main", "127.0.0.1")
    with urllib.request.urlopen(env.url + "/v2/whoami") as r:
        body = json.load(r)
    assert body["address"] == "127.0.0.1" and body["service_id"] == env.store.meta("service_id")
    assert [(d["display_id"], d["kind"]) for d in body["displays"]] == \
        [("head-192-0-2-11", "head"), ("orch-coder-main", "orchestrator")]
    env.store.remove_display("orch-coder-main")
    with urllib.request.urlopen(env.url + "/v2/whoami") as r:
        assert [d["display_id"] for d in json.load(r)["displays"]] == ["head-192-0-2-11"]


def test_refused_enrollment_code_is_reported_as_refused_not_impersonation(env):
    from arena_service.client import CredentialRefused
    env.head("head-192-0-2-3", address="127.0.0.1")
    code = env.store.issue_enrollment("head-192-0-2-3")
    aclient.enroll(env.url, "head-192-0-2-3", code, "m" * 32)
    with pytest.raises(CredentialRefused) as used:
        aclient.enroll(env.url, "head-192-0-2-3", code, "m" * 32)
    assert used.value.code == "code_refused" and "already used" in used.value.message
    assert not isinstance(used.value, ServiceIdentityError)


def test_removed_head_takes_no_work_and_its_display_is_revoked(env):
    producer = setup_ticketing(env)
    viewer = env.viewer("head-192-0-2-6")
    displays, open_count = env.store.remove_head("head-192-0-2-6")
    assert displays == ["head-192-0-2-6"] and open_count == 0
    assert [h["node_id"] for h in env.store.registry()["heads"]] == ["head-192-0-2-3"]
    r = producer.publish([env.ev("assignment.created", assignment_id="T9", agent_id="independent_validation",
                                 kind="inference", location="head:head-192-0-2-6")])
    assert statuses(r) == ["rejected:location:unknown_head"]
    with pytest.raises(ArenaError) as revoked:
        viewer.request("GET", "/v2/config")
    assert revoked.value.status == 403
    env.head("head-192-0-2-6")                                     # re-adding restores the head
    assert len(env.store.registry()["heads"]) == 2


def test_admin_adds_and_removes_heads_by_address(tmp_path, capsys):
    from arena_service.admin import main as admin
    cfg = tmp_path / "aes.json"
    cfg.write_text(json.dumps({"db_path": str(tmp_path / "aes.db")}))
    assert admin(["--config", str(cfg), "head-add", "192.0.2.12"]) == 0
    assert admin(["--config", str(cfg), "enroll", "head-192-0-2-12", "--out", str(tmp_path / "c.txt")]) == 0
    assert (tmp_path / "c.txt").read_text().startswith("ARENA-")
    assert admin(["--config", str(cfg), "head-remove", "head-192-0-2-12"]) == 0
    assert admin(["--config", str(cfg), "displays"]) == 0
    assert '"status": "disabled"' in capsys.readouterr().out
    from arena_service.store import Store
    assert Store(str(tmp_path / "aes.db"))._one("SELECT display_name FROM heads")["display_name"] == "Head 12 (192.0.2.12)"
