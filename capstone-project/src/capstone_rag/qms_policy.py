"""Central access, intended-use, applicability and disclosure authorization.

Procedural knowledge informs the plan. This policy independently limits access,
disclosure and execution. No document or generated plan can grant human approval.
"""
from dataclasses import dataclass, field
from datetime import date
import re

POLICY_ID = 'CAPSTONE-QMS-USE-2'
APPROVAL_OPERATIONS = {'execute_movement': 'inventory_movement', 'send_email': 'customer_email'}


@dataclass
class Authorization:
    allowed: bool
    disclosure_allowed: bool
    permitted_uses: list
    restrictions: list
    denial_reasons: list
    denial_codes: list
    references: list

    def as_dict(self):
        return dict(self.__dict__)


def authorize(document, *, actor='customer_service_agent', operation='customer_answer',
              recipient='customer', context=None):
    context = context or {}
    reasons = []
    codes = []
    def deny(code, message):
        if code not in codes:
            codes.append(code)
            reasons.append(message)
    accesses = document.access_permissions
    disclosure = ('customer_disclosable' in accesses and
                  operation in ('customer_answer', 'revision_troubleshooting'))
    if operation not in document.permitted_uses and operation not in APPROVAL_OPERATIONS:
        deny('USE_NOT_PERMITTED', f'document does not permit requested use {operation}')
    if operation != 'historical_review':
        if document.approval_status != 'Approved':
            deny('DOCUMENT_NOT_CURRENT', f'document status {document.approval_status} is not current Approved')
        if document.superseded_by:
            deny('DOCUMENT_SUPERSEDED', 'document has been superseded')
        if document.effective_date:
            try:
                if date.fromisoformat(document.effective_date) > date.today():
                    deny('DOCUMENT_NOT_EFFECTIVE', 'effective date is in the future')
            except ValueError:
                deny('DOCUMENT_DATE_INVALID', 'invalid controlled effective date')
    if 'audit_only' in accesses and operation != 'historical_review':
        deny('AUDIT_ONLY', 'audit-only material cannot authorize current work')
    if 'qualified_service_only' in accesses and actor != 'qualified_service':
        deny('QUALIFIED_SERVICE_ONLY', 'qualified-service-only access denied for this actor')
    if actor == 'customer_service_agent' and not any(a in accesses for a in ('agent_internal', 'customer_disclosable')):
        deny('ACTOR_ACCESS_DENIED', 'document is not readable by the customer-service agent')
    if actor == 'representative' and 'representative' not in accesses:
        deny('ACTOR_ACCESS_DENIED', 'representative access is not authorized')
    if recipient == 'customer' and not disclosure:
        deny('DISCLOSURE_DENIED', 'customer disclosure is not permitted')
    if operation == 'revision_troubleshooting' and document.scope != 'exact_model_revision':
        deny('WRONG_APPLICABILITY_TIER', 'a general manual does not authorize revision-specific procedure execution')
    if operation in ('customer_answer', 'revision_troubleshooting'):
        if context.get('safety_hold'):
            deny('SAFETY_HOLD', 'safety hold blocks ordinary operating instructions')
        if document.scope not in ('public_general', 'model_independent'):
            model = context.get('model')
            revision = context.get('revision')
            if not model or document.model not in (model, 'ALL'):
                deny('MODEL_NOT_APPLICABLE', 'model applicability is not established')
            if document.revision_scope == 'all_supported_revisions':
                if revision and revision not in document.applicable_revisions:
                    deny('REVISION_NOT_APPLICABLE', 'hardware revision is outside this manual scope')
            if document.scope == 'exact_model_revision':
                revision_authority = context.get('revision_context') or {}
                if not revision_authority.get('usable_for_troubleshooting'):
                    deny('REVISION_PROVENANCE_UNTRUSTED',
                         revision_authority.get('reason') or
                         'hardware revision provenance is not authoritatively usable')
                if revision not in document.applicable_revisions:
                    deny('REVISION_NOT_APPLICABLE', 'hardware revision is outside this procedure scope')
                mapping = context.get('compatibility', {})
                if document.procedure_id not in mapping.get(f'{model}|{revision}', []):
                    deny('COMPATIBILITY_NOT_AUTHORIZED', 'procedure is not authorized by model/revision mapping')
    restrictions = list(document.safety_restrictions) + [f'human approval: {a}' for a in document.required_approvals]
    if operation in APPROVAL_OPERATIONS:
        if operation not in document.workflow_actions:
            deny('WORKFLOW_ACTION_NOT_PERMITTED', 'requested operation is outside this document workflow actions')
        approval = APPROVAL_OPERATIONS[operation]
        if not context.get('identity_verified'):
            deny('IDENTITY_NOT_VERIFIED', 'protected operation requires verified customer authorization')
        if approval not in context.get('human_approvals', []):
            deny('HUMAN_APPROVAL_REQUIRED', f'named human approval required: {approval}')
    if ('execute_movement' in document.workflow_actions and
            'inventory_movement' not in document.required_approvals):
        deny('APPROVAL_BOUNDARY_CONFLICT', 'procedure conflicts with fixed inventory approval boundary')
    if ('send_email' in document.workflow_actions and
            'customer_email' not in document.required_approvals):
        deny('APPROVAL_BOUNDARY_CONFLICT', 'procedure conflicts with fixed outgoing-email approval boundary')
    return Authorization(not reasons, disclosure and not reasons, list(document.permitted_uses),
                         restrictions, reasons, codes,
                         [POLICY_ID, f'{document.document_id} v{document.document_version}', document.procedure_id])


def disclosure_check(text, sources, *, context=None):
    """Check every disclosed source and reject restricted citations/content."""
    from . import qms
    failures = []
    allowed_ids = set()
    for source in sources:
        decision = authorize(source, operation='customer_answer', recipient='customer', context=context)
        if not decision.allowed:
            failures.append({'document_id': source.document_id, 'reasons': decision.denial_reasons})
        else:
            allowed_ids.update((source.document_id, source.procedure_id))
    for source in qms.load_chunks():
        restricted = 'customer_disclosable' not in source.access_permissions or source.approval_status != 'Approved'
        if not restricted:
            continue
        if source.document_id in text or source.procedure_id in text:
            failures.append({'document_id': source.document_id, 'reasons': ['restricted or historical citation in customer reply']})
        # Restricted procedural wording cannot be copied into customer prose.
        words = re.findall(r'[a-z0-9]+', source.text.lower())
        output = ' '.join(re.findall(r'[a-z0-9]+', text.lower()))
        if len(words) >= 12 and any(' '.join(words[i:i+12]) in output for i in range(len(words)-11)):
            failures.append({'document_id': source.document_id, 'reasons': ['restricted procedural text in customer reply']})
    return {'passed': not failures, 'failures': failures, 'policy': POLICY_ID,
            'customer_grounding_sources': sorted(allowed_ids)}


def review_conflicts(documents, *, model=None, revision=None):
    """Controlled-document conflict review over retrieved QMS evidence.

    Uses explicit document metadata only: approval status, supersedes /
    superseded_by, model and applicable revisions, ``topic`` (the decision a
    procedure answers) and ``precedence_over``. Similarity scores, model
    confidence and recalled experience never choose the authority, and wording
    differences are not treated as conflicts. Returns:

      {'superseded': [...], 'inapplicable': [...], 'resolved': [...],
       'conflicts': [{'topic', 'documents', 'reason'}], 'requires_document_control': bool}
    """
    seen = {}
    for item in documents:
        seen.setdefault(getattr(item, 'document_id', None), item)
    current, superseded, inapplicable = [], [], []
    replaced = {getattr(d, 'supersedes', None) for d in seen.values() if getattr(d, 'supersedes', None)}
    for doc_id, doc in seen.items():
        if doc.approval_status != 'Approved' or getattr(doc, 'superseded_by', None) or doc_id in replaced:
            superseded.append(doc_id); continue
        if model and doc.model not in (model, '*', 'ALL'):
            inapplicable.append(doc_id); continue
        if revision and doc.applicable_revisions and revision not in doc.applicable_revisions and 'ALL' not in doc.applicable_revisions:
            inapplicable.append(doc_id); continue
        current.append(doc)
    by_topic = {}
    for doc in current:
        if getattr(doc, 'topic', None):
            by_topic.setdefault(doc.topic, []).append(doc)
    conflicts, resolved = [], []
    for topic, docs in by_topic.items():
        procedures = {d.procedure_id for d in docs}
        if len(procedures) < 2:
            continue
        ids = {d.document_id for d in docs}
        # Controlled precedence resolves the topic only if one document is
        # declared to take precedence over every other current one.
        winners = [d for d in docs if ids - {d.document_id} <= set(getattr(d, 'precedence_over', []) or [])]
        if len(winners) == 1:
            resolved.append({'topic': topic, 'authority': winners[0].document_id,
                             'over': sorted(ids - {winners[0].document_id}), 'basis': 'declared precedence_over'})
        else:
            conflicts.append({'topic': topic, 'documents': sorted(ids),
                              'procedures': sorted(procedures),
                              'reason': 'Two or more current approved procedures answer the same topic for this model/revision '
                                        'and no controlled precedence resolves them; document control must decide.'})
    return {'superseded': sorted(superseded), 'inapplicable': sorted(inapplicable), 'resolved': resolved,
            'conflicts': conflicts, 'requires_document_control': bool(conflicts)}
