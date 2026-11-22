"""Phase 1 identity, catalog, and QMS authorization acceptance tests."""
from types import SimpleNamespace
import json
import sys
from pathlib import Path
import sqlite3

import pytest

from capstone_rag import identity, model_catalog, qms, qms_policy, support_workflow

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_defects import flow, shared_agent, source_db, st  # noqa: E402,F401


@pytest.mark.parametrize("text", [
    "serial number: 1946-11-18",
    "my serial number is 1946-11-18",
])
def test_labelled_dob_never_becomes_a_serial(text):
    claim = identity.Claim.from_text(text)
    assert claim.serial is None
    assert claim.dob == "1946-11-18"


def test_serial_standard_records_status_candidates_and_offsets():
    known = ("98148469", "68177-DV5")
    numeric = identity.Claim.from_text("Serial: 98148469", known_serials=known)
    alpha = identity.Claim.from_text("Serial: 68177 dv5", known_serials=known)
    unknown = identity.Claim.from_text("Serial: 99999999", known_serials=known)
    malformed = identity.Claim.from_text("Serial: ZZ00041", known_serials=known)
    assert (numeric.serial, numeric.serial_status) == ("98148469", "matched")
    assert (alpha.serial, alpha.serial_status) == ("68177-DV5", "matched")
    assert numeric.serial_offsets and alpha.serial_candidates == ("68177 dv5",)
    assert unknown.serial is None and unknown.serial_issue == "unknown"
    assert malformed.serial is None and malformed.serial_issue == "malformed"


def test_conflicting_serials_are_not_resolved_by_position():
    claim = identity.Claim.from_text(
        "The serials shown are 98148469 and 68177-DV5.",
        known_serials=("98148469", "68177-DV5"))
    assert claim.serial is None
    assert claim.serial_issue == "conflicting"
    assert set(claim.serial_candidates) == {"98148469", "68177-DV5"}


@pytest.mark.parametrize("text", ["P100", "p-100", "100XL", "Aquex"])
def test_unsupported_product_references_never_become_supported_models(text):
    ref = model_catalog.resolve_reference(text)
    assert ref and ref.status == "unsupported" and ref.canonical is None


def test_catalog_contains_exactly_the_six_controlled_models():
    assert set(model_catalog.SUPPORTED_MODELS) == {
        "P2400", "P3410", "P4800", "Series-M", "3-Series", "Home-NB"}


def test_direct_qms_tool_blocks_inferred_revision_but_keeps_family_guidance(shared_agent):
    device = SimpleNamespace(serial_number="32601455", model="P4800", revision="A",
                             model_revision="P4800|A")
    result = shared_agent.toolbox.search_qms("My P4800 will not power on", device)
    assert not result.data["accepted_chunks"]
    assert any("provenance" in failure or "inferred" in failure
               for verdict in result.data["verdicts"]
               for failure in verdict.failures)
    assert any(c.procedure_id == "PROC-MANUAL-P4800"
               for c in result.data["general_chunks"])


def test_verified_p4800_revisions_retrieve_only_compatible_power_procedure(shared_agent):
    cases = {
        "98148469": "PROC-P4800-PWR-A",
        "95806143": "PROC-P4800-PWR-C",
    }
    for serial, expected in cases.items():
        revision = "A" if serial == "98148469" else "C"
        device = SimpleNamespace(serial_number=serial, model="P4800", revision=revision,
                                 model_revision=f"P4800|{revision}")
        result = shared_agent.toolbox.search_qms("My P4800 will not power on", device)
        assert result.data["selected_procedure"] == expected
        assert result.data["accepted_chunks"]
        assert all(chunk.procedure_id == expected
                   for chunk in result.data["accepted_chunks"])


def test_rev_b_gets_family_guidance_without_wrong_revision_power_steps(shared_agent):
    device = SimpleNamespace(serial_number="68177-DV5", model="P4800", revision="B",
                             model_revision="P4800|B")
    result = shared_agent.toolbox.search_qms("My P4800 will not power on", device)
    assert not result.data["accepted_chunks"]
    assert any(c.procedure_id == "PROC-MANUAL-P4800"
               for c in result.data["general_chunks"])


@pytest.mark.parametrize("serial,revision,exact", [
    ("32601455", "A", None),       # inferred A
    ("98148469", "A", "PROC-P4800-PWR-A"),
    ("68177-DV5", "B", None),     # no approved Rev B power procedure
    ("95806143", "C", "PROC-P4800-PWR-C"),
])
def test_p4800_power_customer_response_uses_only_authorized_evidence(
        shared_agent, serial, revision, exact):
    device = SimpleNamespace(serial_number=serial, model="P4800", revision=revision,
                             model_revision=f"P4800|{revision}")
    manual = support_workflow.customer_manual_search(
        shared_agent.toolbox, "My P4800 will not power on", "P4800", revision,
        device=device, revision_context=identity.revision_context(device))
    assert "PROC-MANUAL-P4800" in manual["procedures"]
    exact_power = {p for p in manual["procedures"] if p.startswith("PROC-P4800-PWR-")}
    assert exact_power == ({exact} if exact else set())
    generated, report = support_workflow.write_public_reply(
        shared_agent, "My P4800 will not power on",
        {"manual": manual, "model": "P4800",
         "device": {"revision": revision}})
    assert generated is not None and report.grounded


def test_general_and_exact_evidence_can_coexist_for_verified_rev_a(shared_agent):
    device = SimpleNamespace(serial_number="98148469", model="P4800", revision="A",
                             model_revision="P4800|A")
    result = support_workflow.customer_manual_search(
        shared_agent.toolbox, "My P4800 will not power on", "P4800", "A",
        device=device, revision_context=identity.revision_context(device))
    assert {"PROC-MANUAL-P4800", "PROC-P4800-PWR-A"}.issubset(
        set(result["procedures"]))
    assert result["contract"]["mandatory_context_chunk_ids"]


def test_mandatory_context_is_retained_after_prior_turn_delivery(shared_agent):
    device = SimpleNamespace(serial_number="98148469", model="P4800", revision="A",
                             model_revision="P4800|A")
    first = support_workflow.customer_manual_search(
        shared_agent.toolbox, "My P4800 will not power on", "P4800", "A",
        device=device, revision_context=identity.revision_context(device))
    delivered = tuple(c.chunk_id for c in first["chunks"])
    followup = support_workflow.customer_manual_search(
        shared_agent.toolbox, "The outlet works but the unit is still dead",
        "P4800", "A", delivered=delivered, device=device,
        revision_context=identity.revision_context(device))
    mandatory = set(followup["contract"]["mandatory_context_chunk_ids"])
    returned = {c.chunk_id for c in followup["chunks"]}
    assert mandatory and mandatory <= returned


def test_orchestrated_operations_reuse_qms_specialist_evidence(flow, st):
    cid = flow.intake(
        "P4800 no power",
        "My name is Ivan Nowak. Serial 98148469. My P4800 will not power on.")
    result = flow.pre_investigate(cid)
    assert result["paused_for_review"]
    payload = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'",
        (cid,))["payload"])
    authorized = set(payload["sources"])
    used = {row["chunk_id"] for row in st.query(
        "SELECT chunk_id FROM retrieved_sources WHERE case_id=? AND status='USED'",
        (cid,))}
    assert authorized and used and used <= authorized


def test_missing_accessory_document_is_model_independent():
    document = next(c for c in qms.load_chunks()
                    if c.document_id == "QMS-DOC-0111")
    assert document.scope == "model_independent"
    for model in ("Home-NB", "P4800", "P2400"):
        decision = qms_policy.authorize(
            document, context={'model': model, 'revision': None,
                               'compatibility': qms.load_compatibility()})
        assert decision.allowed


def test_safety_hold_precedes_all_ordinary_guidance(shared_agent):
    result = support_workflow.customer_manual_search(
        shared_agent.toolbox, "My P4800 has a burning smell and will not power on",
        "P4800", "A", safety_hold=True)
    assert not result["chunks"]
    assert any("SAFETY_HOLD" in c["authorization"]["denial_codes"]
               for c in result["evidence"]["candidates"])


def test_challenge_dataset_is_separate_reproducible_and_preserves_pool():
    from scripts.build_phase1_challenge_dataset import build
    root = Path(__file__).resolve().parents[1]
    connection = sqlite3.connect(f"file:{(root / 'data' / 'operations.db').as_posix()}?mode=ro",
                                 uri=True)
    connection.row_factory = sqlite3.Row
    first = build(connection)
    second = build(connection)
    pool_count = connection.execute("SELECT count(*) FROM ticket_pool").fetchone()[0]
    connection.close()
    assert first == second and len(first) == 22
    assert pool_count == 1000
    assert (root / 'data' / 'phase1' / 'challenge_scenarios.jsonl').exists()

