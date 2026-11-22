---
document_id: QMS-DOC-0104
procedure_id: PROC-P4800-LAMP-A
title: Lamp Output Low or Unstable — P4800 Rev A
model: P4800
applicable_revisions: [A]
document_version: "1.2"
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
effective_date: 2025-08-21
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the P4800 benchtop phototherapy unit, hardware revision A,
when the unit powers on normally but the customer reports that output appears dim,
flickers, or varies during a session. The unit must be powering on; if there is no
status LED at all, use the no-power procedure for this revision instead.

## Symptom confirmation

Establish whether the status LED is steady amber, blinking amber, or green during
the reported fault. A steady green LED with a customer-perceived dimness is usually
a lamp ageing issue. A blinking amber LED indicates the unit has detected an output
deviation itself and has derated.

Ask the customer roughly how many treatment hours the current lamp has accumulated,
which the unit reports on the service screen.

## Step 1 — Lamp hours and service interval

Revision A units derate output progressively once the lamp passes its rated service
interval. Ask the customer to read the lamp hour counter from the service screen.
If the counter is at or beyond the rated interval, the correct resolution is a lamp
module replacement, not further troubleshooting.

Lamp modules are a customer-replaceable consumable on revision A. Confirm the
customer has a replacement module before advising removal of the existing one.

## Step 2 — Aperture and filter condition

Ask the customer to power the unit off and allow the treatment head to cool for at
least fifteen minutes before inspecting anything. Once cool, ask them to look for
visible dust, film, or residue on the output aperture.

Advise cleaning only with a dry lint-free cloth. Do not advise the use of solvents,
alcohol wipes, or any liquid cleaner on the aperture, as these can damage the
coating and will void the warranty on the head assembly.

## Step 3 — Meter recalibration status

Check the ERP record for the date of last meter recalibration. If the unit is beyond
its recalibration interval, any customer-reported output reading is unreliable and
the unit should be scheduled for recalibration before output is judged faulty.

## Step 4 — Disposition

If lamp hours are within interval, the aperture is clean, and recalibration is
current, but output remains unstable, raise a service request referencing this
procedure ID and the unit serial number.
