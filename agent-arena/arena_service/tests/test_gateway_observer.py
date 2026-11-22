"""The Inference Gateway observer against real shared.inference.observation/1 output.

The fixture is the gateway's own exporter (`inference_gateway.capacity_observe.snapshot`) run on a
scratch ledger built with the gateway's own broker: Capstone and Coder registered, heads .3 and .6
healthy, .7 degraded with a failed qualification, an infrastructure resource, one reservation
executing and one leased, two requests queued, an overload rejection. Arena imports no gateway code.
"""
import copy
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from arena_service import admin_console
from arena_service.adapters.gateway import observation as gw

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gateway_observation.json"
TOKEN = "test-token-123"


def doc():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def publish(path, document):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(document), encoding="utf-8")
    os.replace(tmp, path)       # the operator's atomic replacement
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # a distinct mtime even on coarse clocks


def test_real_observation_is_understood_in_full():
    d = doc()
    obs = gw.parse(d)
    assert obs["dropped"] == 0
    view = gw.summarize(obs, d["observed_at"] + 1)
    assert view["state"] == "ok" and view["admission_open"]
    assert [h["resource_id"] for h in view["heads"]] == ["head-192-0-2-3", "head-192-0-2-6", "head-192-0-2-7"]
    assert [r["resource_type"] for r in view["other_resources"]] == ["infrastructure_service"]   # never counted as a head
    assert (view["heads_up"], view["heads_total"], view["queued"], view["active"], view["executing"]) == (2, 3, 2, 2, 1)
    assert view["utilization"] == 1.0            # both eligible heads leased: allocation, not load
    assert {h["head_name"] for h in view["heads"]} == {"Head 3", "Head 6", "Head 7"}
    lease = {l["app_id"]: l for l in view["leases"]}
    assert lease["capstone"]["head"] == "head:head-192-0-2-3" and lease["capstone"]["executing"]
    assert lease["coder"]["head"] == "head:head-192-0-2-6" and not lease["coder"]["executing"]
    models = {m["model"]: m for m in view["models"]}
    assert models["llama3.1:8b"] == {"model": "llama3.1:8b", "available_on": ["head-192-0-2-3"],
                                     "listed_on": ["head-192-0-2-3", "head-192-0-2-7"]}
    failed = [q for q in view["qualifications"] if not q["passed"]]
    assert [(q["resource_id"], q["model"]) for q in failed] == [("head-192-0-2-7", "llama3.1:8b")]
    assert view["invocations"]["failed"] == 1 and view["requests"]["cancelled"] == 1


def test_only_allowlisted_fields_survive():
    d = doc()
    d["payload"] = "PRIVATE-PROMPT"
    d["resources"][1].update(endpoint="http://192.0.2.3:11434", owner="PRIVATE-OWNER")
    d["leases"][0]["client_key"] = "PRIVATE-KEY"
    d["events"][0].update(payload={"prompt": "PRIVATE"}, token_hash="PRIVATE")
    d["applications"][0]["token_hash"] = "PRIVATE"
    encoded = json.dumps(gw.summarize(gw.parse(d), d["observed_at"]))
    assert "PRIVATE" not in encoded and "11434" not in encoded and "endpoint" not in encoded


def test_bad_records_are_dropped_and_bad_documents_refused():
    d = doc()
    d["leases"][1]["status"] = "granted-by-arena"
    d["events"][0]["event_type"] = "<script>"
    d["resources"][0]["qualified_models"] = ["ok:1", "bad model name", 7]
    obs = gw.parse(d)
    assert obs["dropped"] == 2 and len(obs["leases"]) == 1 and obs["resources"][0]["qualified_models"] == ["ok:1"]
    for broken in ({**doc(), "contract": "shared.inference.observation/2"}, {**doc(), "admission_open": "yes"},
                   {**doc(), "leases": None}, {**doc(), "observed_at": float("nan")}, []):
        with pytest.raises(gw.ObservationError):
            gw.parse(broken)


def test_resource_heads_mapping():
    assert gw.head_for("head-192-0-2-3", {}) == ("head:head-192-0-2-3", "Head 3")
    assert gw.head_for("stable-head", {}) == (None, None)           # the gateway's own name: Arena can't place it
    assert gw.head_for("gpu-box", {"gpu-box": "head-192-0-2-2"}) == ("head:head-192-0-2-2", "Head 2")
    assert gw.head_for("gpu-box", {"gpu-box": "not-a-head"}) == (None, None)


def test_observer_follows_publications_and_deduplicates_events(tmp_path):
    path, d = tmp_path / "observation.json", doc()
    observer = gw.Observer(path, gateway_id="gw-test")
    assert observer.view(d["observed_at"])["state"] == "not_published"
    publish(path, d)
    first = observer.view(d["observed_at"] + 1)
    assert first["state"] == "ok" and first["cursor"] == 20 and len(first["recent_events"]) == 20
    assert first["trouble"] == {"overload.rejected": 1}
    publish(path, d)                                                # replayed publication: no duplicates
    assert len(observer.view(d["observed_at"] + 2)["recent_events"]) == 20
    later = copy.deepcopy(d)
    later["events"] = [dict(later["events"][-1], event_id=21, event_type="queued", at=d["observed_at"] + 3)]
    later["next_event_id"] = 21
    publish(path, later)
    view = observer.view(d["observed_at"] + 4)
    assert view["cursor"] == 21 and view["recent_events"][0]["event_id"] == 21 and len(view["recent_events"]) == 21

    assert observer.view(d["observed_at"] + 60)["state"] == "stale"
    reset = copy.deepcopy(d)
    reset["events"], reset["next_event_id"] = [], 3                  # a reset or foreign ledger
    publish(path, reset)
    view = observer.view(d["observed_at"] + 5)
    assert view["state"] == "resync" and view["cursor"] == 21
    assert any("resynchronize" in i["what"] for i in gw.issues(view))


def test_unreadable_publication_keeps_the_last_good_one(tmp_path):
    path, d = tmp_path / "observation.json", doc()
    observer = gw.Observer(path)
    publish(path, d)
    observer.view(d["observed_at"])
    path.write_text('{"contract": "shared.inference.obs', encoding="utf-8")   # a non-atomic writer, caught mid-write
    view = observer.view(d["observed_at"] + 1)
    assert view["state"] == "invalid" and view["heads_total"] == 3
    assert gw.issues(view)[0]["what"] == "observation unreadable"
    fresh = gw.Observer(tmp_path / "other.json")
    (tmp_path / "other.json").write_text("not json", encoding="utf-8")
    assert fresh.view()["state"] == "invalid"


def test_issues_name_the_head_and_the_failure(tmp_path):
    d = doc()
    d["admission_open"] = False
    path = tmp_path / "observation.json"
    publish(path, d)
    found = {(i["who"], i["what"]) for i in gw.issues(gw.Observer(path).view(d["observed_at"] + 1))}
    assert ("GATEWAY", "admission closed") in found
    assert ("HEAD 7", "gateway: degraded") in found
    assert ("HEAD 7", "failed qualification for llama3.1:8b") in found
    assert ("GATEWAY", "overload rejections in 5 min: 1") in found


# From the hub evidence of 2026-11-18 10:27 (DESKTOP-REDACT10): the published last_hour, plus the breakdown
# gateway 0.4.5 computes from that ledger. Eleven Capstone reviewer calls timed out on two healthy heads.
EVIDENCE_LAST_HOUR = {
    "window_seconds": 3600,
    "invocations_updated": {"active": 16, "cancelled": 1, "completed": 1674, "failed": 11},
    "events": {"invocation.deadline_exceeded": 12, "invocation.failed": 11},
    "invocation_failures": [
        {"resource_id": "head-192-0-2-5", "model": "qwen3:4b", "app_id": "capstone-research", "class": "deadline_exceeded",
         "count": 6, "last_at": -31.0},
        {"resource_id": "head-192-0-2-3", "model": "qwen3:4b", "app_id": "capstone-research", "class": "deadline_exceeded",
         "count": 5, "last_at": -89.4}],
    "invocation_failures_omitted": 0}


def evidence_doc(**last_hour):
    """The fixture with heads .3 and .5 healthy (as at 10:27) and the evidence's last hour, re-timed."""
    d = doc()
    five = dict(next(r for r in d["resources"] if r["resource_id"] == "head-192-0-2-3"), resource_id="head-192-0-2-5")
    d["resources"].append(five)
    hour = copy.deepcopy(EVIDENCE_LAST_HOUR)
    for row in hour["invocation_failures"]:
        row["last_at"] += d["observed_at"]
    hour.update(last_hour)
    d["last_hour"] = hour
    return d


def found(d):
    return [(i["who"], i["what"]) for i in gw.issues(gw.summarize(gw.parse(d), d["observed_at"] + 1))]


def test_request_timeouts_name_the_head_and_the_model():
    issues = found(evidence_doc())
    assert ("HEAD 5", "gateway: 6 qwen3:4b requests timed out in the last hour; head healthy") in issues
    assert ("HEAD 3", "gateway: 5 qwen3:4b requests timed out in the last hour; head healthy") in issues
    assert not any(who in ("HEAD 3", "HEAD 5") and "offline" in what for who, what in issues)   # request failures, not head down
    assert not any(who == "GATEWAY" and "failed requests" in what for who, what in issues)
    view = gw.summarize(gw.parse(evidence_doc()), 0)
    assert view["failed_last_hour"] == 11 and [(f["head_name"], f["count"], f["kind"]) for f in view["request_failures"]] == [
        ("Head 5", 6, "timed out"), ("Head 3", 5, "timed out")]


def test_no_recent_failures_no_warning_and_older_gateways_stay_quiet():
    healthy = evidence_doc(invocations_updated={"completed": 1700}, invocation_failures=[], invocation_failures_omitted=0)
    assert not any("request" in what for _, what in found(healthy))
    assert not any("request" in what for _, what in found(doc()))        # no last_hour at all (before 0.3.0)


def test_an_offline_head_keeps_its_own_warning_apart_from_its_failed_requests():
    d = evidence_doc(invocation_failures=[
        {"resource_id": "head-192-0-2-5", "model": "qwen3:4b", "app_id": "capstone-research", "class": "head_unreachable",
         "count": 1, "last_at": 0.0}])
    d["last_hour"]["invocation_failures"][0]["last_at"] = d["observed_at"]
    next(r for r in d["resources"] if r["resource_id"] == "head-192-0-2-5").update(health="offline", last_seen=0, fresh=False)
    issues = found(d)
    assert ("HEAD 5", "gateway: offline") in issues
    assert ("HEAD 5", "gateway: 1 qwen3:4b request failed: head unreachable in the last hour") in issues   # no "head healthy"
    assert not any(who == "HEAD 3" for who, _ in issues)


def test_failures_without_a_breakdown_or_a_head_or_beyond_the_cap():
    published_by_0_4_4 = evidence_doc()
    del published_by_0_4_4["last_hour"]["invocation_failures"], published_by_0_4_4["last_hour"]["invocation_failures_omitted"]
    assert found(published_by_0_4_4)[-1] == ("GATEWAY", "11 failed requests in the last hour (no per-head detail published)")
    unplaced = evidence_doc(invocation_failures_omitted=4, invocation_failures=[
        {"resource_id": None, "model": "qwen3:4b", "app_id": "capstone-research", "class": None, "count": 1, "last_at": 1.0}])
    issues = found(unplaced)
    assert ("GATEWAY", "1 qwen3:4b request failed in the last hour, before reaching a head") in issues
    assert ("GATEWAY", "4 more failed requests in the last hour") in issues


def test_failure_rows_are_allowlisted_and_bad_ones_dropped():
    d = evidence_doc()
    rows = d["last_hour"]["invocation_failures"]
    rows[0].update(error="PRIVATE timed out at http://192.0.2.5:11434", request_ids=["PRIVATE"])
    rows.append(dict(rows[1], model="bad model name"))
    rows.append(dict(rows[1], count=0))
    rows.append(dict(rows[1], **{"class": "<script>"}))           # an unknown class is kept, worded only as "failed"
    obs = gw.parse(d)
    assert obs["dropped"] == 2 and len(obs["last_hour"]["failures"]) == 3 and obs["last_hour"]["failures"][2]["class"] is None
    encoded = json.dumps(gw.summarize(obs, d["observed_at"]))
    assert "PRIVATE" not in encoded and "11434" not in encoded and "<script>" not in encoded
    d["last_hour"] = {"invocation_failures": "not a list", "invocations_updated": {"failed": -1}}
    obs = gw.parse(d)                                                 # malformed last_hour: unknown, never a refused document
    assert obs["last_hour"] == {"failed": None, "failures": None, "failures_omitted": 0}


@pytest.fixture
def console(env, tmp_path):
    path, d = tmp_path / "gateway-observation.json", doc()
    publish(path, d)
    config = dict(env.config, port=env.server.server_address[1], gateway={"observation_file": str(path), "stale_seconds": 10 ** 9})
    server, c = admin_console.build(config, port=0, token=TOKEN)
    c.store = env.store
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    c.port, c.observation = server.server_address[1], path
    yield c
    server.shutdown()
    server.server_close()


def test_console_shows_the_gateway_read_only_and_apart_from_ticketing(console, env):
    from arena_service.tests.test_admin_console import call
    env.app("capstone", "Ticketing")
    before = (console.observation.read_bytes(), os.stat(console.observation).st_mtime_ns)
    status, view = call(console, "GET", "/api/gateway")
    assert status == 200 and view["state"] == "ok"
    names = {a["app_id"]: a["arena_name"] for a in view["applications"]}
    assert names == {"capstone": "Ticketing", "coder": None}          # same app_id, Arena's name; Coder not onboarded here
    s = call(console, "GET", "/api/status")[1]
    assert s["gateway"] == {"state": "ok", "admission_open": True, "queued": 2, "active": 2, "executing": 1,
                            "heads_up": 2, "heads_total": 3, "utilization": 1.0}
    assert ("HEAD 7", "gateway: degraded") in {(i["who"], i["what"]) for i in s["issues"]}
    board = call(console, "GET", "/api/traffic")[1]
    assert board["departures"] == [] and board["arrivals"] == []      # gateway leases are not Ticketing workflow rows
    assert env.store.registry()["heads"] == []                         # nothing registered from the gateway
    assert (console.observation.read_bytes(), os.stat(console.observation).st_mtime_ns) == before
    assert call(console, "GET", "/api/gateway", token=None)[0] == 401


def test_console_lists_request_timeouts_in_its_issues(console):
    from arena_service.tests.test_admin_console import call
    publish(console.observation, evidence_doc())
    status, s = call(console, "GET", "/api/status")
    assert status == 200
    assert ("HEAD 5", "gateway: 6 qwen3:4b requests timed out in the last hour; head healthy") in {(i["who"], i["what"]) for i in s["issues"]}
    failures = call(console, "GET", "/api/gateway")[1]["request_failures"]
    assert [(f["resource_id"], f["model"], f["count"]) for f in failures] == [("head-192-0-2-5", "qwen3:4b", 6), ("head-192-0-2-3", "qwen3:4b", 5)]


def test_console_without_a_gateway(env):
    c = admin_console.Console(dict(env.config, port=env.server.server_address[1]))
    assert c.gateway_view() == {"state": "not_configured"} and c.status()["gateway"] is None


def test_arena_imports_no_gateway_code():
    import arena_service.admin_console  # noqa: F401
    import arena_service.adapters.gateway.observation  # noqa: F401
    assert not any(name.startswith("inference_gateway") for name in sys.modules)
