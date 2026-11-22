"""Record source hashes and a consistent SQLite backup in an existing backup folder."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
destination = Path(sys.argv[1])
source = sqlite3.connect(f"file:{(root / 'data/operations.db').as_posix()}?mode=ro", uri=True)
target = sqlite3.connect(destination / "data/operations-consistent-backup.db")
source.backup(target)
target.close()
source.close()
excluded = {'.venv', '.venv-new', '.test_deps', '__pycache__', '.pytest_cache', '.idea', '.git', '.aws'}
manifest = []
for path in root.rglob('*'):
    if not path.is_file() or any(p in excluded for p in path.relative_to(root).parts):
        continue
    if path.suffix in {'.zip', '.pyc'} or path.name.startswith('.env'):
        continue
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = None
    manifest.append({'path': str(path.relative_to(root)), 'bytes': path.stat().st_size, 'sha256': digest})
(destination / 'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(f'{len(manifest)} files recorded; consistent database backup saved')
