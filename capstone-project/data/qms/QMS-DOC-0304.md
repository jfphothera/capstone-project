---
document_id: QMS-DOC-0304
document_version: 2.0
approval_status: Approved
effective_date: 2026-10-27
supersedes: null
superseded_by: null
procedure_id: PROC-OPS-MOVEMENT
title: Fictional RMA, Spare Parts, ERP and Fulfillment Control
model: ALL
applicable_revisions: [A, B, C]
audiences: [customer_service]
document_type: rma_returns_procedure
access_permissions: [agent_internal, representative]
permitted_uses: [internal_decision, internal_workflow, representative_reference]
scope: internal_operational
revision_scope: not_device_specific
owning_function: Operations
required_approvals: [inventory_movement, customer_email, human_qa_determination]
safety_restrictions: [stop_on_harm_or_hazard, preserve_audit, no_unapproved_execution]
workflow_associations: [movement, replacement, returns, shipping, erp]
workflow_actions: [identify_device, lookup_account_records, propose_movement, request_approval, execute_movement]
---

## Select the inventory purpose
Determine whether the request concerns an inbound device return, an outbound spare part, or an outbound replacement. An RMA is an inventory movement record, not a synonym for returning every complete device. Verify the account before proposing account-specific execution.
Create or update the ERP service call as the continuing record of the customer, device, reported problem, investigation, assessment and proposed resolution. Multiple movements may share that call. A generic failure requires authorized troubleshooting before product movement. Explicit damage to a named customer-replaceable component can support an outbound part proposal; unidentified damage requires clarification or technical review, not an automatic complete-device return.
## Proposal and authorization
A safety quarantine, exhausted authorized troubleshooting or a missing supplied component may support a proposed movement under the configured business rules. Record its purpose, item, direction, supporting procedure and customer impact. A proposal authorizes no shipment, label, refund, replacement or customer promise.
## ERP and shipping execution
Request a named representative's inventory_movement approval before execute_movement. A rejection blocks execution and remains in audit. After approval, use the existing idempotent simulated ERP transaction to link the RMA, shipment, label and tracking. Report only recorded shipment states; no real carrier is contacted.
An exchange consists of an inbound return and a separately authorized outbound replacement under the same service call. Do not execute the replacement until named receiving records the original device as physically received and accepted. Label creation, pickup, transit and carrier delivery are insufficient. Consult PROC-OPS-RETURN-SHIPPING for method selection, pickup limits, receiving and correspondence.

