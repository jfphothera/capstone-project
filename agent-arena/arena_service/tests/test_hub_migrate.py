"""Moving the hub's Arena to a standard account: export, the start guard, rollback."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from arena_service import hub_install, hub_migrate
from arena_service.store import Store


@pytest.fixture()
def hub(tmp_path):
    data = tmp_path / "old" / "AgentArenaService"
    data.mkdir(parents=True)
    db = data / "arena-events.db"
    store = Store(str(db))
    store.add_display("head-192-0-2-3", "head", store.add_head("head-192-0-2-3", "Head 3", "192.0.2.3"), "192.0.2.3")
    service_id = store.meta("service_id")
    store.db.close()
    config = data / "aes.config.json"
    config.write_text(json.dumps({"db_path": str(db), "host": "127.0.0.1", "port": 1, "public_endpoints": ["http://192.0.2.254:8790"],
                                  "gateway": {"observation_file": "x"}}), encoding="utf-8")
    source = tmp_path / "usb" / "AGENT-ARENA-HUB"
    (source / "app").mkdir(parents=True)
    (source / "app" / "x.txt").write_text("program", encoding="utf-8")
    (source / "manifest.json").write_text(json.dumps({"schema": hub_install.MANIFEST_SCHEMA, "files": {"app/x.txt": hub_install.sha256(source / "app" / "x.txt")}}), encoding="utf-8")
    return {"config": config, "db": db, "source": source, "service_id": service_id, "dest": tmp_path / "migration"}


def quiet(_config):
    return None


def test_export_carries_the_identity_and_marks_the_old_copy(hub):
    out = hub_migrate.export(hub["config"], hub["source"], hub["dest"], ".\\arena-hub", answering=quiet)
    assert out["ok"] and out["service_id"] == hub["service_id"] and out["active_displays"] == 1
    target = Path(out["folder"])
    moved = target / "data" / "arena-events.db"
    assert moved.is_file() and not Path(str(moved) + "-wal").exists()
    assert json.loads((target / "data" / "source.json").read_text())["service_id"] == hub["service_id"]
    # the unchanged hub installer accepts the folder: manifest intact, moved data present
    manifest, problems = hub_install.verify_usb(target)
    assert manifest and problems == []
    con = sqlite3.connect(moved)
    assert json.loads(con.execute("SELECT value FROM meta WHERE key='service_id'").fetchone()[0]) == hub["service_id"]
    con.close()
    marker = json.loads((hub["db"].parent / hub_migrate.MARKER).read_text())
    assert marker["service_id"] == hub["service_id"] and hub_migrate.REASON in marker["reason"]


def test_the_old_service_refuses_to_start_once_marked(hub):
    hub_migrate.export(hub["config"], hub["source"], hub["dest"], ".\\arena-hub", answering=quiet)
    run = subprocess.run([sys.executable, "-m", "arena_service", "--config", str(hub["config"])], capture_output=True, text=True, timeout=60,
                         cwd=Path(hub_migrate.__file__).resolve().parents[1])
    assert run.returncode != 0 and "does not start here" in (run.stderr + run.stdout)


def test_export_refuses_while_the_service_runs_or_twice(hub):
    assert not hub_migrate.export(hub["config"], hub["source"], hub["dest"], "x", answering=lambda c: "aes-1")["ok"]
    assert not (hub["db"].parent / hub_migrate.MARKER).exists()
    assert hub_migrate.export(hub["config"], hub["source"], hub["dest"], "x", answering=quiet)["ok"]
    again = hub_migrate.export(hub["config"], hub["source"], hub["dest"] / "2", "x", answering=quiet)
    assert not again["ok"] and "already marked" in again["error"]


def test_rollback_removes_only_its_own_marker(hub):
    hub_migrate.export(hub["config"], hub["source"], hub["dest"], "x", answering=quiet)
    assert not hub_migrate.rollback(hub["config"], answering=lambda c: "aes-1")["ok"]          # new Arena still running
    out = hub_migrate.rollback(hub["config"], answering=quiet)
    assert out["ok"] and out["changed"] and not (hub["db"].parent / hub_migrate.MARKER).exists()
    # a marker from the original move to the hub is never removed by this tool
    (hub["db"].parent / hub_migrate.MARKER).write_text(json.dumps({"service_id": "s", "hub": "h", "moved_at": "t"}))
    assert not hub_migrate.rollback(hub["config"], answering=quiet)["ok"]


def test_status_is_read_only(hub):
    before = hub["db"].read_bytes()
    out = hub_migrate.status(hub["config"])
    assert out["service_id"] == hub["service_id"] and out["marker"] is None and hub["db"].read_bytes() == before


def test_cli_prints_one_json_object(hub):
    run = subprocess.run([sys.executable, "-m", "arena_service.hub_migrate", "status", "--config", str(hub["config"])], capture_output=True, text=True,
                         timeout=60, cwd=Path(hub_migrate.__file__).resolve().parents[1])
    assert run.returncode == 0 and json.loads(run.stdout)["service_id"] == hub["service_id"]
