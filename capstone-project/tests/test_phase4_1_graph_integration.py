"""Phase 4.1 integration with the existing safety-controlled LangGraph."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_defects import flow, shared_agent, source_db, st  # noqa: E402,F401

from capstone_rag import response_policy


def test_langgraph_consumes_concurrent_mcp_evidence_and_preserves_human_gate(flow, st):
    cid = flow.intake('Synthetic lamp issue',
        'My name is Ivan Nowak. Serial 98148469. The unit powers on but the lamp is dark.')
    result = flow.pre_investigate(cid)
    assert result['paused_for_review']
    run = st.one('SELECT * FROM workflow_runs WHERE workflow_id=?', (result['workflow_id'],))
    tasks = st.query('SELECT * FROM execution_tasks WHERE workflow_id=? ORDER BY created_at',
                     (run['workflow_id'],))
    assert {task['task_type'] for task in tasks} == {
        'initial.erp', 'initial.ticket', 'initial.safety', 'initial.join'}
    assert all(task['state'] == 'completed' for task in tasks)
    calls = st.query('SELECT * FROM mcp_tool_invocations WHERE workflow_id=?',
                     (run['workflow_id'],))
    assert {'erp.lookup_device', 'ticket.lookup_history', 'qms.search_authorized'} <= {
        call['tool_name'] for call in calls}
    erp_finding = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='erp_research'",
        (cid,))['payload'])
    qms_finding = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'",
        (cid,))['payload'])
    assert erp_finding['mcp_erp_evidence']['status'] == 'matched'
    assert erp_finding['mcp_ticket_history']['qms_authority'] is False
    assert qms_finding['mcp_evidence']['authorization_decision'] == 'not_granted_by_tool'
    assert not st.query("SELECT * FROM messages WHERE case_id=? AND status='sent'", (cid,))
    assert not st.query("SELECT * FROM rmas WHERE case_id=? AND status='executed'", (cid,))
    event_types = {row['event_type'] for row in st.query(
        'SELECT event_type FROM arena_events WHERE workflow_id=?', (run['workflow_id'],))}
    assert {'task.started', 'mcp.call.succeeded', 'join.completed',
            'human_review.requested'} <= event_types


def test_parallel_execution_does_not_weaken_unsupported_approval_guard(flow, st):
    cid = flow.intake('Synthetic unsupported approval',
        'My name is Ivan Nowak. Serial 98148469. Please approve a replacement.')
    flow.pre_investigate(cid)
    bad = ('We confirmed that your device serial is 681:77-DV5, and the '
           'requested replacement is approved and shipped.')
    report = response_policy.validate_case(st, cid, bad)
    assert not report['passed']
    assert any('authoritative approved/completed record' in issue['fact']
               for issue in report['failures'])


def test_two_revision_scoped_cases_keep_execution_and_mcp_evidence_isolated(flow, st):
    first = flow.intake('Rev A synthetic case',
        'My name is Ivan Nowak. Serial 98148469. The unit powers on but the lamp is dark.')
    second = flow.intake('Rev B synthetic case',
        'My name is Mary Cohen. Serial 68177-DV5. The unit powers on but the lamp is dark.')
    one = flow.pre_investigate(first); two = flow.pre_investigate(second)
    assert one['workflow_id'] != two['workflow_id']
    rows_one = st.query('SELECT * FROM execution_tasks WHERE workflow_id=?', (one['workflow_id'],))
    rows_two = st.query('SELECT * FROM execution_tasks WHERE workflow_id=?', (two['workflow_id'],))
    assert rows_one and rows_two
    assert {row['case_id'] for row in rows_one} == {first}
    assert {row['case_id'] for row in rows_two} == {second}
    qms_one = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'", (first,))['payload'])
    qms_two = json.loads(st.one(
        "SELECT payload FROM specialist_findings WHERE case_id=? AND role='qms_research'", (second,))['payload'])
    assert qms_one['revision_authority']['revision'] != qms_two['revision_authority']['revision']
    assert all(call['case_id'] == first for call in st.query(
        'SELECT case_id FROM mcp_tool_invocations WHERE workflow_id=?', (one['workflow_id'],)))
    assert all(call['case_id'] == second for call in st.query(
        'SELECT case_id FROM mcp_tool_invocations WHERE workflow_id=?', (two['workflow_id'],)))
