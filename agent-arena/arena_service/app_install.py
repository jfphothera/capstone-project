"""arena-ticketing-install: install the Arena's Ticketing adapter on the Capstone laptop.

    INSTALL-TICKETING-ADAPTER.cmd   runs  python -E -s -m arena_service.app_install --usb <USB folder>

The adapter reads Capstone's operations.db read-only, next to Capstone, and publishes Ticketing's
activity to the Arena on the hub. Checks run first and nothing changes until all pass: this laptop
has an application address (192.0.2.200-.249), Python 3.12 or later, Capstone's database is
there, the hub answers, and the USB matches its manifest. Then:
  1. program files into %LOCALAPPDATA%\\Programs\\AgentArenaAdapter\\app, with their own venv;
  2. the producer credential, from the one-time app code the hub's admin console issued (the code
     is used once, from this laptop; the secret is sealed to it and stored with Windows DPAPI,
     never on the USB). A reinstall without a code keeps the credential already here;
  3. the adapter configuration (Capstone's database, the hub, a fresh checkpoint);
  4. start at sign-in, then start the adapter now.
It never writes to Capstone: Capstone itself installs from its own project, before this.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__, secretfile
from .client import ArenaError, enroll_app
from .hub_install import HUB_ADDRESS, SERVICE_PORT, InstallError, Windows, verify_usb

MANIFEST_SCHEMA = "arena.ticketing-usb.v1"
APP_ID, ORCHESTRATOR = "capstone", "main"
STARTUP_VALUE = "Agent Arena Ticketing Adapter"
CAPSTONE_HEALTH = "http://127.0.0.1:8000"


def app_address(addresses):
    return next((a for a in sorted(addresses) if a.startswith("192.0.2.") and 200 <= int(a.rsplit(".", 1)[1]) <= 249), None)


def install(usb, *, program, data, system, capstone_db, code=None, hub=f"http://{HUB_ADDRESS}:{SERVICE_PORT}", say=print):
    usb, program, data, capstone_db = Path(usb), Path(program), Path(data), Path(capstone_db)
    manifest, problems = verify_usb(usb, MANIFEST_SCHEMA)
    address = app_address(system.addresses())
    if not address:
        problems.insert(0, "this laptop has no application address (192.0.2.200-.249). Provision it first; "
                           "the installer never changes network settings")
    if sys.version_info < (3, 12):
        problems.append("Python 3.12 or later is needed")
    if not capstone_db.is_file():
        problems.append(f"Capstone's database is not at {capstone_db}: install Capstone first, or give --capstone-db")
    credential_file = data / "ticketing-producer.dpapi"
    if not code and not credential_file.exists():
        problems.append("an app code is needed: issue one in the hub's admin console (Machines > Applications > App code)")
    try:
        hub_id = system.health(hub)["service_id"]
    except (OSError, ValueError, KeyError) as exc:
        problems.append(f"the Arena hub does not answer at {hub} ({type(exc).__name__})")
        hub_id = None
    if problems:
        raise InstallError("nothing was changed:\n  - " + "\n  - ".join(problems))
    steps = {"address": address, "hub": {"url": hub, "service_id": hub_id}}

    app, partial = program / "app", program / "app.partial"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(usb / "app", partial)
    system.make_venv(partial / ".venv")
    if app.exists():
        # Arena 2.5.0: an update stops the running adapter first (its window, its venv Python and the Python running
        # arena_service with this adapter's configuration), or Windows refuses to delete its files. Capstone's own
        # processes match none of these and keep running.
        steps["stopped"] = system.stop_hub(program, data / "ticketing-adapter.json")
        if steps["stopped"]:
            say(f"Stopped the running adapter ({steps['stopped']} process(es)); Capstone is not touched.")
        shutil.rmtree(app)
    partial.rename(app)
    steps["program"] = str(app)
    say(f"Program files installed in {app}")

    data.mkdir(parents=True, exist_ok=True)
    if code:
        try:
            credential_id, secret, answer = enroll_app(hub, APP_ID, ORCHESTRATOR, code)
        except ArenaError as exc:
            raise InstallError(f"the hub refused the app code ({exc.message or exc.code}). Program files are installed; "
                               "issue a new app code and run this again") from None
        if answer.get("service_id") != hub_id:
            raise InstallError("the hub's identity changed during the install; run it again")
        secretfile.write(str(credential_file), credential_id, secret)
        steps["credential"] = f"new ({credential_id}), stored with DPAPI for this Windows user"
        say("The producer credential was issued by the hub and stored for this Windows user (DPAPI).")
    else:
        steps["credential"] = "kept (already on this laptop)"
        say("The producer credential already on this laptop was kept.")

    config_path = data / "ticketing-adapter.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    moved = config.get("capstone_db") not in (None, str(capstone_db))
    config.update(capstone_db=str(capstone_db), aes_url=hub, credential_file=str(credential_file),
                  checkpoint_file=str(data / "ticketing-adapter.checkpoint.json"), capstone_health_url=CAPSTONE_HEALTH + "/api/health",
                  log_path=str(data / "ticketing-adapter.log"))
    config.setdefault("start_from", "now")
    config.setdefault("heartbeat_seconds", 15)
    if moved:
        Path(config["checkpoint_file"]).unlink(missing_ok=True)      # a different database: start at its current end
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    steps["config"] = str(config_path)

    launcher = app / "coordinator" / "START-TICKETING-ADAPTER.cmd"
    system.set_startup(f'cmd.exe /c start "Ticketing Arena adapter" /min "{launcher}"', name=STARTUP_VALUE)
    system.start_service(launcher, title="Ticketing Arena adapter")
    steps["startup"] = "at sign-in; started now"
    try:
        capstone_ok = bool(system.health(CAPSTONE_HEALTH, "/api/health").get("ok"))
    except (OSError, ValueError):
        capstone_ok = False
    steps["capstone"] = "answering" if capstone_ok else "not answering yet"
    say("The adapter starts at sign-in and is running now. " + (
        "Capstone answers, so Ticketing shows online on the hub within 15 s." if capstone_ok else
        "Capstone is not answering on :8000 yet; Ticketing shows online once it is."))
    return {"result": "success", "version": manifest.get("version"), "address": address, "steps": steps}


def default_capstone_db(environ=None):
    """Capstone's live database: the installed service's (C:\\ProgramData\\Capstone\\data) when it is there, else a
    developer checkout's. Arena 2.5.1: before, only the checkout path was tried, so running INSTALL-TICKETING-ADAPTER
    directly on the lab laptop (2026-11-18) failed with "Capstone's database is not at ..." and changed nothing."""
    environ = os.environ if environ is None else environ
    candidates = [Path(environ.get("ProgramData", r"C:\ProgramData")) / "Capstone" / "data" / "operations.db",
                  Path(environ.get("USERPROFILE", "~")) / "PycharmProjects" / "Capstone" / "data" / "operations.db"]
    return str(next((c for c in candidates if c.is_file()), candidates[0]))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-ticketing-install", description=__doc__.split("\n\n")[0])
    parser.add_argument("--usb", required=True)
    parser.add_argument("--capstone-db", default=default_capstone_db())
    parser.add_argument("--code", help="the one-time app code (asked for when missing)")
    parser.add_argument("--hub", default=f"http://{HUB_ADDRESS}:{SERVICE_PORT}", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    local = Path(os.environ["LOCALAPPDATA"])
    data, usb = local / "AgentArenaService", Path(args.usb).resolve()
    code = args.code
    if not code and not (data / "ticketing-producer.dpapi").exists():
        code = input("App code from the hub's admin console (ARENA-....): ").strip() or None
    try:
        result = install(usb, program=local / "Programs" / "AgentArenaAdapter", data=data, system=Windows(),
                         capstone_db=args.capstone_db, code=code, hub=args.hub)
    except (InstallError, OSError) as exc:
        result = {"result": "failed", "error": str(exc)}
        print(f"\nFAILED: {exc}")
    result.update(computer=os.environ.get("COMPUTERNAME"), at=time.strftime("%Y-%m-%d %H:%M:%S"), installer=__version__)
    try:
        (usb / "results").mkdir(exist_ok=True)
        (usb / "results" / f"{result['computer']}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    print("\nRESULT " + result["result"])
    return 0 if result["result"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
