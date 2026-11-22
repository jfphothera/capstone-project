import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _lifecycle_script(name):
    candidates = (
        ROOT / "deploy" / "windows" / name,
        ROOT.parents[1] / "tools" / name,
        ROOT.parents[2] / "tools" / name,
    )
    return next(path for path in candidates if path.is_file())


def _database(path, version):
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        database.execute("INSERT INTO meta VALUES('schema_version',?)", (str(version),))


def test_schema_preflight_accepts_only_manifest_range(tmp_path):
    accepted = tmp_path / "accepted.db"
    newer = tmp_path / "newer.db"
    _database(accepted, 11)
    _database(newer, 12)
    script = ROOT / "scripts" / "check_data_schema.py"
    good = subprocess.run(
        [sys.executable, str(script), str(accepted), "--minimum", "11", "--maximum", "11"],
        capture_output=True,
        text=True,
    )
    bad = subprocess.run(
        [sys.executable, str(script), str(newer), "--minimum", "11", "--maximum", "11"],
        capture_output=True,
        text=True,
    )
    assert good.returncode == 0 and '"compatible": true' in good.stdout
    assert bad.returncode == 3 and '"compatible": false' in bad.stdout


def test_update_and_rollback_preserve_both_durable_databases():
    update = _lifecycle_script("Update-Capstone.ps1").read_text(encoding="utf-8")
    rollback = _lifecycle_script("Rollback-Capstone.ps1").read_text(encoding="utf-8")
    assert "check_data_schema.py" in update
    assert "ai-configuration-before" in update
    assert "Copy-Item -LiteralPath $aiBackup -Destination $aiDb -Force" in update
    assert "package-manifest.json" in rollback
    assert "AllowIncompatibleSchema" in rollback
    assert "operations-before-rollback" in rollback
    assert "ai-configuration-before-rollback" in rollback
