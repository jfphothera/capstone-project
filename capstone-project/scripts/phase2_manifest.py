"""Write deterministic Phase 2 baseline and final controlled-source manifests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs' / 'phase2'
BASELINE = DOCS / 'baseline_snapshot_20261029'
ROOT_FILES = {'app.py', 'chatbot.py', 'portal.py', 'pytest.ini', 'README.md',
              'requirements.txt', 'requirements.runtime.lock', 'run_app.py'}
DATA_FILES = {'data/qms_compatibility.json', 'data/revision_register.json'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_file(path):
    rel = path.relative_to(ROOT)
    if any(part in {'__pycache__', '.pytest_cache', '.venv', 'phase1_deps',
                    '.phase1_deps', '.test_deps'} for part in rel.parts):
        return False
    if len(rel.parts) == 1 and rel.name in ROOT_FILES:
        return True
    if rel.as_posix() in DATA_FILES:
        return True
    return rel.parts[0] in {'src', 'tests', 'scripts'} or rel.parts[:2] in {
        ('data', 'qms'), ('data', 'phase1')}


def manifest(paths, relative_to):
    entries=[]
    for path in sorted(paths, key=lambda item: item.as_posix().lower()):
        if '__pycache__' in path.parts or not path.is_file():
            continue
        entries.append({'path': path.relative_to(relative_to).as_posix(),
                        'bytes': path.stat().st_size, 'sha256': digest(path)})
    aggregate=hashlib.sha256(json.dumps(entries,sort_keys=True,
        separators=(',',':')).encode()).hexdigest()
    return {'algorithm':'sha256','aggregate_sha256':aggregate,
            'file_count':len(entries),'files':entries}


def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    current=manifest((path for path in ROOT.rglob('*') if project_file(path)), ROOT)
    baseline=manifest((path for path in BASELINE.rglob('*')), BASELINE)
    (DOCS/'final_source_manifest.json').write_text(
        json.dumps(current,indent=2)+'\n',encoding='utf-8')
    (DOCS/'baseline_source_manifest.json').write_text(
        json.dumps(baseline,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'final':{k:current[k] for k in ('aggregate_sha256','file_count')},
                      'baseline':{k:baseline[k] for k in ('aggregate_sha256','file_count')}},indent=2))


if __name__ == '__main__':
    main()
