"""Real-Windows workstation tests of the built Agent Arena package.

Runs the built AgentArena.exe (WebView2, DPAPI, WinForms) against a live Arena
Event Service process on 127.0.0.1, using a throwaway --profile-root so the
real user profile, real sign-in startup and real scheduled tasks are never
touched. This is workstation evidence on DEVPC-REDACTED, NOT head evidence.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from arena_service.store import Store  # noqa: E402

PKG = Path(os.environ.get("AGENT_ARENA_TEST_PACKAGE", str(ROOT / "dist" / "Agent-Arena-2.1.0" / "app")))
PKG_VERSION = json.loads((PKG / "app-manifest.json").read_text(encoding="utf-8-sig"))["version"] if (PKG / "app-manifest.json").exists() else "?"
OLD_PKG = ROOT / "dist" / "Agent-Arena-2.0.0" / "app"
LEGACY_ZIP = ROOT / "docs" / "history" / "phase4_2-repair" / "original" / "Capstone-Arena-Viewer-ORIGINAL.zip"
PORT = 18890
DISPLAY = "head-192-0-2-1"         # test-only: this workstation's LAN address treated as a head
EVIDENCE = Path(os.environ.get("AGENT_ARENA_TEST_EVIDENCE", str(ROOT / "docs" / "evidence")))
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not (PKG / "AgentArena.exe").exists(), reason="needs Windows and a built package")


def run(*args, timeout=120):
    return subprocess.run([str(args[0]), *map(str, args[1:])], capture_output=True, text=True, timeout=timeout)


class World:
    def __init__(self, tmp):
        self.tmp, self.profile = tmp, tmp / "profile"
        self.profile.mkdir()
        self.deploy = tmp / "deploy.json"
        self.deploy.write_text(json.dumps({"config_version": "test", "event_service_endpoints": [f"http://127.0.0.1:{PORT}"],
                                           "display_networks": ["192.0.2.0/24"], "exclude_addresses": [], "orchestrator_displays": {}}))
        self.config = tmp / "aes.json"
        self.config.write_text(json.dumps({"host": "127.0.0.1", "port": PORT, "db_path": str(tmp / "aes.db"),
                                           "log_path": str(tmp / "aes.log")}))
        store = Store(str(tmp / "aes.db"))
        store.add_application("capstone", "Ticketing")
        store.add_orchestrator("capstone", "main", "Ticketing coordinator")
        location = store.add_head(DISPLAY, "Test head (workstation)")
        store.add_display(DISPLAY, "head", location, "127.0.0.1")
        self.store = store
        self.service = None
        self.start_service()

    def start_service(self):
        self.service = subprocess.Popen([sys.executable, "-E", "-s", "-m", "arena_service", "--config", str(self.config)], cwd=ROOT)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/v2/health", timeout=1)
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("service did not start")

    def stop_service(self):
        self.service.kill()
        self.service.wait()

    def exe(self, *args, installed=False, timeout=120):
        base = self.installed_dir() if installed else PKG
        return run(base / "AgentArena.exe", *args, "--profile-root", self.profile, "--deployment", self.deploy, timeout=timeout)

    def installed_dir(self):
        pointer = self.profile / "Programs" / "AgentArena" / "current.txt"
        return self.profile / "Programs" / "AgentArena" / pointer.read_text().strip()

    def state(self):
        return self.exe("--state").stdout

    def code(self):
        return self.store.issue_enrollment(DISPLAY)

    def service_log(self):
        return (self.tmp / "aes.log").read_text(encoding="utf-8") if (self.tmp / "aes.log").exists() else ""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("arena-desktop"))
    yield w
    subprocess.run(["taskkill", "/F", "/IM", "AgentArena.exe", "/FI", f"WINDOWTITLE eq Agent Arena*"], capture_output=True)
    w.stop_service()
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\AgentArenaTest\Run")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\AgentArenaTest")
    except OSError:
        pass


def plant_legacy(profile):
    """Recreate a Head 3-style partial install of the original viewer, plus unrelated files."""
    legacy = profile / "Capstone-Arena-Viewer"
    legacy.mkdir()
    with zipfile.ZipFile(LEGACY_ZIP) as z:
        for name in ("Viewer.ps1", "Uninstall.ps1", "Metrics.ps1", "Diagnostics.ps1", "README.md", "VERIFY-ON-EACH-HEAD.md"):
            (legacy / name).write_bytes(z.read(name))
    (legacy / "config.json").write_text('{"coordinator":"http://192.0.2.1:8000","head":"head-192-0-2-3"}')
    (legacy / "prior-power-scheme.txt").write_text("Power Scheme GUID: test")
    (legacy / "browser-profile").mkdir()
    (legacy / "browser-profile" / "Cookies").write_text("session")
    (legacy / "my-notes.txt").write_text("operator's own file")
    (profile / "Startup").mkdir()
    (profile / "Startup" / "Capstone Arena.lnk").write_bytes("C:\\x\\Capstone-Arena-Viewer\\Viewer.ps1".encode("utf-16-le"))
    (profile / "unrelated.txt").write_text("keep me")


def ollama_pid():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ollama.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return sorted(line.split(",")[1].strip('"') for line in out.splitlines() if line.startswith('"ollama'))


def window_titles():
    titles = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def collect(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.startswith("Agent Arena") and ctypes.windll.user32.IsWindowVisible(hwnd):
            titles.append((hwnd, buf.value))
        return True
    ctypes.windll.user32.EnumWindows(proc(collect), 0)
    return titles


def capture(hwnd, name):
    """Evidence image of the Arena window ONLY (PrintWindow renders the window's own
    content; other windows on the desktop are never captured)."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE / name
    ps = ("Add-Type -AssemblyName System.Drawing; Add-Type 'using System;using System.Runtime.InteropServices;public static class PW{"
          "[DllImport(\"user32.dll\")]public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint f);"
          "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out R r);public struct R{public int L,T,Rt,B;}}';"
          f"$h=[IntPtr]{int(hwnd)};$r=New-Object PW+R;[void][PW]::GetWindowRect($h,[ref]$r);"
          "$b=New-Object Drawing.Bitmap ($r.Rt-$r.L),($r.B-$r.T);$g=[Drawing.Graphics]::FromImage($b);$dc=$g.GetHdc();"
          f"[void][PW]::PrintWindow($h,$dc,2);$g.ReleaseHdc($dc);$b.Save('{target}')")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return target


# ------------------------------------------------------------------ the sequence (ordered)
def test_01_diagnostics_and_state_before_install(world):
    assert "not_installed" in world.state()
    report = world.tmp / "diag-before.txt"
    assert world.exe("--diagnostics-report", report).returncode == 0
    text = report.read_text(encoding="utf-8")
    assert "[FAIL] Installation" in text and "not installed" in text
    assert f"{DISPLAY} (head)" in text and "[PASS] Arena event API" in text
    assert "secret" not in text.lower().replace("secrets are never included", "")


def test_02_detects_head3_style_partial_legacy_install(world):
    plant_legacy(world.profile)
    state = world.state()
    assert "legacy_phase42_viewer_present" in state and "startup-shortcut" in state and "file" in state


def test_03_fresh_install_enrolls_and_retires_only_owned_legacy_items(world):
    before = ollama_pid()
    r = world.exe("--install-quiet", "--code", world.code())
    assert "RESULT success" in r.stdout, r.stdout
    assert "Enrolled: credential vwr-" in r.stdout
    legacy = world.profile / "Capstone-Arena-Viewer"
    assert sorted(p.name for p in legacy.iterdir()) == ["browser-profile", "my-notes.txt"]    # owned files gone, user file + data kept
    assert not (world.profile / "Startup" / "Capstone Arena.lnk").exists()
    assert (world.profile / "unrelated.txt").read_text() == "keep me"
    state = world.state()
    assert "installed" in state and "legacy_phase42_viewer_present" not in state and "legacy_viewer_data_retained" in state
    assert (world.profile / "Desktop" / "Agent Arena.lnk").exists()
    result = json.loads((world.profile / "AgentArena" / "last-setup-result.json").read_text(encoding="utf-8-sig"))
    assert result["action"] == "install" and result["ok"] is True and result["enrolled"] is True
    report = world.tmp / "diag-after.txt"
    world.exe("--diagnostics-report", report, installed=True)
    text = report.read_text(encoding="utf-8")
    assert "Windows NT 10.0" in text and "6.2.9200" not in text and "scaling" in text
    assert ollama_pid() == before


def test_04_repeated_install_keeps_credential_and_one_startup_entry(world):
    r = world.exe("--install-quiet")
    assert "RESULT success" in r.stdout and "Keeping the existing viewer credential" in r.stdout
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\AgentArenaTest\Run") as k:
        values = [winreg.EnumValue(k, i) for i in range(winreg.QueryInfoKey(k)[1])]
    assert len(values) == 1 and str(world.installed_dir()) in values[0][1]


def test_05_corrupt_missing_and_interrupted_states_are_detected_and_repaired(world):
    config = world.profile / "AgentArena" / "config.json"
    good = config.read_text()
    config.write_text("{not json")
    assert "misconfigured" in world.state()
    assert "RESULT success" in world.exe("--install-quiet").stdout
    config.unlink()
    assert "partial" in world.state()
    assert "RESULT success" in world.exe("--install-quiet").stdout
    (world.profile / "Programs" / "AgentArena" / "2.0.0.partial").mkdir()
    assert "partial" in world.state()
    assert "RESULT success" in world.exe("--install-quiet").stdout
    assert not (world.profile / "Programs" / "AgentArena" / "2.0.0.partial").exists()
    assert json.loads(config.read_text())["display_id"] == json.loads(good)["display_id"]


def test_06_tampered_frontend_is_damaged_until_repaired(world):
    page = world.installed_dir() / "web" / "js" / "agent_arena_live.js"
    page.write_text(page.read_text() + "\n// tampered")
    assert world.exe("--self-test", installed=True).returncode == 4
    assert "damaged" in world.state()
    assert "RESULT success" in world.exe("--install-quiet").stdout
    assert world.exe("--self-test", installed=True).returncode == 0


def test_07_viewer_window_single_instance_renders_and_reconnects(world):
    exe = world.installed_dir() / "AgentArena.exe"
    args = [str(exe), "--profile-root", str(world.profile)]
    first = subprocess.Popen(args)
    deadline = time.time() + 30
    while time.time() < deadline and not window_titles():
        time.sleep(0.5)
    wins = window_titles()
    assert wins and DISPLAY in wins[0][1]
    user32 = ctypes.windll.user32
    user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    assert user32.GetAwarenessFromDpiAwarenessContext(user32.GetWindowDpiAwarenessContext(ctypes.c_void_p(wins[0][0]))) == 2   # per-monitor aware
    time.sleep(6)
    assert "/v2/snapshot" in world.service_log()           # the desktop bridge reached the service with a signed request
    capture(wins[0][0], "desktop-live-workstation.png")
    second = subprocess.run(args, timeout=30)               # duplicate launch hands off and exits
    assert second.returncode == 0 and first.poll() is None and len(window_titles()) == 1
    world.stop_service()
    time.sleep(8)
    capture(wins[0][0], "desktop-disconnected-workstation.png")
    mark = len(world.service_log())
    world.start_service()
    time.sleep(14)
    assert "/v2/snapshot" in world.service_log()[mark:]     # reconciled with a fresh snapshot after the outage
    capture(wins[0][0], "desktop-reconnected-workstation.png")
    assert first.poll() is None


def test_08_uninstall_stops_viewer_and_keeps_data_by_default(world):
    before = ollama_pid()
    r = world.exe("--uninstall-quiet")
    assert "RESULT success" in r.stdout, r.stdout
    time.sleep(2)
    assert not window_titles()
    assert not (world.profile / "Programs" / "AgentArena" / "current.txt").exists()
    assert not (world.profile / "Desktop" / "Agent Arena.lnk").exists()
    assert (world.profile / "AgentArena" / "credential.bin").exists()
    assert "not_installed" in world.state()
    assert ollama_pid() == before


def test_09_reinstall_reuses_credential_then_full_removal(world):
    r = world.exe("--install-quiet")
    assert "RESULT success" in r.stdout and "Keeping the existing viewer credential" in r.stdout
    assert "installed" in world.state()
    r = world.exe("--uninstall-quiet", "--remove-data")
    assert "RESULT success" in r.stdout
    assert not (world.profile / "AgentArena" / "credential.bin").exists()
    assert (world.profile / "unrelated.txt").read_text() == "keep me"


def test_10_unrecognised_machine_is_refused_without_guessing(world, tmp_path):
    other = tmp_path / "deploy-none.json"
    other.write_text(json.dumps({"config_version": "t", "event_service_endpoints": [f"http://127.0.0.1:{PORT}"],
                                 "display_networks": ["10.99.0.0/24"], "exclude_addresses": [], "orchestrator_displays": {}}))
    r = run(PKG / "AgentArena.exe", "--install-quiet", "--code", "ARENA-AAAA-BBBB-CCCC-DDDD",
            "--profile-root", world.tmp / "profile2", "--deployment", other)
    assert "RESULT failed" in r.stdout and "no authorized display identity" in r.stdout
    assert not (world.tmp / "profile2" / "Programs").exists()


@pytest.mark.skipif(not (OLD_PKG / "AgentArena.exe").exists(), reason="2.0.0 package not present")
def test_11_upgrade_from_2_0_0_keeps_enrollment_and_one_startup_entry(world, tmp_path):
    profile = tmp_path / "upgrade-profile"
    base = ["--profile-root", profile, "--deployment", world.deploy]
    r = run(OLD_PKG / "AgentArena.exe", "--install-quiet", "--code", world.code(), *base)
    assert "RESULT success" in r.stdout, r.stdout
    assert (profile / "Programs" / "AgentArena" / "current.txt").read_text().strip() == "2.0.0"
    r = run(PKG / "AgentArena.exe", "--install-quiet", *base)
    assert "RESULT success" in r.stdout and "Keeping the existing viewer credential" in r.stdout, r.stdout
    assert (profile / "Programs" / "AgentArena" / "current.txt").read_text().strip() == PKG_VERSION
    assert sorted(p.name for p in (profile / "Programs" / "AgentArena").iterdir() if p.is_dir()) == ["2.0.0", PKG_VERSION]   # one previous kept
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\AgentArenaTest\Run") as k:
        values = [winreg.EnumValue(k, i) for i in range(winreg.QueryInfoKey(k)[1])]
    assert len(values) == 1 and f"\\{PKG_VERSION}\\AgentArena.exe" in values[0][1]
    assert "installed" in run(PKG / "AgentArena.exe", "--state", *base).stdout
    assert "RESULT success" in run(PKG / "AgentArena.exe", "--uninstall-quiet", "--remove-data", *base).stdout


def lan_address():
    import socket
    return next((a for a in socket.gethostbyname_ex(socket.gethostname())[2] if a.startswith("192.0.2.")), None)


@pytest.mark.skipif(lan_address() is None, reason="needs a 192.0.2.x address on this workstation")
def test_12_registry_identifies_an_orchestrator_display_at_any_address(tmp_path):
    """No package edit: the service registry says this address is an orchestrator display."""
    lan, port = lan_address(), PORT + 1
    store = Store(str(tmp_path / "aes.db"))
    store.add_application("coder", "Coder")
    store.add_orchestrator("coder", "main", "Coder workstation")
    store.add_display("orch-coder-main-test", "orchestrator", "orch:coder/main", lan)
    config = tmp_path / "aes.json"
    config.write_text(json.dumps({"host": lan, "port": port, "db_path": str(tmp_path / "aes.db"), "log_path": str(tmp_path / "aes.log")}))
    deploy = tmp_path / "deploy.json"         # no display network and no orchestrator mapping covers this address
    deploy.write_text(json.dumps({"config_version": "t", "event_service_endpoints": [f"http://{lan}:{port}"],
                                  "display_networks": ["10.99.0.0/24"], "exclude_addresses": [], "orchestrator_displays": {}}))
    service = subprocess.Popen([sys.executable, "-E", "-s", "-m", "arena_service", "--config", str(config)], cwd=ROOT)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://{lan}:{port}/v2/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        base = ["--profile-root", tmp_path / "profile", "--deployment", deploy]
        report = tmp_path / "diag.txt"
        run(PKG / "AgentArena.exe", "--diagnostics-report", report, *base)
        text = report.read_text(encoding="utf-8")
        assert "[PASS] Detected identity" in text and "orch-coder-main-test (orchestrator)" in text and "registered at the event service" in text
        used = store.issue_enrollment("orch-coder-main-test")
        r = run(PKG / "AgentArena.exe", "--install-quiet", "--code", used, "--no-startup", "--no-desktop-shortcut", *base)
        assert "RESULT success" in r.stdout and "Location orch:coder/main" in r.stdout, r.stdout
        saved = json.loads((tmp_path / "profile" / "AgentArena" / "config.json").read_text(encoding="utf-8-sig"))
        assert saved["role"] == "orchestrator" and saved["display_id"] == "orch-coder-main-test"
        if PKG_VERSION not in ("2.0.2", "?"):          # D2 diagnosis: enrollment always records where its time went
            log = (tmp_path / "profile" / "AgentArena" / "logs" / "agent-arena.log").read_text(encoding="utf-8")
            assert "request timing POST /v2/enroll: HTTP 200 after" in log and "new connection started at" in log, log
        r = run(PKG / "AgentArena.exe", "--install-quiet", "--code", used, "--no-startup", "--no-desktop-shortcut", *base)
        assert "ENROLLMENT FAILED (code_refused)" in r.stdout and "already used" in r.stdout, r.stdout     # field defect D3
        assert "RESULT success" in run(PKG / "AgentArena.exe", "--uninstall-quiet", "--remove-data", *base).stdout
    finally:
        service.kill()
        service.wait()
