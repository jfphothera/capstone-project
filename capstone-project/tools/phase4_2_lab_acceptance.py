"""Execute an application-owned campaign and prove durable result reopening."""
import json
import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

from capstone_rag import config,experiments,store
from capstone_rag.ai_settings import Registry


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--category',choices=['case','dependency','performance'],default='case');args=parser.parse_args()
    from capstone_rag.experiment_catalogue import CATEGORIES
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
    directory=Path(config.DATA_DIR)/'phase4_2_acceptance'
    directory.mkdir(parents=True,exist_ok=True)
    registry=Registry(directory/'settings.db')
    endpoint=registry.put('endpoints',{'id':'acceptance-head6','name':'Real 8 GB CPU head','url':'http://192.0.2.6:11434','timeout':90})
    profile=registry.active();profile.update(id='phase42-lab',name='Synthetic phone-log acceptance',default_endpoint=endpoint['id'])
    for role,route in profile['routes'].items():
        route.update(endpoint=endpoint['id'],model='gemma3:270m',enabled=args.category=='performance' and role=='investigation')
    registry.put('profiles',profile)
    st=store.Store(config.DB_PATH)
    manager=experiments.Manager(registry)
    planned=experiments.preview(registry,{'name':'Phase 4.2 bounded persistent campaign','profile_ids':['phase42-lab'],
        'scenario_ids':['phone-log'],'repetitions':1,'concurrency':1,'duration_seconds':30,'max_trials':3,
        'token_limit':64,'timeout_seconds':120,'failure_limit':2,'approval_mode':'interactive','seed':42,
        'level':CATEGORIES[args.category][1],'category':args.category,'model_call_budget':6,'total_token_budget':512})
    launched=manager.launch(planned,st)
    manager.threads[launched['id']].join(timeout=180)
    if manager.threads[launched['id']].is_alive():raise TimeoutError('Bounded acceptance campaign has not finished')
    # A new manager reads from SQLite and per-trial checkpoints, with no browser.
    reopened=experiments.Manager(Registry(directory/'settings.db'))
    runs=reopened.runs(launched['id'])
    evidence={'experiment':reopened.registry.get('experiments',launched['id']),
              'runs':runs,'browser_required':False,'fresh_manager_reopened':True,
              'limitations':['Phone-log fixture has inference disabled; distributed inference is verified separately in distributed_case_trace.json.']}
    filename='lab_persistence_trace.json' if args.category=='case' else 'lab_'+args.category+'_trace.json'
    Path('docs/phase4_2',filename).write_text(json.dumps(evidence,indent=2,default=str))
    print('Experiment:',launched['id'],'runs:',[(r['id'],r['status']) for r in runs])
    st.close()
    assert runs and all(r['status'] in {'completed','paused'} for r in runs)


if __name__=='__main__':main()
