# Outstanding limitations (2.0.0)

1. **No physical head has been verified.** All evidence comes from the
   coordinator workstation `DEVPC-REDACTED`. Head 3 acceptance is pending.
2. **No Ticketing work reaches heads today.** Capstone's production database
   has no enrolled heads and the active AI profile is `baseline-local`.
   Head views will correctly be empty until an operator enrolls heads in
   Capstone and activates a cluster profile.
3. **Capstone event-quality gaps (C1–C5)** are worked around by the Ticketing
   adapter, not fixed:
   - the task instance is carried in `workflow_id`;
   - `task.started` names a head as its execution node;
   - inference is attributed to the route role;
   - there are no heartbeats.

   The fixes belong to the Codex workstream (`CODEX-INTEGRATION-REPORT.md`).
4. **The synthetic Coder producer is test data.** No real Coder producer
   exists yet.
5. **No encryption on the wire.** Requests and responses are HMAC-signed for
   integrity and authenticity on the private LAN, but not encrypted. Payloads
   are restricted to identifiers and short labels.
6. **The executable is unsigned.** SmartScreen may show "Windows protected
   your PC" once per extracted package. Installed copies are unblocked.
7. **WebView2 Runtime is required.** It is in-box on Windows 11. On Windows 10
   heads it may be missing. The installer reports this and stops; it does not
   bundle the runtime.
8. **Event service availability.** It runs on the coordinator hardware, so a
   coordinator power-off means heads show Disconnected. Keeping service
   through that outage needs another host (an operator decision).
9. **Animation approval is pending.** The articulated activity animations
   replace the rejected torso bob and still need the operator's visual
   approval.
10. **Startup on a different Windows user.** Startup is per Windows user. Each
    display user installs once.
11. **The old coordinator-hosted Arena (`/agent-arena`, schema 1.0)** still
    exists in Capstone. It is unchanged, but it is not the deployed viewer.
