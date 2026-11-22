"""Create a deterministic manifest of Phase 1-controlled project sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "phase1" / "final_source_manifest.json"
ROOT_FILES = {"pyproject.toml", "pytest.ini", "requirements.txt", "qms_compatibility.json",
              "revision_register.json"}
DATA_CONTROL_FILES = {"data/qms_compatibility.json", "data/revision_register.json"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in {"__pycache__", ".pytest_cache", "phase1_deps", ".venv",
                    "baseline_snapshot_20261029"} for part in relative.parts):
        return False
    if relative.name in ROOT_FILES and len(relative.parts) == 1:
        return True
    if relative.as_posix() in DATA_CONTROL_FILES:
        return True
    return relative.parts[0] in {"src", "tests", "scripts"} or relative.parts[:2] in {
        ("data", "qms"), ("data", "phase1")}


def main() -> None:
    entries = []
    for path in sorted((p for p in ROOT.rglob("*") if p.is_file() and included(p)),
                       key=lambda p: p.as_posix().lower()):
        data = path.read_bytes()
        entries.append({"path": path.relative_to(ROOT).as_posix(), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    aggregate = hashlib.sha256(json.dumps(entries, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    payload = {"algorithm": "sha256", "aggregate_sha256": aggregate,
               "file_count": len(entries), "files": entries}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("aggregate_sha256", "file_count")}, indent=2))


if __name__ == "__main__":
    main()
