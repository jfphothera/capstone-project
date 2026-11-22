"""Ticket arrival simulator and a bounded single-worker inference queue.

Two background threads, both controllable from the portal.

**ArrivalSimulator** releases tickets from the imported pool at randomised
intervals between 30 and 120 seconds. Each pool ticket is released at most
once, so a replayed demonstration never presents the same synthetic
complaint as a second unique customer. It can be started, paused and reset.

**InferenceQueue** runs exactly **one** Qwen3 request at a time. That is the
point of the design rather than a limitation: the machine has one CPU-bound
model and running two would make both slow. Intake, safety screening,
storage and the dashboard do not go through this queue, so none of them
waits on the model.

Critical work is not allowed to disappear behind a slow queue. Intake has
already triaged and flagged the case before it is enqueued, the queue is a
priority queue so Critical is served first, and a Critical case shows its
severity in the inbox while its deeper investigation is still pending.

States tracked: queued, processing, completed, failed, retriable. Work is
de-duplicated by case id, so a retry or a restart cannot produce two
investigations of the same case.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import caseflow, config, store as store_mod, triage as triage_mod

PRIORITY_RANK = {triage_mod.CRITICAL: 0, triage_mod.HIGH: 1,
                 triage_mod.MEDIUM: 2, triage_mod.LOW: 3}


@dataclass
class QueueStats:
    queued: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0
    retriable: int = 0
    dropped_full: int = 0
    last_error: str = ""

    def as_dict(self):
        return dict(self.__dict__)


class InferenceQueue:
    """Bounded priority queue with a single worker thread."""

    def __init__(self, flow: "caseflow.CaseFlow", maxsize=None,
                 max_retries: int = 1):
        self.flow = flow
        self.maxsize = maxsize or config.INFERENCE_QUEUE_MAX
        self.max_retries = max_retries
        self._heap = []
        self._counter = itertools.count()
        self._enqueued = set()       # case ids in flight - prevents duplicates
        self._attempts = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[str] = None
        self.stats = QueueStats()
        self.on_event: Optional[Callable] = None

    # -- lifecycle -------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="inference-worker")
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- submission ------------------------------------------------------

    def submit(self, case_id: str, priority: str = triage_mod.MEDIUM) -> bool:
        """Enqueue a case for investigation. Idempotent per case."""
        with self._lock:
            if case_id in self._enqueued or case_id == self._current:
                return False
            if len(self._heap) >= self.maxsize:
                self.stats.dropped_full += 1
                self.flow.store.update(
                    "cases", "case_id", case_id,
                    {"ai_state": caseflow.RETRIABLE})
                self.flow.store.audit(
                    "inference_queue_full", actor="queue", case_id=case_id,
                    summary=f"queue at capacity ({self.maxsize}); case marked "
                            f"retriable rather than dropped")
                self.stats.retriable += 1
                return False
            heapq.heappush(self._heap,
                           (PRIORITY_RANK.get(priority, 2),
                            next(self._counter), case_id))
            self._enqueued.add(case_id)
            self.stats.queued = len(self._heap)
        self.flow.store.update("cases", "case_id", case_id,
                               {"ai_state": caseflow.QUEUED})
        self.flow.store.audit('inference_queued', actor='queue', case_id=case_id,
                              summary='Investigation queued on the single shared worker')
        self._wake.set()
        return True

    def submit_pending(self) -> int:
        """Re-enqueue anything left queued or retriable by a previous run."""
        rows = self.flow.store.query(
            "SELECT case_id, priority FROM cases WHERE run_id IS NULL AND "
            "ai_state IN (?,?,?) ORDER BY created_at",
            (caseflow.QUEUED, caseflow.RETRIABLE, caseflow.PROCESSING))
        n = 0
        for r in rows:
            if self.submit(r["case_id"], r["priority"]):
                n += 1
        return n

    # -- worker ----------------------------------------------------------

    def _next(self):
        with self._lock:
            if not self._heap:
                return None
            _, _, case_id = heapq.heappop(self._heap)
            self._enqueued.discard(case_id)
            self._current = case_id
            self.stats.queued = len(self._heap)
            self.stats.processing = 1
            return case_id

    def _run(self):
        while not self._stop.is_set():
            case_id = self._next()
            if case_id is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                # rc.28: mark the case as being investigated when the worker starts it (the screen said "queued"
                # for the whole run), and tell the browser.
                try:
                    self.flow.store.update("cases", "case_id", case_id, {"ai_state": caseflow.PROCESSING})
                    self._emit("started", case_id)
                except Exception:   # noqa: BLE001 - a status label never stops the investigation
                    pass
                self.flow.pre_investigate(case_id)
                self.stats.completed += 1
                self._emit("completed", case_id)
            except Exception as exc:            # noqa: BLE001 - reported
                attempts = self._attempts.get(case_id, 0) + 1
                self._attempts[case_id] = attempts
                retriable = attempts <= self.max_retries
                state = caseflow.RETRIABLE if retriable else caseflow.FAILED
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                if retriable:
                    self.stats.retriable += 1
                else:
                    self.stats.failed += 1
                self.flow.store.update("cases", "case_id", case_id,
                                       {"ai_state": state})
                self.flow.store.audit(
                    "ai_investigation_failed", actor="queue", case_id=case_id,
                    summary=f"attempt {attempts}: {self.stats.last_error}",
                    detail={"traceback": traceback.format_exc()[-2000:],
                            "state": state})
                self._emit(state, case_id)
                if retriable:
                    case = self.flow.store.case(case_id) or {}
                    self.submit(case_id, case.get("priority",
                                                  triage_mod.MEDIUM))
            finally:
                with self._lock:
                    self._current = None
                    self.stats.processing = 0

    def _emit(self, event, case_id):
        if self.on_event:
            try:
                self.on_event(event, case_id)
            except Exception:       # a UI callback must never kill the worker
                pass

    def snapshot(self) -> dict:
        with self._lock:
            pending = [c for _, _, c in self._heap]
        return {**self.stats.as_dict(), "running": self.running,
                "current": self._current, "pending": pending,
                "maxsize": self.maxsize}


class ArrivalSimulator:
    """Releases pooled synthetic tickets as new cases, on a timer."""

    def __init__(self, flow: "caseflow.CaseFlow", queue: InferenceQueue,
                 min_seconds=None, max_seconds=None, seed=None):
        self.flow = flow
        self.queue = queue
        self.min_seconds = min_seconds or config.ARRIVAL_MIN_SECONDS
        self.max_seconds = max_seconds or config.ARRIVAL_MAX_SECONDS
        self.rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.released = 0
        self.next_due: Optional[float] = None
        self.last_released: Optional[str] = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="arrival-simulator")
        self._thread.start()
        self.flow.store.audit("arrival_simulator_started", actor="simulator",
                              summary=f"interval {self.min_seconds}-"
                                      f"{self.max_seconds}s")

    def pause(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.next_due = None
        self.flow.store.audit("arrival_simulator_paused", actor="simulator",
                              summary=f"{self.released} released this session")

    def _run(self):
        while not self._stop.is_set():
            wait = self.rng.uniform(self.min_seconds, self.max_seconds)
            self.next_due = time.time() + wait
            if self._stop.wait(timeout=wait):
                return
            try:
                self.release_one()
            except Exception as exc:        # noqa: BLE001 - reported
                self.flow.store.audit(
                    "arrival_failed", actor="simulator",
                    summary=f"{type(exc).__name__}: {exc}")

    # -- release ---------------------------------------------------------

    def available(self) -> int:
        return self.flow.store.one(
            "SELECT COUNT(*) c FROM ticket_pool WHERE released=0")["c"]

    def release_one(self, pool_id: Optional[str] = None) -> Optional[str]:
        """Turn one pooled ticket into a live case. Never reuses a ticket."""
        row = (self.flow.store.one(
            "SELECT * FROM ticket_pool WHERE pool_id=? AND released=0",
            (pool_id,)) if pool_id else self.flow.store.one(
            "SELECT * FROM ticket_pool WHERE released=0 "
            "ORDER BY source_ticket_id LIMIT 1"))
        if row is None:
            return None

        case_id = self.flow.intake(
            row["subject"] or "(no subject)", row["body"] or "",
            channel="unknown", account_id=row["account_id"],
            pool_id=row["pool_id"],
            author=row["account_id"])
        case = self.flow.store.case(case_id)
        self.queue.submit(case_id, case["priority"])
        self.released += 1
        self.last_released = case_id
        self.flow.store.audit(
            "ticket_arrived", actor="simulator", case_id=case_id,
            summary=f"{row['pool_id']} released as {case_id} "
                    f"({case['priority']})",
            detail={"pool_id": row["pool_id"],
                    "archetype": row["archetype"]})
        return case_id

    def seed_inbox(self, n: int = 10) -> list:
        """Release an opening batch so the inbox is not empty."""
        out = []
        for _ in range(n):
            cid = self.release_one()
            if cid is None:
                break
            out.append(cid)
        return out

    def reset(self, keep_cases: bool = False) -> dict:
        """Put every pooled ticket back, for a repeatable demonstration."""
        self.pause()
        self.flow.store.execute("UPDATE ticket_pool SET released=0")
        removed = 0
        if not keep_cases:
            rows = self.flow.store.query(
                "SELECT case_id FROM cases WHERE run_id IS NULL AND "
                "pool_id IS NOT NULL")
            for r in rows:
                self._delete_case(r["case_id"])
                removed += 1
        self.released = 0
        self.flow.store.audit(
            "arrival_simulator_reset", actor="simulator",
            summary=f"pool restored; {removed} simulated case(s) removed")
        return {"pool_restored": True, "cases_removed": removed}

    def _delete_case(self, case_id):
        st = self.flow.store
        for table in ("shipment_events",):
            st.execute(
                f"DELETE FROM {table} WHERE shipment_id IN "
                f"(SELECT shipment_id FROM sim_shipments WHERE case_id=?)",
                (case_id,))
        # approvals are append-only history and are kept even for removed
        # simulated cases (store triggers refuse deletion).
        for table in ("sim_shipments", "rmas", "erp_calls",
                      "recommendations", "reasoning_attempts",
                      "retrieved_sources", "tool_calls", "checklist_items",
                      "qa_notifications", "conversations", "messages"):
            st.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,))
        st.execute("DELETE FROM cases WHERE case_id=?", (case_id,))
        # The audit trail is append-only; the removal is itself an event.
        st.audit("case_removed_by_reset", actor="simulator", case_id=case_id,
                 summary="arrival simulator reset removed this simulated case")

    def snapshot(self) -> dict:
        return {"running": self.running, "released_this_session": self.released,
                "pool_available": self.available(),
                "next_due_in": (round(self.next_due - time.time(), 1)
                                if self.next_due else None),
                "interval": [self.min_seconds, self.max_seconds],
                "last_released": self.last_released}


@dataclass
class Operations:
    """Convenience bundle: store + flow + queue + simulator."""
    store: object
    flow: "caseflow.CaseFlow"
    queue: InferenceQueue
    simulator: ArrivalSimulator
    erp: object = field(default=None)

    @classmethod
    def build(cls, prefer_llm=True, store=None, agent=None, seed=None):
        st = store or store_mod.get_store()
        ag = agent or caseflow.make_agent(prefer_llm=prefer_llm, store=st)
        flow = caseflow.CaseFlow(st, ag)
        q = InferenceQueue(flow)
        sim = ArrivalSimulator(flow, q, seed=seed)
        return cls(store=st, flow=flow, queue=q, simulator=sim, erp=flow.erp)
