---
document_id: QMS-DOC-0102
procedure_id: PROC-P4800-PWR-C
title: Unit Does Not Power On — P4800 Rev C
model: P4800
applicable_revisions: [C]
document_version: "2.0"
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
effective_date: 2026-01-09
supersedes: null
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the P4800 benchtop phototherapy unit, hardware revision C
only. Revision C units are identified by an ERP install date on or after 1 January
2024 and carry a black rear chassis plate with a sealed mains inlet module. Do not
apply this procedure to revision A or revision B units. Revision C replaced the
user-accessible fuse carrier with a self-resetting electronic supply protector, so
the fuse checks used on earlier revisions do not exist on this hardware and must not
be attempted.

Use this procedure when the customer reports that the unit shows no signs of power:
no status LED, no fan noise, and no display backlight when the mains switch is set
to the on position.

## Symptom confirmation

Confirm with the customer that the front status LED is completely dark, that no fan
or cooling noise is audible within ten seconds of switching on, and that the display
backlight does not illuminate even briefly.

Revision C units perform a brief self-test on power-up during which the status LED
flashes white once. If the customer reports seeing any flash at all, the supply is
working and this is not a total power failure.

## Step 1 — Mains supply and cord check

Ask the customer to unplug the mains cord from both the wall outlet and the rear
inlet, then reseat both ends firmly. Have the customer confirm the wall outlet is
live using a known-working lamp or small appliance. Do not ask the customer to test
the outlet with any metallic probe or tool.

If the outlet is dead, the issue is site electrical supply and no further device
troubleshooting is required.

## Step 2 — Supply protector reset cycle

Revision C has no user-replaceable fuse. The sealed inlet module contains a
self-resetting supply protector that latches off after an over-current event and
clears only after a full discharge interval.

Ask the customer to disconnect the mains cord completely and leave the unit
unplugged for a minimum of five minutes. The protector will not clear if the cord is
merely switched off at the wall, because the module remains energised through the
standby rail.

After five minutes, reconnect and retry power-on. If the unit now starts, log the
event as a protector trip and advise the customer to report any repeat occurrence.

## Step 3 — Dual interlock position

Revision C carries two interlock switches: the treatment head docking collar and an
additional filter-tray interlock on the left side of the chassis, which revision A
units do not have. Both must be closed before the supply will enable.

Ask the customer to reseat the treatment head until the collar clicks, and
separately to push the filter tray fully home until it latches. A partially inserted
filter tray is the single most common cause of an apparent revision C no-power
report and leaves no indication on the front panel.

## Step 4 — Disposition

If the unit powers on after the protector reset or after closing both interlocks,
record the resolution and close the interaction. If the unit remains dark after all
steps above, raise a return authorisation referencing this procedure ID and the unit
serial number. Do not instruct the customer to open the enclosure.
