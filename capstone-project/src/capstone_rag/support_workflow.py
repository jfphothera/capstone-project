"""Shared QMS-backed investigation tools for chat and representative tickets.

Internal instructions control available actions but are never passed to the
customer-response writer. Fixed safety and approval constraints are independent.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace
import re

from . import activity, generation, guidance, identity, qms, qms_policy, triage, validation


def workflows_for(message):
    t = triage.triage(message)
    workflows = {'intake', 'identity', 'response', 'escalation', 'classification', 'priority'}
    if t.complaint.is_complaint or guidance.specific_symptom(message):
        workflows.update(('complaint', 'classification', 'priority', 'troubleshooting'))
    if triage.requires_immediate_escalation(t):
        workflows.update(('safety', 'qa', 'movement'))
    if re.search(r'replace|replacement|return|rma|missing|spare|ship|broken|damaged|cracked', message, re.I):
        workflows.update(('movement', 'shipping'))
    return workflows


def consult_operational(message, toolbox=None, workflows=None):
    """Semantic section retrieval, bounded by explicit workflow association."""
    requested = set(workflows or workflows_for(message))
    candidates = ([h.chunk for h in toolbox.index.search(message, k=len(toolbox.index.chunks))]
                  if toolbox is not None else qms.load_chunks())
    selected, decisions, seen = [], [], set()
    for chunk in candidates:
        if chunk.scope != 'internal_operational' or not requested.intersection(chunk.workflow_associations):
            continue
        verdict = qms_policy.authorize(chunk, operation='internal_workflow', recipient='internal')
        if chunk.document_id not in seen:
            decisions.append({'document_id': chunk.document_id, 'procedure_id': chunk.procedure_id,
                              'document_type': chunk.document_type, 'authorization': verdict.as_dict(),
                              'workflow_associations': chunk.workflow_associations})
            seen.add(chunk.document_id)
        if verdict.allowed:
            selected.append(chunk)
    allowed = sorted({action for c in selected for action in c.workflow_actions})
    required = {'intake': 'request_information', 'safety': 'propose_qa_notification',
                'movement': 'propose_movement', 'response': 'prepare_response'}
    missing = [action for workflow, action in required.items() if workflow in requested and action not in allowed]
    evidence = {'requested_workflows': sorted(requested), 'documents': decisions,
                'available_actions': allowed, 'missing_required_support': missing,
                'policy': qms_policy.POLICY_ID,
                'retrieval_method': 'semantic local embedding plus workflow association' if toolbox else 'controlled workflow metadata lookup before asynchronous investigation'}
    activity.emit('operational_qms', 'Authorized internal procedures consulted to establish available actions',
                  'awaiting_human_review' if missing else 'completed', evidence)
    return {'chunks': selected, 'evidence': evidence, 'actions': allowed, 'missing': missing}


def customer_manual_search(toolbox, message, model=None, revision=None, revision_confirmed=False,
                           delivered=(), safety_hold=False, device=None,
                           revision_context=None):
    activity.emit('manual_retrieval', 'Applicable customer manual search started', 'running', {'model': model, 'query': message})
    revision_context = revision_context or (
        identity.revision_context(device) if device else {
            'revision': revision, 'status': 'unestablished',
            'usable_for_troubleshooting': False,
            'reason': 'no authoritative device revision context was supplied'})
    context = {'model': model, 'revision': revision,
               'revision_context': revision_context,
               'compatibility': toolbox.compatibility, 'safety_hold': safety_hold}
    hits = toolbox.index.search(message, k=len(toolbox.index.chunks))
    candidates, passing = [], []
    for hit in hits:
        chunk = hit.chunk
        # The public search is purpose-specific. Restricted candidates remain
        # visible as denials, with metadata but no customer response text.
        verdict = qms_policy.authorize(chunk, operation='customer_answer', recipient='customer', context=context)
        candidates.append({'chunk_id': chunk.chunk_id, 'document_id': chunk.document_id,
                           'procedure_id': chunk.procedure_id, 'score': round(hit.score, 4),
                           'section': chunk.section_path, 'document_type': chunk.document_type,
                           'scope': chunk.scope, 'authorization': verdict.as_dict()})
        if verdict.allowed and hit.score >= getattr(toolbox.index,'public_manual_similarity',.12):
            if chunk.scope in ('public_general', 'model_independent'):
                passing.append(hit)
            elif model and chunk.model == model:
                passing.append(hit)
    family = validation.expected_procedure_family(message)
    manuals = [h for h in passing if h.chunk.document_type == 'customer_user_manual'
               and h.chunk.scope in ('public_general', 'model_family')]
    logistics = [h for h in passing if h.chunk.scope == 'model_independent'
                 and family == 'MISSING']
    exact = [h for h in passing if h.chunk.scope == 'exact_model_revision'
             and (not family or family in h.chunk.procedure_id)]
    selected_procedures = []
    if manuals:
        selected_procedures.append(manuals[0].chunk.procedure_id)
    if logistics:
        selected_procedures.append(logistics[0].chunk.procedure_id)
    if exact:
        selected_procedures.append(exact[0].chunk.procedure_id)
        # Procedures declaring the same controlled topic answer the same
        # decision. All authorized ones go forward together so the QMS
        # conflict review (orchestration._qms) decides by metadata; similarity
        # ranking must never silently choose between competing authorities.
        lead_topic = getattr(exact[0].chunk, 'topic', None)
        if lead_topic:
            selected_procedures += [h.chunk.procedure_id for h in exact if getattr(h.chunk, 'topic', None) == lead_topic]
    selected_procedures = list(dict.fromkeys(selected_procedures))
    # ``procedure`` remains the primary public response document for backward
    # compatible grounding.  ``procedures`` and the evidence contract carry
    # the co-authorized exact/logistics tiers alongside it.
    selected = (manuals[0].chunk.procedure_id if manuals else
                logistics[0].chunk.procedure_id if logistics else
                exact[0].chunk.procedure_id if exact else None)
    ordered = [c for c in toolbox.index.chunks if c.procedure_id in selected_procedures
               and qms_policy.authorize(c, operation='customer_answer', recipient='customer', context=context).allowed]
    # Prerequisites and safety are always retained with a task section. A
    # task does not become a license to skip warnings based on similarity.
    prefix = [c for c in ordered if c.section.lower().startswith(('scope', 'safety', 'prerequisite'))]
    topic = ('lamp' if re.search(r'lamp|flicker|dim|light output', message, re.I)
             else 'power' if re.search(r'power|turn on|dark|dead', message, re.I)
             else 'finding' if not model else 'indicators')
    task = [c for c in ordered if topic in c.section.lower() and c.chunk_id not in delivered]
    if family == 'MISSING':
        task = [c for c in ordered if c.scope == 'model_independent'
                and c.chunk_id not in delivered]
    if not task:
        task = [h.chunk for h in passing if h.chunk.procedure_id in selected_procedures
                and h.chunk.chunk_id not in delivered][:1]
    chunks = list(dict((c.chunk_id, c) for c in prefix + task[:2]).values()) if task else []
    contract = {
        'canonical_device_context': {'model': model, 'revision': revision,
                                     'revision_authority': revision_context},
        'authorized_chunk_ids': [c.chunk_id for c in chunks],
        'procedure_ids': selected_procedures,
        'mandatory_context_chunk_ids': [c.chunk_id for c in prefix],
        'safety_hold': bool(safety_hold),
        'policy': qms_policy.POLICY_ID,
    }
    evidence = {'query': message, 'model': model, 'revision': revision,
                'selected_procedure': selected, 'selected_procedures': selected_procedures,
                'accepted_chunk_ids': [c.chunk_id for c in chunks],
                'candidates': candidates, 'context': context,
                'source_scope': 'independently authorized public manual'}
    activity.emit('manual_retrieval', 'Customer manual applicability and disclosure evaluated',
                  'completed' if chunks else 'pending', evidence)
    return {'chunks': chunks, 'procedure': selected, 'procedures': selected_procedures,
            'context': context, 'evidence': evidence, 'contract': contract}


def write_public_reply(agent, question, investigation, customer_supplied_name=None):
    from .react import ReasoningLoop
    manual = investigation.get('manual')
    if not manual or not manual['chunks']:
        return None, None
    row = investigation.get('device') or {}
    device = SimpleNamespace(model=investigation.get('model') or 'general device support',
                             revision=row.get('revision'), serial_number=None,
                             customer_name=customer_supplied_name or 'there',
                             account_id=None)
    if getattr(agent,'experimental_reasoning',None)=='direct_guarded':
        from . import grounding,response_policy
        generation_result=generation.generate_grounded(question,device.customer_name,device,
            manual['chunks'],manual['procedure'],prefer_llm=agent.prefer_llm,public_context=True)
        report=grounding.check_grounding(generation_result.text,manual['chunks'],device,
            manual['procedure'],reported_text=question)
        permission=response_policy.validate(generation_result.text,ordinary_public_help=True)
        context_issues=response_policy.conversation_consistency(generation_result.text,question)
        if not permission['passed'] or context_issues:
            report.grounded=False;report.authorization_issues=permission['failures'];report.context_issues=context_issues
        activity.emit('generation','Direct guarded response generated once','completed' if report.grounded else 'rejected',
            {'reasoning_mode':'direct_guarded','model_response_used':generation_result.mode=='llm',
             'grounded':report.grounded,'source_ids':[c.chunk_id for c in manual['chunks']]})
        if not report.grounded:generation_result=None
    else:
        loop = ReasoningLoop(agent.toolbox, prefer_llm=agent.prefer_llm)
        generation_result, report = loop.write_verified_reply(question, device, manual['chunks'],
                                                              manual['procedure'], public_context=True)
    if generation_result:
        disclosure = qms_policy.disclosure_check(generation_result.text, manual['chunks'], context=manual['context'])
        activity.emit('disclosure', 'Public manual response use and disclosure verified',
                      'completed' if disclosure['passed'] else 'rejected', disclosure)
        if not disclosure['passed']:
            return None, report
    return generation_result, report


def _tool(name, description, parameters=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': parameters or {'type': 'object', 'properties': {}, 'additionalProperties': False}}}


def investigate(message, *, store, agent, claim, verified, device=None, delivered=(),
                interface='chat', authorized_evidence=None):
    from .react import ReasoningLoop
    support = consult_operational(message, agent.toolbox)
    loop = ReasoningLoop(agent.toolbox, prefer_llm=agent.prefer_llm)
    model = (device or {}).get('model')
    private_request = bool(re.search(r'tracking|shipment status|my order|refund|warranty|billing|refill code|unlock code|service history', message, re.I))
    manual_task = guidance.specific_symptom(message) or bool(re.search(r'user manual|user guide|setup|basic operation', message, re.I))
    tentative = guidance.tentative_model(message)
    if not model and tentative:
        model = tentative['model']
    rev = identity.assess_revision(device) if device else None
    facts = {'identity_verified': verified, 'exact_device_identified': bool(device),
             'model': model, 'revision_confirmed': bool(rev and rev.usable_for_troubleshooting),
             'serial_received': bool(claim.serial), 'interface': interface}
    options = {}
    if 'request_information' in support['actions']:
        options['request_information'] = _tool('request_information', 'Ask for an unresolved symptom or equipment identifier. Do not repeat received details.')
    if (model or re.search(r'where.*serial|find.*label', message, re.I)) and not private_request and 'search_customer_manual' in support['actions']:
        options['search_customer_manual'] = _tool('search_customer_manual', 'Retrieve the applicable public user manual and verify customer disclosure.')
    if claim.serial and not device and 'identify_device' in support['actions']:
        options['identify_device'] = _tool('identify_device', 'Resolve the supplied serial against fictional equipment; disclose no account records.')
    if verified and 'lookup_account_records' in support['actions'] and guidance.administrative(message):
        options['lookup_account_records'] = _tool('lookup_account_records', 'Read protected account records only after identity verification.')
    if 'propose_movement' in support['actions'] and verified and re.search(r'return|replace|spare|missing|broken|damaged|cracked', message, re.I):
        options['propose_movement'] = _tool('propose_movement', 'Request a proposed inventory action for named human approval. Does not execute or approve anything.')
    if 'assess_complaint' in support['actions']:
        options['assess_complaint'] = _tool('assess_complaint', 'Apply the controlled complaint/priority procedure to the reported product symptom, then continue investigating.')
    # For an identified model and a concrete ordinary fault, consult its
    # manual before deciding what additional diagnostic information is needed.
    # An account-information question cannot substitute for that investigation.
    if model and manual_task and not private_request and not delivered and 'search_customer_manual' in options:
        options.pop('request_information',None)
    summaries = []
    seen = set()
    for chunk in support['chunks']:
        if chunk.document_id not in seen:
            summaries.append({'document_id': chunk.document_id, 'procedure_id': chunk.procedure_id,
                              'actions': chunk.workflow_actions, 'restrictions': chunk.required_approvals,
                              'excerpt': chunk.text[:1400]})
            seen.add(chunk.document_id)
    if triage.requires_immediate_escalation(triage.triage(message)):
        action = 'request_human_review'
        mode = 'fixed_safety_boundary'
    else:
        suggested = ('request_information' if not manual_task
                     else 'search_customer_manual' if model else 'request_information')
        if 'propose_movement' in options and re.search(r'missing|broken|damaged|exchange|replacement device',message,re.I):
            suggested='propose_movement'
        if re.search(r'where.*serial|find.*label', message, re.I):
            suggested = 'search_customer_manual'
        direct=getattr(agent,'experimental_reasoning',None)=='direct_guarded'
        call = generation.choose_support_tool(message, summaries, facts, list(options.values())) if agent.prefer_llm and options and generation.llm_available() and not direct else None
        action = call['name'] if call else suggested
        mode = call.get('_selection_mode', 'qwen3_tool_selection') if call else 'authorized_document_plan'
        # A tool selection is a proposal; deterministic applicability and
        # protected-record controls remain independently authoritative.
        if action not in options:
            action = 'request_information'
            mode = 'controlled_recovery'
    loop._step('Select an authorized tool from controlled operational procedures', action,
               f'{mode}; account verification={verified}; equipment model={model}',
               detail={'documents': support['evidence'], 'facts': facts, 'selection_mode': mode})
    if action == 'identify_device':
        device = store.device(claim.serial)
        model = (device or {}).get('model')
        rev = identity.assess_revision(device) if device else None
        loop._step('Resolve the supplied identifier using the equipment tool', 'identify_device',
                   'device found' if device else 'unknown serial; no fuzzy correction', detail={
                       'model': model, 'revision': (device or {}).get('revision'), 'source': 'operational SQLite equipment'})
        action = 'search_customer_manual' if model and guidance.specific_symptom(message) else 'request_information'
    if action == 'assess_complaint':
        assessment = triage.triage(message)
        loop._step('Apply the authorized complaint and safety assessment tool', 'assess_complaint',
                   f'{assessment.classification}; priority {assessment.priority}',
                   detail={'assessment': assessment.as_dict(),
                           'supporting_procedure': 'PROC-CS-COMPLAINT'})
        followup_tools = [tool for name,tool in options.items() if name != 'assess_complaint']
        followup = generation.choose_support_tool(message, summaries,
            {**facts, 'classification': assessment.classification, 'priority': assessment.priority}, followup_tools) if agent.prefer_llm and followup_tools and not direct else None
        action = followup['name'] if followup else ('search_customer_manual' if model and manual_task else 'request_information')
        mode = followup.get('_selection_mode','qwen3_tool_selection') if followup else 'authorized_document_plan_after_classification'
        if action == 'identify_device':
            device=store.device(claim.serial)
            model=(device or {}).get('model')
            rev=identity.assess_revision(device) if device else None
            loop._step('Resolve supplied equipment after complaint assessment','identify_device',
                'device found' if device else 'unknown serial; no fuzzy match',detail={'model':model,'source':'operational SQLite equipment'})
            action='search_customer_manual' if model and manual_task else 'request_information'
    # A vague symptom remains a clarification even if the model proposes a
    # search. This avoids conflating missing information and missing coverage.
    if action == 'search_customer_manual' and not manual_task and not re.search(r'where.*serial|find.*label', message, re.I):
        action = 'request_information'
    result = {'action': action, 'selection_mode': mode, 'operational': support,
              'requires_account_verification': private_request,
              'device': device, 'model': model, 'manual': None,
              'question': guidance.SYMPTOM_QUESTION if not guidance.specific_symptom(message)
                          else 'What does the visible indicator or display show after the previous check?' if model and delivered
                          else 'Could you describe the visible display or lamp indicator?' if model
                          else 'Could you share the model or serial number from the device label so I can find the right user manual?'}
    missing_accessory = validation.expected_procedure_family(message) == 'MISSING'
    if action == 'search_customer_manual':
        if authorized_evidence and authorized_evidence.get('blocked'):
            # A blocked QMS step (safety hold, QMS conflict awaiting document
            # control) is final for this draft: never fall back to a fresh search.
            result['manual'] = {'chunks': [], 'procedure': None, 'procedures': [], 'context': {},
                                'evidence': {'blocked': authorized_evidence['blocked'],
                                             'conflict_review': authorized_evidence.get('conflict_review')},
                                'contract': {}}
        elif authorized_evidence and authorized_evidence.get('sources'):
            ids = set(authorized_evidence['sources'])
            result['manual'] = {
                'chunks': [c for c in agent.toolbox.index.chunks if c.chunk_id in ids],
                'procedure': (authorized_evidence.get('evidence') or {}).get('selected_procedure'),
                'procedures': (authorized_evidence.get('evidence') or {}).get('selected_procedures', []),
                'context': (authorized_evidence.get('evidence') or {}).get('context', {}),
                'evidence': authorized_evidence.get('evidence') or {},
                'contract': authorized_evidence.get('evidence_contract') or {},
            }
        else:
            result['manual'] = customer_manual_search(agent.toolbox, message, model,
                (device or {}).get('revision'), bool(rev and rev.usable_for_troubleshooting),
                delivered, device=device,
                revision_context=identity.revision_context(device) if device else None)
        loop._step('Retrieve the public manual and apply disclosure controls', 'search_customer_manual',
                   'authorized sections found' if result['manual']['chunks'] else 'more equipment or symptom information is needed',
                   detail=result['manual']['evidence'])
    elif action == 'lookup_account_records':
        account_id = (device or {}).get('account_id')
        if not account_id:
            verification = identity.verify(store, claim)
            account_id = verification.account_id if verification.verified else None
        result['records'] = store.query('SELECT * FROM orders WHERE account_id=? ORDER BY order_date DESC', (account_id,)) if account_id else []
        loop._step('Consult protected records after verification', 'lookup_account_records',
                   f"{len(result['records'])} order records located", detail={'verified': verified, 'account_id': account_id})
    elif action == 'propose_movement':
        loop._step('Request an approval-gated inventory proposal', 'propose_movement',
                   'Proposal requested; existing CaseFlow and ERP rules determine eligibility. No transaction executed.',
                   detail={'required_approvals': ['inventory_movement'], 'authorized': False})
    # The logistics evidence establishes how to investigate a missing carton
    # item; it never executes or approves the movement.  Preserve it even when
    # the selected next action is the separately gated movement proposal.
    if missing_accessory and model and not result.get('manual'):
        result['manual'] = customer_manual_search(
            agent.toolbox, message, model, (device or {}).get('revision'),
            bool(rev and rev.usable_for_troubleshooting), delivered,
            device=device,
            revision_context=identity.revision_context(device) if device else None)
    activity.emit('workflow_decision', 'QMS-backed investigation selected the next tool', evidence={
        'action': action, 'mode': mode, 'supporting_documents': list(seen),
        'protected_account_access': verified, 'human_approval_required': action == 'propose_movement'})
    # Agentic Testing evidence: who selected the executed tool, whether the
    # selection passed the authorization set, and what executing it produced.
    manual = result.get('manual') or {}
    activity.emit('tool_interaction', f'Tool executed: {action}', 'completed', {
        'phase': 'execution', 'tool': action, 'selection_mode': mode,
        'selected_by': 'model' if mode in ('native_tool_call', 'qwen3_structured_tool_selection', 'delegated_native_tool_call', 'qwen3_tool_selection')
                       or (mode or '').endswith('tool_call') else 'workflow',
        'workflow_reason': None if mode.endswith('tool_call') or 'structured' in mode else mode,
        'permission': {'offered_tools': sorted(options), 'executed_tool_authorized': action in options or action == 'request_human_review',
                       'protected_records_require_verification': True, 'identity_verified': bool(verified)},
        'result': {'manual_chunks': len(manual.get('chunks') or []), 'procedure': manual.get('procedure'),
                   'records': len(result.get('records') or []), 'device_found': bool(result.get('device')),
                   'blocked': (manual.get('evidence') or {}).get('blocked')}})
    return result
