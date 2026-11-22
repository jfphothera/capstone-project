"""Freeze hashes and build a source/evidence package without operational data."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT/'docs/phase4_2'
GENERATED={'docs/phase4_2/final_source_manifest.json','docs/phase4_2/changed_file_inventory.json','docs/phase4_2/phase4_2_acceptance_bundle.zip'}
baseline=json.loads((ROOT/'docs/phase4_1/final_source_manifest.json').read_text())
base={r['path']:r for r in baseline['files']}


def selected(path):
    rel=path.relative_to(ROOT);name=rel.as_posix()
    if name in GENERATED or '__pycache__' in rel.parts or 'acceptance_evidence_bundle' in rel.parts:return False
    if path.suffix.lower() in {'.pyc','.db','.sqlite','.sqlite3','.safetensors','.npz','.pt','.bin'}:return False
    return (name in base or rel.parts[0] in {'src','tests','tools','scripts'}
            or name.startswith(('docs/phase4_2/','deploy/Arena-Viewer/'))
            or name in {'requirements.txt','requirements.runtime.lock','.gitignore'})


paths=set()
for prefix in ('src','tests','tools','scripts','docs/phase4_2','deploy/Arena-Viewer'):
    for path in (ROOT/prefix).rglob('*'):
        if path.is_file() and selected(path):paths.add(path.relative_to(ROOT).as_posix())
paths.update(p for p in base if (ROOT/p).is_file() and selected(ROOT/p))
paths.update({'requirements.txt','requirements.runtime.lock','.gitignore'})
rows=[{'path':p,'bytes':(ROOT/p).stat().st_size,'sha256':hashlib.sha256((ROOT/p).read_bytes()).hexdigest()} for p in sorted(paths)]
aggregate=hashlib.sha256('\n'.join(r['path']+':'+r['sha256'] for r in rows).encode()).hexdigest()
manifest={'format':'phase4.2-source-evidence-v1','baseline_aggregate':baseline['aggregate_sha256'],
          'aggregate_sha256':aggregate,'file_count':len(rows),'files':rows,
          'acceptance_status':'physical and independent-application deployment acceptance pending; see acceptance matrix'}
(DOCS/'final_source_manifest.json').write_text(json.dumps(manifest,indent=2))
after={r['path']:r for r in rows}
delta={'modified':sorted(p for p in base.keys()&after.keys() if base[p]['sha256']!=after[p]['sha256']),
       'added':sorted(after.keys()-base.keys()),'removed':sorted(base.keys()-after.keys())}
(DOCS/'changed_file_inventory.json').write_text(json.dumps(delta,indent=2))
archive_path=DOCS/'phase4_2_acceptance_bundle.zip'
with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_DEFLATED) as archive:
    for row in rows:archive.write(ROOT/row['path'],row['path'])
    for name in ('final_source_manifest.json','changed_file_inventory.json'):
        archive.write(DOCS/name,'docs/phase4_2/'+name)
with zipfile.ZipFile(archive_path) as archive:
    assert archive.testzip() is None
    assert all(hashlib.sha256(archive.read(r['path'])).hexdigest()==r['sha256'] for r in rows)
assert not delta['removed'],delta['removed']
print(json.dumps({'manifest_sha256':aggregate,'files':len(rows),'baseline_delta':{k:len(v) for k,v in delta.items()},
    'zip_sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),'zip':str(archive_path)},indent=2))
