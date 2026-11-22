"""New continuation evidence/source archives; never rebuild frozen bundles."""
import hashlib,json,re,sqlite3,zipfile,os,subprocess,sys,tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from capstone_rag import index,qms

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/phase4_2_continuation'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    baseline=json.loads((OUT/'baseline_state.json').read_text())
    manifest=[];changes=[]
    before={r['path']:r['sha256'] for r in baseline['source_files']}
    for top in ('src','scripts','tests'):
        for p in (ROOT/top).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix.lower() not in {'.py','.js','.mjs','.ps1','.cmd','.json','.html','.css'}:continue
            name=p.relative_to(ROOT).as_posix();digest=sha(p)
            manifest.append({'path':name,'sha256':digest})
            if name not in before or before[name]!=digest:changes.append({'path':name,'change':'modified' if name in before else 'not tracked in baseline manifest' if p.suffix=='.css' else 'added'})
    missing=[name for name in before if not (ROOT/name).exists()]
    assert not missing,missing
    inventory=[]
    pattern=re.compile(r'https?://|192\.0\.2\.|coordinator-|11434|\b(qwen3|gemma3|all-minilm)|CAPSTONE_(GATEWAY|CLUSTER|APPLICATION|ORCHESTRATOR|CAPACITY)|arena_service',re.I)
    for top in ('src','scripts','tools','tests','deploy','docs'):
        for p in (ROOT/top).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix.lower() not in {'.py','.js','.mjs','.ps1','.cmd','.md','.json','.html'}:continue
            name=p.relative_to(ROOT).as_posix()
            if name.startswith('docs/phase4_2_continuation/'):continue
            for n,line in enumerate(p.read_text(encoding='utf-8',errors='replace').splitlines(),1):
                if not pattern.search(line):continue
                if top=='tests':category='fixture'
                elif top=='docs':category='historical_audit_or_documentation'
                elif top=='deploy':category='deployment_example'
                elif top in {'tools','scripts'}:category='manual_tool_or_deployment_value_review_before_use'
                elif p.name=='telemetry.py' and 'coordinator-192.0.2' in line:category='legacy_AGENTS_tuple_not_used_for_new_location'
                elif 'environ' in line or 'config.' in line:category='configurable_default_or_configuration_reference'
                elif '11434' in line and p.name=='cluster.py':category='default_port_overridden_by_registered_endpoint'
                elif 'localhost' in line or '127.0.0.1' in line or '::1' in line:category='loopback_security_boundary_or_local_example'
                else:category='logical_contract_or_model_profile_reference_reviewed'
                inventory.append({'path':name,'line':n,'classification':category,'text':line.strip()[:300]})
    (OUT/'current_location_inventory.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')
    databases={}
    for name in ('operations.db','ai_configuration.db','shared_capacity.db'):
        p=ROOT/'data'/name
        if not p.exists():continue
        with closing(sqlite3.connect('file:'+p.as_posix()+'?mode=ro',uri=True)) as db:
            record={'integrity':db.execute('pragma integrity_check').fetchone()[0]}
            if name=='operations.db':record['physical_nodes']=db.execute('select count(*) from physical_nodes').fetchone()[0]
            if name=='ai_configuration.db':record['preferences']=db.execute('select key,value from preferences').fetchall()
            databases[name]=record
    frozen={p.relative_to(ROOT).as_posix():sha(p) for top in ('phase3','phase4_1','phase4_2') for p in (ROOT/'docs'/top).glob('*.zip')}
    checks={'source_files':len(manifest),'changes':changes,'deleted_baseline_files':missing,'databases':databases,
            'qms_fingerprint':index.corpus_fingerprint(qms.load_chunks()),'frozen_archives_sha256':frozen,
            'inventory_occurrences':len(inventory),'git':'no repository metadata available'}
    checks['test_suites']={p.name:dict(ET.parse(p).getroot().find('testsuite').attrib) for p in OUT.glob('*.xml')}
    revision=hashlib.sha256()
    for p in sorted((ROOT/'src').rglob('*.py')):revision.update(p.relative_to(ROOT).as_posix().encode());revision.update(p.read_bytes())
    checks['application_source_revision']=revision.hexdigest()
    lab=json.loads((OUT/'physical_lab_trace.json').read_text(encoding='utf-8'))
    checks['physical_lab_source_revision']=lab['runs'][0]['source_revision']
    checks['physical_lab_matches_delivered_application_source']=checks['application_source_revision']==checks['physical_lab_source_revision']
    assert checks['physical_lab_matches_delivered_application_source']
    assert checks['qms_fingerprint']=='a3b68d86451db05e'
    assert all(v['integrity']=='ok' for v in databases.values())
    (OUT/'current_source_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (OUT/'final_state.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    neutral=['__init__.py','capacity_broker.py','capacity_gateway.py','capacity_gateway_server.py','capacity_admin.py','capacity_bootstrap.py']
    with zipfile.ZipFile(OUT/'shared_inference_service.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name in neutral:z.write(ROOT/'src/capstone_rag'/name,'capstone_rag/'+name)
        z.write(ROOT/'src/shared_inference_client.py','shared_inference_client.py')
        for name in ('SHARED_INFERENCE_CONTRACT.md','DEPLOYMENT_AND_ENFORCEMENT.md'):z.write(OUT/name,name)
    with zipfile.ZipFile(OUT/'shared_inference_client.zip','w',zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT/'src/shared_inference_client.py','shared_inference_client.py')
        z.write(ROOT/'scripts/clients/gateway_probe.py','gateway_probe.py')
        z.write(OUT/'SHARED_INFERENCE_CONTRACT.md','SHARED_INFERENCE_CONTRACT.md')
    with tempfile.TemporaryDirectory(prefix='neutral-service-smoke-') as temp:
        with zipfile.ZipFile(OUT/'shared_inference_service.zip') as z:z.extractall(temp)
        environment={k:v for k,v in os.environ.items() if k!='PYTHONPATH'}
        check=subprocess.run([sys.executable,'-S','-c',
            "import sys; from capstone_rag import capacity_gateway_server,capacity_admin,capacity_bootstrap; import shared_inference_client; allowed={'capstone_rag','capstone_rag.capacity_broker','capstone_rag.capacity_gateway','capstone_rag.capacity_gateway_server','capstone_rag.capacity_admin','capstone_rag.capacity_bootstrap'}; loaded={k for k in sys.modules if k.startswith('capstone_rag')}; assert loaded<=allowed,loaded; print('neutral service and client import without site packages or business modules')"],
            cwd=temp,env=environment,capture_output=True,text=True,timeout=15,check=True)
        checks['standalone_package_smoke']=check.stdout.strip()
    (OUT/'final_state.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    paths=[ROOT/r['path'] for r in manifest]
    paths += list(OUT.glob('*.md'))+[p for p in OUT.glob('*.json') if p.name!='delivery_checksums.json']+list(OUT.glob('*.xml'))
    paths += [p for p in (ROOT/'tools').glob('*continuation*.py')]
    paths += [ROOT/'run_app.py',ROOT/'requirements.runtime.lock']
    with zipfile.ZipFile(OUT/'capstone_continuation_source_evidence.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(set(paths)):
            assert p.suffix not in {'.db','.zip','.pem','.key'}
            z.write(p,p.relative_to(ROOT).as_posix())
    packages={name:{'sha256':sha(OUT/name),'bytes':(OUT/name).stat().st_size} for name in
        ('shared_inference_service.zip','shared_inference_client.zip','capstone_continuation_source_evidence.zip')}
    (OUT/'delivery_checksums.json').write_text(json.dumps(packages,indent=2),encoding='utf-8')
    print(json.dumps({'source_files':len(manifest),'changed_files':len(changes),'deleted':missing,'databases':databases,'qms_fingerprint':checks['qms_fingerprint']}))

if __name__=='__main__':main()
