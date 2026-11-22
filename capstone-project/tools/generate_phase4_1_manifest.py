"""Generate the frozen Phase 4.1 source manifest and baseline delta."""
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'phase4_1'
BASELINE = json.loads((OUT / 'baseline_source_manifest.json').read_text(encoding='utf-8'))
BASELINE_PATHS = {row['path'] for row in BASELINE['files']}
INCLUDE = ('src', 'tests', 'tools', 'scripts')
EXACT = ('requirements.txt', 'requirements.runtime.lock', 'pytest.ini', 'README.md')
EXCLUDE_PARTS = {'__pycache__', '.pytest_cache', '.venv', 'phase1_deps',
                 'phase4_deps', 'phase1_wheelhouse', 'acceptance_evidence_bundle'}
GENERATED = {
    'docs/phase4_1/final_source_manifest.json',
    'docs/phase4_1/changed_file_inventory.json',
    'docs/phase4_1/phase4_1_acceptance_evidence_bundle.zip',
}


def included(path):
    rel = path.relative_to(ROOT)
    if rel.as_posix() in GENERATED: return False
    if any(part in EXCLUDE_PARTS for part in rel.parts): return False
    if path.suffix.lower() in {'.pyc', '.db', '.sqlite', '.zip'}: return False
    return rel.as_posix() in BASELINE_PATHS or rel.parts[0] in INCLUDE or rel.as_posix() in EXACT or (
        len(rel.parts) > 1 and rel.parts[0] == 'docs' and rel.parts[1] == 'phase4_1')


files = []
for path in sorted(ROOT.rglob('*')):
    if not path.is_file() or not included(path): continue
    data = path.read_bytes()
    files.append({'path': path.relative_to(ROOT).as_posix(), 'bytes': len(data),
                  'sha256': hashlib.sha256(data).hexdigest()})
aggregate = hashlib.sha256('\n'.join(
    f"{row['path']}:{row['sha256']}" for row in files).encode()).hexdigest()
manifest = {'format': 'phase4.1-final-source-v1', 'created': '2026-10-30',
            'aggregate_sha256': aggregate, 'file_count': len(files), 'files': files}
(OUT / 'final_source_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')

before = {row['path']: row for row in BASELINE['files']}
after = {row['path']: row for row in files}
delta = {
    'baseline_aggregate_sha256': BASELINE['aggregate_sha256'],
    'final_aggregate_sha256': aggregate,
    'modified': sorted(path for path in before.keys() & after.keys()
                       if before[path]['sha256'] != after[path]['sha256']),
    'added': sorted(after.keys() - before.keys()),
    'removed': sorted(before.keys() - after.keys()),
}
(OUT / 'changed_file_inventory.json').write_text(json.dumps(delta, indent=2), encoding='utf-8')
print(aggregate, len(files), len(delta['modified']), len(delta['added']), len(delta['removed']))
