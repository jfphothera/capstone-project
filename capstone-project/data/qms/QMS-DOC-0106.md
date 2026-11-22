---
document_id: QMS-DOC-0106
procedure_id: PROC-P4800-TIMER-C
title: Treatment Timer Resets Mid-Session — P4800 Rev C
model: P4800
applicable_revisions: [C]
document_version: "1.1"
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
effective_date: 2026-02-18
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the P4800 benchtop phototherapy unit, hardware revision C,
when the unit remains powered but the session timer returns to zero before the
programmed treatment time has elapsed. The unit must be powering on and completing
its self-test; if the unit is dark, use the no-power procedure for this revision.

## Symptom confirmation

Establish whether the display stays lit through the reset. If the display blanks and
re-illuminates, the unit is restarting rather than resetting the timer, which is a
supply fault and not covered here.

Ask the customer how far into the session the reset occurs and whether it happens at
a consistent elapsed time.

## Step 1 — Filter tray latch

Revision C monitors the filter-tray interlock continuously during a session, not
only at start-up. A tray that is latched but slightly proud will intermittently open
the interlock as the chassis warms and expands, which aborts the session and returns
the timer to zero without any error message.

Ask the customer to push the filter tray fully home until it latches audibly, then
retry. A reset that consistently occurs at roughly the same elapsed time is strongly
indicative of this cause.

## Step 2 — Programmed schedule check

Confirm the customer is reading the session timer and not the schedule countdown.
The revision C display shows both, and the schedule countdown resets at the end of
each programmed block by design. This is normal behaviour and not a fault.

## Step 3 — Firmware level

Check the firmware version on the service screen. Units below the current released
level had a timer handling defect corrected in a later release. If the unit is
below that level, arrange a firmware update through service rather than continuing
hardware troubleshooting.

## Step 4 — Disposition

If the session completes after the tray is latched or the firmware is updated,
record the resolution. If the timer continues to reset, raise a service request
referencing this procedure ID and the unit serial number.
