"""Behavior checks for the presentation controller, independent of a browser clock."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_presentation_behaviors():
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("choreography_check.mjs"))],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_live_room_model():
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("scene_live_check.mjs"))],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_room_asset_manifest_is_current():
    """The desktop build packages exactly the files in frontend/assets/room-assets.txt."""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["node", str(root / "tools" / "room_assets.mjs"), "--check"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
