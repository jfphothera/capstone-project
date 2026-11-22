AGENT ARENA ADMIN HUB  (192.0.2.254)
====================================

This folder installs the Arena Event Service and the admin console on the admin hub.
The Inference Gateway, the Ticketing adapter and the head viewers install separately.

BEFORE YOU START
  - Run SET-UP-THIS-COMPUTER.cmd first (as administrator: it asks). It sets the address
    192.0.2.254, installs Python 3.12, keeps the computer awake, makes it the cluster's
    time server, answers ping and allows Remote Desktop from 192.0.2.1.
    The installer itself never changes network settings; it checks the address and stops
    if it is different.
  - Python 3.12 or later, installed for the Windows user who will sign in on the hub.
  - Sign in as that user. The service starts when they sign in, and the hub's screen
    shows the admin room.

INSTALL
  1. Run INSTALL-ADMIN-HUB.cmd from this folder.
  2. Every check runs first; nothing changes until all pass.
  3. Windows asks once for administrator approval: that is the firewall rule letting
     the heads (192.0.2.0/24 only) reach the event service on port 8790.
  4. At the end it opens the admin hub window and checks the service answers.
     RESULT success means done.
  5. "Agent Arena Admin Hub" on the desktop opens the window (Arena icon). It also opens by
     itself at sign-in. F11 or View > Full screen fills the screen; Esc leaves it.
     The tray icon shows whether the event service is up, can restart it, and opens its log.
     The event service runs without a window; closing the admin window does not stop it.

UPDATING
  Run INSTALL-ADMIN-HUB.cmd from a newer USB folder. It stops the running admin window,
  console and service itself, replaces the program, keeps the data, and starts them again.
  The result of every run is saved in results\ on this USB (no secrets).

IF THIS USB CARRIES THE OLD SERVICE'S DATA  (prepared with --move-data)
  The heads keep their enrollment; nobody needs new codes. Do it in this order:
  1. Install the hub as above. The old service on 192.0.2.1 keeps running meanwhile.
  2. On 192.0.2.1, tell the displays where the hub is:
        python -m arena_service.admin --config %LOCALAPPDATA%\AgentArenaService\aes.config.json endpoints-set http://192.0.2.254:8790
     (or the old admin console: Service > Display addresses).
     Each head checks that the hub answers with the same identity, then switches.
  3. Watch the heads come up in the hub's admin console (Machines). Then close the
     old service on 192.0.2.1 and don't start it again: it has the same identity.
  Don't add machines or issue codes on the old service between preparing this USB and
  step 3; those changes are not in the moved data.
  The moved data holds the service's credentials. Keep this USB private; a successful
  install deletes that copy from it.

FRESH HUB  (no data on this USB)
  Ticketing and heads .2-.10 are registered. Each head needs a new enrollment code from
  the admin console (Machines > Code) and a reinstall of its viewer.

AFTERWARDS
  - Gateway: when the gateway's operator publishes its observation file, add
    "gateway": {"observation_file": "<path>"} to
    %LOCALAPPDATA%\AgentArenaService\aes.config.json and reopen the admin console.
  - Ticketing: the adapter on the Capstone laptop points its "aes_url" at
    http://192.0.2.254:8790.
  - UNINSTALL-ADMIN-HUB.cmd removes the program, start at sign-in and the shortcut.
    Service data and the firewall rule stay.
