"""Paths and tunables for the capstone RAG demonstration."""

from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(PACKAGE_DIR))

# Deployment keeps replaceable application files separate from operational
# state.  Development remains unchanged when CAPSTONE_DATA_ROOT is unset.
DATA_DIR = os.path.abspath(os.environ.get(
    "CAPSTONE_DATA_ROOT", os.path.join(PROJECT_ROOT, "data")))
ERP_DIR = os.path.join(DATA_DIR, "erp")
QMS_DIR = os.path.join(DATA_DIR, "qms")
COMPATIBILITY_PATH = os.path.join(DATA_DIR, "qms_compatibility.json")
INDEX_PATH = os.path.join(DATA_DIR, "index", "qms_index.npz")
TICKET_LOG_PATH = os.path.join(DATA_DIR, "ticket_log.jsonl")

# --- embedding model -------------------------------------------------------
# A genuine transformer sentence-embedding model, run locally on CPU.
# Separate from the generation model, as required by the brief.
EMBEDDING_MODEL = os.environ.get(
    "CAPSTONE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)

# --- generation model ------------------------------------------------------
# Legacy defaults retained for compatibility. Application model requests use
# ai_settings/inference pinned private routes; transport failures are recorded
# and stop the session. Explicit --no-llm remains a deterministic mode.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CAPSTONE_LLM_MODEL", "qwen3.5:4b")
# CPU-only generation on this class of machine runs at roughly 8 tokens/sec,
# so a 600-token reply needs a generous ceiling.
OLLAMA_TIMEOUT = float(os.environ.get("CAPSTONE_LLM_TIMEOUT", "300"))

# --- retrieval tunables ----------------------------------------------------
# SEARCH_K candidates are pulled from the vector index by pure semantic
# similarity, with no metadata filtering, so that wrong-revision and withdrawn
# documents are visibly considered and then visibly rejected.
SEARCH_K = 20
# At most ACCEPT_K validated chunks are passed to generation.
ACCEPT_K = 3
# Candidates below this cosine similarity are treated as not relevant at all.
MIN_SIMILARITY = 0.20
# An authorised but unrelated procedure can clear the absolute floor on
# generic words. It must also remain close to the best semantic candidate.
MAX_RELATIVE_SCORE_GAP = 0.15

# --- grounding verification -------------------------------------------------
# A reply sentence is supported if ANY of three clauses holds. One threshold is
# not enough: generic instruction phrasing ("please check the X and verify Y")
# scores around 0.5 against almost any procedure text, so similarity alone
# would wave through an invented step. Pairing a moderate similarity with a
# lexical anchor separates paraphrase from fabrication.
#
#   1. STRONG_SIM alone          - a clean paraphrase of a source sentence
#   2. MIN_SIM and MIN_RECALL    - moderate match, anchored by shared wording
#   3. STRONG_RECALL alone       - near-verbatim lift the encoder scored oddly
#
# Calibrated against the captured Qwen3 output in docs/evidence/run_llm.txt and
# against deliberately fabricated steps; see tests/test_grounding.py.
GROUNDING_STRONG_SIM = float(os.environ.get("CAPSTONE_GROUNDING_STRONG_SIM", "0.70"))
GROUNDING_MIN_SIM = float(os.environ.get("CAPSTONE_GROUNDING_MIN_SIM", "0.50"))
GROUNDING_MIN_RECALL = float(os.environ.get("CAPSTONE_GROUNDING_MIN_RECALL", "0.30"))
GROUNDING_STRONG_RECALL = float(
    os.environ.get("CAPSTONE_GROUNDING_STRONG_RECALL", "0.60")
)

# --- ReAct loop ---------------------------------------------------------------
# Unproductive attempts only: a retrieval that yields nothing usable, or a draft
# that fails grounding. Legitimate troubleshooting steps are not counted.
MAX_UNPRODUCTIVE_ATTEMPTS = int(
    os.environ.get("CAPSTONE_MAX_UNPRODUCTIVE", "3")
)

# --- chatbot identification policy ---------------------------------------------
# A separate, bounded budget for *identification*, deliberately not the same
# counter as MAX_UNPRODUCTIVE_ATTEMPTS above: that one governs the agent's
# reasoning loop, this one governs how many times a customer may be asked for
# identification before a person is offered instead. An attempt counts only
# when a turn produced no new verification point and no new identifying
# detail, so multi-turn troubleshooting is never mistaken for stagnation.
MAX_IDENTIFICATION_ATTEMPTS = int(
    os.environ.get("CAPSTONE_MAX_ID_ATTEMPTS", "3")
)
# When the details were read correctly but match no record, asking again
# cannot help, so the budget is shorter.
MAX_UNMATCHED_IDENTIFICATION_ATTEMPTS = int(
    os.environ.get("CAPSTONE_MAX_UNMATCHED_ID_ATTEMPTS", "2")
)

# --- case / approval stores ---------------------------------------------------
ESCALATION_PATH = os.path.join(DATA_DIR, "escalations.jsonl")
RMA_PATH = os.path.join(DATA_DIR, "rma_requests.jsonl")
MEMORY_PATH = os.path.join(DATA_DIR, "memory", "resolutions.jsonl")

# --- persistent operational database ------------------------------------------
DB_PATH = os.environ.get("CAPSTONE_DB",
                         os.path.join(DATA_DIR, "operations.db"))
LABEL_DIR = os.path.join(DATA_DIR, "labels")

# --- synthetic source pools (read-only, outside this project) ------------------
PYCHARM_PROJECTS_ROOT = os.environ.get(
    "PYCHARM_PROJECTS_ROOT", os.path.expanduser(os.path.join("~", "PycharmProjects")))
SANDBOX_DB = os.path.join(PYCHARM_PROJECTS_ROOT, "SalesforceExtract",
                          "sandbox", "data", "sandbox.db")
TICKETS_JSONL = os.path.join(PYCHARM_PROJECTS_ROOT, "PhotheraOnPremStack",
                             "ticket-generator", "output",
                             "tickets_synthetic_1000.jsonl")

# --- arrival simulator ---------------------------------------------------------
ARRIVAL_MIN_SECONDS = int(os.environ.get("CAPSTONE_ARRIVAL_MIN", "30"))
ARRIVAL_MAX_SECONDS = int(os.environ.get("CAPSTONE_ARRIVAL_MAX", "120"))
INFERENCE_QUEUE_MAX = int(os.environ.get("CAPSTONE_QUEUE_MAX", "64"))

# --- device revision provenance ------------------------------------------------
# The synthetic ERP has no revision field. A revision derived from the install
# date is an INFERENCE, not source data, and must not by itself authorise
# revision-specific troubleshooting.
REVISION_VERIFIED = "verified"
REVISION_INFERRED = "inferred"
REVISION_MISSING = "missing"
REVISION_CONTRADICTORY = "contradictory"
# Retained as a compatibility name only.  No controlled source establishes
# that a meter recalibration also inspected the hardware revision, so this
# undocumented exception is deliberately disabled and is not consumed by the
# authorization boundary.  RV-1 requires the revision register or a named
# case-specific human confirmation.
REVISION_TRUST_INFERRED_WITH_RECAL = False

# --- safety ----------------------------------------------------------------
# Substrings that force escalation before any retrieval happens.
SAFETY_TERMS = (
    "burning smell", "burnt smell", "smoke", "smoking", "scorch", "charred",
    "melted", "melting", "fire", "sparks", "sparking", "electric shock",
    "shocked me", "burn", "burned", "burnt", "blister", "injury", "injured",
    "hospital", "bleeding", "too hot to touch",
)
