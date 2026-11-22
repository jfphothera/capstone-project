"""Compare original operational values and protected artifacts with the new backup."""
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT=Path(__file__).resolve().parents[1]
BACKUP=ROOT.parent/'Capstone-Backup-2026-10-27-customer-operations'
out=ROOT/'docs/evidence/customer_operations/preservation.json'
old=sqlite3.connect(f"file:{(BACKUP/'data/operations-consistent-backup.db').as_posix()}?mode=ro&immutable=1",uri=True)
live=sqlite3.connect(f"file:{(ROOT/'data/operations.db').as_posix()}?mode=ro",uri=True)
report={'backup':str(BACKUP),'tables':[],'protected_files':[]}
for (name,) in old.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
    quote=lambda s:'"'+s.replace('"','""')+'"'
    before_cols=[row[1] for row in old.execute(f'PRAGMA table_info({quote(name)})')]
    now_cols=[row[1] for row in live.execute(f'PRAGMA table_info({quote(name)})')]
    columns=','.join(map(quote,before_cols))
    before=old.execute(f'SELECT {columns} FROM {quote(name)}').fetchall()
    after=live.execute(f'SELECT {columns} FROM {quote(name)}').fetchall()
    removed=[row for row in before if row not in after]
    extra=[row for row in after if row not in before]
    report['tables'].append({'table':name,'original_rows':len(before),'current_rows':len(after),
        'all_original_values_retained':not removed,'additional_rows':len(extra),
        'added_columns':[column for column in now_cols if column not in before_cols],
        'changed_or_missing_original_rows':removed})
old.close();live.close()
manifest=json.loads((BACKUP/'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
for row in manifest:
    relative=row['path'].replace('\\','/')
    if relative in ('data/ticket_log.jsonl','data/escalations.jsonl','data/memory/resolutions.jsonl') or relative.startswith('data/labels/'):
        file=ROOT/relative
        digest=hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
        report['protected_files'].append({'path':relative,'matches_backup':digest==row['sha256']})
report['passed']=all(t['all_original_values_retained'] for t in report['tables']) and all(f['matches_backup'] for f in report['protected_files'])
out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'passed':report['passed'],'tables':len(report['tables']),'protected_files':len(report['protected_files']),'output':str(out)},indent=2))
