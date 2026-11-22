"""Export current controlled-document axes and their workflow consumers."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from capstone_rag.webapp.routers.trace import qms_inventory

inventory = qms_inventory()
destination = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT / 'docs/evidence/qms_agentic'
destination.mkdir(parents=True, exist_ok=True)
(destination / 'document-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n', encoding='utf-8')
lines = ['# Fictional QMS inventory and authorization matrix', '',
         f"{inventory['document_count']} documents, {inventory['chunk_count']} sections. Policy `{inventory['policy']}`.", '',
         'Authorization columns are examples for the context recorded in the JSON export. Every actual use is checked again.', '',
         '| Document / procedure | Type / status | Access | Permitted uses | Model / revision scope | Workflows / actions | Human approvals |',
         '|---|---|---|---|---|---|---|']
for d in inventory['documents']:
    def joined(key):
        value = d.get(key, [])
        return ', '.join(value) if isinstance(value, list) else str(value)
    lines.append(f"| {d['document_id']} / {d['procedure_id']} | {d['document_type']} / {d['approval_status']} | {joined('access_permissions')} | {joined('permitted_uses')} | {d['model']} / {d['scope']} / {d['revision_scope']} / {joined('applicable_revisions')} | {joined('workflow_associations')} / {joined('workflow_actions')} | {joined('required_approvals')} |")
lines += ['', '## Runtime consumers', '',
          '- Intake, identity, complaint classification, priority: shared `support_workflow.consult_operational`, consumed by chat investigation and CaseFlow intake/pre-investigation.',
          '- Safety/QA: controlled QA procedure evidence is attached to the actual notification; fixed safety boundaries remain independent.',
          '- RMA/spares/shipping: missing approved movement support blocks proposals; available support is attached to proposed ERP actions. Existing representative approval executes them.',
          '- Response/follow-up/escalation: controlled actions constrain the available agent tools; customer writing receives only authorized public sources.',
          '- Public manuals: serial resolves equipment without disclosing owner records; model/revision applicability and disclosure are checked for every retrieved section.',
          '- Qualified-service and historical documents remain unavailable to ordinary customer guidance.', '']
(destination / 'document-inventory.md').write_text('\n'.join(lines), encoding='utf-8')
print(f"Exported {inventory['document_count']} documents / {inventory['chunk_count']} sections to {destination}")
