---
document_id: QMS-DOC-0301
document_version: 1.0
approval_status: Approved
effective_date: 2026-10-27
supersedes: null
superseded_by: null
procedure_id: PROC-CS-INTAKE
title: Fictional Customer Inquiry Intake and Verification
model: ALL
applicable_revisions: [A, B, C]
audiences: [customer_service]
document_type: internal_customer_service_procedure
access_permissions: [agent_internal, representative]
permitted_uses: [internal_decision, internal_workflow, representative_reference]
scope: internal_operational
revision_scope: not_device_specific
owning_function: Customer Service
required_approvals: [inventory_movement, customer_email, human_qa_determination]
safety_restrictions: [stop_on_harm_or_hazard, preserve_audit, no_unapproved_execution]
workflow_associations: [intake, identity, general_support]
workflow_actions: [request_information, identify_device, search_customer_manual, lookup_account_records]
---

## Intake and task selection
Preserve the customer's request and distinguish an operating question from an account transaction. Record received, parsed, corrected, matched and verified identity fields separately. A received unmatched name must not be treated as an absent name.
## Verification and useful assistance
For public manual assistance, establish equipment applicability without requiring private account verification. A serial may identify equipment without authorizing account disclosure. Before exposing orders, shipments, service history or making account changes, verify any two independent authorized identity points against the fictional ERP. Ambiguous dates require clarification.
## Authorized next actions
Use identify_device for a supplied serial, request_information for an unresolved symptom, and search_customer_manual for public instructions. Use lookup_account_records only after successful identity verification. A model alias never confirms a serial or hardware revision.

