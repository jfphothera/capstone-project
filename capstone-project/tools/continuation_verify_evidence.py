"""Validate recorded physical responses with the final integrity gate.

This is recording validation, not a new physical execution or bypass test.
"""
import json
from pathlib import Path
from capstone_rag.capacity_gateway import Gateway

def main():
    out=Path(__file__).resolve().parents[1]/'docs/phase4_2_continuation';checked=[]
    trace=json.loads((out/'physical_gateway_trace.json').read_text(encoding='utf-8'))
    for q in trace['qualification']:
        operation='/api/embed' if q['purpose']=='embedding' else '/api/generate'
        Gateway.validate_response(operation,q['response'],q['model'])
        assert q['passed'] and q['actual_execution'] and q['digest']
        checked.append({'source':'physical qualification','model':q['model'],'operation':operation})
    for client in trace['clients']:
        result=client['result']['result'];Gateway.validate_response('/api/generate',result['response'],result['model'])
        checked.append({'source':'independent physical client','application_id':result['application_id']})
    case=json.loads((out/'physical_case_trace.json').read_text(encoding='utf-8'))
    lab=json.loads((out/'physical_lab_trace.json').read_text(encoding='utf-8'))
    for source,rows in [('physical case',case['calls']),('physical Lab',[call for trial in lab['traces'] for call in trial['model_calls']])]:
        for row in rows:
            record=json.loads(row['payload']);Gateway.validate_response('/api/generate',record['response'],record['model'])
            assert row['status']=='completed' and record['gateway']['resource_id']=='head-192-0-2-6'
            checked.append({'source':source,'task_id':record['task_id'],'workflow_id':record['workflow_id']})
    result={'recording_validation_only':True,'physical_responses_validated':len(checked),'checks':checked,
        'serialized_model_intervals':trace['serialized_model_intervals'],'overlapping_client_demand':trace['overlapping_client_demand'],
        'raw_ollama_bypass_prevented':False,'lab_statuses':[r['status'] for r in lab['runs']]}
    (out/'recording_integrity_validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))

if __name__=='__main__':main()
