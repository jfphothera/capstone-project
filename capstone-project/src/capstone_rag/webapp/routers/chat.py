"""The customer-facing chatbot.

Conversation state lives in SQLite, so a stateless handler can resume a
conversation by id and a customer can come back to it. Nothing internal is
returned to the customer view: no session JSON, no field names, no model
diagnostics. The representative-facing shape of the same conversation is on
the case, which is where internals belong.
"""

from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException

from ..runtime import get_runtime
from ..schemas import ChatMessage

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _customer_view(session, reply=None, execution=None) -> dict:
    """What the customer's browser is allowed to see."""
    out = {
        "conversation_id": session.conversation_id,
        "turns": [{"role": t["role"], "text": t["text"], "at": t["at"]}
                  for t in session.turns],
        "handed_off": session.handed_off,
        "case_reference": session.handoff_case_id,
        # Enough for the widget to show "we have your name and date of
        # birth", without echoing the values back or exposing the claim.
        "identification": {
            "name": bool(session.claim.name),
            "date_of_birth": bool(session.claim.dob),
            "serial_number": bool(session.claim.serial),
            "verified": session.verified,
        },
    }
    if reply is not None:
        out["reply"] = {"text": reply.text, "sources": reply.sources,
                        "handed_off": reply.handed_off,
                        "case_reference": reply.handoff_case_id}
        if execution:out['reply']['execution']=execution
    return out


@router.post("/sessions", status_code=201)
def new_session():
    rt = get_runtime()
    bot = rt.chatbot()
    session = bot.new_session()
    from ... import chat as chat_mod
    return {**_customer_view(session), "opening": chat_mod.OPENING}


@router.get("/sessions/{conversation_id}")
def resume_session(conversation_id: str):
    """Resume rather than silently starting an unrelated conversation."""
    rt = get_runtime()
    session = rt.chatbot().load_session(conversation_id)
    if session is None:
        raise HTTPException(404, f"no conversation {conversation_id}")
    from ... import chat as chat_mod
    return {**_customer_view(session), "opening": chat_mod.OPENING}


@router.post("/sessions/{conversation_id}/messages")
def send(conversation_id: str, body: ChatMessage):
    rt = get_runtime()
    bot = rt.chatbot()
    session = bot.load_session(conversation_id)
    if session is None:
        raise HTTPException(404, f"no conversation {conversation_id}")
    was_handed_off = session.handed_off
    from ...coordination import CaseBusy
    try:
        reply = bot.respond(session, body.text)
    except CaseBusy as exc:
        raise HTTPException(409,str(exc)) from exc
    except (TimeoutError, PermissionError) as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        # Gateway admission and execution errors are already deliberately
        # phrased for operators. Return that useful state instead of leaving
        # the browser at an indefinite typing indicator.
        message = str(exc)
        if any(word in message.lower() for word in ('gateway', 'embedding', 'inference', 'model', 'queue')):
            raise HTTPException(503, message) from exc
        raise
    if reply.handed_off and not was_handed_off and reply.handoff_case_id:
        rt.announce_case(reply.handoff_case_id, "case_arrived")
        rt.ops.queue.submit(
            reply.handoff_case_id,
            (rt.store.case(reply.handoff_case_id) or {}).get("priority")
            or "Medium")
    call=rt.store.one('SELECT payload FROM inference_calls WHERE conversation_id=? AND status=? ORDER BY at DESC LIMIT 1',(conversation_id,'completed'))
    execution=None
    if call:
        payload=json.loads(call['payload']);node=payload.get('inference_node')
        execution={'head':node,'model':payload.get('model'),'gateway_request_id':payload.get('gateway_request_id'),
          'queue_ms':(payload.get('gateway') or {}).get('queue_ms'),'duration_ms':payload.get('duration_ms')}
    return _customer_view(session, reply, execution)


@router.get("/sessions")
def list_sessions(limit: int = 50):
    """For the representative side: which chats exist and where they went."""
    rt = get_runtime()
    rows = rt.store.query(
        "SELECT conversation_id, case_id, account_id, created_at, updated_at "
        "FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,))
    return {"conversations": rows}
