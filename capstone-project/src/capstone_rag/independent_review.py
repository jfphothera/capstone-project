"""Frozen-evidence validation and bounded artifact correction.

The reviewer may identify problems and request rework.  It cannot change
identity, revision authority, QMS authorization, safety, approvals, or an
already proposed operational transaction.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import response_policy
from .coordination import timestamp, uid


SCHEMA = """
CREATE TABLE IF NOT EXISTS validation_packages (
 package_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 cycle INTEGER NOT NULL, case_version INTEGER NOT NULL, package_hash TEXT NOT NULL,
 payload TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(workflow_id,cycle));
CREATE TABLE IF NOT EXISTS validation_verdicts (
 verdict_id TEXT PRIMARY KEY, package_id TEXT NOT NULL, case_id TEXT NOT NULL,
 workflow_id TEXT NOT NULL, cycle INTEGER NOT NULL, passed INTEGER NOT NULL,
 issues TEXT NOT NULL, correction_target TEXT, human_escalation INTEGER NOT NULL,
 semantic_review TEXT, at TEXT NOT NULL, UNIQUE(workflow_id,cycle));
CREATE TABLE IF NOT EXISTS correction_cycles (
 correction_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 cycle INTEGER NOT NULL, verdict_id TEXT NOT NULL, target TEXT NOT NULL,
 original_artifact_id TEXT, corrected_artifact_id TEXT, status TEXT NOT NULL,
 detail TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(workflow_id,cycle));
CREATE TABLE IF NOT EXISTS artifact_versions (
 artifact_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workflow_id TEXT NOT NULL,
 kind TEXT NOT NULL, source_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 payload TEXT NOT NULL, supersedes_artifact_id TEXT, status TEXT NOT NULL,
 at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_validation_records (
 record_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, predecessor_artifact_id TEXT,
 case_id TEXT NOT NULL, workflow_id TEXT NOT NULL, cycle INTEGER NOT NULL,
 package_id TEXT NOT NULL, evidence_snapshot_hash TEXT NOT NULL,
 authoritative_refs TEXT NOT NULL, policy_results TEXT NOT NULL,
 semantic_review TEXT, issues TEXT NOT NULL, resolved_issue_ids TEXT NOT NULL,
 unresolved_issue_ids TEXT NOT NULL, new_issue_ids TEXT NOT NULL,
 disposition TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(workflow_id,cycle,artifact_id));
"""

MAX_CORRECTION_CYCLES = 2
SERIAL_LABEL = re.compile(r'(?i)\b(serial(?: number)?(?: is)?)[\s:#-]+([A-Z0-9]*\d[A-Z0-9:-]{4,23})')
MALFORMED_SERIAL = re.compile(r'\b\d{3,5}:\d{2}-[A-Z]{2}\d\b', re.I)
PROCEDURE = re.compile(r'\bPROC-[A-Z0-9-]+\b')


def _decode(value, default=None):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value if default is None else default


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _strings(value, path=''):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f'{path}.{key}' if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f'{path}[{index}]')


def _latest_artifact(store, case_id, source_id):
    row = store.one(
        'SELECT * FROM artifact_versions WHERE case_id=? AND source_id=? '
        'ORDER BY cycle DESC,at DESC LIMIT 1', (case_id, source_id))
    return _decode(row['payload']) if row else None


def freeze(store, case_id, workflow_id, state, cycle):
    case = store.case(case_id)
    latest = store.one(
        "SELECT * FROM messages WHERE case_id=? AND direction='outbound' "
        "AND status='draft' ORDER BY created_at DESC,message_id DESC LIMIT 1",
        (case_id,))
    findings=[]
    for row in store.query(
            'SELECT * FROM specialist_findings WHERE case_id=? ORDER BY at', (case_id,)):
        replacement=_latest_artifact(store,case_id,row['finding_id'])
        findings.append({**row,'payload':replacement if replacement is not None else _decode(row['payload'])})
    package={
        'case': case,
        'case_version': case['case_version'],
        'device': store.device(case.get('serial_number')) if case.get('serial_number') else None,
        'messages': store.messages(case_id),
        'draft': latest,
        'specialist_findings': findings,
        'authorized_qms': state.get('qms') or {},
        'selected_plan': state.get('planning') or {},
        'operational_result': state.get('result') or {},
        'plan_transition': store.one(
            'SELECT * FROM plan_transitions WHERE case_id=? AND workflow_id=? ORDER BY at DESC LIMIT 1',
            (case_id,workflow_id)),
        'authoritative_boundary': {
            'reviewer_may_change': ['advisory finding','customer draft','plan proposal'],
            'reviewer_may_not_change': ['identity','device ownership','revision authority',
                'QMS authorization','safety hold','human approval','executed operation']}}
    payload=_canonical(package);package_hash=hashlib.sha256(payload.encode()).hexdigest()
    existing=store.one('SELECT * FROM validation_packages WHERE workflow_id=? AND cycle=?',
                       (workflow_id,cycle))
    if existing:
        return existing,_decode(existing['payload'])
    row={'package_id':uid('VALPKG'),'case_id':case_id,'workflow_id':workflow_id,
         'cycle':cycle,'case_version':case['case_version'],'package_hash':package_hash,
         'payload':payload,'at':timestamp()}
    store.insert('validation_packages',row)
    return row,package


def issue(category,severity,message,references,artifact,target,human=False,code=None):
    value={'issue_category':category,'severity':severity,'message':message,
            'evidence_references':list(references),'affected_artifact':artifact,
            'recommended_correction_target':target,'human_escalation_required':bool(human)}
    value['issue_code']=code or category
    value['issue_id']='ISSUE-'+hashlib.sha256(
        f"{category}|{value['issue_code']}|{target}".encode()).hexdigest()[:20]
    return value


def _identifier_issues(package):
    issues=[];case=package['case'];canonical=case.get('serial_number')
    if not canonical:
        return issues
    artifacts=[]
    for finding in package['specialist_findings']:
        if finding['role'] in ('qms_research','resolution_planning','operations','erp_research'):
            artifacts.append((finding['finding_id'],finding['role'],finding['payload']))
    if package.get('draft'):
        artifacts.append((package['draft']['message_id'],'communication',package['draft'].get('body','')))
    for artifact_id,role,payload in artifacts:
        for path,text in _strings(payload):
            candidates=[match.group(2) for match in SERIAL_LABEL.finditer(text)]
            candidates+=MALFORMED_SERIAL.findall(text)
            for candidate in dict.fromkeys(candidates):
                compact=re.sub(r'[\s-]','',candidate).upper()
                expected=re.sub(r'[\s-]','',canonical).upper()
                if compact!=expected:
                    issues.append(issue('identifier_inconsistency','high',
                        f'Advisory/output identifier {candidate!r} does not match authoritative serial {canonical!r}',
                        [f'case.serial_number:{canonical}',f'{artifact_id}:{path}'],artifact_id,
                        'communication' if role=='communication' else role))
    return issues


VALIDATOR_VERSION = 'frozen-evidence-validator-2026-11-19.1'  # rc.25: task-scoped mandatory context


def _document(chunk_id):
    return str(chunk_id).split('#', 1)[0]


def required_context(contract, used):
    """Mandatory context chunks applicable to the documents the draft uses."""
    mandatory = set((contract or {}).get('mandatory_context_chunk_ids', []))
    used_documents = {_document(u) for u in used or ()}
    return {m for m in mandatory if _document(m) in used_documents}


def evaluate(store, package_row, package, semantic_review=None):
    case=package['case'];draft=package.get('draft');issues=[];deterministic={'passed':True,'failures':[]}
    issues.extend(_identifier_issues(package))
    if draft:
        deterministic=response_policy.validate_case(store,case['case_id'],draft.get('body',''))
        for failure in deterministic.get('failures',[]):
            claim_kind=failure.get('claim_kind') if isinstance(failure,dict) else None
            category='unsupported_consequential_claim' if claim_kind else 'unsupported_or_contradictory_statement'
            issues.append(issue(category,'high',
                failure.get('fact') or failure.get('reason') or str(failure),
                [f"message:{draft['message_id']}"]+list(failure.get('evidence_refs',[])),
                draft['message_id'],'communication',code=claim_kind or category))
        metadata=_decode(draft.get('metadata') or '{}',{}) or {}
        authorized=set((package.get('authorized_qms') or {}).get('sources',[]))
        used=set(metadata.get('source_chunk_ids',[]))
        if used-authorized:
            issues.append(issue('qms_evidence_mismatch','critical',
                'Draft cites chunks outside the frozen authorized QMS package',
                sorted(used-authorized),draft['message_id'],'qms_research',True))
        contract=(package.get('authorized_qms') or {}).get('evidence_contract') or {}
        # rc.25: mandatory scope/safety/prerequisite sections are required for
        # the documents whose content the draft actually uses. A co-retrieved
        # manual does not make its safety prelude mandatory in a logistics
        # answer; a used manual task still requires its own safety sections.
        mandatory=required_context(contract,used)
        if used and not mandatory.issubset(used):
            issues.append(issue('mandatory_context_missing','critical',
                'Draft evidence omits mandatory scope/safety/prerequisite chunks',
                sorted(mandatory-used),draft['message_id'],'qms_research',True))
        allowed_procedures=set(contract.get('procedure_ids',[]))
        for procedure in PROCEDURE.findall(draft.get('body','')):
            if procedure not in allowed_procedures:
                issues.append(issue('unsupported_procedure_reference','critical',
                    f'Procedure {procedure} is not in the frozen authorized evidence contract',
                    [f'message:{draft["message_id"]}'],draft['message_id'],'qms_research',True))
    transition=package.get('plan_transition') or {}
    plan=package.get('selected_plan') or {}
    if transition and plan and transition.get('selected_action')!=plan.get('selected_action'):
        issues.append(issue('operational_disposition_mismatch','critical',
            'Recorded operational transition does not match the selected plan',
            [transition.get('transition_id'),plan.get('selected_plan_id')],
            transition.get('transition_id'),'resolution_planning',True))
    for conflict in (plan.get('conflicts') or []):
        issues.append(issue('specialist_conflict','high','Unresolved specialist conflict remains',
                            [str(conflict)],plan.get('selected_plan_id'),'resolution_planning',True))
    if semantic_review and not semantic_review.get('passed',True):
        # The model critic's opinions are ADVISORY. They are recorded on the
        # verdict and shown to the reviewer, but they do not block. Field
        # evidence (rc.15 Testing Lab): a small-model critic vetoed correct
        # identity clarifications and grounded troubleshooting, then the
        # generic fallback, so every methodology ended in human review.
        # Deterministic checks above remain the only blocking gate.
        for text in semantic_review.get('issues',[])[:3]:
            issues.append(issue('semantic_quality','advisory',str(text),
                [package_row['package_id']],draft['message_id'] if draft else 'case',
                'communication' if draft else 'resolution_planning',
                code='review_unavailable' if semantic_review.get('review_unavailable') else 'semantic_quality'))
    for concern in (semantic_review or {}).get('supervisor_concerns') or []:
        # D (rc.25): a supervisor stop is an advisory unresolved concern the
        # reviewer must see; it neither blocks by itself nor disappears.
        issues.append(issue('supervisor_concern','advisory',
            f"Supervisor recommended stopping after {concern.get('role')}: {concern.get('concern') or 'no concern stated'}",
            [package_row['package_id']],draft['message_id'] if draft else 'case','resolution_planning',code='supervisor_stop'))
    # Distinct by category/artifact/message, retaining first authoritative reference.
    unique=[];seen=set()
    for value in issues:
        key=(value['issue_category'],value['affected_artifact'],value['message'])
        if key not in seen:seen.add(key);unique.append(value)
    blocking=[value for value in unique if value['severity']!='advisory']
    passed=not blocking
    human=any(value['human_escalation_required'] or value['severity']=='critical' for value in blocking)
    target=next((value['recommended_correction_target'] for value in blocking
                 if not value['human_escalation_required']),None)
    existing=store.one('SELECT * FROM validation_verdicts WHERE workflow_id=? AND cycle=?',
                       (package_row['workflow_id'],package_row['cycle']))
    if existing:return existing
    row={'verdict_id':uid('VERDICT'),'package_id':package_row['package_id'],
         'case_id':package_row['case_id'],'workflow_id':package_row['workflow_id'],
         'cycle':package_row['cycle'],'passed':int(passed),'issues':_canonical(unique),
         'correction_target':target,'human_escalation':int(human),
         'semantic_review':_canonical(semantic_review) if semantic_review else None,
         'at':timestamp()}
    store.insert('validation_verdicts',row)
    prior=store.one('SELECT * FROM validation_verdicts WHERE workflow_id=? AND cycle<? ORDER BY cycle DESC LIMIT 1',
                    (package_row['workflow_id'],package_row['cycle']))
    prior_ids={i.get('issue_id') for i in _decode(prior['issues'],[]) if i.get('issue_id')} if prior else set()
    current_ids={i['issue_id'] for i in unique}
    resolved=sorted(prior_ids-current_ids);unresolved=sorted(prior_ids&current_ids);new=sorted(current_ids-prior_ids)
    disposition='pass' if passed else ('human_review' if human or package_row['cycle']>=MAX_CORRECTION_CYCLES else 'rework')
    artifact_id=draft['message_id'] if draft else (package.get('selected_plan') or {}).get('selected_plan_id') or 'case'
    predecessor=draft.get('parent_message_id') if draft else None
    refs=set((package.get('authorized_qms') or {}).get('sources',[]))
    refs.update(deterministic.get('authoritative_operational_refs',[]))
    refs.add(package_row['package_id'])
    store.insert('artifact_validation_records',{'record_id':uid('ARTVAL'),'artifact_id':artifact_id,
        'predecessor_artifact_id':predecessor,'case_id':package_row['case_id'],
        'workflow_id':package_row['workflow_id'],'cycle':package_row['cycle'],
        'package_id':package_row['package_id'],'evidence_snapshot_hash':package_row['package_hash'],
        'authoritative_refs':_canonical(sorted(refs)),'policy_results':_canonical({**deterministic,'validator_version':VALIDATOR_VERSION}),
        'semantic_review':_canonical(semantic_review) if semantic_review else None,
        'issues':_canonical(unique),'resolved_issue_ids':_canonical(resolved),
        'unresolved_issue_ids':_canonical(unresolved),'new_issue_ids':_canonical(new),
        'disposition':disposition,'at':timestamp()})
    return row


def _sanitize_text(text, canonical):
    original=text
    if canonical:
        text=MALFORMED_SERIAL.sub(canonical,text)
        def labelled(match):
            candidate=match.group(2)
            compact=re.sub(r'[\s-]','',candidate).upper()
            expected=re.sub(r'[\s-]','',canonical).upper()
            return match.group(1)+' '+(canonical if compact!=expected else candidate)
        text=SERIAL_LABEL.sub(labelled,text)
    else:
        text=MALFORMED_SERIAL.sub('the device serial',text)
    return text, text!=original


def _sanitize_payload(value,canonical):
    changed=False
    if isinstance(value,str):return _sanitize_text(value,canonical)
    if isinstance(value,list):
        out=[]
        for item in value:
            fixed,did=_sanitize_payload(item,canonical);out.append(fixed);changed|=did
        return out,changed
    if isinstance(value,dict):
        out={}
        for key,item in value.items():
            fixed,did=_sanitize_payload(item,canonical);out[key]=fixed;changed|=did
        return out,changed
    return value,False


def correct(store, package, verdict, cycle):
    if cycle>MAX_CORRECTION_CYCLES:raise ValueError('Correction retry budget exhausted')
    issues=_decode(verdict['issues'],[]) or []
    if verdict['human_escalation']:
        return {'status':'human_escalation','changed':False,'reason':'critical safety/authorization/conflict issue'}
    target=verdict.get('correction_target') or 'communication'
    canonical=package['case'].get('serial_number')
    original_id=None;corrected_id=None;detail={}
    if target=='communication' and package.get('draft'):
        draft=package['draft'];original_id=draft['message_id']
        body,changed=_sanitize_text(draft.get('body',''),canonical)
        blocking_claim=any(i.get('issue_category')=='unsupported_consequential_claim' for i in issues)
        semantic_problem=any(i.get('issue_category')=='semantic_quality' for i in issues)
        if blocking_claim or semantic_problem:
            body=(f"Hello {package['case'].get('customer_name') or 'there'},\n\n"
                  "Thank you for your message. A support representative will review your request and respond with the next supported step.\n\n"
                  "Kind regards,\nCustomer Support")
            changed=body!=draft.get('body','')
        elif not changed:
            body=(f"Hello {package['case'].get('customer_name') or 'there'},\n\n"
                  "Thank you for the information provided. A representative is reviewing the documented evidence and will confirm the supported next step.\n\n"
                  "Kind regards,\nCustomer Support")
            changed=body!=draft.get('body','')
        corrected_id=f"{package['case']['case_id']}-CORR-{cycle}-{uid('DRAFT')[-8:]}"
        store.update('messages','message_id',draft['message_id'],{'status':'validation_failed'})
        store.insert('messages',{**{key:draft.get(key) for key in (
            'case_id','run_id','channel','author','subject')},'message_id':corrected_id,
            'created_at':timestamp(),'direction':'outbound','body':body,'status':'draft',
            'approved_by':None,'approved_at':None,'parent_message_id':draft['message_id'],
            'metadata':_canonical({'kind':'validation_correction','supersedes':draft['message_id'],
                                   'correction_cycle':cycle})})
        detail={'body_changed':changed,'deterministic_preview':response_policy.validate_case(
            store,package['case']['case_id'],body),'full_validation_pending':True,
            'prior_issue_ids':[i.get('issue_id') for i in issues]}
    else:
        finding=next((item for item in package['specialist_findings']
                      if item['finding_id']==next((i['affected_artifact'] for i in issues
                                                  if i['recommended_correction_target']==target),None)),None)
        if not finding:
            finding=next((item for item in package['specialist_findings'] if item['role']==target),None)
        if finding:
            original_id=finding['finding_id'];fixed,changed=_sanitize_payload(finding['payload'],canonical)
            if target=='resolution_planning' and isinstance(fixed,dict) and not changed:
                alternatives=[p for p in fixed.get('plans',[]) if p.get('feasible') or not p.get('hard_gate_verdicts')]
                alternative=next((p for p in alternatives if p.get('action')!=fixed.get('selected_action')),None)
                if alternative:
                    fixed={**fixed,'reconsideration':{
                        'action':alternative.get('action'),
                        'action_sequence':alternative.get('action_sequence',[]),
                        'authority':'proposal_only_no_execution',
                        'reason':'Independent validation requested plan reconsideration'}}
                    changed=True
            corrected_id=uid('ARTIFACT') if changed else None
            if changed:
                store.insert('artifact_versions',{'artifact_id':corrected_id,
                    'case_id':package['case']['case_id'],'workflow_id':verdict['workflow_id'],
                    'kind':'specialist_finding_correction','source_id':finding['finding_id'],
                    'cycle':cycle,'payload':_canonical(fixed),'supersedes_artifact_id':finding['finding_id'],
                    'status':'corrected','at':timestamp()})
            detail={'payload_changed':changed,'role':finding['role']}
        else:
            detail={'changed':False,'reason':'No correctable artifact found'}
    row={'correction_id':uid('CORRECTION'),'case_id':package['case']['case_id'],
         'workflow_id':verdict['workflow_id'],'cycle':cycle,'verdict_id':verdict['verdict_id'],
         'target':target,'original_artifact_id':original_id,'corrected_artifact_id':corrected_id,
         'status':'produced_for_revalidation' if corrected_id else 'failed','detail':_canonical(detail),'at':timestamp()}
    store.insert('correction_cycles',row)
    return {**row,'changed':bool(corrected_id)}
