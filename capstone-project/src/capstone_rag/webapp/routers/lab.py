"""Human-facing Gateway Testing Lab.  This never contacts Ollama directly."""
from __future__ import annotations
import copy,json,os,re,time,random,threading,io,csv
from pathlib import Path
from fastapi import APIRouter,HTTPException,Response
from ... import config,gateway_transport,experiments
from ..runtime import get_runtime

router=APIRouter(prefix='/api/lab',tags=['Gateway lab heads'])
STATE_BUILD_TIMEOUT_SECONDS=60
# rc.17: per-call output budget for agent runs. 160 cut off the structured
# answers of wordier models (invalid JSON = run error, not a judged answer).
# Each step's own smaller max_tokens still applies.
LAB_OUTPUT_TOKENS=512
_cache={'at':0.0,'value':None,'building':False,'inflight':False,'error':None,'generation':0,'started_at':None}
_cache_lock=threading.Lock()
CATALOG=[
 {'id':'agentic-testing','name':'Agentic Testing','description':'Complete tickets through the real Capstone workflow: intake, investigation, tools, customer exchanges, a separate reviewer agent in place of the human, operational events and case disposition. Compare models, teams and methodologies.','scenario_ids':[],'calls':20,'minutes':6},
 {'id':'capacity-pressure','name':'Sustained model-call pressure','description':'Keep a chosen number of identical, review-free model calls active so Gateway and head concurrency can be measured.','scenario_ids':['qms-technical'],'calls':32,'minutes':10},
 {'id':'agentic-evaluation','name':'Agentic Evaluation','description':'Replay selected cases through the complete 8 × 4 methodology matrix.','scenario_ids':list(experiments.SMOKE),'calls':32*6,'minutes':64},
 {'id':'smoke','name':'Smoke','description':'One technical support case that proves the complete distributed path.','scenario_ids':['qms-technical'],'calls':6,'minutes':2},
 {'id':'safety','name':'Safety','description':'A smoke and burning-smell case; the safety gate must hold.','scenario_ids':['safety-complaint'],'calls':5,'minutes':2},
 {'id':'ticket-triage','name':'Ticket triage','description':'Three reviewed intake and identity cases.','scenario_ids':['ambiguous-accessory','phone-log','fuzzy-identity'],'calls':15,'minutes':6},
 {'id':'qms-lookup','name':'QMS lookup','description':'Technical support using controlled QMS documents.','scenario_ids':['qms-technical'],'calls':6,'minutes':2},
 {'id':'erp-lookup','name':'ERP lookup','description':'A reviewed component case using orders and shipments.','scenario_ids':['component-approval'],'calls':8,'minutes':4},
 {'id':'full','name':'Full set','description':'Every reviewed Testing Lab scenario.','scenario_ids':list(experiments.SMOKE),'calls':sum(6 for _ in experiments.SMOKE),'minutes':max(10,len(experiments.SMOKE)*2)},
 {'id':'erp-identity','name':'ERP identity matrix','description':'Real ERP customers whose first message has correct, missing, mistyped or someone else\'s identity details. A synthetic customer answers the agent from a hidden answer key; results are scored against that key.','scenario_ids':[],'calls':8,'minutes':3},
 {'id':'model-compatibility','name':'Model compatibility check','description':'Screen each selected model on every model-backed role: 3 frozen, versioned tasks per role (normal, tool/evidence, boundary), run through the production prompt for that role. Results feed the compatibility matrix.','scenario_ids':[],'calls':1,'minutes':1},
 {'id':'team-comparison','name':'Team comparison','description':'Run the same cases with a mixed-model team (a model per role, optional per-role context/output budgets) and with each uniform baseline model. Every call is checked: requested model vs. model that executed.','scenario_ids':list(experiments.SMOKE)[:5],'calls':6,'minutes':3},
 {'id':'safety-6-1','name':'Safety evaluation (Checkpoint 6.1)','description':'Synthetic safety cases with expected safe outcomes: correct and wrong revision, superseded procedure, uncertain revision, missing and ambiguous identity, burning smell, adverse event, routine controls, fabricated reference, replacement demand. Scored inside each run.','scenario_ids':[],'calls':10,'minutes':4},
]

def _catalog():
    """Catalog with the ERP identity cases resolved from the operational DB."""
    from ... import lab_cases
    rows=copy.deepcopy(CATALOG)
    try:ids=lab_cases.case_ids()
    except Exception:ids=[]
    from ... import safety_cases
    for row in rows:
        if row['id']=='erp-identity':row['scenario_ids']=ids;row['calls']=8*max(1,len(ids));row['minutes']=3*max(1,len(ids))
        if row['id']=='safety-6-1':row['scenario_ids']=sorted(safety_cases.CASES);row['calls']=10*len(safety_cases.CASES);row['minutes']=4*len(safety_cases.CASES)
        if row['id']=='agentic-testing':
            from ... import agentic_scenarios
            row['scenario_ids']=agentic_scenarios.keys();row['scenario_set_version']=agentic_scenarios.SCENARIO_SET_VERSION
        if row['id']=='model-compatibility':
            from ... import role_screening
            row['scenario_ids']=[role_screening.scenario(t)['id'] for t in role_screening.TASKS];row['calls']=len(role_screening.TASKS);row['minutes']=max(1,len(role_screening.TASKS)//4)
    return rows

def _scenario_rows():
    from ... import lab_cases
    rows=[{'id':key,'name':value['subject'],'category':value['category']} for key,value in experiments.SMOKE.items()]
    try:rows+=lab_cases.catalog()
    except Exception:pass
    from ... import safety_cases
    rows+=[{'id':key,'name':'Safety 6.1 · '+value['policy'],'category':'safety'} for key,value in sorted(safety_cases.CASES.items())]
    return rows

def _gateway_only():
    if not gateway_transport.enabled():raise HTTPException(409,'available only in lab heads (gateway) mode')

# Capstone-side model notes (the Gateway still serves every model; this only
# decides what the Testing Lab offers and how it is labelled). Ratings come
# from the November 2026 Lab campaigns; the Compatibility matrix supersedes
# them per role as screening evidence accrues.
EVIDENCE_SOURCE='Testing Lab campaigns, November 2026 (benchmark workbook)'
MODEL_NOTES=[
  (r'^deepseek-r1',{'excluded':True,'reason':'Removed from Capstone options at the operator\'s direction. In the November campaigns '
     'deepseek-r1 returned no usable answers: Capstone stripped think:false for non-Qwen models, so the answer went into '
     'reasoning output (fixed in rc.20). Still installed on the heads. Set CAPSTONE_LAB_INCLUDE_EXCLUDED=1 to re-screen it.'}),
  # rc.25 (U1): the former 'recommended', 'most accurate' and 'fastest' labels
  # were not supported by controlled comparisons and are removed; scoped,
  # dated evidence comes from model_evidence (tested / discouraged / role).
  (r'^gemma3',{'native_tools':False,'note':'No native tool calling in the observed configuration (Gateway HTTP 400). '
     'An explicit mediated or delegated strategy is required; the default never sends native tools to it.'}),
]
NATIVE_TOOL_FAMILIES=r'^(qwen|llama3\.[123]|granite|mistral|phi4-mini|gpt-oss)'

# Evidence-based starting team for Team comparison (pre-filled, editable):
# collaboration stages on the most accurate model (qwen3:8b), every other role
# on the recommended small model (llama3.2:3b). Budgets stay inside the
# Gateway's 8192-token head qualification; a larger window is refused by the
# Gateway until the heads are re-qualified. The comparison runs the team and
# both uniform baselines on the same cases, which measures the time cost.
TEAM_PRESET={'name':'Example team (editable; makes no performance claim)','evidence':None,
  'role_models':{'supervisor':'qwen3:8b','peer_reviewer':'qwen3:8b','reflection':'qwen3:8b'},
  'team_base_model':'llama3.2:3b',
  'role_budgets':{r:{'num_ctx':8192,'num_predict':512} for r in ('supervisor','peer_reviewer','reflection')},
  'baselines':['llama3.2:3b','qwen3:8b'],
  'note':'A user-configurable starting point only. The controlled comparisons do not establish a best team; choose models per role from scoped evidence.'}

def model_notes(name,detail=None,delegates=None):
    """Labels for one model: operator exclusion, scoped evidence, native tool support."""
    from ... import model_evidence
    value={'excluded':False,'native_tools':None,'note':None,'evidence':model_evidence.REGISTRY_VERSION,
           'presentation':model_evidence.presentation(name,'main',gateway_digest=(detail or {}).get('digest') or (detail or {}).get('model_digest')),
           'reviewer_presentation':model_evidence.presentation(name,'lab_reviewer')}
    # rc.25 field fix: tool support comes from the Gateway's tools test or Ollama's declared capabilities
    # (model_evidence.tool_facts); a completion-only qualified list is NOT "no tools".
    facts=detail if isinstance(detail,dict) and 'tools_state' in detail else model_evidence.tool_facts([detail] if detail else [])
    value['tool_facts']=facts
    native,basis=model_evidence.native_tools_evidence(name,facts)
    if native is not None:
        value['native_tools']=native;value['tools_source']=basis
    elif re.match(NATIVE_TOOL_FAMILIES,name):
        value['native_tools']=True;value['tools_source']='model family (Ollama tool support); not confirmed by the Gateway'
    for pattern,note in MODEL_NOTES:
        if re.match(pattern,name) and not (native is True and 'native_tools' in note):value.update(note)
    if value['native_tools'] is False:value.setdefault('tools_source','Lab campaign: Gateway HTTP 400 on tool requests')
    try:
        value['tool_strategy_default']=model_evidence.resolve_tool_strategy(name,None,facts,delegates=delegates)
    except model_evidence.ToolStrategyIncompatible as exc:
        value['tool_strategy_default']={'strategy':None,'incompatible':True,'reason':str(exc)}
    if value['excluded'] and os.environ.get('CAPSTONE_LAB_INCLUDE_EXCLUDED')=='1':value['excluded']=False;value['note']='(re-screening) '+value['reason']
    return value

def _inventory_path():
    return Path(os.environ.get('CAPSTONE_LAB_HEADS_FILE',r'C:\ProgramData\Capstone\config\lab-heads.json'))

def _number(resource_id):
    match=re.search(r'(\d+)$',resource_id or '')
    return int(match.group(1)) if match else 999

def _size(model,detail):
    for key in ('size_bytes','bytes','model_size_bytes'):
        if detail.get(key):return int(detail[key])
    match=re.search(r':(\d+(?:\.\d+)?)([bm])(?:$|[-_])',model.lower())
    if not match:return None
    value=float(match.group(1));return int(value*(1_000_000_000 if match.group(2)=='b' else 1_000_000))

def _reason(value):
    return {'available':'usable now','busy':'busy right now','not_qualified':'not qualified on this head',
      'resource_unhealthy':'head is unhealthy','resource_stale':'head has not checked in recently',
      'context_exceeds_qualification':'requested context is larger than this head qualified',
      'not_permitted':'model is not allowed','operation_not_permitted':'chat is not allowed',
      'context_exceeds_policy':'requested context is larger than policy allows'}.get(value,str(value or 'not available').replace('_',' '))

def _capacity_steps(body):
    if not body.get('stair_step'):return []
    maximum=max(1,min(1024,int(body.get('stair_maximum',body.get('concurrency',1)))))
    calls=max(1,min(10000,int(body.get('step_calls',32))))
    minutes=max(1,min(1440,int(body.get('step_minutes',5))))
    basis=body.get('step_basis','calls')
    if basis not in ('calls','minutes'):raise ValueError('Stair-step duration must use calls or minutes')
    stop=body.get('stair_stop_rule','maximum')
    if stop not in ('maximum','first_failure','diminishing_returns'):raise ValueError('Unknown stair-step stop rule')
    return [{'step':level,'parallel':level,'calls':max(level,calls) if basis=='calls' else level*2,
      'basis':basis,'minutes':minutes,'duration_seconds':minutes*60 if basis=='minutes' else None,
      'stop_rule':stop} for level in range(1,maximum+1)]

def _agentic_catalog():
    from ... import agentic_scenarios
    return {'version':agentic_scenarios.SCENARIO_SET_VERSION,'scenarios':agentic_scenarios.catalog(),
            'behaviours':agentic_scenarios.BEHAVIOURS,
            'templates':{k:{'title':v['title'],'version':v['version'],'behaviours':v['behaviours']} for k,v in agentic_scenarios.TEMPLATES.items()},
            'presets':AGENTIC_PRESETS}

AGENTIC_PRESETS={
  'vertical-slice':['agentic:power-fault:cooperative','agentic:power-fault:serial_typo_then_corrects','agentic:missing-goggles:cooperative',
                    'agentic:burning-smell:cooperative','agentic:power-fault:impostor'],
  'safety':['agentic:burning-smell:cooperative','agentic:burning-smell:withdraws_safety_report','agentic:skin-burn:cooperative',
            'agentic:fake-approval:cooperative','agentic:qms-conflict:cooperative','agentic:replacement-demand:cooperative',
            'agentic:power-fault:impostor','agentic:order-status:impostor'],
  # Run with the reviewer candidate before relying on it as a campaign
  # control: elicits approval, blocked approval -> revision, movement
  # approval and rejection, identity decline, QA determination, escalation
  # and unresolved closure. Read the "Reviewer quality" table afterwards.
  'reviewer-qualification':['agentic:power-fault:cooperative','agentic:power-fault:serial_typo_then_corrects','agentic:missing-goggles:cooperative',
                            'agentic:missing-goggles:impostor','agentic:burning-smell:cooperative','agentic:qms-conflict:cooperative',
                            'agentic:replacement-demand:cooperative','agentic:fake-approval:cooperative','agentic:power-fault:refuses_identity'],
  'identity':['agentic:power-fault:cooperative','agentic:power-fault:serial_typo_then_corrects','agentic:power-fault:missing_identity_then_provides',
              'agentic:power-fault:dob_instead_of_serial','agentic:power-fault:impostor','agentic:power-fault:refuses_identity','agentic:power-fault:nonresponsive'],
}

def discover_reviewer(models):
    """The Qwen3 4B reviewer candidate's exact installed identifier, if any.

    Never substitutes another family (qwen3.5 is a different model)."""
    names=[m['name'] for m in models]
    exact=[n for n in names if n=='qwen3:4b']
    family=[n for n in names if n.startswith('qwen3:4b')]
    chosen=(exact or sorted(family) or [None])[0]
    return {'requested':'Qwen3 4B','identifier':chosen,'found':bool(chosen),
            'candidates':family,'note':None if chosen else 'Qwen3 4B is not in the Gateway inventory for capstone-research; choose a reviewer model explicitly.'}

def _state_shell():
    from ...agentic_methodologies import catalogue as methodology_catalogue
    return {'mode':'gateway','heads':[],'models':[],'excluded_models':[],'team_preset':TEAM_PRESET,'policy':{},'catalog':_catalog(),
      'agentic':_agentic_catalog(),'reviewer':discover_reviewer([]),
      'methodologies':methodology_catalogue(),'inventory_status':'checking',
      'inventory_message':'Checking the Gateway model inventory…',
      'scenarios':_scenario_rows(),
      'refreshed_at':time.time()}

def _state_log(event,started,error=None,**detail):
    print(json.dumps({'component':'testing_lab.inventory','event':event,
      'duration_ms':round((time.monotonic()-started)*1000,3),'error':error,**detail},sort_keys=True),flush=True)

def _state_timeout(generation,started):
    time.sleep(STATE_BUILD_TIMEOUT_SECONDS)
    timed_out=False
    with _cache_lock:
      if _cache['generation']==generation and _cache['building']:
        message=f'Gateway inventory did not finish within {STATE_BUILD_TIMEOUT_SECONDS} seconds'
        fallback=copy.deepcopy(_cache['value'] or _state_shell())
        fallback.update(inventory_status='partial',inventory_message=message,refreshed_at=time.time())
        _cache.update(value=fallback,error=message,at=time.monotonic(),building=False)
        timed_out=True
    if timed_out:_state_log('timeout',started,error=message)

def _build_state(generation,started):
  _state_log('started',started)
  try:
    client=gateway_transport.client('research')
    inventory=client.models();policy={'source':'authenticated models() inventory'};resources={r['resource_id']:r for r in inventory.get('resources',[])}
    # models() already contains the measured model/head pairs and resource
    # health.  Do not turn opening the page into one 120-second availability
    # request per model.  A run's final server-side preflight still calls
    # availability() for its exact request shape.
    configured=[];path=_inventory_path()
    if path.is_file():
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('schema')!='agent-arena.lab-heads/1':raise ValueError('Unsupported lab-heads.json schema')
        configured=data.get('heads',[])
    heads={h['resource_id']:{**h} for h in configured}
    for rid in resources:heads.setdefault(rid,{'resource_id':rid,'number':_number(rid),'address':rid.replace('head-','').replace('-','.')})
    all_models={}
    for rid,head in heads.items():
        resource=resources.get(rid);models=[]
        if resource:
            health=str(resource.get('status') or resource.get('health') or resource.get('resource_health') or 'healthy')
            for name in resource.get('models',[]):
                detail=(resource.get('model_details') or {}).get(name,{})
                ops=detail.get('qualified_operations') or []
                operation='/api/embed' if ('embedding' in ops and 'completion' not in ops) else '/api/generate'
                kind='embedding' if operation=='/api/embed' else 'chat'
                raw='available' if health in ('healthy','available','online') else health
                usable=raw in ('available','busy')
                row={'name':name,'kind':kind,'qualified':('embedding' if kind=='embedding' else 'completion') in ops,
                     'max_context':(detail.get('execution_policy') or {}).get('max_context'),
                     'usable':usable,'availability':raw,'reason':_reason(raw),'size_bytes':_size(name,detail)}
                models.append(row)
                if kind=='chat':
                    summary=all_models.setdefault(name,{'name':name,'size_bytes':row['size_bytes'],'heads':[],'blocked':{},
                        'digest':detail.get('digest') or detail.get('model_digest'),
                        'qualified_capabilities':detail.get('qualified_capabilities'),'installed':True,**model_notes(name,detail)})
                    summary.setdefault('_details',[]).append(detail)
                    if usable:summary['heads'].append(rid)
                    else:summary['blocked'][rid]=row['reason']
            statuses=[m['availability'] for m in models]
            status='busy' if statuses and all(s=='busy' for s in statuses) else ('unhealthy or stale' if health not in ('healthy','available','online') else 'serving the Testing Lab')
        else:status='in the lab but not serving the Testing Lab';models=[]
        head.update(status=status,serving=bool(resource),models=models,number=head.get('number',_number(rid)))
    from ... import model_evidence
    for model in all_models.values():
        # Tool facts from EVERY head that lists the model (Gateway tools test, Ollama-declared capabilities).
        facts=model_evidence.tool_facts(model.pop('_details',None) or [])
        model.update(**model_notes(model['name'],{**facts,'digest':model.get('digest')}))
    # Default delegates come only from models offered and usable now.
    delegates={m['name']:m['tool_facts'] for m in all_models.values() if m['heads'] and not m['excluded']}
    for model in all_models.values():
        if model.get('tool_strategy_default',{}).get('strategy')!='native':
            try:model['tool_strategy_default']=model_evidence.resolve_tool_strategy(model['name'],None,model['tool_facts'],delegates=delegates)
            except model_evidence.ToolStrategyIncompatible as exc:model['tool_strategy_default']={'strategy':None,'incompatible':True,'reason':str(exc)}
        size=model.get('size_bytes')
        model['group']='small' if size is not None and size<2_000_000_000 else ('large' if size is not None and size>5_000_000_000 else 'medium')
    value={'mode':'gateway','heads':sorted(heads.values(),key=lambda h:h['number']),
      'models':sorted((m for m in all_models.values() if m['heads'] and not m['excluded']),key=lambda m:(m['group'],m['name'])),
      'excluded_models':[{'name':m['name'],'reason':m.get('reason')} for m in sorted(all_models.values(),key=lambda m:m['name']) if m['excluded']],
      # G5 (rc.25): installed but currently unavailable is not "not installed".
      'unavailable_models':[{'name':m['name'],'installed':True,'reasons':m['blocked']} for m in sorted(all_models.values(),key=lambda m:m['name'])
                            if not m['heads'] and not m['excluded']],
      'team_preset':TEAM_PRESET,'agentic':_agentic_catalog(),
      'reviewer':discover_reviewer([m for m in all_models.values() if m['heads'] and not m['excluded']]),
      'policy':policy,'catalog':_catalog(),'methodologies':_state_shell()['methodologies'],
      'scenarios':_scenario_rows(),
      'inventory_status':'ready','inventory_message':None,'refreshed_at':time.time()}
    pairs=sum(len(resource.get('models',[])) for resource in resources.values())
    with _cache_lock:
      if _cache['generation']==generation:
        _cache.update(at=time.monotonic(),value=value,error=None,building=False,inflight=False)
    _state_log('finished',started,heads=len(value['heads']),models=len(value['models']),qualified_pairs=pairs)
  except Exception as exc:
    with _cache_lock:
      if _cache['generation']==generation:
        fallback=copy.deepcopy(_cache['value'] or _state_shell())
        fallback.update(inventory_status='partial',inventory_message='Gateway inventory is temporarily unavailable: '+str(exc),refreshed_at=time.time())
        _cache.update(value=fallback,error=str(exc),at=time.monotonic(),building=False,inflight=False)
    _state_log('finished',started,error=str(exc),heads=0,models=0,qualified_pairs=0)

def state(force=False):
    _gateway_only();now=time.monotonic()
    with _cache_lock:
      fresh=_cache['value'] is not None and now-_cache['at']<25
      if (force or not fresh) and not _cache['inflight']:
        _cache.update(building=True,inflight=True,error=None,generation=int(_cache['generation'])+1,started_at=time.time())
        generation=_cache['generation'];started=time.monotonic()
        threading.Thread(target=_build_state,args=(generation,started),name='capstone-lab-inventory',daemon=True).start()
        threading.Thread(target=_state_timeout,args=(generation,started),name='capstone-lab-inventory-timeout',daemon=True).start()
      value=copy.deepcopy(_cache['value'] or _state_shell())
      if _cache['building']:
        value['inventory_status']='checking'
        value['inventory_message']='Checking the Gateway model inventory…'
      return value

@router.get('/mode')
async def mode():return {'mode':'gateway'}
@router.get('/state')
async def get_state(refresh:bool=False):return state(refresh)

def _selection(body):
    current=state();item=next((x for x in _catalog() if x['id']==body.get('experiment','smoke')),None)
    if not item:raise ValueError('Choose a built-in experiment')
    models=body.get('models') or ['qwen3.5:4b'];heads=body.get('heads') or []
    if item['id']=='team-comparison':
        team={r:m for r,m in (body.get('role_models') or {}).items() if m}
        if not team:raise HTTPException(400,'Assign at least one role to a model for the team')
        # Every model the team or the baselines use must be usable now.
        models=list(dict.fromkeys(list(body.get('models') or [])+list(team.values())+([body['team_base_model']] if body.get('team_base_model') else [])))
    known={m['name']:m for m in current['models']}
    excluded={m['name']:m['reason'] for m in current.get('excluded_models',[])}
    for model in models:
        if model in excluded:raise HTTPException(409,f'{model} is not offered by Capstone: '+excluded[model])
        if model not in known:raise HTTPException(409,f'{model} is not usable now for capstone-research')
        for head in heads:
            if head not in known[model]['heads']:
                raise HTTPException(409,f"{model} cannot run on {head}: "+known[model]['blocked'].get(head,'not available'))
    cap_steps=_capacity_steps(body) if item['id']=='capacity-pressure' else []
    reps=max(1,min(10000 if item['id']=='capacity-pressure' else 100,int((cap_steps[0]['calls'] if cap_steps else body.get('capacity_calls',32)) if item['id']=='capacity-pressure' else body.get('repetitions',1))))
    scenarios=list(item['scenario_ids']);methodology_count=1
    if item['id']=='agentic-evaluation':
        from ... import lab_cases
        from ... import safety_cases
        requested=[value for value in body.get('scenario_ids',[]) if value in experiments.SMOKE or lab_cases.is_case(value) or safety_cases.is_case(value)]
        count=max(1,min(len(scenarios),int(body.get('case_count',1))))
        if requested:scenarios=requested[:count]
        else:scenarios=random.Random(int(body.get('seed',7))).sample(scenarios,count)
        matrix=body.get('methodology_matrix') or {}
        from ...agentic_methodologies import candidates
        methodology_count=len(candidates(matrix.get('topology_ids'),matrix.get('treatment_ids')))
    if item['id']=='agentic-testing':
        from ... import agentic_scenarios
        requested=[v for v in body.get('scenario_ids',[]) if agentic_scenarios.is_scenario(v) and v in item['scenario_ids']]
        scenarios=requested or list(AGENTIC_PRESETS['vertical-slice'])
        matrix=body.get('methodology_matrix') or {'topology_ids':['A'],'treatment_ids':['1']}
        from ...agentic_methodologies import candidates
        methodology_count=len(candidates(matrix.get('topology_ids') or ['A'],matrix.get('treatment_ids') or ['1']))
        reviewer=body.get('reviewer_model') or (current.get('reviewer') or {}).get('identifier')
        if not reviewer:raise HTTPException(409,'No reviewer model: Qwen3 4B is not in the Gateway inventory; choose a reviewer model under Advanced.')
        if reviewer not in known:raise HTTPException(409,f'Reviewer model {reviewer} is not usable now for capstone-research')
        team={r:m for r,m in (body.get('role_models') or {}).items() if m}
        for model in list(team.values())+([body['team_base_model']] if body.get('team_base_model') else []):
            if model not in known:raise HTTPException(409,f'{model} is not usable now for capstone-research')
    if item['id']=='model-compatibility':
        from ... import role_screening
        chosen=[t for t in role_screening.tasks_for(body.get('screening_roles') or None) if not body.get('screening_task_ids') or t['id'] in body['screening_task_ids']]
        if not chosen:raise HTTPException(400,'No screening tasks match the selected roles')
        scenarios=[role_screening.scenario(t)['id'] for t in chosen]
    if item['id'] in ('safety-6-1','erp-identity') and body.get('scenario_ids'):
        # Choose a subset of the category's own cases.
        picked=[v for v in body['scenario_ids'] if v in item['scenario_ids']]
        if picked:scenarios=picked
    if item['id']=='team-comparison':
        from ... import lab_cases,safety_cases
        requested=[v for v in body.get('scenario_ids',[]) if v in experiments.SMOKE or lab_cases.is_case(v) or safety_cases.is_case(v)]
        if requested:scenarios=requested
    calls_per_path=sum(step['calls'] for step in cap_steps) if cap_steps else reps
    agentic_team=item['id']=='agentic-testing' and any((body.get('role_models') or {}).values())
    profile_count=(1+len(body.get('models') or [])) if item['id']=='team-comparison' else len(models)+(1 if agentic_team else 0)
    total=profile_count*len(scenarios)*calls_per_path*max(1,len(heads))*methodology_count
    return current,item,models,heads,reps,total,scenarios,methodology_count

_tool_probe_cache={}

def _tool_probe(model,digest=None,num_ctx=None,strategy=None):
    """Gateway availability for a tool-carrying chat request.

    rc.25 (U1): cached per model build, context and strategy (not by name
    alone) for at most 10 minutes. Informs planning only; it does not
    establish compatibility - qualification and complete runs do."""
    key=(model,digest,num_ctx,strategy)
    cached=_tool_probe_cache.get(key)
    if cached and time.time()-cached['at']<600:return cached
    try:
        check=gateway_transport.client('research').availability(model,'/api/chat',capabilities=['tools'])
        value={'model':model,'status':check.get('status'),'reason':_reason(check.get('reason')) if check.get('reason') else None,'at':time.time()}
    except Exception as exc:
        value={'model':model,'status':'unknown','reason':str(exc)[:160],'at':time.time()}
    value['label']={'available':'tools accepted by the Gateway','busy':'tools accepted (head busy)'}.get(value['status'],'tools not confirmed: '+str(value.get('reason') or value['status']))
    if 'capability_not_qualified' in str(value.get('reason') or '') or 'capability not qualified' in str(value.get('reason') or '').lower():
        # The Gateway's separate tools test was never run for this model; it does not block tool calls (rc.25 field fix).
        value['label']='Gateway tools test not run (tool calls are not blocked)'
    value['notes']=model_notes(model).get('native_tools')
    value['cache_key']={'model':model,'digest':digest,'num_ctx':num_ctx,'strategy':strategy}
    _tool_probe_cache[key]=value
    return value

def _tool_caps(current):
    """{model: tool facts} for every offered model (falls back to the qualified list of an older state)."""
    return {m['name']:(m.get('tool_facts') or m.get('qualified_capabilities')) for m in current.get('models') or []}

def _delegate_candidates(current,heads):
    """Models that may take delegated tool calls: offered, usable now and, when heads are chosen, usable on each."""
    return {m['name']:(m.get('tool_facts') or m.get('qualified_capabilities')) for m in current.get('models') or []
            if m.get('heads') and not m.get('excluded') and all(h in m['heads'] for h in heads)}

def _default_tool_strategies(current,body,models):
    """The operator's explicit choices plus, for every other model without confirmed native tools, the default
    delegate the review screen showed. Explicit choices are never changed."""
    from ... import model_evidence
    chosen=dict(body.get('tool_strategies') or {})
    caps=_tool_caps(current);delegates=_delegate_candidates(current,body.get('heads') or [])
    for m in models:
        if m in chosen:continue
        try:r=model_evidence.resolve_tool_strategy(m,None,caps.get(m),delegates=delegates)
        except model_evidence.ToolStrategyIncompatible:continue
        if r.get('strategy')=='delegated':chosen[m]={'strategy':'delegated','delegate_model':r['delegate_model']}
    return chosen

def _agentic_review(current,body,models):
    """Campaign review: roles each methodology cell activates, tool probes,
    reviewer and limits."""
    from ...agentic_methodologies import candidates
    from ...agentic_coordinator import DEFAULT_LIMITS
    matrix=body.get('methodology_matrix') or {'topology_ids':['A'],'treatment_ids':['1']}
    cells=[]
    for cell in candidates(matrix.get('topology_ids') or ['A'],matrix.get('treatment_ids') or ['1']):
        topology,treatment=cell['methodology']['topology'],cell['methodology']['treatment']
        stages=[r for r,on in (('supervisor',topology.get('supervisor_checkpoints')),('peer_reviewer',topology.get('peer_exchange')),
                                 ('reflection',treatment.get('reflection')),('critic/judge (communication_review, independent_validation)',topology.get('debate')),
                                 ('tree-of-thought planning',treatment.get('force_tree'))) if on]
        cells.append({'id':cell['id'],'name':cell['name'],'stage_roles_active':stages,'stage_coverage':cell.get('stage_coverage') or [],
                      'advisory':cell.get('advisory'),'registry_version':cell.get('registry_version'),
                      'stage_roles_not_exercised':[r for r in ('supervisor','peer_reviewer','reflection') if r not in stages]})
    team={r:m for r,m in (body.get('role_models') or {}).items() if m}
    probe_models=sorted(set(models)|set(team.values())|({body['team_base_model']} if body.get('team_base_model') else set()))
    strategies=body.get('tool_strategies') or {}
    reviewer=body.get('reviewer_model') or (current.get('reviewer') or {}).get('identifier')
    from ...agentic_reviewer import REVIEWER_POLICY_VERSION
    from ... import model_evidence
    caps=_tool_caps(current)
    digests={m['name']:m.get('digest') for m in current.get('models') or []}
    delegates=_delegate_candidates(current,body.get('heads') or [])
    def resolved(m):
        # The SAME resolver the experiment builder launches with (T1/U1).
        try:return model_evidence.resolve_tool_strategy(m,strategies.get(m),caps.get(m),delegates=delegates)
        except model_evidence.ToolStrategyIncompatible as exc:return {'strategy':None,'incompatible':True,'reason':str(exc)}
    probes=[]
    for m in probe_models:
        r=resolved(m)
        probes.append({**_tool_probe(m,digests.get(m),8192,r.get('strategy')),'declared_strategy':r.get('strategy') or 'incompatible',
                       'resolution':r,'delegate_model':r.get('delegate_model') or (strategies.get(m) or {}).get('delegate_model')})
    return {'cells':cells,'tool_probes':probes,
            'reviewer':{'model':reviewer,'discovered':current.get('reviewer'),'policy':REVIEWER_POLICY_VERSION,
                        'fixed':True,'note':'The reviewer model and policy stay fixed across the configurations compared.'},
            'limits':{**DEFAULT_LIMITS,**(body.get('agentic_limits') or {})},
            'team':team or None,'team_base_model':body.get('team_base_model'),
            'context_note':'Heads are qualified at the context shown per model in the Heads tab; a larger requested window is refused by the Gateway, not silently reduced.'}

@router.post('/plan')
def plan(body:dict):
    current,item,models,heads,reps,total,scenarios,methodology_count=_selection(body)
    steps=_capacity_steps(body) if item['id']=='capacity-pressure' else []
    concurrency=max(1,min(1024,int((steps[-1]['parallel'] if steps else body.get('concurrency',1)))))
    eligible={model:current_model['heads'] for model in models for current_model in current['models'] if current_model['name']==model}
    eligible_count=max(1,min([len(v) for v in eligible.values()] or [1]))
    expected_waves=max(1,(concurrency+eligible_count-1)//eligible_count)
    # Team comparison: the team counts as one configuration next to each baseline.
    configurations=(1+len(body.get('models') or [])) if item['id']=='team-comparison' else len(models)
    agentic=None
    if item['id']=='agentic-testing':
        team=any((body.get('role_models') or {}).values());configurations=len(models)+(1 if team else 0)
        agentic=_agentic_review(current,body,models)
    return {'runs':total,'models':configurations,'cases':len(scenarios),'repetitions':reps,'agentic':agentic,
      'heads':heads,'minutes':max(1,total*2),
      'parallel_flows':min(total,concurrency),'waves':max(1,(total+concurrency-1)//concurrency),
      'selected_scenarios':scenarios,'methodologies':methodology_count,
      'capacity_pressure':item['id']=='capacity-pressure',
      'stair_steps':steps,'eligible_heads_by_model':eligible,
      'expected_wait':{'eligible_heads':eligible_count,'parallel_flows':concurrency,'minimum_head_waves':expected_waves,
        'message':f'{concurrency} requested flows across {eligible_count} eligible heads: at least {expected_waves} head wave(s), before model execution time.'},
      'formula':{'cases':len(scenarios),'methodologies':methodology_count,'models':configurations,'heads':max(1,len(heads)),'repetitions':reps},
      'confirmation_required':total>50}

@router.post('/start')
def start(body:dict):
    current,item,models,heads,reps,total,scenarios,methodology_count=_selection(body)
    review_mode=body.get('review_mode','automated' if item['id'] in ('agentic-evaluation','erp-identity','safety-6-1') else 'scripted')
    if item['id'] in ('erp-identity','safety-6-1') and review_mode=='scripted':review_mode='automated'
    if review_mode not in ('interactive','automated','autonomous_sandbox','scripted'):raise ValueError('Unknown review mode')
    capacity=item['id']=='capacity-pressure'
    steps=_capacity_steps(body) if capacity else []
    if capacity:
        gateway=gateway_transport.client('research');num_ctx=int((body.get('parameters') or {}).get('num_ctx',8192))
        for model in models:
            check=gateway.availability(model,'/api/generate',num_ctx=num_ctx)
            if check.get('status') not in ('available','busy'):
                raise HTTPException(409,f'{model} is not eligible for this capacity request: '+_reason(check.get('reason') or check.get('status')))
    spec={'name':item['name'],'profile_ids':['gateway-default'],'gateway_models':models,'gateway_head_ids':heads,
      'scenario_ids':scenarios,'repetitions':reps,'concurrency':int(body.get('concurrency',1)),
      'token_limit':140 if capacity else int(body.get('token_limit',LAB_OUTPUT_TOKENS)),'timeout_seconds':int(body.get('timeout_seconds',600)),
      'approval_mode':'scripted' if capacity else review_mode,'seed':int(body.get('seed',7)),'failure_limit':total if body.get('continue_after_failures',item['id'] in ('agentic-evaluation','capacity-pressure')) else 3,'parameters':body.get('parameters') or {},
      'level':'specialist' if capacity else 'system','specialist_roles':['ticket_interpretation'] if capacity else ['ticket_interpretation'],
      'criteria':list(experiments.CRITERIA),'capacity_pressure':capacity,'fixed_seed':capacity,
      'capacity_step_plan':steps,'stair_step':bool(steps),'stair_stop_rule':body.get('stair_stop_rule','maximum')}
    if capacity:spec['capacity_preflight_verified']=True
    if steps:
        spec['concurrency']=steps[-1]['parallel'];spec['max_trials']=sum(step['calls'] for step in steps)*max(1,len(models))*max(1,len(heads))
    if item['id']=='agentic-evaluation':spec['methodology_matrix']=body.get('methodology_matrix') or {}
    if item['id']=='agentic-testing':
        spec.update(approval_mode='agentic_reviewer',reviewer_model=body.get('reviewer_model') or (state().get('reviewer') or {}).get('identifier'),
                    methodology_matrix=body.get('methodology_matrix') or {'topology_ids':['A'],'treatment_ids':['1']},
                    role_models={r:m for r,m in (body.get('role_models') or {}).items() if m},role_budgets=body.get('role_budgets') or {},
                    team_base_model=body.get('team_base_model'),tool_strategies=_default_tool_strategies(current,body,models),
                    gateway_capabilities=_tool_caps(current),
                    agentic_limits=body.get('agentic_limits') or {},failure_limit=max(1,total),
                    timeout_seconds=min(3600,max(int(body.get('timeout_seconds',0) or 0),60*int((body.get('agentic_limits') or {}).get('max_minutes',25))+120)))
    if item['id']=='model-compatibility':
        spec.update(level='role_screen',scenario_ids=[],approval_mode='scripted',failure_limit=max(1,total),
                    screening_roles=body.get('screening_roles') or None,screening_task_ids=body.get('screening_task_ids') or None,
                    diagnoses_attempt_id=body.get('diagnoses_attempt_id'),role_budgets=body.get('role_budgets') or {})
    if item['id']=='team-comparison':
        spec.update(role_models={r:m for r,m in (body.get('role_models') or {}).items() if m},
                    role_budgets=body.get('role_budgets') or {},team_base_model=body.get('team_base_model'),
                    gateway_models=list(body.get('models') or []),failure_limit=max(1,total))
    preview=experiments.preview(get_runtime().inference_registry,spec)
    confirmation=f'RUN {total} ISOLATED CASES' if total>100 else ''
    return get_runtime().experiments.launch(preview,get_runtime().store,confirmation)

RUN_DISPLAY_LIMIT=500
_STATUS_ORDER={'running':0,'paused':1,'failed':2,'completed':3,'queued':4,'not_run':5,'cancelled':6}

def _classify(run):
    error=str(run.get('error') or '').lower()
    return run.get('error_classification') or ('gateway_queue_capacity' if 'queue capacity exhausted' in error else 'gateway_timeout' if 'timed out' in error else 'capstone_execution_error')

def _comparisons(runs):
    groups={}
    for run in runs:
        key=(run.get('methodology_id') or 'legacy',run.get('configuration_id') or run.get('model'),run.get('head') or run.get('requested_head') or 'pending')
        group=groups.setdefault(key,{'methodology_id':key[0],'topology_id':run.get('topology_id'),'treatment_id':run.get('treatment_id'),
            'model':run.get('configuration_label') or run.get('model'),'configuration_id':run.get('configuration_id'),'head':key[2],'runs':0,'scored':0,'correct':0,'safety_violations':0,'model_calls':0,'handoffs':0,
            'times':[],'queues':[],'heads_used':set()})
        group['runs']+=1;group['model_calls']+=run.get('model_calls') or 0;group['handoffs']+=run.get('handoffs') or 0
        group['unsafe_proposals_blocked']=group.get('unsafe_proposals_blocked',0)+(run.get('proposal_verdict')=='unsafe proposal blocked')
        if run.get('correct') is not None:group['scored']+=1;group['correct']+=bool(run['correct'])
        group['safety_violations']+=run.get('safety_violations') or 0
        if run.get('duration_ms') is not None and run.get('status') in ('completed','failed','paused'):group['times'].append(float(run['duration_ms']))
        if run.get('queue_ms') is not None:group['queues'].append(float(run['queue_ms']))
        group['heads_used'].update(run.get('heads_observed') or [])
    rows=[]
    for group in groups.values():
        times=sorted(group.pop('times'));queues=group.pop('queues');heads=sorted(group.pop('heads_used'))
        mean=sum(times)/len(times) if times else None
        rows.append({**group,'accuracy':group['correct']/group['scored'] if group['scored'] else None,
            'mean_case_ms':mean,'p95_case_ms':times[min(len(times)-1,max(0,int(len(times)*.95)-1))] if times else None,
            'mean_queue_ms':sum(queues)/len(queues) if queues else None,'cases_per_hour':3600000/mean if mean else None,'heads_used':heads})
    return sorted(rows,key=lambda row:(str(row['methodology_id']),str(row['model']),str(row['head'])))

def _team_comparison(runs):
    """Team vs. uniform baselines on the same (paired) cases.

    Only cases every configuration finished are compared, so a configuration
    is never credited for skipping hard cases. Time cost = median case time
    relative to each baseline; per-role time shows where the cost lands.
    """
    if not any(r.get('is_team') for r in runs):return None
    # Pair only within one reviewer configuration, evidence class and scenario
    # version; other partitions are reported separately, never pooled.
    partitions={}
    for r in runs:partitions.setdefault((r.get('reviewer_id'),r.get('evidence_class'),r.get('scenario_set_version')),[]).append(r)
    if len(partitions)>1:
        results=[]
        for key,members in partitions.items():
            value=_team_comparison(members) if len(partitions)>1 and any(m.get('is_team') for m in members) else None
            if value:results.append({**value,'partition':{'reviewer_id':key[0],'evidence_class':key[1],'scenario_set_version':key[2]}})
        if not results:return None
        primary=max(results,key=lambda v:sum(r['runs'] for r in v['rows']))
        return {**primary,'other_partitions':[r for r in results if r is not primary],
                'note':primary['note']+' Runs with a different reviewer, evidence class or scenario version are compared separately.'}
    done=[r for r in runs if r.get('status') in ('completed','paused','failed')]
    # Identity, not label: two teams with the same models in different roles stay separate.
    ident=lambda r:r.get('configuration_id') or r.get('model')
    labels={ident(r):(r.get('configuration_label') or r.get('model')) for r in done}
    configs=sorted({ident(r) for r in done},key=lambda m:(not any(r.get('is_team') for r in done if ident(r)==m),str(labels.get(m))))
    cases={c for c in {r.get('case') for r in done} if all(any(r.get('case')==c and ident(r)==m for r in done) for m in configs)}
    def median(values):
        values=sorted(values);return values[len(values)//2] if values else None
    rows=[]
    for config in configs:
        mine=[r for r in done if ident(r)==config and r.get('case') in cases]
        scored=[r for r in mine if r.get('correct') is not None]
        roles={}
        for r in mine:
            for role,ms in (r.get('role_ms') or {}).items():roles.setdefault(role,[]).append(ms)
        rows.append({'configuration':labels.get(config),'configuration_id':config,'is_team':any(r.get('is_team') for r in mine),'runs':len(mine),
            'accuracy':sum(bool(r['correct']) for r in scored)/len(scored) if scored else None,
            'failed':sum(r.get('status')=='failed' for r in mine),
            'safety_violations':sum(r.get('safety_violations') or 0 for r in mine),
            'unsafe_proposals_blocked':sum(r.get('proposal_verdict')=='unsafe proposal blocked' for r in mine),
            'routing_unverified':sum(r.get('routing_verified') is False for r in mine),
            'median_case_ms':median([float(r['duration_ms']) for r in mine if r.get('duration_ms')]),
            'median_role_ms':{role:median(v) for role,v in sorted(roles.items())}})
    team=next((r for r in rows if r['is_team']),None)
    for row in rows:
        if team and not row['is_team'] and row['median_case_ms'] and team['median_case_ms']:
            row['team_time_vs_this']=team['median_case_ms']/row['median_case_ms']
    return {'paired_cases':sorted(cases),'rows':rows,
            'note':'Paired on cases every configuration finished. Runs with unverified routing are counted but should not be used as evidence.'}

def _agentic_results(eid):
    """Configuration x methodology outcomes from complete runs, plus role findings."""
    from ... import agentic_store
    registry=get_runtime().experiments.registry
    outcomes=agentic_store.rows(registry,'agentic_run_outcomes',eid)
    groups={}
    for o in outcomes:
        # Identity, not label: distinct role assignments, budgets, tool
        # strategies or methodology never merge; different reviewer
        # configurations, evidence classes or scenario versions are never
        # pooled by default.
        legacy=any(o.get(k) is None for k in agentic_store.COMPARABILITY)
        identity=o.get('configuration_id') if not legacy else agentic_store.LEGACY_IDENTITY
        key=('LEGACY',o['run_id']) if legacy else tuple(o.get(k) for k in agentic_store.COMPARABILITY)
        g=groups.setdefault(key,{'configuration_id':identity,'configuration':o.get('configuration_label') or o['configuration'],'methodology':o['methodology'],
            'reviewer_id':o.get('reviewer_id'),'scenario_set_version':o.get('scenario_set_version'),'evaluator_version':o.get('evaluator_version'),
            'identity_status':agentic_store.LEGACY_IDENTITY if legacy else 'complete',
            'reviewer_model':o['reviewer_model'],'tool_strategy':o['tool_strategy'],'evidence_class':set(),'runs':0,'completed':0,'correct':0,
            'eligible':0,'not_attributable':0,'objective_achieved':0,'process_unsafe':0,'process_unverified':0,
            'disposition_correct':0,'released_harm':0,'unsafe_proposals_stopped':0,'reviewer_errors':0,'system_errors':0,
            'unnecessary_escalation':0,'unnecessary_abstention':0,'limit_or_failure':0,'turns':[],'durations':[],'tokens':0,'truncated':0,
            'routing_unverified':0,'terminals':{},'causes':{}})
        g['runs']+=1;g['completed']+=o['completed'];g['correct']+=o['complete_case_correct'];g['disposition_correct']+=o['disposition_correct']
        g['eligible']+=o.get('eligible_for_conclusions') or 0;g['not_attributable']+=o.get('attributable')==0
        g['objective_achieved']+=o.get('objective_achieved') or 0
        g['process_unsafe']+=o.get('process_safe')==0;g['process_unverified']+=o.get('configuration_id') is not None and o.get('process_safe') is None
        g['released_harm']+=o['released_harm'];g['unsafe_proposals_stopped']+=o['unsafe_proposals_stopped']
        g['reviewer_errors']+=o['reviewer_errors'];g['system_errors']+=o['system_errors']
        g['unnecessary_escalation']+=o['unnecessary_escalation'];g['unnecessary_abstention']+=o['unnecessary_abstention']
        g['limit_or_failure']+=o['terminal'] in ('limit_reached','execution_failure');g['evidence_class'].add(o['evidence_class'])
        g['turns'].append(o['customer_turns'] or 0)
        if o['duration_ms']:g['durations'].append(o['duration_ms'])
        g['tokens']+=(o['prompt_tokens'] or 0)+(o['output_tokens'] or 0);g['truncated']+=o['truncated_calls'] or 0
        g['routing_unverified']+=o['routing_verified']==0
        g['terminals'][o['terminal']]=g['terminals'].get(o['terminal'],0)+1
        for cause in json.loads(o['failure_causes'] or '[]'):g['causes'][cause]=g['causes'].get(cause,0)+1
    rows=[]
    for g in groups.values():
        turns,durations=sorted(g.pop('turns')),sorted(g.pop('durations'))
        g['median_customer_turns']=turns[len(turns)//2] if turns else None
        g['median_duration_ms']=durations[len(durations)//2] if durations else None
        g['evidence_class']=sorted(g['evidence_class'])
        g['top_causes']=sorted(g.pop('causes').items(),key=lambda kv:-kv[1])[:6]
        g['sample_note']='small sample: not a basis for broad suitability' if g['runs']<20 else None
        rows.append(g)
    # Every summary uses the same comparability boundary (store.COMPARABILITY).
    return {'groups':sorted(rows,key=lambda r:(str(r['configuration']),str(r['configuration_id']))),
            'reviewer_quality':agentic_store.reviewer_quality(registry,eid),
            'role_findings':agentic_store.role_findings(registry,eid),
            'role_decisions':agentic_store.role_decision_summary(registry,eid),
            'exports':{t:f'/api/lab/agentic/{eid}/{t}.csv' for t in AGENTIC_EXPORTS},
            'note':'Grouped by configuration identity (role assignments, budgets, tool strategy, methodology); reviewer, evidence class and scenario version are never pooled. '
                   'Only runs eligible for conclusions (justified terminal state, attributable execution) support model claims. A blocked unsafe proposal is not safe model behaviour.'}

AGENTIC_EXPORTS=('agentic_run_outcomes','agentic_reviewer_decisions','agentic_exchanges','agentic_tool_interactions','agentic_role_decisions',
                 'role_findings','role_decision_summary','reviewer_quality')

@router.get('/agentic/{eid}/{table}.csv')
def agentic_csv(eid:str,table:str):
    from ... import agentic_store
    if table not in AGENTIC_EXPORTS:
        raise HTTPException(404,'Unknown Agentic Testing export')
    text=agentic_store.csv_export(get_runtime().experiments.registry,table,eid)
    return Response(text,media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{eid}-{table}.csv"'})

@router.get('/results')
def results(experiment_id:str|None=None,limit:int=RUN_DISPLAY_LIMIT):
    """Live campaign view. Reads only compact run summaries (one indexed
    query); never opens per-run files or queries the live operations DB."""
    manager=get_runtime().experiments;history=manager.history()[:100]
    history_rows=[{'id':exp['id'],'name':exp.get('name') or ('Agentic Evaluation' if exp.get('methodology_matrix') else exp['id']),
        'status':exp.get('status'),'created_at':exp.get('created_at'),'review_mode':exp.get('approval_mode')} for exp in history]
    selected=next((exp for exp in history if exp['id']==experiment_id),None) if experiment_id else None
    selected=selected or (history[0] if history else None)
    if not selected:return {'history':history_rows,'experiments':[]}
    exp=selected;runs=manager.summaries(exp['id'])
    count=lambda status:sum(run.get('status')==status for run in runs)
    failure_limit=int(exp.get('failure_limit',3));failed=count('failed');queued=count('queued')
    termination_reason=exp.get('termination_reason')
    if not termination_reason and exp.get('status')=='incomplete' and queued and failed>=failure_limit:termination_reason='failure_limit_reached'
    inactive=exp.get('status')!='running' or bool(exp.get('stop_requested')) or bool(termination_reason)
    for run in runs:
        run['stored_status']=run.get('status')
        if run.get('status')=='queued' and inactive:
            run['status']='not_run'
            run['error']=run.get('error') or ('Not run: campaign stopped after reaching its failure limit.' if termination_reason=='failure_limit_reached'
                else 'Not run: campaign dispatch stopped; this preserved trial is inactive.')
        run['trace_url']='/api/experiments/runs/'+run['id']+'/trace'
    error_groups={}
    for run in runs:
        if run.get('stored_status')!='failed':continue
        group=error_groups.setdefault(_classify(run),{'classification':_classify(run),'count':0,'example':run.get('error')})
        group['count']+=1
    ordered=sorted(runs,key=lambda run:(_STATUS_ORDER.get(run.get('status'),9),-(run.get('attempt_started_at') or run.get('queued_at') or 0)))
    limit=max(1,min(10000,int(limit)))
    return {'history':history_rows,'poll_after_ms':3000 if exp.get('status') in ('running','stopping','cancelling') else 15000,'experiments':[{
        'id':exp['id'],'name':exp.get('name') or ('Agentic Evaluation' if exp.get('methodology_matrix') else exp['id']),
        'status':exp.get('status'),'stop_requested':bool(exp.get('stop_requested')),'termination_reason':termination_reason,
        'failure_limit':failure_limit,'planned_runs':exp.get('planned_runs'),'concurrency':int(exp.get('concurrency') or 1),
        'peak_active_workers':exp.get('peak_active_workers'),'business_services':exp.get('business_services'),
        'completed_runs':count('completed'),'failed_runs':failed,'paused_runs':count('paused'),
        'active_runs':count('running'),'queued_runs':0 if inactive else queued,'not_run_runs':queued if inactive else 0,
        'error_groups':list(error_groups.values()),'created_at':exp.get('created_at'),'review_mode':exp.get('approval_mode'),
        'runs':ordered[:limit],'runs_total':len(runs),'comparisons':_comparisons(runs),'team_comparison':_team_comparison(runs),
        'agentic':_agentic_results(exp['id']) if exp.get('approval_mode')=='agentic_reviewer' else None,
        'capacity_steps':exp.get('capacity_steps') or [],'diminishing_returns_at':exp.get('diminishing_returns_at')}]}

@router.get('/{eid}/capacity-steps.json')
def capacity_steps_json(eid:str):
    exp=get_runtime().experiments.registry.get('experiments',eid)
    if not exp:raise HTTPException(404,'Unknown experiment')
    return {'experiment_id':eid,'status':exp.get('status'),'diminishing_returns_at':exp.get('diminishing_returns_at'),'steps':exp.get('capacity_steps') or []}

@router.get('/{eid}/capacity-steps.csv')
def capacity_steps_csv(eid:str):
    exp=get_runtime().experiments.registry.get('experiments',eid)
    if not exp:raise HTTPException(404,'Unknown experiment')
    rows=exp.get('capacity_steps') or [];stream=io.StringIO()
    fields=['step','parallel','calls_planned','calls_completed','calls_failed','average_latency_ms','p95_latency_ms','average_tokens_per_second_per_call','total_tokens_per_second','calls_per_second','average_queue_wait_ms','heads_served','failure_classes']
    writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    for row in rows:writer.writerow({key:json.dumps(row.get(key),sort_keys=True) if isinstance(row.get(key),(dict,list)) else row.get(key) for key in fields})
    return Response(stream.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{eid}-capacity-steps.csv"'})

@router.post('/runs/{rid}/automatic-review')
def automatic_review(rid:str,body:dict):
    manager=get_runtime().experiments;run=manager.registry.get('experiment_runs',rid)
    if not run:raise HTTPException(404,'Unknown run')
    mode=body.get('mode','automated')
    if mode not in ('automated','autonomous_sandbox'):raise ValueError('Choose automated or autonomous sandbox review')
    return manager.command(rid,{'action':'automatic_review','mode':mode,'fixture':run['scenario'],
      'authorizer':'testing-lab-'+mode,'rationale':'Explicit operator request to complete review only inside this isolated synthetic run.'})

# --- Model compatibility check (rc.20) -------------------------------------

@router.get('/screening/inventory')
def screening_inventory():
    """Model-backed roles with their tasks, and the deterministic workers."""
    from ... import role_screening,role_contracts
    return {'task_set_version':role_screening.TASK_SET_VERSION,
      'model_roles':[{'role':r,'contract_hash':role_contracts.contract_hash(r)[:12],
        'tasks':[{'id':t['id'],'version':t['version'],'kind':t['kind'],'title':t['title']} for t in role_screening.TASKS if t['role']==r]}
        for r in role_screening.MODEL_ROLES],
      'deterministic_workers':[{'name':k,'implementation':v} for k,v in role_screening.DETERMINISTIC_WORKERS.items()],
      'statuses':['passed','failed','inconclusive_blocked','not_tested']}

def _split(value):return [v for v in (value or '').split(',') if v] or None

@router.get('/screening/matrix')
def screening_matrix(models:str|None=None,roles:str|None=None):
    from ... import screening_store
    # Live Gateway digests (when the inventory reports them) mark evidence
    # from older weights stale.
    with _cache_lock:value=copy.deepcopy(_cache['value']) if _cache['value'] else {}
    digests={m['name']:m.get('digest') for m in value.get('models',[]) if m.get('digest')}
    return {'cells':screening_store.eligibility(get_runtime().inference_registry,models=_split(models),roles=_split(roles),current_digests=digests),
            'live_digests':len(digests)}

@router.get('/screening/attempts')
def screening_attempts(model:str|None=None,role:str|None=None,experiment_id:str|None=None):
    from ... import screening_store
    rows=screening_store.attempts(get_runtime().inference_registry,model=model,role=role,experiment_id=experiment_id)
    for row in rows:row['payload']=json.loads(row['payload'])
    return {'attempts':rows[-500:]}

@router.get('/screening/export.csv')
def screening_export(kind:str='summary',models:str|None=None,roles:str|None=None):
    from ... import screening_store
    if kind not in ('summary','attempts'):raise HTTPException(400,'kind must be summary or attempts')
    text=(screening_store.csv_export(get_runtime().inference_registry,'attempts') if kind=='attempts'
          else screening_store.csv_export(get_runtime().inference_registry,'summary',models=_split(models),roles=_split(roles)))
    return Response(text,media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="model-compatibility-{kind}.csv"'})

@router.post('/screening/diagnose')
def screening_diagnose(body:dict):
    """Plan (not start) a budget diagnostic linked to one failed attempt."""
    from ... import screening_store
    try:return screening_store.diagnostic_spec(get_runtime().inference_registry,str(body.get('attempt_id','')))
    except ValueError as exc:raise HTTPException(400,str(exc))

@router.get('/screening/team-seed')
def screening_team_seed(models:str|None=None):
    """Per role: models whose evidence is current and passed, fastest first.

    A suggestion with its data for the operator to accept or change; nothing
    is activated.
    """
    from ... import screening_store
    cells=screening_store.eligibility(get_runtime().inference_registry,models=_split(models))
    by_role={}
    for cell in cells:
        if cell['status']=='passed' and not model_notes(cell['model'])['excluded']:by_role.setdefault(cell['role'],[]).append(cell)
    seed={role:sorted(rows,key=lambda c:(c['median_passed_ms'] is None,c['median_passed_ms'] or 0))[0]['model'] for role,rows in by_role.items()}
    untested=sorted({c['role'] for c in cells}-set(seed))
    return {'role_models':seed,'roles_without_eligible_model':untested,
      'candidates':{role:[{'model':c['model'],'median_passed_ms':c['median_passed_ms'],'required_num_ctx':c['required_num_ctx']} for c in rows] for role,rows in by_role.items()}}
