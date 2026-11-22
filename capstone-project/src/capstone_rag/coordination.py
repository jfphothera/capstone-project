"""Versioned case proposals, append-only documentation and atomic coordination.

Specialists propose findings. This repository owns commits; no specialist tool
can send a message, approve itself, change QMS or execute inventory movements.
"""
import json
import time
import uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_events (
 event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, version INTEGER NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL,
 command_id TEXT NOT NULL UNIQUE, UNIQUE(case_id,version));
CREATE TABLE IF NOT EXISTS case_notes (
 note_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
 body TEXT NOT NULL, author TEXT NOT NULL, authority TEXT NOT NULL,
 evidence TEXT NOT NULL, amends_note_id TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_tag_events (
 event_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, tag TEXT NOT NULL,
 operation TEXT NOT NULL, author TEXT NOT NULL, evidence TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contact_tasks (
 task_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
 status TEXT NOT NULL, endpoint TEXT, evidence TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS specialist_findings (
 finding_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, role TEXT NOT NULL,
 based_on_version INTEGER NOT NULL, payload TEXT NOT NULL, at TEXT NOT NULL, command_id TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS case_leases (
 case_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coordination_outbox (
 command_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL);
"""

class StaleCase(ValueError): pass
class CaseBusy(ValueError): pass

def timestamp():
    from .store import now
    return now()

def uid(prefix): return prefix+'-'+uuid.uuid4().hex

class CaseRepository:
    def __init__(self,store): self.store=store

    def version(self,cid):
        case=self.store.case(cid)
        if not case: raise ValueError('Unknown case')
        return case['case_version']

    def commit(self,cid,version,kind,actor,payload,command_id):
        """CAS + event + outbox commit together, including command deduplication."""
        with self.store.transaction():
            duplicate=self.store.one('SELECT * FROM case_events WHERE command_id=?',(command_id,))
            if duplicate:
                if duplicate['case_id']!=cid or duplicate['kind']!=kind or json.loads(duplicate['payload'])!=payload:
                    raise ValueError('Idempotency key reused for a different command')
                return duplicate['version']
            cursor=self.store.execute('UPDATE cases SET case_version=case_version+1 WHERE case_id=? AND case_version=?',(cid,version))
            if cursor.rowcount!=1: raise StaleCase('Case changed; refresh and re-evaluate the proposal')
            new=version+1
            self.store.insert('case_events',dict(event_id=uid('EV'),case_id=cid,version=new,kind=kind,actor=actor,
                payload=json.dumps(payload),at=timestamp(),command_id=command_id))
            self.store.insert('coordination_outbox',dict(command_id=command_id,case_id=cid,kind=kind,
                payload=json.dumps(payload),status='recorded',at=timestamp()))
            return new

    def finding(self,cid,role,payload,version,command_id):
        with self.store.transaction():
            self.commit(cid,version,'specialist_finding',role,payload,command_id)
            old=self.store.one('SELECT finding_id FROM specialist_findings WHERE command_id=?',(command_id,))
            if old: return old['finding_id']
            fid=uid('FIND')
            self.store.insert('specialist_findings',dict(finding_id=fid,case_id=cid,role=role,
                based_on_version=version,payload=json.dumps(payload),at=timestamp(),command_id=command_id))
            return fid

    def note(self,cid,body,author,*,kind='representative',evidence=None,amends=None,version=None,command_id=None):
        if not body.strip() or not author.strip(): raise ValueError('A named author and note text are required')
        if amends and not self.store.one('SELECT 1 FROM case_notes WHERE note_id=? AND case_id=?',(amends,cid)):
            raise ValueError('Amendment must reference a note in the same case')
        command_id=command_id or uid('NOTE-CMD')
        payload=dict(body=body,author=author,kind=kind,evidence=evidence or [],amends=amends)
        with self.store.transaction():
            prior=self.store.one('SELECT payload FROM case_events WHERE command_id=?',(command_id,))
            self.commit(cid,self.version(cid) if version is None else version,'note_appended',author,payload,command_id)
            if prior: return {'duplicate':True}
            nid=uid('NOTE')
            self.store.insert('case_notes',dict(note_id=nid,case_id=cid,kind=kind,body=body,author=author,
                authority='human_statement' if kind=='representative' else 'agent_observation',
                evidence=json.dumps(evidence or []),amends_note_id=amends,at=timestamp()))
            return {'note_id':nid}

    def tag(self,cid,tag,operation,author,evidence,version):
        allowed={'callback_required','transcript_missing','internal_task','document_only','safety_review','identity_ambiguous','follow_up','manual_support'}
        if tag not in allowed or operation not in ('add','remove') or not author.strip(): raise ValueError('Invalid typed tag event')
        with self.store.transaction():
            self.commit(cid,version,'tag_'+operation,author,{'tag':tag,'evidence':evidence},uid('TAG-CMD'))
            self.store.insert('case_tag_events',dict(event_id=uid('TAG'),case_id=cid,tag=tag,operation=operation,
                author=author,evidence=json.dumps(evidence),at=timestamp()))

    @contextmanager
    def lease(self,cid,seconds=600):
        owner=uid('LEASE')
        with self.store.transaction():
            old=self.store.one('SELECT * FROM case_leases WHERE case_id=?',(cid,))
            if old and old['expires_at']>time.time(): raise CaseBusy('Case investigation is already leased')
            self.store.upsert('case_leases',{'case_id':cid,'owner':owner,'expires_at':time.time()+seconds},'case_id')
        try: yield owner
        finally: self.store.execute('DELETE FROM case_leases WHERE case_id=? AND owner=?',(cid,owner))

    def snapshot(self,cid):
        snapshot={'case':self.store.case(cid),**{table:self.store.query(f'SELECT * FROM {table} WHERE case_id=?',(cid,))
            for table in ('case_events','case_notes','case_tag_events','contact_tasks','specialist_findings','coordination_outbox')}}
        from .collaboration import Coordinator
        snapshot['collaboration']=Coordinator(self.store,'snapshot').trace(cid)
        return snapshot
