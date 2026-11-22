"""Experiment controls and evidence, integrated with the existing app runtime."""
import csv,io,json,os,sqlite3,tempfile,threading,time,zipfile
from pathlib import Path
from starlette.background import BackgroundTask
from fastapi import APIRouter,HTTPException
from fastapi.responses import FileResponse,Response
from ... import experiments
from ..runtime import get_runtime
router=APIRouter(prefix='/api/experiments',tags=['isolated experiments'])
@router.get('/modes')
def modes():
    from ...experiment_modes import contract
    return contract()
@router.get('/components')
def components():
    from ...experimental_architecture import registry
    return registry()
@router.get('/architectures/templates')
def architecture_templates():
    from ...experimental_architecture import TEMPLATES,template
    return {'templates':[template(key) for key in TEMPLATES]}
@router.post('/architectures/validate')
def validate_architecture(body:dict):
    from ...experimental_architecture import assemble
    return assemble(body)
@router.get('/architectures')
def architectures():
    from ...experimental_architecture import ArchitectureStore
    return {'architectures':ArchitectureStore(manager().registry).list()}
@router.post('/architectures')
def save_architecture(body:dict):
    from ...experimental_architecture import ArchitectureStore
    return ArchitectureStore(manager().registry).save(body.get('architecture') or {},body.get('authorizer'))
def _configured_model_requests():
    """Return requested names without implying Gateway qualification or availability."""
    registry=get_runtime().inference_registry
    requested={}
    for profile in registry.list('profiles'):
        for role,route in profile.get('routes',{}).items():
            model=route.get('model')
            if model:
                requested.setdefault(model,[]).append({'profile_id':profile['id'],'role':role,
                    'enabled':route.get('enabled',True)})
    return [{'model_id':key,'requested_by':sorted(value,key=lambda row:(row['profile_id'],row['role'])),
        'qualification':'not_checked','currently_available':None}
        for key,value in sorted(requested.items())]
@router.get('/models/discovery')
def model_discovery():
    from ... import gateway_transport
    if not gateway_transport.enabled():
        return {'format':'capstone-model-research-v1','status':'gateway_not_configured',
            'contract':'shared.inference/1','source':'configured_profile_requests',
            'models':[],'configured_requests':_configured_model_requests(),
            'note':'Configured model names are requests only; qualification and availability were not checked.'}
    from ...model_research import discover
    value=discover(gateway_transport.client('research'))
    value['status']='discovered' if value.get('gateway_status')=='authenticated' else 'gateway_unverified'
    value['configured_requests']=_configured_model_requests()
    return value
@router.post('/models/suitability')
def model_suitability(body:dict):
    from ...model_research import SuitabilityStore,suitability
    inventory=body.get('inventory')
    if not isinstance(inventory,dict) or inventory.get('source')!='authenticated_gateway':
        raise ValueError('An authenticated normalized Gateway inventory is required')
    evidence=SuitabilityStore(manager().registry).list()
    return suitability(inventory,evidence)
@router.post('/models/suitability/evidence')
def save_model_suitability(body:dict):
    from ...model_research import SuitabilityStore
    return SuitabilityStore(manager().registry).record(body.get('evidence') or {},body.get('authorizer'))
@router.post('/models/exploration/plan')
def plan_model_exploration(body:dict):
    from ...model_research import SuitabilityStore,exploration_plan,suitability
    inventory=body.get('inventory')
    if not isinstance(inventory,dict) or inventory.get('source')!='authenticated_gateway':
        raise ValueError('An authenticated normalized Gateway inventory is required')
    report=suitability(inventory,SuitabilityStore(manager().registry).list())
    return exploration_plan(inventory,report,body.get('policy') or {})
@router.post('/curator/snapshots')
def curator_snapshot(body:dict):
    if not str(body.get('authorizer','')).strip():raise ValueError('Named operator required')
    from ...document_curator import create_snapshot
    return create_snapshot()
@router.post('/curator/chunks')
def curator_chunks(body:dict):
    if not str(body.get('authorizer','')).strip():raise ValueError('Named operator required')
    from ...document_curator import build_chunks
    return build_chunks(body.get('snapshot_hash',''),body.get('strategy',''),body.get('settings'))
@router.get('/curator/chunks/{artifact_hash}/verify')
def curator_verify_chunks(artifact_hash:str):
    from ...document_curator import verify_chunks
    return verify_chunks(artifact_hash)
@router.post('/curator/indexes')
def curator_index(body:dict):
    if not str(body.get('authorizer','')).strip():raise ValueError('Named operator required')
    from ...document_curator import build_index
    return build_index(body.get('chunk_artifact_hash',''),body.get('kind','sparse'))
@router.get('/curator/proposals/{snapshot_hash}')
def curator_proposals(snapshot_hash:str):
    from ...document_curator import curation_proposal
    return curation_proposal(snapshot_hash)
@router.get('/categories')
def categories():
    from ... import config
    from ...experiment_catalogue import category_contract
    return {'categories':category_contract(config.PROJECT_ROOT)}
def manager():
    rt=get_runtime()
    if not hasattr(rt,'experiments'):rt.experiments=experiments.Manager(rt.inference_registry)
    return rt.experiments
@router.get('/configuration')
def configuration():
    """Gateway-only experiment choices; no retired endpoint controls."""
    registry=get_runtime().inference_registry
    return {'profiles':[{'id':p['id'],'name':p['name']} for p in registry.list('profiles')],
        'retrieval_profiles':[{'id':p['id'],'name':p['name'],'enabled':p.get('enabled',False),
            'validated':p.get('validated',False)} for p in registry.list('retrieval_profiles')],
        'roles':list(__import__('capstone_rag.ai_settings',fromlist=['ROLES']).ROLES),
        'mode':'gateway'}
@router.get('/scenarios')
def scenarios(source:str='smoke'):
    if source=='corpus':
        from ... import scenarios as original
        return {'scenarios':[s.summary() for s in original.load()],'note':'Original labels retained. Supply separate reviewed annotations for new metrics; unlabelled metrics are not scored.'}
    return {'scenarios':[{'id':v['id'],'category':v['category'],'subject':v['subject'],'expected':v['expected']} for v in experiments.SMOKE.values()],'default_ids':experiments.DEFAULT_SCENARIOS,'criteria':experiments.CRITERIA}
@router.get('/presets')
def presets():
    from ... import gateway_transport
    if not gateway_transport.enabled():return {'presets':[],'status':'gateway_not_configured'}
    try:inventory=gateway_transport.client('research').models()
    except Exception as exc:return {'presets':[],'status':'gateway_offline_unverified','allowed_models':[],'error_class':type(exc).__name__}
    allowed=sorted({model for resource in inventory.get('resources',[]) for model in resource.get('models',[])})
    registry=get_runtime().inference_registry
    definitions=[
      {'id':'gateway-smoke','name':'Five-minute Gateway smoke','description':'One synthetic technical case proves Capstone, capstone-research authentication, Gateway admission, one qualified head, recorded results and execution traces.','expected_minutes':'about 5 on CPU heads','profile_id':'gateway-default','scenario_ids':['qms-technical'],'repetitions':1,'token_limit':96,'timeout_seconds':600},
      {'id':'safety-gate','name':'Safety and refusal check','description':'Exercises the synthetic smoke/hazard case and verifies that safety and approval gates remain hard failures rather than score tradeoffs.','expected_minutes':'5–10 on CPU heads','profile_id':'gateway-default','scenario_ids':['safety-complaint'],'repetitions':1,'token_limit':128,'timeout_seconds':600},
      {'id':'paired-baseline','name':'Small paired comparison','description':'Runs two reviewed cases twice with deterministic seeds for a bounded Gateway comparison and trace inspection.','expected_minutes':'15–25 on CPU heads','profile_id':'gateway-default','scenario_ids':['ambiguous-accessory','qms-technical'],'repetitions':2,'token_limit':160,'timeout_seconds':900}]
    for preset in definitions:
        profile=registry.get('profiles',preset['profile_id']);requested=sorted({r['model'] for r in profile['routes'].values() if r.get('enabled',True)}) if profile else []
        preset.update(requested_models=requested,allowed_models=allowed,available=bool(requested and set(requested)<=set(allowed)))
        if not preset['available']:preset['unavailable_reason']='Preset requests '+(', '.join(requested) or 'an unknown model')+'. capstone-research currently allows '+(', '.join(allowed) or 'no models')+'.'
    return {'presets':definitions,'status':'authenticated','allowed_models':allowed}
@router.post('/preview')
def preview(body:dict):return experiments.preview(get_runtime().inference_registry,body)
@router.post('/saved')
def save(body:dict):
    experiments.preview(get_runtime().inference_registry,body)
    if body.get('status') and body['status']!='draft':raise ValueError('Only draft experiment settings may be saved')
    return manager().save({**body,'status':'draft'})
@router.get('')
def history():return {'experiments':manager().history()}
@router.post('/start')
def start(body:dict):
    planned=body.get('preview') or {}
    if not str(body.get('authorizer','')).strip():raise ValueError('Named operator required')
    return manager().launch(planned,get_runtime().store,body.get('confirmation',''))
@router.get('/{eid}')
def detail(eid:str,offset:int=0,limit:int=200):
    value=manager().registry.get('experiments',eid)
    if not value:raise HTTPException(404,'Unknown experiment')
    rows=manager().runs(eid);comparisons={}
    for run in rows:
        architecture=run.get('architecture') or {};methodology=architecture.get('methodology') or {}
        key=run['profile']['id']+'|'+(architecture.get('id') or 'legacy')
        entry=comparisons.setdefault(key,{'profile_id':run['profile']['id'],'profile_name':run['profile']['name'],
            'methodology_id':architecture.get('id'),'methodology_name':architecture.get('name'),
            'topology_id':(methodology.get('topology') or {}).get('id'),'treatment_id':(methodology.get('treatment') or {}).get('id'),
            'runs':0,'completed':0,'paused':0,'failed':0,'scored':0,'correct':0,'critical_violations':0,'case_completions':0,'model_calls':0,'agent_handoffs':0,'latencies_ms':[],'specialist_scored':0,'specialist_correct':0})
        entry['runs']+=1
        entry.setdefault('human_quality_scores',[]).extend(r['quality_score'] for r in run.get('human_ratings',[]))
        if run['status'] in ('completed','paused','failed'):entry[run['status']]+=1
        metric=run.get('metrics',{});entry['critical_violations']+=len(metric.get('critical_violations',[]));entry['case_completions']+=bool(metric.get('case_completed'));entry['model_calls']+=metric.get('model_calls',0);entry['agent_handoffs']+=(metric.get('communication_metrics') or {}).get('agent_handoffs',0)
        if metric.get('all_scored_checks_passed') is not None:entry['scored']+=1;entry['correct']+=bool(metric['all_scored_checks_passed'])
        if metric.get('specialist_accuracy') is not None:entry['specialist_scored']+=1;entry['specialist_correct']+=bool(metric['specialist_accuracy'])
        if metric.get('total_ms') is not None:entry['latencies_ms'].append(metric['total_ms'])
    for c in comparisons.values():c['mean_latency_ms']=sum(c['latencies_ms'])/len(c['latencies_ms']) if c['latencies_ms'] else None;c['safety_gate_passed']=c['critical_violations']==0
    for c in comparisons.values():
        c['human_ratings_count']=len(c['human_quality_scores']);c['mean_human_quality']=sum(c['human_quality_scores'])/len(c['human_quality_scores']) if c['human_quality_scores'] else None
    model_calls=sum((row.get('metrics') or {}).get('model_calls',0) for row in rows)
    running=sum(row.get('status')=='running' for row in rows);queued=sum(row.get('status')=='queued' for row in rows);answered=sum(row.get('status')=='completed' for row in rows)
    dispatching=value.get('status')=='running' and not value.get('stop_requested')
    failure_limit=int(value.get('failure_limit',3));failed=sum(row.get('status')=='failed' for row in rows)
    termination_reason=value.get('termination_reason')
    if not termination_reason and value.get('status')=='incomplete' and queued and failed>=failure_limit:
        termination_reason='failure_limit_reached'
    waiting=running+(queued if dispatching else 0);not_run=0 if dispatching else queued
    models=sorted({route.get('model') for row in rows for route in (row.get('profile') or {}).get('routes',{}).values() if route.get('enabled',True)})
    return {'experiment':{**value,'termination_reason':termination_reason},'runs':rows[max(0,offset):max(0,offset)+max(1,min(limit,10000))],'total_runs':len(rows),'offset':offset,'comparisons':list(comparisons.values()),'progress':{'state':value.get('status'),'requests_sent':model_calls,'waiting_or_running':waiting,'active_runs':running,'queued_runs':queued if dispatching else 0,'not_run':not_run,'answered_runs':answered,'failed_runs':failed,'termination_reason':termination_reason,'models':models},'trace_url':f'/trace?experiment={eid}','note':'Paused human review is not failure. Preserved not-run trials are inactive and send no Gateway traffic. Completed experiment execution does not imply a resolved customer case. No profile is automatically activated.'}
@router.post('/{eid}/stop')
def stop(eid:str):return manager().stop(eid)
@router.post('/{eid}/pause')
def pause(eid:str):return manager().stop(eid)
@router.post('/{eid}/cancel')
def cancel(eid:str):
    return manager().cancel(eid)
@router.get('/{eid}/comparison')
def comparison(eid:str):
    from ...experiment_reports import compare
    if not manager().registry.get('experiments',eid):raise HTTPException(404,'Unknown experiment')
    return compare(manager().runs(eid),get_runtime().store)
@router.get('/{eid}/comparison/phase6')
def phase6_comparison(eid:str):
    from ...evaluation_engine import compare
    if not manager().registry.get('experiments',eid):raise HTTPException(404,'Unknown experiment')
    return compare(manager().runs(eid))
@router.get('/{eid}/research')
def research(eid:str):
    """P5-M1 proposal-only analysis over the existing comparison contract."""
    from ...experiment_reports import compare
    from ...research import build_report
    experiment=manager().registry.get('experiments',eid)
    if not experiment:raise HTTPException(404,'Unknown experiment')
    runs=manager().runs(eid)
    return build_report(experiment,runs,compare(runs,get_runtime().store))
@router.post('/{eid}/research/iterations')
def propose_research_iteration(eid:str,body:dict):
    from ...evaluation_engine import compare
    from ...research_loop import ResearchLedger
    if not manager().registry.get('experiments',eid):raise HTTPException(404,'Unknown experiment')
    evidence=compare(manager().runs(eid))
    return ResearchLedger(manager().registry).propose(evidence,body.get('policy') or {},body.get('authorizer'))
@router.get('/research/iterations/all')
def research_iterations():
    from ...research_loop import ResearchLedger
    return {'iterations':ResearchLedger(manager().registry).list()}
@router.post('/research/iterations/{iteration_id}/approve')
def approve_research_iteration(iteration_id:str,body:dict):
    from ...research_loop import ResearchLedger
    return ResearchLedger(manager().registry).approve(iteration_id,body.get('authorizer'),body.get('rationale'))
@router.post('/research/iterations/{iteration_id}/dispatch')
def dispatch_research_iteration(iteration_id:str,body:dict):
    from ...research_loop import ResearchLedger
    return ResearchLedger(manager().registry).dispatch(iteration_id,body.get('preview') or {},
        manager(),get_runtime().store,body.get('authorizer'),body.get('rationale'))
@router.post('/research/iterations/{iteration_id}/followup/{eid}')
def followup_research_iteration(iteration_id:str,eid:str,body:dict):
    from ...evaluation_engine import compare
    from ...research_loop import ResearchLedger
    experiment=manager().registry.get('experiments',eid)
    if not experiment:raise HTTPException(404,'Unknown experiment')
    if experiment.get('status') not in ('completed','completed_with_failures','incomplete'):
        raise ValueError('Follow-up evidence requires a terminal campaign state')
    evidence=compare(manager().runs(eid))
    policy={**(body.get('policy') or {}),'evidence_refs':[eid]}
    return ResearchLedger(manager().registry).propose_followup(iteration_id,evidence,policy)
@router.post('/{eid}/resume')
def resume(eid:str,body:dict):
    if not str(body.get('authorizer','')).strip():raise ValueError('Named operator required')
    if not str(body.get('rationale','')).strip():raise ValueError('Recovery rationale required')
    return manager().resume_campaign(eid,body['authorizer'],body['rationale'])
@router.get('/{eid}/export/bundle')
def export_bundle(eid:str):
    experiment=manager().registry.get('experiments',eid)
    if not experiment:raise HTTPException(404,'Unknown experiment')
    report=detail(eid,0,10000);root=Path(experiment['directory']).resolve();started=time.perf_counter()
    temporary=tempfile.NamedTemporaryFile(prefix=eid+'-',suffix='.zip',dir=root,delete=False);temporary.close();output=Path(temporary.name);files=[]
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
        archive.writestr('README.txt','Capstone complete synthetic experiment evidence bundle.\nNo credential files are included. Historical source data is not modified by export.\n')
        archive.writestr('summary.json',json.dumps(report,indent=2,default=str))
        for name in ('manifest.json','source-snapshot.db','coordinator.jsonl'):
            path=root/name
            if path.is_file():
                archive.write(path,name,compress_type=zipfile.ZIP_STORED if path.suffix=='.db' else zipfile.ZIP_DEFLATED);files.append({'path':name,'bytes':path.stat().st_size})
        allowed_names={'manifest.json','result.json','trace.json','state.json','worker.log','operations.db','human-ratings.jsonl','engineering.log','engineering.xml'}
        for run in manager().runs(eid):
            directory=Path(run['directory']).resolve()
            if not directory.is_relative_to(root):raise ValueError('Run directory escaped the managed experiment root')
            prefix='runs/'+run['id']+'/'
            archive.writestr(prefix+'registry-record.json',json.dumps(run,indent=2,default=str))
            for path in directory.iterdir():
                if path.is_file() and (path.name in allowed_names or path.name.startswith('command-') or path.name.startswith('synthetic-')):
                    archive.write(path,prefix+path.name,compress_type=zipfile.ZIP_STORED if path.suffix=='.db' else zipfile.ZIP_DEFLATED);files.append({'path':prefix+path.name,'bytes':path.stat().st_size})
        archive.writestr('export-manifest.json',json.dumps({'experiment_id':eid,'files':files,'source_snapshot_deduplicated':True,'build_ms':round((time.perf_counter()-started)*1000,3)},indent=2))
    return FileResponse(output,media_type='application/zip',filename=f'{eid}-complete.zip',background=BackgroundTask(os.unlink,output))
@router.get('/{eid}/export/{kind}')
def export(eid:str,kind:str):
    result=detail(eid,0,10000)
    if kind=='json':return Response(json.dumps(result,indent=2,default=str),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="{eid}.json"'})
    if kind!='csv':raise ValueError('Use json or csv')
    output=io.StringIO();writer=csv.DictWriter(output,fieldnames=['run_id','profile','methodology_id','topology_id','treatment_id','review_mode','scenario','iteration','level','status','case_id','model','head','gateway_request_id','prompt_tokens','generated_tokens','case_disposition','case_completed','all_scored_checks_passed','critical_violations','model_calls','logical_agent_count','model_backed_agent_count','agent_handoffs','delegation_depth','total_ms','server_model_ms','slot_wait_ms','error']);writer.writeheader()
    for row in result['runs']:
        metric=row.get('metrics',{});calls=metric.get('inference_correlations') or [];observed=next((x for x in calls if x.get('resource_id')),{});usage=metric.get('model_usage') or []
        architecture=row.get('architecture') or {};methodology=architecture.get('methodology') or {};communication=metric.get('communication_metrics') or {}
        writer.writerow({**{k:metric.get(k) for k in ('case_disposition','case_completed','all_scored_checks_passed','model_calls','total_ms','server_model_ms','slot_wait_ms')},'run_id':row['id'],'profile':row['profile']['id'],'methodology_id':architecture.get('id'),'topology_id':(methodology.get('topology') or {}).get('id'),'treatment_id':(methodology.get('treatment') or {}).get('id'),'review_mode':row.get('approval_mode'),'scenario':row['scenario']['id'],'iteration':row['iteration'],'level':row['level'],'status':row['status'],'case_id':row.get('case_id'),'model':observed.get('observed_model') or next(iter({r.get('model') for r in row['profile']['routes'].values()}),None),'head':observed.get('resource_id'),'gateway_request_id':observed.get('gateway_request_id'),'prompt_tokens':sum(x.get('prompt_tokens',0) for x in usage),'generated_tokens':sum(x.get('generated_tokens',0) for x in usage),'critical_violations':len(metric.get('critical_violations',[])),'logical_agent_count':communication.get('logical_agent_count'),'model_backed_agent_count':communication.get('model_backed_agent_count'),'agent_handoffs':communication.get('agent_handoffs'),'delegation_depth':communication.get('maximum_delegation_depth'),'error':row.get('error')})
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{eid}.csv"'})
@router.get('/runs/{rid}/trace')
def trace(rid:str):
    run=manager().registry.get('experiment_runs',rid)
    if not run:raise HTTPException(404,'Unknown run')
    rd=Path(run['directory']);path=rd/'trace.json'
    if path.exists():return json.loads(path.read_text(encoding='utf-8'))
    db=rd/'operations.db';state=rd/'state.json'
    if not db.exists() or not state.exists():return {'events':[],'note':'Worker is initializing; no specialist events invented.'}
    cid=json.loads(state.read_text())['case_id']
    with sqlite3.connect(f'file:{db.as_posix()}?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        return {'events':[dict(x) for x in connection.execute('SELECT * FROM activity_events WHERE case_id=? ORDER BY event_id',(cid,))],'note':'Live recorded events from isolated worker.'}
@router.post('/runs/{rid}/review')
def review(rid:str,body:dict):return manager().command(rid,body)
@router.post('/runs/{rid}/rating')
def rating(rid:str,body:dict):
    run=manager().registry.get('experiment_runs',rid)
    if not run:raise HTTPException(404,'Unknown run')
    if not str(body.get('reviewer','')).strip() or not 1<=int(body.get('quality_score',0))<=5:raise ValueError('Named reviewer and quality score 1–5 required')
    path=Path(run['directory'])/'human-ratings.jsonl'
    with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps({**body,'at':time.time()})+'\n')
    return {'ok':True,'note':'Human rating appended separately; automatic results were not rewritten.'}
