"""Advisory discovery and reviewed, non-destructive ERP contact enrichment."""
from difflib import SequenceMatcher
import json
import re
import unicodedata
from .coordination import CaseRepository, uid, timestamp

SCHEMA='''
CREATE TABLE IF NOT EXISTS contact_observations (
 observation_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,account_id TEXT,
 field TEXT NOT NULL,value TEXT NOT NULL,message_id TEXT NOT NULL,
 start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,at TEXT NOT NULL,
 UNIQUE(case_id,message_id,field,value));
CREATE TABLE IF NOT EXISTS contact_decisions (
 decision_id TEXT PRIMARY KEY,observation_id TEXT NOT NULL,case_id TEXT NOT NULL,
 decision TEXT NOT NULL,reviewer TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approved_contacts (
 contact_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,field TEXT NOT NULL,value TEXT NOT NULL,
 observation_id TEXT NOT NULL UNIQUE,reviewer TEXT NOT NULL,at TEXT NOT NULL);
'''

def folded(value):
    value=''.join(c for c in unicodedata.normalize('NFKD',value or '').casefold() if not unicodedata.combining(c))
    return ' '.join(sorted(re.findall(r'\w+',value)))

def candidates(store,name='',serial='',limit=5):
    """Candidate scores do not satisfy even one authoritative verification point."""
    scores={}
    if name:
        for row in store.query('SELECT account_id,customer_name FROM customers'):
            score=SequenceMatcher(None,folded(name),folded(row['customer_name'])).ratio()
            if score>=.65: scores[row['account_id']]={**row,'name_similarity':round(score,3),'serial_similarity':0}
    if serial:
        normalized=re.sub(r'[^A-Z0-9]','',serial.upper())
        for row in store.query('SELECT d.account_id,d.serial_number,c.customer_name FROM devices d JOIN customers c ON c.account_id=d.account_id'):
            score=SequenceMatcher(None,normalized,re.sub(r'[^A-Z0-9]','',row['serial_number'].upper())).ratio()
            if score>=.72:
                result=scores.setdefault(row['account_id'],{'account_id':row['account_id'],'customer_name':row['customer_name'],'name_similarity':0,'serial_similarity':0})
                result['serial_similarity']=max(result['serial_similarity'],round(score,3))
    ranked=sorted(scores.values(),key=lambda r:max(r['name_similarity'],r['serial_similarity']),reverse=True)[:limit]
    return {'candidates':ranked,'authority':'advisory_only','verification_points_granted':0,'requires_exact_evidence':True}

def observe_contacts(store,cid,message,verified_account_id=None):
    case=store.case(cid); body=message['body'] or ''
    found=[]
    patterns=[('email',r'(?P<value>\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b)'),
              ('phone',r'\b(?:phone|tel|call me at)\s*:?\s*(?P<value>\+?\d[\d () .-]{6,}\d)')]
    for field,pattern in patterns:
      for match in re.finditer(pattern,body,re.I):
        oid=uid('CONTACT-OBS')
        store.execute('INSERT OR IGNORE INTO contact_observations VALUES(?,?,?,?,?,?,?,?,?)',
            (oid,cid,verified_account_id or case.get('account_id'),field,match.group('value'),message['message_id'],match.start('value'),match.end('value'),timestamp()))
        found.append({'field':field,'value':match.group('value')})
    return {'observed_contacts':found,'promoted':False,'authority':'customer_report'}

def review_contact(store,oid,decision,reviewer,reason,version):
    if decision not in ('approve','reject') or not reviewer.strip() or not reason.strip(): raise ValueError('Named review and rationale required')
    obs=store.one('SELECT * FROM contact_observations WHERE observation_id=?',(oid,))
    if not obs: raise ValueError('Unknown observation')
    case=store.case(obs['case_id']);repo=CaseRepository(store)
    if decision=='approve' and (not case['identity_verified'] or not case['account_id'] or obs['account_id']!=case['account_id']):
        raise ValueError('Exact verified account relationship required before contact promotion')
    with store.transaction():
        repo.commit(case['case_id'],version,'contact_review',reviewer,{'observation_id':oid,'decision':decision,'reason':reason},uid('CONTACT-CMD'))
        if store.one('SELECT 1 FROM contact_decisions WHERE observation_id=?',(oid,)): raise ValueError('Observation already reviewed')
        store.insert('contact_decisions',dict(decision_id=uid('CONTACT-DEC'),observation_id=oid,case_id=case['case_id'],decision=decision,reviewer=reviewer,reason=reason,at=timestamp()))
        if decision=='approve':
            store.insert('approved_contacts',dict(contact_id=uid('CONTACT'),account_id=case['account_id'],field=obs['field'],value=obs['value'],observation_id=oid,reviewer=reviewer,at=timestamp()))
            # An existing different value is preserved. The approved alternate
            # retains provenance instead of overwriting canonical ERP data.
            if obs['field'] not in ('email','phone'):raise ValueError('Unsupported contact field')
            field=obs['field']
            store.execute(f"UPDATE customers SET {field}=? WHERE account_id=? AND ({field} IS NULL OR trim({field})='')",(obs['value'],case['account_id']))
    return {'ok':True,'decision':decision,'canonical_existing_value_preserved':True}
