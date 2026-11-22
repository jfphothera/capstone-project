"""arena-hub-usb: write the admin hub and Ticketing adapter installer folders onto a USB drive.

    PREPARE-HUB-USB.cmd [D:\\]               a fresh hub: Ticketing and heads .2-.10 registered, new codes
    PREPARE-HUB-USB.cmd [D:\\] --move-data   also carry this service's data, so heads keep their enrollment

AGENT-ARENA-HUB installs the hub; AGENT-ARENA-TICKETING installs the Ticketing adapter on the
Capstone laptop, which carries no credential (it redeems a one-time app code from the hub).

It copies only what the hub runs: the event service and admin console code, the admin pages and
their art, and the hub's launchers. Every file goes into manifest.json with its SHA-256, and the
installer refuses a USB that doesn't match. Other folders on the drive (such as a head package)
are left alone. Moved data is a consistent copy of the live database taken while the service
keeps running; it holds the service's credentials, so keep the USB private until the install has
deleted it.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__
from .hub_install import MANIFEST_SCHEMA, sha256

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "AGENT-ARENA-HUB"
# What the admin pages load (admin.html, admin-room.html and their scripts); tests keep this complete.
FRONTEND = ["admin.html", "admin-room.html", "css/admin_console.css", "css/scene_composer.css", "js/admin_console.mjs", "js/fleet.mjs",
            "js/admin_room.mjs", "js/traffic_board.mjs", "assets/admin-hub-v1", "assets/admin-traffic-board-v1",
            "assets/characters-v1", "assets/scenes/_shared",
            # Arena 2.4.0: the robot cat's tour (cat_tour.mjs reads the room list) and its frames.
            "js/cat_tour.mjs", "js/composer_rooms.mjs", "assets/characters-v2/bots/h-robot-cat",
            # Arena 2.5.0: the frame list for the artist's animations (the cat's leap, sit and tail-flick on the desk).
            "js/body_frames.mjs"]
LAUNCHERS = ["START-ARENA-EVENT-SERVICE.cmd", "OPEN-ADMIN-CONSOLE.cmd", "REGISTER-DISPLAY.cmd", "ENROLL-DISPLAY.cmd"]
# The admin hub window is the heads' app in admin mode (AgentArena.exe --admin): only the exe and its
# WebView2 libraries, since the window loads its page from the admin console.
DESKTOP_VERSION = "2.3.0"
DESKTOP_FILES = ["AgentArena.exe", "AgentArena.exe.config", "Microsoft.Web.WebView2.Core.dll",
                 "Microsoft.Web.WebView2.WinForms.dll", "WebView2Loader.dll", "WebView2-LICENSE.txt"]
USB_FILES = ROOT / "coordinator" / "hub-usb"           # INSTALL-, UNINSTALL-ADMIN-HUB.cmd and README-HUB.txt
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "tests")
TICKETING_FOLDER = "AGENT-ARENA-TICKETING"
TICKETING_FILES = ROOT / "coordinator" / "ticketing-usb"   # INSTALL-TICKETING-ADAPTER.cmd and README-TICKETING.txt
TICKETING_SCHEMA = "arena.ticketing-usb.v1"


def _copy(source, target):
    """Copy a file; .cmd and .txt go out with CRLF line endings, as cmd.exe and Notepad expect."""
    if Path(source).suffix.lower() in (".cmd", ".txt"):
        text = Path(source).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\n", "\r\n")
        Path(target).write_bytes(text.encode("utf-8"))
    else:
        shutil.copy2(source, target)


def _start(drive, folder, what):
    out, partial = Path(drive) / folder, Path(drive) / (folder + ".partial")
    if out.exists() and not (out / "manifest.json").exists():
        raise SystemExit(f"{out} exists and is not {what} USB folder; move it aside first")
    shutil.rmtree(partial, ignore_errors=True)
    return out, partial


def _finish(out, partial, manifest_fields):
    """Hash every file into the manifest, keep earlier install results, and replace the old folder whole."""
    files = {p.relative_to(partial).as_posix(): sha256(p) for p in sorted(partial.rglob("*")) if p.is_file()}
    manifest = dict(manifest_fields, version=__version__, prepared_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    prepared_on=socket.gethostname(), files=files)
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if out.exists():
        results = out / "results"
        if results.exists():
            shutil.copytree(results, partial / "results")        # earlier install results stay with the USB
        shutil.rmtree(out)
    for attempt in range(20):           # a just-deleted folder on a USB can stay locked a moment (Explorer, indexing, AV)
        try:
            partial.rename(out)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    return manifest, sum((out / rel).stat().st_size for rel in files) / 1e6


def prepare(drive, *, repo=ROOT, move_data_config=None, say=print):
    out, partial = _start(drive, FOLDER, "an admin hub")
    app = partial / "app"
    shutil.copytree(repo / "arena_service", app / "arena_service", ignore=SKIP)
    for rel in FRONTEND:
        source, target = repo / "frontend" / rel, app / "frontend" / rel
        if source.is_dir():
            shutil.copytree(source, target, ignore=SKIP)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    package = repo / "dist" / f"Agent-Arena-{DESKTOP_VERSION}" / "app"
    if not (package / "AgentArena.exe").is_file():
        raise SystemExit(f"Build the desktop package first: desktop\\build\\Build-AgentArena.ps1 -Version {DESKTOP_VERSION}")
    (app / "desktop").mkdir(parents=True)
    for name in DESKTOP_FILES:
        shutil.copy2(package / name, app / "desktop" / name)
    (app / "coordinator").mkdir()
    for name in LAUNCHERS:
        _copy(repo / "coordinator" / name, app / "coordinator" / name)
    for f in USB_FILES.iterdir():
        _copy(f, partial / f.name)
    source = None
    if move_data_config:
        config = json.loads(Path(move_data_config).read_text(encoding="utf-8"))
        (partial / "data").mkdir()
        src = sqlite3.connect(f"file:{Path(config['db_path']).as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(partial / "data" / "arena-events.db")
        try:
            src.backup(dst)         # consistent while the service keeps writing; the live file is only read
            dst.execute("PRAGMA journal_mode=DELETE")     # one self-contained file on the USB, no -wal/-shm
            service_id = json.loads(dst.execute("SELECT value FROM meta WHERE key='service_id'").fetchone()[0])
            displays = dst.execute("SELECT COUNT(*) FROM displays WHERE status='active'").fetchone()[0]
        finally:
            dst.close()
            src.close()
        source = {"service_id": service_id, "from_computer": socket.gethostname(), "from_config": str(move_data_config),
                  "endpoints": config.get("public_endpoints"), "active_displays": displays, "taken_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        (partial / "data" / "source.json").write_text(json.dumps(source, indent=2), encoding="utf-8")
        # The service here must not start again once the hub has its identity (see arena_service.__main__).
        (Path(config["db_path"]).parent / "moved-to-hub.json").write_text(json.dumps(
            {"service_id": service_id, "hub": "http://192.0.2.254:8790", "moved_at": source["taken_at"]}, indent=2), encoding="utf-8")
    manifest, size = _finish(out, partial, {"schema": MANIFEST_SCHEMA, "moves_data": bool(source),
                                            "service_id": source and source["service_id"]})
    say(f"Admin hub USB ready: {out} ({len(manifest['files'])} files, {size:.0f} MB, version {__version__}).")
    if source:
        say(f"It carries this service's data (service {source['service_id']}, {source['active_displays']} active displays).")
        say("That copy holds the service's credentials: keep the USB private. The install deletes it from the USB.")
    return manifest


def prepare_head_fix(drive, *, repo=ROOT, say=print):
    """FIX-EXISTING-HEADS: the one-off script that brings heads set up by provisioner 1.0.3 in line with
    the hub (Ollama firewall, time, power). Earlier reports in results\\ are kept."""
    out = Path(drive) / "FIX-EXISTING-HEADS"
    out.mkdir(exist_ok=True)
    for f in (repo / "coordinator" / "fleet").iterdir():
        if f.is_file():
            _copy(f, out / f.name)
    say(f"Existing-heads fix ready: {out}")


def prepare_ticketing(drive, *, repo=ROOT, say=print):
    """AGENT-ARENA-TICKETING: the Ticketing adapter for the Capstone laptop. It carries no credential:
    the installer redeems a one-time app code from the hub's admin console."""
    out, partial = _start(drive, TICKETING_FOLDER, "a Ticketing adapter")
    shutil.copytree(repo / "arena_service", partial / "app" / "arena_service", ignore=SKIP)
    (partial / "app" / "coordinator").mkdir()
    _copy(repo / "coordinator" / "START-TICKETING-ADAPTER.cmd", partial / "app" / "coordinator" / "START-TICKETING-ADAPTER.cmd")
    for f in TICKETING_FILES.iterdir():
        _copy(f, partial / f.name)
    manifest, size = _finish(out, partial, {"schema": TICKETING_SCHEMA})
    say(f"Ticketing adapter USB ready: {out} ({len(manifest['files'])} files, {size:.1f} MB, version {__version__}).")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-hub-usb", description=__doc__.split("\n\n")[0])
    parser.add_argument("drive", nargs="?", default="D:\\", help="the USB drive (default D:\\)")
    parser.add_argument("--move-data", action="store_true", help="carry this computer's service data to the hub")
    parser.add_argument("--config", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "AgentArenaService", "aes.config.json"),
                        help="this computer's service configuration (for --move-data)")
    args = parser.parse_args(argv)
    if not Path(args.drive).exists():
        print(f"{args.drive} is not available: insert the USB drive")
        return 2
    if Path(args.drive).resolve().anchor.upper() == Path(os.environ.get("SystemDrive", "C:") + "\\").anchor.upper():
        print("That is the system drive; give the USB drive's letter")
        return 2
    if args.move_data and not Path(args.config).exists():
        print(f"--move-data needs this computer's service configuration, not found at {args.config}")
        return 2
    prepare(args.drive, move_data_config=args.config if args.move_data else None)
    prepare_ticketing(args.drive)
    prepare_head_fix(args.drive)
    _copy(ROOT / "coordinator" / "usb-root" / "START-HERE.txt", Path(args.drive) / "START-HERE.txt")
    print(f"Steps in order: {Path(args.drive) / 'START-HERE.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
