"""The admin hub's Fleet window: tiers from the heads' own reports, the merged view, pending changes, and
Commit (a request file and an administrator prompt; Arena itself changes nothing in the Gateway)."""
import json
import threading
import time
from pathlib import Path

import pytest

from arena_service import admin_console, fleet as fl

TOKEN = "test-token-123"
CATALOG = Path(__file__).resolve().parents[2] / "lab-provisioner" / "config" / "model-catalog.json"
GB = 1024 ** 3


def tiers():
    return json.loads(CATALOG.read_text(encoding="utf-8"))["tiers"]


def test_tier_from_the_heads_own_report():
    t = tiers()
    assert fl.tier_for({"ram_total_bytes": int(7.6 * GB), "gpus": []}, t)["id"] == "CPU8"
    assert fl.tier_for({"ram_total_bytes": int(15.6 * GB), "gpus": []}, t)["id"] == "CPU16"
    assert fl.tier_for({"ram_total_bytes": int(31.4 * GB), "gpus": []}, t)["id"] == "CPU32", "head 10"
    gpu = fl.tier_for({"ram_total_bytes": 64 * GB, "gpus": [{"name": "NVIDIA GeForce RTX 4060 Ti", "vram_total_bytes": 16380 * 1024 * 1024}]}, t)
    assert gpu["id"] == "GPU16" and gpu["gpu"].endswith("4060 Ti"), "head 2"
    assert fl.tier_for({"gpus": [{"vram_total_bytes": 24 * GB}]}, t)["id"] == "GPU24"
    assert fl.tier_for(None, t)["id"] is None
    assert fl.tier_for(None, t, override="CPU16")["source"] == "chosen by the operator"


def test_tier_from_the_labs_head_list_when_the_report_lacks_it():
    t = tiers()
    inv2 = {"address": "192.0.2.2", "ram_gb": 63.9, "gpu": "RTX 4060 Ti 16 GB", "vram_gb": 16.0}
    # Head 2's viewer reports its RAM but not its GPU (field, 2026-11-09): it was filed as a 32 GB CPU head.
    t2 = fl.tier_for({"ram_total_bytes": int(63.9 * GB), "gpus": []}, t, inventory=inv2)
    assert t2["id"] == "GPU16" and t2["vram_gb"] == 16.0 and "GPU from the lab's head list" in t2["source"]
    # No viewer at all: everything from the list.
    t10 = fl.tier_for(None, t, inventory={"address": "192.0.2.10", "ram_gb": 31.4, "gpu": None, "vram_gb": None})
    assert t10["id"] == "CPU32" and t10["source"] == "from the lab's head list"
    # The head's own report still wins where it has the fact; the operator's choice wins over both.
    assert fl.tier_for({"ram_total_bytes": int(15.4 * GB), "gpus": []}, t, inventory={"ram_gb": 31.4})["id"] == "CPU16"
    assert fl.tier_for(None, t, override="CPU8", inventory=inv2)["id"] == "CPU8"
    assert "choose its Tier" in fl.tier_for(None, t)["label"]


def test_one_slow_answer_does_not_make_a_head_vanish():
    first = fl.merge_scan({}, {"192.0.2.7": {"version": "0.40.13", "models": []}}, now=1000)
    assert first["192.0.2.7"]["late"] is False
    kept = fl.merge_scan(first, {}, now=1100)
    assert kept["192.0.2.7"]["late"] is True, "head 7 missed one scan: still listed, marked late"
    assert fl.merge_scan(kept, {}, now=1300) == {}, "gone after 3 minutes without an answer"
    assert fl.scan_heads.__defaults__[0] >= 3, "1 s was too short for a busy head"


def test_changes_are_checked_before_they_are_kept():
    assert fl.check_change({"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"}) == {"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"}
    assert fl.check_change({"op": "head-projects", "address": "192.0.2.3", "applications": ["b", "a", "a"]})["applications"] == ["a", "b"]
    for bad in ({"op": "install", "address": "192.0.2.254", "model": "x:1"}, {"op": "install", "address": "192.0.2.6", "model": "a b"},
                {"op": "run", "address": "192.0.2.6"}, {"op": "allow", "app": "", "model": "x:1"}, {"op": "head-projects", "address": "192.0.2.3", "applications": []}):
        with pytest.raises(ValueError):
            fl.check_change(bad)


@pytest.fixture
def fleet_env(env, tmp_path):
    fdir = tmp_path / "fleet"
    for d in ("catalog", "published", "requests", "status"):
        (fdir / d).mkdir(parents=True)
    (fdir / "catalog" / "model-catalog.json").write_text(CATALOG.read_text(encoding="utf-8"), encoding="utf-8")
    helper = tmp_path / "fleet_commit.py"
    helper.write_text("# stand-in\n")
    launched = []
    env.store.add_display(*_head(env.store, "192.0.2.6"))
    env.store.put_telemetry("head-192-0-2-6", {"sampled_at": time.time(), "ram_total_bytes": int(7.6 * GB), "gpus": [], "ollama": {"reachable": True}})
    (fdir / "published" / "fleet-state.json").write_text(json.dumps({
        "schema": fl.STATE_SCHEMA, "written_at": time.time(), "gateway": {"release": "0.3.0"},
        "applications": [{"app_id": "capstone", "enabled": True, "models": ["qwen3.5:4b"]},
                         {"app_id": "capstone-research", "enabled": True, "models": ["qwen3.5:4b", "llama3.2:1b"]}],
        "heads": [{"resource_id": "head-192-0-2-6", "address": "192.0.2.6", "applications": ["capstone-research"], "health": "healthy", "qualified": ["llama3.2:1b"]}],
        "do_not_use": [{"resource_id": "head-192-0-2-6", "model": "gemma3:1b"}], "library": [], "failed_tests": [], "last_commit": None}), encoding="utf-8")
    scan = {"192.0.2.6": {"version": "0.40.13", "models": [{"name": "llama3.2:1b", "digest": "d", "size": 1}, {"name": "gemma3:1b", "digest": "e", "size": 1},
                                                           {"name": "granite4:3b", "digest": "f", "size": 1}]},
            "192.0.2.11": {"version": "0.40.13", "models": []}}
    cfg = dict(env.config, port=env.server.server_address[1], fleet={"dir": str(fdir), "helper": str(helper), "python": "python.exe"})
    server, c = admin_console.build(cfg, port=0, token=TOKEN)
    c.store = env.store
    c.fleet.store = env.store
    c.fleet.scanner = lambda addresses: scan
    c.fleet.launcher = lambda python, script, args: launched.append((python, script, list(args)))
    c.fleet.scan_now(wait=True)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    c.port = server.server_address[1]
    yield c, fdir, launched
    server.shutdown()
    server.server_close()


def _head(store, address):
    from arena_service.admin import head_identity
    node, label = head_identity(address)
    location = store.add_head(node, label, address)
    return node, "head", location, address


def call(c, method, path, body=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=5)
    headers = {"Host": f"127.0.0.1:{c.port}", "X-Arena-Admin-Token": TOKEN}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body), "application/json"
    conn.request(method, path, data, headers)
    r = conn.getresponse()
    out = json.loads(r.read())
    conn.close()
    return r.status, out


def test_the_view_merges_arena_the_scan_the_gateway_and_the_catalog(fleet_env):
    c, fdir, _ = fleet_env
    status, v = call(c, "GET", "/api/fleet")
    assert status == 200
    h6 = next(h for h in v["heads"] if h["address"] == "192.0.2.6")
    assert h6["tier"]["id"] == "CPU8" and h6["in_arena"] and h6["reachable"]
    assert h6["gateway"]["applications"] == ["capstone-research"] and h6["gateway"]["qualified"] == ["llama3.2:1b"]
    assert h6["do_not_use"] == ["gemma3:1b"]
    assert h6["too_large"] == ["granite4:3b"], "the plan says no for 8 GB"
    assert h6["planned_missing"], "the catalog plans more for CPU8"
    new = next(h for h in v["heads"] if h["address"] == "192.0.2.11")
    assert new["new"] and not new["in_arena"] and not new["gateway"]["in_gateway"]
    coder = next(p for p in v["projects"] if p["app_id"] == "coder")
    assert not coder["in_gateway"] and "not connected" in coder["note"], "coder is visible before it has a Gateway login"
    research = next(p for p in v["projects"] if p["app_id"] == "capstone-research")
    assert research["heads"] == ["192.0.2.6"] and research["models"] == ["qwen3.5:4b", "llama3.2:1b"]
    assert v["catalog"]["models"] and v["health"]["new"] == 1 and v["helper_installed"]


def test_commit_writes_the_request_and_asks_for_an_administrator(fleet_env):
    c, fdir, launched = fleet_env
    assert call(c, "POST", "/api/fleet/commit", {})[0] == 400, "nothing to commit"
    changes = [{"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"}, {"op": "install", "address": "192.0.2.6", "model": "smollm2:135m"},
               {"op": "head-projects", "address": "192.0.2.11", "applications": ["capstone", "capstone-research"]}]
    status, p = call(c, "POST", "/api/fleet/pending", {"changes": changes})
    assert status == 200 and len(p["changes"]) == 2, "duplicates dropped"
    assert call(c, "POST", "/api/fleet/pending", {"changes": [{"op": "install", "address": "192.0.2.254", "model": "x:1"}]})[0] == 400
    status, r = call(c, "POST", "/api/fleet/commit", {})
    assert status == 200 and launched[0][2] == ["--commit", r["id"]]
    req = json.loads((fdir / "requests" / f"{r['id']}.json").read_text(encoding="utf-8"))
    assert req["schema"] == "agent-arena.fleet-request/1" and len(req["changes"]) == 2
    # the helper reports; while it runs the list is locked, and a successful Commit empties it
    (fdir / "status" / f"{r['id']}.json").write_text(json.dumps({"id": r["id"], "state": "running", "lines": [], "results": []}))
    assert call(c, "POST", "/api/fleet/commit", {})[0] == 400
    assert call(c, "POST", "/api/fleet/pending", {"changes": []})[0] == 400
    kept = [fl.check_change(x) for x in changes[1:]]
    results = [{"change": fl.describe(kept[0]), "ok": True, "detail": "installed"}, {"change": fl.describe(kept[1]), "ok": False, "detail": "refused"}]
    (fdir / "status" / f"{r['id']}.json").write_text(json.dumps({"id": r["id"], "state": "done", "ok": False, "lines": ["x"], "results": results}))
    v = call(c, "GET", "/api/fleet")[1]
    assert v["pending"] == [kept[1]], "what completed leaves the list; what failed stays to try again"
    assert v["commit"]["status"]["state"] == "done"
    assert call(c, "GET", "/api/fleet")[1]["pending"] == [kept[1]], "settled once, not again"


def test_after_a_declined_commit_every_change_stays():
    ch = [{"op": "install", "address": "192.0.2.6", "model": "a:1"}]
    assert fl.still_to_do(ch, {"state": "declined"}) == ch and fl.still_to_do(ch, None) == ch
    assert fl.still_to_do(ch, {"state": "done", "results": [{"change": fl.describe(ch[0]), "ok": True}]}) == []


def test_a_cancelled_administrator_prompt_changes_nothing(fleet_env):
    c, fdir, _ = fleet_env

    def refuse(*a):
        raise PermissionError("the administrator prompt was cancelled or refused; nothing was changed")
    c.fleet.launcher = refuse
    call(c, "POST", "/api/fleet/pending", {"changes": [{"op": "uninstall", "address": "192.0.2.6", "model": "gemma3:1b"}]})
    status, r = call(c, "POST", "/api/fleet/commit", {})
    assert status == 400 and "cancelled" in r["message"]
    assert not list((fdir / "requests").glob("*.json")), "no request left behind"
    assert call(c, "GET", "/api/fleet")[1]["pending"], "the list is kept for another try"


def test_tier_override_and_scan(fleet_env):
    c, fdir, _ = fleet_env
    assert call(c, "POST", "/api/fleet/tier", {"address": "192.0.2.6", "tier": "CPU16"})[0] == 200
    h6 = next(h for h in call(c, "GET", "/api/fleet")[1]["heads"] if h["address"] == "192.0.2.6")
    assert h6["tier"]["id"] == "CPU16" and h6["tier"]["source"] == "chosen by the operator"
    assert call(c, "POST", "/api/fleet/tier", {"address": "192.0.2.6", "tier": "NOPE"})[0] == 400
    assert call(c, "POST", "/api/fleet/scan", {})[0] == 200
