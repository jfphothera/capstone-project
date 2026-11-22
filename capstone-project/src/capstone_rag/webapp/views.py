"""Read models: what the browser is sent for each screen.

Assembling these here rather than in the route handlers keeps the handlers to
"authorise, call the module, return the view", and keeps the same shape
available to the tests without going through HTTP.

Nothing in this module writes.
"""

from __future__ import annotations

import json
from typing import Optional

from .. import (
    caseflow, erp_sim, identity as identity_mod, movement as movement_mod,
    presentation, review, triage as triage_mod,
)

# Statuses that mean the case is done with.
TERMINAL = (caseflow.RESOLVED, caseflow.CLOSED)


def _loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value if default is None else default


# --- inbox -----------------------------------------------------------------

def inbox(store, *, query: str = "", priority: str = "", status: str = "",
          assigned: str = "", include_closed: bool = False,
          sort: str = "priority", limit: int = 200) -> dict:
    """The case list, filtered and sorted server-side (defect C11).

    Search is deliberately over the fields a representative would search by -
    case id, subject, customer name, serial - and not over message bodies,
    which would make an obvious typo in a long e-mail look like a match.
    """
    sql = ["SELECT * FROM cases WHERE run_id IS NULL"]
    params: list = []
    if not include_closed:
        sql.append(f"AND status NOT IN ('{caseflow.RESOLVED}', "
                   f"'{caseflow.CLOSED}')")
    if priority:
        sql.append("AND priority=?")
        params.append(priority)
    if status == 'follow_up':
        sql.append('AND EXISTS (SELECT 1 FROM messages m WHERE m.case_id=cases.case_id AND m.parent_message_id IS NOT NULL AND m.direction=\'inbound\')')
    elif status:
        sql.append("AND status=?")
        params.append(status)
    if assigned:
        sql.append("AND assigned_to=?" if assigned != "unassigned"
                   else "AND assigned_to IS NULL")
        if assigned != "unassigned":
            params.append(assigned)
    if query:
        sql.append("AND (case_id LIKE ? OR subject LIKE ? OR "
                   "customer_name LIKE ? OR serial_number LIKE ? OR "
                   "account_id LIKE ?)")
        params += [f"%{query}%"] * 5

    order = {
        "priority": ("CASE priority WHEN 'Critical' THEN 0 WHEN 'High' "
                     "THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, created_at "
                     "DESC"),
        "newest": "created_at DESC",
        "oldest": "created_at ASC",
        "updated": "updated_at DESC",
    }.get(sort, "created_at DESC")
    sql.append(f"ORDER BY {order} LIMIT {int(limit)}")

    rows = store.query(" ".join(sql), tuple(params))
    ids = [r["case_id"] for r in rows]
    decisions = _open_decision_counts(store, ids)
    preview = _previews(store, ids)

    out = []
    for r in rows:
        out.append({
            "case_id": r["case_id"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "status": r["status"],
            "priority": r["priority"],
            "channel": r["channel"],
            "subject": r["subject"],
            "preview": preview.get(r["case_id"], ""),
            "customer_name": r["customer_name"],
            "customer_label": (r["customer_name"] or
                               ("unmatched customer" if not r["account_id"]
                                else r["account_id"])),
            "account_id": r["account_id"],
            "identity_verified": bool(r["identity_verified"]),
            "serial_number": r["serial_number"],
            "model": r["model"],
            "is_complaint": bool(r["is_complaint"]),
            "is_adverse_event": bool(r["is_adverse_event"]),
            "ae_certainty": r["ae_certainty"],
            "classification": r["classification"],
            "rma_needed": bool(r["rma_needed"]),
            "rma_outcome": r["rma_outcome"],
            "tracking_status": r["tracking_status"],
            "ai_state": r["ai_state"],
            "assigned_to": r["assigned_to"],
            "open_decisions": decisions.get(r["case_id"], 0),
        })
    return {"cases": out, "count": len(out),
            "counts": status_counts(store)}


def _open_decision_counts(store, case_ids) -> dict:
    """How many real decisions each case is waiting on.

    Counted the same way the workspace counts them, so the badge on the inbox
    row and the panel inside the case cannot disagree.
    """
    if not case_ids:
        return {}
    marks = ",".join("?" for _ in case_ids)
    rows = store.query(
        f"SELECT * FROM checklist_items WHERE case_id IN ({marks})",
        tuple(case_ids))
    by_case: dict = {c: [] for c in case_ids}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)
    rmas = store.query(
        f"SELECT case_id, COUNT(*) n FROM rmas WHERE case_id IN ({marks}) "
        f"AND status=? GROUP BY case_id",
        tuple(case_ids) + (erp_sim.PROPOSED,))
    drafts = store.query(
        f"SELECT case_id, COUNT(*) n FROM messages WHERE case_id IN ({marks}) "
        f"AND direction='outbound' AND channel!='qa_notification' "
        f"AND status='draft' GROUP BY case_id", tuple(case_ids))
    counts = {}
    for cid, items in by_case.items():
        view = presentation.describe_checklist(items)
        counts[cid] = view["open_decisions"]
    for r in rmas:
        counts[r["case_id"]] = counts.get(r["case_id"], 0) + r["n"]
    for r in drafts:
        counts[r["case_id"]] = counts.get(r["case_id"], 0) + r["n"]
    return counts


def _previews(store, case_ids) -> dict:
    if not case_ids:
        return {}
    marks = ",".join("?" for _ in case_ids)
    rows = store.query(
        f"SELECT case_id, body FROM messages WHERE case_id IN ({marks}) "
        f"AND direction='inbound' ORDER BY created_at", tuple(case_ids))
    out = {}
    for r in rows:
        out.setdefault(r["case_id"],
                       " ".join((r["body"] or "").split())[:140])
    return out


def status_counts(store) -> dict:
    rows = store.query(
        "SELECT status, COUNT(*) n FROM cases WHERE run_id IS NULL "
        "GROUP BY status")
    counts = {r["status"]: r["n"] for r in rows}
    ai = store.query(
        "SELECT ai_state, COUNT(*) n FROM cases WHERE run_id IS NULL "
        "GROUP BY ai_state")
    counts["_ai"] = {r["ai_state"]: r["n"] for r in ai}
    counts["_open"] = sum(
        n for s, n in counts.items()
        if isinstance(n, int) and s not in TERMINAL)
    counts["_critical_open"] = store.one(
        "SELECT COUNT(*) c FROM cases WHERE run_id IS NULL AND "
        "priority='Critical' AND status NOT IN (?,?)",
        (caseflow.RESOLVED, caseflow.CLOSED))["c"]
    return counts


# --- one case --------------------------------------------------------------

def case_workspace(store, case_id: str) -> Optional[dict]:
    """Everything the three columns need, in one request."""
    case = store.case(case_id)
    if case is None:
        return None

    items = store.checklist(case_id)
    described = presentation.describe_checklist(items)
    for item in described['all']:
        if item['action'] == 'prepare_response':
            result = _loads(item.get('tool_result'), {})
            if isinstance(result,dict) and result.get('message_id'):
                status = store.one('SELECT status FROM messages WHERE message_id=?',(result['message_id'],))
                if status and status['status'] != 'draft':
                    item['offers_decision'] = False
    messages = [_message(m) for m in store.messages(case_id)]
    rmas = [_rma(store, r) for r in store.query(
        "SELECT * FROM rmas WHERE case_id=? ORDER BY CASE direction WHEN 'inbound' THEN 0 ELSE 1 END, created_at", (case_id,))]
    shipments = store.query(
        "SELECT * FROM sim_shipments WHERE case_id=? ORDER BY created_at",
        (case_id,))
    for s in shipments:
        s["contents"] = _loads(s["contents"], [])
        s["ship_to"] = _loads(s["ship_to"], {})
        s["ship_from"] = _loads(s["ship_from"], {})
        s["events"] = store.query(
            "SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY id",
            (s["shipment_id"],))
    notifications = store.query(
        "SELECT * FROM qa_notifications WHERE case_id=? ORDER BY created_at",
        (case_id,))
    draft = review.latest_draft(store, case_id)

    device = (store.device(case["serial_number"])
              if case.get("serial_number") else None)
    revision = None
    if device:
        rev = identity_mod.assess_revision(
            device, human_confirmation=(
                case.get("revision_confirmed_by")
                if case.get("revision_confirmation") == "confirmed" else None))
        revision = rev.as_dict()
        revision["confirmation"] = case.get("revision_confirmation")
        revision["confirmed_by"] = case.get("revision_confirmed_by")
        revision["confirmed_at"] = case.get("revision_confirmed_at")
        revision["needs_confirmation"] = (
            not rev.usable_for_troubleshooting
            and not case.get("revision_confirmation"))

    history = store.query(
        "SELECT case_id, created_at, status, classification, priority, "
        "resolution FROM cases WHERE account_id=? AND case_id!=? AND "
        "run_id IS NULL ORDER BY created_at DESC LIMIT 8",
        (case.get("account_id"), case_id)) if case.get("account_id") else []

    conversation = store.one(
        "SELECT * FROM conversations WHERE case_id=?", (case_id,))

    return {
        "case": _case(case),
        'inference_snapshots':store.query('SELECT * FROM inference_snapshots WHERE scope_key=? ORDER BY at DESC',('case:'+case_id,)),
        'response_reviews':store.query('SELECT r.* FROM response_reviews r JOIN messages m ON m.message_id=r.message_id WHERE m.case_id=? ORDER BY r.at DESC',(case_id,)),
        "coordination": __import__('capstone_rag.coordination',fromlist=['CaseRepository']).CaseRepository(store).snapshot(case_id),
        "contact_observations": store.query('SELECT o.*,d.decision FROM contact_observations o LEFT JOIN contact_decisions d ON d.observation_id=o.observation_id WHERE o.case_id=?',(case_id,)),
        "workflow_runs": store.query('SELECT * FROM workflow_runs WHERE case_id=? ORDER BY at DESC',(case_id,)),
        "customer": store.customer(case.get("account_id")),
        "device": device,
        "revision": revision,
        "checklist": described,
        "summary": investigation_summary(case, described, rmas, revision),
        "primary_decision": presentation.primary_decision(
            described, case, rmas, draft),
        "messages": messages,
        "draft": _message(draft) if draft else None,
        'service_calls': store.query("SELECT * FROM erp_calls WHERE case_id=? AND call_type='service_call' ORDER BY created_at", (case_id,)),
        'feedback': [dict(p, evidence=_loads(p['evidence'], {})) for p in store.query("SELECT * FROM improvement_proposals WHERE kind='representative_feedback' ORDER BY created_at DESC")
                     if _loads(p['evidence'], {}).get('case_id') == case_id],
        "rmas": rmas,
        "shipments": shipments,
        "notifications": notifications,
        "history": history,
        "chat": ({"conversation_id": conversation["conversation_id"],
                  "state": _loads(conversation["state_json"], {})}
                 if conversation else None),
        "audit": store.audit_trail(case_id),
        "evidence": evidence(store, case_id),
    }


def _case(case: dict) -> dict:
    out = dict(case)
    out["identity_verified"] = bool(case.get("identity_verified"))
    out["is_complaint"] = bool(case.get("is_complaint"))
    out["is_adverse_event"] = bool(case.get("is_adverse_event"))
    out["rma_needed"] = bool(case.get("rma_needed"))
    out["verification_points"] = _loads(case.get("verification_points"), [])
    out["is_closed"] = case.get("status") in TERMINAL
    return out


def _message(m: Optional[dict]) -> Optional[dict]:
    if m is None:
        return None
    out = dict(m)
    body = m.get("body") or ""
    note = None
    if "[For representative:" in body:
        body, _, rest = body.partition("[For representative:")
        note = rest.rstrip("]").strip()
    out["body"] = body.rstrip()
    out["internal_note"] = note
    out["is_draft"] = m.get("status") == "draft"
    return out


def _rma(store, r: dict) -> dict:
    out = dict(r)
    out["items"] = _loads(r["items"], [])
    call = store.one("SELECT * FROM erp_calls WHERE call_id=?",
                     (r["call_id"],))
    out["call"] = dict(call) if call else None
    if call:
        out["call"]["payload"] = _loads(call["payload"], {})
        out["call"]["result"] = _loads(call["result"], {})
    shipment = store.one("SELECT * FROM sim_shipments WHERE rma_id=?",
                         (r["rma_id"],))
    out["shipment"] = dict(shipment) if shipment else None
    if shipment:
        out["shipment"]["contents"] = _loads(shipment["contents"], [])
        out["shipment"]["events"] = store.query(
            "SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY id",
            (shipment["shipment_id"],))
    out["decidable"] = r["status"] == erp_sim.PROPOSED
    out['replacement_eligible'], out['eligibility_reason'] = erp_sim.ErpSimulator(store).replacement_eligibility(r)
    out["consequences"] = movement_consequences(out)
    return out


def movement_consequences(rma: dict) -> dict:
    """What approving this movement will actually do, stated before the click.

    The representative should not have to know what `part_outbound` means to
    understand that approving it creates an outbound inventory movement and
    generates a simulated label.
    """
    direction = rma.get("direction")
    kind = rma.get("movement_type")
    items = ", ".join(str(i.get("name")) for i in rma.get("items") or []) \
        or "unspecified items"
    outbound = direction == movement_mod.OUTBOUND
    headline = {
        movement_mod.PART_OUTBOUND: f"Send {items} to the customer.",
        movement_mod.DEVICE_REPLACEMENT:
            "Send a replacement unit to the customer.",
        movement_mod.DEVICE_RETURN:
            "Have the customer return the unit to the service centre.",
    }.get(kind, f"{kind} ({direction}).")
    return {
        "headline": headline,
        "reason": rma.get("reason"),
        "erp_action": (f"Create an {'outbound' if outbound else 'inbound'} "
                       f"inventory movement against this account."),
        "shipping": ("Generate a simulated outbound label and tracking "
                     "number after authorisation."
                     if outbound else
                     "Generate a simulated return label for the customer "
                     "after authorisation."),
        "if_rejected": ("No RMA is finalised, no label is created and "
                        "nothing is promised to the customer."),
    }


def investigation_summary(case, described, rmas, revision) -> dict:
    """The concise version at the top of the copilot panel."""
    by_action = {d["action"]: d for d in described["all"]}

    def outcome(action):
        return (by_action.get(action) or {}).get("outcome")

    verified = bool(case.get("identity_verified"))
    search = by_action.get("search_qms") or {}
    validate = by_action.get("validate_procedure_applicability") or {}
    accepted = (search.get("tool_result") or {}).get("accepted") or []
    # The procedure the reply was written from is recorded on the response
    # step, which is the only place it is known to have survived validation.
    response = by_action.get("prepare_response") or {}
    procedure = (response.get("evidence") or {}).get("source")
    grounded = (response.get("evidence") or {}).get("grounded")

    return {
        "priority": case.get("priority"),
        "priority_reason": case.get("priority_reason"),
        "classification": case.get("classification"),
        "identity": {
            "verified": verified,
            "points": _loads(case.get("verification_points"), []),
            "customer_name": case.get("customer_name"),
            "account_id": case.get("account_id"),
            "outcome": outcome("verify_identity"),
        },
        "device": {
            "serial_number": case.get("serial_number"),
            "model": case.get("model"),
            "revision": case.get("revision"),
            "revision_status": (revision or {}).get("status"),
            "usable": (revision or {}).get("usable_for_troubleshooting"),
            "outcome": outcome("assess_device_revision"),
        },
        "safety": {
            "is_adverse_event": bool(case.get("is_adverse_event")),
            "certainty": case.get("ae_certainty"),
            "outcome": outcome("screen_adverse_event"),
        },
        "complaint": {
            "is_complaint": bool(case.get("is_complaint")),
            "reason": case.get("complaint_reason"),
            "outcome": outcome("classify_complaint"),
        },
        "procedure": {
            "selected": procedure,
            "accepted_chunks": accepted,
            "outcome": outcome("search_qms"),
            "validation_outcome": outcome("validate_procedure_applicability"),
            "rejected": ((validate.get("evidence") or {}).get("rejected")
                         or [])[:6],
            "missing_info": (search.get("missing_info")
                             or validate.get("missing_info")),
        },
        "reply": {
            "grounded": grounded,
            "outcome": outcome("prepare_response"),
        },
        "movement": {
            'reason':((by_action.get('determine_product_movement') or {}).get('tool_result') or {}).get('reason'),
            "proposed": [r["rma_id"] for r in rmas
                         if r["status"] == erp_sim.PROPOSED],
            "decided": [{"rma_id": r["rma_id"], "status": r["status"]}
                        for r in rmas if r["status"] != erp_sim.PROPOSED],
            "outcome": outcome("determine_product_movement"),
        },
        "missing_information": [
            {"label": d["label"], "detail": d["missing_info"]}
            for d in described.get('current',described['all']) if d.get("missing_info")],
        "open_decisions": described["open_decisions"],
        "unresolved": described["unresolved"],
    }


def evidence(store, case_id: str) -> dict:
    """The technical trace, for the expandable detail panel."""
    return {
        "tool_calls": store.query(
            "SELECT * FROM tool_calls WHERE case_id=? ORDER BY id",
            (case_id,)),
        "retrieved_sources": store.query(
            "SELECT * FROM retrieved_sources WHERE case_id=? ORDER BY id",
            (case_id,)),
        "reasoning_attempts": store.query(
            "SELECT * FROM reasoning_attempts WHERE case_id=? ORDER BY id",
            (case_id,)),
        "approvals": store.query(
            "SELECT * FROM approvals WHERE case_id=? ORDER BY at",
            (case_id,)),
        "recommendations": store.query(
            "SELECT * FROM recommendations WHERE case_id=? ORDER BY created_at",
            (case_id,)),
    }


# --- cross-case views ------------------------------------------------------

def movements(store, *, status: str = "") -> dict:
    sql = "SELECT * FROM rmas WHERE run_id IS NULL"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT 300"
    rows = [_rma(store, r) for r in store.query(sql, tuple(params))]
    for r in rows:
        case = store.case(r["case_id"]) or {}
        r["case"] = {"case_id": r["case_id"], "subject": case.get("subject"),
                     "status": case.get("status"),
                     "customer_name": case.get("customer_name"),
                     "priority": case.get("priority")}
    by_status = {s["status"]: s["n"] for s in store.query(
        "SELECT status, COUNT(*) n FROM rmas WHERE run_id IS NULL "
        "GROUP BY status")}
    return {"rmas": rows, "count": len(rows), "by_status": by_status}


def quality(store) -> dict:
    """The safety and quality review queue."""
    notifications = store.query(
        "SELECT * FROM qa_notifications WHERE run_id IS NULL "
        "ORDER BY created_at DESC LIMIT 300")
    for n in notifications:
        case = store.case(n["case_id"]) or {}
        n["case"] = {"case_id": n["case_id"], "subject": case.get("subject"),
                     "priority": case.get("priority"),
                     "status": case.get("status"),
                     "customer_name": case.get("customer_name")}
        msg = store.one(
            "SELECT body FROM messages WHERE case_id=? AND direction='inbound'"
            " ORDER BY created_at LIMIT 1", (n["case_id"],))
        # The customer's own words are kept verbatim next to the
        # classification; a triage label is not a substitute for what they
        # actually said.
        n["customer_statement"] = (msg or {}).get("body")
    groups = {
        "reported_injury": [n for n in notifications
                            if n["kind"] == "adverse_event"],
        "possible_adverse_event": [n for n in notifications
                                   if n["kind"] == "possible_adverse_event"],
        "qc_discrepancy": [n for n in notifications
                           if n["kind"] == "qc_discrepancy"],
        "qa_document_concern": [n for n in notifications
                                if n["kind"] == "qa_document_concern"],
    }
    return {
        "notifications": notifications,
        "groups": groups,
        "open": [n for n in notifications if n["status"] == "open"],
        "complaint_categories": store.query(
            "SELECT classification, COUNT(*) n FROM cases WHERE "
            "run_id IS NULL AND is_complaint=1 GROUP BY classification "
            "ORDER BY n DESC"),
        "mdr_note": (
            "This is a simulated quality and regulatory review workflow. "
            "No reportability determination is made automatically and no "
            "filing of any kind is produced."),
    }


def audit_feed(store, *, limit: int = 200, case_id: str = "",
               event_type: str = "", actor: str = "") -> dict:
    sql = ["SELECT * FROM audit_events WHERE 1=1"]
    params: list = []
    if case_id:
        sql.append("AND case_id=?")
        params.append(case_id)
    if event_type:
        sql.append("AND event_type LIKE ?")
        params.append(f"%{event_type}%")
    if actor:
        sql.append("AND actor LIKE ?")
        params.append(f"%{actor}%")
    sql.append(f"ORDER BY id DESC LIMIT {int(limit)}")
    rows = store.query(" ".join(sql), tuple(params))
    for r in rows:
        r["detail"] = _loads(r["detail"])
    return {"events": rows,
            "event_types": [r["event_type"] for r in store.query(
                "SELECT DISTINCT event_type FROM audit_events "
                "ORDER BY event_type")]}


def triage_preview(text: str) -> dict:
    """Run the deterministic triage over arbitrary text, for the test view."""
    t = triage_mod.triage(text)
    return {**t.as_dict(),
            "requires_immediate_escalation":
                triage_mod.requires_immediate_escalation(t)}
