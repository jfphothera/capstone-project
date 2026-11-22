"""Read-only correlated activity, plus explicit local evidence export."""
import json
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from ... import config, generation, qms
from ..runtime import get_runtime

router = APIRouter(prefix='/api/trace', tags=['agent activity'])


def decoded(value):
    try:
        return json.loads(value, parse_constant=lambda _: None) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return value


@router.get('/subjects')
def subjects():
    store = get_runtime().store
    chats = store.query('SELECT conversation_id AS id, case_id, updated_at, state_json FROM conversations ORDER BY updated_at DESC LIMIT 100')
    for chat in chats:
        state = decoded(chat.pop('state_json')) or {}
        chat.update(kind='conversation', label=f"{chat['id']} · {(state.get('claim') or {}).get('name') or 'Customer chat'}")
    cases = store.query('SELECT case_id AS id, subject, updated_at, ai_state, status FROM cases ORDER BY updated_at DESC LIMIT 100')
    for case in cases:
        case.update(kind='case', label=f"{case['id']} · {case['subject']}")
    active = store.query("SELECT e.* FROM activity_events e WHERE e.category='investigation' AND e.status='running' "
        "AND NOT EXISTS (SELECT 1 FROM activity_events x WHERE x.investigation_id=e.investigation_id "
        "AND x.category='investigation' AND x.status IN ('completed','failed')) ORDER BY e.event_id DESC LIMIT 8")
    return {'subjects': chats + cases, 'active': active}


@router.get('/runtime')
def runtime():
    rt = get_runtime()
    return {'queue': rt.ops.queue.snapshot(), 'model': generation.model_status(),
            'prefer_llm': rt.prefer_llm, 'inference_workers': 1}


@router.get('/qms/inventory')
def qms_inventory():
    from ... import qms_policy
    documents = {}
    for chunk in qms.load_chunks():
        if chunk.document_id not in documents:
            documents[chunk.document_id] = {**chunk.as_dict(), 'sections': [],
                'customer_authorization_context': {'model': chunk.model, 'revision': 'A',
                    'revision_context': {'status':'inventory_example','usable_for_troubleshooting':True}},
                'internal_workflow_authorization': qms_policy.authorize(chunk, operation='internal_workflow', recipient='internal').as_dict(),
                'customer_disclosure_authorization': qms_policy.authorize(chunk, operation='customer_answer', recipient='customer',
                    context={'model': chunk.model, 'revision': 'A',
                             'revision_context': {'status':'inventory_example','usable_for_troubleshooting':True,
                                                  'reason':'inventory display example'},
                             'compatibility': qms.load_compatibility()}).as_dict()}
        documents[chunk.document_id]['sections'].append(chunk.section_path)
    return {'policy': qms_policy.POLICY_ID, 'documents': list(documents.values()),
            'document_count': len(documents), 'chunk_count': len(qms.load_chunks()),
            'note': 'Examples of authorization include their context; every actual use is evaluated separately.'}


def bundle(kind, subject_id):
    store = get_runtime().store
    if kind not in ('conversation', 'case') or not re.fullmatch(r'(?:CONV|CASE)-\d+', subject_id):
        raise HTTPException(400, 'Select a recorded case or conversation')
    case_id, conversation_id = (subject_id, None) if kind == 'case' else (None, subject_id)
    conversation = store.one('SELECT * FROM conversations WHERE conversation_id=?', (conversation_id,)) if conversation_id else store.one('SELECT * FROM conversations WHERE case_id=?', (case_id,))
    if conversation:
        conversation_id = conversation['conversation_id']
        case_id = case_id or conversation.get('case_id')
    case = store.case(case_id) if case_id else None
    if not case and not conversation:
        raise HTTPException(404, 'No recorded subject')
    events = store.query('SELECT * FROM activity_events WHERE conversation_id=? OR case_id=? ORDER BY event_id', (conversation_id, case_id))
    for event in events:
        event['evidence'] = decoded(event.get('evidence'))
    # Audit/checklist rows remain their original records and timestamps. Older
    # records are shown as historical evidence, never reconstructed model calls.
    audit = store.query('SELECT * FROM audit_events WHERE case_id=? ORDER BY at', (case_id,)) if case_id else []
    checklist = store.checklist(case_id) if case_id else []
    for row in audit:
        events.append({'event_id': f"audit-{row.get('event_id', row.get('id', len(events)))}",
                       'at': row['at'], 'category': 'audit', 'status': 'recorded',
                       'summary': row['event_type'] + ': ' + (row.get('summary') or ''),
                       'case_id': case_id, 'evidence': {**row, 'detail': decoded(row.get('detail'))}})
    events.sort(key=lambda event: (event['at'], str(event['event_id']).zfill(16)))
    state = decoded(conversation['state_json']) if conversation else None
    documents = {c.chunk_id: {'document_id': c.document_id, 'procedure_id': c.procedure_id,
                              'section': c.section_path, 'audience': c.audiences,
                              'model': c.model, 'applicable_revisions': c.applicable_revisions,
                              'document_version': c.document_version} for c in qms.load_chunks()}
    sources = store.query('SELECT * FROM retrieved_sources WHERE case_id=? ORDER BY at, rank', (case_id,)) if case_id else []
    for source in sources:
        source['metadata'] = documents.get(source['chunk_id'])
    coordination=__import__('capstone_rag.coordination',fromlist=['CaseRepository']).CaseRepository(store).snapshot(case_id) if case_id else {}
    if conversation_id:
        coordination['specialist_findings']=coordination.get('specialist_findings',[])+store.query('SELECT * FROM conversation_findings WHERE conversation_id=? ORDER BY at',(conversation_id,))
    return {'schema_version': 1, 'subject': {'kind': kind, 'id': subject_id},
            'coordination': coordination,
            'inference_snapshots':store.query('SELECT * FROM inference_snapshots WHERE scope_key=? OR scope_key=? ORDER BY at',('case:'+str(case_id),'conversation:'+str(conversation_id))),
            'inference_calls':store.query('SELECT * FROM inference_calls WHERE case_id=? OR conversation_id=? ORDER BY at',(case_id,conversation_id)),
            'candidate_plans': store.query('SELECT * FROM candidate_plans WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'plan_evaluations': store.query('SELECT * FROM plan_evaluations WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'delegated_tasks': store.query('SELECT * FROM delegated_tasks WHERE case_id=? ORDER BY created_at',(case_id,)) if case_id else [],
            'delegation_events': store.query('SELECT * FROM delegation_events WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'specialist_conflicts': store.query('SELECT * FROM specialist_conflicts WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'plan_transitions': store.query('SELECT * FROM plan_transitions WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'workflow_runs': store.query('SELECT * FROM workflow_runs WHERE case_id=? ORDER BY at',(case_id,)) if case_id else [],
            'contact_enrichment': {table:store.query(f'SELECT * FROM {table} WHERE case_id=?',(case_id,)) for table in ('contact_observations','contact_decisions')} if case_id else {},
            'case_id': case_id, 'conversation_id': conversation_id,
            'runtime': {'configured_model': config.OLLAMA_MODEL, 'prefer_llm': get_runtime().prefer_llm,
                        'inference_workers': 1, 'source': 'persisted observable events; no hidden reasoning'},
            'case': case, 'conversation': state, 'events': events, 'checklist': checklist,
            'retrieved_sources': sources,
            'messages': store.messages(case_id) if case_id else [],
            'operational_records': {table: store.query(f'SELECT * FROM {table} WHERE case_id=?', (case_id,))
                for table in ('erp_calls', 'rmas', 'sim_shipments', 'approvals', 'qa_notifications')} if case_id else {},
            'feedback_proposals': [p for p in store.query("SELECT * FROM improvement_proposals WHERE kind='representative_feedback'")
                if decoded(p['evidence']).get('case_id') == case_id] if case_id else [],
            'legacy_evidence_note': 'Cases created before activity logging retain their original checklist/audit evidence; missing model-call events are not invented.'}


@router.get('/{kind}/{subject_id}')
def read(kind: str, subject_id: str):
    return bundle(kind, subject_id)


@router.post('/{kind}/{subject_id}/export')
def export(kind: str, subject_id: str):
    data = bundle(kind, subject_id)
    directory = Path(config.DATA_DIR).parent / 'docs/evidence/agent_traces'
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{subject_id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    json_path, md_path = directory / (stem + '.json'), directory / (stem + '.md')
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    lines = [f"# Agent activity: {subject_id}", '', f"Case: {data['case_id']} · Conversation: {data['conversation_id']}",
             '', 'Recorded decisions and observable execution evidence only.', '',
             '## Runtime', '', '```json', json.dumps(data['runtime'], indent=2), '```', '', '## Timeline', '']
    for event in data['events']:
        lines += [f"### {event['at']} · {event['category']} · {event['status']}", '',
                  event['summary'], '', '```json', json.dumps(event.get('evidence'), indent=2, ensure_ascii=False), '```', '']
    lines += ['## Complete supporting records', '', '```json', json.dumps({k:v for k,v in data.items() if k != 'events'}, indent=2, ensure_ascii=False), '```']
    md_path.write_text('\n'.join(lines), encoding='utf-8')
    return {'json_path': str(json_path), 'markdown_path': str(md_path), 'trace': data}
