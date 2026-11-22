"""The admin hub console: local-only and token-guarded, the same registry actions as arena-admin,
and status the room's monitor can show."""
import http.client
import json
import sqlite3
import threading
import time

import pytest

from arena_service import admin_console

TOKEN = "test-token-123"


@pytest.fixture
def console(env):
    server, c = admin_console.build(dict(env.config, port=env.server.server_address[1]), port=0, token=TOKEN)
    c.store = env.store         # same database as the running event service
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    c.port = server.server_address[1]
    yield c
    server.shutdown()
    server.server_close()


def call(c, method, path, body=None, token=TOKEN, host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{c.port}"}
    if token:
        headers["X-Arena-Admin-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, data, headers)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    return r.status, (json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw)


def test_api_needs_the_token_this_host_and_this_origin(console):
    assert call(console, "GET", "/api/status", token=None)[0] == 401
    assert call(console, "GET", "/api/status", token="wrong")[0] == 401
    assert call(console, "GET", "/api/status", host="evil.example:80")[0] == 403        # DNS rebinding
    assert call(console, "POST", "/api/backup", {}, origin="http://evil.example")[0] == 403
    assert call(console, "GET", "/api/status")[0] == 200


def test_page_is_served_without_secrets_and_only_from_frontend(console):
    status, page = call(console, "GET", "/", token=None)
    assert status == 200 and b"ADMIN HUB" in page and TOKEN.encode() not in page
    assert call(console, "GET", "/../arena_service/admin_console.py", token=None)[0] == 404
    assert call(console, "GET", "/assets/room-assets.txt", token=None)[0] == 404      # not a served type


def test_address_plan_roles():
    assert admin_console.role_for("192.0.2.2") == "head"
    assert admin_console.role_for("192.0.2.199") == "head"
    assert admin_console.role_for("192.0.2.200") == "orchestrator"
    assert admin_console.role_for("192.0.2.249") == "orchestrator"
    assert admin_console.role_for("192.0.2.254") == "admin"
    with pytest.raises(ValueError):
        admin_console.role_for("203.0.113.5")


def test_add_code_retire_restore_a_head(console, env):
    status, added = call(console, "POST", "/api/machines", {"address": "192.0.2.11"})
    assert status == 200 and added["display_id"] == "head-192-0-2-11"
    machine = next(m for m in call(console, "GET", "/api/status")[1]["machines"] if m["display_id"] == "head-192-0-2-11")
    assert machine["state"] == "not-enrolled" and machine["detail"] == "needs a code"

    status, code = call(console, "POST", "/api/codes", {"display_id": "head-192-0-2-11", "hours": 4})
    assert status == 200 and len(code["code"]) >= 8
    s = call(console, "GET", "/api/status")[1]
    assert s["codes"] == 1 and code["code"] not in json.dumps(s)            # shown once, never listed

    status, retired = call(console, "POST", "/api/machines/retire", {"display_id": "head-192-0-2-11"})
    assert status == 200
    machine = next(m for m in call(console, "GET", "/api/status")[1]["machines"] if m["display_id"] == "head-192-0-2-11")
    assert machine["state"] == "retired" and env.store.enrollment_for("head-192-0-2-11") is None
    assert call(console, "POST", "/api/codes", {"display_id": "head-192-0-2-11"})[0] == 400

    assert call(console, "POST", "/api/machines/restore", {"display_id": "head-192-0-2-11"})[0] == 200
    assert any(h["node_id"] == "head-192-0-2-11" for h in env.store.registry()["heads"])


def test_orchestrator_display_needs_its_orchestrator_and_admin_addresses_are_refused(console, env):
    env.store.add_application("coder", "Coder")
    env.store.add_orchestrator("coder", "main", "Coder main")
    assert call(console, "POST", "/api/machines", {"address": "192.0.2.248"})[0] == 400
    status, added = call(console, "POST", "/api/machines", {"address": "192.0.2.248", "location_id": "orch:coder/main"})
    assert status == 200 and added["display_id"] == "orch-coder-main-192-0-2-248"
    status, refused = call(console, "POST", "/api/machines", {"address": "192.0.2.254"})
    assert status == 400 and "admin" in refused["message"]


def test_status_reports_service_heads_and_issues(console, env):
    env.head("head-192-0-2-3")          # no address binding, so the test can enroll from 127.0.0.1
    env.head("head-192-0-2-4")
    env.viewer("head-192-0-2-3")
    env.viewer("head-192-0-2-4")
    env.store.put_telemetry("head-192-0-2-3", {"ollama": {"reachable": True, "version": "0.40.13"}})
    env.store.put_telemetry("head-192-0-2-4", {"ollama": {"reachable": False}})
    s = call(console, "GET", "/api/status")[1]
    assert s["service"]["up"] and s["service"]["version"]
    assert s["heads"] == {"up": 1, "total": 2}
    assert [i["what"] for i in s["issues"]] == ["Ollama not reachable"]
    states = {m["display_id"]: m["state"] for m in s["machines"]}
    assert states == {"head-192-0-2-3": "up", "head-192-0-2-4": "warn"}


def test_service_down_is_reported(tmp_path):
    config = {"host": "127.0.0.1", "port": 1, "db_path": str(tmp_path / "aes.db")}
    console = admin_console.Console(config)
    s = console.status()                      # Arena 2.5.1: one missed check is "slow to answer" (warn)
    assert s["service"]["slow"] and s["issues"][0]["level"] == "warn"
    s = console.status()                      # two in a row: down
    assert not s["service"]["up"] and s["issues"][0]["level"] == "down"


def test_endpoints_are_validated_and_published(console, env):
    assert call(console, "POST", "/api/endpoints", {"urls": ["ftp://192.0.2.254"]})[0] == 400
    assert call(console, "POST", "/api/endpoints", {"urls": []})[0] == 400
    status, r = call(console, "POST", "/api/endpoints", {"urls": ["http://192.0.2.254:8790", "http://192.0.2.1"]})
    assert status == 200 and env.store.meta("endpoints") == ["http://192.0.2.254:8790", "http://192.0.2.1:8790"]


def test_backup_is_a_consistent_copy(console, env):
    env.head("head-192-0-2-5", "192.0.2.5")
    status, r = call(console, "POST", "/api/backup", {})
    assert status == 200 and r["bytes"] > 0
    copy = sqlite3.connect(r["path"])
    assert copy.execute("SELECT COUNT(*) FROM heads").fetchone()[0] == 1
    copy.close()


def test_post_requires_json(console):
    conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=5)
    conn.request("POST", "/api/backup", "x", {"Host": f"127.0.0.1:{console.port}", "X-Arena-Admin-Token": TOKEN,
                                               "Content-Type": "text/plain"})
    assert conn.getresponse().status == 415
    conn.close()


def test_traffic_board_departures_and_arrivals(console, env):
    head = env.head("head-192-0-2-3")
    ticketing = env.app("capstone", "Ticketing")
    ticketing.publish([env.ev("agent.registered", agent_id="intake", display_name="Ticketing Intake", role="intake"),
                       *(env.ev("assignment.created", assignment_id=a, agent_id="intake", kind="inference")
                         for a in ("T1", "T2", "T3", "T4"))])
    ticketing.publish([env.ev("assignment.moved", assignment_id=a, to_location=head, state="waiting", wait_reason=w)
                       for a, w in (("T1", "capacity"), ("T2", "model"), ("T3", "model"), ("T4", "model"))])
    ticketing.publish([env.ev("assignment.state", assignment_id="T3", state="executing", lease_seconds=5)])
    board = call(console, "GET", "/api/traffic")[1]
    out = {t["status"] for t in board["departures"]}
    assert out == {"gate", "boarding", "running"} and not board["arrivals"]
    trip = board["departures"][0]
    assert trip["app_name"] == "Ticketing" and trip["head_name"] == "Head 3" and trip["head"] == head

    ticketing.publish([env.ev("assignment.moved", assignment_id="T1", to_location="orch:capstone/main", state="executing"),
                       env.ev("assignment.moved", assignment_id="T2", to_location="orch:capstone/main", state="executing",
                              visual_action="error_handling"),
                       env.ev("assignment.state", assignment_id="T4", state="canceled")])
    board = call(console, "GET", "/api/traffic")[1]
    assert [t["status"] for t in board["departures"]] == ["running"]
    assert [t["status"] for t in board["arrivals"]] == ["landed", "failed", "canceled"]
    assert all(t["head_name"] == "Head 3" for t in board["arrivals"])

    later = console.traffic(now=time.time() + 61)               # landed and canceled flap off; failed stays
    assert [t["status"] for t in later["arrivals"]] == ["failed"]
    assert [t["status"] for t in later["departures"]] == ["unknown"]   # T3's 5 s lease lapsed: never "failed"
    assert not console.traffic(now=time.time() + 301)["arrivals"]


def test_traffic_board_ignores_work_that_never_left_its_orchestrator(console, env):
    env.head("head-192-0-2-6")
    coder = env.app("coder", "Coder")
    coder.publish([env.ev("agent.registered", agent_id="writer", display_name="Coder Writer", role="code-generation"),
                   env.ev("assignment.created", assignment_id="C1", agent_id="writer", kind="orchestration"),
                   env.ev("assignment.state", assignment_id="C1", state="executing"),
                   env.ev("assignment.state", assignment_id="C1", state="completed")])
    board = call(console, "GET", "/api/traffic")[1]
    assert board["departures"] == [] and board["arrivals"] == []
    assert call(console, "GET", "/api/traffic", token=None)[0] == 401


def test_reserve_addresses_add_applications_and_issue_app_codes(console, env):
    env.head("head-192-0-2-2", "192.0.2.2")
    s = call(console, "GET", "/api/status")[1]
    assert s["next_free"] == {"head": "192.0.2.3", "app": "192.0.2.200"} and s["addresses"] == []
    status, r = call(console, "POST", "/api/addresses", {"address": "192.0.2.3", "role": "head", "label": "Head 3"})
    assert status == 200 and r["code"].startswith("ARENA-")
    assert call(console, "POST", "/api/addresses", {"address": "192.0.2.2", "role": "head"})[0] == 400   # registered already
    status, app = call(console, "POST", "/api/apps", {"app_id": "coder", "name": "Coder"})
    assert status == 200 and app["location_id"] == "orch:coder/main"
    assert call(console, "POST", "/api/apps", {"app_id": "Coder!", "name": "x"})[0] == 400
    status, r = call(console, "POST", "/api/addresses", {"address": "192.0.2.201", "role": "app", "app_id": "coder", "orchestrator_id": "main"})
    assert status == 200
    s = call(console, "GET", "/api/status")[1]
    assert [(a["address"], a["status"]) for a in s["addresses"]] == [("192.0.2.3", "reserved"), ("192.0.2.201", "reserved")]
    assert r["code"] not in json.dumps(s) and s["next_free"]["head"] == "192.0.2.4"
    coder = next(a for a in s["applications"] if a["app_id"] == "coder")["orchestrators"][0]
    assert coder["has_credential"] is False and coder["producer_state"] == "offline"
    status, code = call(console, "POST", "/api/app-codes", {"app_id": "coder"})
    assert status == 200 and code["code"].startswith("ARENA-")
    coder = next(a for a in call(console, "GET", "/api/status")[1]["applications"] if a["app_id"] == "coder")["orchestrators"][0]
    assert coder["code_expires_at"]
    assert call(console, "POST", "/api/addresses/release", {"address": "192.0.2.3"})[0] == 200
    assert call(console, "GET", "/api/status")[1]["next_free"]["head"] == "192.0.2.3"
