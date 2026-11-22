# Sample reports

Real output from `New-CapstoneStatusReport`, the same function the engine
uses. Each file is what `C:\ProgramData\CapstoneProvisioner\reports\LATEST-STATUS.txt`
would contain in that situation.

| File | Scenario | Result |
|---|---|---|
| `LATEST-STATUS.full-success.txt` | Full success | `FULL_SUCCESS` |
| `LATEST-STATUS.partial-pending-coordinator.txt` | Partial: everything on the head worked, coordinator check not yet run | `PARTIAL_SUCCESS` |
| `LATEST-STATUS.partial-failed-tag.txt` | Partial: one model tag failed | `PARTIAL_SUCCESS` |
| `LATEST-STATUS.resumable-second-run.txt` | Resumed run: seven models were already present, eleven still needed | `FULL_SUCCESS` |
| `LATEST-STATUS.failed-no-models.txt` | Failed: Ollama healthy but no model could be downloaded | `FAILED` |
| `LATEST-STATUS.failed-ambiguous-adapter.txt` | Stopped safely: two wired ports, correct one could not be established | `FAILED` |

A `.json` file of the same name accompanies each one: that is
`LATEST-STATUS.json`, the machine-readable form.
