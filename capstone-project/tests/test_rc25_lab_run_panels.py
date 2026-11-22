"""rc.25 hotfix: Run-screen panels stay open while their controls are used.

Every selection change rebuilds the Run screen. The tool-strategy panel
("Advanced: reviewer, tool strategy, limits") came back closed after each
choice, so native / mediated / delegated and the delegate model could not be
chosen. tests/js/lab_run_panels.mjs drives the real run.js against a minimal
DOM stand-in and checks every panel and control on the Run screen.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / 'js' / 'lab_run_panels.mjs'


def test_run_screen_panels_stay_open_and_keep_selection():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is not installed')
    done = subprocess.run([node, str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.startswith('OK'), done.stdout + done.stderr


def test_run_screen_has_no_bare_details_panels():
    from lab_js import compact
    run = (Path(__file__).parents[1] / 'src' / 'capstone_rag' / 'webapp' / 'static' / 'js' / 'lab' / 'run.js').read_text(encoding='utf-8')
    # Only panel() builds <details>, so every panel keeps its open state.
    assert compact(run).count("el('details'") == 1 and 'function panel(' in run
