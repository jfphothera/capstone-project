"""The hub fix of 2026-11-12 (FIX-GATEWAY-QUALIFY): which embedding models it tests again, read-only, and that it
uses the Gateway's embedding test. The whole fix was also rehearsed with the real Gateway 0.4.0 code on a copy of
the hub's ledger and stand-in heads (27 back, a held head reported, a second run finds nothing)."""
import json
import sqlite3
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "engine" / "hub-tools"
sys.path.insert(0, str(TOOLS / "gateway-qualify-fix"))
import requalify_embedding as rq  # noqa: E402


def ledger(tmp_path):
    f = tmp_path / "ledger.db"
    db = sqlite3.connect(f)
    db.execute("CREATE TABLE capacity_resources(resource_id TEXT, endpoint TEXT, resource_type TEXT, health TEXT, qualified_models TEXT)")
    db.execute("CREATE TABLE capacity_qualification_evidence(evidence_id TEXT, resource_id TEXT, model TEXT, passed INT, evidence TEXT)")
    heads = {"head-192-0-2-3": ["gemma3:4b"], "head-192-0-2-7": ["gemma3:4b", "all-minilm:latest"], "head-192-0-2-9": ["gemma3:4b"]}
    for rid, q in heads.items():
        db.execute("INSERT INTO capacity_resources VALUES(?,?,?,?,?)", (rid, "http://" + rid, "shared_inference", "healthy", json.dumps(q)))
    rows = [("head-192-0-2-3", "all-minilm:latest", 1, "embedding", "http://head-192-0-2-3"),     # taken off: put back
            ("head-192-0-2-3", "nomic-embed-text:latest", 1, "embedding", "http://head-192-0-2-3"),
            ("head-192-0-2-7", "all-minilm:latest", 1, "embedding", "http://head-192-0-2-7"),     # still on: left alone
            ("head-192-0-2-9", "all-minilm:latest", 1, "embedding", "http://old-address"),        # passed at another address
            ("head-192-0-2-9", "granite-embedding:30m", 0, "embedding", "http://head-192-0-2-9"), # never passed
            ("head-192-0-2-9", "qwen3:4b", 1, "completion", "http://head-192-0-2-9")]             # a text model: not this fix
    for i, (rid, m, p, purpose, ep) in enumerate(rows):
        db.execute("INSERT INTO capacity_qualification_evidence VALUES(?,?,?,?,?)", ("E%d" % i, rid, m, p, json.dumps({"purpose": purpose, "endpoint": ep})))
    db.commit()
    db.close()
    return f


def test_only_embedding_models_that_passed_before_and_are_off_now(tmp_path):
    heads, pairs = rq.read_state(ledger(tmp_path))
    assert pairs == [("head-192-0-2-3", "all-minilm:latest"), ("head-192-0-2-3", "nomic-embed-text:latest")]
    assert heads["head-192-0-2-7"]["qualified"] == ["gemma3:4b", "all-minilm:latest"]


def test_reads_the_ledger_read_only(tmp_path):
    f = ledger(tmp_path)
    before = f.read_bytes()
    rq.read_state(f)
    assert f.read_bytes() == before


def test_do_not_use_is_respected(tmp_path):
    (tmp_path / "do-not-use.json").write_text(json.dumps({"entries": [{"resource_id": "head-192-0-2-3", "model": "nomic-embed-text:latest"}]}), encoding="utf-8")
    assert rq.do_not_use(tmp_path) == {("head-192-0-2-3", "nomic-embed-text:latest")}
    assert rq.do_not_use(tmp_path / "none") == set()


def test_dry_run_changes_nothing_and_reports(tmp_path):
    f = ledger(tmp_path)
    rel = tmp_path / "release" / "inference_gateway"
    rel.mkdir(parents=True)
    (rel / "service_launcher.py").write_text("raise SystemExit(9)", encoding="utf-8")
    code = rq.main(["--python", sys.executable, "--release", str(tmp_path / "release"), "--ledger", str(f), "--evidence", str(tmp_path),
                    "--models-console", str(tmp_path), "--operator", "t", "--report", str(tmp_path / "r.json"), "--dry-run"])
    assert code == 0
    r = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert [x["model"] for x in r["results"]] == ["all-minilm:latest", "nomic-embed-text:latest"]
    assert "would test all-minilm:latest on head-192-0-2-3" in (tmp_path / "r.txt").read_text(encoding="utf-8")


def test_uses_the_gateways_embedding_test():
    src = (TOOLS / "gateway-qualify-fix" / "requalify_embedding.py").read_text(encoding="utf-8")
    assert '"--purpose", "embedding"' in src
    assert 'mode=ro' in src
