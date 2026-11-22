"""arena-hub-install: install the Arena admin hub (event service + admin console) from its USB folder.

    INSTALL-ADMIN-HUB.cmd     runs  python -E -s -m arena_service.hub_install --usb <USB folder>
    UNINSTALL-ADMIN-HUB.cmd   runs  ... --uninstall

Every check runs first and nothing changes until all pass: this computer already has the hub's
address (192.0.2.254; this never sets an address), Python 3.12 or later, ports 8790 and 8791 free,
and the USB folder matches its manifest. Then, in order:
  1. program files into %LOCALAPPDATA%\\Programs\\AgentArenaHub\\app, with their own venv;
  2. service data: moved from the old service when the USB carries it, kept when this hub has
     some, otherwise fresh (Ticketing and heads .2-.10 registered);
  3. the service configuration for this address, keeping any other settings (such as "gateway");
  4. an inbound firewall rule for the event service, cluster network only (one UAC prompt);
  5. start at sign-in, and an Agent Arena Admin Console shortcut on the desktop;
  6. start the service and check it answers with the expected service ID.
It never touches the Inference Gateway, Capstone, the heads or network settings. Moved data holds
the service's credentials, so after a successful install its copy on the USB is deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import __version__
from .admin import head_identity
from .store import Store

HUB_ADDRESS = "192.0.2.254"
SERVICE_PORT, CONSOLE_PORT = 8790, 8791
CLUSTER = "192.0.2.0/24"
FIRST_HEADS = [f"192.0.2.{n}" for n in range(2, 11)]
FIREWALL_RULE = "Agent Arena Event Service"
STARTUP_VALUE, OLD_STARTUP = "Agent Arena Admin Hub", "Agent Arena Event Service"
SHORTCUT, OLD_SHORTCUT = "Agent Arena Admin Hub.lnk", "Agent Arena Admin Console.lnk"
MANIFEST_SCHEMA = "arena.hub-usb.v1"


class InstallError(Exception):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_usb(usb, schema=MANIFEST_SCHEMA):
    """The USB folder against its manifest: every listed file present and unchanged."""
    try:
        manifest = json.loads((usb / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ["manifest.json is missing or unreadable: prepare the USB again"]
    if manifest.get("schema") != schema:
        return None, [f"manifest.json is not a {schema} manifest"]
    problems = []
    for rel, digest in manifest["files"].items():
        path = usb / rel
        if not path.is_file():
            if not rel.startswith("data/"):              # moved data is deleted after a successful install
                problems.append(f"missing {rel}")
        elif sha256(path) != digest:
            problems.append(f"changed {rel}")
    return manifest, problems


def _powershell(script):
    """Run a PowerShell script without Windows command-line quoting in the way."""
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True)


def _ps(text):
    return "'" + str(text).replace("'", "''") + "'"


class Windows:
    """The few changes outside the program and data folders, kept apart so tests can stand in."""

    def addresses(self):
        found = set()
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
        return found

    def port_free(self, host, port):
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False

    def make_venv(self, folder):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(folder)], check=True, capture_output=True)

    def firewall(self, port, remote):
        show = ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"]
        if subprocess.run(show, capture_output=True).returncode == 0:
            return "already present"
        add = (f'advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow protocol=TCP '
               f"localport={port} remoteip={remote} profile=any")
        _powershell(f"Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList {_ps(add)}")   # one UAC prompt
        if subprocess.run(show, capture_output=True).returncode == 0:
            return "added"
        return "NOT added (the administrator prompt was declined). Run as administrator: netsh " + add

    def set_startup(self, command, name=STARTUP_VALUE):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
            if command is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

    def shortcut(self, target, remove=False, name=SHORTCUT, arguments="", icon=None):
        # The desktop may be redirected; ask Windows where it is rather than guessing a path.
        script = (f"$d=[Environment]::GetFolderPath('Desktop');$p=Join-Path $d {_ps(name)};"
                  + ("if(Test-Path $p){Remove-Item $p}" if remove else
                     f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);$s.TargetPath={_ps(target)};"
                     f"$s.Arguments={_ps(arguments)};$s.WorkingDirectory={_ps(Path(target).parent)};"
                     + (f"$s.IconLocation={_ps(str(icon) + ',0')};" if icon else "") + "$s.Save()"))
        if _powershell(script).returncode != 0:
            raise OSError("could not " + ("remove" if remove else "create") + " the desktop shortcut")

    def start_service(self, launcher, title="Arena Event Service"):
        subprocess.Popen(["cmd", "/c", "start", title, "/min", str(launcher)], cwd=str(Path(launcher).parent.parent))

    def start_admin(self, exe):
        """The admin hub window; it starts the event service without a window when it doesn't answer."""
        subprocess.Popen([str(exe), "--admin"], cwd=str(Path(exe).parent))

    def stop_hub(self, program, config):
        """Stop this hub's window (run from its program folder) and its console and event service (Python
        running arena_service with this hub's configuration: a venv's python.exe is only a launcher whose
        child is the base Python, so the command line is what identifies them). Nothing else is touched."""
        script = (f"$root={_ps(str(Path(program)) + chr(92))};$cfg={_ps(str(config))};$n=0;"
                  f"Get-CimInstance Win32_Process | Where-Object {{ ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, "
                  f"[StringComparison]::OrdinalIgnoreCase)) -or ($_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -and "
                  f"$_.CommandLine.Contains('-m arena_service') -and $_.CommandLine.IndexOf($cfg, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or "
                  f"($_.Name -eq 'cmd.exe' -and $_.CommandLine -and $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0) }} | "
                  f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }}; Start-Sleep -Milliseconds 800; $n")
        out = _powershell(script).stdout.decode(errors="replace").strip().splitlines()
        return int(out[-1]) if out and out[-1].isdigit() else 0

    def health(self, url, path="/v2/health"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + path, timeout=3) as response:
            return json.loads(response.read())


def checks(usb, system, address, data=None, replace_data=False, ports=True):
    manifest, problems = verify_usb(usb)
    if data is not None and (usb / "data" / "arena-events.db").exists() and (Path(data) / "arena-events.db").exists() and not replace_data:
        problems.append(f"this computer already has service data ({Path(data) / 'arena-events.db'}) and the USB carries "
                        f"moved data. Run INSTALL-ADMIN-HUB.cmd --replace-data to replace it (the current copy is backed up first)")
    here = address in system.addresses()
    if not here:
        problems.insert(0, f"this computer does not have the hub's address {address}. Give it that address first "
                           f"(the installer never changes network settings)")
    if sys.version_info < (3, 12):
        problems.append("Python 3.12 or later is needed")
    for host, port in (((address, SERVICE_PORT),) * here + (("127.0.0.1", CONSOLE_PORT),)) * ports:
        if not system.port_free(host, port):
            problems.append(f"port {port} is in use: if the event service or admin console is already running, close it and run this again")
    return manifest, problems


def _copy_db(source, target):
    """A consistent copy, which also proves the file is a readable sqlite database."""
    src, dst = sqlite3.connect(f"file:{Path(source).as_posix()}?mode=ro", uri=True), sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise InstallError(f"{source} failed its integrity check")
    finally:
        dst.close()
        src.close()


def install(usb, *, program, data, system, address=HUB_ADDRESS, replace_data=False, keep_usb_data=False, say=print):
    usb, program, data = Path(usb), Path(program), Path(data)
    manifest, problems = checks(usb, system, address, data, replace_data, ports=False)
    if problems:
        raise InstallError("nothing was changed:\n  - " + "\n  - ".join(problems))
    # An update: this hub's own window, console and service are stopped so their files can be replaced.
    stopped = system.stop_hub(program, data / "aes.config.json") if (program / "app").exists() else 0
    if stopped:
        say(f"Stopped the running admin hub ({stopped} process{'es' if stopped != 1 else ''}) to update it.")
    _, problems = checks(usb, system, address, data, replace_data)
    if problems:
        raise InstallError("nothing else was changed:\n  - " + "\n  - ".join(problems))
    url = f"http://{address}:{SERVICE_PORT}"
    steps = {}

    # 1. Program files, swapped in whole.
    app, partial = program / "app", program / "app.partial"
    shutil.rmtree(partial, ignore_errors=True)
    shutil.copytree(usb / "app", partial)
    system.make_venv(partial / ".venv")
    if app.exists():
        shutil.rmtree(app)
    partial.rename(app)
    steps["program"] = str(app)
    say(f"Program files installed in {app}")

    # 2. Service data.
    data.mkdir(parents=True, exist_ok=True)
    db, moved = data / "arena-events.db", usb / "data" / "arena-events.db"
    if moved.exists():
        source = json.loads((usb / "data" / "source.json").read_text(encoding="utf-8"))
        if db.exists():                                   # only with --replace-data: checks() refused otherwise
            (data / "backups").mkdir(exist_ok=True)
            kept = data / "backups" / f"arena-events-before-move-{time.strftime('%Y%m%d-%H%M%S')}.db"
            _copy_db(db, kept)
            db.unlink()
            for extra in ("-wal", "-shm"):
                Path(str(db) + extra).unlink(missing_ok=True)
            say(f"The hub's previous data was backed up to {kept}")
        _copy_db(moved, db)
        mode = "moved"
    else:
        mode = "kept" if db.exists() else "fresh"
    if db.exists():
        say("Preparing the service data (Arena 2.5.1, once: result totals and indexes; up to a minute on a large hub)...")
    store = Store(str(db))
    if mode == "moved" and store.meta("service_id") != source["service_id"]:
        raise InstallError("the moved data's service ID does not match what the USB recorded")
    if mode == "fresh":
        store.add_application("capstone", "Ticketing")
        store.add_orchestrator("capstone", "main", "Ticketing coordinator")
        for head in FIRST_HEADS:
            node, label = head_identity(head)
            store.add_display(node, "head", store.add_head(node, label + (" GPU" if head == "192.0.2.2" else ""), head), head)
    store.set_meta("endpoints", [url])
    service_id = store.meta("service_id")
    store.db.close()
    steps["data"] = {"mode": mode, "db": str(db), "service_id": service_id}
    say({"moved": f"Service data moved from the old service (service {service_id}): heads keep their enrollment.",
         "kept": f"This hub's existing service data was kept (service {service_id}).",
         "fresh": f"New service data (service {service_id}): Ticketing and heads .2-.10 registered; each head needs a code."}[mode])

    # 3. Configuration for this address; other settings (such as "gateway") are kept.
    config_path = data / "aes.config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config.update(host=address, port=SERVICE_PORT, db_path=str(db), log_path=str(data / "arena-event-service.log"),
                  public_endpoints=[url])
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    steps["config"] = str(config_path)

    # 4-5. Firewall, start at sign-in, shortcut.
    steps["firewall"] = system.firewall(SERVICE_PORT, CLUSTER)
    say(f"Firewall rule for port {SERVICE_PORT} from {CLUSTER}: {steps['firewall']}")
    # The admin hub window (AgentArena.exe --admin) opens at sign-in and starts the event service, which
    # runs without a window; earlier installs started a console window instead.
    exe = app / "desktop" / "AgentArena.exe"
    system.set_startup(None, name=OLD_STARTUP)
    system.set_startup(f'"{exe}" --admin')
    system.shortcut(None, remove=True, name=OLD_SHORTCUT)
    system.shortcut(str(exe), arguments="--admin", icon=str(exe))
    steps["startup"], steps["shortcut"] = "admin hub window at sign-in", SHORTCUT
    say("The admin hub window opens at sign-in and keeps the event service running; the Agent Arena Admin Hub "
        "shortcut is on the desktop.")

    # 6. Start and check.
    system.start_admin(exe)
    answer, deadline = None, time.time() + 40
    while time.time() < deadline:
        try:
            answer = system.health(url)
            break
        except OSError:
            time.sleep(0.5)
    if not answer or answer.get("service_id") != service_id:
        raise InstallError(f"the event service did not answer at {url} as {service_id}: see {config['log_path']}")
    steps["service"] = {"url": url, "service_id": service_id, "version": answer.get("version")}
    say(f"The event service answers at {url} (version {answer.get('version')}).")

    if mode == "moved" and not keep_usb_data:
        for f in (moved, Path(str(moved) + "-wal"), Path(str(moved) + "-shm")):
            f.unlink(missing_ok=True)
        steps["usb_data"] = "deleted from the USB (it holds the service's credentials)"
        say("The moved data was deleted from the USB: it holds the service's credentials.")
    return {"result": "success", "version": manifest.get("version"), "address": address, "steps": steps}


def uninstall(*, program, system, data=None, say=print):
    """Program files, start at sign-in and the shortcut. Service data and the firewall rule stay."""
    if (Path(program) / "app").exists():
        system.stop_hub(program, Path(data or Path(os.environ.get("LOCALAPPDATA", "")) / "AgentArenaService") / "aes.config.json")
    for name in (STARTUP_VALUE, OLD_STARTUP):
        system.set_startup(None, name=name)
    for name in (SHORTCUT, OLD_SHORTCUT):
        system.shortcut(None, remove=True, name=name)
    shutil.rmtree(Path(program) / "app", ignore_errors=True)
    say("Removed the program files, start at sign-in and the shortcut. Service data was kept; remove the firewall "
        f"rule with: netsh advfirewall firewall delete rule name=\"{FIREWALL_RULE}\"")
    return {"result": "success", "steps": {"uninstalled": str(Path(program) / "app")}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-hub-install", description=__doc__.split("\n\n")[0])
    parser.add_argument("--usb", required=True, help="the AGENT-ARENA-HUB folder on the USB")
    parser.add_argument("--address", default=HUB_ADDRESS, help=argparse.SUPPRESS)
    parser.add_argument("--replace-data", action="store_true", help="replace this hub's service data with the moved data")
    parser.add_argument("--keep-usb-data", action="store_true", help="keep the moved data on the USB after installing")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)
    ipaddress.IPv4Address(args.address)
    local = Path(os.environ["LOCALAPPDATA"])
    program, data, system = local / "Programs" / "AgentArenaHub", local / "AgentArenaService", Windows()
    usb = Path(args.usb).resolve()
    try:
        result = uninstall(program=program, system=system, data=data) if args.uninstall else install(
            usb, program=program, data=data, system=system, address=args.address,
            replace_data=args.replace_data, keep_usb_data=args.keep_usb_data)
    except (InstallError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        result = {"result": "failed", "error": str(exc)}
        print(f"\nFAILED: {exc}")
    result.update(computer=os.environ.get("COMPUTERNAME"), at=time.strftime("%Y-%m-%d %H:%M:%S"), installer=__version__)
    try:
        (usb / "results").mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (usb / "results" / f"{result['computer']}-{stamp}-{'uninstall' if args.uninstall else 'install'}.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    print("\nRESULT " + result["result"])
    return 0 if result["result"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
