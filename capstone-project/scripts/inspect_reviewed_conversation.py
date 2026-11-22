"""Read the user-referenced fictional case/conversations without changing state."""
import json
from pathlib import Path
import sqlite3

root = Path(__file__).resolve().parents[1]
connection = sqlite3.connect(f"file:{(root / 'data/operations.db').as_posix()}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
result = {'source': 'read-only operational SQLite', 'case_id_requested': 'CASE-000080'}
case = connection.execute('SELECT * FROM cases WHERE case_id=?', ('CASE-000080',)).fetchone()
result['case'] = dict(case) if case else None
conversations = [dict(row) for row in connection.execute('SELECT * FROM conversations WHERE case_id=? OR state_json LIKE ?', ('CASE-000080', '%mei murphy%'))]
for row in conversations:
    row['state'] = json.loads(row.pop('state_json'))
result['conversations'] = conversations
for table in ('checklist_items', 'retrieved_sources', 'tool_calls', 'audit_events'):
    result[table] = [dict(row) for row in connection.execute(f'SELECT * FROM {table} WHERE case_id=?', ('CASE-000080',))]
connection.close()
output = root / 'docs/evidence/conversation_trace/observed-persisted-case.json'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'output': str(output), 'case_found': case is not None,
                  'conversations': len(conversations),
                  'handoff_reasons': [c['state'].get('handoff_reason') for c in conversations]}, indent=2))
