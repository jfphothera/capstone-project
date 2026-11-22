"""WATCH-LAB on the Capstone laptop (engine/watch/watch_capstone.py), against a synthetic Capstone data folder with
the tables and columns Capstone rc.24 writes (checked against a real Testing Lab database, 2026-11-17)."""
import hashlib
import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "engine" / "watch" / "watch_capstone.py"
spec = importlib.util.spec_from_file_location("watch_capstone", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

SCHEMA = """
CREATE TABLE activity_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, investigation_id TEXT, conversation_id TEXT,
  case_id TEXT, turn_id TEXT, category TEXT, status TEXT, summary TEXT, duration_ms REAL, evidence TEXT);
CREATE TABLE retrieved_sources(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, run_id TEXT, at TEXT, chunk_id TEXT,
  document_id TEXT, procedure_id TEXT, similarity REAL, rank INTEGER, selected_by TEXT, status TEXT, reason TEXT);
CREATE TABLE inference_calls(call_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, case_id TEXT, conversation_id TEXT,
  investigation_id TEXT, role TEXT NOT NULL, endpoint_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
  payload TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, run_id TEXT, case_id TEXT, event_type TEXT,
  actor TEXT, summary TEXT, detail TEXT);
CREATE TABLE messages(message_id TEXT PRIMARY KEY, case_id TEXT, run_id TEXT, created_at TEXT, direction TEXT,
  channel TEXT, author TEXT, subject TEXT, body TEXT, status TEXT, approved_by TEXT, approved_at TEXT, metadata TEXT,
  parent_message_id TEXT, idempotency_key TEXT);
"""


def iso(offset=0.0):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


def call(db, cid, role, content, *, offset=0.0, done="stop", endpoint="head-192-0-2-7", model="qwen3.5:4b", status="completed", extra=None):
    payload = {"route_role": role, "endpoint_id": endpoint, "model": model, "duration_ms": 12400, "status": status,
               "timings": {"eval_count": 88, "done_reason": done}, "requested_num_predict": 160,
               "response": {"message": {"content": content}}}
    payload.update(extra or {})
    db.execute("INSERT INTO inference_calls VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (cid, "SNAP", "CASE-1", None, "INV-1", role, endpoint, model, status, json.dumps(payload), time.time() + offset))


def make_trial(root, run, *, lease_in=60.0):
    rd = root / "data" / "experiments" / "EXP-1" / run
    rd.mkdir(parents=True)
    db = sqlite3.connect(rd / "operations.db")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO activity_events(at,case_id,category,status,summary) VALUES(?,?,?,?,?)", (iso(-5), "CASE-1", "message", "completed", "Customer message received"))
    db.execute("INSERT INTO activity_events(at,case_id,category,status,summary) VALUES(?,?,?,?,?)", (iso(-4), "CASE-1", "inference", "completed", "writer: done"))
    db.executemany("INSERT INTO retrieved_sources(case_id,at,chunk_id,similarity,rank,selected_by,status,reason) VALUES(?,?,?,?,?,?,?,?)",
                   [("CASE-1", iso(-3), "QMS-DOC-0101#S2-logistics", 0.61, 1, "similarity", "ACCEPT", None),
                    ("CASE-1", iso(-3), "QMS-DOC-0115#S2-safety", 0.18, 9, "similarity", "REJECT", "similarity 0.18 below threshold 0.20")])
    call(db, "CALL-1", "lab_reviewer", json.dumps({"decision": "request_revision", "rationale": "Step 3 is not in the approved passage."}), offset=-2)
    call(db, "CALL-2", "communication_review", '{"issues": ["the draft says', offset=-1, done="length")   # cut off mid-answer
    call(db, "CALL-3", "retrieval_embedding", None, extra={"response": {"embeddings": [[0.1, 0.2]]}})
    db.execute("INSERT INTO audit_events(at,case_id,event_type,actor,summary) VALUES(?,?,?,?,?)",
               (iso(0), "CASE-1", "customer_message_rejected", "lab-reviewer-agent:qwen3:4b", "DRAFT-2 rejected: regeneration requested"))
    db.execute("INSERT INTO messages(message_id,case_id,created_at,direction,author,subject,body,status) VALUES(?,?,?,?,?,?,?,?)",
               ("DRAFT-2", "CASE-1", iso(0), "outbound", "ai", "Re: goggles", "Hello Ivan,\nPlease pack the goggles in the original carton.", "rejected"))
    db.commit()
    db.close()
    lease = sqlite3.connect(rd / "worker-lease.db")
    lease.execute("CREATE TABLE lease (id INTEGER PRIMARY KEY, owner TEXT, expires REAL)")
    lease.execute("INSERT INTO lease VALUES(1,'w',?)", (time.time() + lease_in,))
    lease.commit()
    lease.close()
    (rd / "manifest.json").write_text(json.dumps({"run": {"method": "A", "main_model": "qwen3.5:4b", "scenario": "goggles-logistics"}}), encoding="utf-8")
    return rd


def digest(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}


def test_finds_running_trials_and_the_live_database_but_not_finished_ones(tmp_path):
    make_trial(tmp_path, "RUN-aaaa1111")
    make_trial(tmp_path, "RUN-bbbb2222", lease_in=-600)                     # its worker stopped ten minutes ago
    live = sqlite3.connect(tmp_path / "data" / "operations.db"); live.executescript(SCHEMA); live.close()
    found = watch.discover(tmp_path / "data", "", set(), time.time())
    labels = sorted(s.label for s in found)
    assert labels[0] == "LIVE" and len(labels) == 2
    assert labels[1].startswith("RUN-aaaa1111") and "A, qwen3.5:4b, goggles-logistics" in labels[1]
    only = watch.discover(tmp_path / "data", "aaaa", set(), time.time())      # --run follows one trial
    assert [s.label.split()[0] for s in only] == ["RUN-aaaa1111"]


def test_each_step_and_its_written_output_is_shown_and_nothing_is_written(tmp_path):
    rd = make_trial(tmp_path, "RUN-cccc3333")
    before = digest(rd)
    text = "\n".join(t for _, t in watch.Source(rd / "operations.db", "RUN-cccc", True).poll(color=False))
    assert "message: Customer message received" in text and "writer: done" not in text          # inference lines: shown as model blocks
    assert "RAG: 2 candidate passage(s), 1 kept" in text and "kept   QMS-DOC-0101#S2-logistics" in text
    assert "best rejected QMS-DOC-0115#S2-safety (0.18): similarity 0.18 below threshold 0.20" in text
    assert "MODEL Reviewer (simulated human): qwen3.5:4b on head 7, 12.4 s, 88 tokens" in text
    assert "decision: request_revision" in text and "rationale: Step 3 is not in the approved passage." in text
    assert "CUT OFF: the answer reached its output limit (160 tokens)" in text
    assert "an embedding: numbers for the search index" in text and "[0.1, 0.2]" not in text and "0.2]" not in text
    assert "RULE customer_message_rejected (lab-reviewer-agent:qwen3:4b)" in text
    assert "DRAFT DRAFT-2 by ai [rejected]" in text and "original carton" in text
    assert digest(rd) == before                                              # read-only: not a byte changed


def test_only_new_rows_after_the_first_pass_in_time_order(tmp_path):
    rd = make_trial(tmp_path, "RUN-dddd4444")
    src = watch.Source(rd / "operations.db", "RUN-dddd", False)              # already running: from now
    assert src.poll(color=False) == []
    db = sqlite3.connect(rd / "operations.db")
    call(db, "CALL-9", "resolution_planning", json.dumps({"preferred_action": "clarify", "plans": [{"action": "clarify", "rationale": "serial unknown"}]}), offset=1)
    db.execute("INSERT INTO activity_events(at,case_id,category,status,summary) VALUES(?,?,?,?,?)", (iso(0.5), "CASE-1", "tool", "completed", "erp.lookup answered"))
    db.commit(); db.close()
    lines = [t for _, t in src.poll(color=False)]
    assert len(lines) == 2 and "tool: erp.lookup answered" in lines[0] and "MODEL Planner" in lines[1]   # ISO and epoch times ordered together
    assert "plans 1: " in lines[1] and "serial unknown" in lines[1]


def test_replay_runs_once_from_the_command_line(tmp_path, capsys):
    rd = make_trial(tmp_path, "RUN-eeee5555")
    assert watch.main(["--replay", str(rd / "operations.db"), "--once", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "WATCH-LAB" in out and "thinking off" in out and "MODEL Communication review" in out


def test_says_so_when_this_is_not_the_capstone_laptop(tmp_path, capsys):
    assert watch.main(["--root", str(tmp_path / "nowhere"), "--once", "--no-color"]) == 2
    assert "Is this the Capstone laptop?" in capsys.readouterr().out
