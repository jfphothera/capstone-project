---
document_id: QMS-DOC-0303
document_version: 1.0
approval_status: Approved
effective_date: 2026-10-27
supersedes: null
superseded_by: null
procedure_id: PROC-QA-SAFETY
title: Fictional Safety Screening and Simulated QA Notification
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
workflow_associations: [safety, qa]
workflow_actions: [screen_safety, propose_qa_notification, request_human_review]
---

## Screen the actual report
Screen customer statements before identity verification or routine troubleshooting. Distinguish actual injury, possible reaction, device hazard without injury, negated statements and attributed manual quotations. A burning smell is a device hazard, not evidence that a person was burned.
## Notification workflow
When harm, a suspected reaction or meaningful device hazard is present, stop ordinary troubleshooting, preserve the exact customer statement, and raise a simulated QA notification linked to the case. Retain certainty and hazard indicators. Notify a representative for structured determination; do not make a regulatory reportability claim.
## Human determination and retention
A representative selects confirmed adverse event, suspected adverse event, device hazard without confirmed injury, not an adverse event, or insufficient information, with a separate rationale. Preserve the original automated screening and every decision. Lack of confirmed injury does not release a device hazard from review.

