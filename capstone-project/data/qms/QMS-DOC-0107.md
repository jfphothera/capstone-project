---
document_id: QMS-DOC-0107
procedure_id: PROC-3SERIES-PWR
title: Unit Does Not Power On — 3-Series
model: 3-Series
applicable_revisions: [A, B, C]
document_version: "1.4"
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
effective_date: 2025-06-02
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the 3-Series cabinet phototherapy unit at all
hardware revisions. Use it when the customer reports no signs of power: no
status indicator, no fan, and no display when the mains switch is set to on.

The 3-Series shares a common supply module across revisions, which is why one
procedure covers A, B and C. If the unit powers on but behaves oddly, this is
the wrong procedure.

## Symptom confirmation

Ask the customer to confirm that the green mains indicator beside the inlet is
dark, that no fan noise is audible within fifteen seconds of switching on, and
that the control panel stays blank. The 3-Series fan is deliberately quiet, so
ask them to listen closely in a quiet room rather than assume.

If the mains indicator is lit but the panel is blank, the supply is working and
the fault is in the control panel, which is not covered here.

## Step 1 — Mains supply and isolator

Ask the customer to check the wall isolator switch, if one is fitted, before
anything else. The 3-Series is commonly installed on a switched spur and the
isolator is often mistaken for a light switch.

Then have them unplug the mains cord at both ends and reseat it firmly, and
confirm the outlet is live with a known-working lamp or small appliance. Do not
ask the customer to test the outlet with any metallic probe or tool.

## Step 2 — Door interlock

The 3-Series cabinet door carries an interlock that holds the unit completely
off when the door is not fully closed, with no indication on the panel. This is
the most common cause of an apparent total power failure on this model.

Ask the customer to open the door fully and close it again until the catch
engages audibly, then retry. Ask them not to tape, wedge or otherwise defeat
the interlock under any circumstances.

## Step 3 — Supply module reset

With the unit unplugged, ask the customer to wait two minutes before
reconnecting. The supply module holds a charge that must drain before it will
restart after an over-current event.

## Step 4 — Disposition

If the unit starts after the isolator check, the door interlock or the reset,
record the resolution and close the interaction. If it remains dark, raise a
service request referencing this procedure ID and the unit serial number. Do
not instruct the customer to open the enclosure.
