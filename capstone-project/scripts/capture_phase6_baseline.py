"""Capture immutable Candidate A provenance without contacting the Gateway."""
from __future__ import annotations
import argparse,hashlib,json,time
from pathlib import Path

from capstone_rag import config,experiments,qms,scenarios
from capstone_rag.ai_settings import Registry,canonical,fingerprint
from capstone_rag.experimental_architecture import registry as components,template
from capstone_rag.version import get_version


def file_hash(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output');args=parser.parse_args()
    settings=Registry();chunks=qms.load_chunks();documents={}
    for chunk in chunks:
        path=Path(config.PROJECT_ROOT)/chunk.source_path
        documents.setdefault(chunk.document_id,{'document_id':chunk.document_id,
            'version':chunk.document_version,'status':chunk.approval_status,
            'source_path':chunk.source_path,'source_sha256':file_hash(path),
            'model':chunk.model,'revisions':chunk.applicable_revisions,
            'audiences':chunk.audiences,'permitted_uses':chunk.permitted_uses})
    profiles=[]
    for profile in settings.list('profiles'):
        snapshot=settings.snapshot(profile)
        profiles.append({'id':profile['id'],'name':profile['name'],
            'configuration_hash':snapshot['configuration_hash'],
            'routes':{role:{'model':route['model'],'endpoint_id':route['endpoint'],
                'enabled':route.get('enabled',True),'options':route.get('options',{})}
                for role,route in snapshot['routes'].items()}})
    smoke=[experiments.SMOKE[key] for key in sorted(experiments.SMOKE)]
    legacy=[row.as_dict() for row in scenarios.load()]
    result={'format':'capstone-phase6-candidate-a-baseline-v1','captured_at':time.time(),
        'version':get_version(),'source_revision':experiments.source_revision(),
        'candidate_architecture':template('candidate-a'),'component_registry':components(),
        'configured_profiles':profiles,
        'model_inventory_status':'configured_requests_only; no Gateway availability inferred',
        'document_snapshot':{'documents':sorted(documents.values(),key=lambda row:row['document_id']),
            'corpus_hash':fingerprint([{'metadata':c.as_dict(),'text':c.text} for c in chunks]),
            'chunk_count':len(chunks),'chunking':'heading sections'},
        'evaluation_datasets':{
            'smoke':{'count':len(smoke),'sha256':fingerprint(smoke),'ids':[x['id'] for x in smoke]},
            'legacy_synthetic':{'count':len(legacy),'sha256':fingerprint(legacy),
                                'ids':[x['scenario_id'] for x in legacy]}},
        'limitations':['Configured model routes are not proof of Gateway qualification or current availability.',
                       'Regression coverage is not comparative model evidence.',
                       'Candidate A is immutable evidence; activation and promotion are not authorized.']}
    result['baseline_sha256']=hashlib.sha256(canonical(result).encode()).hexdigest()
    Path(args.output).write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(Path(args.output).resolve()),'baseline_sha256':result['baseline_sha256'],
                      'source_revision':result['source_revision'],'profiles':len(profiles),
                      'documents':len(documents),'chunks':len(chunks)},indent=2))


if __name__=='__main__':main()
