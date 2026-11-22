"""Preview the admin hub console against a copy of this machine's service data. Development only.

Copies the live database (a consistent sqlite backup; the live file is only read) into %TEMP%,
then runs the console on it: every add, code, retire or endpoint change lands in the copy. Status
still asks the live event service for its public health check and reads its log for warnings.
    python tools/admin_console_preview.py [port]
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena_service import admin_console  # noqa: E402

live_config = Path(os.environ["LOCALAPPDATA"]) / "AgentArenaService" / "aes.config.json"
config = json.loads(live_config.read_text(encoding="utf-8"))
folder = Path(tempfile.gettempdir()) / "arena-admin-preview"
folder.mkdir(exist_ok=True)
copy = folder / "arena-events-copy.db"
source, target = sqlite3.connect(f"file:{config['db_path']}?mode=ro", uri=True), sqlite3.connect(copy)
source.backup(target)
source.close()
target.close()
config["db_path"] = str(copy)
preview_config = folder / "aes.preview.json"
preview_config.write_text(json.dumps(config, indent=2), encoding="utf-8")
print(f"PREVIEW on a copy of the live data: {copy}", flush=True)
port = sys.argv[1] if len(sys.argv) > 1 else "18795"
sys.exit(admin_console.main(["--config", str(preview_config), "--port", port, "--no-browser"]))
