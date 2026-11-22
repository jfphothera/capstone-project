AGENT ARENA - TICKETING ADAPTER  (the Capstone laptop, 192.0.2.200-.249)
=========================================================================

The adapter reads Capstone's database (read-only) and shows Ticketing's agents in the Arena.
It runs next to Capstone, on the Capstone laptop.

ORDER
  1. On the hub's admin console: Machines > Addresses > reserve the laptop's address as an
     Application laptop for Ticketing (capstone/main). Note the claim code.
  2. Provision the laptop with that address and claim code (the provisioner).
  3. Install Capstone from its own project, and start it once (python run_app.py).
  4. On the hub's admin console: Machines > Applications > Ticketing > App code.
  5. On the laptop, run INSTALL-TICKETING-ADAPTER.cmd and type the app code when asked.
     The code works once, only from this laptop; the credential is stored for this Windows
     user and never travels on the USB.
  6. On the hub, Machines > Applications shows Ticketing "online" while Capstone runs.

  Capstone's database is looked for at %USERPROFILE%\PycharmProjects\Capstone\data\operations.db.
  If it lives elsewhere: INSTALL-TICKETING-ADAPTER.cmd --capstone-db <path>

  Running it again updates the program and keeps the credential (no code needed).
  A new app code replaces the credential; the adapter on any other machine stops publishing.
  Results of every run are in results\ on this USB (no secrets).
