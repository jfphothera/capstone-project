"""Build the credential-free Phase 3 independent-inspection bundle."""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "phase3" / "acceptance_evidence_bundle"
ZIP = ROOT / "docs" / "phase3" / "phase3_acceptance_evidence_bundle.zip"

SOURCE_FILES = [
    "src/capstone_rag/response_policy.py",
    "src/capstone_rag/independent_review.py",
    "src/capstone_rag/store.py",
    "src/capstone_rag/orchestration.py",
    "src/capstone_rag/review.py",
    "src/capstone_rag/planning.py",
    "src/capstone_rag/reasoning_strategies.py",
    "src/capstone_rag/telemetry.py",
    "src/capstone_rag/inference.py",
    "src/capstone_rag/webapp/app.py",
    "src/capstone_rag/webapp/routers/arena.py",
    "src/capstone_rag/webapp/static/agent_arena.html",
    "src/capstone_rag/webapp/static/agent_arena_login.html",
    "src/capstone_rag/webapp/static/css/agent_arena.css",
    "src/capstone_rag/webapp/static/js/agent_arena.js",
    "src/capstone_rag/webapp/static/js/agent_arena_reducer.mjs",
    "src/capstone_rag/webapp/static/js/agent_arena_demo.mjs",
]
TEST_FILES = [
    "tests/test_phase3_remediation.py",
    "tests/test_phase3_validation_and_arena.py",
    "tests/test_phase2_collaboration.py",
    "tests/agent_arena_reducer.test.mjs",
]
TOOL_FILES = [
    "tools/phase3_endpoint_audit.py",
    "tools/phase3_remediation_smoke.py",
    "tools/phase3_model_backed_scenarios.py",
    "tools/phase3_arena_acceptance_server.py",
    "tools/phase3_arena_screenshots.mjs",
    "tools/phase3_arena_scale_benchmark.mjs",
    "tools/generate_phase3_manifests.py",
    "tools/build_phase3_acceptance_bundle.py",
]


def selected_files() -> list[Path]:
    paths = [ROOT / name for name in SOURCE_FILES + TEST_FILES + TOOL_FILES]
    paths.extend(p for p in (ROOT / "tools" / "agent_arena_viewer").rglob("*") if p.is_file())
    docs = ROOT / "docs" / "phase3"
    paths.extend(p for p in docs.iterdir() if p.is_file() and p.name != ZIP.name)
    paths.extend(p for p in (docs / "PHASE3_ARENA_SCREENSHOTS").rglob("*") if p.is_file())
    return sorted(set(paths))


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    entries = []
    for source in selected_files():
        relative = source.relative_to(ROOT)
        target = OUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append({
            "path": relative.as_posix(),
            "bytes": source.stat().st_size,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        })
    manifest = {
        "format": "phase3-acceptance-evidence-v1",
        "created": "2026-10-29",
        "classification": "independent inspection; not proof of remote Windows hardware execution",
        "exclusions": ["credentials", "customer records", "databases", "model weights", "caches"],
        "file_count": len(entries),
        "files": entries,
    }
    (OUT / "BUNDLE_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in OUT.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(OUT).as_posix())
    print(json.dumps({
        "directory": str(OUT),
        "zip": str(ZIP),
        "files": len(entries),
        "zip_sha256": hashlib.sha256(ZIP.read_bytes()).hexdigest(),
    }, indent=2))


if __name__ == "__main__":
    main()
