"""Tests for the capstone retrieval and validation behaviour.

    python -m pytest tests -v

These run with --no-llm semantics (prefer_llm=False) so that they are fast and
deterministic. The LLM path is exercised separately by run_demo.py.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import agent as agent_mod, config, erp as erp_mod, qms  # noqa: E402
from capstone_rag import validation  # noqa: E402


@pytest.fixture(scope="module")
def ag():
    return agent_mod.Agent(prefer_llm=False)


@pytest.fixture(scope="module")
def scenarios():
    with open(os.path.join(config.ERP_DIR, "scenarios.json"),
              encoding="utf-8") as fh:
        return {s["scenario_id"]: s for s in json.load(fh)}


def run(ag, scenarios, sid):
    s = scenarios[sid]
    return ag.handle(s["customer_message"], account_id=s["account_id"],
                     scenario_id=sid)


# --- corpus ----------------------------------------------------------------

def test_corpus_loads_and_chunks():
    chunks = qms.load_chunks()
    assert len(chunks) >= 30
    docs = {c.document_id for c in chunks}
    # The corpus grew when coverage was extended to the models that actually
    # appear in the imported ticket pool. The originals must still be there.
    assert len(docs) >= 6, f"expected at least 6 QMS documents, got {docs}"
    assert {f"QMS-DOC-010{n}" for n in range(1, 7)} <= docs
    for c in chunks:
        assert c.procedure_id and c.model and c.applicable_revisions
        assert c.approval_status in ("Approved", "Superseded")
        assert c.audiences


def test_nested_qms_headings_keep_parent_context():
    chunks = [c for c in qms.load_chunks()
              if c.document_id == "QMS-DOC-0112"]
    step = next(c for c in chunks if c.section.startswith("Step 1"))
    assert step.heading_level == 3
    assert step.section_path.startswith("Customer checks > Step 1")


def test_corpus_has_the_required_edge_cases():
    chunks = qms.load_chunks()
    # two procedures, same symptom, different revisions of the same model
    power = {c.procedure_id for c in chunks
             if c.model == "P4800" and "Power On" in c.title}
    assert {"PROC-P4800-PWR-A", "PROC-P4800-PWR-C"} <= power
    # at least one superseded document
    assert any(c.approval_status == "Superseded" for c in chunks)


def test_embeddings_are_real_vectors(ag):
    assert ag.index.matrix.shape[0] == len(ag.index.chunks)
    assert ag.index.matrix.shape[1] == 384
    norms = (ag.index.matrix ** 2).sum(axis=1) ** 0.5
    assert all(abs(n - 1.0) < 1e-3 for n in norms), "vectors not normalised"


def test_search_is_semantic_not_keyword(ag):
    """A query sharing no distinctive words with the document still ranks it."""
    hits = ag.index.search(
        "the machine is dead, nothing happens when I switch it on", k=3
    )
    top_docs = {h.chunk.document_id for h in hits}
    assert top_docs & {"QMS-DOC-0101", "QMS-DOC-0102", "QMS-DOC-0103"}, (
        f"expected a power procedure in the top 3, got {top_docs}"
    )


# --- ERP -------------------------------------------------------------------

def test_erp_resolves_serial_to_model_and_revision():
    e = erp_mod.ERP()
    dev = e.device_by_serial("98148469")
    assert dev.model == "P4800" and dev.revision == "A"
    dev_b = e.device_by_serial("68177-DV5")
    assert dev_b.model == "P4800" and dev_b.revision == "B"


def test_erp_will_not_guess_between_two_devices():
    e = erp_mod.ERP()
    res = erp_mod.identify_device(e, "my unit is broken", account_id="ACC-00022")
    assert res.confirmed is False
    assert len(res.candidates) == 2


# --- the four demonstrations ----------------------------------------------

def test_1_successful_grounded_retrieval(ag, scenarios):
    r = run(ag, scenarios, "SCN-01")
    assert r.identity.confirmed
    assert r.identity.device.model == "P4800"
    assert r.identity.device.revision == "A"
    assert r.outcome == "grounded"
    assert r.verdict.result == "PASS"
    assert r.selected_procedure == "PROC-P4800-PWR-A"
    assert r.accepted, "expected at least one accepted chunk"
    assert "PROC-P4800-PWR-A" in r.response


def test_2_wrong_revision_is_rejected(ag, scenarios):
    r = run(ag, scenarios, "SCN-02")
    assert r.identity.device.revision == "B"
    assert r.outcome == "abstained"
    assert r.verdict.result == "FAIL"
    assert r.selected_procedure is None
    assert not r.accepted

    # The Rev A and Rev C power procedures must have been retrieved and then
    # rejected, not filtered out before the search.
    rejected_procs = {v.procedure_id for v in r.rejected}
    assert "PROC-P4800-PWR-A" in rejected_procs
    assert "PROC-P4800-PWR-C" in rejected_procs

    reasons = " ".join(f for v in r.rejected for f in v.failures)
    assert "revision" in reasons

    # And none of their content leaked into the reply.
    for marker in ("fuse", "filter tray", "interlock", "five minutes"):
        assert marker not in r.response.lower(), (
            f"wrong-revision content {marker!r} leaked into the response"
        )


def test_2b_identical_query_passes_for_the_right_revision(ag, scenarios):
    """Same words, different serial -> different outcome. Proves it is the
    revision that decides, not the phrasing."""
    a = run(ag, scenarios, "SCN-01")
    b = run(ag, scenarios, "SCN-02")
    assert a.retrieval_query == b.retrieval_query
    assert a.verdict.result == "PASS"
    assert b.verdict.result == "FAIL"
    # Top semantic hit is the same chunk at the same score in both runs.
    top_a = sorted(a.accepted + a.rejected, key=lambda v: v.rank)[0]
    top_b = sorted(b.accepted + b.rejected, key=lambda v: v.rank)[0]
    assert top_a.chunk_id == top_b.chunk_id
    assert abs(top_a.similarity - top_b.similarity) < 1e-6


def test_3_no_applicable_source_abstains(ag, scenarios):
    r = run(ag, scenarios, "SCN-03")
    assert r.identity.device.model == "Home-NB"
    assert r.outcome == "abstained"
    assert r.verdict.result == "FAIL"
    assert r.selected_procedure is None
    low = r.response.lower()
    assert "engineer" in low or "human" in low
    # Must not invent steps.
    assert "step 1" not in low


def test_4_retrieval_changes_the_response(ag, scenarios):
    s = scenarios["SCN-01"]
    gen_a, ident = ag.answer_without_retrieval(s["customer_message"],
                                               account_id=s["account_id"])
    r_b = run(ag, scenarios, "SCN-01")

    assert ident.device.serial_number == "98148469"
    # No-retrieval reply cites nothing and gives no procedure steps.
    assert "PROC-P4800-PWR-A" not in gen_a.text
    # Retrieval-enabled reply cites the source and carries its content.
    assert "PROC-P4800-PWR-A" in r_b.response
    assert r_b.response != gen_a.text


@pytest.mark.parametrize("message,account_id,expected", [
    ("The lamp stays dark although the unit powers on. Serial: 52385502",
     "ACC-00022", None),  # Rev C is inferred, not QMS-authoritative.
    ("The lamp does not light but the power indicator is on. Serial: 38180-BM2",
     "ACC-00026", "PROC-SERIESM-LAMP"),
    ("The lamp is dark while the supply light stays on. Serial: 21308910",
     "ACC-00026", "PROC-P3410-LAMP"),
])
def test_expanded_customer_service_lamp_coverage(
        ag, message, account_id, expected):
    result = ag.handle(message, account_id=account_id)
    if expected is None:
        assert result.outcome in ("answered", "abstained", "escalated")
        assert result.selected_procedure is None
    else:
        assert result.outcome == "grounded"
        assert result.selected_procedure == expected
        assert expected in result.response


# --- extra scenarios -------------------------------------------------------

def test_missing_serial_asks_instead_of_guessing(ag, scenarios):
    r = run(ag, scenarios, "SCN-04")
    assert r.identity.confirmed is False
    assert r.outcome == "abstained"
    assert r.retrieval_query is None, "must not retrieve without a confirmed device"
    assert "serial number" in r.response.lower()


def test_safety_complaint_escalates_before_retrieval(ag, scenarios):
    r = run(ag, scenarios, "SCN-05")
    assert r.outcome == "escalated"
    assert r.retrieval_query is None, "safety gate must fire before retrieval"
    assert r.selected_procedure is None
    assert "safety" in r.response.lower()


# --- validator unit behaviour ---------------------------------------------

def test_superseded_document_is_rejected_even_when_model_and_rev_match(ag):
    device = erp_mod.ERP().device_by_serial("98148469")  # P4800 Rev A
    hit = next(h for h in ag.index.search("unit will not power on", k=20)
               if h.chunk.document_id == "QMS-DOC-0103")
    v = validation.validate_chunk(hit, device, ag.compatibility, 0.0)
    assert v.accepted is False
    assert any("Superseded" in f for f in v.failures)


def test_service_only_document_is_rejected_for_customer_service(ag):
    device = erp_mod.ERP().device_by_serial("38180-BM2")
    chunk = next(c for c in ag.index.chunks
                 if c.procedure_id == "PROC-SERIESM-CONTROLLER-SVC")
    hit = SimpleNamespace(chunk=chunk, score=1.0, rank=1)
    verdict = validation.validate_chunk(
        hit, device, ag.compatibility, min_similarity=0.0)
    assert verdict.accepted is False
    assert any("audience" in failure for failure in verdict.failures)


def test_explicit_lamp_symptom_cannot_select_power_procedure(ag):
    result = ag.handle(
        "My name is Mary Cohen. Serial 52385502. The unit powers on but "
        "one panel is noticeably dimmer than the others.",
        account_id="ACC-00022")
    assert result.selected_procedure is None
    assert result.outcome in ("answered", "abstained", "escalated")


def test_uncovered_screen_code_abstains_instead_of_using_lamp_document(ag):
    result = ag.handle(
        "My name is Robert Petrov. Serial 77592661. The screen shows a code "
        "I don't recognize.", account_id="ACC-00046")
    assert result.selected_procedure is None
    assert result.outcome in ("abstained", "escalated")


def test_uncited_response_fails_final_validation():
    device = erp_mod.ERP().device_by_serial("98148469")
    identity = erp_mod.IdentityResult(confirmed=True, device=device,
                                      customer=None, reason="test")
    accepted = [{"model": "P4800", "applicable_revisions": ["A"],
                 "approval_status": "Approved"}]
    v = validation.validate_interaction(
        identity, accepted, "here are some steps with no citation",
        "PROC-P4800-PWR-A",
    )
    assert v.result == "FAIL"
    assert "response_cites_source" in v.reason


def test_ticket_interactions_are_recorded(ag, scenarios):
    run(ag, scenarios, "SCN-01")
    assert os.path.exists(config.TICKET_LOG_PATH)
    with open(config.TICKET_LOG_PATH, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert rows
    last = rows[-1]
    assert last["ticket_id"].startswith("TKT-")
    assert "validation" in last and "outcome" in last
