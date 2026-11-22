---
document_id: QMS-DOC-0307
document_version: 1.0
approval_status: Approved
effective_date: 2026-10-27
supersedes: null
superseded_by: null
procedure_id: PROC-OPS-RETURN-SHIPPING
title: Fictional Return Methods, Receiving and Exchange Sequencing
model: ALL
applicable_revisions: [A, B, C]
audiences: [customer_service]
document_type: rma_returns_procedure
access_permissions: [agent_internal, representative]
permitted_uses: [internal_workflow, internal_decision, representative_reference]
scope: internal_operational
revision_scope: not_device_specific
owning_function: Operations
required_approvals: [inventory_movement, customer_email, receiving_confirmation]
safety_restrictions: [no_unapproved_execution, do_not_equate_delivery_with_receiving, preserve_audit]
workflow_associations: [movement, shipping, returns, replacement, erp]
workflow_actions: [request_approval, execute_movement, prepare_response, confirm_receiving]
---

## Return method selection
Record the representative's choice and rationale before authorization: carrier pickup with up to three attempts, or customer drop-off using a simulated return label. Both methods require a recorded tracking reference and inventory approval before creation. Refer to the public shipping guide for customer instructions; do not disclose internal workflow text.
## Pickup and drop-off progress
For pickup, record each unsuccessful attempt and any successful pickup, with at most three attempts total. After three failed attempts, request a reviewed alternative arrangement; do not invent another pickup or imply collection occurred. For drop-off, record pending drop-off, carrier acceptance, transit and delivery separately. A label does not establish physical movement.
A named representative may approve a drop-off alternative after three failed pickups, documenting the reason. Reuse the existing return label, tracking and authorized inbound movement; preserve the failed attempts and create a new customer draft. Do not create a second return or reset the historical attempt count.
## Receiving and exchange
A named receiving operator must confirm physical receipt and acceptance of the original device with evidence after recorded inbound delivery. Carrier delivery alone does not satisfy this requirement. Audit every blocked early replacement attempt. Receiving makes a linked replacement eligible for separate named inventory authorization; it never dispatches automatically.
## Shipping communications
After a successful recorded shipping operation, prepare a new customer draft with exact recorded tracking and label reference, selected method and current state. Use the public shipping guide, and do not invent carrier actions. Reuse each milestone draft on retries; a representative must separately approve sending. Failure produces an operational failure, not an invented shipment message.
