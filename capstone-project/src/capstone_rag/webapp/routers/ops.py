"""The arrival simulator, the inference queue and the live event stream.

These belong to the application, not to a browser session. Starting arrivals
in one tab starts them for the operation; closing that tab does not stop
them. Opening five tabs does not create five workers.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..runtime import get_runtime

router = APIRouter(prefix="/api/ops", tags=["ops"])


@router.get("/queue")
def queue():
    rt = get_runtime()
    return {"queue": rt.ops.queue.snapshot(),
            "simulator": rt.ops.simulator.snapshot()}


@router.post("/arrivals/start")
def start_arrivals():
    rt = get_runtime()
    rt.ops.simulator.start()
    rt.bus.publish("simulator", rt.ops.simulator.snapshot())
    return rt.ops.simulator.snapshot()


@router.post("/arrivals/pause")
def pause_arrivals():
    rt = get_runtime()
    rt.ops.simulator.pause()
    rt.bus.publish("simulator", rt.ops.simulator.snapshot())
    return rt.ops.simulator.snapshot()


@router.post("/arrivals/next")
def release_one():
    rt = get_runtime()
    case_id = rt.ops.simulator.release_one()
    if case_id:
        rt.announce_case(case_id)
    return {"case_id": case_id,
            "simulator": rt.ops.simulator.snapshot()}


@router.post("/arrivals/seed")
def seed(n: int = Query(default=10, ge=1, le=50)):
    """The opening inbox. Ten by default, as the demonstration expects."""
    rt = get_runtime()
    ids = rt.ops.simulator.seed_inbox(n)
    for cid in ids:
        rt.announce_case(cid)
    return {"released": ids, "simulator": rt.ops.simulator.snapshot()}


@router.post("/arrivals/reset")
def reset(keep_cases: bool = False):
    """Return every pooled ticket. The removal is itself audited."""
    rt = get_runtime()
    out = rt.ops.simulator.reset(keep_cases=keep_cases)
    rt.bus.publish("reset", out)
    return out


@router.get("/events")
async def events(request: Request):
    """Server-sent events: new arrivals and investigation progress.

    Replaces polling, so a Critical ticket appears without the
    representative pressing anything. A heartbeat keeps the connection from
    being dropped by an idle timeout and doubles as a liveness signal.
    """
    rt = get_runtime()
    queue_ = rt.bus.subscribe()

    async def stream():
        try:
            yield _sse("hello", rt.snapshot())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue_.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield _sse("heartbeat", {"queue":
                                             rt.ops.queue.snapshot(),
                                             "simulator":
                                             rt.ops.simulator.snapshot()})
                    continue
                yield _sse(payload.get("event", "message"), payload)
        finally:
            rt.bus.unsubscribe(queue_)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
