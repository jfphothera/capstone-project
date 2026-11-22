"""Preserve early test appends, restore original logs, and compare operational rows."""
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT.parent / 'Capstone-Backup-2026-10-27-conversation-trace'
OUT = ROOT / 'docs/evidence/conversation_trace/preservation'
OUT.mkdir(parents=True, exist_ok=True)
report = {'logs': [], 'operational_tables': []}
for name in ('data/ticket_log.jsonl', 'data/escalations.jsonl', 'data/memory/resolutions.jsonl'):
    current, original = ROOT / name, BACKUP / name
    before, after = original.read_bytes(), current.read_bytes()
    if before != after:
        if not after.startswith(before):
            raise RuntimeError(f'Unexpected non-append change: {name}; no restoration performed')
        archive = OUT / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists() and archive.read_bytes() != after:
            raise RuntimeError(f'Archive already differs: {archive}')
        archive.write_bytes(after)
        current.write_bytes(before)
    report['logs'].append({'path': name, 'restored_sha256': hashlib.sha256(current.read_bytes()).hexdigest(),
                           'matches_backup': current.read_bytes() == before,
                           'archived_test_version': str((OUT / name).relative_to(ROOT)) if (OUT / name).exists() else None})
live = sqlite3.connect(f"file:{(ROOT/'data/operations.db').as_posix()}?mode=ro", uri=True)
old = sqlite3.connect(f"file:{(BACKUP/'data/operations-consistent-backup.db').as_posix()}?mode=ro&immutable=1", uri=True)
for (name,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
    quoted = '"' + name.replace('"', '""') + '"'
    left = sorted(old.execute(f'SELECT * FROM {quoted}').fetchall(), key=repr)
    right = sorted(live.execute(f'SELECT * FROM {quoted}').fetchall(), key=repr)
    entry = {'table': name, 'rows_before': len(left), 'rows_now': len(right), 'identical': left == right}
    if left != right:
        entry['removed_or_changed'] = [row for row in left if row not in right]
        entry['added_or_changed'] = [row for row in right if row not in left]
    report['operational_tables'].append(entry)
live.close()
old.close()
(OUT/'verification.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(json.dumps(report, indent=2))
