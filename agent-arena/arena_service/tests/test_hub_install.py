"""The admin hub USB and its installer, with a stand-in for the Windows-only steps (firewall,
start at sign-in, shortcut, service start), so nothing here changes this computer."""
import json
import re
import sqlite3
from pathlib import Path

import pytest

from arena_service import hub_install, hub_usb
from arena_service.store import Store

ROOT = Path(__file__).resolve().parents[2]
URL = f"http://{hub_install.HUB_ADDRESS}:{hub_install.SERVICE_PORT}"


class FakeWindows:
    def __init__(self, data, addresses=(hub_install.HUB_ADDRESS,), busy=(), answer_as=None):
        self.data, self._addresses, self.busy, self.answer_as = Path(data), set(addresses), set(busy), answer_as
        self.done, self.startup, self.shortcuts, self.started, self.stopped = [], {}, [], None, 0

    def addresses(self):
        return self._addresses

    def port_free(self, host, port):
        return port not in self.busy

    def make_venv(self, folder):
        Path(folder).mkdir(parents=True)
        self.done.append("venv")

    def firewall(self, port, remote):
        self.done.append(("firewall", port, remote))
        return "added"

    def set_startup(self, command, name=hub_install.STARTUP_VALUE):
        self.startup[name] = command

    def shortcut(self, target, remove=False, name=hub_install.SHORTCUT, arguments="", icon=None):
        self.shortcuts.append(("remove" if remove else "create", name, target, arguments, icon))

    def start_admin(self, exe):
        self.started = exe

    def stop_hub(self, program, config):
        assert Path(config).name == "aes.config.json"
        self.stopped += 1
        return 3

    def health(self, url):
        if not self.started:
            raise OSError("not started")
        db = sqlite3.connect(self.data / "arena-events.db")
        service_id = json.loads(db.execute("SELECT value FROM meta WHERE key='service_id'").fetchone()[0])
        db.close()
        return {"service_id": self.answer_as or service_id, "version": "test"}


PACKAGE = ROOT / "dist" / f"Agent-Arena-{hub_usb.DESKTOP_VERSION}" / "app" / "AgentArena.exe"


@pytest.fixture
def small_frontend(monkeypatch):
    # hub_usb.prepare copies the admin window from the built desktop package (not kept in the repository).
    if not PACKAGE.is_file():
        pytest.skip(f"needs the built desktop package: desktop\\build\\Build-AgentArena.ps1 -Version {hub_usb.DESKTOP_VERSION}")
    monkeypatch.setattr(hub_usb, "FRONTEND", ["admin.html", "js/traffic_board.mjs", "assets/admin-traffic-board-v1"])


@pytest.fixture
def usb(tmp_path, small_frontend):
    drive = tmp_path / "usb"
    drive.mkdir()
    hub_usb.prepare(drive, say=lambda *_: None)
    return drive / hub_usb.FOLDER


def live_service(tmp_path):
    """The old service on .1: Ticketing with its producer credential and an enrolled head."""
    db = tmp_path / "live" / "arena-events.db"
    db.parent.mkdir()
    store = Store(str(db))
    store.add_application("capstone", "Ticketing")
    store.add_orchestrator("capstone", "main", "Ticketing coordinator")
    store.issue_producer("capstone", ["main"])
    store.add_display("head-192-0-2-3", "head", store.add_head("head-192-0-2-3", "Head 3", "192.0.2.3"), "192.0.2.3")
    store.issue_viewer("head-192-0-2-3", "m" * 32)
    service_id = store.meta("service_id")
    store.db.close()
    config = tmp_path / "live" / "aes.config.json"
    config.write_text(json.dumps({"host": "192.0.2.1", "port": 8790, "db_path": str(db),
                                  "public_endpoints": ["http://192.0.2.1:8790"]}), encoding="utf-8")
    return config, service_id


def run(usb, tmp_path, system=None, **kw):
    data = tmp_path / "AgentArenaService"
    system = system or FakeWindows(data)
    result = hub_install.install(usb, program=tmp_path / "Programs" / "AgentArenaHub", data=data, system=system,
                                 say=lambda *_: None, **kw)
    return result, system, data


def test_usb_carries_only_the_hub_and_its_manifest(usb):
    app = usb / "app"
    assert (app / "arena_service" / "hub_install.py").is_file() and (app / "frontend" / "admin.html").is_file()
    assert not list(app.rglob("tests")) and not list(app.rglob("__pycache__"))
    assert sorted(p.name for p in (app / "coordinator").iterdir()) == sorted(hub_usb.LAUNCHERS)
    assert {"INSTALL-ADMIN-HUB.cmd", "UNINSTALL-ADMIN-HUB.cmd", "README-HUB.txt"} <= {p.name for p in usb.iterdir()}
    manifest, problems = hub_install.verify_usb(usb)
    assert problems == [] and not manifest["moves_data"] and "app/arena_service/store.py" in manifest["files"]
    assert not (usb / "data").exists()


def test_admin_pages_load_only_what_the_usb_carries():
    web = ROOT / "frontend"
    wanted = set()
    for page in ("admin.html", "admin-room.html"):
        wanted |= {m.split("?")[0] for m in re.findall(r'(?:src|href)="([^"#:]+)"', (web / page).read_text(encoding="utf-8"))}
    for script in ("admin_console.mjs", "admin_room.mjs", "traffic_board.mjs"):
        text = (web / "js" / script).read_text(encoding="utf-8")
        wanted |= {"js/" + m[2:] for m in re.findall(r"from '(\./[^']+)'", text)}
        wanted |= {"assets/" + m for m in re.findall(r"at\('([a-z0-9_/-]+/)'", text)}
        wanted |= {"assets/" + m for m in re.findall(r"assets/([a-z0-9-]+/)", text)}
    assert wanted, "nothing found to check"
    missing = [w for w in sorted(wanted) if not any(w == f or w.startswith(f.rstrip("/") + "/") for f in hub_usb.FRONTEND)]
    assert not missing, missing
    assert all((web / f).exists() for f in hub_usb.FRONTEND)


def test_nothing_changes_without_the_hubs_address(usb, tmp_path):
    with pytest.raises(hub_install.InstallError, match="192.0.2.254"):
        run(usb, tmp_path, FakeWindows(tmp_path / "AgentArenaService", addresses={"192.0.2.1", "198.51.100.149"}))
    assert not (tmp_path / "Programs").exists() and not (tmp_path / "AgentArenaService").exists()


def test_a_changed_usb_or_a_busy_port_is_refused(usb, tmp_path):
    (usb / "app" / "arena_service" / "store.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(hub_install.InstallError, match="changed app/arena_service/store.py"):
        run(usb, tmp_path)
    (usb / "manifest.json").unlink()
    with pytest.raises(hub_install.InstallError, match="manifest"):
        run(usb, tmp_path, FakeWindows(tmp_path / "AgentArenaService", busy={8790}))
    assert not (tmp_path / "Programs").exists()


def test_fresh_hub(usb, tmp_path):
    result, system, data = run(usb, tmp_path)
    assert result["result"] == "success" and result["steps"]["data"]["mode"] == "fresh"
    app = tmp_path / "Programs" / "AgentArenaHub" / "app"
    assert (app / "arena_service" / "server.py").is_file() and (app / ".venv").is_dir()
    config = json.loads((data / "aes.config.json").read_text(encoding="utf-8"))
    assert config["host"] == "192.0.2.254" and config["port"] == 8790 and config["public_endpoints"] == [URL]
    store = Store(str(data / "arena-events.db"))
    assert store.meta("endpoints") == [URL]
    registry = store.registry()
    assert [a["app_id"] for a in registry["applications"]] == ["capstone"]
    assert {d["display_id"]: d["address"] for d in store.list_displays()} == {f"head-192-0-2-{n}": f"192.0.2.{n}" for n in range(2, 11)}
    assert ("firewall", 8790, "192.0.2.0/24") in system.done
    exe = app / "desktop" / "AgentArena.exe"
    assert exe.is_file() and not (app / "desktop" / "web").exists()          # the window only, not the heads' pages
    assert system.startup == {hub_install.OLD_STARTUP: None, hub_install.STARTUP_VALUE: f'"{exe}" --admin'}
    assert system.shortcuts[-1] == ("create", hub_install.SHORTCUT, str(exe), "--admin", str(exe))   # the Arena icon
    assert ("remove", hub_install.OLD_SHORTCUT, None, "", None) in system.shortcuts
    assert system.started == exe and system.stopped == 0                      # nothing was running on a first install


def test_reinstall_keeps_data_and_other_settings(usb, tmp_path):
    _, _, data = run(usb, tmp_path)
    config = json.loads((data / "aes.config.json").read_text(encoding="utf-8"))
    config["gateway"] = {"observation_file": "C:/gateway/observation.json"}
    (data / "aes.config.json").write_text(json.dumps(config), encoding="utf-8")
    store = Store(str(data / "arena-events.db"))
    store.add_head("head-192-0-2-11", "Head 11", "192.0.2.11")
    service_id = store.meta("service_id")
    store.db.close()
    result, system, _ = run(usb, tmp_path)
    assert system.stopped == 1                                                 # the running hub is stopped to update it
    assert result["steps"]["data"] == {"mode": "kept", "db": str(data / "arena-events.db"), "service_id": service_id}
    assert json.loads((data / "aes.config.json").read_text(encoding="utf-8"))["gateway"]["observation_file"]
    assert any(h["node_id"] == "head-192-0-2-11" for h in Store(str(data / "arena-events.db")).registry()["heads"])


def test_moved_data_keeps_identity_and_credentials(tmp_path, small_frontend):
    config, service_id = live_service(tmp_path)
    drive = tmp_path / "usb"
    drive.mkdir()
    manifest = hub_usb.prepare(drive, move_data_config=config, say=lambda *_: None)
    usb = drive / hub_usb.FOLDER
    assert manifest["moves_data"] and manifest["service_id"] == service_id
    result, _, data = run(usb, tmp_path)
    assert result["steps"]["data"]["mode"] == "moved" and result["steps"]["service"]["service_id"] == service_id
    hub, old = Store(str(data / "arena-events.db")), Store(json.loads(config.read_text())["db_path"])
    assert hub.list_credentials() == old.list_credentials()                 # heads and Ticketing keep working
    assert hub.meta("endpoints") == [URL]
    assert not (usb / "data" / "arena-events.db").exists()                 # credentials don't stay on the USB
    assert hub_install.verify_usb(usb)[1] == []


def test_moved_data_never_overwrites_a_hub_silently(tmp_path, small_frontend):
    config, service_id = live_service(tmp_path)
    drive = tmp_path / "usb"
    drive.mkdir()
    usb = hub_usb.FOLDER
    hub_usb.prepare(drive, move_data_config=config, say=lambda *_: None)
    data = tmp_path / "AgentArenaService"
    data.mkdir()
    other = Store(str(data / "arena-events.db"))
    other_id = other.meta("service_id")
    other.db.close()
    with pytest.raises(hub_install.InstallError, match="--replace-data"):
        run(drive / usb, tmp_path)
    assert not (tmp_path / "Programs").exists()
    result, _, _ = run(drive / usb, tmp_path, replace_data=True)
    assert result["steps"]["data"]["service_id"] == service_id
    backups = list((data / "backups").glob("arena-events-before-move-*.db"))
    assert len(backups) == 1 and Store(str(backups[0])).meta("service_id") == other_id


def test_the_service_must_answer_as_itself(usb, tmp_path):
    with pytest.raises(hub_install.InstallError, match="did not answer"):
        run(usb, tmp_path, FakeWindows(tmp_path / "AgentArenaService", answer_as="aes-someone-else"))


def test_uninstall_keeps_data(usb, tmp_path):
    _, system, data = run(usb, tmp_path)
    hub_install.uninstall(program=tmp_path / "Programs" / "AgentArenaHub", system=system, say=lambda *_: None)
    assert not (tmp_path / "Programs" / "AgentArenaHub" / "app").exists() and (data / "arena-events.db").exists()
    assert set(system.startup.values()) == {None} and system.shortcuts[-1][0] == "remove"


def test_prepare_leaves_other_folders_alone(tmp_path, small_frontend):
    drive = tmp_path / "usb"
    (drive / "AGENT-ARENA-2.1.1").mkdir(parents=True)
    (drive / hub_usb.FOLDER).mkdir()
    with pytest.raises(SystemExit):
        hub_usb.prepare(drive, say=lambda *_: None)                      # not ours: never replaced
    (drive / hub_usb.FOLDER).rmdir()
    hub_usb.prepare(drive, say=lambda *_: None)
    (drive / hub_usb.FOLDER / "results").mkdir()
    (drive / hub_usb.FOLDER / "results" / "earlier.json").write_text("{}", encoding="utf-8")
    hub_usb.prepare(drive, say=lambda *_: None)                          # ours: replaced, results kept
    assert (drive / "AGENT-ARENA-2.1.1").is_dir() and (drive / hub_usb.FOLDER / "results" / "earlier.json").exists()


def test_the_old_service_refuses_to_start_once_its_data_moved(tmp_path, small_frontend):
    import subprocess
    import sys
    config, service_id = live_service(tmp_path)
    drive = tmp_path / "usb"
    drive.mkdir()
    hub_usb.prepare(drive, move_data_config=config, say=lambda *_: None)
    marker = json.loads((tmp_path / "live" / "moved-to-hub.json").read_text(encoding="utf-8"))
    assert marker["service_id"] == service_id and marker["hub"] == URL
    run = subprocess.run([sys.executable, "-E", "-s", "-m", "arena_service", "--config", str(config)], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode != 0 and "moved to the hub" in run.stderr and service_id in run.stderr
