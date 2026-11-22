"""Host-side infrastructure administration; never an application tool endpoint.

0.4.4: --parallel N (completion only) measures, after a passing qualification, how many calls of
the model the head really runs at once (Gateway.measure_parallel) and stores it with the head's
runtime version, the model's digest and num_ctx. Requalify, and remeasure, after any runtime change.
"""
import argparse,json
from .capacity_gateway import Gateway

def main():
    p=argparse.ArgumentParser();p.add_argument('--database',required=True)
    p.add_argument('--resource',required=True);p.add_argument('--model',required=True)
    p.add_argument('--operator',required=True);p.add_argument('--purpose',choices=['completion','embedding','chat','tools','structured_output','vision','context','structured_chat'],default='completion')
    p.add_argument('--max-context',type=int,default=1024)
    p.add_argument('--parallel',type=int,help='0.4.4: also measure N concurrent generations (2-64) after a passing completion qualification')
    p.add_argument('--upstream-ca');p.add_argument('--upstream-certificate');p.add_argument('--upstream-key')
    p.add_argument('--output',required=True);a=p.parse_args()
    if a.parallel is not None and (a.purpose!='completion' or not 2<=a.parallel<=64):p.error('--parallel needs --purpose completion and a value of 2-64')
    gateway=Gateway(a.database,upstream_ca=a.upstream_ca,upstream_certificate=a.upstream_certificate,upstream_key=a.upstream_key)
    evidence=gateway.qualify(a.resource,a.model,operator=a.operator,purpose=a.purpose,max_context=a.max_context)
    summary={'resource_id':a.resource,'model':a.model,'purpose':a.purpose,'passed':evidence['passed'],'evidence_id':evidence.get('evidence_id')}
    if a.parallel and evidence['passed']:
        measurement=gateway.measure_parallel(a.resource,a.model,parallel=a.parallel,operator=a.operator)
        evidence['parallel_measurement']=measurement
        summary['parallel']={k:measurement.get(k) for k in ('requested_parallel','measured_parallel','runtime_version','digest','num_ctx','stored','error')}
    from pathlib import Path
    Path(a.output).write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    print(json.dumps(summary))
    return 0 if evidence['passed'] and (not a.parallel or summary.get('parallel',{}).get('stored')) else 1

if __name__=='__main__':raise SystemExit(main())
