"""rc.19 QMS conflict review on ISOLATED fixtures (the demo corpus is untouched)."""
import shutil
from pathlib import Path

import pytest

from capstone_rag import config, qms, qms_policy

HEADER = """---
document_id: {doc}
procedure_id: {proc}
title: {title}
model: P4800
applicable_revisions: [{revs}]
document_version: "1.0"
approval_status: {status}
audiences: [customer_service]
document_type: customer_troubleshooting_instruction
access_permissions: [customer_disclosable, agent_internal, representative]
permitted_uses: [customer_answer, revision_troubleshooting]
effective_date: 2026-01-01
{extra}---
# {title}

## Steps
{steps}
"""


def _doc(folder, doc, proc, *, revs='A', status='Approved', topic=None, precedence=None, steps='Check the power lead.', extra=''):
    lines = extra
    if topic:
        lines += f'topic: {topic}\n'
    if precedence:
        lines += f'precedence_over: [{precedence}]\n'
    (folder / f'{doc}.md').write_text(HEADER.format(doc=doc, proc=proc, title=f'Fixture {doc}', revs=revs, status=status,
                                                    extra=lines, steps=steps), encoding='utf-8')


@pytest.fixture
def corpus(tmp_path):
    folder = tmp_path / 'qms'
    folder.mkdir()
    return folder


def _review(folder, model='P4800', revision='A'):
    return qms_policy.review_conflicts(qms.load_chunks(str(folder)), model=model, revision=revision)


def test_conflicting_current_approved_procedures_require_document_control(corpus):
    _doc(corpus, 'FIX-001', 'PROC-FIX-PWR-1', topic='power_failure', steps='Replace the inlet fuse with a 2 A fuse.')
    _doc(corpus, 'FIX-002', 'PROC-FIX-PWR-2', topic='power_failure', steps='Do not open the inlet; return the unit.')
    review = _review(corpus)
    assert review['requires_document_control']
    assert review['conflicts'][0]['documents'] == ['FIX-001', 'FIX-002']


def test_declared_precedence_resolves_without_human(corpus):
    _doc(corpus, 'FIX-001', 'PROC-FIX-PWR-1', topic='power_failure')
    _doc(corpus, 'FIX-003', 'PROC-FIX-PWR-3', topic='power_failure', precedence='FIX-001')
    review = _review(corpus)
    assert not review['requires_document_control']
    assert review['resolved'] == [{'topic': 'power_failure', 'authority': 'FIX-003', 'over': ['FIX-001'], 'basis': 'declared precedence_over'}]


def test_wrong_revision_and_superseded_are_not_conflicts(corpus):
    _doc(corpus, 'FIX-001', 'PROC-FIX-PWR-1', topic='power_failure')
    _doc(corpus, 'FIX-004', 'PROC-FIX-PWR-C', revs='C', topic='power_failure')            # inapplicable revision
    _doc(corpus, 'FIX-005', 'PROC-FIX-PWR-OLD', status='Superseded', topic='power_failure',
         extra='superseded_by: FIX-001\n')                                                  # superseded
    review = _review(corpus)
    assert not review['requires_document_control']
    assert review['inapplicable'] == ['FIX-004'] and review['superseded'] == ['FIX-005']


def test_complementary_documents_and_wording_differences_do_not_conflict(corpus):
    _doc(corpus, 'FIX-001', 'PROC-FIX-PWR-1', topic='power_failure', steps='Check the lead. Confirm the indicator.')
    _doc(corpus, 'FIX-006', 'PROC-FIX-ACC-1', topic='missing_accessory', steps='Check under the moulded insert.')
    _doc(corpus, 'FIX-007', 'PROC-FIX-PWR-1', topic='power_failure', steps='Confirm the indicator, then check the lead.')  # same procedure, reworded
    review = _review(corpus)
    assert not review['requires_document_control'] and review['conflicts'] == []


def test_demo_corpus_has_no_declared_conflicts():
    review = qms_policy.review_conflicts(qms.load_chunks(), model='P4800', revision='A')
    assert not review['requires_document_control']


def test_workflow_blocks_release_and_routes_conflict_to_document_control(corpus, monkeypatch, tmp_path):
    import sqlite3
    from capstone_rag import caseflow, store as store_mod, task_context, reasoning_strategies, phase4_runtime
    from capstone_rag.collaboration import Coordinator
    from capstone_rag.orchestration import CaseOrchestrator, ROLE_CAPABILITIES
    _doc(corpus, 'FIX-001', 'PROC-FIX-PWR-1', topic='power_failure')
    _doc(corpus, 'FIX-002', 'PROC-FIX-PWR-2', topic='power_failure')
    chunks = qms.load_chunks(str(corpus))
    st = store_mod.Store(str(tmp_path / 'ops.db'))
    with sqlite3.connect(config.DB_PATH) as src, sqlite3.connect(st.path) as dst:
        for table in ('customers', 'devices'):
            cols = [r[1] for r in src.execute(f'PRAGMA table_info({table})')]
            dst.executemany(f'INSERT OR IGNORE INTO {table} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})', src.execute(f'SELECT {",".join(cols)} FROM {table}').fetchall())
    flow = caseflow.CaseFlow(st, caseflow.make_agent(prefer_llm=False, store=st))
    cid = flow.intake('Power', 'My name is Ivan Nowak. Serial 98148469. It will not power on.', channel='email')
    monkeypatch.setattr(reasoning_strategies, 'observation_driven_qms',
                        lambda *a, **k: ({'chunks': chunks, 'procedures': ['PROC-FIX-PWR-1', 'PROC-FIX-PWR-2'], 'evidence': {}},
                                         {'termination': 'answered', 'rounds': []}))
    monkeypatch.setattr(phase4_runtime, 'qms_mcp_evidence', lambda *a, **k: {})
    task = Coordinator(st, 'WF-CONFLICT').delegate(case_id=cid, issuer='erp_research', recipient='qms_research', purpose='test',
        based_on_version=0, permitted_tools=sorted(ROLE_CAPABILITIES['qms_research']), operation_scope='advisory_finding',
        idempotency_key='WF-CONFLICT:qms')
    device = st.device('98148469')
    with task_context.bind(task_id=task['task_id'], workflow_id='WF-CONFLICT', agent_id='qms_research'):
        result = CaseOrchestrator(flow)._qms({'cid': cid, 'workflow_id': 'WF-CONFLICT', 'body': 'It will not power on.',
                                              'safety': {'hold': False}, 'erp': {'device': device, 'evidence_message_ids': []}})
    assert result['sources'] == [] and result['blocked'].startswith('QMS conflict')
    # The status is applied by the specialist wrapper after the finding is
    # recorded (rc.20: changing the case mid-specialist crashed real runs).
    assert result['case_status_after_finding'] == caseflow.HUMAN_REVIEW
    assert st.one("SELECT 1 FROM audit_events WHERE case_id=? AND event_type='qms_conflict_document_control_review'", (cid,))
    st.close()
