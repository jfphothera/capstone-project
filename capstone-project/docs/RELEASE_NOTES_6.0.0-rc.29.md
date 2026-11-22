# Capstone 6.0.0-rc.29 (2026-11-21): live investigation steps fixed

Field report on rc.28: the Details pane stayed on "Queued" and no step ever updated.

- **The progress request crashed once a step had two events.** Activity times are ISO text; rc.27/28 subtracted them as
  numbers, so /api/cases/{id}/progress failed and the pane stayed frozen on its first answer. Times are now parsed.
  Specialist steps are also recognised from the event text when the event detail has no role.
- **The case said "queued" for the whole run.** The case worker never marked a ticket as being investigated. It now sets
  processing when it starts a ticket and tells the browser, so the label and the steps go live immediately.

Unchanged otherwise (profile, models, Details layout, QA handoff).
