"""Export changed/new/missing files against the pre-repair source manifest."""
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT.parent / 'Capstone-Backup-2026-10-27-conversation-trace/SOURCE_MANIFEST.json'
OUT = Path(sys.argv[2]) if len(sys.argv)>2 else ROOT / 'docs/evidence/conversation_trace/changed-files.json'
excluded = {'.venv', '.venv-new', '.test_deps', '__pycache__', '.pytest_cache', '.idea', '.git', '.aws'}
before = {str(Path(row['path'])): row for row in json.loads(BASE.read_text(encoding='utf-8'))}
current = {}
for directory, folders, files in os.walk(ROOT):
    folders[:] = [name for name in folders if name not in excluded]
    for name in files:
        path = Path(directory) / name
        if path == OUT or path.suffix in {'.zip', '.pyc'} or name.startswith('.env'):
            continue
        relative = str(path.relative_to(ROOT))
        try:
            current[relative] = {'path': relative, 'bytes': path.stat().st_size,
                                 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        except OSError:
            current[relative] = {'path': relative, 'sha256': None}
changes = []
for relative, row in current.items():
    old = before.get(relative)
    if not old or old['sha256'] != row['sha256']:
        category = 'runtime/evidence' if relative.startswith(('docs\\evidence\\', 'data\\index\\', 'data\\_')) or '.db' in relative else 'implementation/documentation'
        changes.append({**row, 'change': 'modified' if old else 'new', 'category': category,
                        'previous_sha256': old['sha256'] if old else None})
changes += [{'path': p, 'change': 'missing'} for p in before if p not in current and p != str(OUT.relative_to(ROOT))]
OUT.write_text(json.dumps({'baseline': str(BASE), 'excluded': sorted(excluded),
    'note': 'ZIP archives, bytecode, environment/credential files and this self-referential manifest excluded. Runtime files may continue changing while server runs.',
    'changes': sorted(changes, key=lambda r:r['path'])}, indent=2)+'\n', encoding='utf-8')
print(f'{len(changes)} changed/new/missing files recorded at {OUT}')
