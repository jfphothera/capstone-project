"""Tests for response-grounding verification.

The fixtures use the real captured Qwen3 output from docs/evidence/run_llm.txt
as the known-good case, and mutate it to produce the failure cases. That keeps
the thresholds honest: they have to accept output the system actually produced,
not a hand-written ideal.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import erp as erp_mod, grounding, index as index_mod  # noqa: E402

# Verbatim from docs/evidence/run_llm.txt, TEST 1.
REAL_LLM_REPLY = """Hello Ivan Nowak,

Thank you for reaching out about your P4800 revision A unit. To help diagnose the
issue, we need to confirm the symptoms match a total power failure. Please verify
that: the front status LED is completely dark (not amber or blinking), there's no fan
noise within ten seconds after switching on, and the display backlight doesn't
illuminate briefly. If any of these aren't true, this isn't a total power failure and
a different procedure applies.

Next, we'll check the mains supply. Could you unplug the cord from both the wall
outlet and the rear inlet, then firmly reseat both ends? After that, confirm the
outlet is live using a known-working lamp or small appliance-avoid metallic probes.
If the outlet is dead, the issue is site electrical supply.

This follows PROC-P4800-PWR-A. Let me know the results so we can proceed.

Kind regards,
Customer Support"""

APPEND = "\n\nPlease also {} before powering the unit back on."


@pytest.fixture(scope="module")
def ctx():
    idx = index_mod.VectorIndex.load_or_build()
    by_id = {c.chunk_id: c for c in idx.chunks}
    accepted = [by_id["QMS-DOC-0101#S2-symptom-confirmation"],
                by_id["QMS-DOC-0101#S3-step-1-mains-supply-and-cord-check"]]
    device = erp_mod.ERP().device_by_serial("98148469")
    return accepted, device


def check(ctx, text):
    accepted, device = ctx
    return grounding.check_grounding(text, accepted, device, "PROC-P4800-PWR-A")


# --- the known-good case ---------------------------------------------------

def test_real_llm_output_is_accepted(ctx):
    """Thresholds must not reject output the system genuinely produced."""
    r = check(ctx, REAL_LLM_REPLY)
    assert r.grounded, f"real output rejected: {r.reason}"
    assert r.checked >= 5, "too few sentences actually checked"
    assert not r.identifier_issues


def test_boilerplate_is_skipped_not_checked(ctx):
    r = check(ctx, REAL_LLM_REPLY)
    checked = " ".join(c.sentence.lower() for c in r.sentence_checks)
    assert "hello ivan" not in checked
    assert "kind regards" not in checked
    assert r.skipped > 0


# --- fabricated instructions ----------------------------------------------

@pytest.mark.parametrize("label,addition", [
    ("fuse replacement",
     "replace the 3.15A fuse in the rear carrier and check that the cooling fan "
     "spins freely"),
    ("invented measurement",
     "measure the mains voltage at the rear inlet and confirm it reads between "
     "220 and 240 volts"),
    ("dangerous advice",
     "open the rear enclosure panel and bridge the fuse holder with a short "
     "length of wire"),
    ("plausible absent step",
     "hold the power button down for thirty seconds to discharge residual power"),
])
def test_fabricated_instruction_is_detected(ctx, label, addition):
    r = check(ctx, REAL_LLM_REPLY + APPEND.format(addition))
    assert not r.grounded, f"fabrication not detected: {label}"
    assert any(addition[:30] in c.sentence for c in r.unsupported), (
        f"the fabricated sentence itself was not the one flagged ({label})"
    )


def test_wrong_revision_content_is_unsupported(ctx):
    """Rev C steps are real text from the corpus, but not from the accepted
    chunks, so they must still be rejected."""
    r = check(ctx, REAL_LLM_REPLY + APPEND.format(
        "leave the unit unplugged for a minimum of five minutes so the "
        "self-resetting supply protector can clear, then push the filter tray "
        "fully home until it latches"))
    assert not r.grounded
    assert any("five minutes" in c.sentence for c in r.unsupported)


# --- identifier transcription ---------------------------------------------

def test_mangled_serial_is_detected(ctx):
    """The defect actually observed from Qwen3 4B: 98148469 -> 981484:69."""
    r = check(ctx, REAL_LLM_REPLY.replace(
        "revision A unit", "revision A unit (serial 981484:69)"))
    assert not r.grounded
    assert any("981484" in i for i in r.identifier_issues)


def test_wrong_procedure_id_is_detected(ctx):
    r = check(ctx, REAL_LLM_REPLY.replace("PROC-P4800-PWR-A",
                                          "PROC-P4800-PWR-C"))
    assert not r.grounded
    assert any("PROC-P4800-PWR-C" in i for i in r.identifier_issues)


def test_unknown_document_id_is_detected(ctx):
    r = check(ctx, REAL_LLM_REPLY + "\n\nSee also QMS-DOC-0199 for details.")
    assert not r.grounded
    assert any("QMS-DOC-0199" in i for i in r.identifier_issues)


def test_correct_serial_is_accepted(ctx):
    r = check(ctx, REAL_LLM_REPLY.replace(
        "revision A unit", "revision A unit (serial 98148469)"))
    assert not r.identifier_issues
    assert r.grounded


# --- report shape ----------------------------------------------------------

def test_report_produces_actionable_feedback(ctx):
    r = check(ctx, REAL_LLM_REPLY + APPEND.format(
        "replace the 3.15A fuse in the rear carrier"))
    fb = r.feedback()
    assert "3.15A fuse" in fb
    assert fb.startswith("-")


def test_empty_source_cannot_support_anything(ctx):
    _, device = ctx
    r = grounding.check_grounding(
        "Please unplug the mains cord and reseat both ends.", [], device, None)
    assert not r.grounded


def test_sentence_splitter_unwraps_hard_wrapped_text():
    """A sentence wrapped across lines must stay one sentence."""
    wrapped = ("Please unplug the mains cord from both the wall outlet and\n"
               "the rear inlet, then reseat both ends firmly. Then retry.")
    sents = grounding.split_sentences(wrapped)
    assert len(sents) == 2
    assert sents[0].endswith("reseat both ends firmly.")


# --- mixed authorised + invented in ONE sentence --------------------------
# The risk the whole-sentence check cannot see: most of the sentence is
# faithful, so the sentence scores well, and the invented half rides along.

@pytest.mark.parametrize("label,sentence", [
    ("fuse rider",
     "Please unplug the mains cord from both the wall outlet and the rear "
     "inlet, reseat both ends firmly, and replace the 3.15A fuse in the rear "
     "carrier."),
    ("voltage rider",
     "Confirm the wall outlet is live using a known-working lamp, and measure "
     "the mains voltage at the inlet to check it reads 240 volts."),
    ("bypass rider",
     "Reseat both ends of the mains cord firmly; then bridge the fuse holder "
     "with a short length of wire to test continuity."),
    ("button-hold rider",
     "Check that the front status LED is completely dark, and hold the power "
     "button down for thirty seconds to discharge residual power."),
])
def test_invented_clause_inside_a_faithful_sentence_is_caught(ctx, label,
                                                              sentence):
    reply = (f"Hello Ivan Nowak,\n\n{sentence}\n\n"
             f"This follows PROC-P4800-PWR-A.\n\nKind regards,\n"
             f"Customer Support")
    r = check(ctx, reply)
    assert not r.grounded, f"mixed-clause fabrication not caught: {label}"
    bad = [c for c in r.unsupported]
    assert bad, "no sentence was flagged"
    assert any(c.level == "clause" for c in bad), (
        f"flagged at sentence level, not clause level ({label}): "
        f"{[c.level for c in bad]}"
    )


def test_clause_feedback_names_the_offending_fragment(ctx):
    reply = ("Hello Ivan Nowak,\n\nPlease unplug the mains cord from both the "
             "wall outlet and the rear inlet, reseat both ends firmly, and "
             "replace the 3.15A fuse in the rear carrier.\n\n"
             "This follows PROC-P4800-PWR-A.\n\nKind regards,\nCustomer Support")
    r = check(ctx, reply)
    fb = r.feedback()
    assert "3.15A fuse" in fb
    assert "keep the rest" in fb, "feedback should ask for a partial edit"


def test_fully_faithful_compound_sentence_still_passes(ctx):
    """Clause checking must not reject a sentence whose every clause is real."""
    reply = ("Hello Ivan Nowak,\n\nPlease unplug the mains cord from both the "
             "wall outlet and the rear inlet of the unit, and then reseat "
             "both ends firmly.\n\nThis follows PROC-P4800-PWR-A.\n\n"
             "Kind regards,\nCustomer Support")
    r = check(ctx, reply)
    assert r.grounded, f"faithful compound sentence rejected: {r.reason}"


def test_clause_splitter_ignores_short_fragments():
    assert grounding.split_clauses("Unplug it and wait.") == []
    cl = grounding.split_clauses(
        "Reseat both ends of the mains cord firmly, and then replace the "
        "fuse in the rear carrier assembly.")
    assert len(cl) == 2


def test_process_statements_are_exempt_but_device_claims_are_not():
    """The process exemption must not become a smuggling route.

    A sentence about what the agent will do next is not traceable to a QMS
    extract and is correctly exempt. The same sentence opening, followed by a
    physical instruction, is NOT exempt.
    """
    assert grounding.is_process_statement(
        "I have confirmed your P4800 revision A on your account record.")
    assert grounding.is_process_statement(
        "I will escalate this to a support engineer.")
    # Device instructions hidden behind process phrasing stay checkable.
    assert not grounding.is_process_statement(
        "Let me know once you have replaced the fuse in the rear carrier.")
    assert not grounding.is_process_statement(
        "I will raise a return once you unplug the mains cord.")


def test_fabrication_behind_process_phrasing_is_still_caught(ctx):
    r = check(ctx, REAL_LLM_REPLY + APPEND.format(
        "let me know once you have replaced the 3.15A fuse in the rear "
        "carrier"))
    assert not r.grounded


def test_word_boundary_not_substring(ctx):
    """'record' must not be read as mentioning a mains 'cord'."""
    assert grounding.is_process_statement(
        "I have checked your account record and the system entry.")


def test_sentence_splitter_does_not_break_on_colon():
    sents = grounding.split_sentences(
        "Please verify that: the LED is dark, the fan is silent.")
    assert len(sents) == 1
