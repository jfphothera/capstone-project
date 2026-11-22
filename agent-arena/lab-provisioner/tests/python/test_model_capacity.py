"""APPLY-MODEL-CAPACITY (hub, provisioner 1.6.14): the qualification matrix from the HEAD-OLLAMA reports, the
Gateway's resources[].model_capacity (parallel = measured_parallel, passed entries only), and that every later hub
bootstrap (pressure test, SET-PARALLEL-PER-HEAD) carries it again."""
import json
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "pressure-test"))
sys.path.insert(0, str(TOOLS / "model-capacity"))
import model_capacity as mc  # noqa: E402
import pressure_test as pt  # noqa: E402
import set_parallel as sp  # noqa: E402

DIGEST = "sha256:" + "a" * 64


def entry(addr, model, passed=True, measured=3, setting=3, loaded=2, digest=DIGEST, version="0.40.15"):
    rid = "head-" + addr.replace(".", "-")
    return {"resource_id": rid, "address": addr, "model": model, "digest": digest, "runtime_version": version, "backend": "cpu", "num_ctx": 8192,
            "num_parallel_setting": setting, "measured_parallel": measured, "max_loaded_models": loaded, "max_queue": 16, "keep_alive": "30m",
            "flash_attention": "runtime default (not set)", "kv_cache_type": "runtime default (not set)", "memory": {"ram_gb": 31.4, "min_free_gb": 12.0},
            "throughput": {"serial_tokens_per_s": 10.0, "parallel_tokens_per_s": 22.0, "gain": 2.2}, "latency": {"p50_s": 20.0, "p95_s": 31.0, "queue_ms_avg": 40},
            "isolation_passed": passed, "passed": passed, "evidence": "reports\\X\\ollama-qualify-1.json: level 3 passed"}


def report(addr, entries, adopted=(3, 2), proof=True):
    return {"schema": "agent-arena.ollama-qualify/1", "address": addr, "generated_at": "2026-11-18T10:00:00",
            "qualification": {"adopted": {"num_parallel": adopted[0], "max_loaded_models": adopted[1]}, "final_proof": {"Version": "0.40.15"} if proof else None},
            "entries": entries}


def write(tmp, computer, stamp, doc):
    (tmp / computer).mkdir(exist_ok=True)
    (tmp / computer / ("ollama-qualify-%s.json" % stamp)).write_text(json.dumps(doc), encoding="utf-8")


def test_newest_report_per_head_and_the_matrix_has_every_contract_field(tmp_path):
    write(tmp_path, "DESKTOP-REDACT8", "20261118-090000", report("192.0.2.7", [entry("192.0.2.7", "qwen3.5:4b", measured=2)]))
    write(tmp_path, "DESKTOP-REDACT8", "20261118-100000", report("192.0.2.7", [entry("192.0.2.7", "qwen3.5:4b"), entry("192.0.2.7", "qwen3:4b")]))
    write(tmp_path, "DESKTOP-REDACT6", "20261118-110000", report("192.0.2.6", [entry("192.0.2.6", "qwen3.5:4b", passed=False, measured=1, setting=1, loaded=1)]))
    (tmp_path / "DESKTOP-REDACT8" / "ollama-qualify-20261118-120000.json").write_text("not json", encoding="utf-8")
    reps = mc.newest_reports(tmp_path)
    assert set(reps) == {"DESKTOP-REDACT8", "DESKTOP-REDACT6"} and reps["DESKTOP-REDACT8"][0].name.endswith("100000.json"), "an unreadable newer file is skipped"
    doc = mc.aggregate(reps, generated_at="t")
    assert doc["schema"] == "agent-arena.model-capacity/1"
    assert [(e["address"], e["model"]) for e in doc["entries"]] == [("192.0.2.6", "qwen3.5:4b"), ("192.0.2.7", "qwen3.5:4b"), ("192.0.2.7", "qwen3:4b")]
    assert all(tuple(e) == mc.ENTRY_KEYS for e in doc["entries"])
    assert set(mc.ENTRY_KEYS) == {"resource_id", "address", "model", "digest", "runtime_version", "backend", "num_ctx", "num_parallel_setting", "measured_parallel",
                                  "max_loaded_models", "max_queue", "keep_alive", "flash_attention", "kv_cache_type", "memory", "throughput", "latency",
                                  "isolation_passed", "passed", "evidence"}
    rows = mc.matrix_rows(doc["entries"])
    assert rows[1]["measured_parallel"] == 3 and rows[0]["passed"] is False and rows[1]["p95_s"] == 31.0


def test_policy_is_measured_parallel_for_passed_entries_only():
    es = [entry("192.0.2.7", "qwen3.5:4b", measured=3, setting=4), entry("192.0.2.7", "qwen3:4b", measured=2),
          entry("192.0.2.6", "qwen3.5:4b", passed=False), entry("192.0.2.8", "qwen3.5:4b", digest=""), entry("192.0.2.9", "qwen3.5:4b", version="")]
    pol = pt.model_capacity_policy(es)
    assert set(pol) == {"head-192-0-2-7"}, "failed, digest-less or version-less entries give the Gateway nothing"
    q = pol["head-192-0-2-7"]["qwen3.5:4b"]
    assert q == {"parallel": 3, "digest": DIGEST, "runtime_version": "0.40.15", "num_ctx": 8192, "evidence": "reports\\X\\ollama-qualify-1.json: level 3 passed"}
    assert pol["head-192-0-2-7"]["qwen3:4b"]["parallel"] == 2


def test_plan_sets_model_capacity_and_keeps_everything_else():
    base = [{"resource_id": "head-192-0-2-%d" % n, "endpoint": "http://192.0.2.%d:11434" % n, "resource_type": "shared_inference", "owner": "infrastructure",
             "applications": ["capstone"], "permitted_uses": ["inference"], "slots": 4, "memory_gb": 27.4, "health": "healthy"} for n in (6, 7)]
    pol = pt.model_capacity_policy([entry("192.0.2.7", "qwen3.5:4b")])
    planned = mc.plan(base, pol)
    assert planned[0] == base[0] and "model_capacity" not in planned[0]
    assert planned[1]["model_capacity"]["qwen3.5:4b"]["parallel"] == 3 and planned[1]["slots"] == 4
    doc = pt.resources_policy(planned)
    assert "model_capacity" not in doc["resources"][0], "only heads that have it declare it"
    assert doc["resources"][1]["model_capacity"] == pol["head-192-0-2-7"]
    assert set(doc["resources"][1]) == set(pt.RESOURCE_KEYS) | {"model_capacity"}
    # Gateway 0.4.4: an absent key keeps what it stored, {} clears it. A head whose newest report passed nothing is cleared.
    cleared = mc.plan(base, pol, {"head-192-0-2-6", "head-192-0-2-7"})
    assert cleared[0]["model_capacity"] == {} and pt.resources_policy(cleared)["resources"][0]["model_capacity"] == {}
    long = pt.model_capacity_policy([dict(entry("192.0.2.7", "qwen3.5:4b"), evidence="x" * 900)])
    assert len(long["head-192-0-2-7"]["qwen3.5:4b"]["evidence"]) == 400, "the Gateway refuses evidence over 400 characters"


def test_later_hub_bootstraps_carry_the_applied_capacity(tmp_path):
    led = tmp_path / "ledger.db"
    with sqlite3.connect(led) as c:
        c.execute("CREATE TABLE capacity_resources (resource_id, endpoint, resource_type, owner, applications, permitted_uses, health)")
        c.execute("CREATE TABLE capacity_resource_capacity (resource_id, slots, memory_gb)")
        for n in (6, 7):
            c.execute("INSERT INTO capacity_resources VALUES (?,?,?,?,?,?,?)", ("head-192-0-2-%d" % n, "http://192.0.2.%d:11434" % n, "shared_inference", "infrastructure", '["capstone"]', '["inference"]', "healthy"))
            c.execute("INSERT INTO capacity_resource_capacity VALUES (?,?,?)", ("head-192-0-2-%d" % n, 2, 27.4))
    cap = tmp_path / "model-capacity.json"
    cap.write_text(json.dumps({"schema": mc.SCHEMA, "entries": [entry("192.0.2.7", "qwen3.5:4b")]}), encoding="utf-8")
    res = pt.Hub({"release": str(tmp_path), "ledger": str(led), "python": "x", "model_capacity_file": str(cap)}).resources()
    assert "model_capacity" not in res[0] and res[1]["model_capacity"]["qwen3.5:4b"]["parallel"] == 3
    assert pt.Hub({"release": str(tmp_path), "ledger": str(led), "python": "x", "model_capacity_file": str(tmp_path / "absent.json")}).resources()[1].get("model_capacity") is None
    cap.write_text("{broken", encoding="utf-8")
    assert pt.load_model_capacity(cap) == {}


def test_gateway_version_gate_and_set_parallel_reads_head_ollama_reports(tmp_path):
    assert mc.release_version(r"C:\Program Files\InferenceGateway\releases\0.4.4") == (0, 4, 4)
    assert mc.release_version(r"C:\x\releases\0.4.3") < mc.GATEWAY_MIN <= mc.release_version(r"C:\x\releases\0.5.0")
    assert mc.release_version("C:\\x\\current") is None
    (tmp_path / "DESKTOP-REDACT8").mkdir()
    (tmp_path / "DESKTOP-REDACT8" / "head-parallel-20261112-160000.txt").write_text("head 192.0.2.7\nRESULT success: x; it now runs up to 10 requests at once.", encoding="utf-8")
    write(tmp_path, "DESKTOP-REDACT8", "20261118-100000", report("192.0.2.7", [], adopted=(3, 2)))
    write(tmp_path, "DESKTOP-REDACT6", "20261118-100000", report("192.0.2.6", [], adopted=(2, 1), proof=False))
    caps = sp.latest_caps(tmp_path)
    assert caps["192.0.2.7"] == 6, "the newer HEAD-OLLAMA report wins: 3 per model x 2 resident models"
    assert "192.0.2.6" not in caps, "an unfinished qualification allows nothing new"
    write(tmp_path, "DESKTOP-REDACT8", "20261118-120000", {"schema": "agent-arena.ollama-qualify/1", "address": "192.0.2.7", "rolled_back": True, "qualification": None,
                                                    "entries": [entry("192.0.2.7", "qwen3.5:4b", passed=False, measured=1, version="0.40.13")]})
    assert sp.latest_caps(tmp_path)["192.0.2.7"] == 1, "after a rollback the head is back to 1"
    doc = mc.aggregate(mc.newest_reports(tmp_path))
    assert "head-192-0-2-7" not in pt.model_capacity_policy(doc["entries"]), "and the hub clears its per-model limits"


def test_machine_class_sets_the_gateway_slots_and_memory_and_they_are_kept(tmp_path):
    """1.6.19: each proven report's machine_class gives the head its Gateway slots and memory_gb (classes decided with the
    lab operator on 2026-11-17); a report without one leaves that head as it is; head-slots.json keeps them."""
    r8 = dict(report("192.0.2.8", [entry("192.0.2.8", "qwen3.5:4b", measured=4, setting=4, loaded=3)]), resource_id="head-192-0-2-8",
              machine_class={"name": "32 GB", "ram_gb": 31.4, "vram_gb": None, "gateway_slots": 10, "gateway_memory_gb": 23.0})
    r3 = dict(report("192.0.2.3", [entry("192.0.2.3", "qwen3.5:4b")]), resource_id="head-192-0-2-3")
    write(tmp_path, "DESKTOP-REDACT7", "20261118-100000", r8)
    write(tmp_path, "DESKTOP-REDACT5", "20261118-100000", r3)
    heads = mc.head_settings(mc.newest_reports(tmp_path))
    assert heads == {"head-192-0-2-8": {"slots": 10, "memory_gb": 23.0, "class": "32 GB"}}
    base = [{"resource_id": "head-192-0-2-%d" % n, "endpoint": "e", "resource_type": "shared_inference", "owner": "infrastructure",
             "applications": ["capstone"], "permitted_uses": ["inference"], "slots": 8, "memory_gb": 27.4, "health": "healthy"} for n in (3, 8)]
    planned = mc.plan(base, {}, (), heads)
    assert (planned[0]["slots"], planned[0]["memory_gb"]) == (8, 27.4), "a head without a class is unchanged"
    assert (planned[1]["slots"], planned[1]["memory_gb"]) == (10, 23.0)
    old = {"schema": "agent-arena.head-slots/1", "by_class": {"32 GB": 16}, "slots": {"head-192-0-2-3": 8, "head-192-0-2-8": 10}}
    doc = mc.merge_slots_file(old, heads, "t")
    assert doc["slots"] == {"head-192-0-2-3": 8, "head-192-0-2-8": 10} and doc["memory_gb"] == {"head-192-0-2-8": 23.0} and doc["by_class"] == {"32 GB": 16}


def test_a_report_with_one_entry_written_as_an_object_is_read(tmp_path):
    """1.6.22: HEAD-OLLAMA up to 1.6.21 wrote a one-model head's entries as a single object (heads 3-6, 2026-11-17);
    the importer stopped with AttributeError: 'str' object has no attribute 'get' and applied no head."""
    one = dict(report("192.0.2.3", []), entries=entry("192.0.2.3", "qwen3.5:4b", measured=4, setting=4), resource_id="head-192-0-2-3")
    write(tmp_path, "DESKTOP-REDACT2", "20261117-195744", one)
    write(tmp_path, "DESKTOP-REDACT8", "20261117-190325", report("192.0.2.7", [entry("192.0.2.7", "qwen3.5:4b", measured=4, setting=4), entry("192.0.2.7", "qwen3:4b", measured=1, setting=4)]))
    doc = mc.aggregate(mc.newest_reports(tmp_path))
    pol = pt.model_capacity_policy(doc["entries"])
    assert pol["head-192-0-2-3"]["qwen3.5:4b"]["parallel"] == 4
    assert pol["head-192-0-2-7"]["qwen3.5:4b"]["parallel"] == 4 and pol["head-192-0-2-7"]["qwen3:4b"]["parallel"] == 1, "each model keeps its own measured limit"
    assert mc.entry_list(None) == [] and mc.entry_list({"a": 1}) == [{"a": 1}] and mc.entry_list(["x", {"b": 2}]) == [{"b": 2}]
    hub = tmp_path / "model-capacity.json"
    hub.write_text(json.dumps({"schema": mc.SCHEMA, "entries": entry("192.0.2.3", "qwen3.5:4b")}), encoding="utf-8")
    assert pt.load_model_capacity(hub)["head-192-0-2-3"]["qwen3.5:4b"]["parallel"] == 3, "later hub runs read a one-entry file too"


def test_operator_limit_caps_the_unmeasured_reviewer_on_16_gb_heads_only(tmp_path):
    """1.6.23: heads 3 and 5 ran 7-8 qwen3:4b calls at once at the 8-slot fallback (11 timeouts, 2026-11-18). The 16 GB
    class's operator_model_limits declares qwen3:4b at 1 there, with the head's own digest; measured entries win."""
    classes = mc.load_classes(Path(__file__).resolve().parents[2] / "config" / "lab-network.json")
    c16 = [c for c in classes if c["name"] == "16 GB"][0]
    assert c16["operator_model_limits"]["qwen3:4b"]["parallel"] == 1
    inv = {"models": [{"name": "qwen3:4b", "digest": "359d"}, {"name": "qwen3.5:4b", "digest": "d8b0"}]}
    r3 = dict(report("192.0.2.3", [entry("192.0.2.3", "qwen3.5:4b", measured=4, setting=4)], adopted=(4, 2)), resource_id="head-192-0-2-3",
              runtime={"version": "0.40.15"}, before=inv, machine_class={"name": "16 GB", "gateway_slots": 8, "gateway_memory_gb": 8.5})
    r7 = dict(report("192.0.2.7", [entry("192.0.2.7", "qwen3.5:4b", measured=4, setting=4), entry("192.0.2.7", "qwen3:4b", measured=1, setting=4)]),
              resource_id="head-192-0-2-7", runtime={"version": "0.40.15"}, before=inv, machine_class={"name": "32 GB", "gateway_slots": 10, "gateway_memory_gb": 23})
    r4 = dict(report("192.0.2.4", [entry("192.0.2.4", "qwen3.5:4b")], proof=False), resource_id="head-192-0-2-4", runtime={"version": "0.40.15"},
              before=inv, machine_class={"name": "16 GB"})
    reps = {"DESKTOP-REDACT2": (Path("a"), r3), "DESKTOP-REDACT8": (Path("b"), r7), "DESKTOP-REDACT4": (Path("c"), r4)}
    doc = mc.aggregate(reps)
    ops = mc.operator_entries(reps, classes, doc["entries"])
    assert [(e["resource_id"], e["model"], e["measured_parallel"], e["digest"], e["runtime_version"]) for e in ops] == [("head-192-0-2-3", "qwen3:4b", 1, "359d", "0.40.15")], \
        "only the proven 16 GB head; the 32 GB head keeps its measurement; an unproven report adds nothing"
    pol = pt.model_capacity_policy(doc["entries"] + ops)
    assert pol["head-192-0-2-3"] == {"qwen3.5:4b": pol["head-192-0-2-3"]["qwen3.5:4b"], "qwen3:4b": pol["head-192-0-2-3"]["qwen3:4b"]}
    assert pol["head-192-0-2-3"]["qwen3.5:4b"]["parallel"] == 4 and pol["head-192-0-2-3"]["qwen3:4b"]["parallel"] == 1
    assert "operator limit" in pol["head-192-0-2-3"]["qwen3:4b"]["evidence"] and pol["head-192-0-2-7"]["qwen3:4b"]["parallel"] == 1
    assert mc.load_classes(tmp_path / "absent.json") == []


def test_the_lab_placement_rule_goes_into_the_bootstrap_only_when_given():
    """1.6.23: config gateway.placement_policy.unmeasured_model_limit = 1 (every unmeasured model: 1 at once per head),
    sent by APPLY-MODEL-CAPACITY to Gateway 0.4.5+; other hub bootstraps send no placement block, so it is kept."""
    cfg = mc.load_config(Path(__file__).resolve().parents[2] / "config" / "lab-network.json")
    assert cfg["gateway"]["placement_policy"]["unmeasured_model_limit"] == 1
    r = [{"resource_id": "head-192-0-2-3", "endpoint": "e", "resource_type": "shared_inference", "owner": "infrastructure",
          "applications": ["capstone"], "permitted_uses": ["inference"], "slots": 8, "memory_gb": 8.5}]
    assert "placement_policy" not in pt.resources_policy(r), "pressure test, SET-PARALLEL: no block, the stored rule stays"
    assert pt.resources_policy(r, {"unmeasured_model_limit": 1})["placement_policy"] == {"unmeasured_model_limit": 1}
    assert mc.PLACEMENT_MIN == (0, 4, 5) and mc.release_version(r"C:\x\releases\0.4.4") < mc.PLACEMENT_MIN


def test_the_current_class_memory_wins_over_the_report_and_every_class_fits_its_heads_models():
    """1.6.24: head 6's report says memory_gb 2 (1.6.19), which refused every model over ~1.7 GB (SmolLM3-3B: admission
    deadline exceeded, 2026-11-18); the config's class value (3.4) is applied without re-running the head. And each class's
    memory_gb must hold the largest models those heads carry, as the Gateway estimates or measured them."""
    classes = mc.load_classes(Path(__file__).resolve().parents[2] / "config" / "lab-network.json")
    rep = dict(report("192.0.2.6", []), resource_id="head-192-0-2-6", machine_class={"name": "8 GB", "gateway_slots": 4, "gateway_memory_gb": 2})
    assert mc.head_settings({"DESKTOP-REDACT6": (Path("a"), rep)}, classes)["head-192-0-2-6"] == {"slots": 4, "memory_gb": 3.4, "class": "8 GB"}
    assert mc.head_settings({"DESKTOP-REDACT6": (Path("a"), rep)})["head-192-0-2-6"]["memory_gb"] == 2.0, "without a config: the report's value"
    mem = {c["name"]: c["gateway_memory_gb"] for c in classes}
    # Gateway estimate = disk size x 1.2 (model_size_factor), or the resident size HEAD-OLLAMA measured at 4 at once.
    for cls, need in (("8 GB", 2.43), ("8 GB", 1.78 * 1.2), ("16 GB", 7.0), ("32 GB", 8.64 * 1.2 + 5.2), ("GPU", 8.64 * 1.2 + 5.2)):
        assert mem[cls] >= need, "%s memory_gb %s cannot hold a %.2f GB model it serves" % (cls, mem[cls], need)


def test_no_report_changes_nothing(tmp_path, capsys):
    code = mc.main(["--python", "x", "--release", str(tmp_path / "releases" / "0.4.4"), "--reports", str(tmp_path), "--work", str(tmp_path / "w")])
    assert code == 2 and "Nothing was changed" in capsys.readouterr().out


def test_an_old_gateway_gets_the_matrix_but_no_bootstrap(tmp_path, capsys):
    write(tmp_path, "DESKTOP-REDACT8", "20261118-100000", report("192.0.2.7", [entry("192.0.2.7", "qwen3.5:4b")]))
    cap = tmp_path / "cap.json"
    code = mc.main(["--python", "x", "--release", str(tmp_path / "releases" / "0.4.3"), "--reports", str(tmp_path), "--work", str(tmp_path / "w"), "--capacity-file", str(cap)])
    out = capsys.readouterr().out
    assert code == 1 and "not applied" in out and "hub > 3 > 2" in out
    assert (tmp_path / "w" / "matrix.csv").exists() and (tmp_path / "w" / "model-capacity.json").exists()
    assert not cap.exists(), "the hub file is written only after the Gateway took it"
