"""Keep legacy agent files and simulated labels out of operational data."""
import sys
import sqlite3
import os
from pathlib import Path

import threading

import pytest

# The stand-in Gateway embeds with MiniLM on its HTTP server threads; Windows'
# default thread stack occasionally overflows inside torch ("Windows fatal
# exception: stack overflow", seen once in a full rc.25 run). Test threads get
# a larger stack (4 MB: Windows commits it per thread, so it stays modest). Test infrastructure only.
threading.stack_size(4 * 1024 * 1024)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # shared test helpers (lab_js)
os.environ.setdefault('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING','1')


@pytest.fixture(scope='session', autouse=True)
def isolated_legacy_artifacts(tmp_path_factory):
    from capstone_rag import config, store
    root = tmp_path_factory.mktemp('legacy-agent-artifacts')
    patch = pytest.MonkeyPatch()
    original = Path(config.DB_PATH).resolve()
    if original.exists():
        source=sqlite3.connect(f'file:{original.as_posix()}?mode=ro',uri=True)
        target=sqlite3.connect(root/'operations.db')
        source.backup(target); target.close(); source.close()
        patch.setattr(config,'DB_PATH',str(root/'operations.db'))
        store.reset_default()
    for key, filename in (
        ('TICKET_LOG_PATH', 'tickets.jsonl'), ('ESCALATION_PATH', 'escalations.jsonl'),
        ('RMA_PATH', 'approvals.jsonl'), ('MEMORY_PATH', 'memory.jsonl'),
        ('LABEL_DIR', 'labels'),
    ):
        patch.setattr(config, key, str(root / filename))
    yield
    store.reset_default()
    patch.undo()
