"""Supervised publisher of the sanitized observation document for Agent Arena.

Runs under the gateway's infrastructure identity, never Arena's. Each cycle
takes one read-only capacity_observe snapshot with an overlapping event cursor,
writes it to a temporary file beside the publication and atomically replaces
the published file. The cursor is persisted per ledger_id. A ledger that is not
the operator-accepted one, or whose event history fell behind the persisted
cursor, is never published: the Arena operator must be told first, then an
infrastructure operator runs accept-ledger. Nothing here admits, schedules or
mutates the ledger.
"""
import argparse,json,logging,logging.handlers,os,signal,tempfile,threading,time
from pathlib import Path
from .capacity_observe import ledger_identity,snapshot
from .capacity_lifecycle import exclusive_lock

STATE_VERSION=1
MAX_DOCUMENT_BYTES=4*1024*1024 # Arena refuses larger documents.
STALE_TEMPORARY_SECONDS=60

class PublicationBlocked(RuntimeError):
    """Publishing would mislead Arena until an operator accepts the ledger."""

def load_state(path):
    path=Path(path)
    if not path.exists():return {'state_version':STATE_VERSION,'accepted_ledger_id':None,'cursors':{},'acceptances':[]}
    state=json.loads(path.read_text(encoding='utf-8'))
    if state.get('state_version')!=STATE_VERSION:raise ValueError('Unsupported publisher state')
    return state

def replace_atomically(path,data,*,attempts=20):
    """Write beside the target, flush to disk, then replace it in one rename."""
    path=Path(path)
    fd,temporary=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name+'.',suffix='.tmp')
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        for attempt in range(attempts):
            try:return os.replace(temporary,path)
            except PermissionError:
                # Windows refuses the rename while a reader holds the file open
                # without delete sharing. Readers hold it for milliseconds.
                if attempt==attempts-1:raise
                time.sleep(.05)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

def save_state(path,state):
    replace_atomically(path,json.dumps(state,indent=2,sort_keys=True).encode())

def check_layout(database,output,state_path):
    """The Arena-readable folder must hold only the publication and its temporaries."""
    database,output,state_path=(Path(p).resolve() for p in (database,output,state_path))
    if not output.parent.is_dir() or not state_path.parent.is_dir():raise ValueError('Publication and state folders must already exist')
    if output.parent in {database.parent,state_path.parent}:
        raise ValueError('Publish into a dedicated folder, separate from the ledger and publisher state')
    if output in {database,state_path}:raise ValueError('The publication cannot replace the ledger or publisher state')

def remove_stale_temporaries(output):
    output=Path(output)
    for path in output.parent.glob('.'+output.name+'.*.tmp'):
        try:
            if time.time()-path.stat().st_mtime>STALE_TEMPORARY_SECONDS:path.unlink()
        except OSError:pass

def publish_once(database,output,state_path,*,service_id,overlap=100,limit=1000):
    if not 0<=overlap<limit<=1000:raise ValueError('Overlap must be smaller than the 1–1000 event limit, or the cursor cannot advance')
    state=load_state(state_path)
    identity=ledger_identity(database);ledger=identity['ledger_id']
    if not ledger:raise PublicationBlocked('Ledger has no ledger_id; open it with this gateway release once before publishing')
    if ledger!=state['accepted_ledger_id']:
        raise PublicationBlocked('Ledger '+ledger+' is not the accepted ledger; tell the Arena operator, then run accept-ledger')
    cursor=state['cursors'].get(ledger,0)
    if identity['max_event_id']<cursor:
        raise PublicationBlocked('Ledger event history is behind the persisted cursor (restored or reset); tell the Arena operator, then run accept-ledger')
    document=snapshot(database,after_event_id=max(0,cursor-overlap),limit=limit,service_id=service_id)
    if document['ledger_id']!=ledger:raise PublicationBlocked('Ledger identity changed during the publication cycle')
    data=json.dumps(document).encode()
    if len(data)>MAX_DOCUMENT_BYTES:raise ValueError('Observation document exceeds the 4 MiB Arena limit; lower --limit')
    replace_atomically(output,data)
    # Overlap re-reads recent events; the persisted cursor never moves backwards.
    state['cursors'][ledger]=max(cursor,document['next_event_id'])
    save_state(state_path,state)
    return {'ledger_id':ledger,'next_event_id':state['cursors'][ledger],'events':len(document['events']),'bytes':len(data)}

def accept_ledger(database,state_path,*,operator):
    """Operator step after telling the Arena operator a ledger was replaced, reset or restored."""
    if not operator:raise ValueError('Explicit infrastructure operator identity required')
    state=load_state(state_path)
    identity=ledger_identity(database);ledger=identity['ledger_id']
    if not ledger:raise PublicationBlocked('Ledger has no ledger_id; open it with this gateway release once before accepting it')
    previous=state['accepted_ledger_id']
    # A restored ledger restarts from the history it actually has.
    cursor=min(state['cursors'].get(ledger,0),identity['max_event_id'])
    state['cursors'][ledger]=cursor;state['accepted_ledger_id']=ledger
    state['acceptances'].append({'ledger_id':ledger,'previous_ledger_id':previous,'operator':operator,'at':time.time(),'cursor':cursor})
    save_state(state_path,state)
    return {'accepted_ledger_id':ledger,'previous_ledger_id':previous,'cursor':cursor}

def status(database,state_path):
    state=load_state(state_path);identity=ledger_identity(database);ledger=identity['ledger_id']
    cursor=state['cursors'].get(ledger,0) if ledger else 0
    blocked=('ledger has no ledger_id' if not ledger else 'ledger not accepted' if ledger!=state['accepted_ledger_id']
        else 'event history behind cursor' if identity['max_event_id']<cursor else None)
    return {'ledger_id':ledger,'accepted_ledger_id':state['accepted_ledger_id'],'cursor':cursor,
        'max_event_id':identity['max_event_id'],'blocked':blocked}

def run(database,output,state_path,*,service_id,interval=5.0,overlap=100,limit=1000,stop=None,log=None):
    """Publish every interval until stopped. Logs state transitions only."""
    if not 1<=interval<=30:raise ValueError('Interval must be 1–30 seconds; Arena treats 30 s as stale')
    check_layout(database,output,state_path)
    remove_stale_temporaries(output)
    stop=stop or threading.Event();log=log or logging.getLogger(__name__);last=None
    while not stop.is_set():
        began=time.monotonic()
        try:
            with exclusive_lock(str(Path(state_path).resolve())+'.lock','Another publisher holds the publication state'):
                result=publish_once(database,output,state_path,service_id=service_id,overlap=overlap,limit=limit)
            current=('observation.published',result['ledger_id'])
        except PublicationBlocked as exc:current=('observation.blocked',str(exc))
        except Exception as exc:current=('observation.failed',type(exc).__name__+': '+str(exc)[:300])
        if current!=last:log.warning(json.dumps({'event':current[0],'detail':current[1],'at':time.time()}))
        last=current
        stop.wait(max(0,interval-(time.monotonic()-began)))

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    for name in ('run','accept-ledger','status'):
        command=sub.add_parser(name);command.add_argument('--database',required=True);command.add_argument('--state',required=True)
        if name=='run':
            command.add_argument('--output',required=True);command.add_argument('--service-id',default='shared-inference')
            command.add_argument('--interval',type=float,default=5);command.add_argument('--overlap',type=int,default=100)
            command.add_argument('--limit',type=int,default=1000);command.add_argument('--log-file');command.add_argument('--once',action='store_true')
        if name=='accept-ledger':command.add_argument('--operator',required=True)
    a=p.parse_args(argv)
    if a.action=='accept-ledger':
        with exclusive_lock(str(Path(a.state).resolve())+'.lock','A publisher cycle holds the publication state; retry'):
            print(json.dumps(accept_ledger(a.database,a.state,operator=a.operator)))
        return 0
    if a.action=='status':
        print(json.dumps(status(a.database,a.state)));return 0
    if a.once:
        check_layout(a.database,a.output,a.state)
        try:
            with exclusive_lock(str(Path(a.state).resolve())+'.lock','Another publisher holds the publication state'):
                print(json.dumps(publish_once(a.database,a.output,a.state,service_id=a.service_id,overlap=a.overlap,limit=a.limit)))
        except PublicationBlocked as exc:
            print(json.dumps({'event':'observation.blocked','detail':str(exc)}));return 3
        return 0
    log=logging.getLogger('inference_gateway.observation_publisher');log.propagate=False
    handler=(logging.handlers.RotatingFileHandler(a.log_file,maxBytes=1024*1024,backupCount=5,encoding='utf-8') if a.log_file
             else logging.StreamHandler())
    handler.setFormatter(logging.Formatter('%(message)s'));log.addHandler(handler)
    stop=threading.Event()
    for name in ('SIGINT','SIGTERM','SIGBREAK'):
        if hasattr(signal,name):signal.signal(getattr(signal,name),lambda *args:stop.set())
    log.warning(json.dumps({'event':'observation.publisher_started','interval':a.interval,'at':time.time()}))
    run(a.database,a.output,a.state,service_id=a.service_id,interval=a.interval,overlap=a.overlap,limit=a.limit,stop=stop,log=log)
    log.warning(json.dumps({'event':'observation.publisher_stopped','at':time.time()}))
    return 0

if __name__=='__main__':raise SystemExit(main())
