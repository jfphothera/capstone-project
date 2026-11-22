"""Run a bounded, offline Testing Lab acceptance campaign and write durable evidence."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from capstone_rag import config, experiments, telemetry
from capstone_rag.ai_settings import Registry
from capstone_rag.experiment_catalogue import category_contract
from capstone_rag.experiment_modes import contract
from capstone_rag.experiment_reports import compare
from capstone_rag.store import Store


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('output')
    args=parser.parse_args();output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    config.DATA_DIR=str(output/'runtime-data')
    os.environ['CAPSTONE_DATA_ROOT']=config.DATA_DIR
    registry=Registry(output/'ai_configuration.db');store=Store(str(output/'operations.db'))
    manager=experiments.Manager(registry)
    spec={'name':'Phase 5 bounded installed-lab acceptance','category':'dependency',
          'level':'engineering','profile_ids':['gateway-default'],
          'scenario_ids':['phone-log'],'repetitions':2,'concurrency':2,'seed':41,
          'approval_mode':'scripted','timeout_seconds':120,'failure_limit':2,
          'max_trials':4,'token_limit':64,'duration_seconds':0,
          'minimum_free_disk_bytes':64*1024*1024}
    planned=experiments.preview(registry,spec);launched=manager.launch(planned,store)
    manager.threads[launched['id']].join(300)
    if manager.threads[launched['id']].is_alive():
        manager.stop(launched['id']);raise SystemExit('Acceptance campaign exceeded five minutes')
    runs=manager.runs(launched['id']);record=registry.get('experiments',launched['id'])

    # A restart drill uses the real durable reconciliation path. It never launches
    # or replays the deliberately interrupted record.
    drill='EXP-acceptance-restart';manager.save({'id':drill,'status':'running'})
    for state in ('completed','running','queued'):
        directory=output/'recovery-drill'/state;directory.mkdir(parents=True,exist_ok=True)
        manager._store_run({'id':'DRILL-'+state,'experiment_id':drill,'status':state,
                            'directory':str(directory)})
    manager.recover();recovery={row['id']:row['status'] for row in manager.runs(drill)}

    # Current-schema Arena sample. These are synthetic contract-validation events,
    # not proof that a physical inference head was contacted during this offline run.
    workflow='WF-phase5-arena-sample';case='CASE-synthetic-arena-sample'
    for agent,name,role in [('acceptance_coordinator','Acceptance Coordinator','coordination'),
                            ('acceptance_specialist','Acceptance Specialist','testing')]:
        telemetry.register(store,agent,name,role,capabilities=(role,))
    common={'case_id':case,'workflow_id':workflow,'task_id':'TASK-remote-probe',
            'agent_id':'acceptance_specialist','correlation_id':'CALL-sample',
            'inference_node':'head-192-0-2-11','model_id':'qualified-sample-model',
            'endpoint_id':'gateway-authorized-resource'}
    detail={'execution_class':'synthetic_contract_validation','physical_execution_observed':False}
    telemetry.emit(store,'task.started',**common,task_status='running',summary='Synthetic remote-probe task started',detail=detail)
    telemetry.emit(store,'inference.started',**common,task_status='running',summary='Remote inference identity sample',detail=detail)
    telemetry.emit(store,'inference.failed',**common,task_status='failed',summary='Synthetic gateway-loss sample',detail={**detail,'failure':'gateway_unavailable'})
    telemetry.emit(store,'task.recovered',**common,task_status='running',summary='Explicit recovery sample',detail={**detail,'authorization':'operator_required'})
    telemetry.emit(store,'task.completed',**common,task_status='completed',summary='Synthetic recovery path completed',detail=detail)
    arena=[row for row in telemetry.replay(store,0,1000) if row.get('workflow_id')==workflow]

    result={'format':'capstone-phase5-lab-acceptance-v1','generated_at':time.time(),
        'evidence_class':'local_engineering_and_synthetic_contract_validation',
        'physical_remote_inference_performed':False,'campaign':record,
        'runs':runs,'comparison':compare(runs,store),'recovery_drill':recovery,
        'mode_contract':contract(),'categories':category_contract(config.PROJECT_ROOT),
        'arena_sample':arena,
        'limitations':['Engineering campaign executes packaged pytest targets without contacting model heads.',
                       'Arena remote identity events validate the current schema only; they are not physical remote-inference measurements.',
                       'No statistical, hardware, model, or production-readiness winner is inferred.']}
    (output/'acceptance-report.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'experiment_id':launched['id'],'status':record['status'],
                      'run_states':{state:sum(row['status']==state for row in runs)
                                    for state in sorted({row['status'] for row in runs})},
                      'recovery_drill':recovery,'arena_events':len(arena)},indent=2))
    return 0 if record['status']=='completed' else 1


if __name__=='__main__':raise SystemExit(main())
