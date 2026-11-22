---
document_id: QMS-DOC-0101
procedure_id: PROC-P4800-PWR-A
title: Unit Does Not Power On — P4800 Rev A
model: P4800
applicable_revisions: [A]
document_version: "3.0"
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
effective_date: 2025-03-14
supersedes: QMS-DOC-0103
owner: Technical Support Engineering
---

## Scope and applicability

This procedure applies to the P4800 benchtop phototherapy unit, hardware revision A
only. Revision A units are identified by an ERP install date earlier than 1 January
2022 and carry a grey rear chassis plate with a single captive mains inlet. Do not
apply this procedure to revision B or revision C units; the rear panel interlock
wiring differs and the checks below will give misleading results.

Use this procedure when the customer reports that the unit shows no signs of power:
no status LED, no fan noise, and no display backlight when the mains switch is set
to the on position.

## Symptom confirmation

Before any hardware check, confirm with the customer that all three of the following
are true. If any one of them is false, this is not a total power failure and a
different procedure applies.

The front status LED is completely dark, not amber and not blinking. No fan or
cooling noise is audible within ten seconds of switching on. The display backlight
does not illuminate even briefly.

If the status LED is amber or blinking, the unit has power and has entered a fault
state; refer instead to the lamp output procedure for this revision.

## Step 1 — Mains supply and cord check

Ask the customer to unplug the mains cord from both the wall outlet and the rear
inlet of the unit, then reseat both ends firmly. On revision A the rear inlet is a
captive connector and can work loose during transport.

Have the customer confirm the wall outlet is live by plugging in a known-working
lamp or small appliance. Do not ask the customer to test the outlet with any
metallic probe or tool.

If the outlet is dead, the issue is site electrical supply and no further device
troubleshooting is required.

## Step 2 — Rear panel fuse carrier

Revision A units carry two user-accessible fuses in a slide-out carrier located
directly beneath the mains inlet. With the mains cord fully disconnected, ask the
customer to slide the carrier out and visually inspect both fuse elements for a
broken filament or darkened glass.

A single blown fuse is a replaceable consumable and a replacement carrier may be
shipped to the customer. If both fuses are blown, the unit must be returned for
service; repeated fuse failure indicates an internal fault that is not field
serviceable.

Do not advise the customer to bypass, bridge, or substitute a fuse of a different
rating under any circumstances.

## Step 3 — Interlock position

The revision A rear panel carries a single mechanical interlock switch behind the
treatment head docking collar. If the head is not fully seated the interlock holds
the unit in a hard off state with no indication at all, which closely resembles a
total power failure.

Ask the customer to remove and reseat the treatment head until the collar clicks.
Then retry power-on. This step resolves a substantial share of reported revision A
no-power cases and should always be performed before authorising a return.

## Step 4 — Disposition

If the unit powers on after reseating the head or replacing a single fuse, record
the resolution and close the interaction. If the unit remains dark after all steps
above, raise a return authorisation referencing this procedure ID and the unit
serial number. Do not instruct the customer to open the enclosure.
