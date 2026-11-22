"""Validate customer responses against authoritative facts before release."""
import re


CONSEQUENTIAL_TERMS = re.compile(
    r'\b(?:requested action|request|rma|return|replacement|refund|credit|shipment|'
    r'return label|warranty|entitlement|inventory movement)\b', re.I)
CONFIRMED_COMMITMENT = re.compile(
    r'\b(?:approved|authorized|confirmed|covered|cleared|granted|completed|dispatched|shipped|sent|'
    r'delivered|processed|issued|created)\b', re.I)
PENDING_OR_NEGATED = re.compile(
    r'\b(?:not|not yet|has not|have not|pending|awaiting|proposed|subject to|if|once|after|'
    r'requires?|requesting|will request|can request|for review)\b.{0,45}'
    r'\b(?:approved|approval|authorized|authorization|completed|dispatched|shipped|sent|delivered|created|review)\b|'
    r'\b(?:approved|authorized)\b.{0,25}\b(?:before|prior to|required)\b', re.I)


def _consequential_evidence(store, case_id):
    rmas=store.query('SELECT * FROM rmas WHERE case_id=?',(case_id,))
    shipments=store.query('SELECT * FROM sim_shipments WHERE case_id=?',(case_id,))
    return {'rmas':rmas,'shipments':shipments}


def consequential_claim_guard(store, case_id, text):
    """Validate operational commitments against current authoritative records.

    This is contextual: pending/conditional language is allowed, while a
    confirmed claim needs the matching approved or executed record.
    """
    evidence=_consequential_evidence(store,case_id);failures=[];checks=[]
    clauses=[c.strip() for c in re.split(r'(?<=[.!?;])\s+|\n+',text or '') if c.strip()]
    approved={'approved','executed'}
    dispatched={'carrier_accepted','picked_up','in_transit','delivered'}
    for clause in clauses:
        low=clause.lower()
        pending_claim=bool(CONSEQUENTIAL_TERMS.search(clause) and
            re.search(r'\b(?:pending|awaiting|proposed)\b.{0,35}\b(?:approval|authorization|review)\b',clause,re.I))
        if not CONFIRMED_COMMITMENT.search(clause) and not pending_claim:
            continue
        if PENDING_OR_NEGATED.search(clause):
            proposed=[r for r in evidence['rmas'] if r['status']=='proposed']
            asserts_pending=bool(re.search(r'\b(?:pending|awaiting|proposed)\b',clause,re.I)
                                 and CONSEQUENTIAL_TERMS.search(clause))
            supported=bool(proposed) if asserts_pending else True
            refs=[r['rma_id'] for r in proposed] if asserts_pending else []
            checks.append({'claim':clause,'classification':'pending_or_conditional',
                           'supported':supported,'evidence_refs':refs})
            if not supported:
                failures.append({'fact':'pending consequential action requires a matching proposed record',
                    'conflicting_text':clause,'claim_kind':'pending_without_proposal','evidence_refs':[]})
            continue
        kind=None;supported=False;refs=[]
        if re.search(r'\b(?:rma|return)\b',low) and re.search(r'\b(?:approved|authorized)\b',low):
            kind='rma_approval';rows=[r for r in evidence['rmas'] if r['status'] in approved and r.get('approved_by')]
            supported=bool(rows);refs=[r['rma_id'] for r in rows]
        elif re.search(r'\breplacement\b',low) and re.search(r'\b(?:approved|authorized|sent|dispatched)\b',low):
            kind='replacement';rows=[r for r in evidence['rmas'] if r['movement_type']=='replacement' and r['status'] in approved and r.get('approved_by')]
            supported=bool(rows);refs=[r['rma_id'] for r in rows]
            if re.search(r'\b(?:sent|dispatched)\b',low):
                ships=[s for s in evidence['shipments'] if s['status'] in dispatched and any(r['rma_id']==s['rma_id'] for r in rows)]
                supported=bool(ships);refs=[s['shipment_id'] for s in ships]
        elif re.search(r'\b(?:shipment|shipped|dispatched|delivered)\b',low):
            kind='shipment_status';wanted='delivered' if 'delivered' in low else None
            rows=[s for s in evidence['shipments'] if (s['status']==wanted if wanted else s['status'] in dispatched)]
            supported=bool(rows);refs=[s['shipment_id'] for s in rows]
        elif re.search(r'\breturn label\b',low) and re.search(r'\b(?:created|issued|sent)\b',low):
            kind='return_label';rows=[s for s in evidence['shipments'] if s.get('label_path') and s['status'] not in ('failed','proposed')]
            supported=bool(rows);refs=[s['shipment_id'] for s in rows]
        elif re.search(r'\b(?:refund|credit|warranty|entitlement)\b',low):
            kind='unsupported_entitlement';supported=False
        elif CONSEQUENTIAL_TERMS.search(clause):
            kind='generic_consequential_commitment';supported=False
        if kind:
            checks.append({'claim':clause,'classification':kind,'supported':supported,
                           'evidence_refs':refs})
            if not supported:
                failures.append({'fact':'consequential claim requires a matching authoritative approved/completed record',
                    'conflicting_text':clause,'claim_kind':kind,'evidence_refs':refs})
    return {'passed':not failures,'failures':failures,'checks':checks,
            'authoritative_refs':sorted({r for c in checks for r in c['evidence_refs']})}


def reported_observations(text):
    text=text or ''
    all_lamps=bool(re.search(r'\b(?:all|every)\s+(?:the\s+)?lamps\b.{0,35}\b(?:dark|off|not|remain)',text,re.I))
    if re.search(r'\bnot (?:all|every)\b|\ball lamps\b.{0,15}\bnot (?:dark|off)\b',text,re.I):
        all_lamps=False
    power=bool(re.search(r'\b(?:powers? on|turns on)\b',text,re.I)) and not re.search(r'\b(?:never|not) (?:powers? on|turns on)\b',text,re.I)
    return {'all_lamps_affected':all_lamps,'power_on':power}


def conversation_consistency(text, reported):
    known=reported_observations(reported)
    failures=[]
    if known['all_lamps_affected'] and re.search(r'\b(?:whether|confirm|are)\b.{0,40}\ball\b.{0,35}\b(?:or|one|area)\b',text,re.I):
        failures.append('The customer already reported that all lamps are affected. Ask for the unresolved indicator or display message instead.')
    return failures


def validate(text, *, verified=False, name=None, serial=None, device=None,
             revision_confirmed=False, administrative=False, supplied_name=None, ordinary_public_help=False):
    low = (text or '').lower()
    failures = []
    if re.search(r'\baccessories\s+(?:typically|normally|usually)\s+include\b',low):
        failures.append({'fact':'Generic accessory examples do not establish the contents of this shipment; ask for the item and verify packing/order evidence',
                         'conflicting_text':'unsupported typical packing-list claim'})
    if ordinary_public_help and re.search(r'(?:provide|share|tell|need|confirm|verify|give).{0,65}(?:date of birth|name on the account|verify your account|full name)',low):
        failures.append({'fact':'ordinary applicable manual help does not require private account verification','conflicting_text':'unnecessary personal-information request'})
    checks = [
        (bool(name or supplied_name), r'(?:no name|name (?:was )?not (?:given|provided)|provide (?:the |your )?name)', 'name already received'),
        (verified, r'(?:cannot|can.t|not able to|could not) (?:find|confirm|match|verify) (?:a |the |your )?(?:customer|account|identity)', 'identity is verified'),
        (bool(serial), r'(?:no serial|serial number (?:was )?not (?:provided|given)|provide (?:the |your )?serial|give me the serial)', 'serial already confirmed'),
        (bool(device), r'(?:not able to confirm which device|cannot identify (?:the|your) (?:device|unit)|no .*device.*confirmed|which unit this is about)', 'device already identified'),
        (not verified, r'(?:i have|we have|i.ve|we.ve) (?:now )?(?:verified|confirmed) your (?:identity|account)', 'identity is not verified'),
        (bool(device) and not revision_confirmed, r'(?:confirmed|verified) (?:hardware )?revision|approved procedure for (?:that|your) exact revision', 'revision is not confirmed'),
        (administrative, r'\bstep [1-9]|check (?:the )?(?:fuse|interlock)|disconnect the power', 'administrative request must retain its intent'),
    ]
    for condition, pattern, fact in checks:
        if condition and re.search(pattern, low):
            failures.append({'fact': fact, 'conflicting_text': re.search(pattern, low).group(0)})
    jargon = ('erp account', 'qms retrieval', 'deterministic validation', 'install cohort',
              'no procedure authorized', 'no applicable qms', 'missing identity locator',
              'decided_response', 'we are given', 'conversation history', 'the task is',
              'output only', 'verified status', 'ollama', 'qwen3')
    for term in jargon:
        if term in low:
            failures.append({'fact': 'customer language must explain the next step', 'conflicting_text': term})
    # Qualified-service work cannot be introduced by a model or an edited draft.
    for clause in re.split(r'[.!?;]|\band\s+(?=(?:remove|open|bypass|measure)\b)', text or '', flags=re.I):
        dangerous = re.search(r'\b(?:open|remove)\b.{0,35}\b(?:enclosure|cover|internal panel)\b|\bbypass\b.{0,20}\binterlock\b|\bmeasure\b.{0,25}\b(?:internal voltage|controller voltage)\b',clause,re.I)
        if dangerous and not re.search(r"\b(?:do not|don't|never|must not|avoid)\b",clause[:dangerous.end()],re.I):
            failures.append({'fact':'qualified-service work is not customer guidance','conflicting_text':dangerous.group(0)})
    return {'passed': not failures, 'failures': failures}


def case_facts(store, case_id):
    from .identity import case_revision_authorized
    case = store.case(case_id) or {}
    device = store.device(case.get('serial_number')) if case.get('serial_number') else None
    return dict(verified=bool(case.get('identity_verified')), name=case.get('customer_name') if case.get('identity_verified') else None,
                serial=case.get('serial_number'), device=device,
                revision_confirmed=case_revision_authorized(store, case),
                administrative=case.get('classification') in {'order_status_enquiry', 'general_enquiry'})


_MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december')
_TITLES = r'(?:mr|mrs|ms|miss|mx|dr)\.?'


def _digits(text):
    return re.sub(r'\D', '', str(text or ''))


def _compact(text):
    return re.sub(r'[\s\-_.:/]', '', str(text or '')).upper()


def _date_forms(iso):
    """Common written forms of a stored YYYY-MM-DD date (lower case)."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(iso or ''))
    if not m:
        return []
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if not 1 <= mo <= 12:
        return []
    month, short = _MONTHS[mo - 1], _MONTHS[mo - 1][:3]
    forms = {f'{y}-{mo:02d}-{d:02d}', f'{d} {month} {y}', f'{d}th {month} {y}', f'{month} {d}, {y}', f'{month} {d} {y}',
             f'{d} {short} {y}', f'{short} {d}, {y}', f'{d:02d}/{mo:02d}/{y}', f'{mo:02d}/{d:02d}/{y}', f'{d}/{mo}/{y}', f'{mo}/{d}/{y}',
             f'{d:02d}.{mo:02d}.{y}', f'{d}.{mo}.{y}', f'{d:02d}-{mo:02d}-{y}'}
    if d in (1, 21, 31):forms.add(f'{d}st {month} {y}')
    if d in (2, 22):forms.add(f'{d}nd {month} {y}')
    if d in (3, 23):forms.add(f'{d}rd {month} {y}')
    return sorted(forms)


def private_account_values(store, case_id):
    """Account-record facts the customer has not supplied themselves.

    Returns ``(label, kind, value)``: kind ``name`` | ``date`` | ``phone`` |
    ``serial`` | ``email`` | ``text`` decides how the value is matched."""
    case = store.case(case_id) or {}
    inbound = ' '.join((m['body'] or '') for m in store.messages(case_id) if m['direction'] == 'inbound')
    low, digits, compact = inbound.lower(), _digits(inbound), _compact(inbound)
    accounts = {case.get('account_id')} if case.get('account_id') else set()
    if case.get('serial_number'):
        row = store.device(case['serial_number'])
        if row and row.get('account_id'):
            accounts.add(row['account_id'])
    values = []
    for account in accounts:
        c = store.one('SELECT * FROM customers WHERE account_id=?', (account,)) or {}
        name = str(c.get('customer_name') or '')
        for part in [p for p in re.split(r'\s+', name) if len(p) > 2]:
            if not re.search(r'\b' + re.escape(part.lower()) + r'\b', low):
                values.append(('account holder name', 'name', part))
        if c.get('date_of_birth') and not any(f in low for f in _date_forms(c['date_of_birth'])):
            values.append(('date of birth', 'date', c['date_of_birth']))
        for key in ('phone', 'mobile_phone'):
            if c.get(key) and len(_digits(c[key])) >= 7 and _digits(c[key])[-7:] not in digits:
                values.append(('phone', 'phone', c[key]))
        email = str(c.get('email') or '')
        if email and email.lower() not in low:
            values.append(('email', 'email', email))
        if c.get('street') and str(c['street']).lower() not in low:
            values.append(('address', 'text', c['street']))
        for d in store.devices_for_account(account):
            if _compact(d['serial_number']) not in compact:
                values.append(('account device serial', 'serial', d['serial_number']))
    return values


def _disclosed(kind, value, low, digits, compact):
    if kind == 'name':
        part = re.escape(str(value).lower())
        return bool(re.search(r'\b' + part + r'\b', low))
    if kind == 'date':
        return any(f in low for f in _date_forms(value))
    if kind == 'phone':
        wanted = _digits(value)
        return any(len(run) >= 7 and (run.endswith(wanted[-7:]) or wanted.endswith(run)) for run in digits)
    if kind == 'serial':
        return _compact(value) in compact
    if kind == 'email':
        local = str(value).split('@', 1)[0].lower()
        return str(value).lower() in low or (len(local) >= 4 and re.search(r'(?<![\w.])' + re.escape(local) + r'(?![\w])', low) is not None)
    return str(value).lower() in low


_REVISION = re.compile(r'\b(?:revision|rev\.?)\s+([A-Z])\b', re.I)
_ASSERTION = re.compile(r"\b(?:your|you have|you own|on (?:your|the) account|on record|our records|we have|registered|i can see|we can see|shows? as|listed as)\b", re.I)
_ALTERNATIVES = re.compile(r"\?|\bwhether\b|\beither\b|\bwhich\b|\b(?:revision|rev\.?)\s+[A-Z]\s*(?:,|/|\bor\b|\band\b)\s*(?:(?:revision|rev\.?)\s+)?[A-Z]\b", re.I)


def unverified_disclosure(store, case_id, text):
    """rc.25 (C4): before identity verification a draft may not show account
    facts the customer did not supply: the account holder's first name,
    surname or title + surname anywhere (whole words), the date of birth in
    common written forms, phone digits whatever the separators, other serials,
    the email address or its local part, the street - nor ASSERT the account's
    recorded hardware revision. Asking which revision the unit is, offering
    alternatives, and public manual text that names revisions are allowed."""
    case = store.case(case_id) or {}
    if case.get('identity_verified'):
        return []
    body = (text or '').split('[For representative:')[0]
    low, compact = body.lower(), _compact(body)
    digit_runs = [_digits(m.group(0)) for m in re.finditer(r'\+?\d[\d\s().\-]{5,}\d', body)]
    failures, seen = [], set()
    for label, kind, value in private_account_values(store, case_id):
        if label in seen:
            continue
        if _disclosed(kind, value, low, digit_runs, compact):
            seen.add(label)
            failures.append({'fact': f'{label} from the account record disclosed before identity verification',
                             'conflicting_text': 'private account data', 'claim_kind': 'unverified_disclosure'})
    recorded = str(case.get('revision') or '').upper()
    inbound = ' '.join((m['body'] or '') for m in store.messages(case_id) if m['direction'] == 'inbound')
    if recorded and not re.search(r'\b(?:revision|rev\.?)\s+' + re.escape(recorded) + r'\b', inbound, re.I):
        from .grounding import split_sentences
        # "rev. A" must not be split into two sentences at the abbreviation.
        for sentence in split_sentences(re.sub(r'\brev\.\s*', 'rev ', body, flags=re.I)):
            hits = [m for m in _REVISION.finditer(sentence) if m.group(1).upper() == recorded]
            if hits and _ASSERTION.search(sentence) and not _ALTERNATIVES.search(sentence):
                failures.append({'fact': 'hardware revision from the account record asserted before identity verification',
                                 'conflicting_text': hits[0].group(0), 'claim_kind': 'unverified_disclosure'})
                break
    return failures


def validate_case(store, case_id, text):
    from . import qms, identity, qms_policy
    facts = case_facts(store, case_id)
    result = validate(text, **facts)
    commitments=consequential_claim_guard(store,case_id,text)
    result['failures'].extend(commitments['failures'])
    result['consequential_claim_checks']=commitments['checks']
    result['authoritative_operational_refs']=commitments['authoritative_refs']
    procedures = set(re.findall(r'\bPROC-[A-Z0-9-]+', text or ''))
    case = store.case(case_id) or {}
    if procedures:
        allowed = qms.load_compatibility().get(f"{case.get('model')}|{case.get('revision')}", [])
        chunks = qms.load_chunks()
        for procedure in procedures:
            valid = any(c.procedure_id == procedure and qms_policy.authorize(c,
                operation='customer_answer', recipient='customer', context={
                    'model': case.get('model'), 'revision': case.get('revision'),
                    'revision_confirmed': facts['revision_confirmed'],
                    'compatibility': qms.load_compatibility(),
                    'safety_hold': bool(case.get('safety_hold')) or str(case.get('classification', '')).startswith(('adverse_event', 'device_safety_hazard'))}).allowed for c in chunks)
            if not valid:
                result['failures'].append({'fact': 'procedure must pass current identity, revision, status, audience and mapping authorization',
                                           'conflicting_text': procedure})
    result['passed'] = not result['failures']
    disclosure = qms_policy.disclosure_check(text, [], context={})
    result['failures'].extend(disclosure['failures'])
    result['passed'] = not result['failures']
    result['failures'].extend(unverified_disclosure(store, case_id, text))
    result['passed'] = not result['failures']
    known_tracking={s['tracking_number'] for s in store.query('SELECT tracking_number FROM sim_shipments WHERE case_id=?',(case_id,))}
    if set(re.findall(r'SIMFX\d+',text or ''))-known_tracking:
        result['failures'].append({'fact':'tracking must come from this case recorded shipment','conflicting_text':'unknown simulated tracking'})
        result['passed']=False
    return result


def validate_procedural_edit(store, case_id, message, text):
    """Recheck changed customer device instructions against current authorized sources."""
    import json
    from types import SimpleNamespace
    from . import grounding, identity, qms, qms_policy
    meta=json.loads(message.get('metadata') or '{}')
    if meta.get('kind')=='shipping_update':
        return {'passed':True,'failures':[]}
    # Non-procedural correspondence and identity questions do not need a manual.
    if not grounding._DEVICE_ACTION_RE.search(text) and not re.search(r'\b(?:power lead|lamp indicator|start a treatment|turn on|switch on)\b',text,re.I):
        return {'passed':True,'failures':[]}
    case=store.case(case_id) or {}
    safety=case.get('is_adverse_event') or str(case.get('classification','')).startswith('device_safety_hazard')
    if safety and re.search(r'stop using|unplug',text,re.I) and not re.search(r'\b(?:replace|reseat|open|remove|bypass|measure|switch on|turn on)\b',text,re.I):
        return {'passed':True,'failures':[]}
    ids=set(meta.get('source_chunk_ids') or [])
    if not ids:
        ids={row['chunk_id'] for row in store.query("SELECT chunk_id FROM retrieved_sources WHERE case_id=? AND status='USED'",(case_id,))}
    facts=case_facts(store,case_id)
    device_row=facts.get('device')
    context={'model':case.get('model'),'revision':case.get('revision'),
             'revision_context':identity.revision_context(
                 device_row,
                 case.get('revision_confirmed_by') if facts['revision_confirmed'] else None),
             'compatibility':qms.load_compatibility(),'safety_hold':bool(safety or case.get('safety_hold'))}
    sources=[c for c in qms.load_chunks() if c.chunk_id in ids and qms_policy.authorize(c,context=context).allowed]
    if not sources:
        return {'passed':False,'failures':[{'fact':'no currently authorized source supports edited device instructions'}]}
    row=facts['device'] or {}
    device=SimpleNamespace(model=row.get('model'),revision=row.get('revision'),serial_number=row.get('serial_number'),
        account_id=row.get('account_id') if facts['verified'] else None,customer_name=facts['name'])
    reported='\n'.join(m['body'] for m in store.messages(case_id) if m['direction']=='inbound')
    report=grounding.check_grounding(text,sources,device,sources[0].procedure_id,reported_text=reported)
    return {'passed':report.grounded,'failures':[] if report.grounded else [{'fact':'edited device instructions failed current source grounding','report':report.as_dict()}]}
