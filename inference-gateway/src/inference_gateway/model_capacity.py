"""Per head and model: how many leases of one model a head takes at once (release 0.4.4).

0.4.3 limited the qwen35 and qwen35moe families to one lease per head because Ollama
0.40.13 runs them one request at a time. Ollama 0.40.15 runs recurrent models in parallel
(ollama#19545), but upgrading a head proves nothing about a given model build, context and
setting. So the limit now comes from evidence that names the runtime and the build:

  declared   Agent Arena's head qualification matrix (agent-arena.model-capacity/1), carried by
             the bootstrap policy as resources[].model_capacity[model] =
             {parallel, digest, runtime_version, num_ctx, evidence}
  measured   the Gateway's own measurement (qualify --parallel N): N concurrent short
             generations whose Ollama timings show how many really ran at the same time

A value is used only while the head's live /api/version equals its runtime_version AND the
model's installed digest equals its digest. With both, the limit is min(declared, measured).
Otherwise the conservative default:

  serial family (placement_policy.serial_families) on a runtime below
  placement_policy.parallel_runtime_min (default 0.40.15), or with unknown version  -> 1
  serial family on a newer runtime, no current evidence                              -> 1
      (placement_policy.serial_families_need_evidence, default true; false -> slots)
  any other model                                                                    -> slots

placement_policy.model_parallel[model] (0.4.3, operator) still wins over all of these.
Every result is capped at the head's slots. Sources: policy, declared, measured,
serial_family, slots.
"""
import json
import re

VERSION=re.compile(r'^\d+(\.\d+){0,3}$')


def version_tuple(value):
    """'0.40.15' -> (0, 40, 15); None for anything else (pre-releases and unknown count as older)."""
    if not isinstance(value,str) or not VERSION.match(value.strip()):return None
    return tuple(int(p) for p in value.strip().split('.'))


def at_least(version,minimum):
    have,need=version_tuple(version),version_tuple(minimum)
    if have is None or need is None:return False
    width=max(len(have),len(need))
    return have+(0,)*(width-len(have))>=need+(0,)*(width-len(need))


def same_digest(a,b):
    def norm(d):return d.strip().lower().removeprefix('sha256:') if isinstance(d,str) else None
    return norm(a) is not None and norm(a)==norm(b)


def live_version(runtime_json):
    """The version string from a stored /api/version reply (capacity_inventory.runtime_version)."""
    try:value=json.loads(runtime_json) if isinstance(runtime_json,str) else runtime_json
    except ValueError:return None
    return value.get('version') if isinstance(value,dict) and isinstance(value.get('version'),str) else None


def _entry(row):
    if not row:return None
    return {'parallel':row['parallel'],'digest':row['digest'],'runtime_version':row['runtime_version'],'num_ctx':row['num_ctx'],
            'evidence':row['evidence'],'recorded_at':row['recorded_at']}


def effective(model,*,slots,families,runtime_version,digest,declared,measured,policy):
    """The effective per-model limit on one head, with its source and the evidence considered."""
    slots=max(1,int(slots or 1))
    out={'model':model,'slots':slots,'runtime_version':runtime_version,'digest':digest,
         'declared':_entry(declared),'measured':_entry(measured),'reason':None}
    for key in ('declared','measured'):
        item=out[key]
        if item:
            item['current']=bool(runtime_version and item['runtime_version']==runtime_version and same_digest(item['digest'],digest))
            if not item['current']:item['stale_because']='runtime_changed' if item['runtime_version']!=runtime_version else 'digest_changed'
    explicit=(policy.get('model_parallel') or {}).get(model)
    if explicit:
        out.update(limit=min(slots,int(explicit)),source='policy');return out
    current=[(item['parallel'],key) for key in ('measured','declared') if (item:=out[key]) and item['current']]
    if current:
        value,source=min(current) # a tie names the measurement
        out.update(limit=min(slots,value),source=source);return out
    if set(families or ())&set(policy.get('serial_families') or []):
        if not at_least(runtime_version,policy.get('parallel_runtime_min','0.40.15')):
            out.update(limit=1,source='serial_family',reason='runtime_below_parallel_min' if version_tuple(runtime_version) else 'runtime_version_unknown');return out
        if policy.get('serial_families_need_evidence',True):
            out.update(limit=1,source='serial_family',reason='no_current_parallel_evidence');return out
    default=int(policy.get('unmeasured_model_limit') or 0) # 0.4.5: no current evidence for this model on this head
    if default>0:
        out.update(limit=min(slots,default),source='unmeasured_default',reason='no_current_evidence');return out
    out.update(limit=slots,source='slots');return out


def load(db):
    """{(resource_id, model): {'declared': row, 'measured': row}} from the ledger (empty before 0.4.4)."""
    out={}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capacity_model_capacity'").fetchone():return out
    for row in db.execute('SELECT * FROM capacity_model_capacity'):
        out.setdefault((row['resource_id'],row['model']),{})[row['source']]=row
    return out


def head_limits(db,resource_id,models,policy,*,evidence=None):
    """Effective limits for these models on one head, from the ledger (observation, status, /v1/models)."""
    evidence=load(db) if evidence is None else evidence
    # 0.4.6 (G2): the map is keyed by (resource_id, model). 0.4.5's /v1/models passed a qualification document here
    # (string keys), which silently reported default limits; refuse anything that is not the capacity map.
    if not isinstance(evidence,dict) or any(not (isinstance(k,tuple) and len(k)==2) for k in evidence):
        raise TypeError('head_limits needs the capacity map from model_capacity.load (keys (resource_id, model))')
    cap=db.execute('SELECT slots FROM capacity_resource_capacity WHERE resource_id=?',(resource_id,)).fetchone()
    inventory=db.execute('SELECT runtime_version,models FROM capacity_inventory WHERE resource_id=?',(resource_id,)).fetchone()
    runtime=db.execute('SELECT * FROM capacity_inventory_runtime WHERE resource_id=?',(resource_id,)).fetchone()
    families=json.loads(runtime['families']) if runtime and 'families' in runtime.keys() and runtime['families'] else {}
    installed=json.loads(inventory['models']) if inventory else {}
    version=live_version(inventory['runtime_version']) if inventory else None
    out=[]
    for model in models:
        rows=evidence.get((resource_id,model),{})
        out.append(effective(model,slots=cap['slots'] if cap else 1,families=families.get(model) or [],runtime_version=version,
                             digest=installed.get(model),declared=rows.get('declared'),measured=rows.get('measured'),policy=policy))
    return out


def judge_overlap(replies):
    """How many of these generations really ran at the same time on the head (0.4.4 measurement).

    Each Ollama reply carries created_at (when the reply was produced) and the nanosecond
    durations of prompt evaluation and generation. A call computed during
    [created_at - (prompt_eval_duration + eval_duration), created_at]. Time spent loading or
    waiting for a runner is in load_duration and is excluded. Intervals are shrunk by 10% at
    each end so calls that merely touch do not count. The answer is the largest number of
    intervals that contain one moment, on the head's own clock (no hub/head clock comparison).
    """
    intervals=[]
    for reply in replies:
        end=parse_time(reply.get('created_at'))
        compute=sum(reply.get(k) or 0 for k in ('prompt_eval_duration','eval_duration'))/1e9
        if end is None or compute<=0:raise ValueError('Reply lacks created_at or Ollama durations; overlap cannot be judged')
        start=end-compute;trim=compute*.1
        intervals.append((start+trim,end-trim))
    points=sorted([(s,1) for s,_ in intervals]+[(e,-1) for _,e in intervals],key=lambda p:(p[0],p[1]))
    best=now=0
    for _,step in points:
        now+=step;best=max(best,now)
    return max(1,best),intervals


def parse_time(value):
    """RFC 3339 with up to nanoseconds ('2026-11-17T01:47:29.123456789-05:00' or 'Z') -> epoch seconds."""
    from datetime import datetime
    if not isinstance(value,str):return None
    m=re.match(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$',value.strip())
    if not m:return None
    base=datetime.fromisoformat(m.group(1)+('+00:00' if m.group(3)=='Z' else m.group(3)))
    return base.timestamp()+(float('0'+m.group(2)) if m.group(2) else 0.0)
