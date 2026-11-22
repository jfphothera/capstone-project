"""Infrastructure-host admission drain/status/resume over the SAME ledger.

Filesystem access is administrative authority. No application HTTP route is
added, no OS account, service, certificate or network setting is changed.
Also: authority binding (one writable gateway per ledger identity), verified
backup/restore, and operator idle-reconciliation of quarantined heads.
0.4.0: suspensions (list), probe (ask the service to probe now) and release
(manual override with --confirmed) for automatic failure recovery.
"""
import argparse,hashlib,json,os,socket,sqlite3,time
from contextlib import contextmanager
from pathlib import Path
from .capacity_broker import CapacityBroker

@contextmanager
def exclusive_lock(path,refusal):
    """Non-blocking kernel-held lock; the kernel releases it after a crash."""
    with open(path,'a+b') as lock:
        if lock.seek(0,os.SEEK_END)==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:raise RuntimeError(refusal) from exc
        try:yield
        finally:
            lock.seek(0)
            if os.name=='nt':msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(lock,fcntl.LOCK_UN)

@contextmanager
def service_owner(database):
    """One CLI service owner per ledger; kernel releases the lock after a crash."""
    with exclusive_lock(str(Path(database).resolve())+'.service.lock','A shared service already owns this ledger, or its lock is unavailable'):yield

def _policy(db,key):
    row=db.execute('SELECT value FROM capacity_service_policy WHERE key=?',(key,)).fetchone()
    return row[0] if row else None

def authority_identity(database,service_id):
    return {'host':socket.gethostname().lower(),'ledger':str(Path(database).resolve()).lower(),'service_id':service_id}

def claim_authority(broker,service_id):
    """Bind on first start; refuse a copied, moved or re-identified ledger until an operator adopts it."""
    current=authority_identity(broker.path,service_id)
    with broker.write('claim_authority') as db:
        stored=_policy(db,'authority')
        if stored is None:
            db.execute("INSERT INTO capacity_service_policy VALUES('authority',?)",(json.dumps(current,sort_keys=True),))
            broker._event(db,'infrastructure',None,'service.authority_bound',{});return current
        if json.loads(stored)!=current:
            raise RuntimeError('This ledger is bound to another authority (host, path or service ID differ). Verify that authority is stopped and '
                               'cannot restart, then run: inference-lifecycle adopt --database <ledger> --service-id <id> --operator <id>')
    return current

def adopt(broker,service_id,operator):
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    current=authority_identity(broker.path,service_id)
    with broker.write('adopt') as db:
        previous=_policy(db,'authority')
        db.execute("INSERT INTO capacity_service_policy VALUES('authority',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(current,sort_keys=True),))
        broker._event(db,'infrastructure',None,'service.authority_adopted',{'operator':operator})
    return {'authority':current,'previous':json.loads(previous) if previous else None}

def status(broker):
    broker.reconcile_expired()
    with broker.connect() as db:
        admission=_policy(db,'admission_open')=='1'
        active=db.execute("SELECT count(*) FROM capacity_requests WHERE status IN ('leased','cancel_requested')").fetchone()[0]
        queued=db.execute("SELECT count(*) FROM capacity_requests WHERE status='queued'").fetchone()[0]
        uncertain=db.execute("SELECT count(*) FROM capacity_resources WHERE health='quarantined'").fetchone()[0]
        resume=_policy(db,'service_stop_drain')=='1';authority=_policy(db,'authority');ledger=_policy(db,'ledger_id')
        suspensions={r['state']:r['n'] for r in db.execute("SELECT state,count(*) n FROM capacity_suspensions WHERE state IN ('suspended','held','needs_attention') GROUP BY state")}
    return {'admission_open':admission,'active_allocations':active,'queued_requests':queued,'uncertain_resources':uncertain,
        'safe_to_stop':not admission and active==0 and uncertain==0,'resume_on_service_start':resume,
        'ledger_id':ledger,'authority':json.loads(authority) if authority else None,'suspensions':suspensions}

def set_admission(broker,opened,operator):
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    with broker.write('set_admission') as db:
        db.execute("UPDATE capacity_service_policy SET value=? WHERE key='admission_open'",('1' if opened else '0',))
        # An explicit operator decision always overrides a service-stop drain.
        db.execute("DELETE FROM capacity_service_policy WHERE key='service_stop_drain'")
        broker._event(db,'infrastructure',None,'service.admission_resumed' if opened else 'service.admission_drained',{'operator':operator})
    return status(broker)

def service_stop_drain(broker):
    """Pre-stop hook: close admission persistently, remembering that the service (not an operator) did it."""
    with broker.write('service_stop_drain') as db:
        if _policy(db,'admission_open')=='1':
            db.execute("UPDATE capacity_service_policy SET value='0' WHERE key='admission_open'")
            db.execute("INSERT OR REPLACE INTO capacity_service_policy VALUES('service_stop_drain','1')")
            broker._event(db,'infrastructure',None,'service.admission_drained',{'operator':'service-stop'})
            return True
    return False

def resume_after_service_stop(broker):
    """Startup: reopen only a drain the service itself made; an operator drain is preserved."""
    with broker.write('resume_after_service_stop') as db:
        if _policy(db,'service_stop_drain')!='1':return False
        db.execute("DELETE FROM capacity_service_policy WHERE key='service_stop_drain'")
        db.execute("UPDATE capacity_service_policy SET value='1' WHERE key='admission_open'")
        broker._event(db,'infrastructure',None,'service.admission_resumed',{'operator':'service-start'})
    return True

def _sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as handle:
        for block in iter(lambda:handle.read(1<<20),b''):digest.update(block)
    return digest.hexdigest()

def _check(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Ledger integrity check failed')
        return {'ledger_id':db.execute("SELECT value FROM capacity_service_policy WHERE key='ledger_id'").fetchone()[0],
                'max_event_id':db.execute('SELECT coalesce(max(event_id),0) FROM capacity_events').fetchone()[0]}
    finally:db.close()

def backup(database,output):
    """Consistent online copy (SQLite backup API), integrity-checked, with a hash record."""
    output=Path(output)
    if not Path(database).is_file():raise FileNotFoundError('Ledger does not exist')
    if output.exists():raise FileExistsError('Backup target exists')
    source=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True);target=sqlite3.connect(output)
    try:source.backup(target)
    finally:target.close();source.close()
    record={'backup':output.name,'sha256':_sha256(output),'created':time.time(),'source':str(Path(database).resolve()),**_check(output)}
    Path(str(output)+'.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    return record

def restore(backup_path,database,operator,*,replace=False):
    """Restore while no service owns the ledger; the previous file is kept, never deleted."""
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    record=json.loads(Path(str(backup_path)+'.json').read_text(encoding='utf-8'))
    if _sha256(backup_path)!=record['sha256']:raise ValueError('Backup hash does not match its record')
    _check(backup_path);database=Path(database)
    with service_owner(database):
        kept=None
        if database.exists():
            if not replace:raise FileExistsError('Ledger exists; pass --replace to keep it aside and restore')
            kept=database.with_name(database.name+'.pre-restore-'+time.strftime('%Y%m%dT%H%M%S'))
            os.replace(database,kept)
        source=sqlite3.connect(Path(backup_path).resolve().as_uri()+'?mode=ro',uri=True);target=sqlite3.connect(database)
        try:source.backup(target)
        finally:target.close();source.close()
        broker=CapacityBroker(database)
        with broker.write('restore_event') as db:broker._event(db,'infrastructure',None,'service.ledger_restored',{'operator':operator})
    return {'restored':str(database),'previous_kept_as':str(kept) if kept else None,**_check(database),
            'next':'Tell the Arena operator, then accept-ledger; adopt if host, path or service ID changed'}

def reconcile_resource(broker,resource_id,operator):
    """After verifying physical idleness: quarantined -> unqualified (requalify before use)."""
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    broker.reconcile_resource(resource_id,confirmed_idle=True)
    with broker.write('reconcile_event') as db:broker._event(db,'infrastructure',None,'resource.reconciled',{'resource_id':resource_id,'operator':operator})
    return {'resource_id':resource_id,'health':'unqualified','next':'requalify before use'}

def capacity(broker):
    """0.4.4: each head's per-model limit and its source (declared, measured, serial_family, slots, policy). Read-only."""
    from . import model_capacity,recovery
    with broker.connect() as db:
        policy=recovery.load_policy(db)['placement'];evidence=model_capacity.load(db);out=[]
        for row in db.execute("SELECT resource_id,qualified_models,health FROM capacity_resources WHERE resource_type='shared_inference' ORDER BY resource_id").fetchall():
            slots=db.execute('SELECT slots FROM capacity_resource_capacity WHERE resource_id=?',(row['resource_id'],)).fetchone()
            models=sorted(set(json.loads(row['qualified_models']))|{m for (r,m) in evidence if r==row['resource_id']})
            out.append({'resource_id':row['resource_id'],'health':row['health'],'slots':slots[0] if slots else 1,
                        'model_limits':model_capacity.head_limits(db,row['resource_id'],models,policy,evidence=evidence)})
    return {'resources':out,'parallel_runtime_min':policy['parallel_runtime_min'],'serial_families':policy['serial_families'],
            'serial_families_need_evidence':policy['serial_families_need_evidence'],'at':time.time()}

def upstream_errors(database,*,request_id=None,since_seconds=3600,limit=50):
    """0.4.6: what heads answered when they refused calls (protected: bodies are operator-only). Read-only."""
    import sqlite3
    db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_upstream_errors'").fetchone():
            return {'errors':[],'note':'ledger has no capacity_upstream_errors table (written by 0.4.6 and later)'}
        if request_id:rows=db.execute('SELECT * FROM capacity_upstream_errors WHERE request_id=? ORDER BY error_id',(request_id,)).fetchall()
        else:rows=db.execute('SELECT * FROM capacity_upstream_errors WHERE at>? ORDER BY error_id DESC LIMIT ?',(time.time()-since_seconds,limit)).fetchall()
        out=[]
        for r in rows:
            item=dict(r);item['shape']=json.loads(item['shape']) if item.get('shape') else None;out.append(item)
        return {'errors':out,'at':time.time()}
    finally:db.close()

def main():
    from . import recovery
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['status','drain','resume','backup','restore','adopt','reconcile-resource','suspensions','probe','release','capacity','upstream-errors'])
    p.add_argument('--request');p.add_argument('--since-seconds',type=float,default=3600);p.add_argument('--limit',type=int,default=50)
    p.add_argument('--database',required=True);p.add_argument('--operator');p.add_argument('--wait-seconds',type=float,default=None)
    p.add_argument('--output');p.add_argument('--backup');p.add_argument('--replace',action='store_true')
    p.add_argument('--service-id',default='shared-inference');p.add_argument('--resource');p.add_argument('--confirmed-idle',action='store_true')
    p.add_argument('--suspension');p.add_argument('--confirmed',action='store_true');p.add_argument('--all',action='store_true');a=p.parse_args()
    if a.wait_seconds is None:a.wait_seconds=120 if a.action=='probe' else 0
    if not 0<=a.wait_seconds<=3600:p.error('Drain wait must be 0–3600 seconds')
    if a.action=='upstream-errors':
        if not Path(a.database).is_file():p.error('Ledger does not exist')
        if not 1<=a.limit<=1000 or not 0<a.since_seconds<=30*86400:p.error('--limit 1-1000 and --since-seconds up to 30 days')
        print(json.dumps(upstream_errors(a.database,request_id=a.request,since_seconds=a.since_seconds,limit=a.limit),default=str));return 0
    if a.action in ('suspensions','probe','release','capacity'):
        if not Path(a.database).is_file():p.error('Ledger does not exist')
        broker=CapacityBroker(a.database)
        if a.action=='capacity':print(json.dumps(capacity(broker),default=str));return 0
        if a.action=='suspensions':print(json.dumps(recovery.list_suspensions(broker,include_resolved=a.all)));return 0
        if not a.suspension or not a.operator:p.error('--suspension and --operator required')
        try:
            if a.action=='probe':print(json.dumps(recovery.probe_now(broker,a.suspension,a.operator,wait_seconds=a.wait_seconds)));return 0
            if not a.confirmed:p.error('--confirmed required: a manual release overrides automatic recovery')
            print(json.dumps(recovery.release(broker,a.suspension,a.operator,confirmed=True)));return 0
        except ValueError as exc:print(json.dumps({'ok':False,'error':str(exc)}));return 1
    if a.action=='backup':
        if not a.output:p.error('--output required')
        print(json.dumps(backup(a.database,a.output)));return 0
    if a.action=='restore':
        if not a.backup:p.error('--backup required')
        print(json.dumps(restore(a.backup,a.database,a.operator,replace=a.replace)));return 0
    if not Path(a.database).is_file():p.error('Ledger does not exist')
    broker=CapacityBroker(a.database)
    if a.action=='adopt':print(json.dumps(adopt(broker,a.service_id,a.operator)));return 0
    if a.action=='reconcile-resource':
        if not a.resource or not a.confirmed_idle:p.error('--resource and --confirmed-idle (after verifying the head is idle) required')
        print(json.dumps(reconcile_resource(broker,a.resource,a.operator)));return 0
    result=status(broker) if a.action=='status' else set_admission(broker,a.action=='resume',a.operator)
    deadline=time.monotonic()+a.wait_seconds
    while a.action=='drain' and result['active_allocations'] and time.monotonic()<deadline:
        time.sleep(min(.2,max(0,deadline-time.monotonic())));result=status(broker)
    print(json.dumps(result))
    return 1 if a.action=='drain' and not result['safe_to_stop'] else 0

if __name__=='__main__':raise SystemExit(main())
