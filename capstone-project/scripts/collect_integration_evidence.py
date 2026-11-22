"""Read-only final exports and baseline preservation verification."""
import hashlib,json,sqlite3,urllib.request
from pathlib import Path
root=Path(__file__).resolve().parents[1];evidence=root/'docs/evidence/local_integration'
def get(path):
    with urllib.request.urlopen('http://127.0.0.1:8000'+path,timeout=90) as response:return response.read()
exports={}
for label,eid in [('matrix','EXP-0fb1d119ae0e45b1bd517117579ca81d'),('component-repair','EXP-944394fc55c1482689fdc368d383713f'),('specialist-intake','EXP-5383707c88f04771a3709ba30fe61734'),('specialist-review','EXP-47deb3602f914be2b1042ffa14e6f493')]:
    for kind in ['json','csv']:(evidence/(label+'-export.'+kind)).write_bytes(get('/api/experiments/'+eid+'/export/'+kind))
    value=json.loads((evidence/(label+'-export.json')).read_text(encoding='utf-8'));exports[label]={'experiment_id':eid,'status':value['experiment']['status'],'comparisons':value['comparisons']}
for label,url in [('live-case-trace','/api/trace/case/CASE-000082'),('live-chat-trace','/api/trace/conversation/CONV-000008')]:
    (evidence/(label+'.json')).write_bytes(get(url))
(evidence/'comparison-summary.json').write_text(json.dumps(exports,indent=2)+'\n',encoding='utf-8')
baseline=Path((evidence/'baseline-location.txt').read_text().strip())
checks=[]
for name,digest in json.loads((baseline/'SHA256SUMS.json').read_text()).items():checks.append({'path':str((baseline/name).relative_to(root)),'unchanged':hashlib.sha256((baseline/name).read_bytes()).hexdigest()==digest})
for row in json.loads((baseline/'manifest.json').read_text()):
    if row['path'].startswith('data/'):
        path=root/row['path'];checks.append({'path':row['path'],'unchanged':path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256']})
before=sqlite3.connect(f'file:{(baseline/"operations.db").as_posix()}?mode=ro',uri=True)
after=sqlite3.connect(f'file:{(root/"data/operations.db").as_posix()}?mode=ro',uri=True)
tables={r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sqlite_')};logical=[]
for table in sorted(tables):
    old=before.execute('SELECT * FROM "'+table+'"').fetchall();new=after.execute('SELECT * FROM "'+table+'"').fetchall()
    digest=lambda rows:hashlib.sha256(json.dumps(sorted((repr(r) for r in rows)),ensure_ascii=True).encode()).hexdigest()
    logical.append({'table':table,'baseline_rows':len(old),'current_rows':len(new),'unchanged':digest(old)==digest(new)})
before.close();after.close()
report={'files':checks,'operational_tables':logical,'all_preserved':all(x['unchanged'] for x in checks+logical),'note':'Checks original operational database, frozen baseline and every prior data file present in the starting manifest. New configuration/index/experiment files are additive.'}
(evidence/'preservation-verification.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'all_preserved':report['all_preserved'],'files_checked':len(checks),'tables_checked':len(logical),'changed_files':[x['path'] for x in checks if not x['unchanged']],'changed_tables':[x['table'] for x in logical if not x['unchanged']]}))
