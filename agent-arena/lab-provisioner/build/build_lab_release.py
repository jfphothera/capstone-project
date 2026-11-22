"""Build the Agent Arena Lab Provisioner release (dist/lab-provisioner-<version>/), ready to copy to a USB.

    python lab-provisioner/build/build_lab_release.py [--capstone-package ZIP | --no-capstone] [--tests RESULTS.json]

The release root is laid out exactly as the USB will be:

    START-LAB-PROVISIONER.cmd, COLLECT-LAB-EVIDENCE.cmd, VERIFY-USB.cmd, OPERATOR-GUIDE.txt, FIELD-ACCEPTANCE-CHECKLIST.txt, SHA256SUMS.txt
    AGENT-ARENA-LAB-<version>/   engine, config, tests, docs, payload (Arena toolsets), packages (other projects)

Payloads are built by the existing builders, unchanged in behaviour:
  * the head viewer toolset as desktop/usb/New-ArenaUsb.ps1 lays it out, but with no enrollment codes;
  * AGENT-ARENA-HUB and AGENT-ARENA-TICKETING by arena_service.hub_usb, without moved data.
Nothing secret is shipped: the build fails if a credential, code, key or service database is found.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
REPO = LAB.parent
sys.path.insert(0, str(REPO))
from arena_service import __version__ as SERVICE_VERSION  # noqa: E402
from arena_service import hub_usb  # noqa: E402

VERSION = (LAB / "VERSION").read_text(encoding="utf-8").strip()
FOLDER = f"AGENT-ARENA-LAB-{VERSION}"
DESKTOP = hub_usb.DESKTOP_VERSION
CAPSTONE_DEFAULT = REPO.parent / "Capstone" / "deploy" / "windows" / "dist" / "Capstone-6.0.0-rc.29-windows-x64.zip"
SERVICES_DEFAULT = REPO.parent / "Capstone" / "deploy" / "services" / "dist"   # CapstoneQMS / CapstoneERP (the approved rc.4, by name)
GATEWAY_DEFAULT = REPO.parent / "Inference Gateway" / "release" / "inference-gateway-0.4.6.zip"
ORIGINAL_103 = REPO / "provisioner"
ORIGINAL_ZIP = ORIGINAL_103 / "original" / "Capstone-Head-Provisioner-v1.0.3.zip"
FIX_SNAPSHOT = ORIGINAL_103 / "original" / "FIX-EXISTING-HEADS-2026-11-04"
DERIVED = ["Capstone.Core.psm1", "Capstone.Network.psm1", "Capstone.Ollama.psm1", "Capstone.Models.psm1", "Capstone.Preflight.psm1"]
ROLES = [
    {"id": "cpu-head", "name": "New CPU inference head", "address": "192.0.2.2-.199 (not protected addresses)"},
    {"id": "gpu-head", "name": "New GPU inference head", "address": "192.0.2.2-.199; NVIDIA GPU with 8 GB+ VRAM"},
    {"id": "hub", "name": "Administration hub (Agent Arena; Inference Gateway pending)", "address": "192.0.2.254"},
    {"id": "capstone", "name": "Capstone application laptop", "address": "192.0.2.201"},
    {"id": "coder", "name": "Coder application laptop (installation pending)", "address": "192.0.2.202"},
    {"id": "existing", "name": "Existing machine: inspect, verify, repair, update, roll back", "address": "as found"},
]
SECRET_NAMES = re.compile(r"(?i)(\.dpapi$|\.code\.txt$|^credential\.bin$|\.pfx$|\.p12$|\.key$|^aes\.config\.json$|^arena-events\.db|^moved-to-hub\.json$|developer\.token$|\.token$)")
SECRET_TEXT = [re.compile(r"ARENA-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}"),
               re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"), re.compile(r'"sealed_secret"\s*:\s*"[A-Za-z0-9_\-]{20,}"'),
               re.compile(r'"secret"\s*:\s*"[A-Za-z0-9_\-]{20,}"')]
TEXT_SUFFIXES = {".ps1", ".psm1", ".cmd", ".txt", ".json", ".md", ".py", ".html", ".mjs", ".js", ".css", ".xml", ".config", ".diff"}
EXAMPLE_CODES = {"ARENA-ABCD-EFGH-JKLM-NPQR", "ARENA-XXXX-XXXX-XXXX-XXXX"}   # documented examples and placeholders, not real codes


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text(path: Path, text: str, crlf: bool | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if crlf is None:
        crlf = path.suffix.lower() in (".cmd", ".txt")
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))


def copy_tree(source: Path, target: Path, *, skip=("__pycache__", "*.pyc")) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*skip))
    for p in target.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".cmd", ".txt"):
            write_text(p, p.read_text(encoding="utf-8"), crlf=True)


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def build_viewer_toolset(payload: Path, service_id: str) -> dict:
    """AGENT-ARENA-<desktop>: what desktop/usb/New-ArenaUsb.ps1 copies, minus enrollment codes."""
    pkg_name = f"Agent-Arena-{DESKTOP}"
    pkg = REPO / "dist" / pkg_name
    sums = (pkg / "SHA256SUMS.txt").read_text(encoding="utf-8-sig").splitlines()
    for line in sums:
        m = re.match(r"^([0-9A-F]{64})\s+(.+)$", line)
        if m and sha256(pkg / m.group(2)).upper() != m.group(1):
            raise SystemExit(f"dist package damaged: {m.group(2)}")
    manifest = json.loads((pkg / "app" / "app-manifest.json").read_text(encoding="utf-8-sig"))
    if manifest["version"] != DESKTOP:
        raise SystemExit(f"dist/{pkg_name} is version {manifest['version']}")
    deploy = json.loads((pkg / "app" / "arena-deployment.json").read_text(encoding="utf-8-sig"))
    usb = REPO / "desktop" / "usb"
    target = payload / f"AGENT-ARENA-{DESKTOP}"
    for sub in ("tools", "package", "enroll", "evidence"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    for f in (usb / "tools").glob("*.ps1"):
        shutil.copy2(f, target / "tools" / f.name)
    for f in usb.glob("*.cmd"):
        write_text(target / f.name, f.read_text(encoding="utf-8"), crlf=True)
    readme = (usb / "README.txt").read_text(encoding="utf-8").replace("__VERSION__", DESKTOP).replace("__SERVICE__", service_id) \
        .replace("__ENDPOINTS__", ", ".join(deploy["event_service_endpoints"]))
    write_text(target / "README.txt", readme, crlf=True)
    profile = {"toolset_version": DESKTOP, "package": pkg_name, "version": DESKTOP, "service_id": service_id,
               "event_service_endpoints": deploy["event_service_endpoints"], "display_networks": deploy["display_networks"],
               "prepared_at": git("log", "-1", "--format=%cI") or time.strftime("%Y-%m-%dT%H:%M:%S"), "prepared_on": "lab-provisioner release build"}
    (target / "tools" / "usb-profile.json").write_text(json.dumps(profile, indent=4), encoding="utf-8")
    shutil.copytree(pkg, target / "package" / pkg_name)
    return {"version": DESKTOP, "package": pkg_name, "exe_sha256": sha256(pkg / "app" / "AgentArena.exe"),
            "endpoints": deploy["event_service_endpoints"], "service_id": service_id}


def derived_diff() -> tuple[str, dict]:
    """Unified diff of the derived head engine against the unchanged 1.0.3 modules, and their hashes."""
    out, record = [], {}
    pairs = [(ORIGINAL_103 / "engine" / "modules" / n, LAB / "engine" / "head-engine" / "modules" / n) for n in DERIVED]
    pairs.append((ORIGINAL_103 / "engine" / "catalog" / "model-catalog.json", LAB / "engine" / "head-engine" / "catalog" / "model-catalog.json"))
    for original, derived in pairs:
        a = original.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        b = derived.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        rel_a = original.relative_to(REPO).as_posix()
        rel_b = derived.relative_to(REPO).as_posix()
        out.extend(difflib.unified_diff(a, b, f"a/{rel_a}", f"b/{rel_b}"))
        record[derived.relative_to(LAB / "engine").as_posix()] = {"from": rel_a, "original_sha256": sha256(original),
                                                                   "derived_sha256": sha256(derived), "changed": a != b}
    return "".join(out), record


def scan_for_secrets(root: Path) -> list[str]:
    found = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if SECRET_NAMES.search(p.name):
            found.append(f"{rel}: file name looks like a credential")
            continue
        if p.suffix.lower() in TEXT_SUFFIXES and p.stat().st_size < 5_000_000:
            text = p.read_text(encoding="utf-8", errors="replace")
            for rx in SECRET_TEXT:
                for m in rx.finditer(text):
                    if m.group(0) not in EXAMPLE_CODES:
                        found.append(f"{rel}: {rx.pattern[:30]}...")
                        break
    for rel in ("payload/AGENT-ARENA-HUB/data",):
        if (root / FOLDER / rel).exists():
            found.append(f"{rel}: moved service data must never be on this USB")
    return found


GATEWAY_LAB_TOOLS = {   # 1.6.29: the Gateway release's own hub tools, one click each from the USB root
    "GATEWAY-CAPACITY-CHECK.cmd": ("g6-capacity-check/G6-CAPACITY-CHECK.cmd",
                                   "on the hub, once after installing and before the long campaign: how the lab does at 1 to 24 calls at once (about 65 min, the lab idle)"),
    "GATEWAY-LAUNCH-STORM.cmd": ("launch-storm/LAUNCH-STORM.cmd",
                                 "on the hub, the lab idle: how fast the Gateway answers many workers starting at once (a few minutes, no model calls)"),
    "GATEWAY-GEMMA-TEST.cmd": ("g4-gemma500-repro/G4-GEMMA500-REPRO.cmd",
                               "on the hub, the lab idle: replays the saved Gemma requests that failed with HTTP 500 on one idle head (about 10 min)"),
}


def extract_gateway_lab_tools(zip_path: Path, dest: Path) -> list:
    """The release's lab-tools folder, byte for byte (each member already checked against its SHA256SUMS)."""
    import zipfile
    out = []
    with zipfile.ZipFile(zip_path) as z:
        prefix = next(n for n in z.namelist() if n.endswith("/SHA256SUMS"))[: -len("SHA256SUMS")] + "lab-tools/"
        for name in z.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                target = dest / name[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(name))
                out.append(name[len(prefix):])
    missing = [rel for rel, _ in GATEWAY_LAB_TOOLS.values() if rel not in out]
    if missing:
        raise SystemExit(f"the Gateway release has no lab tool {missing}")
    return sorted(out)


def check_gateway_release(zip_path: Path) -> dict:
    """The Gateway release as supplied: archive against its .sha256, every member against SHA256SUMS."""
    import zipfile
    sha_file = Path(str(zip_path) + ".sha256")
    line = sha_file.read_text(encoding="utf-8").strip()
    m = re.match(r"^([0-9a-f]{64})\s+\*?(.+)$", line)
    if not m or m.group(2).strip() != zip_path.name:
        raise SystemExit(f"{sha_file.name} does not name {zip_path.name}")
    actual = sha256(zip_path)
    if actual != m.group(1):
        raise SystemExit(f"{zip_path.name} does not match its published SHA-256")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        sums_name = next(n for n in names if n.endswith("/SHA256SUMS"))
        prefix = sums_name[: -len("SHA256SUMS")]
        count = 0
        for row in z.read(sums_name).decode("utf-8").splitlines():
            if not row.strip():
                continue
            digest, rel = row.split("  ", 1)
            if hashlib.sha256(z.read(prefix + rel)).hexdigest() != digest:
                raise SystemExit(f"Gateway release member fails its SHA-256: {rel}")
            count += 1
        manifest = json.loads(z.read(prefix + "deployment-manifest.json"))
    return {"status": "included unchanged", "file": zip_path.name, "sha256": actual, "sha256_file": sha_file.name, "release": manifest["release"],
            "manifest_schema": manifest["schema"], "files_verified": count, "source": str(zip_path),
            "open_items": [f"{o['id']} ({o['owner']}): {o['item']}" for o in manifest.get("open_items", [])]}


def check_runtime(cache: Path, config: dict) -> list[tuple[Path, dict]]:
    """The official installers (Python, PowerShell, the Visual C++ runtime Capstone's torch needs), against the
    hashes recorded in the config."""
    out = []
    for kind in ("python", "pwsh", "vcredist"):
        spec = config["runtime"][kind]
        path = cache / spec["file"]
        if not path.is_file():
            raise SystemExit(f"missing runtime installer {path}")
        if "md5_published" in spec and hashlib.md5(path.read_bytes()).hexdigest() != spec["md5_published"]:
            raise SystemExit(f"{path.name}: MD5 differs from python.org's published value")
        want = spec.get("sha256_published") or spec.get("sha256")
        if want and sha256(path) != want:
            raise SystemExit(f"{path.name}: SHA-256 differs from the recorded value")
        out.append((path, {"file": path.name, "version": spec["version"], "sha256": sha256(path), "publisher": spec["publisher"]}))
    return out


def check_ollama(folder: Path, config: dict) -> tuple[Path, dict]:
    """1.6.14: the pinned Ollama Windows archive (config ollama.runtime), byte for byte against its recorded SHA-256.
    The heads update from the stick (HEAD-OLLAMA), so no head needs the internet for it."""
    spec = config["ollama"]["runtime"]
    path = folder / spec["file"]
    if not path.is_file():
        raise SystemExit(f"missing the pinned Ollama {spec['version']} archive {path} (download {spec['source']}; or --no-ollama)")
    digest = sha256(path)
    if digest != spec["sha256"]:
        raise SystemExit(f"{path} ({digest}) is not the pinned Ollama {spec['version']} archive ({spec['sha256']})")
    return path, {"status": "included unchanged", "file": spec["file"], "version": spec["version"], "sha256": digest, "source": spec["source"]}


def check_services(folder: Path, config: dict) -> list[tuple[Path, dict]]:
    """Capstone's business-service packages named in config business_services, each the approved archive byte for byte."""
    out = []
    for name, spec in (config.get("business_services") or {}).items():
        if name.startswith("_"):
            continue
        approved = spec["approved_release"]
        path = folder / approved["file"]
        if not path.is_file():
            raise SystemExit(f"{approved['file']} is not in {folder} (use --no-services to build without the QMS and ERP services)")
        digest = sha256(path)
        if digest != approved["sha256"]:
            raise SystemExit(f"{path.name} ({digest}) is not the approved {spec['product']} release ({approved['sha256']}) in config/lab-network.json")
        out.append((path, {"service": name, "product": spec["product"], "file": path.name, "version": approved["version"],
                           "sha256": digest, "head": spec["head"], "port": spec["port"], "status": "included unchanged"}))
    return out


def build(out_dir: Path, *, capstone: Path | None, tests: Path | None, gateway: Path | None = None, runtime: Path | None = None,
          services: Path | None = None, ollama: Path | None = None, say=print) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    release = out_dir / FOLDER
    release.mkdir(parents=True)
    config = json.loads((LAB / "config" / "lab-network.json").read_text(encoding="utf-8"))
    service_id = config["arena"]["service_id"]

    copy_tree(LAB / "engine", release / "engine")
    copy_tree(LAB / "config", release / "config")
    copy_tree(LAB / "tests", release / "tests", skip=("__pycache__", "*.pyc", "*.py"))   # the PowerShell suite runs on the USB; builder tests stay in the repo
    for name in ("START-LAB-PROVISIONER.cmd", "COLLECT-LAB-EVIDENCE.cmd", "FIX-LAB-HEAD.cmd", "RESTORE-HEAD-MODELS.cmd", "MEMORY-TRACE.cmd", "RUN-LAB-TESTS.cmd", "WATCH-LAB.cmd", "VERSION"):
        write_text(release / name, (LAB / name).read_text(encoding="utf-8"))
    for f in (LAB / "docs").glob("*.md"):
        write_text(release / "docs" / f.name, f.read_text(encoding="utf-8"), crlf=False)
    diff, derived = derived_diff()
    write_text(release / "docs" / "CHANGES-FROM-1.0.3.diff", diff, crlf=False)
    results = LAB / "docs" / "test-results"
    if results.exists():   # automated results only; the dry-run reports describe the dev laptop and stay in the repo
        for f in results.rglob("*.json"):
            target = release / "docs" / "test-results" / f.relative_to(results)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
    if tests and tests.exists():
        (release / "docs" / "test-results").mkdir(parents=True, exist_ok=True)
        shutil.copy2(tests, release / "docs" / "test-results" / tests.name)

    payload = release / "payload"
    payload.mkdir()
    viewer = build_viewer_toolset(payload, service_id)
    quiet = lambda *_a, **_k: None  # noqa: E731
    hub_manifest = hub_usb.prepare(payload, move_data_config=None, say=quiet)
    ticketing_manifest = hub_usb.prepare_ticketing(payload, say=quiet)

    packages = release / "packages"
    for sub in ("ollama", "gateway", "coder", "capstone", "runtime", "services"):
        (packages / sub).mkdir(parents=True, exist_ok=True)
        write_text(packages / sub / "README.txt", (LAB / "packages" / sub / "README.txt").read_text(encoding="utf-8"))
    gateway_info = {"status": "not supplied"}
    if gateway:
        gateway_info = check_gateway_release(gateway)
        shutil.copyfile(gateway, packages / "gateway" / gateway.name)                       # byte for byte
        shutil.copyfile(Path(str(gateway) + ".sha256"), packages / "gateway" / (gateway.name + ".sha256"))
        gateway_info["lab_tools"] = extract_gateway_lab_tools(gateway, release / "engine" / "hub-tools" / "gateway-lab-tools")
    runtime_info = []
    if runtime:
        for path, info in check_runtime(runtime, config):
            shutil.copyfile(path, packages / "runtime" / path.name)
            runtime_info.append(info)
    ollama_info = {"status": "not included"}
    if ollama:
        path, ollama_info = check_ollama(ollama, config)
        shutil.copyfile(path, packages / "ollama" / path.name)                             # byte for byte
    services_info = []
    if services:
        for path, info in check_services(services, config):
            shutil.copyfile(path, packages / "services" / path.name)                       # byte for byte
            services_info.append(info)
    capstone_info = {"status": "not included"}
    if capstone:
        approved = config["capstone"].get("approved_release")
        digest = sha256(capstone)
        if approved and (capstone.name != approved["file"] or digest != approved["sha256"]):
            raise SystemExit(f"{capstone.name} ({digest}) is not the approved Capstone release "
                             f"{approved['file']} ({approved['sha256']}) in config/lab-network.json")
        shutil.copy2(capstone, packages / "capstone" / capstone.name)
        capstone_info = {"status": "included unchanged", "file": capstone.name, "sha256": digest,
                         "version": re.search(r"Capstone-(.+)-windows-x64\.zip", capstone.name).group(1),
                         "format": config["capstone"]["package_format"], "source": str(capstone)}

    # USB root files
    write_text(out_dir / "START-LAB-PROVISIONER.cmd",
               f'@echo off\r\nrem  Agent Arena Lab Provisioner {VERSION}: the one entry point for every lab machine.\r\n'
               f'call "%~dp0{FOLDER}\\START-LAB-PROVISIONER.cmd" %*\r\n')
    write_text(out_dir / "COLLECT-LAB-EVIDENCE.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: one double-click collects everything needed to diagnose this computer onto this stick.\r\n'
               f'call "%~dp0{FOLDER}\\COLLECT-LAB-EVIDENCE.cmd" %*\r\n')
    if gateway:
        for root_name, (rel, what) in GATEWAY_LAB_TOOLS.items():
            write_text(out_dir / root_name,
                       f'@echo off\r\nrem  Agent Arena Lab {VERSION}, Inference Gateway lab tool: {what}.\r\n'
                       f'call "%~dp0{FOLDER}\\engine\\hub-tools\\gateway-lab-tools\\{rel.replace("/", chr(92))}" %*\r\n')
    write_text(out_dir / "WATCH-LAB.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: a live, read-only window of this machine at work (hub: the Gateway; Capstone laptop: the agents step by step; head: its Ollama). Changes nothing.\r\n'
               f'call "%~dp0{FOLDER}\\WATCH-LAB.cmd" %*\r\n')
    write_text(out_dir / "FIX-LAB-HEAD.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: one double-click on a lab head records its memory, excludes the models from Defender, stops the Ollama tray app, updates Agent Arena and restarts (asks). It deletes nothing.\r\n'
               f'call "%~dp0{FOLDER}\\FIX-LAB-HEAD.cmd" %*\r\n')
    write_text(out_dir / "MEMORY-TRACE.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: on a lab head, about 2 hours: what makes its kernel memory grow (quiet, Agent Arena, inference, both). Changes nothing.\r\n'
               f'call "%~dp0{FOLDER}\\MEMORY-TRACE.cmd" %*\r\n')
    write_text(out_dir / "RESTORE-HEAD-MODELS.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: on the hub, puts a head\'s models back from the hub\'s library (Fleet Commit; asks APPROVE).\r\n'
               f'call "%~dp0{FOLDER}\\RESTORE-HEAD-MODELS.cmd" %*\r\n')
    # 1.6.14: the Ollama update and qualification on each head, and the hub's capacity step, from the USB root.
    write_text(out_dir / "HEAD-OLLAMA.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: on a lab head, updates Ollama to the pinned version from this stick (no internet) and qualifies parallel inference (1 update, 2 again, 0 roll back). It deletes nothing.\r\n'
               f'call "%~dp0{FOLDER}\\engine\\head-tools\\head-ollama\\HEAD-OLLAMA.cmd" %*\r\n')
    # 1.6.17: head 2 (the hand-built GPU workstation) is protected; this one-click is the approved way to run HEAD-OLLAMA there.
    write_text(out_dir / "HEAD-OLLAMA-HEAD-2.cmd",
               '@echo off\r\nrem Head 2 (192.0.2.2, the GPU workstation) only: HEAD-OLLAMA with the approval for the protected head.\r\n'
               f'call "%~dp0{FOLDER}\\engine\\head-tools\\head-ollama\\HEAD-OLLAMA.cmd" -AllowProtected %*\r\n')
    write_text(out_dir / "APPLY-MODEL-CAPACITY.cmd",
               f'@echo off\r\nrem  Agent Arena Lab {VERSION}: on the hub, shows every head\'s qualification and gives the Gateway the measured per-model limits (asks APPLY). It deletes nothing.\r\n'
               f'call "%~dp0{FOLDER}\\engine\\hub-tools\\model-capacity\\APPLY-MODEL-CAPACITY.cmd" %*\r\n')
    write_text(out_dir / "VERIFY-USB.cmd",
               '@echo off\r\ntitle Agent Arena Lab - verify this USB\r\nrem  Read-only: checks every file against SHA256SUMS.txt.\r\n'
               f'"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0{FOLDER}\\engine\\Verify-LabUsb.ps1"\r\npause\r\n')
    for md, txt in (("OPERATOR-GUIDE.md", "OPERATOR-GUIDE.txt"), ("FIELD-ACCEPTANCE-CHECKLIST.md", "FIELD-ACCEPTANCE-CHECKLIST.txt")):
        src = LAB / "docs" / md
        if src.exists():
            write_text(out_dir / txt, src.read_text(encoding="utf-8"), crlf=True)

    problems = scan_for_secrets(out_dir)
    if problems:
        shutil.rmtree(out_dir)
        raise SystemExit("Refusing to build: possible secrets\n  " + "\n  ".join(problems))
    too_big = [p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file() and p.stat().st_size >= 4 * 1024 ** 3]
    if too_big:
        raise SystemExit(f"Files too large for a FAT32 USB: {too_big}")

    files = {p.relative_to(release).as_posix(): sha256(p) for p in sorted(release.rglob("*")) if p.is_file()}
    digest = hashlib.sha256("".join(f"{k}\n{v}\n" for k, v in sorted(files.items())).encode()).hexdigest()
    commit = git("rev-parse", "--short=12", "HEAD")
    dirty = bool(git("status", "--porcelain", "--", "lab-provisioner", "arena_service", "coordinator", "desktop/usb", "frontend"))
    original_zip = {"file": ORIGINAL_ZIP.name, "sha256": sha256(ORIGINAL_ZIP)} if ORIGINAL_ZIP.exists() else {"status": "not in repo"}
    fix_snapshot = {}
    if (FIX_SNAPSHOT / "SHA256SUMS.txt").exists():
        for line in (FIX_SNAPSHOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
            if m:
                fix_snapshot[m.group(2)] = m.group(1).lower()
    manifest = {
        "schema": "agent-arena.lab-release/1", "product": "Agent Arena Lab Provisioner", "version": VERSION,
        "release_id": f"agent-arena-lab-{VERSION}+{commit or 'nogit'}{'.dirty' if dirty else ''}.{digest[:12]}",
        "content_digest": digest, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "built_on": socket.gethostname(),
        "git": {"commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "dirty": dirty},
        "supported_roles": ROLES,
        "dependencies": {
            "windows": "Windows 10 or 11, 64-bit, Windows PowerShell 5.1 (in-box)",
            "head_viewer": ".NET Framework 4.8 and the WebView2 Runtime (checked by the toolset)",
            "hub_and_ticketing": "Python 3.12 or later on PATH for the signed-in user",
            "ollama": f"the pinned Ollama {config['ollama']['runtime']['version']} Windows archive in packages/ollama (HEAD-OLLAMA; no download)",
            "models": "internet access to registry.ollama.ai from the head (pulled, never bundled)",
            "arena_hub": f"Agent Arena event service at {config['arena']['endpoint']} as {service_id}",
        },
        "sources": {
            "head_provisioner_1_0_3": {"commit": "5574901", "zip": original_zip, "use": "network, Ollama install, model queue and probes reused (engine/head-engine); remote/firewall/task replaced"},
            "fix_existing_heads_2026_11_04": {"status": "reference only, not shipped and never run", "snapshot": "provisioner/original/FIX-EXISTING-HEADS-2026-11-04", "sha256": fix_snapshot},
            "arena_head_viewer": viewer,
            "arena_hub": {"version": SERVICE_VERSION, "files": len(hub_manifest["files"]), "moves_data": hub_manifest["moves_data"]},
            "arena_ticketing_adapter": {"version": SERVICE_VERSION, "files": len(ticketing_manifest["files"])},
            "capstone": capstone_info,
            "capstone_business_services": services_info or {"status": "not included"},
            "inference_gateway": gateway_info,
            "runtime_installers": runtime_info or {"status": "not included"},
            "ollama_runtime": ollama_info,
            "coder": {"status": "not supplied: no Windows release, Gateway client or Arena publisher (Coder project)"},
        },
        "derived_files": derived,
        "secrets_included": False,
        "files": files,
    }
    (release / "release-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    root_sums = out_dir / "SHA256SUMS.txt"
    sums = [f"{sha256(p)}  {p.relative_to(out_dir).as_posix()}" for p in sorted(out_dir.rglob("*")) if p.is_file() and p != root_sums]
    write_text(out_dir / "SHA256SUMS.txt", "\n".join(sums) + "\n", crlf=True)
    size = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    say(f"Release {manifest['release_id']}: {len(sums)} files, {size / 1e6:.0f} MB in {out_dir}")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(REPO / "dist" / f"lab-provisioner-{VERSION}"))
    parser.add_argument("--capstone-package", default=str(CAPSTONE_DEFAULT))
    parser.add_argument("--no-capstone", action="store_true", help="leave packages/capstone empty")
    parser.add_argument("--tests", help="test results JSON to include in docs/test-results")
    parser.add_argument("--gateway-package", default=str(GATEWAY_DEFAULT))
    parser.add_argument("--no-gateway", action="store_true")
    parser.add_argument("--runtime-dir", default=str(REPO / "dist" / "_runtime-cache"), help="the official Python and PowerShell installers")
    parser.add_argument("--no-runtime", action="store_true")
    parser.add_argument("--services-dir", default=str(SERVICES_DEFAULT), help="Capstone's CapstoneQMS and CapstoneERP packages")
    parser.add_argument("--no-services", action="store_true")
    parser.add_argument("--ollama-dir", default=str(REPO / "dist" / "_runtime-cache" / "ollama-0.40.15"), help="the pinned Ollama Windows archive (config ollama.runtime)")
    parser.add_argument("--no-ollama", action="store_true")
    args = parser.parse_args(argv)
    capstone = None if args.no_capstone else Path(args.capstone_package)
    if capstone and not capstone.is_file():
        print(f"Capstone package not found: {capstone} (use --no-capstone to build without it)")
        return 2
    gateway = None if args.no_gateway else Path(args.gateway_package)
    if gateway and not gateway.is_file():
        print(f"Gateway release not found: {gateway} (use --no-gateway)")
        return 2
    build(Path(args.out), capstone=capstone, tests=Path(args.tests) if args.tests else None, gateway=gateway,
          runtime=None if args.no_runtime else Path(args.runtime_dir), services=None if args.no_services else Path(args.services_dir),
          ollama=None if args.no_ollama else Path(args.ollama_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
