---
document_id: QMS-DOC-0105
procedure_id: PROC-SERIESM-THERM
title: Overheat Shutdown and Thermal Fault — Series-M
model: Series-M
applicable_revisions: [A, B, C]
document_version: "2.1"
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
effective_date: 2025-11-04
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the Series-M mobile phototherapy unit, hardware revisions
A and B, when the unit shuts down mid-session and reports a thermal fault, or when
the customer reports the chassis is unusually hot to the touch.

Revision C units use a different cooling assembly and are out of scope for this
document.

## Safety gate

If the customer reports smoke, a burning smell, visible scorching, melted plastic,
or any injury, stop troubleshooting immediately. Do not work through the steps
below. Escalate the interaction to a human safety reviewer and begin a complaint
record. Thermal events with any of those indicators are reportable and must not be
handled as routine troubleshooting.

## Symptom confirmation

Establish whether the unit shut down on its own or whether the customer switched it
off. A genuine thermal shutdown on revisions A and B leaves a steady red status LED
for sixty seconds after the event, then clears to dark.

Ask the customer for the ambient room temperature and whether the unit was operating
on a hard surface or on carpet, bedding, or upholstery.

## Step 1 — Airflow clearance

Series-M draws cooling air through a slotted intake on the underside of the chassis.
Operating the unit on carpet, bedding, or any soft surface blocks that intake and is
the most common cause of a reported thermal shutdown.

Ask the customer to move the unit onto a hard, flat surface with at least ten
centimetres of clearance on all sides, allow it to cool for thirty minutes, and then
retry a session.

## Step 2 — Intake filter condition

Revisions A and B carry a removable foam intake filter in a clip-out frame on the
underside. Ask the customer to power the unit off, unplug it, and remove the filter
frame.

A filter that is visibly grey, matted, or clogged should be replaced. Replacement
filters are a customer-orderable consumable. Advise the customer not to run the unit
with the filter frame removed, because the frame also retains the intake guard.

## Step 3 — Ambient conditions

Confirm the room temperature is within the rated operating range. Units operated in
conservatories, vehicles, or unventilated rooms in warm weather will shut down
thermally even when the unit itself is working correctly.

## Step 4 — Disposition

If the unit completes a full session after the airflow and filter steps, record the
resolution. If thermal shutdown recurs with clear airflow, a clean filter, and
normal ambient temperature, raise a service request referencing this procedure ID
and the unit serial number.
