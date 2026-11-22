"""Isolated Arena Event Service instances for tests (never the Capstone test tree)."""
import threading
import time

import pytest

from arena_service import client as aclient
from arena_service.server import build


class Env:
    def __init__(self, tmp_path, **config):
        self.config = dict(host="127.0.0.1", port=0, db_path=str(tmp_path / "aes.db"),
                           public_endpoints=["http://127.0.0.1:0"], **config)
        self.server, self.service = build(self.config)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.store = self.service.store
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.seq = 0

    def app(self, app_id, display, orchestrators=("main",)):
        self.store.add_application(app_id, display)
        for orch in orchestrators:
            self.store.add_orchestrator(app_id, orch, f"{display} {orch}")
        cid, secret = self.store.issue_producer(app_id, list(orchestrators))
        return aclient.Client(self.url, cid, secret)

    def head(self, node_id, address=None, apps=("*",)):
        location = self.store.add_head(node_id, node_id.replace("head-", "Head "), address, apps)
        self.store.add_display(node_id, "head", location, address)
        return location

    def viewer(self, display_id, address=None):
        code = self.store.issue_enrollment(display_id)
        cid, secret, _ = aclient.enroll(self.url, display_id, code, "m" * 32)
        return aclient.Client(self.url, cid, secret)

    def ev(self, type_, offset=0.0, orch="main", **fields):
        self.seq += 1
        return dict(type=type_, event_id=f"e{self.seq}", occurred_at=aclient.iso(time.time() + offset),
                    orchestrator_id=orch, **fields)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()
