"""Durable, privacy-minimized Agent Arena telemetry (contract v1)."""
from __future__ import annotations

import json
import hashlib
import sqlite3,time
from .coordination import timestamp, uid
from . import runtime_identity

SCHEMA_VERSION = "1.0"
SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_registry (
 agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
 color TEXT NOT NULL UNIQUE, logical_home_node TEXT NOT NULL,
 retired INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, retired_at TEXT);
CREATE TABLE IF NOT EXISTS agent_capability_registry (
 agent_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
 capabilities TEXT NOT NULL, required_tools TEXT NOT NULL,
 eligible_model_categories TEXT NOT NULL, execution_constraints TEXT NOT NULL,
 operational_state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arena_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 schema_version TEXT NOT NULL, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
 case_id TEXT, workflow_id TEXT, task_id TEXT, agent_id TEXT,
 sender_agent_id TEXT, receiver_agent_id TEXT, correlation_id TEXT,
 logical_home_node TEXT, execution_node TEXT, tool_node TEXT, inference_node TEXT, model_id TEXT,
 endpoint_id TEXT, task_status TEXT, summary TEXT NOT NULL,
 evidence_refs TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_arena_case ON arena_events(case_id,sequence);
"""

AGENTS = (
    ("coordinator", "Coordinator", "coordination", "#55D6BE", "coordinator-192.0.2.1"),
    ("ticket_interpretation", "Interpretation", "intake", "#6EA8FE", "coordinator-192.0.2.1"),
    ("erp_research", "ERP Research", "identity", "#FFB86B", "coordinator-192.0.2.1"),
    ("qms_research", "QMS Research", "controlled-evidence", "#B38CFF", "coordinator-192.0.2.1"),
    ("safety_complaint", "Safety", "safety", "#FF6B7A", "coordinator-192.0.2.1"),
    ("documentation", "Documentation", "case-evidence", "#F4D35E", "coordinator-192.0.2.1"),
    ("resolution_planning", "Resolution Planner", "planning", "#78E08F", "coordinator-192.0.2.1"),
    ("operations", "Operations", "operations", "#52B2CF", "coordinator-192.0.2.1"),
    ("communication_review", "Communications", "communication", "#FF8EC7", "coordinator-192.0.2.1"),
    ("independent_validation", "Independent Validator", "validation", "#E9ECEF", "coordinator-192.0.2.1"),
    ("communication_correction", "Correction", "artifact-rework", "#FFA94D", "coordinator-192.0.2.1"),
    ("reviewed_learning", "Reviewed Learning", "reviewed-recall", "#8CE3FF", "coordinator-192.0.2.1"),
)


def ensure_registry(store):
    for agent_id, name, role, color, home in AGENTS:
        home=runtime_identity.coordinator_id()
        if not store.one("SELECT 1 FROM agent_registry WHERE agent_id=?", (agent_id,)):
            store.insert("agent_registry", dict(agent_id=agent_id, display_name=name,
                role=role, color=color, logical_home_node=home, retired=0,
                created_at=timestamp(), retired_at=None))
            store.insert("arena_events", dict(event_id=uid("ARENA"),schema_version=SCHEMA_VERSION,
                timestamp=timestamp(),event_type="agent.registered",case_id=None,workflow_id=None,
                task_id=None,agent_id=agent_id,sender_agent_id=None,receiver_agent_id=None,
                correlation_id=agent_id,logical_home_node=home,execution_node=runtime_identity.coordinator_id(),tool_node=None,
                inference_node=None,model_id=None,endpoint_id=None,task_status="registered",
                summary=f"{name} registered",evidence_refs="[]",detail=json.dumps({'role':role,**runtime_identity.identity()})))


def register(store, agent_id, display_name, role, logical_home_node=None,
             *, capabilities=(), required_tools=(), eligible_model_categories=(),
             execution_constraints=None):
    """Register or reinstate any logical agent without imposing a population cap."""
    logical_home_node=logical_home_node or runtime_identity.coordinator_id()
    ensure_registry(store)
    existing=store.one("SELECT * FROM agent_registry WHERE agent_id=?",(agent_id,))
    if existing:
        if existing['retired']:
            store.update('agent_registry','agent_id',agent_id,{'retired':0,'retired_at':None,
                'display_name':display_name or existing['display_name'],'role':role or existing['role'],
                'logical_home_node':logical_home_node or existing['logical_home_node']})
            emit(store,'agent.reinstated',agent_id=agent_id,correlation_id=agent_id,
                 logical_home_node=logical_home_node or existing['logical_home_node'],
                 task_status='registered',summary=f"{display_name or existing['display_name']} reinstated")
        _capabilities(store, agent_id, capabilities or (role,), required_tools,
                      eligible_model_categories, execution_constraints or {}, 'active')
        return store.one("SELECT * FROM agent_registry WHERE agent_id=?",(agent_id,))
    salt=0
    while True:
        digest=hashlib.sha256(f"{agent_id}:{salt}".encode()).hexdigest()
        hue=int(digest[:8],16)%360;color=f"hsl({hue} 65% 67%)"
        if not store.one("SELECT 1 FROM agent_registry WHERE color=?",(color,)):break
        salt+=1
    store.insert('agent_registry',dict(agent_id=agent_id,display_name=display_name,
        role=role,color=color,logical_home_node=logical_home_node,retired=0,
        created_at=timestamp(),retired_at=None))
    emit(store,'agent.registered',agent_id=agent_id,correlation_id=agent_id,
         logical_home_node=logical_home_node,task_status='registered',
         summary=f"{display_name} registered",detail={'role':role})
    _capabilities(store, agent_id, capabilities or (role,), required_tools,
                  eligible_model_categories, execution_constraints or {}, 'active')
    return store.one("SELECT * FROM agent_registry WHERE agent_id=?",(agent_id,))


def _capabilities(store, agent_id, capabilities, required_tools, model_categories,
                  constraints, state):
    store.upsert('agent_capability_registry', {
        'agent_id': agent_id, 'schema_version': '1.0',
        'capabilities': json.dumps(sorted(set(capabilities))),
        'required_tools': json.dumps(sorted(set(required_tools))),
        'eligible_model_categories': json.dumps(sorted(set(model_categories))),
        'execution_constraints': json.dumps(constraints, sort_keys=True),
        'operational_state': state, 'updated_at': timestamp()}, 'agent_id')


def retire(store, agent_id):
    """Retire without deleting or recycling the historical identity/color."""
    row=store.one("SELECT * FROM agent_registry WHERE agent_id=?",(agent_id,))
    if not row or row['retired']:
        return row
    store.update('agent_registry','agent_id',agent_id,{'retired':1,'retired_at':timestamp()})
    extension=store.one('SELECT * FROM agent_capability_registry WHERE agent_id=?',(agent_id,))
    if extension:
        store.update('agent_capability_registry','agent_id',agent_id,
                     {'operational_state':'retired','updated_at':timestamp()})
    return emit(store,'agent.retired',agent_id=agent_id,correlation_id=agent_id,
                logical_home_node=row['logical_home_node'],task_status='retired',
                summary=f"{row['display_name']} retired")


def emit(store, event_type, *, case_id=None, workflow_id=None, task_id=None,
         agent_id=None, sender_agent_id=None, receiver_agent_id=None,
         correlation_id=None, logical_home_node=None,
         execution_node=None, tool_node=None, inference_node=None,
         model_id=None, endpoint_id=None, task_status=None, summary="",
         evidence_refs=(), detail=None, event_id=None):
    """Append one observable event. Detail must not contain raw customer data."""
    ensure_registry(store)
    logical_home_node=logical_home_node or runtime_identity.coordinator_id()
    execution_node=execution_node or runtime_identity.coordinator_id()
    metadata=dict(detail or {})
    for key,value in runtime_identity.identity().items():metadata.setdefault(key,value)
    metadata.setdefault('event_contract','capstone.activity/1.1')
    row = dict(event_id=event_id or uid("ARENA"), schema_version=SCHEMA_VERSION,
        timestamp=timestamp(), event_type=event_type, case_id=case_id,
        workflow_id=workflow_id, task_id=task_id, agent_id=agent_id,
        sender_agent_id=sender_agent_id, receiver_agent_id=receiver_agent_id,
        correlation_id=correlation_id, logical_home_node=logical_home_node,
        execution_node=execution_node, tool_node=tool_node, inference_node=inference_node,
        model_id=model_id, endpoint_id=endpoint_id, task_status=task_status,
        summary=str(summary)[:240], evidence_refs=json.dumps(list(evidence_refs)),
        detail=json.dumps(metadata, sort_keys=True, default=str))
    store.insert("arena_events", row)
    mirror=getattr(store,'arena_mirror_database',None)
    mirror_file=getattr(store,'arena_mirror_file',None)
    if mirror or mirror_file:
        mirrored=dict(row);mirrored['event_id']=uid('ARENA')
        mirrored['workflow_id']=getattr(store,'arena_experiment_run_id',None) or workflow_id
        mirrored['summary']=('EXPERIMENT '+str(summary))[:240]
    if mirror_file:
        # Testing Lab workers: append locally; the coordinator imports the
        # whole file into the live database in one transaction at run end.
        with open(mirror_file,'a',encoding='utf-8') as stream:stream.write(json.dumps(mirrored,sort_keys=True)+'\n')
    elif mirror:
        columns=list(mirrored);sql='INSERT INTO arena_events ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
        last=None
        for attempt in range(6):
            try:
                with sqlite3.connect(mirror,timeout=5) as db:
                    db.execute('PRAGMA busy_timeout=5000');db.execute(sql,[mirrored[k] for k in columns])
                last=None;break
            except sqlite3.OperationalError as exc:
                last=exc
                if 'locked' not in str(exc).lower():raise
                time.sleep(.05*(attempt+1))
        if last:raise last
    return store.one("SELECT * FROM arena_events WHERE event_id=?", (row["event_id"],))


class MirrorTail:
    """Reads only complete new lines of a worker's mirror file (live streaming).

    The worker appends one JSON line per event; a line still being written
    has no trailing newline yet and is left for the next read.
    """
    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path); self.offset = 0

    def read_new(self):
        if not self.path.exists():
            return []
        with open(self.path, 'rb') as stream:
            stream.seek(self.offset); data = stream.read()
        end = data.rfind(b'\n')
        if end < 0:
            return []
        self.offset += end + 1
        return [json.loads(line) for line in data[:end + 1].decode('utf-8').splitlines() if line.strip()]


def import_rows(rows, database):
    """Insert mirrored events (idempotent on event_id). Returns rows given."""
    if not rows:
        return 0
    groups = {}
    for row in rows:groups.setdefault(tuple(row),[]).append(row)
    for attempt in range(10):
        try:
            with sqlite3.connect(database,timeout=30) as db:
                db.execute('PRAGMA busy_timeout=30000')
                for columns,items in groups.items():
                    sql='INSERT OR IGNORE INTO arena_events ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
                    db.executemany(sql,[[item[key] for key in columns] for item in items])
            break
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt==9:raise
            time.sleep(.2*(attempt+1))
    return len(rows)


def import_mirror(path, database):
    """Import a worker's mirrored events into ``database`` in one transaction.

    Returns the number of rows written. The file is renamed afterwards so a
    resumed run never imports the same events twice.
    """
    from pathlib import Path
    path=Path(path)
    if not path.exists():return 0
    rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    groups={}
    for row in rows:groups.setdefault(tuple(row),[]).append(row)
    for attempt in range(10):
        try:
            with sqlite3.connect(database,timeout=30) as db:
                db.execute('PRAGMA busy_timeout=30000')
                for columns,items in groups.items():
                    sql='INSERT OR IGNORE INTO arena_events ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')'
                    db.executemany(sql,[[item[key] for key in columns] for item in items])
            break
        except sqlite3.OperationalError as exc:
            if 'locked' not in str(exc).lower() or attempt==9:raise
            time.sleep(.2*(attempt+1))
    path.rename(path.with_name(f'{path.stem}.imported-{int(time.time()*1000)}.jsonl'))
    return len(rows)


def replay(store, after_sequence=0, limit=250):
    limit = max(1, min(1000, int(limit)))
    rows = store.query("SELECT * FROM arena_events WHERE sequence>? ORDER BY sequence LIMIT ?",
                       (max(0, int(after_sequence)), limit))
    for row in rows:
        row["evidence_refs"] = json.loads(row["evidence_refs"])
        row["detail"] = json.loads(row["detail"])
    return rows


def registry(store):
    ensure_registry(store)
    rows=store.query("SELECT * FROM agent_registry ORDER BY created_at,agent_id")
    for row in rows:
        ext=store.one('SELECT * FROM agent_capability_registry WHERE agent_id=?',(row['agent_id'],))
        if ext:
            for key in ('capabilities','required_tools','eligible_model_categories','execution_constraints'):
                row[key]=json.loads(ext[key])
            row['operational_state']=ext['operational_state']
        else:
            row.update(capabilities=[row['role']],required_tools=[],eligible_model_categories=[],
                       execution_constraints={},operational_state='retired' if row['retired'] else 'active')
        try:
            row['current_tasks']=[task['task_id'] for task in store.query(
                "SELECT task_id FROM execution_tasks WHERE logical_agent_id=? "
                "AND state IN ('assigned','running','waiting_for_tool','waiting_for_inference')",
                (row['agent_id'],))]
        except Exception:
            row['current_tasks']=[]
    return rows


def resource_status():
    return {name: {"available": False, "status": "not_instrumented", "value": None}
            for name in ("cpu", "ram", "gpu")}


def phase6_fixture():
    """Sanitized representative events for the independent Arena adapter."""
    base={key:None for key in ('case_id','workflow_id','task_id','agent_id',
        'sender_agent_id','receiver_agent_id','correlation_id','logical_home_node',
        'execution_node','tool_node','inference_node','model_id','endpoint_id','task_status')}
    def event(sequence,event_type,status,summary,detail,**fields):
        return {**base,'sequence':sequence,'event_id':f'ARENA-FIXTURE-{sequence:02d}',
            'schema_version':SCHEMA_VERSION,'timestamp':'2026-11-04T12:00:00Z',
            'event_type':event_type,'task_status':status,'summary':summary,
            'evidence_refs':[],'detail':{'fixture':True,'contains_customer_data':False,**detail},**fields}
    events=[
        event(1,'experiment.campaign.started','running','Testing Lab campaign started',
            {'campaign_id':'EXP-FIXTURE','planned_runs':6,'synthetic':True},
            workflow_id='EXP-FIXTURE',agent_id='coordinator',correlation_id='EXP-FIXTURE'),
        event(2,'experiment.candidate.started','running','Candidate execution started',
            {'campaign_id':'EXP-FIXTURE','trial_id':'RUN-FIXTURE','scenario_id':'holdout-fixture',
             'candidate_architecture_id':'candidate-c-tot','candidate_configuration_hash':'sha256-fixture',
             'synthetic':True},workflow_id='GRAPH-FIXTURE',task_id='RUN-FIXTURE',
            agent_id='coordinator',correlation_id='RUN-FIXTURE'),
        event(3,'inference.queued','queued','Planning inference queued',
            {'campaign_id':'EXP-FIXTURE','trial_id':'RUN-FIXTURE','selected_model':'model-request-fixture',
             'actual_execution_not_yet_known':True},workflow_id='GRAPH-FIXTURE',task_id='TASK-FIXTURE',
            agent_id='resolution_planning',model_id='model-request-fixture',correlation_id='CALL-FIXTURE'),
        event(4,'inference.completed','completed','Gateway inference completed',
            {'gateway_request_id':'GW-FIXTURE','actual_model':'model-observed-fixture',
             'resource_id':'qualified-resource-fixture','physical_inference':True},
            workflow_id='GRAPH-FIXTURE',task_id='TASK-FIXTURE',agent_id='resolution_planning',
            model_id='model-observed-fixture',inference_node='qualified-resource-fixture',
            endpoint_id='qualified-resource-fixture',correlation_id='CALL-FIXTURE'),
        event(5,'experiment.safety_evaluation.completed','completed','Experimental safety gate passed',
            {'campaign_id':'EXP-FIXTURE','trial_id':'RUN-FIXTURE','critical_violation_count':0,
             'synthetic':True},workflow_id='EXP-FIXTURE',task_id='RUN-FIXTURE',
            agent_id='independent_validation',correlation_id='RUN-FIXTURE'),
        event(6,'research.iteration.dispatched','running','Approved bounded follow-up dispatched',
            {'research_iteration_id':'ITER-FIXTURE','approval_hash':'approval-fixture',
             'operational_activation_permitted':False,'synthetic':True},
            workflow_id='EXP-FOLLOWUP',agent_id='coordinator',correlation_id='ITER-FIXTURE'),
        event(7,'experiment.trial.failed','failed','Testing Lab trial interrupted',
            {'campaign_id':'EXP-FIXTURE','trial_id':'RUN-INTERRUPTED','error_classification':'worker_failure',
             'completion_inferred':False,'synthetic':True},workflow_id='EXP-FIXTURE',
            task_id='RUN-INTERRUPTED',agent_id='coordinator',correlation_id='RUN-INTERRUPTED')]
    return {'schema_version':SCHEMA_VERSION,'fixture_only':True,'events':events,
        'note':'Representative sanitized adapter input. The physical inference event is schema illustration, not measured Capstone evidence.'}
