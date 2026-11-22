"""Hash/size/ZIP-CRC verification of proposed source candidates, no copying."""
import json,time
from pathlib import Path
from controlled_archive import Archive,checked

def main():
    out=Path(__file__).resolve().parents[1]/'docs/phase4_2_closure';plan=Archive(out/'archive_manifest.json')
    failures=[];verified=[];started=time.time()
    for entry,source,_,_ in plan.entries:
        if not entry['safe_to_move']:continue
        try:checked(source,entry);verified.append(entry['id'])
        except Exception as exc:failures.append({'relative_path':entry['relative_path'],'error':str(exc)})
    result={'source_verification_only':True,'manifest_sha256':plan.plan_hash,'verified_files':len(verified),
        'verified_ids':verified,'failures':failures,'elapsed_seconds':time.time()-started,'copied_files':0,'relocated_files':0}
    (out/'archive_source_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='verified_ids'}))
    if failures:raise SystemExit(1)

if __name__=='__main__':main()
