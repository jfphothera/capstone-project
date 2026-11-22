"""Ticketing adapter process: tail Capstone's arena_events read-only and publish arena.v2.

    python -E -s -m arena_service.adapters.capstone --config adapter.json

It never writes to the Capstone database (SQLite `mode=ro`), keeps a durable
checkpoint, and relies on deterministic event IDs for idempotent re-publishing.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import logging.handlers
import os
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from ... import secretfile
from ...client import ArenaError, Client, ServiceIdentityError, iso
from .translate import APP_DISPLAY, ORCHESTRATOR, Translator

log = logging.getLogger("arena_service.adapters.capstone")
BATCH = 100


class Adapter:
    def __init__(self, config, client=None):
        self.config = config
        self.db_path = Path(config["capstone_db"]).resolve()
        self.checkpoint_path = Path(config["checkpoint_file"])
        if client is None:
            credential_id, secret = secretfile.read(config["credential_file"])
            client = Client(config["aes_url"], credential_id, secret)
        self.client = client
        self.heartbeat_seconds = int(config.get("heartbeat_seconds", 15))
        saved = self._load()
        self.sequence = saved.get("sequence")
        self.registry_digests = saved.get("registry_digests", {})
        self.translator = Translator(saved.get("registry"), saved.get("translator"), declare_all=True)
        self.translator.prune()             # a checkpoint written by 2.3.5 or earlier holds the whole history
        self.last_heartbeat = 0.0
        self.published = self.rejected = 0

    # ----------------------------------------------------------- persistence
    def _load(self):
        try:
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self):
        data = {"sequence": self.sequence, "registry_digests": self.registry_digests,
                "registry": self.translator.registry, "translator": self.translator.state(), "saved_at": iso()}
        tmp = self.checkpoint_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.checkpoint_path)

    def _mark(self):
        return copy.deepcopy((self.translator.registry, self.translator.state(), self.registry_digests))

    def _restore(self, saved):
        registry, state, digests = copy.deepcopy(saved)
        self.translator = Translator(registry, state, declare_all=True)
        self.registry_digests = digests

    def _connect(self):
        return sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=5)

    # --------------------------------------------------------------- publish
    def _publish(self, events):
        for start in range(0, len(events), BATCH):
            chunk = events[start:start + BATCH]
            response = self.client.publish(chunk)
            for result in response["results"]:
                if result["status"] == "rejected":
                    self.rejected += 1
                    log.warning("event %s rejected: %s", result.get("event_id"), result.get("reason"))
            self.published += len(chunk)

    def sync_registry(self):
        """Publish current agent registrations from Capstone's registry (as of now, not history)."""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_registry'").fetchone()
            rows = [dict(r) for r in db.execute("SELECT agent_id,display_name,role,retired FROM agent_registry")] if exists else []
        saved = self._mark()
        events, now = [], iso()
        for row in rows:
            agent = row["agent_id"]
            self.translator.registry[agent] = {"display_name": row["display_name"], "role": row["role"]}
            digest = hashlib.sha256(f"{row['display_name']}|{row['role']}|{row['retired']}".encode()).hexdigest()[:12]
            if self.registry_digests.get(agent) == digest:
                continue
            fake = {"event_id": f"registry-{agent}-{digest}", "timestamp": now, "event_type": "agent.registry_sync",
                    "sequence": 0}
            if row["retired"]:
                events.append(self.translator._base(fake, "retire", "agent.retired", agent_id=agent))
            else:
                self.translator.agents.pop(agent, None)
                self.translator._ensure_agent(fake, agent, events)
            self.registry_digests[agent] = digest
        if events:
            try:
                self._publish(events)
            except BaseException:
                self._restore(saved)
                raise
            self._save()
        return len(events)

    def step(self, limit=200):
        """Translate and publish the next batch; returns the number of Capstone rows consumed."""
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if self.sequence is None:
                start = self.config.get("start_from", "now")
                top = db.execute("SELECT COALESCE(MAX(sequence),0) FROM arena_events").fetchone()[0]
                # Never replay history as live work: a first start begins at the current end.
                self.sequence = top if start == "now" else int(start)
                self._save()
            rows = [dict(r) for r in db.execute("SELECT * FROM arena_events WHERE sequence>? ORDER BY sequence LIMIT ?",
                                                (self.sequence, limit))]
        if not rows:
            return 0
        saved = self._mark()
        events = []
        try:
            for row in rows:
                events.extend(self.translator.translate(row))
            if events:
                self._publish(events)
        except BaseException:
            self._restore(saved)        # nothing is half-applied: the retry re-translates from the checkpoint
            raise
        self.sequence = rows[-1]["sequence"]
        self.translator.prune()
        self._save()
        return len(rows)

    def heartbeat(self):
        url = self.config.get("capstone_health_url")
        if url:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    if not json.load(response).get("ok"):
                        return False
            except Exception:
                return False        # Capstone down: let the producer go offline honestly
        self.client.publish([{"type": "orchestrator.heartbeat", "event_id": f"hb-{int(time.time() * 1000)}",
                              "occurred_at": iso(), "orchestrator_id": ORCHESTRATOR,
                              "display_name": f"{APP_DISPLAY} coordinator", "heartbeat_seconds": self.heartbeat_seconds}])
        self.last_heartbeat = time.time()
        return True

    def run(self, stop: threading.Event):
        delay, poll = 1.0, float(self.config.get("poll_seconds", 1.0))
        registry_at = 0.0
        while not stop.is_set():
            try:
                if time.time() - registry_at > 60:
                    self.sync_registry()
                    registry_at = time.time()
                if time.time() - self.last_heartbeat >= self.heartbeat_seconds:
                    self.heartbeat()
                consumed = self.step()
                delay = 1.0
                stop.wait(0 if consumed else poll)
            except ServiceIdentityError:
                log.error("event service failed its authenticity check; not publishing")
                stop.wait(delay)
                delay = min(30, delay * 2)
            except (ArenaError, OSError, sqlite3.Error) as exc:
                log.warning("adapter retrying in %.0fs: %s", delay, exc)
                stop.wait(delay)
                delay = min(30, delay * 2)


def main():
    parser = argparse.ArgumentParser(prog="ticketing-arena-adapter")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # Rotated like the event service's log (before 2.3.6 this file grew for as long as the laptop ran).
    handlers = [logging.handlers.RotatingFileHandler(config["log_path"], maxBytes=5_000_000, backupCount=5, encoding="utf-8")]         if config.get("log_path") else None
    logging.basicConfig(handlers=handlers, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Ticketing adapter starting; Capstone DB %s (read-only) -> %s", config["capstone_db"], config["aes_url"])
    Adapter(config).run(threading.Event())


if __name__ == "__main__":
    main()
