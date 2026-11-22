"""Observed support defects and persistent execution evidence controls."""
import json
import threading
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capstone_rag import activity, generation, guidance, identity, response_policy, review
from capstone_rag.webapp.app import create_app
from fastapi.testclient import TestClient
from test_defects import st, source_db, shared_agent, bot, flow


@pytest.mark.parametrize('text', ['wei murphy', 'WEI MURPHY', 'WeI MuRpHy',
    'its wei murphy', "it's wei murphy", 'my name is wei murphy', 'this is Wei Murphy',
    'wei murphy and my date of birth is 1954-04-18'])
def test_names_case_and_context(text):
    claim = identity.Claim.from_text(text, expecting=identity.VERIFICATION_POINTS,
                                     known_names=['Wei Murphy'])
    assert identity.normalise_name(claim.name) == 'wei murphy'


@pytest.mark.parametrize('text', ['1954-04-18', '04/18/1954', '4/18/1954',
    '04-18-1954', 'April 18, 1954', 'Apr 18 1954', '18 April 1954', '18 Apr 1954',
    '18/04/1954', '18-04-1954', 'my birthday is april 18 1954', 'born on 1954-04-18'])
def test_unambiguous_dates(text):
    assert identity.Claim.from_text(text, expecting=identity.VERIFICATION_POINTS).dob == '1954-04-18'


@pytest.mark.parametrize('text', ['04/05/1954', '1954-02-30', '29 Feb 1953', '31 April 1954', '1800-04-18', '2999-01-01'])
def test_bad_or_ambiguous_dates_do_not_verify(text):
    claim = identity.Claim.from_text('date of birth ' + text)
    assert claim.dob is None
    assert claim.dob_issue


def test_leap_date():
    assert identity.normalise_dob('29 Feb 1956') == '1956-02-29'


def test_name_only_received_is_not_missing(bot):
    session = bot.new_session()
    bot.respond(session, 'hi my p100 isnt working, the lamp wont turn on')
    assert session.claim.name is None
    reply = bot.respond(session, 'john doe')
    assert session.claim.name.lower() == 'john doe'
    assert 'name on the account' not in reply.text.lower()
    assert 'serial number' in reply.text.lower()
    assert 'thank you for contacting' not in reply.text.lower()
    assert 'lamp' in session.symptoms[0]


def test_typo_correction_preserves_dob_and_vague_issue(bot, st):
    customer = st.one('SELECT * FROM customers WHERE customer_name=?', ('Wei Murphy',))
    assert customer['date_of_birth'] == '1954-04-18'
    session = bot.new_session()
    bot.respond(session, 'hi my device isnt working')
    bot.respond(session, 'mei murphy and my date of birth is 1954-04-18')
    bot.respond(session, 'its wei murphy')
    reply = bot.respond(session, '52323377')
    assert session.claim.name.lower() == 'wei murphy'
    assert session.claim.dob == '1954-04-18'
    assert session.verified
    assert not session.handed_off
    assert 'turn on' in reply.text.lower()
    assert session.corrections
    assert not st.query("SELECT * FROM activity_events WHERE conversation_id=? AND category='retrieval'", (session.conversation_id,))
    reply = bot.respond(session, 'It turns on but the lamps stay off. There is no smoke or injury.')
    assert not session.handed_off
    assert reply.sources
    assert session.procedures_used == ['PROC-3SERIES-LAMP']


def test_ambiguous_date_requests_clarification(bot):
    session = bot.new_session()
    reply = bot.respond(session, 'wei murphy dob 04/05/1954')
    assert not session.verified
    assert 'month spelled out' in reply.text


def test_alias_is_not_identity():
    for text in ('my P100', 'p-100', 'P 100'):
        result = guidance.tentative_model(text)
        assert result['model'] is None
        assert result['status'] == 'unsupported_model_reference'
        assert result['revision'] is None
        assert not result['device_identified']
        assert identity.Claim.from_text(text).serial is None


def test_public_label_help_before_verification(bot):
    session = bot.new_session()
    reply = bot.respond(session, 'where can I find the serial label?')
    assert not session.verified
    assert 'without opening' in reply.text


def test_current_sqlite_device_reaches_case_agent(flow, st):
    customer = st.one('SELECT * FROM customers WHERE customer_name=?', ('Yuki Williams',))
    device = st.one('SELECT * FROM devices WHERE account_id=?', (customer['account_id'],))
    cid = flow.intake('Refill process', f"my name is {customer['customer_name']}. serial number {device['serial_number']}. How does the refill process work?")
    flow.pre_investigate(cid)
    case = st.case(cid)
    assert case['identity_verified']
    draft = review.latest_draft(st, cid)
    assert 'Yuki Williams' in draft['body']
    assert 'no serial' not in draft['body'].lower()
    assert 'troubleshooting' not in draft['body'].lower()
    denied = review.approve_and_send(st, cid, draft['message_id'], 'I was not able to confirm which device this is about. No serial number was provided.', 'rep.test')
    assert not denied['ok']
    assert review.latest_draft(st, cid)['status'] == 'draft'
    assert any(e['event_type'] == 'draft_consistency_rejected' for e in st.audit_trail(cid))


def test_trace_contains_turns_before_handoff_and_survives_reopen(bot, st):
    previous_count=len(st.query('SELECT * FROM activity_events'))
    session = bot.new_session()
    bot.respond(session, 'wei murphy')
    before = st.query('SELECT * FROM activity_events WHERE conversation_id=?', (session.conversation_id,))
    assert {'message', 'identity', 'verification', 'response'} <= {e['category'] for e in before}
    bot.respond(session, 'speak to a person')
    assert session.handoff_case_id
    rows = st.query('SELECT * FROM activity_events WHERE conversation_id=?', (session.conversation_id,))
    assert all(r['case_id'] == session.handoff_case_id for r in rows)
    from capstone_rag.store import Store
    reopened = Store(st.path)
    assert len(reopened.query('SELECT * FROM activity_events')) == previous_count + len(rows)
    reopened.close()


def test_trace_routes_do_not_infer_or_duplicate_and_export(st, shared_agent, monkeypatch, tmp_path):
    from capstone_rag import config
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
    app = create_app(store=st, agent=shared_agent, prefer_llm=False, start_workers=False)
    with TestClient(app) as client:
        chat = client.post('/api/chat/sessions').json()
        cid = chat['conversation_id']
        client.post(f'/api/chat/sessions/{cid}/messages', json={'text': 'wei murphy'})
        before = len(st.query('SELECT * FROM activity_events'))
        for route in ('/', '/chat', '/trace', f'/api/trace/conversation/{cid}'):
            assert client.get(route).status_code == 200
        assert len(st.query('SELECT * FROM activity_events')) == before
        export = client.post(f'/api/trace/conversation/{cid}/export').json()
        assert Path(export['json_path']).exists()
        assert Path(export['markdown_path']).exists()
        again = client.post(f'/api/trace/conversation/{cid}/export').json()
        assert export['json_path'] != again['json_path']
        assert 'message' in Path(export['markdown_path']).read_text(encoding='utf-8')


def test_actual_model_call_events_and_failure(st, monkeypatch):
    monkeypatch.setattr(generation, '_post', lambda *a, **kw: {'message': {'content': 'Thank you. Could you provide the serial number?'}})
    with activity.investigation(st, conversation_id='CONV-TEST'):
        text = generation.llm_complete('system', 'user')
    assert 'Thank you' in text
    rows = st.query("SELECT * FROM activity_events WHERE category='model'")
    assert [r['status'] for r in rows] == ['queued', 'awaiting_model_response', 'validating']
    assert rows[-1]['duration_ms'] >= 0
    def failed(*a, **kw):
        raise TimeoutError('test timeout')
    monkeypatch.setattr(generation, '_post', failed)
    with activity.investigation(st, conversation_id='CONV-TEST'):
        with pytest.raises(TimeoutError):
            generation.llm_complete('system', 'user')
    assert st.one("SELECT * FROM activity_events WHERE category='model' AND status='failed'")


def test_model_task_commentary_is_rejected(st, monkeypatch):
    monkeypatch.setattr(generation, '_post', lambda *a, **kw: {'message': {'content': 'We are given a conversation history. The task is to output only the reply.'}})
    with activity.investigation(st, conversation_id='CONV-COMMENTARY'):
        with pytest.raises(ValueError, match='task commentary'):
            generation.llm_complete('system', 'user')
    assert st.one("SELECT * FROM activity_events WHERE category='model' AND status='rejected'")


def test_failed_ollama_call_uses_identified_template_fallback(bot, st, monkeypatch):
    monkeypatch.setattr(bot.agent, 'prefer_llm', True)
    monkeypatch.setattr(generation, 'llm_available', lambda: True)
    def failed(*args, **kwargs):
        raise TimeoutError('simulated local inference failure')
    monkeypatch.setattr(generation, '_post', failed)
    session = bot.new_session()
    reply = bot.respond(session, 'wei murphy')
    assert 'serial number' in reply.text
    events = st.query('SELECT * FROM activity_events WHERE conversation_id=?', (session.conversation_id,))
    assert any(e['category'] == 'model' and e['status'] == 'failed' for e in events)
    response = next(json.loads(e['evidence']) for e in events if e['category'] == 'response')
    assert response['generation_mode'] == 'template'
    assert not response['model_response_used']
    monkeypatch.setattr(generation, '_post', lambda *a, **kw: {'message': {
        'content': json.dumps({'reply': '. Please press the reset button. Could you provide the serial number from the label on the unit?'})}})
    another = bot.new_session()
    rejected = bot.respond(another, 'wei murphy')
    assert 'press' not in rejected.text.lower()
    assert st.one("SELECT * FROM activity_events WHERE conversation_id=? AND category='response_validation' AND status='rejected'", (another.conversation_id,))


def test_slow_call_visible_while_running(st, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def slow(*a, **kw):
        entered.set()
        assert release.wait(5)
        return {'message': {'content': 'Hello, thank you.'}}
    monkeypatch.setattr(generation, '_post', slow)
    def run():
        with activity.investigation(st, conversation_id='CONV-SLOW'):
            generation.llm_complete('system', 'user')
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        assert st.one("SELECT * FROM activity_events WHERE status='awaiting_model_response'")
    finally:
        release.set()
        worker.join(5)


def test_hazard_determination_retains_screening(flow, st):
    cid = flow.intake('Burning smell', 'There is a burning smell but no one was injured.')
    note = st.one('SELECT * FROM qa_notifications WHERE case_id=?', (cid,))
    result = review.determine_adverse_event(st, note['notification_id'], 'not an adverse event', 'rep.test', 'No reported injury; hazard remains under review.')
    assert result['ok']
    after = st.one('SELECT * FROM qa_notifications WHERE notification_id=?', (note['notification_id'],))
    assert after['body'] == note['body']
    assert after['certainty'] == note['certainty']
    assert after['determination_note']
    assert st.case(cid)['classification'] == 'device_safety_hazard'
    assert st.case(cid)['priority'] == 'Critical'


def test_determination_advances_attention_without_erasing_original_screen(flow, st):
    from capstone_rag import presentation
    cid = flow.intake('Smoke', 'My name is Ivan Nowak. Serial 98148469. There is smoke but no injury.')
    flow.pre_investigate(cid)
    note = st.one('SELECT * FROM qa_notifications WHERE case_id=?', (cid,))
    item = st.one('SELECT * FROM checklist_items WHERE case_id=? AND action=?', (cid, 'screen_adverse_event'))
    review.determine_adverse_event(st, note['notification_id'], 'device hazard without confirmed injury', 'rep.test', 'Hazard retained.')
    after = st.one('SELECT * FROM checklist_items WHERE item_id=?', (item['item_id'],))
    assert after['tool_result'] == item['tool_result']
    assert presentation.outcome_for(after) == presentation.COMPLETED


def test_safety_handoff_uses_customer_statements_instead_of_truncated_subject(bot, st):
    session = bot.new_session()
    bot.respond(session, 'My name is Ivan Nowak. Serial 98148469. There is a burning smell, but no one was injured.')
    case = st.case(session.handoff_case_id)
    assert case['classification'] == 'device_safety_hazard'
    note = st.one('SELECT * FROM qa_notifications WHERE case_id=?', (case['case_id'],))
    assert note['kind'] == 'possible_adverse_event'
    assert note['certainty'] == 'none'


def test_free_text_cannot_substitute_structured_determination():
    from capstone_rag.webapp.schemas import Determination
    with pytest.raises(ValueError):
        Determination(authorizer='rep.test', determination='I think probably not')


@pytest.mark.parametrize('text', ['There is no smoke or injury.', 'No redness, irritation or pain.',
                                 'There was no injury, but it smoked.',
                                 'The manual says "stop if there is smoke or pain". I only need a guide.'])
def test_negated_and_quoted_safety_statements(text):
    from capstone_rag import triage
    result = triage.assess_adverse_event(text)
    if 'but it smoked' in text:
        assert result.hazard_indicators
        assert not result.is_adverse_event
    else:
        assert not result.is_adverse_event
        assert not result.hazard_indicators
