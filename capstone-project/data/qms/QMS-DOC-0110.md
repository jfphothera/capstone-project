---
document_id: QMS-DOC-0110
procedure_id: PROC-P3410-PWR
title: Unit Does Not Power On — P3410
model: P3410
applicable_revisions: [A, B, C]
document_version: "1.0"
approval_status: Approved
audiences: [customer_service]
document_type: customer_troubleshooting_instruction
access_permissions: [customer_disclosable, agent_internal, representative]
permitted_uses: [customer_answer, revision_troubleshooting]
scope: exact_model_revision
revision_scope: listed_revisions
owning_function: Customer Service
required_approvals: [inventory_movement, customer_email]
safety_restrictions: [stop_on_harm_or_hazard, never_open_enclosure]
workflow_associations: [device_troubleshooting]
workflow_actions: [search_revision_procedure]
effective_date: 2025-11-28
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the P3410 bench unit at all hardware revisions when
it shows no signs of power. The P3410 uses an external power supply brick, so
most reports on this model are the supply rather than the unit.

## Symptom confirmation

Ask the customer whether the small indicator on the power supply brick is lit.
That one question separates a supply fault from a unit fault and should always
be asked first on this model.

Then confirm the unit's own front indicator is dark and no fan is audible.

## Step 1 — Power supply brick

If the brick indicator is dark, the fault is in the supply or the outlet. Ask
the customer to confirm the outlet is live using a known-working lamp or small
appliance, and to reseat the mains lead at the brick, which is a separable
connector and works loose easily.

The P3410 power supply is a customer-replaceable accessory and can be shipped
out without returning the unit.

## Step 2 — Output lead at the unit

If the brick indicator is lit but the unit is dark, ask the customer to unplug
and firmly reseat the output lead at the rear of the unit until the collar
clicks. A partially seated output connector powers nothing and gives no
indication.

## Step 3 — Rear rocker switch

Ask the customer to check the rear rocker switch next to the output socket.
It is separate from the front power button and is sometimes knocked during
cleaning.

## Step 4 — Disposition

If the unit starts after reseating the lead or switching the rear rocker on,
record the resolution. If the brick indicator is dark on a confirmed live
outlet, arrange a replacement power supply. If the brick is lit and the unit
is still dark, raise a service request referencing this procedure ID and the
unit serial number.
