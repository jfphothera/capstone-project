"""The one set of background workers, owned by the application.

The old arrangement put the inference worker and the arrival simulator inside
a Streamlit `@st.cache_resource` function, so they belonged to the UI process
and died with it, and opening a second browser tab risked a second set. Here
they belong to the FastAPI app's lifespan: started once when the server
starts, stopped once when it stops, however many pages are open.

There is one application case worker. Explicit isolated experiments can own
bounded child workers, while a shared per-endpoint SQLite lease serializes
model inference across processes. Intake, triage and safety screening do not go through the queue, so a
Critical ticket is visible immediately even with the queue saturated.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Optional

from .. import caseflow, generation, queueing, review, store as store_mod


class EventBus:
    """Fan-out of server events to any number of connected browsers.

    The queue and the simulator run on plain threads; the HTTP side is
    asyncio. `publish` is therefore thread-safe and schedules delivery on the
    loop, and each subscriber gets a bounded queue so one stalled browser
    cannot hold the worker up or grow without limit.
    """

    def __init__(self, maxsize: int = 100):
        self._subscribers: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.maxsize = maxsize
        self.history: list = []

    def bind_loop(self, loop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: str, data: dict):
        payload = {"event": event, **(data or {})}
        self.history = (self.history + [payload])[-50:]
        loop = self._loop
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            if loop is None or not loop.is_running():
                continue
            loop.call_soon_threadsafe(self._offer, q, payload)

    @staticmethod
    def _offer(q: asyncio.Queue, payload):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # A browser that cannot keep up loses the oldest notification
            # rather than blocking the worker thread that produced it.
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except Exception:       # noqa: BLE001 - best effort only
                pass


class Runtime:
    """Everything the API needs, built once."""

    def __init__(self, prefer_llm: bool = True, store=None, agent=None,
                 representative: str = "rep.jordan"):
        self.bus = EventBus()
        self.representative = representative
        self.ops = queueing.Operations.build(prefer_llm=prefer_llm,
                                             store=store, agent=agent)
        self.prefer_llm = prefer_llm
        from .. import ai_settings
        self.inference_registry=ai_settings.Registry()
        self.ops.store.routing_enabled=True
        self.ops.store.inference_registry=self.inference_registry
        # rc.27: new operational cases are pinned to the ticketing C1 profile (role routes, output caps, policy).
        from .. import operational_profile
        try:
            self.operational_profile=operational_profile.install(self.ops.store,self.inference_registry)
        except Exception as exc:   # visible, never a silent fallback to another profile
            self.operational_profile=None;self.operational_profile_error=str(exc)[:300]
            print(json.dumps({'component':'operational_profile','event':'install_failed','error':self.operational_profile_error}),flush=True)
        from ..experiments import Manager
        self.experiments=Manager(self.inference_registry)
        if __import__('os').environ.get('CAPSTONE_TEST_ALLOW_LOCAL_EMBEDDING')=='1':
            from ..cluster_runtime import ClusterService
            self.cluster=ClusterService(self.ops.store);self.ops.store.cluster_service=self.cluster
        self.ops.queue.on_event = self._on_queue_event
        self.bot = None     # built lazily; shares the agent and the store
        self._started = False
        self._model_probe_stop = threading.Event()
        self._model_status = {"model": None, "profile_id": None,
            "profile_name": None, "endpoint": None, "ollama_available": False,
            "installed": False, "loaded": False,
            "error": "Gateway verification is pending", "verification": "pending"}

    def _refresh_model_status(self):
        try:
            self._model_status={**generation.model_status(),"verification":"verified"}
        except Exception as exc:
            self._model_status={**self._model_status,"ollama_available":False,
                "installed":False,"loaded":False,"error":str(exc),
                "verification":"degraded"}

    def _model_probe_loop(self):
        while not self._model_probe_stop.is_set():
            self._refresh_model_status()
            self._model_probe_stop.wait(30)

    # -- lifecycle -------------------------------------------------------

    def start(self, loop=None):
        if self._started:
            return
        if loop is not None:
            self.bus.bind_loop(loop)
        self.ops.queue.start()
        if hasattr(self,'cluster'):self.cluster.start()
        self.experiments.recover()
        recovered = self.ops.queue.submit_pending()
        self._model_probe_stop.clear()
        threading.Thread(target=self._model_probe_loop,name='gateway-health-probe',daemon=True).start()
        self.ops.store.audit(
            "webapp_started", actor="webapp",
            summary=f"inference worker started; {recovered} pending case(s) "
                    f"recovered",
            detail={"prefer_llm": self.prefer_llm,
                    "model_available": None,"model_verification":"background"})
        self._started = True

    def stop(self):
        self._model_probe_stop.set()
        if hasattr(self,'cluster'):self.cluster.stop()
        try:
            self.ops.simulator.pause()
        except Exception:           # noqa: BLE001 - shutting down anyway
            pass
        self.ops.queue.stop()
        self.ops.store.audit("webapp_stopped", actor="webapp",
                             summary="inference worker stopped")
        self._started = False

    # -- accessors --------------------------------------------------------

    @property
    def store(self):
        return self.ops.store

    @property
    def flow(self) -> "caseflow.CaseFlow":
        return self.ops.flow

    @property
    def erp(self):
        return self.ops.erp

    def chatbot(self):
        if self.bot is None:
            from .. import chat as chat_mod
            self.bot = chat_mod.ChatBot(self.ops.store, self.ops.flow.agent)
        return self.bot

    # -- events -----------------------------------------------------------

    def _on_queue_event(self, event, case_id):
        case = self.store.case(case_id) or {}
        self.bus.publish("case_updated", {
            "case_id": case_id, "ai_state": case.get("ai_state"),
            "status": case.get("status"), "priority": case.get("priority"),
            "queue_event": event})

    def announce_case(self, case_id: str, event: str = "case_arrived"):
        case = self.store.case(case_id) or {}
        self.bus.publish(event, {
            "case_id": case_id, "status": case.get("status"),
            "priority": case.get("priority"),
            "subject": case.get("subject"),
            "ai_state": case.get("ai_state")})

    def snapshot(self, probe_model: bool = True) -> dict:
        if probe_model:self._refresh_model_status()
        model=dict(self._model_status)
        return {
            "queue": self.ops.queue.snapshot(),
            "simulator": self.ops.simulator.snapshot(),
            "model": {"prefer_llm": self.prefer_llm,
                      "available": model["ollama_available"] and model["installed"], **model},
            "representative": self.representative,
            "store": self.store.stats(),
        }


_RUNTIME: Optional[Runtime] = None


def get_runtime() -> Runtime:
    if _RUNTIME is None:
        raise RuntimeError("the application runtime has not been started")
    return _RUNTIME


def set_runtime(runtime: Optional[Runtime]):
    global _RUNTIME
    _RUNTIME = runtime


__all__ = ["Runtime", "EventBus", "get_runtime", "set_runtime",
           "review", "store_mod"]
