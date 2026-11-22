# Troubleshooting and advanced configuration

## What diagnostics results mean

| Diagnostics shows | Meaning | Action |
|---|---|---|
| Installation FAIL "not installed" | Nothing is installed for this Windows user | Run INSTALL |
| Installation WARN "partial" / "damaged" / "misconfigured" | Interrupted install, modified files or a bad `config.json` | Run INSTALL again; it repairs |
| Detected identity FAIL | No local address in `192.0.2.0/24` | Wrong machine or cable. Nothing is guessed. |
| Service reachability FAIL | Coordinator or event service is down, or the network path is blocked | Coordinator problem, not a viewer problem |
| Arena event API FAIL with the port open | Something other than the Arena Event Service answered | Check the coordinator |
| Authentication FAIL "login required" / "not_authorized" | No credential, or it was revoked | Get a new code and run INSTALL |
| Authentication FAIL "service_identity" | The answering service could not prove it is the enrolled service | Do not trust it; check the coordinator |
| Display engine FAIL | WebView2 Runtime missing (possible on Windows 10) | IT installs the WebView2 Evergreen Runtime |
| Local Ollama WARN | Ollama is not answering on this head | Unrelated to Arena; Arena never changes Ollama |

The in-window banner distinguishes:

- **Event service unreachable** (coordinator side);
- **Local Arena runtime is not responding** (this head);
- **not enrolled**;
- **credential revoked**;
- **authenticity check failed**.

## Coordinator: event service

- Configuration: `%LOCALAPPDATA%\AgentArenaService\aes.config.json`. The
  defaults are host `192.0.2.1` and port `8790`.
- Database and logs are in the same folder.
- `python -m arena_service.admin --config <cfg> list` shows the registry and
  credential IDs. It never shows secrets.
- **Revoke a head:** `… admin --config <cfg> revoke <credential_id>`
- **Rotate a head:** `… rotate <credential_id>`. The head picks up the new
  secret automatically, sealed under the old one.
- **Move the service:** start it on the new host, then
  `… endpoints-set http://NEW:8790`. Each head validates the new endpoint's
  signed response before switching. No reinstall is needed.
- **Firewall.** The workstation already has an inbound allow rule for its
  Python 3.12 interpreter, so no change was made. If heads cannot connect, an
  administrator must add an inbound rule for TCP 8790 scoped to
  `192.0.2.0/24`. This package never changes firewall settings.

## Clearing viewer data on a head

Uninstall with *also remove the stored credential*. That deletes
`%LOCALAPPDATA%\AgentArena\credential.bin`, `config.json`, `window.json` and
`webview-data\`. The original viewer's `Capstone-Arena-Viewer\browser-profile`
is removed only when *delete its old browser data* is ticked during install.

## Files

| Path | Purpose |
|---|---|
| `%LOCALAPPDATA%\Programs\AgentArena\<version>\` | Program and Arena web files, with `app-manifest.json` |
| `%LOCALAPPDATA%\AgentArena\` | Configuration, DPAPI credential, logs, window state, WebView2 data |
| `HKCU\…\Run\AgentArena` | The single sign-in startup entry |
| Start menu → Agent Arena | Launch, Diagnostics, Setup |
