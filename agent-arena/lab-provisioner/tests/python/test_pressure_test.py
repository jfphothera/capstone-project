"""The hub pressure test (PRESSURE-TEST.cmd): which head settings each step declares, the numbers it computes from
the Gateway's records, and when it stops a head. Rehearsed in full on 2026-11-12 with the real Gateway 0.4.0 bootstrap
on a copy of the hub ledger (prepare, six steps, restore; qualifications untouched; heads restored)."""
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "pressure-test"))
import pressure_test as pt  # noqa: E402

BASE = [{"resource_id": "head-192-0-2-%d" % n, "endpoint": "http://192.0.2.%d:11434" % n, "resource_type": "shared_inference", "owner": "infrastructure",
         "applications": ["capstone", "capstone-research"], "permitted_uses": ["inference"], "slots": 1, "memory_gb": 27.4, "health": "healthy"} for n in (2, 3, 7)]


def test_plan_sets_slots_on_test_heads_and_funnels_research():
    plan = {r["resource_id"]: r for r in pt.plan_heads(BASE, ["head-192-0-2-2", "head-192-0-2-7"], "capstone-research", 3, True)}
    assert plan["head-192-0-2-2"]["slots"] == 3 and plan["head-192-0-2-7"]["slots"] == 3
    assert plan["head-192-0-2-3"]["slots"] == 1 and plan["head-192-0-2-3"]["applications"] == ["capstone"]
    assert plan["head-192-0-2-7"]["applications"] == ["capstone", "capstone-research"]
    assert BASE[1]["applications"] == ["capstone", "capstone-research"], "the baseline itself is not changed"


def test_plan_without_funnel_keeps_other_heads_and_takes_per_head_slots():
    plan = {r["resource_id"]: r for r in pt.plan_heads(BASE, ["head-192-0-2-2"], "capstone-research", {"head-192-0-2-2": 5}, False)}
    assert plan["head-192-0-2-2"]["slots"] == 5 and "capstone-research" in plan["head-192-0-2-3"]["applications"]


def test_policy_names_heads_only_with_the_gateway_fields():
    p = pt.resources_policy(BASE)
    assert p["clients"] == [] and p["contract"] == "shared.inference/1"
    assert set(p["resources"][0]) == set(pt.RESOURCE_KEYS), "health and other read-only fields are never sent"


def test_overlap():
    assert pt.max_overlap([(0, 10), (1, 5), (2, 3), (11, 12)]) == 3
    assert pt.max_overlap([(0, 1), (1, 2)]) == 1, "one ends as the next starts"
    assert pt.max_overlap([]) == 0


def rec(s, e, toks=50, dur_s=None):
    return {"started_at": s, "finished_at": e, "queue_ms": 100, "model": "qwen3.5:2b", "app": "capstone-research",
            "eval_count": toks, "eval_duration": int((dur_s or (e - s)) * 1e9), "prompt_eval_count": 300}


def test_metrics_from_gateway_records():
    m = pt.step_metrics([rec(0, 10), rec(0, 10), rec(10, 20)], [{"error": "x"}], 60)
    assert m["completed"] == 3 and m["failed"] == 1 and m["max_running_measured"] == 2
    assert m["output_tokens_per_s"] == 2.5 and m["tokens_per_s_per_request"] == 5.0 and m["requests_per_min"] == 3.0
    assert m["run_s_avg"] == 10.0 and m["queue_s_avg"] == 0.1 and m["models"] == ["qwen3.5:2b"]


def m(tps, done=10, failed=0, running=None):
    return {"output_tokens_per_s": tps, "completed": done, "failed": failed, "max_running_measured": running or 0}


def test_stops_at_diminishing_returns():
    st = {}
    curve = [40, 75, 105, 120, 124, 126, 127]
    out = [pt.decide(st, n, m(t), held=False, top=10, gain=0.10, sampled=n) for n, t in enumerate(curve, 1)]
    assert [o[0] for o in out[:5]] == [True, True, True, True, True]
    assert out[5][0] is False and "4 at once is the point (120 output tokens/s)" in out[5][1] and "highest 126" in out[5][1], out[5]
    assert pt.knee([(1, 40), (2, 75), (3, 105), (4, 120), (5, 124), (6, 126)], 0.10) == (4, 120)
    assert pt.knee([], 0.1) == (None, None)


def test_stops_on_failure_hold_top_and_thin_traffic():
    assert pt.decide({}, 3, m(10, failed=1), held=False, top=10, gain=0.1, sampled=3)[0] is False
    assert "held" in pt.decide({}, 3, m(10), held=True, top=10, gain=0.1, sampled=3)[1]
    assert "top step 2" in pt.decide({"points": [(1, 10)]}, 2, m(20), held=False, top=2, gain=0.1, sampled=2)[1]
    st = {}
    assert pt.decide(st, 4, m(10), held=False, top=10, gain=0.1, sampled=2) == (True, "not filled: at most 2 ran at once")
    go, text = pt.decide(st, 5, m(10), held=False, top=10, gain=0.1, sampled=2)
    assert go is False and "did not fill" in text
    assert pt.decide({}, 1, m(0, done=0), held=False, top=10, gain=0.1, sampled=1)[1] == "not filled: nothing finished in the step"


def test_reads_numbers_only_and_restores():
    src = (TOOLS / "pressure-test" / "pressure_test.py").read_text(encoding="utf-8")
    assert "mode=ro" in src and '"bootstrap"' in src
    assert "def restore" in src and "baseline.json" in src
    for text_field in ('resp.get("response")', "message"):
        assert text_field not in src.split("def finished")[1].split("def loaded_mb")[0], "no answer text is read"


# ---- SET-PARALLEL-PER-HEAD: requests at once per machine class, on all heads
sys.path.insert(0, str(TOOLS / "pressure-test"))
import set_parallel as sp  # noqa: E402


def test_machine_classes_from_the_lab_head_list():
    assert [sp.machine_class(r, g) for r, g in ((7.4, None), (15.4, None), (31.4, None), (63.9, "RTX 4060 Ti 16 GB"), (63.9, None))] == \
           ["8 GB", "16 GB", "32 GB", "GPU", "64 GB"]


def test_head_parallel_reports_are_read():
    on = "10:00:01 computer DESKTOP-REDACT8, head 192.0.2.7, user x\n10:00:40 RESULT success: Ollama 0.40.13 answers; it now runs up to 10 requests at once."
    off = "computer X, head 192.0.2.6, user x\nRESULT success: Ollama 0.40.13 answers; it now runs as before."
    same = "computer X, head 192.0.2.5, user x\nnow: up to 8 requests at once; memory 15.4 GB\nAlready so: nothing to change."
    failed = "computer X, head 192.0.2.2, user x\nRESULT Ollama did not answer within 3 minutes after the restart."
    assert [sp.parse_head_report(t) for t in (on, off, same, failed)] == [("192.0.2.7", 10), ("192.0.2.6", 1), ("192.0.2.5", 8), ("192.0.2.2", None)]
    # 1.6.10 wording: a setting, not a claim that Ollama runs that many (it runs qwen3.5 one at a time).
    on2 = "computer DESKTOP-REDACT13, head 192.0.2.8, user x\nRESULT success: Ollama 0.40.13 answers; OLLAMA_NUM_PARALLEL is 10.\nNOTE: this is a setting"
    off2 = "computer X, head 192.0.2.9, user x\nRESULT success: Ollama 0.40.13 answers; OLLAMA_NUM_PARALLEL is as before."
    assert [sp.parse_head_report(t) for t in (on2, off2)] == [("192.0.2.8", 10), ("192.0.2.9", 1)]


def test_latest_report_per_head_wins(tmp_path):
    (tmp_path / "DESKTOP-REDACT8").mkdir()
    (tmp_path / "DESKTOP-REDACT8" / "head-parallel-20261112-160000.txt").write_text("head 192.0.2.7\nRESULT success: x; it now runs up to 10 requests at once.", encoding="utf-8")
    (tmp_path / "DESKTOP-REDACT8" / "head-parallel-20261112-170000.txt").write_text("head 192.0.2.7\nRESULT success: x; it now runs up to 6 requests at once.", encoding="utf-8")
    assert sp.latest_caps(tmp_path) == {"192.0.2.7": 6}


def test_a_head_never_gets_more_than_its_ollama_allows():
    heads = [{"resource_id": "head-192-0-2-%d" % n, "address": "192.0.2.%d" % n, "cls": c} for n, c in ((6, "8 GB"), (7, "32 GB"), (8, "32 GB"), (2, "GPU"))]
    slots, notes = sp.plan_slots(heads, {"8 GB": 3, "32 GB": 6, "GPU": 8}, {"192.0.2.6": 4, "192.0.2.7": 10, "192.0.2.2": 10})
    assert slots == {"head-192-0-2-6": 3, "head-192-0-2-7": 6, "head-192-0-2-8": 1, "head-192-0-2-2": 8}
    assert len(notes) == 1 and "head-192-0-2-8" in notes[0] and "run HEAD-PARALLEL" in notes[0]


def test_knees_from_a_pressure_test(tmp_path):
    f = tmp_path / "steps.csv"
    rows = [("head-192-0-2-7", 1, 15), ("head-192-0-2-7", 2, 26), ("head-192-0-2-7", 3, 33), ("head-192-0-2-7", 4, 34), ("head-192-0-2-7", 5, 35)]
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = pt.csv.DictWriter(fh, fieldnames=pt.CSV_FIELDS)
        w.writeheader()
        for h, s, t in rows:
            w.writerow({"head": h, "step": s, "filled": True, "output_tokens_per_s": t})
    assert sp.knees(f) == {"head-192-0-2-7": (3, 33.0)}
