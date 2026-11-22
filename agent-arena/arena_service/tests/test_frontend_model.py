"""Cross-language check: the browser location model consumes real service output.

The real Phase 4.2 trace flows through the Ticketing adapter into a live
service; its initial snapshot, every delta and the final snapshot are handed to
Node, which replays them through agent_arena_locations.mjs.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from arena_service.adapters.capstone.runner import Adapter
from arena_service.tests.test_capstone_adapter import capstone_db, trace_rows

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "frontend" / "js" / "agent_arena_locations.mjs"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_location_model_against_real_service_output(env, tmp_path):
    db = tmp_path / "operations.db"
    capstone_db(db, trace_rows())
    client = env.app("capstone", "Ticketing")
    for n in (3, 6, 7):
        env.head(f"head-192-0-2-{n}")
    initial = env.store.snapshot()
    adapter = Adapter({"capstone_db": str(db), "checkpoint_file": str(tmp_path / "cp.json"), "start_from": 0}, client)
    adapter.sync_registry()
    while adapter.step():
        pass
    fixture = {"module": str(MODULE), "initial": initial, "final": env.store.snapshot(),
               "events": env.store.events_after(0, limit=1000)["events"]}
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(["node", "--test-reporter=spec", str(Path(__file__).with_name("locations_check.mjs")),
                             str(path)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
