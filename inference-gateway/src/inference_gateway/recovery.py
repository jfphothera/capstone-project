"""Failure classes, suspensions, holds and automatic recovery (release 0.4.0).

Every failure of a dispatched call is put in exactly one class, and only that
class's action is taken (docs/FAILURES-AND-PLACEMENT.md):

  request_fault        the head answered 4xx, or ClientPolicyError: fail the request only
  model_on_head        the head answered 5xx, or a reply failed validation: suspend this
                       model on this head for this operation; re-place the request once
  head_unreachable     no connection before the request was sent: mark the head offline
                       (heartbeat brings it back); re-place the request once
  uncertain_execution  timeout/reset/truncation after the request was sent, or an
                       executing lease expiring unanswered: hold the whole head; never replay
  deadline_exceeded    0.4.3: the call reached its own deadline and the head still answers
                       (/api/version within failure_policy.deadline_liveness_seconds): fail
                       that request only; no hold, no fault, no replay

Suspensions and holds are then recovered automatically: wait until safe, probe with
the qualification test, release on a pass, back off and finally mark
needs_attention. Nothing here deletes history; every transition is evented.
"""
import http.client
import json
import time
import urllib.error
import uuid

REQUEST_FAULT='request_fault'
MODEL_ON_HEAD='model_on_head'
HEAD_UNREACHABLE='head_unreachable'
UNCERTAIN='uncertain_execution'
DEADLINE='deadline_exceeded'
CLASSES=(REQUEST_FAULT,MODEL_ON_HEAD,HEAD_UNREACHABLE,UNCERTAIN,DEADLINE)
STRATEGIES=('least_busy','loaded_first')

# Suspension scopes: one model on one head, a whole head (hold), one model on every head (cascade).
RESOURCE_MODEL,RESOURCE,MODEL='resource_model','resource','model'
ACTIVE=('suspended','held','needs_attention')

DEFAULTS={
    'failure_policy':{
        'probe_backoff_seconds':[60,300,900],   # wait before probe 1, 2, 3; after the last failure: needs_attention
        'cascade_window_seconds':600,           # same model+digest+operation failing on N heads within this window
        'cascade_heads':2,                      # ... suspends it on every head
        'ps_settle_seconds':2,                  # held head: two /api/ps reads this far apart must both list no loaded model
        'safe_recheck_seconds':15,              # held head not yet safe: look again after this long
        'hold_max_wait_seconds':1800,           # held head never safe to probe for this long: needs_attention
        'probe_timeout_seconds':90,
        'deadline_liveness_seconds':3},         # 0.4.3: call timed out at its deadline: ask the head /api/version this
                                                # long; it answers = deadline_exceeded (request only), 0 = always uncertain
    'placement_policy':{
        'loaded_fresh_seconds':30,              # /api/ps data older than this is "loaded unknown"
        'model_size_factor':1.2,                # /api/tags size x factor when no /api/ps size was observed
        'strategy':'least_busy',                # 0.4.3: least_busy = fewest active leases per slot first (loaded breaks
                                                # ties, evicting last); loaded_first = the 0.4.0-0.4.2 order
        'serial_families':['qwen35','qwen35moe'],  # 0.4.3: families Ollama runs one call at a time (it ignores
                                                # OLLAMA_NUM_PARALLEL for them): at most 1 lease of such a model per head
        'model_parallel':{},                    # 0.4.3: {model name: max leases of that model per head}; wins over the family
        'parallel_runtime_min':'0.40.15',       # 0.4.4: serial_families force 1 only on runtimes below this (Ollama #19545)
        'serial_families_need_evidence':True,   # 0.4.4: ... and on newer runtimes until a declared/measured value is current
        'unmeasured_model_limit':0}}            # 0.4.5: any other model without a current declared/measured value on a
                                                # head: at most this many leases there; 0 = the head's slots (0.4.4).
                                                # The lab sets 1 (2026-11-18: an unmeasured reviewer ran 7-8 at once).
BOUNDS={
    'failure_policy':{'cascade_window_seconds':(60,86400),'cascade_heads':(2,64),'ps_settle_seconds':(0,30),
                      'safe_recheck_seconds':(1,600),'hold_max_wait_seconds':(60,86400),'probe_timeout_seconds':(5,300),
                      'deadline_liveness_seconds':(0,30)},
    'placement_policy':{'loaded_fresh_seconds':(5,600),'model_size_factor':(1.0,4.0)}}


NETWORK=(urllib.error.URLError,ConnectionError,TimeoutError,http.client.HTTPException)


class InvalidReply(ValueError):
    """The head answered, but the answer is not a valid model response (model-on-head fault)."""


# ---------------------------------------------------------------- policy

def validate_policy(block,values):
    """Validated copy of one policy block over its defaults; unknown keys and bad ranges are refused."""
    if values is None:return dict(DEFAULTS[block])
    if not isinstance(values,dict):raise ValueError(block+' must be an object')
    unknown=set(values)-set(DEFAULTS[block])
    if unknown:raise ValueError('%s: unknown key(s) %s'%(block,', '.join(sorted(unknown))))
    out={**DEFAULTS[block],**values}
    for key,(low,high) in BOUNDS[block].items():
        value=out[key]
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not low<=value<=high:
            raise ValueError('%s.%s must be %s-%s'%(block,key,low,high))
    if block=='failure_policy':
        backoff=out['probe_backoff_seconds']
        if (not isinstance(backoff,list) or not 1<=len(backoff)<=10
                or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not 5<=v<=86400 for v in backoff)):
            raise ValueError('failure_policy.probe_backoff_seconds must list 1-10 waits of 5-86400 s')
    if block=='placement_policy':
        if out['strategy'] not in STRATEGIES:raise ValueError('placement_policy.strategy must be one of '+', '.join(STRATEGIES))
        families=out['serial_families']
        if not isinstance(families,list) or len(families)>64 or any(not isinstance(f,str) or not 0<len(f)<=100 for f in families):
            raise ValueError('placement_policy.serial_families must list up to 64 family names')
        limits=out['model_parallel']
        if (not isinstance(limits,dict) or len(limits)>256 or any(not isinstance(k,str) or not 0<len(k)<=200 for k in limits)
                or any(isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=64 for v in limits.values())):
            raise ValueError('placement_policy.model_parallel must map up to 256 model names to 1-64')
        out['serial_families']=list(families);out['model_parallel']=dict(limits)
        from .model_capacity import version_tuple
        if version_tuple(out['parallel_runtime_min']) is None:raise ValueError('placement_policy.parallel_runtime_min must be a version like "0.40.15"')
        if not isinstance(out['serial_families_need_evidence'],bool):raise ValueError('placement_policy.serial_families_need_evidence must be true or false')
        default=out['unmeasured_model_limit']
        if isinstance(default,bool) or not isinstance(default,int) or not 0<=default<=64:
            raise ValueError('placement_policy.unmeasured_model_limit must be 0-64 (0 = the head\'s slots)')
    return out


def load_policy(db):
    """Effective policy from the ledger (set by bootstrap), defaults when absent."""
    out={}
    for block in DEFAULTS:
        row=db.execute('SELECT value FROM capacity_service_policy WHERE key=?',(block,)).fetchone()
        out[block.split('_')[0]]=validate_policy(block,json.loads(row[0]) if row else None)
    return out


def store_policy(db,block,values):
    db.execute('INSERT INTO capacity_service_policy VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (block,json.dumps(validate_policy(block,values),sort_keys=True)))


# ---------------------------------------------------------------- classification

def classify(exc,phase):
    """One failure class for an exception raised in a phase of one execution attempt.

    phase: 'admission' (no head yet), 'checks' (leased, read-only pre-dispatch calls),
    'dispatch' (the inference call was being sent or awaited), 'answered' (a reply came
    back). None means no head or model fault: the request fails as in 0.3.0.
    urllib wraps every error raised while connecting or sending in URLError; errors
    after the request was written (waiting for or reading the reply) are raised raw.
    HTTPError is a URLError and an OSError, so it is tested first.
    """
    from .capacity_gateway import ClientPolicyError
    if isinstance(exc,ClientPolicyError):return REQUEST_FAULT
    from .capacity_broker import LedgerBusy
    if isinstance(exc,LedgerBusy):return REQUEST_FAULT # 0.4.4: the Gateway's own ledger, never the head
    if phase=='admission':return None
    if phase=='checks':
        # PermissionError is an OSError too (revocation, version change): not a network failure.
        if isinstance(exc,urllib.error.HTTPError):return None
        return HEAD_UNREACHABLE if isinstance(exc,NETWORK) else None
    if phase=='dispatch':
        if isinstance(exc,urllib.error.HTTPError):return REQUEST_FAULT if 400<=exc.code<=499 else MODEL_ON_HEAD
        if isinstance(exc,(urllib.error.URLError,ConnectionRefusedError)):return HEAD_UNREACHABLE
        if isinstance(exc,(OSError,http.client.HTTPException)):return UNCERTAIN
        if isinstance(exc,ValueError):return MODEL_ON_HEAD # a complete reply that is not bounded JSON
        return UNCERTAIN
    if phase=='answered':return MODEL_ON_HEAD if isinstance(exc,InvalidReply) else None
    return None


def http_detail(exc):
    """Short, non-content description of a failure for private ledger rows."""
    if isinstance(exc,urllib.error.HTTPError):return 'HTTP %d'%exc.code
    return type(exc).__name__


# ---------------------------------------------------------------- suspension store

def _sid():return 'SUSP-'+uuid.uuid4().hex


def active(db,scope=None):
    sql="SELECT * FROM capacity_suspensions WHERE state IN ('suspended','held','needs_attention')"+(' AND scope=?' if scope else '')+' ORDER BY since,suspension_id'
    return [dict(r) for r in db.execute(sql,(scope,) if scope else ())]


def active_hold(db,resource_id):
    row=db.execute("SELECT * FROM capacity_suspensions WHERE scope='resource' AND resource_id=? AND state IN ('suspended','held','needs_attention')",(resource_id,)).fetchone()
    return dict(row) if row else None


def suspension_reason(rows,resource_id,model,digest,operation):
    """Reason code if this model/operation must not run on this head now, else None.

    operation None (unknown, e.g. broker-only callers) matches a suspension of any operation.
    """
    if any(s['scope']==RESOURCE and s['resource_id']==resource_id for s in rows):return 'resource_held'
    mine=[s for s in rows if s['model']==model and (operation is None or s['operation'] is None or s['operation']==operation)]
    # Every-head first, so availability reports one reason for a model suspended everywhere.
    if any(s['scope']==MODEL and (digest is None or s['digest'] is None or s['digest']==digest) for s in mine):return 'model_suspended'
    if any(s['scope']==RESOURCE_MODEL and s['resource_id']==resource_id for s in mine):return 'model_suspended_on_resource'
    return None


def hold_resource(broker,db,resource_id,*,request_id,model,operation,hold_until,source,now=None):
    """Uncertain execution: hold the whole head (health 'quarantined', as in 0.3.0).

    Never downgraded and never shortened by a later event; a second uncertain event on a
    held head only extends the wait. Returns (suspension_id, created).
    """
    now=now or time.time();policy=load_policy(db)['failure']
    db.execute("UPDATE capacity_resources SET health='quarantined' WHERE resource_id=?",(resource_id,))
    current=active_hold(db,resource_id)
    if current:
        if hold_until and (current['hold_until'] or 0)<hold_until:
            db.execute('UPDATE capacity_suspensions SET hold_until=?,next_probe=max(coalesce(next_probe,0),?) WHERE suspension_id=?',
                       (hold_until,hold_until+policy['probe_backoff_seconds'][0],current['suspension_id']))
        return current['suspension_id'],False
    sid=_sid();until=max(now,hold_until or now)
    db.execute('INSERT INTO capacity_suspensions(suspension_id,scope,resource_id,model,digest,operation,failure_class,source,state,since,hold_until,next_probe,request_id) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid,RESOURCE,resource_id,model,_digest(db,resource_id,model),operation,UNCERTAIN,source,'held',now,until,
                until+policy['probe_backoff_seconds'][0],request_id))
    broker._event(db,'infrastructure',request_id,'resource.quarantined',{'resource_id':resource_id})
    broker._event(db,'infrastructure',request_id,'resource.held',{'resource_id':resource_id,'suspension_id':sid,'source':source})
    return sid,True


def _digest(db,resource_id,model):
    if not model:return None
    row=db.execute('SELECT models FROM capacity_inventory WHERE resource_id=?',(resource_id,)).fetchone()
    return json.loads(row['models']).get(model) if row else None


def suspend_model_on_head(broker,db,resource_id,model,digest,operation,*,request_id,source,now=None):
    """Model-on-head fault: this model stops on this head for this operation; the head keeps serving.

    Also records the fault and applies the cascade rule. Returns (suspension_id, cascade_id).
    """
    now=now or time.time();policy=load_policy(db)['failure']
    digest=digest or _digest(db,resource_id,model)
    row=db.execute("SELECT suspension_id FROM capacity_suspensions WHERE scope='resource_model' AND resource_id=? AND model=? AND operation=? AND state IN ('suspended','held','needs_attention')",
                   (resource_id,model,operation)).fetchone()
    if row:sid=row[0]
    else:
        sid=_sid()
        db.execute('INSERT INTO capacity_suspensions(suspension_id,scope,resource_id,model,digest,operation,failure_class,source,state,since,next_probe,request_id) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid,RESOURCE_MODEL,resource_id,model,digest,operation,MODEL_ON_HEAD,source,'suspended',now,now+policy['probe_backoff_seconds'][0],request_id))
        broker._event(db,'infrastructure',request_id,'model.suspended',{'resource_id':resource_id,'model':model,'operation':operation,'suspension_id':sid,'scope':RESOURCE_MODEL})
    return sid,record_fault(broker,db,resource_id,model,digest,operation,request_id=request_id,now=now)


def record_fault(broker,db,resource_id,model,digest,operation,*,request_id,now):
    """Cascade rule: the same model+digest+operation failing on N different heads within the window."""
    policy=load_policy(db)['failure']
    db.execute('INSERT INTO capacity_faults(resource_id,model,digest,operation,failure_class,request_id,at) VALUES(?,?,?,?,?,?,?)',
               (resource_id,model,digest,operation,MODEL_ON_HEAD,request_id,now))
    heads={r[0] for r in db.execute("SELECT DISTINCT resource_id FROM capacity_faults WHERE model=? AND coalesce(digest,'')=coalesce(?,'') AND operation=? AND at>?",
                                    (model,digest,operation,now-policy['cascade_window_seconds']))}
    if len(heads)<policy['cascade_heads']:return None
    if db.execute("SELECT 1 FROM capacity_suspensions WHERE scope='model' AND model=? AND coalesce(digest,'')=coalesce(?,'') AND operation=? AND state IN ('suspended','held','needs_attention')",
                  (model,digest,operation)).fetchone():return None
    sid=_sid()
    db.execute('INSERT INTO capacity_suspensions(suspension_id,scope,resource_id,model,digest,operation,failure_class,source,state,since,next_probe,request_id,heads) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid,MODEL,None,model,digest,operation,MODEL_ON_HEAD,'cascade','suspended',now,now+policy['probe_backoff_seconds'][0],request_id,json.dumps(sorted(heads))))
    broker._event(db,'infrastructure',request_id,'model.suspended',{'model':model,'operation':operation,'suspension_id':sid,'scope':MODEL,'heads':sorted(heads)})
    return sid


def resolve(broker,db,suspension,*,state,by,resolution,event,request_id=None,now=None):
    now=now or time.time()
    db.execute('UPDATE capacity_suspensions SET state=?,resolved_at=?,resolved_by=?,resolution=?,next_probe=NULL,manual_probe=0 WHERE suspension_id=?',
               (state,now,by,resolution,suspension['suspension_id']))
    payload={'suspension_id':suspension['suspension_id'],'scope':suspension['scope'],'resolution':resolution}
    if suspension['resource_id']:payload['resource_id']=suspension['resource_id']
    if suspension['model']:payload['model']=suspension['model']
    broker._event(db,'infrastructure',request_id,event,payload)


def adopt_quarantined(broker,db,*,source,now=None):
    """Every quarantined head without an active hold becomes a held head (no 0.3.0 quarantine is dropped).

    Its wait ends at the latest deadline of any reservation still active on it (normally none,
    so now); the failed model is the one whose request caused the newest quarantine event.
    """
    now=now or time.time();held=[]
    for row in db.execute("SELECT resource_id FROM capacity_resources WHERE health='quarantined' ORDER BY resource_id").fetchall():
        rid=row[0]
        if active_hold(db,rid):continue
        deadline=db.execute("SELECT max(deadline) FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(rid,)).fetchone()[0]
        cause=db.execute("SELECT e.request_id,r.model FROM capacity_events e LEFT JOIN capacity_requests r ON r.request_id=e.request_id "
                         "WHERE e.event_type='resource.quarantined' AND json_extract(e.payload,'$.resource_id')=? ORDER BY e.event_id DESC LIMIT 1",(rid,)).fetchone()
        model=cause['model'] if cause else None
        operation=_operation(db,cause['request_id']) if cause else None
        sid,_=hold_resource(broker,db,rid,request_id=cause['request_id'] if cause else None,model=model,operation=operation,
                            hold_until=max(now,deadline or now),source=source,now=now)
        current=dict(db.execute('SELECT * FROM capacity_suspensions WHERE suspension_id=?',(sid,)).fetchone())
        held.append({'resource_id':rid,'suspension_id':sid,'failed_model':model,'failed_operation':operation,
                     'active_reservations':bool(deadline),'hold_until':current['hold_until'],'first_probe_not_before':current['next_probe']})
    return held


def _operation(db,request_id):
    row=db.execute('SELECT operation FROM capacity_request_routing WHERE request_id=?',(request_id,)).fetchone()
    return row[0] if row else 'completion'


# ---------------------------------------------------------------- views (observation, operator)

ALERT_REASONS={
    'probes_exhausted':'automatic probes failed %(probes_failed)d times; inspect, then release or reconcile',
    'not_safe_to_probe':'held head never became safe to probe; check that it answers and is idle',
    'model_suspended_everywhere':'model failed on %(head_count)d heads within the cascade window; suspended on every head for %(operation)s'}


def view(row):
    """Sanitized suspension view: identifiers, states and times only (no error text, no endpoint)."""
    return {'suspension_id':row['suspension_id'],'scope':row['scope'],'resource_id':row['resource_id'],'model':row['model'],
            'digest':row['digest'],'operation':row['operation'],'class':row['failure_class'],'source':row['source'],
            'since':row['since'],'hold_until':row['hold_until'],'next_probe':row['next_probe'],'probes_failed':row['probes_failed'],
            'state':row['state'],'waiting':row['waiting'],'heads':json.loads(row['heads']) if row['heads'] else None}


def alerts(rows):
    """Items that need a person: needs_attention, and every model suspended on all heads."""
    out=[]
    for row in rows:
        if row['state']=='needs_attention':
            code='not_safe_to_probe' if row['waiting'] else 'probes_exhausted'
        elif row['scope']==MODEL:code='model_suspended_everywhere'
        else:continue
        heads=json.loads(row['heads']) if row['heads'] else []
        out.append({'alert_id':'ALERT-'+row['suspension_id'][5:],'suspension_id':row['suspension_id'],'kind':'needs_attention' if row['state']=='needs_attention' else 'model.suspended',
                    'scope':row['scope'],'resource_id':row['resource_id'],'model':row['model'],'operation':row['operation'],'since':row['since'],
                    'reason_code':code,'reason':ALERT_REASONS[code]%{'probes_failed':row['probes_failed'],'head_count':len(heads),'operation':row['operation']}})
    return out


# ---------------------------------------------------------------- operator commands

def list_suspensions(broker,*,include_resolved=False):
    with broker.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM capacity_suspensions'+('' if include_resolved else " WHERE state IN ('suspended','held','needs_attention')")+' ORDER BY since,suspension_id')]
        probes={}
        for row in db.execute('SELECT suspension_id,probe_id,passed,detail,at FROM capacity_probes ORDER BY at'):
            probes[row['suspension_id']]={'probe_id':row['probe_id'],'passed':bool(row['passed']),'detail':row['detail'],'at':row['at']}
    items=[]
    for row in rows:
        item=view(row);item['last_probe']=probes.get(row['suspension_id'])
        if include_resolved:item.update(resolved_at=row['resolved_at'],resolved_by=row['resolved_by'],resolution=row['resolution'])
        items.append(item)
    return {'suspensions':items,'alerts':alerts([r for r in rows if r['state'] in ACTIVE]),'at':time.time()}


def _one(db,suspension_id):
    row=db.execute('SELECT * FROM capacity_suspensions WHERE suspension_id=?',(suspension_id,)).fetchone()
    if not row:raise ValueError('Unknown suspension '+str(suspension_id))
    return dict(row)


def request_probe(broker,suspension_id,operator):
    """Ask the running service to probe now (it does so within one recovery interval)."""
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    with broker.write('request_probe') as db:
        row=_one(db,suspension_id)
        if row['state'] not in ACTIVE:raise ValueError('Suspension is already '+row['state'])
        now=time.time()
        db.execute('UPDATE capacity_suspensions SET next_probe=?,manual_probe=1 WHERE suspension_id=?',(now,suspension_id))
        broker._event(db,'infrastructure',None,'probe.requested',{'suspension_id':suspension_id,'operator':operator})
    return now


def probe_now(broker,suspension_id,operator,*,wait_seconds=120,poll=0.5):
    requested=request_probe(broker,suspension_id,operator);deadline=time.monotonic()+wait_seconds;probe=None
    while True:
        with broker.connect() as db:
            row=_one(db,suspension_id)
            found=db.execute('SELECT probe_id,passed,detail,at FROM capacity_probes WHERE suspension_id=? AND at>=? ORDER BY at DESC LIMIT 1',(suspension_id,requested)).fetchone()
        if found:probe={'probe_id':found['probe_id'],'passed':bool(found['passed']),'detail':found['detail'],'at':found['at']}
        if probe or row['state'] not in ACTIVE or time.monotonic()>=deadline:break
        time.sleep(poll)
    item=view(row)
    return {'requested_at':requested,'suspension':item,'probe':probe,'pending':probe is None and row['state'] in ACTIVE,
            'note':None if probe or row['state'] not in ACTIVE else
                   ('waiting: '+row['waiting'] if row['waiting'] else 'no probe yet: is the InferenceGateway service running?')}


def release(broker,suspension_id,operator,*,confirmed):
    """Manual override. A held head must have no active reservation (as reconcile-resource)."""
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    if confirmed is not True:raise ValueError('Explicit operator confirmation is required (--confirmed)')
    with broker.write('release') as db:
        row=_one(db,suspension_id)
        if row['state'] not in ACTIVE:raise ValueError('Suspension is already '+row['state'])
        health=None
        if row['scope']==RESOURCE:
            if db.execute("SELECT 1 FROM capacity_requests WHERE resource_id=? AND status IN ('leased','cancel_requested')",(row['resource_id'],)).fetchone():
                raise ValueError('Resource still has an active reservation')
            # Degraded is heartbeat-managed: the next health refresh re-validates every
            # qualification against the head's digests and runtime and returns it to healthy.
            db.execute("UPDATE capacity_resources SET health='degraded' WHERE resource_id=? AND health='quarantined'",(row['resource_id'],))
            health='degraded'
        resolve(broker,db,row,state='released',by=operator,resolution='operator_release',event='suspension.released')
        row=_one(db,suspension_id)
    return {'released':view(row),'resource_health':health,
            'next':'the next health refresh re-validates qualification and returns the head to healthy' if health else 'back in placement immediately'}
