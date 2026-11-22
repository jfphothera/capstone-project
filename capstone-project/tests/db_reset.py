"""Test-only reset of a COPIED operations database.

Audit, approval and activity tables are append-only for the application
(store triggers). Test fixtures that start from a copy of the seed database
clear that copy with a raw connection before the Store opens it: the
triggers are dropped on the throwaway copy only and recreated when Store()
opens the file. Never used on a live database.
"""
import sqlite3

APPEND_ONLY_TRIGGERS = ('immutable_audit_events_update', 'immutable_audit_events_delete',
                        'immutable_approvals_update', 'immutable_approvals_delete',
                        'immutable_activity_events_update', 'immutable_activity_events_delete')


def clear_copy(path, tables, extra_sql=()):
    with sqlite3.connect(path) as db:
        for name in APPEND_ONLY_TRIGGERS:
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table in existing:
                db.execute(f'DELETE FROM {table}')
        for sql in extra_sql:
            db.execute(sql)
