"""arena-hub-migrate: move this hub's Arena to another Windows account on the same computer.

    python -m arena_service.hub_migrate status   --config <aes.config.json>
    python -m arena_service.hub_migrate export   --config <aes.config.json> --source <AGENT-ARENA-HUB folder> --dest <folder> --account <name>
    python -m arena_service.hub_migrate rollback --config <aes.config.json>

Why: the Inference Gateway (0.3.0) requires every account that runs an Arena process reading its
observation to be a standard account, not an administrator. Arena installs per user, so it moves to a
standard account the same way it moved from the development laptop to the hub: the new account runs
the unchanged INSTALL-ADMIN-HUB from a folder carrying "moved data", which keeps the service identity,
registrations, enrollments and history. Then the old copy is marked moved (moved-to-hub.json) and the
old service refuses to start, so two services never run on one identity.

export   copies the hub program folder, adds a consistent copy of the database (one self-contained
         file, no -wal/-shm) and source.json, and marks the old data moved. The old service must be
         stopped first, or events after the copy would be lost; export refuses if it answers.
rollback removes the marker this tool wrote, so the old account's Arena starts again with its data as
         it was at the export. Run it only after the new account's Arena is stopped.
Every command prints one JSON object; exit 0 ok, 1 refused, 2 bad input. The copy holds the service's
credentials: the destination folder must be private (F: restricts it to the target account).
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

MARKER = "moved-to-hub.json"
REASON = "moved to a standard account"


def _load(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return config, Path(config["db_path"])


def _answers(config):
    url = f"http://{config.get('host', '127.0.0.1')}:{config.get('port', 8790)}/v2/health"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as response:
            return json.loads(response.read()).get("service_id")
    except (OSError, ValueError):
        return None


def status(config_path):
    config, db = _load(config_path)
    marker = db.parent / MARKER
    out = {"ok": True, "config": str(config_path), "db": str(db), "db_present": db.exists(), "marker": None, "service_answers": _answers(config)}
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            out["service_id"] = json.loads(con.execute("SELECT value FROM meta WHERE key='service_id'").fetchone()[0])
            out["active_displays"] = con.execute("SELECT COUNT(*) FROM displays WHERE status='active'").fetchone()[0]
        finally:
            con.close()
    if marker.exists():
        out["marker"] = json.loads(marker.read_text(encoding="utf-8"))
    return out


def export(config_path, source, dest, account, *, answering=_answers):
    config, db = _load(config_path)
    source, dest = Path(source), Path(dest)
    if answering(config):
        return {"ok": False, "error": "the Arena event service is still running here: stop the admin hub first, so no event is lost"}
    if not (source / "manifest.json").is_file() or not (source / "app").is_dir():
        return {"ok": False, "error": f"{source} is not an AGENT-ARENA-HUB folder"}
    if (db.parent / MARKER).exists():
        return {"ok": False, "error": f"{db} is already marked moved; roll back first or use the data where it went"}
    target = dest / "AGENT-ARENA-HUB"
    if target.exists():
        return {"ok": False, "error": f"{target} exists; choose an empty destination"}
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("data", "results"))
    (target / "data").mkdir()
    src, dst = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True), sqlite3.connect(target / "data" / "arena-events.db")
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return {"ok": False, "error": "the copy failed its integrity check; nothing was marked"}
        service_id = json.loads(dst.execute("SELECT value FROM meta WHERE key='service_id'").fetchone()[0])
        displays = dst.execute("SELECT COUNT(*) FROM displays WHERE status='active'").fetchone()[0]
    finally:
        dst.close()
        src.close()
    taken = time.strftime("%Y-%m-%d %H:%M:%S")
    record = {"service_id": service_id, "from_computer": socket.gethostname(), "from_config": str(config_path),
              "endpoints": config.get("public_endpoints"), "active_displays": displays, "taken_at": taken, "to_account": account}
    (target / "data" / "source.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (db.parent / MARKER).write_text(json.dumps({"service_id": service_id, "hub": (config.get("public_endpoints") or [None])[0],
                                                "moved_at": taken, "reason": f"{REASON} ({account})"}, indent=2), encoding="utf-8")
    return {"ok": True, "folder": str(target), "service_id": service_id, "active_displays": displays, "marked": str(db.parent / MARKER),
            "next": f"sign in as {account} and run {target / 'INSTALL-ADMIN-HUB.cmd'}"}


def rollback(config_path, *, answering=_answers):
    config, db = _load(config_path)
    marker = db.parent / MARKER
    if not marker.exists():
        return {"ok": True, "changed": False, "note": "not marked moved"}
    record = json.loads(marker.read_text(encoding="utf-8"))
    if REASON not in str(record.get("reason", "")):
        return {"ok": False, "error": "this marker was not written by an account migration (it is the move to the hub); not removed"}
    if answering(config):
        return {"ok": False, "error": "an Arena event service answers on this address: stop the new account's Arena first"}
    marker.unlink()
    return {"ok": True, "changed": True, "service_id": record.get("service_id"), "note": "the old account's Arena may start again"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arena-hub-migrate", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("status", "export", "rollback"):
        p = sub.add_parser(verb)
        p.add_argument("--config", required=True)
        if verb == "export":
            p.add_argument("--source", required=True)
            p.add_argument("--dest", required=True)
            p.add_argument("--account", required=True)
    args = parser.parse_args(argv)
    try:
        if args.verb == "status":
            result = status(args.config)
        elif args.verb == "export":
            result = export(args.config, args.source, args.dest, args.account)
        else:
            result = rollback(args.config)
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
