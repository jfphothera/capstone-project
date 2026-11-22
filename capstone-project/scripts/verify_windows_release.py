"""Verify a Capstone Windows release ZIP without extracting or executing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package")
    args = parser.parse_args()
    with zipfile.ZipFile(args.package) as archive:
        bad = archive.testzip()
        if bad:
            raise SystemExit("ZIP CRC failure: " + bad)
        names = archive.namelist()
        if any(info.date_time != (1980, 1, 1, 0, 0, 0) for info in archive.infolist()):
            raise SystemExit("ZIP contains non-deterministic timestamps")
        manifest = json.loads(archive.read("package-manifest.json"))
        if manifest.get("format") != "capstone-windows-release-v1":
            raise SystemExit("Unknown release format")
        if manifest.get("secrets_included") is not False:
            raise SystemExit("Manifest does not assert a secret-free package")
        compatibility = manifest.get("compatibility") or {}
        if compatibility.get("data_schema_min") != 11 or compatibility.get("data_schema_max") != 11:
            raise SystemExit("Release does not pin the accepted data schema range")
        listed = {row["path"]: row for row in manifest["files"]}
        if set(names) != set(listed) | {"package-manifest.json"}:
            raise SystemExit("Manifest/ZIP file set mismatch")
        for name, row in listed.items():
            payload = archive.read(name)
            if len(payload) != row["size"]:
                raise SystemExit("Size mismatch: " + name)
            if hashlib.sha256(payload).hexdigest() != row["sha256"]:
                raise SystemExit("Hash mismatch: " + name)
        forbidden = (".pem", ".key", ".pfx", ".p12", ".token", ".secret")
        allowed_public_cas = {"payload/python-runtime/Lib/public-ca-bundle.pem",
                              "payload/python-runtime/Lib/codex-rejected-cas.pem"}
        leaked_names = [name for name in names if name.lower().endswith(forbidden)
                        and name not in allowed_public_cas]
        if leaked_names:
            raise SystemExit("Credential-like files included: " + ", ".join(leaked_names))
        if any("/__pycache__/" in name or name.endswith((".pyc", ".pyo")) for name in names):
            raise SystemExit("Release contains development bytecode/cache artifacts")
        required = {
            "payload/python-runtime/python.exe",
            "payload/seed-data/operations.db",
            "payload/application/VERSION",
            "payload/application/tests/conftest.py",
            "payload/application/tests/test_phase4_1_execution.py",
            "payload/application/tests/test_phase4_1_graph_integration.py",
            "payload/application/tests/test_qms_workflow_policy.py",
            "payload/application/scripts/check_data_schema.py",
            "payload/application/docs/PHASE6_IMPLEMENTATION_HANDOFF.md",
            "payload/application/docs/PHASE6_INSTALLATION_HANDOFF.md",
            "tools/Install-Capstone.ps1",
            "tools/Update-Capstone.ps1",
            "tools/Configure-Capstone.ps1",
            "tools/Test-CapstoneGateway.ps1",
            "tools/Rollback-Capstone.ps1",
        }
        missing = required - set(names)
        if missing:
            raise SystemExit("Required release files missing: " + ", ".join(sorted(missing)))
        if not any(name.startswith("payload/wheelhouse/torch-") for name in names):
            raise SystemExit("Locked runtime wheelhouse is incomplete")
        bundled_models = [name for name in names if name.startswith("payload/models/")]
        if bundled_models:
            raise SystemExit("Gateway-only release contains bundled local model data: "
                             + ", ".join(bundled_models[:5]))
    digest = hashlib.sha256(open(args.package, "rb").read()).hexdigest()
    print(json.dumps({"ok": True, "version": manifest["version"],
                      "entries": len(names), "sha256": digest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
