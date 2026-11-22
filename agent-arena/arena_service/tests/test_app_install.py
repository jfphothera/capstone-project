"""The Ticketing adapter installer against a real event service: the app code is redeemed over
signed HTTP and the credential lands in a DPAPI file. Windows-only steps are stood in for."""
import json
from pathlib import Path

import pytest

from arena_service import app_install, hub_usb, secretfile, server
from arena_service.hub_install import InstallError, Windows


class FakeLaptop:
    def __init__(self, addresses=("192.0.2.200",), capstone_up=False):
        self._addresses, self.capstone_up = set(addresses), capstone_up
        self.startup, self.started, self.venvs, self.stopped = {}, [], [], []

    def addresses(self):
        return self._addresses

    def make_venv(self, folder):
        Path(folder).mkdir(parents=True)
        self.venvs.append(folder)

    def set_startup(self, command, name=None):
        self.startup[name] = command

    def stop_hub(self, program, config):
        self.stopped.append((Path(program), Path(config)))
        return 2

    def start_service(self, launcher, title=None):
        self.started.append((launcher, title))

    def health(self, url, path="/v2/health"):
        if path == "/api/health":
            if not self.capstone_up:
                raise OSError("capstone down")
            return {"ok": True}
        return Windows().health(url, path)                        # the real hub under test


@pytest.fixture
def usb(tmp_path):
    drive = tmp_path / "usb"
    drive.mkdir()
    hub_usb.prepare_ticketing(drive, say=lambda *_: None)
    return drive / hub_usb.TICKETING_FOLDER


@pytest.fixture
def capstone_db(tmp_path):
    db = tmp_path / "Capstone" / "data" / "operations.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    return db


def run(usb, tmp_path, env, laptop, capstone_db, code=None):
    return app_install.install(usb, program=tmp_path / "Programs" / "AgentArenaAdapter", data=tmp_path / "data",
                               system=laptop, capstone_db=capstone_db, code=code, hub=env.url, say=lambda *_: None)


def test_usb_carries_the_adapter_and_no_credential(usb):
    assert (usb / "app" / "arena_service" / "adapters" / "capstone" / "runner.py").is_file()
    assert (usb / "app" / "coordinator" / "START-TICKETING-ADAPTER.cmd").is_file()
    assert (usb / "INSTALL-TICKETING-ADAPTER.cmd").is_file()
    assert not any(p.suffix == ".dpapi" for p in usb.rglob("*"))


def test_nothing_changes_without_an_app_address_or_capstone(usb, tmp_path, env, capstone_db):
    env.app("capstone", "Ticketing")
    with pytest.raises(InstallError) as refused:
        run(usb, tmp_path, env, FakeLaptop(addresses={"192.0.2.5"}), tmp_path / "missing.db")
    text = str(refused.value)
    assert "application address" in text and "Capstone's database" in text and "app code is needed" in text
    assert not (tmp_path / "Programs").exists() and not (tmp_path / "data").exists()


def test_install_redeems_the_app_code_from_the_claimed_laptop(usb, tmp_path, env, capstone_db, monkeypatch):
    env.app("capstone", "Ticketing")
    env.store.reserve_address("192.0.2.200", "app", app_id="capstone", orchestrator_id="main")
    env.store.consume_claim(env.store.claim_for("192.0.2.200")["credential_id"], "CAPSTONE-LAPTOP", "c" * 32)
    code = env.store.issue_app_code("capstone", "main")
    monkeypatch.setattr(server.Handler, "_peer", lambda self: "192.0.2.200")
    laptop = FakeLaptop(capstone_up=True)
    result = run(usb, tmp_path, env, laptop, capstone_db, code=code)
    assert result["result"] == "success" and result["address"] == "192.0.2.200"
    assert result["steps"]["capstone"] == "answering"
    credential_id, secret = secretfile.read(str(tmp_path / "data" / "ticketing-producer.dpapi"))
    assert credential_id.startswith("prd-") and secret not in json.dumps(result)
    config = json.loads((tmp_path / "data" / "ticketing-adapter.json").read_text(encoding="utf-8"))
    assert config["aes_url"] == env.url and config["capstone_db"] == str(capstone_db) and config["start_from"] == "now"
    app = tmp_path / "Programs" / "AgentArenaAdapter" / "app"
    assert str(app / "coordinator" / "START-TICKETING-ADAPTER.cmd") in laptop.startup[app_install.STARTUP_VALUE]
    assert laptop.started and (app / "arena_service" / "app_install.py").is_file()

    assert not laptop.stopped                                         # a first install has nothing to stop
    update = FakeLaptop()
    again = run(usb, tmp_path, env, update, capstone_db)            # update: no code, credential kept
    # Arena 2.5.0: the running adapter is stopped before its files are replaced (only its own folder and configuration).
    assert update.stopped == [(tmp_path / "Programs" / "AgentArenaAdapter", tmp_path / "data" / "ticketing-adapter.json")] and again["steps"]["stopped"] == 2
    assert again["steps"]["credential"].startswith("kept") and again["steps"]["capstone"] == "not answering yet"
    assert secretfile.read(str(tmp_path / "data" / "ticketing-producer.dpapi"))[0] == credential_id


def test_the_installed_capstone_database_is_found_without_arguments(tmp_path):
    """Arena 2.5.1: the lab laptop's Capstone runs as a service with its data in ProgramData (2026-11-18 the direct
    run looked only in the developer checkout and failed)."""
    program_data, profile = tmp_path / "ProgramData", tmp_path / "Users" / "LabOperator2"
    env = {"ProgramData": str(program_data), "USERPROFILE": str(profile)}
    assert app_install.default_capstone_db(env) == str(program_data / "Capstone" / "data" / "operations.db")   # neither: the service path
    dev = profile / "PycharmProjects" / "Capstone" / "data" / "operations.db"
    dev.parent.mkdir(parents=True); dev.write_bytes(b"")
    assert app_install.default_capstone_db(env) == str(dev)                                              # only a checkout
    live = program_data / "Capstone" / "data" / "operations.db"
    live.parent.mkdir(parents=True); live.write_bytes(b"")
    assert app_install.default_capstone_db(env) == str(live)                                             # the service wins


def test_a_used_code_is_refused_after_the_program_is_in_place(usb, tmp_path, env, capstone_db):
    env.app("capstone", "Ticketing")
    code = env.store.issue_app_code("capstone", "main")
    run(usb, tmp_path, env, FakeLaptop(), capstone_db, code=code)
    (tmp_path / "data" / "ticketing-producer.dpapi").unlink()
    with pytest.raises(InstallError, match="refused the app code"):
        run(usb, tmp_path, env, FakeLaptop(), capstone_db, code=code)
