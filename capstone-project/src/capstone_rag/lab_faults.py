"""Testing Lab fault injection for recovery verification.

Inactive unless ``CAPSTONE_LAB_CRASH_AT`` names a crash point (set only by
recovery tests for an isolated worker). The process is killed abruptly
(os._exit) the first time that point is reached in a run directory, simulating
a power loss; a marker file prevents crashing again on resume.

Points: before_domain_action, after_domain_action, after_journal_applied,
between_commands, customer_after_reply, workflow_started,
environment_after_event, revision_after_action.
"""
from __future__ import annotations

import os
from pathlib import Path


def crash_point(name, run_dir=None):
    wanted = os.environ.get('CAPSTONE_LAB_CRASH_AT')
    if not wanted or wanted.split('@')[0] != name:
        return
    # Optional "@n": crash at the n-th time the point is reached (default 1).
    nth = int(wanted.split('@')[1]) if '@' in wanted else 1
    directory = Path(run_dir or os.environ.get('CAPSTONE_LAB_RUN_DIR') or '.')
    counter = directory / f'.crash-count-{name}'
    marker = directory / f'.crashed-{name}'
    if marker.exists():
        return
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if count >= nth:
        marker.write_text('crashed')
        os._exit(97)
