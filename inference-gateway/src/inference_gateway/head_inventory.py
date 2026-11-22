"""Convert Agent Arena's verified head inventory into reviewable bootstrap resources.

Input: agent-arena.verified-head-inventory/1 (F: lab provisioner 1.1.0, hub stage D):
heads[] with resource_id/arena_node_id head-192-0-2-N, address, endpoint and
verified{ollama_version, models}. Output: the approved policy with those heads as
explicit shared_inference resources for the named applications. Nothing is
enrolled or qualified here; the operator reviews the output and applies it with
bootstrap, then qualifies each head. Exit 0 ok, 1 refused heads, 2 bad input.
"""
import argparse
import ipaddress
import json
import re
from pathlib import Path
from .capacity_broker import CapacityBroker

SCHEMA='agent-arena.verified-head-inventory/1'
HEAD=re.compile(r'^head-192-0-2-(\d{1,3})$')
GATEWAY_HOST='192.0.2.254'


def resources(inventory,applications,*,owner='infrastructure',port=11434):
    if inventory.get('schema')!=SCHEMA:raise ValueError('Expected '+SCHEMA)
    if not applications or not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',a) for a in applications):raise ValueError('Explicit application IDs required')
    accepted=[];refused=[]
    for head in inventory.get('heads',[]):
        rid=head.get('resource_id');address=head.get('address');match=HEAD.match(rid or '')
        reason=None
        if not match or head.get('arena_node_id')!=rid:reason='resource_id must be the Arena node ID head-192-0-2-N'
        elif address!='192.0.2.'+match.group(1):reason='address does not match the node ID'
        elif address==GATEWAY_HOST:reason='the Gateway host is never an inference head'
        elif not 2<=int(match.group(1))<=199:reason='outside the head range 192.0.2.2-199'
        elif not ipaddress.ip_address(address).is_private:reason='not a private address'
        elif head.get('endpoint')!='http://%s:%d'%(address,port):reason='endpoint must be http://<address>:%d'%port
        elif not (head.get('verified') or {}).get('ollama_version'):reason='not verified as Ollama from the hub'
        if reason:refused.append({'resource_id':rid,'reason':reason});continue
        entry={'resource_id':rid,'endpoint':head['endpoint'],'resource_type':'shared_inference','owner':owner,
               'applications':sorted(set(applications)),'permitted_uses':['inference']}
        # 0.4.0: optional per-head capacity from Arena's lab head list (slots = OLLAMA_NUM_PARALLEL).
        capacity=head.get('capacity') or {}
        # 0.4.4: model_capacity (agent-arena.model-capacity/1, keyed by model name) travels the same way.
        for key in ('slots','memory_gb','model_capacity'):
            if capacity.get(key) is not None:entry[key]=capacity[key]
        if 'model_capacity' in entry:
            try:CapacityBroker.validate_model_capacity(entry['model_capacity'])
            except ValueError as exc:refused.append({'resource_id':rid,'reason':str(exc)});continue
        accepted.append(entry)
    if len({r['resource_id'] for r in accepted})!=len(accepted):raise ValueError('Duplicate head in inventory')
    return accepted,refused


def merge(policy,new):
    """Replace same-ID resources, keep every other declaration; never removes anything.

    A replaced head keeps the slots/memory_gb (0.4.4: and model_capacity) its previous declaration
    had unless the inventory supplies new values, so a re-import never silently resets capacity.
    """
    previous={r['resource_id']:r for r in policy.get('resources',[])}
    new=[{**{k:previous[n['resource_id']][k] for k in ('slots','memory_gb','model_capacity') if k in previous.get(n['resource_id'],{})},**n} for n in new]
    kept=[r for r in policy.get('resources',[]) if r['resource_id'] not in {n['resource_id'] for n in new}]
    return {**policy,'resources':kept+new}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inventory',required=True);p.add_argument('--policy',required=True);p.add_argument('--output',required=True)
    p.add_argument('--applications',required=True,help='Comma-separated application IDs allowed on these heads')
    try:a=p.parse_args(argv)
    except SystemExit as exc:return 2 if exc.code else 0
    try:
        inventory=json.loads(Path(a.inventory).read_text(encoding='utf-8-sig'));policy=json.loads(Path(a.policy).read_text(encoding='utf-8'))
        if policy.get('configuration_version')!=1 or policy.get('contract')!='shared.inference/1':raise ValueError('Unsupported infrastructure policy')
        accepted,refused=resources(inventory,[x.strip() for x in a.applications.split(',') if x.strip()])
        if Path(a.output).exists():raise FileExistsError('Output exists; choose a new file for review')
    except (ValueError,OSError) as exc:print(json.dumps({'ok':False,'error':str(exc)}));return 2
    Path(a.output).write_text(json.dumps(merge(policy,accepted),indent=2),encoding='utf-8')
    print(json.dumps({'ok':not refused,'output':a.output,'resources':[r['resource_id'] for r in accepted],'refused':refused,
                      'excluded_by_arena':inventory.get('excluded',[]),'next':'review, then bootstrap --policy <output>, then qualify each head'}))
    return 1 if refused else 0


if __name__=='__main__':raise SystemExit(main())
