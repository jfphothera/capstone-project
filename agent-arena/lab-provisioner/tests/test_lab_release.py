"""Tests for the lab-provisioner release builder (no USB is touched)."""
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build" / "build_lab_release.py"
spec = importlib.util.spec_from_file_location("build_lab_release", BUILD)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    out = tmp_path_factory.mktemp("lab") / "release"
    manifest = builder.build(out, capstone=None, tests=None, say=lambda *_: None)
    return out, manifest


def test_manifest_lists_every_release_file_with_its_hash(release):
    out, manifest = release
    folder = out / builder.FOLDER
    on_disk = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and p != folder / "release-manifest.json"}
    assert on_disk == set(manifest["files"])
    for rel, digest in list(manifest["files"].items())[:50]:
        assert _sha(folder / rel) == digest


def test_sha256sums_covers_the_whole_usb_root(release):
    out, _ = release
    listed = {}
    for line in (out / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        h, rel = line.split("  ", 1)
        listed[rel] = h
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p != out / "SHA256SUMS.txt"}
    assert any(rel.endswith("Agent-Arena-2.3.0/SHA256SUMS.txt") for rel in files)   # nested sums files are covered too
    assert files == set(listed)
    assert listed[f"{builder.FOLDER}/release-manifest.json"] == _sha(out / builder.FOLDER / "release-manifest.json")


def test_no_secrets_no_moved_data_no_codes(release):
    out, manifest = release
    assert manifest["secrets_included"] is False
    assert builder.scan_for_secrets(out) == []
    payload = out / builder.FOLDER / "payload"
    assert not (payload / "AGENT-ARENA-HUB" / "data").exists()
    assert not list((payload / f"AGENT-ARENA-{builder.DESKTOP}" / "enroll").glob("*.code.txt"))
    hub = json.loads((payload / "AGENT-ARENA-HUB" / "manifest.json").read_text(encoding="utf-8"))
    assert hub["moves_data"] is False


def test_secret_scan_catches_codes_keys_and_credential_files(tmp_path):
    (tmp_path / "x.code.txt").write_text("ARENA-QWER-TYUP-ASDF-GHJK\r\n")
    (tmp_path / "notes.txt").write_text("use ARENA-QWER-TYUP-ASDF-GHJK once")
    (tmp_path / "k.json").write_text('{"secret": "abcdefghijklmnopqrstuvwxyz"}')
    (tmp_path / "ok.md").write_text("example ARENA-ABCD-EFGH-JKLM-NPQR")
    found = builder.scan_for_secrets(tmp_path)
    assert any("x.code.txt" in f for f in found) and any("notes.txt" in f for f in found) and any("k.json" in f for f in found)
    assert not any("ok.md" in f for f in found)


def test_viewer_toolset_is_the_2_1_2_layout_pointed_at_the_hub(release):
    out, manifest = release
    tool = out / builder.FOLDER / "payload" / f"AGENT-ARENA-{builder.DESKTOP}"
    profile = json.loads((tool / "tools" / "usb-profile.json").read_text(encoding="utf-8"))
    assert profile["service_id"] == "aes-a1e79a8b3902" and profile["version"] == builder.DESKTOP
    assert profile["event_service_endpoints"] == ["http://192.0.2.254:8790"]
    for name in ("2-INSTALL-AND-VERIFY.cmd", "tools/Install-And-Verify.ps1", "tools/HeadCommon.ps1"):
        assert (tool / name).is_file()
    assert (tool / "package" / f"Agent-Arena-{builder.DESKTOP}" / "app" / "AgentArena.exe").is_file()
    assert b"\r\n" in (tool / "2-INSTALL-AND-VERIFY.cmd").read_bytes()


def test_derived_engine_is_traceable_to_1_0_3(release):
    out, manifest = release
    derived = manifest["derived_files"]
    assert derived["head-engine/modules/Capstone.Core.psm1"]["changed"] is True
    assert derived["head-engine/modules/Capstone.Ollama.psm1"]["changed"] is True
    for unchanged in ("Capstone.Network.psm1", "Capstone.Models.psm1", "Capstone.Preflight.psm1"):
        assert derived[f"head-engine/modules/{unchanged}"]["changed"] is False
    assert derived["head-engine/catalog/model-catalog.json"]["changed"] is False
    diff = (out / builder.FOLDER / "docs" / "CHANGES-FROM-1.0.3.diff").read_text(encoding="utf-8")
    assert "LAB FIX" in diff and diff.count("--- a/provisioner/engine/modules/") == 2


GATEWAY_ZIP = builder.GATEWAY_DEFAULT


@pytest.mark.skipif(not GATEWAY_ZIP.is_file(), reason="Gateway release not on this machine")
def test_gateway_release_is_carried_byte_for_byte(tmp_path):
    out = tmp_path / "rel"
    manifest = builder.build(out, capstone=None, tests=None, gateway=GATEWAY_ZIP, say=lambda *_: None)
    pkg = out / builder.FOLDER / "packages" / "gateway"
    assert (pkg / GATEWAY_ZIP.name).read_bytes() == GATEWAY_ZIP.read_bytes()
    assert (pkg / (GATEWAY_ZIP.name + ".sha256")).read_bytes() == Path(str(GATEWAY_ZIP) + ".sha256").read_bytes()
    g = manifest["sources"]["inference_gateway"]
    assert g["status"] == "included unchanged" and g["release"] == "0.4.6" and g["files_verified"] >= 30
    assert g["sha256"] == _sha(GATEWAY_ZIP) and any(o.startswith("O3") for o in g["open_items"])
    # 1.6.29: the release's own hub lab tools, byte for byte, one click each from the USB root.
    import zipfile
    tools = out / builder.FOLDER / "engine" / "hub-tools" / "gateway-lab-tools"
    with zipfile.ZipFile(GATEWAY_ZIP) as z:
        members = [n for n in z.namelist() if "/lab-tools/" in n and not n.endswith("/")]
        assert members and all((tools / n.split("/lab-tools/", 1)[1]).read_bytes() == z.read(n) for n in members)
    for root_name, (rel, _) in builder.GATEWAY_LAB_TOOLS.items():
        stub = (out / root_name).read_text(encoding="utf-8")
        assert (tools / rel).is_file() and ("gateway-lab-tools\\" + rel.replace("/", "\\")) in stub, root_name
    assert sorted(g["lab_tools"]) == sorted(n.split("/lab-tools/", 1)[1] for n in members)


@pytest.mark.skipif(not GATEWAY_ZIP.is_file(), reason="Gateway release not on this machine")
def test_a_gateway_archive_that_does_not_match_its_sha256_is_refused(tmp_path):
    bad = tmp_path / GATEWAY_ZIP.name
    bad.write_bytes(GATEWAY_ZIP.read_bytes() + b"x")
    (tmp_path / (GATEWAY_ZIP.name + ".sha256")).write_bytes(Path(str(GATEWAY_ZIP) + ".sha256").read_bytes())
    with pytest.raises(SystemExit, match="published SHA-256"):
        builder.check_gateway_release(bad)


def test_runtime_installers_are_checked_against_published_hashes(tmp_path):
    cache = builder.REPO / "dist" / "_runtime-cache"
    config = json.loads((builder.LAB / "config" / "lab-network.json").read_text(encoding="utf-8"))
    if not (cache / config["runtime"]["python"]["file"]).is_file():
        pytest.skip("runtime installers not cached here")
    infos = builder.check_runtime(cache, config)
    assert [i["file"] for _, i in infos] == ["python-3.12.10-amd64.exe", "PowerShell-7.4.22-win-x64.msi", "vc_redist.x64.exe"]
    fake = tmp_path / "rt"
    fake.mkdir()
    for name in (config["runtime"]["python"]["file"], config["runtime"]["pwsh"]["file"]):
        (fake / name).write_bytes(b"not the installer")
    with pytest.raises(SystemExit, match="MD5"):
        builder.check_runtime(fake, config)


def test_root_launcher_and_roles(release):
    out, manifest = release
    assert builder.FOLDER in (out / "START-LAB-PROVISIONER.cmd").read_text(encoding="utf-8")
    assert [r["id"] for r in manifest["supported_roles"]] == ["cpu-head", "gpu-head", "hub", "capstone", "coder", "existing"]
    assert manifest["sources"]["inference_gateway"]["status"].startswith("not supplied")
    assert manifest["sources"]["coder"]["status"].startswith("not supplied")
    assert re.match(r"^agent-arena-lab-\d+\.\d+\.\d+\+", manifest["release_id"])


def test_only_the_approved_capstone_release_is_packaged(tmp_path):
    config = json.loads((BUILD.parents[1] / "config" / "lab-network.json").read_text(encoding="utf-8"))
    approved = config["capstone"]["approved_release"]
    # The release approved since stick 1.5.9 (2026-11-09).
    assert approved["file"] == "Capstone-6.0.0-rc.29-windows-x64.zip"
    assert approved["sha256"] == "1b2fe0b422e786a8fbc9b7d2582c12224cafe37814701a250f7cd461c05fb2c4"
    assert builder.CAPSTONE_DEFAULT.name == approved["file"]
    for name in (approved["file"], "Capstone-6.0.0-rc.7-windows-x64.zip"):
        fake = tmp_path / name
        fake.write_bytes(b"not the released archive")
        with pytest.raises(SystemExit, match="not the approved Capstone release"):
            builder.build(tmp_path / ("out-" + name), capstone=fake, tests=None, say=lambda *_: None)

def test_capstone_page_scripts_parse_as_es_modules(tmp_path):
    """Every browser script in the approved Capstone package must parse as an ES module, the way its pages load it.
    rc.10-rc.13 shipped testing_lab.js with `async function renderResults` declared twice: a classic-script check
    passes, but the browser rejects the module and the Testing Lab never starts (DESKTOP-REDACT11, 2026-11-14)."""
    import json as _json, shutil as _shutil, subprocess as _sp, zipfile as _zip
    node = _shutil.which("node")
    if not node:
        pytest.skip("node is not installed on this machine")
    root = Path(__file__).resolve().parents[1]
    cfg = _json.loads((root / "config" / "lab-network.json").read_text(encoding="utf-8-sig"))
    pkg = Path(builder.CAPSTONE_DEFAULT).with_name(cfg["capstone"]["approved_release"]["file"])
    if not pkg.exists():
        pytest.skip("the approved Capstone package is not on this machine")
    z = _zip.ZipFile(pkg)
    scripts = [n for n in z.namelist() if n.replace("\\", "/").startswith("payload/application/src/capstone_rag/webapp/static/js/") and n.endswith(".js")]
    assert scripts, "no page scripts found in the package"
    bad = []
    for n in scripts:
        p = _sp.run([node, "--input-type=module", "--check"], input=z.read(n), capture_output=True)
        if p.returncode:
            bad.append("%s: %s" % (n.rsplit("/", 1)[-1], p.stderr.decode("utf-8", "replace").strip().splitlines()[:4]))
    assert not bad, bad
