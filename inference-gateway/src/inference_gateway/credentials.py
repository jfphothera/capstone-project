"""Host-side application credential issuance, rotation and revocation.

Run only as an infrastructure administrator on the Gateway host. A token is
generated here, written once to a new file whose ACL is restricted *before* the
token is written, and never printed. The ledger stores only its SHA-256 hash.
Deliver the file to the application host out of band, then delete it here.
Exit 0 success, 1 refused, 2 bad input; one JSON object on stdout.

0.4.6, for a temporary identity a lab tool issues and revokes on every run: `reissue` (issue, or revoke, re-apply
the policy entry and re-key an existing row), `grant-heads --like <app>` (the identity may use exactly that
application's heads) and `withdraw-heads`. They change only that application's client row, its limits and its name
in head application lists: never slots, memory, model_capacity, qualifications, policy blocks or other clients.
"""
import argparse,json,os,re,secrets,subprocess,sys,time
from pathlib import Path
from .capacity_broker import CapacityBroker

POLICY_FIELDS=('models','max_concurrency','queue_limit','priority')


def _broker(database):
    # Never create an empty authority by mistyping the ledger path.
    if not Path(database).is_file():raise FileNotFoundError('Ledger does not exist; bootstrap or restore it first')
    return CapacityBroker(database)


def write_secret(path,secret,*,reader=None):
    """Create path exclusively, restrict it, then write the secret."""
    path=Path(path)
    if not path.parent.is_dir():raise FileNotFoundError('Token output folder must exist')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        if os.name=='nt':
            me=os.environ.get('USERDOMAIN','.')+'\\'+os.environ.get('USERNAME','')
            grants=['*S-1-5-18:F','*S-1-5-32-544:F',me+':F']+([reader+':R'] if reader else [])
            result=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',*grants,'/Q'],capture_output=True,text=True)
            if result.returncode:raise PermissionError('Could not restrict the token file ACL')
        os.write(fd,(secret+'\n').encode())
    except BaseException:
        os.close(fd);path.unlink(missing_ok=True);raise
    os.close(fd)


def _policy_entry(policy,app_id):
    document=json.loads(Path(policy).read_text(encoding='utf-8'))
    if document.get('configuration_version')!=1 or document.get('contract')!='shared.inference/1':raise ValueError('Unsupported infrastructure policy')
    entry=next((c for c in document.get('clients',[]) if c.get('app_id')==app_id),None)
    if not entry:raise ValueError('The approved policy does not declare this application')
    return {k:entry[k] for k in POLICY_FIELDS if k in entry},{'operations':entry.get('operations'),'max_context':entry.get('max_context')}


def issue(database,policy,app_id,output,*,reader=None):
    broker=_broker(database);fields,limits=_policy_entry(policy,app_id)
    with broker.connect() as db:
        if db.execute('SELECT 1 FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone():raise PermissionError('Application already has a credential; use rotate')
    token=secrets.token_urlsafe(32)
    write_secret(output,token,reader=reader)
    try:
        broker.register_client(app_id,token,**fields);broker.set_client_limits(app_id,**limits)
    except BaseException:
        Path(output).unlink(missing_ok=True);raise
    return {'app_id':app_id,'token_file':str(Path(output).resolve()),'action':'issued'}


def rotate(database,app_id,output,*,reader=None,enable=False):
    broker=_broker(database)
    with broker.connect() as db:
        if not db.execute('SELECT 1 FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone():raise PermissionError('Unknown application; use issue')
    token=secrets.token_urlsafe(32)
    write_secret(output,token,reader=reader)
    try:broker.set_token(app_id,token,enable=enable)
    except BaseException:
        Path(output).unlink(missing_ok=True);raise
    return {'app_id':app_id,'token_file':str(Path(output).resolve()),'action':'rotated','enabled':enable or None}


def revoke(database,app_id):
    broker=_broker(database)
    with broker.connect() as db:
        if not db.execute('SELECT 1 FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone():raise PermissionError('Unknown application')
    broker.revoke(app_id)
    return {'app_id':app_id,'action':'revoked'}


TEMPORARY_NAME=re.compile(r'^lab-[a-z0-9][a-z0-9-]{0,62}$')
MAX_TEMPORARY_SECONDS=24*3600


def _temporary_guard(db,broker,app_id,*,policy=None):
    """0.4.6: reissue, grant-heads and withdraw-heads act only on temporary lab identities. Refused (nothing changes):
    a name that is not lab-<lowercase letters, digits, hyphens>; an application that exists in the ledger but was not
    issued by reissue (a credential from `credentials issue` or the bootstrap policy, for example lab-traffic); and,
    for reissue, a policy file that is not clients-only with exactly this one application (the hub's infrastructure
    policy, which declares the real applications and heads, is never accepted)."""
    if not isinstance(app_id,str) or not TEMPORARY_NAME.fullmatch(app_id):
        raise PermissionError('%s is not a temporary lab identity: these commands act only on names that start with "lab-" '
                              '(lowercase letters, digits and hyphens)'%app_id)
    existing=db.execute('SELECT enabled FROM capacity_clients WHERE app_id=?',(app_id,)).fetchone()
    if existing and app_id not in broker.temporary_clients(db):
        raise PermissionError('%s holds a credential that was not issued as temporary (credentials issue or the bootstrap policy); '
                              'these commands never change it'%app_id)
    if policy is not None:
        document=json.loads(Path(policy).read_text(encoding='utf-8'))
        clients=document.get('clients') if isinstance(document.get('clients'),list) else []
        if (set(document)-{'configuration_version','contract','clients'} or len(clients)!=1 or not isinstance(clients[0],dict)
                or clients[0].get('app_id')!=app_id or 'credential' in clients[0] or 'token_environment' in clients[0]):
            raise PermissionError('reissue needs a clients-only policy file declaring exactly %s (not the infrastructure policy)'%app_id)
    return existing


def reissue(database,policy,app_id,output,*,reader=None,expires_in=4*3600):
    """0.4.6: a temporary lab identity: issue it, or (an earlier run's row, disabled or left enabled by a crash) revoke
    it, apply its policy entry and give it a new token. It expires after `expires_in` seconds (at most 24 h): the
    Gateway then refuses its token and its health cycle revokes it and withdraws it from the heads.
    Changes only this application's client row, its limits and the temporary-clients list; nothing else.
    """
    if isinstance(expires_in,bool) or not isinstance(expires_in,(int,float)) or not 60<=expires_in<=MAX_TEMPORARY_SECONDS:
        raise ValueError('--expires-in-seconds must be 60-%d'%MAX_TEMPORARY_SECONDS)
    broker=_broker(database)
    with broker.connect() as db:_temporary_guard(db,broker,app_id,policy=policy)
    fields,limits=_policy_entry(policy,app_id)
    token=secrets.token_urlsafe(32)
    write_secret(output,token,reader=reader) # first: a token file that cannot be created changes nothing in the ledger
    expires_at=time.time()+expires_in
    try:
        # One transaction: earlier row revoked, client row and limits written, temporary mark set last (re-checked inside).
        previous=broker.issue_temporary(app_id,token,**fields,**limits,expires_at=expires_at,by='credentials reissue')
    except BaseException:
        Path(output).unlink(missing_ok=True);raise
    return {'app_id':app_id,'token_file':str(Path(output).resolve()),'action':'reissued' if previous else 'issued','previous':previous,
            'expires_at':expires_at}


def grant_heads(database,app_id,like):
    """0.4.6: let the temporary `app_id` use exactly the heads `like` may use: its name is added to those heads' applications.

    `like` may be any application; it is only read. Nothing else on a head changes (slots, memory, model_capacity,
    qualified models, health), and no policy block. Refused for anything _temporary_guard refuses, a disabled or
    unknown application, an unknown template, or if `app_id` is already on a head the template is not (a grant never
    widens beyond the template). Idempotent; evented per head.
    """
    broker=_broker(database)
    with broker.write('grant_heads') as db:
        client=_temporary_guard(db,broker,app_id)
        if not client or not client['enabled']:raise PermissionError('Unknown or disabled application; issue it first')
        if not db.execute('SELECT 1 FROM capacity_clients WHERE app_id=?',(like,)).fetchone():raise PermissionError('Unknown template application')
        rows=[(r['resource_id'],json.loads(r['applications'])) for r in db.execute('SELECT resource_id,applications FROM capacity_resources ORDER BY resource_id')]
        stray=[rid for rid,apps in rows if app_id in apps and like not in apps]
        if stray:raise ValueError('%s is already authorized on heads %s, which %s is not'%(app_id,', '.join(stray),like))
        granted=[];already=[]
        for rid,apps in rows:
            if like not in apps:continue
            if app_id in apps:already.append(rid);continue
            db.execute('UPDATE capacity_resources SET applications=? WHERE resource_id=?',(json.dumps(apps+[app_id]),rid))
            broker._event(db,'infrastructure',None,'resource.application_granted',{'resource_id':rid,'app_id':app_id,'like':like})
            granted.append(rid)
    return {'app_id':app_id,'like':like,'granted':granted,'already':already,'action':'heads_granted'}


def withdraw_heads(database,app_id):
    """0.4.6: remove the temporary `app_id` from every head's applications (nothing else changes). Refused for anything
    _temporary_guard refuses, and while the application is enabled and still has queued or running work; a revoked
    (disabled) one can always be withdrawn."""
    broker=_broker(database)
    with broker.write('withdraw_heads') as db:
        client=_temporary_guard(db,broker,app_id)
        if not client:raise PermissionError('Unknown application')
        busy=db.execute("SELECT count(*) FROM capacity_requests WHERE app_id=? AND status IN ('queued','leased','cancel_requested')",(app_id,)).fetchone()[0]
        if client['enabled'] and busy:raise ValueError('%s still has %d queued or running requests; revoke it first'%(app_id,busy))
        withdrawn=broker.withdraw_application(db,app_id)
    return {'app_id':app_id,'withdrawn':withdrawn,'still_running':busy,'action':'heads_withdrawn'}


def listing(database):
    broker=_broker(database)
    with broker.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT app_id,enabled,max_concurrency,queue_limit,priority,models FROM capacity_clients ORDER BY app_id')]
    for row in rows:row.update(models=json.loads(row['models']),enabled=bool(row['enabled']),**broker.client_limits(row['app_id']))
    return {'applications':rows}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action',required=True)
    for name in ('issue','rotate','revoke','list','reissue','grant-heads','withdraw-heads'):
        command=sub.add_parser(name);command.add_argument('--database',required=True)
        if name!='list':command.add_argument('--app-id',required=True)
        if name in ('issue','rotate','reissue'):command.add_argument('--output',required=True);command.add_argument('--reader',help='Windows account that may read the token file')
        if name in ('issue','reissue'):command.add_argument('--policy',required=True)
        if name=='reissue':command.add_argument('--expires-in-seconds',type=float,default=4*3600,help='the temporary identity stops working after this (60-86400)')
        if name=='rotate':command.add_argument('--enable',action='store_true',help='Re-enable a revoked application with the new token')
        if name=='grant-heads':command.add_argument('--like',required=True,help='the application whose heads this one may use')
    try:a=p.parse_args(argv)
    except SystemExit as exc:return 2 if exc.code else 0
    try:
        result=(issue(a.database,a.policy,a.app_id,a.output,reader=a.reader) if a.action=='issue'
                else reissue(a.database,a.policy,a.app_id,a.output,reader=a.reader,expires_in=a.expires_in_seconds) if a.action=='reissue'
                else grant_heads(a.database,a.app_id,a.like) if a.action=='grant-heads'
                else withdraw_heads(a.database,a.app_id) if a.action=='withdraw-heads'
                else rotate(a.database,a.app_id,a.output,reader=a.reader,enable=a.enable) if a.action=='rotate'
                else revoke(a.database,a.app_id) if a.action=='revoke' else listing(a.database))
    except (ValueError,FileNotFoundError,FileExistsError) as exc:print(json.dumps({'ok':False,'error':str(exc)}));return 2
    except PermissionError as exc:print(json.dumps({'ok':False,'error':str(exc)}));return 1
    print(json.dumps({'ok':True,**result}));return 0


if __name__=='__main__':raise SystemExit(main())
