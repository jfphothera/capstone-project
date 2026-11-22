"""Host-side sanitized observation export; never grants capacity or mutates state.

Run as the infrastructure owner, publishing stdout through an independently
protected file/transport. Arena must never receive access to the private ledger.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import time

CONTRACT = 'shared.inference.observation/1'
WINDOW_SECONDS = 3600
FAILURE_ROWS = 50   # cap on last_hour.invocation_failures rows; the rest are counted, not listed


def _open(database):
    # URI mode=ro prevents both schema initialization and accidental file creation.
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def _ledger_id(db):
    row=db.execute("SELECT value FROM capacity_service_policy WHERE key='ledger_id'").fetchone()
    return row[0] if row else None


def ledger_identity(database):
    """Ledger identity and newest event ID, from one read-only snapshot."""
    db=_open(database)
    try:
        db.execute('BEGIN')
        return {'ledger_id':_ledger_id(db),'max_event_id':db.execute('SELECT coalesce(max(event_id),0) FROM capacity_events').fetchone()[0]}
    finally:
        db.close()


def _invocation_failures(db, since, attempts):
    """0.4.5, additive: last-hour failed invocations grouped by head, model, application and failure class.

    The head is the one the last attempt ran on (null: the request never reached a head). The class is
    recovery's (deadline_exceeded, uncertain_execution, head_unreachable, model_on_head, request_fault;
    null when unclassified). The private error string is never exported. At most FAILURE_ROWS rows,
    largest first; failures in rows beyond the cap are counted in invocation_failures_omitted."""
    last='FROM capacity_request_attempts a WHERE a.request_id=i.request_id ORDER BY a.attempt DESC LIMIT 1'
    head=f'coalesce((SELECT a.resource_id {last}),r.resource_id)' if attempts else 'r.resource_id'
    kind=f'(SELECT a.failure_class {last})' if attempts else 'NULL'
    rows=[dict(r) for r in db.execute(f'''SELECT {head} resource_id,r.model model,i.app_id app_id,{kind} class,count(*) count,max(i.updated) last_at
        FROM capacity_invocations i LEFT JOIN capacity_requests r ON r.request_id=i.request_id
        WHERE i.state='failed' AND i.updated>? GROUP BY 1,2,3,4''',(since,))]
    rows.sort(key=lambda r:(-r['count'],-r['last_at'],r['resource_id'] or '',r['model'] or '',r['app_id'],r['class'] or ''))
    return {'invocation_failures':rows[:FAILURE_ROWS],'invocation_failures_omitted':sum(r['count'] for r in rows[FAILURE_ROWS:])}


def snapshot(database, *, after_event_id=0, limit=100, service_id=None):
    if not isinstance(after_event_id,int) or after_event_id<0 or not isinstance(limit,int) or not 1<=limit<=1000:
        raise ValueError('Nonnegative event cursor and limit 1–1000 required')
    db=_open(database)
    try:
        db.execute('BEGIN')
        observed=time.time()
        clients=[dict(r) for r in db.execute('SELECT app_id,enabled,max_concurrency,queue_limit,priority FROM capacity_clients ORDER BY app_id')]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        v04={'capacity_resource_capacity','capacity_inventory_runtime','capacity_suspensions'}<=tables
        capacity={r['resource_id']:dict(r) for r in db.execute('SELECT resource_id,slots,memory_gb FROM capacity_resource_capacity')} if v04 else {}
        runtime={r['resource_id']:dict(r) for r in db.execute('SELECT resource_id,loaded,loaded_at FROM capacity_inventory_runtime')} if v04 else {}
        in_use={r['resource_id']:r['n'] for r in db.execute("SELECT resource_id,count(*) n FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND resource_id IS NOT NULL GROUP BY resource_id")}
        from . import model_capacity
        from .recovery import load_policy
        evidence=model_capacity.load(db);placement=load_policy(db)['placement'] if v04 else None
        resources=[]
        for row in db.execute('SELECT resource_id,resource_type,qualified_models,health,last_seen FROM capacity_resources ORDER BY resource_id').fetchall():
            item=dict(row)
            item['qualified_models']=json.loads(item['qualified_models'])
            item['fresh']=item['last_seen']>observed-120
            # 0.4.0, additive: slot use and what is loaded (names, digests and sizes as /api/ps reports them).
            cap=capacity.get(row['resource_id'],{});run=runtime.get(row['resource_id'],{})
            loaded=json.loads(run['loaded']) if run.get('loaded') else None
            item['capacity']={'slots':cap.get('slots',1),'slots_in_use':in_use.get(row['resource_id'],0),'memory_gb':cap.get('memory_gb'),
                'loaded_models':[{'name':m.get('name'),'digest':m.get('digest'),'size_bytes':m.get('size')} for m in loaded] if loaded is not None else None,
                'loaded_observed_at':run.get('loaded_at'),'loaded_fresh':bool(run.get('loaded_at') and run['loaded_at']>observed-30)}
            if v04:
                # 0.4.4, additive: per qualified model, the leases of that model the head takes at once and why.
                item['capacity']['model_limits']=[{k:l[k] for k in ('model','limit','source','reason','runtime_version','digest','declared','measured')}
                    for l in model_capacity.head_limits(db,row['resource_id'],item['qualified_models'],placement,evidence=evidence)]
            resources.append(item)
        suspensions=[];alerts=[]
        if v04:
            from .recovery import view,alerts as alert_items
            rows=[dict(r) for r in db.execute("SELECT * FROM capacity_suspensions WHERE state IN ('suspended','held','needs_attention') ORDER BY since,suspension_id")]
            suspensions=[view(r) for r in rows];alerts=alert_items(rows)
        requests={r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM capacity_requests GROUP BY status')}
        invocations={r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM capacity_invocations GROUP BY state')}
        leases=[dict(r) for r in db.execute("SELECT request_id,app_id,resource_id,lease_id,status,expires,deadline,executing FROM capacity_requests WHERE status IN ('leased','cancel_requested') ORDER BY request_id")]
        events=[dict(r) for r in db.execute('SELECT event_id,app_id,request_id,event_type,at FROM capacity_events WHERE event_id>? ORDER BY event_id LIMIT ?',(after_event_id,limit))]
        opened=db.execute("SELECT value FROM capacity_service_policy WHERE key='admission_open'").fetchone()[0]=='1'
        # 0.4.1, additive: listener and scheduler load counters (503 answers, renewal failures) since service start.
        counters=db.execute("SELECT value FROM capacity_service_policy WHERE key='gateway_counters'").fetchone()
        counters=json.loads(counters[0]) if counters else None
        qualifications=[dict(r) for r in db.execute('SELECT evidence_id,resource_id,model,digest,passed,at FROM capacity_qualification_evidence ORDER BY at DESC LIMIT ?',(limit,))]
        inventory=[]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_inventory'").fetchone():
            # Installed models as the head reports them; endpoints are never exported.
            for row in db.execute('SELECT resource_id,observed_at,runtime_version,models FROM capacity_inventory ORDER BY resource_id'):
                runtime=json.loads(row['runtime_version']) if row['runtime_version'] else {}
                inventory.append({'resource_id':row['resource_id'],'observed_at':row['observed_at'],'fresh':row['observed_at']>observed-120,
                    'runtime_version':runtime.get('version') if isinstance(runtime,dict) else None,
                    'models':[{'name':name,'digest':digest} for name,digest in sorted(json.loads(row['models']).items())]})
        since=observed-WINDOW_SECONDS
        # Counts only, over the same allowlisted columns as the lifetime totals.
        last_hour={'window_seconds':WINDOW_SECONDS,
            'requests_created':{r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM capacity_requests WHERE created>? GROUP BY status',(since,))},
            'invocations_updated':{r['state']:r['n'] for r in db.execute('SELECT state,count(*) n FROM capacity_invocations WHERE updated>? GROUP BY state',(since,))},
            'events':{r['event_type']:r['n'] for r in db.execute('SELECT event_type,count(*) n FROM capacity_events WHERE at>? GROUP BY event_type',(since,))}}
        last_hour.update(_invocation_failures(db,since,'capacity_request_attempts' in tables))
        if 'capacity_upstream_errors' in tables:
            # 0.4.6, additive: HTTP error replies from heads, counted by head, model, operation and status. Bodies and
            # messages are never exported (they stay in the protected ledger table).
            last_hour['upstream_http_errors']=[dict(r) for r in db.execute(
                'SELECT resource_id,model,operation,status,count(*) count,max(at) last_at FROM capacity_upstream_errors WHERE at>? '
                'GROUP BY 1,2,3,4 ORDER BY count DESC,last_at DESC LIMIT ?',(since,FAILURE_ROWS))]
        return {'contract':CONTRACT,'observed_at':observed,'service_id':service_id,'ledger_id':_ledger_id(db),
            'admission_open':opened,'applications':clients,
            'resources':resources,'requests':requests,'invocations':invocations,'leases':leases,
            'qualification_observations':qualifications,'events':events,
            'next_event_id':events[-1]['event_id'] if events else after_event_id,'last_hour':last_hour,'inventory':inventory,
            'suspensions':suspensions,'alerts':alerts,'gateway_load':counters,
            'scope':'snapshot only; enabled is authorization, not application liveness; stale leases are not reconciled by observer'}
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True)
    parser.add_argument('--after-event-id',type=int,default=0)
    parser.add_argument('--limit',type=int,default=100)
    parser.add_argument('--service-id')
    args=parser.parse_args()
    print(json.dumps(snapshot(args.database,after_event_id=args.after_event_id,limit=args.limit,service_id=args.service_id)))


if __name__=='__main__':main()
