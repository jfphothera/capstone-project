"""Generate reproducible SHA-256 manifests for Phase 3 evidence."""
import hashlib,json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DOCS=ROOT/'docs'/'phase3'

def entry(path,base):
    return {'path':path.relative_to(base).as_posix(),'bytes':path.stat().st_size,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

baseline=DOCS/'baseline_snapshot_20261029'
baseline_files=[entry(p,baseline) for p in sorted(baseline.rglob('*')) if p.is_file()]
(DOCS/'baseline_recovery_manifest.json').write_text(json.dumps({
    'format':'phase3-baseline-recovery-v1','created':'2026-10-29',
    'source_phase':'accepted Phase 2 before Phase 3 edits','file_count':len(baseline_files),
    'files':baseline_files},indent=2),encoding='utf-8')

files=[]
for directory in ('src','tests','tools'):
    for p in sorted((ROOT/directory).rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc',):files.append(p)
for p in sorted(DOCS.glob('*')):
    if p.is_file() and p.name not in ('final_source_manifest.json','phase3_acceptance_evidence_bundle.zip'):files.append(p)
for p in sorted((DOCS/'PHASE3_ARENA_SCREENSHOTS').rglob('*')):
    if p.is_file():files.append(p)
entries=[entry(p,ROOT) for p in files]
aggregate=hashlib.sha256(''.join(f"{x['path']}:{x['sha256']}\n" for x in entries).encode()).hexdigest()
(DOCS/'final_source_manifest.json').write_text(json.dumps({
    'format':'phase3-final-source-v1','created':'2026-10-29','aggregate_sha256':aggregate,
    'file_count':len(entries),'files':entries},indent=2),encoding='utf-8')
print(aggregate,len(entries),len(baseline_files))
