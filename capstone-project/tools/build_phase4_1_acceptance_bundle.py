"""Build a source-and-evidence-only Phase 4.1 acceptance package."""
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs' / 'phase4_1'
BUNDLE = DOCS / 'acceptance_evidence_bundle'
ZIP = DOCS / 'phase4_1_acceptance_evidence_bundle.zip'
if BUNDLE.exists(): shutil.rmtree(BUNDLE)
BUNDLE.mkdir(parents=True)

manifest = json.loads((DOCS / 'final_source_manifest.json').read_text(encoding='utf-8'))
selected = []
for row in manifest['files']:
    path = row['path']
    if path.startswith(('src/', 'tests/', 'tools/phase4_1_')) or path in {
            'requirements.txt', 'requirements.runtime.lock', 'pytest.ini'} or path.startswith('docs/phase4_1/'):
        if path.endswith('.zip') or '/acceptance_evidence_bundle/' in path: continue
        selected.append(row)
        target = BUNDLE / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / path, target)
for path in ('docs/phase4_1/final_source_manifest.json',
             'docs/phase4_1/changed_file_inventory.json'):
    source = ROOT / path
    data = source.read_bytes()
    row = {'path': path, 'bytes': len(data),
           'sha256': hashlib.sha256(data).hexdigest(), 'generated_artifact': True}
    selected.append(row)
    target = BUNDLE / path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
bundle_manifest = {'format': 'phase4.1-acceptance-bundle-v1',
                   'source_manifest_aggregate': manifest['aggregate_sha256'],
                   'file_count': len(selected), 'files': selected,
                   'exclusions': ['databases', 'model weights', 'dependency caches',
                                  'credentials', 'environment files']}
(BUNDLE / 'bundle_manifest.json').write_text(
    json.dumps(bundle_manifest, indent=2), encoding='utf-8')
with zipfile.ZipFile(ZIP, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(BUNDLE.rglob('*')):
        if path.is_file(): archive.write(path, path.relative_to(BUNDLE).as_posix())
print(ZIP, hashlib.sha256(ZIP.read_bytes()).hexdigest(), len(selected))
