"""Infrastructure-host policy loader. Not an application administration API."""
import argparse,json,os
from pathlib import Path
from .capacity_broker import CapacityBroker

def apply(path,database):
    policy=json.loads(Path(path).read_text(encoding='utf-8'))
    if policy.get('configuration_version')!=1 or policy.get('contract')!='shared.inference/1':raise ValueError('Unsupported infrastructure policy')
    from . import recovery
    for block in ('failure_policy','placement_policy'):recovery.validate_policy(block,policy.get(block)) # refuse before changing anything
    for resource in policy.get('resources',[]):
        if resource.get('model_capacity') is not None:CapacityBroker.validate_model_capacity(resource['model_capacity'])
    broker=CapacityBroker(database)
    # Explicit declarations only. Nothing scans, discovers, qualifies, or
    # authorizes an additional host by its mere presence on a network.
    for client in policy.get('clients',[]):
        client=dict(client);limits={'operations':client.pop('operations',None),'max_context':client.pop('max_context',None)}
        if client.pop('credential',None)=='provisioned':
            # Token issued by inference-credentials; never re-enables a revoked client.
            if 'token_environment' in client:raise ValueError('A provisioned client must not also name a token environment')
            broker.update_client_policy(**client)
        else:
            secret=os.environ.get(client.pop('token_environment'))
            if not secret:raise PermissionError('Required infrastructure-admin credential environment is absent')
            broker.register_client(token=secret,**client)
        broker.set_client_limits(client['app_id'],**limits)
    for resource in policy.get('resources',[]):broker.authorize_resource(**resource)
    with broker.write('bootstrap_policy') as db:
        for block in ('failure_policy','placement_policy'):
            # An absent block keeps the ledger's current values (defaults until one is applied).
            if block in policy:recovery.store_policy(db,block,policy[block])
        effective=recovery.load_policy(db)
    result={'clients':len(policy.get('clients',[])),'resources':len(policy.get('resources',[])),
        'qualification':'required separately','implicit_revocations':False}
    # Output is unchanged for a 0.3.0 policy; the effective 0.4.0 values are added when it uses them.
    if {'failure_policy','placement_policy'}&set(policy) or any({'slots','memory_gb','model_capacity'}&set(r) for r in policy.get('resources',[])):
        result.update(capacity={r['resource_id']:{'slots':r.get('slots',1),'memory_gb':r.get('memory_gb'),
                                                  # 0.4.4: declared per-model parallelism, as stored (absent = kept as before)
                                                  **({'model_capacity':sorted(r['model_capacity'])} if 'model_capacity' in r else {})}
                                for r in policy.get('resources',[])},
                      failure_policy=effective['failure'],placement_policy=effective['placement'])
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--database',required=True);p.add_argument('--policy',required=True);args=p.parse_args()
    print(json.dumps(apply(args.policy,args.database)))

if __name__=='__main__':main()
