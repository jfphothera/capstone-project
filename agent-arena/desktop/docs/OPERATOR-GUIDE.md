# Agent Arena 2.0.0: operator guide

## Which machine is which

| Machine | Address | What runs there | Install Agent Arena? |
|---|---|---|---|
| Coordinator | `192.0.2.1` | Capstone ("Ticketing"), the **Arena Event Service** (port 8790) and the Ticketing adapter | Only as the orchestrator display, if wanted |
| GPU head | `192.0.2.2` | Ollama | **Yes** (head role) |
| CPU heads | `192.0.2.3` – `192.0.2.10` | Ollama | **Yes** (head role) |

```
 Heads (Agent Arena window, local files) ──read only──► Arena Event Service (192.0.2.1:8790) ◄── Ticketing adapter ◄── Capstone
                                                                     ▲
                                                                     └── Coder and future applications (own credentials)
```

Agent Arena is a normal desktop window. The whole Arena is installed on the
head. The coordinator supplies live activity, not the interface. If the
coordinator stops, the window stays open and says **Disconnected**. It
reconnects by itself.

## Coordinator, once (administrator)

In the project folder `coordinator\`:

1. Run `SETUP-ARENA-EVENT-SERVICE.cmd`. This is idempotent: it registers
   Ticketing and heads `.2` to `.10`.
2. Run `START-ARENA-EVENT-SERVICE.cmd`, then `START-TICKETING-ADAPTER.cmd`.
   Leave both windows open. Neither depends on Capstone being up.
3. For each head, run `ENROLL-DISPLAY.cmd head-192-0-2-N`. Give the printed
   one-time code to the person at that head.

## On each head

1. Copy the ZIP from USB and **extract all of it**.
2. Double-click **INSTALL AGENT ARENA**.
3. Confirm "Detected identity" shows this head, for example
   `head-192-0-2-3`.
4. Type the enrollment code and click **Install**. Wait for **SUCCESS**.
5. Click **Launch Arena**.

You do **not** enter IP addresses, ports, node IDs or PowerShell commands, and
you do not change execution policy. The window keeps the display awake only
while it is open, and it starts at sign-in for this Windows user.

## Diagnostics, repair, upgrade, uninstall

- **Diagnostics:** double-click **AGENT ARENA DIAGNOSTICS**. This works
  before installation. Click **Export report…**; the report has no secrets.
- **Repair or upgrade:** run **INSTALL AGENT ARENA** again. It detects
  partial, damaged and older installs and keeps your enrollment.
- **Uninstall:** Start menu → Agent Arena → *Agent Arena Setup* →
  **Uninstall**. It stops the window, removes sign-in startup and removes the
  program files. By default it **keeps** the stored credential and the Arena's
  own browser data, so a reinstall needs no new code. Tick *also remove the
  stored credential* to erase them. When you retire a machine, ask the
  administrator to revoke its credential.

## The original Phase 4.2 viewer (Head 3)

The installer looks for anything left by the original viewer: its folder,
startup shortcut, scheduled task, watchdog and Edge window. It removes **only
those items**. Its old browser data is kept unless you tick *delete its old
browser data*. Ollama, models, network and power settings are never touched.
