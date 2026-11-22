---
document_id: QMS-DOC-0302
document_version: 1.0
approval_status: Approved
effective_date: 2026-10-27
supersedes: null
superseded_by: null
procedure_id: PROC-CS-COMPLAINT
title: Fictional Complaint Recognition, Classification and Priority
model: ALL
applicable_revisions: [A, B, C]
audiences: [customer_service]
document_type: complaint_qa_procedure
access_permissions: [agent_internal, representative]
permitted_uses: [internal_decision, internal_workflow, representative_reference]
scope: internal_operational
revision_scope: not_device_specific
owning_function: Quality
required_approvals: [inventory_movement, customer_email, human_qa_determination]
safety_restrictions: [stop_on_harm_or_hazard, preserve_audit, no_unapproved_execution]
workflow_associations: [complaint, classification, priority]
workflow_actions: [assess_complaint, screen_safety, request_information, request_human_review]
---

## Complaint recognition
A reported failure preventing intended use, a missing supplied component or a product discrepancy requires a recorded complaint assessment. Buying an accessory or asking about refill administration alone does not establish a malfunction. Ask which observed symptom affects use.
## Priority and decision evidence
Evaluate safety first. Harm or a meaningful device hazard takes precedence. Otherwise assess inability to use required equipment, repeated failure and unresolved contact. Frustration alone is not authority to raise priority. Retain the evidence and explain disagreements for representative review.
## Boundaries
Use assess_complaint and screen_safety before selecting ordinary troubleshooting. A document concern is a controlled-document discrepancy, not automatic proof of product injury. Where the configured safety/approval rule and this procedure disagree, stop the affected action and request review.

