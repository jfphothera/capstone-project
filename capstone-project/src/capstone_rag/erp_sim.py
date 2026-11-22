"""Simulated ERP transactions and a mock shipping service.

Nothing here contacts a real system. No carrier request is made, no label is
valid, no goods move. What is real is the *control flow*: a proposal cannot
become a transaction without a named human approving it, an approval cannot
be applied twice, and nothing claims a state the store has not recorded.

Identifiers are fictional and linked:

    CA######   ERP call       the request made of the ERP
    R######    RMA            the inventory movement it authorised
    SHP-####   shipment       the physical movement, inbound or outbound
    SIMFX####  tracking       clearly not a real carrier's format

Idempotency is explicit. Every state-changing call takes a key derived from
the case and the intent; a repeat click, a retry or a page refresh returns
the record that already exists rather than making a second one.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import wraps
from dataclasses import dataclass
from typing import Optional

from . import config, movement as movement_mod, store as store_mod

# ERP call / RMA states
PROPOSED = "proposed"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
FAILED = "failed"
CANCELED = "canceled"

# Shipment states
LABEL_CREATED = "label_created"
IN_TRANSIT = "in_transit"
DELIVERED = "delivered"
SHIP_FAILED = "failed"

CARRIER = "SimEx Logistics (simulated)"
FACILITY = {
    "name": "Capstone Service Centre (fictional)",
    "street": "1 Example Industrial Way",
    "city": "Riverton", "state": "MN", "postcode": "00000",
}


def _key(*parts) -> str:
    return hashlib.sha1("::".join(str(p) for p in parts).encode()).hexdigest()


def _case_status(store, case_id, status, actor, why, run_id=None):
    """Imported late: caseflow imports this module, so the other direction
    has to be resolved at call time rather than at import time."""
    from . import caseflow
    return caseflow.set_status(store, case_id, status, actor, why, run_id)


# Case statuses this module sets, named here so the late import above does
# not have to happen just to read a constant.
ACTION_APPROVED_STATUS = "Action Approved"
AWAITING_STATUS = "Awaiting Customer/Shipment"
HUMAN_REVIEW_STATUS = "Human Review Needed"


@dataclass
class ErpResult:
    ok: bool
    call: Optional[dict] = None
    rma: Optional[dict] = None
    shipment: Optional[dict] = None
    reason: str = ""
    duplicate: bool = False

    def as_dict(self):
        return {"ok": self.ok, "reason": self.reason,
                "duplicate": self.duplicate,
                "call_id": (self.call or {}).get("call_id"),
                "rma_id": (self.rma or {}).get("rma_id"),
                "shipment_id": (self.shipment or {}).get("shipment_id"),
                "tracking_number": (self.shipment or {}).get(
                    "tracking_number")}


def serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.store.transaction():
            return method(self, *args, **kwargs)
    return locked


class ErpSimulator:
    def __init__(self, store=None):
        self.store = store or store_mod.get_store()

    # -- proposals -------------------------------------------------------

    def ensure_service_call(self, case_id, *, account_id=None, serial_number=None, run_id=None):
        """A service record can exist without any inventory authorization."""
        case = self.store.case(case_id)
        if not case:
            raise ValueError('A service call requires an existing case')
        with self.store._lock:
            key = _key('service_call', case_id)
            call = self.store.one('SELECT * FROM erp_calls WHERE idempotency_key=?', (key,))
            payload = {
                'reported_problem': [m['body'] for m in self.store.messages(case_id) if m['direction'] == 'inbound'],
                'investigation': self.store.checklist(case_id),
                'assessment': {k: case.get(k) for k in ('classification', 'priority', 'is_complaint', 'is_adverse_event')},
                'proposed_resolution': case.get('resolution'),
                'rma_ids': [r['rma_id'] for r in self.store.query('SELECT rma_id FROM rmas WHERE case_id=?', (case_id,))],
                'case_status': case['status'],
            }
            if call:
                self.store.update('erp_calls', 'call_id', call['call_id'], {
                    'payload': json.dumps(payload), 'account_id': account_id or case.get('account_id'),
                    'service_status': 'resolved' if case.get('resolved') else call.get('service_status') or 'active',
                    'serial_number': serial_number or case.get('serial_number')})
                self.store.audit('erp_service_call_updated', actor='agent', case_id=case_id,
                    summary=f"{call['call_id']} updated; inventory authorization unchanged")
                return self.store.one('SELECT * FROM erp_calls WHERE call_id=?', (call['call_id'],))
            call_id = self.store.new_call_id()
            self.store.insert('erp_calls', {'call_id': call_id, 'case_id': case_id, 'run_id': run_id,
                'created_at': store_mod.now(), 'call_type': 'service_call', 'status': 'open',
                'service_status': 'active',
                'account_id': account_id or case.get('account_id'),
                'serial_number': serial_number or case.get('serial_number'),
                'payload': json.dumps(payload), 'idempotency_key': key})
            self.store.audit('erp_service_call_created', actor='agent', case_id=case_id,
                summary=f'{call_id} service record created; no movement authorized', detail=payload, run_id=run_id)
            return self.store.one('SELECT * FROM erp_calls WHERE call_id=?', (call_id,))

    @serialized
    def propose_movement(self, case_id: str, decision, *, account_id=None,
                         serial_number=None, run_id=None) -> ErpResult:
        """Record an intended ERP call. Authorises nothing."""
        if not decision.needs_transaction:
            return ErpResult(False, reason=(
                f"movement '{decision.movement_type}' needs no ERP "
                f"transaction"))

        idem = _key("propose", case_id, decision.movement_type,
                    json.dumps(decision.items, sort_keys=True, default=str))
        existing = self.store.one('SELECT * FROM rmas WHERE idempotency_key=?', (idem,))
        if existing:
            call = self.store.one('SELECT * FROM erp_calls WHERE call_id=?', (existing['call_id'],))
            return ErpResult(True, call, existing, duplicate=True,
                             reason="an identical proposal already exists")

        service = self.ensure_service_call(case_id, account_id=account_id, serial_number=serial_number, run_id=run_id)
        call_id = service['call_id']
        now = store_mod.now()
        prerequisite = None
        if decision.movement_type == movement_mod.DEVICE_REPLACEMENT:
            inbound = movement_mod.MovementDecision(movement_mod.DEVICE_RETURN, movement_mod.INBOUND,
                [{'part_number': 'UNIT', 'name': 'Original device'}], rule='MR-EXCHANGE',
                reason='Original device must be returned and receiving verified before replacement dispatch',
                evidence=decision.evidence)
            original=self.store.one("SELECT * FROM rmas WHERE call_id=? AND movement_type='device_return' AND status NOT IN ('rejected','canceled') ORDER BY received_at DESC, created_at DESC LIMIT 1",(call_id,))
            prerequisite = (original['rma_id'] if original else self.propose_movement(case_id, inbound, account_id=account_id,
                serial_number=serial_number, run_id=run_id).rma['rma_id'])
        rma_id = self.store.new_rma_id()
        self.store.insert("rmas", {
            "rma_id": rma_id, "call_id": call_id, "case_id": case_id,
            "run_id": run_id, "created_at": now,
            "movement_type": decision.movement_type,
            "direction": decision.direction,
            "items": json.dumps(decision.items, default=str),
            "reason": decision.reason, "status": PROPOSED,
            "approved_by": None, "approved_at": None,
            'idempotency_key': idem, 'prerequisite_rma_id': prerequisite,
            'return_method': 'drop_off_label' if decision.direction == movement_mod.INBOUND else None,
        })
        self.store.update("cases", "case_id", case_id,
                          {"rma_needed": 1, "updated_at": now})
        self.store.audit("erp_movement_proposed", actor="ai", case_id=case_id,
                         summary=f"{call_id}/{rma_id} "
                                 f"{decision.movement_type} proposed "
                                 f"(rule {decision.rule})",
                         detail=decision.as_dict(), run_id=run_id)
        return ErpResult(True, self.store.one(
            "SELECT * FROM erp_calls WHERE call_id=?", (call_id,)),
            self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,)))

    def replacement_eligibility(self, rma):
        if rma['movement_type'] != movement_mod.DEVICE_REPLACEMENT:
            return True, 'No whole-device exchange receipt prerequisite'
        original = self.store.one('SELECT * FROM rmas WHERE rma_id=?', (rma.get('prerequisite_rma_id'),))
        if not original or original['call_id'] != rma['call_id'] or not original.get('received_at') or not original.get('received_by'):
            return False, 'Replacement blocked until named receiving confirms the linked original device was received and accepted'
        return True, f"Original return {original['rma_id']} received by {original['received_by']}"

    # -- decisions -------------------------------------------------------

    def _identity_prerequisite(self, rma, actor):
        case=self.store.case(rma['case_id']) or {}
        call=self.store.one('SELECT * FROM erp_calls WHERE call_id=?',(rma['call_id'],)) or {}
        reason=None
        if not case.get('identity_verified'):
            reason='Verify customer identity or record a named representative attestation before an account-specific inventory operation'
        elif not case.get('account_id') or call.get('account_id') != case.get('account_id'):
            reason='The service call account does not match the verified case account'
        elif rma['movement_type'] in (movement_mod.DEVICE_RETURN,movement_mod.DEVICE_REPLACEMENT) and not case.get('serial_number'):
            reason='Identify the exact original device before authorizing a whole-device movement'
        elif rma['movement_type']==movement_mod.PART_OUTBOUND and (not json.loads(rma.get('items') or '[]') or any(
                not item.get('part_number') or item.get('part_number') in ('UNKNOWN','TBD','UNSPECIFIED')
                for item in json.loads(rma.get('items') or '[]'))):
            reason='Identify a specific authorized replacement item before approving or executing a component shipment'
        if reason:
            self.store.audit('inventory_identity_prerequisite_blocked',actor=actor,case_id=rma['case_id'],summary=reason,
                detail={'rma_id':rma['rma_id'],'call_id':rma['call_id'],'procedure':'PROC-OPS-MOVEMENT'})
        return reason

    @serialized
    def approve(self, rma_id: str, authorizer: str, note: str = "",
                run_id=None, force_failure=False) -> ErpResult:
        """A named human authorises the movement, and only then is it made."""
        rma = self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
        if rma is None:
            return ErpResult(False, reason=f"no RMA {rma_id}")
        if not authorizer or not authorizer.strip():
            return ErpResult(False, reason='A named representative is required')
        identity_reason=self._identity_prerequisite(rma,authorizer)
        if identity_reason:
            return ErpResult(False,reason=identity_reason)
        eligible, why = self.replacement_eligibility(rma)
        if not eligible:
            self.store.audit('replacement_dispatch_blocked', actor=authorizer, case_id=rma['case_id'], summary=why,
                             detail={'rma_id': rma_id, 'prerequisite_rma_id': rma.get('prerequisite_rma_id')})
            return ErpResult(False, reason=why)
        if rma["status"] != PROPOSED:
            return ErpResult(
                False, reason=(f"{rma_id} is already {rma['status']}; a "
                               f"decision cannot be applied twice"),
                duplicate=True)

        now = store_mod.now()
        self.store.update("rmas", "rma_id", rma_id,
                          {"status": APPROVED, "approved_by": authorizer,
                           "approved_at": now})
        self.store.update("erp_calls", "call_id", rma["call_id"],
                          {"status": APPROVED})
        self.store.insert("approvals", {
            "approval_id": self.store.next_id("approval", "APV-{:06d}"),
            "case_id": rma["case_id"], "run_id": run_id, "at": now,
            "subject_type": "rma", "subject_id": rma_id,
            "decision": APPROVED, "authorizer": authorizer, "note": note,
        })
        self.store.audit("rma_approved", actor=authorizer,
                         case_id=rma["case_id"],
                         summary=f"{rma_id} approved", detail={"note": note},
                         run_id=run_id)
        # The movement is no longer awaiting a decision. `rma_needed` means
        # "a movement is outstanding", so it is cleared here and the outcome
        # is recorded separately - a case that once needed an RMA must not go
        # on showing an open request after it has been decided (defect C9).
        if rma["case_id"]:
            self.store.update("cases", "case_id", rma["case_id"],
                              {"rma_needed": 0, "rma_outcome": APPROVED,
                               "updated_at": now})
            # Approved but not yet shipped is a real stage, so it gets its own
            # status rather than being skipped over (defect C10).
            _case_status(
                self.store, rma["case_id"], ACTION_APPROVED_STATUS,
                authorizer,
                f"{rma_id} ({rma['movement_type']}) approved; awaiting "
                f"execution", run_id=run_id)
        self._decide_movement_checklist(rma, APPROVED, authorizer, note)
        return self.execute(rma_id, run_id=run_id, force_failure=force_failure)

    def _decide_movement_checklist(self, rma, decision, authorizer, note):
        """Keep the checklist finding and the RMA from disagreeing.

        A representative who approves or rejects the movement from the RMA
        panel has decided the `determine_product_movement` finding too. If
        that item is still undecided it is marked here, so the two controls
        cannot end up showing opposite things about the same decision.
        """
        if not rma.get("case_id"):
            return None
        item = self.store.one(
            "SELECT * FROM checklist_items WHERE case_id=? AND action=? AND "
            "approval_decision IS NULL",
            (rma["case_id"], "determine_product_movement"))
        if item is None:
            return None
        self.store.update("checklist_items", "item_id", item["item_id"], {
            "approval_decision": decision, "authorizer": authorizer,
            "decided_at": store_mod.now(),
            "feedback": note or f"decided with {rma['rma_id']}",
            "consequence": f"{rma['rma_id']} {decision}"})
        return item["item_id"]

    @serialized
    def retry(self, rma_id: str, authorizer: str, note: str = "", run_id=None):
        """Re-execute a FAILED movement under its existing, unrevoked approval.

        A retry never manufactures approval: the latest decision recorded for
        this exact RMA must be an approval (not a later rejection or cancel),
        and the retry itself is recorded as a separate, named approvals row
        that references the original. execute() re-checks identity and
        eligibility. A changed action needs a new proposal and approval.
        """
        if not authorizer.strip():
            return ErpResult(False, reason="A named person is required to retry a movement")
        rma = self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
        if rma is None:
            return ErpResult(False, reason=f"no RMA {rma_id}")
        if rma["status"] != FAILED:
            return ErpResult(False, reason=f"{rma_id} is {rma['status']}; only a failed movement can be retried")
        decision = self.store.one("SELECT approval_id, decision FROM approvals WHERE subject_type='rma' AND subject_id=? "
                                  "AND decision IN (?,?,?) ORDER BY at DESC, approval_id DESC LIMIT 1",
                                  (rma_id, APPROVED, REJECTED, CANCELED))
        if not decision or decision["decision"] != APPROVED:
            self.store.audit("rma_retry_refused", actor=authorizer, case_id=rma["case_id"],
                             summary=f"{rma_id} retry refused: no current recorded approval", detail={"rma_id": rma_id})
            return ErpResult(False, reason="Retry refused: this movement has no current recorded approval; propose and approve it again")
        now = store_mod.now()
        self.store.insert("approvals", {
            "approval_id": self.store.next_id("approval", "APV-{:06d}"),
            "case_id": rma["case_id"], "run_id": run_id, "at": now,
            "subject_type": "rma_retry", "subject_id": rma_id,
            "decision": "retry_authorized", "authorizer": authorizer,
            "note": f"retry under {decision['approval_id']}: {note}"[:500]})
        self.store.update("rmas", "rma_id", rma_id, {"status": APPROVED})
        self.store.audit("erp_movement_retry_requested", actor=authorizer, case_id=rma["case_id"],
                         summary=f"{rma_id} retried after failure under {decision['approval_id']}: {note}")
        return self.execute(rma_id, run_id=run_id)

    @serialized
    def reject(self, rma_id: str, authorizer: str, note: str = "",
               run_id=None) -> ErpResult:
        rma = self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
        if rma is None:
            return ErpResult(False, reason=f"no RMA {rma_id}")
        if rma["status"] != PROPOSED:
            return ErpResult(False, duplicate=True,
                             reason=f"{rma_id} is already {rma['status']}")
        now = store_mod.now()
        self.store.update("rmas", "rma_id", rma_id,
                          {"status": REJECTED, "approved_by": authorizer,
                           "approved_at": now})
        self.store.update("erp_calls", "call_id", rma["call_id"],
                          {"status": REJECTED,
                           "result": json.dumps({"rejected_by": authorizer,
                                                 "note": note})})
        self.store.insert("approvals", {
            "approval_id": self.store.next_id("approval", "APV-{:06d}"),
            "case_id": rma["case_id"], "run_id": run_id, "at": now,
            "subject_type": "rma", "subject_id": rma_id,
            "decision": REJECTED, "authorizer": authorizer, "note": note,
        })
        self.store.update("cases", "case_id", rma["case_id"],
                          {"rma_needed": 0, "rma_outcome": REJECTED,
                           "updated_at": now})
        self.store.audit("rma_rejected", actor=authorizer,
                         case_id=rma["case_id"],
                         summary=f"{rma_id} rejected: {note}",
                         detail={"note": note}, run_id=run_id)
        self._decide_movement_checklist(rma, REJECTED, authorizer, note)
        return ErpResult(True, reason=f"{rma_id} rejected; no finalized RMA "
                                      f"or shipping label was created")

    def cancel(self, rma_id: str, authorizer: str, note: str = "",
               run_id=None) -> ErpResult:
        """Withdraw a proposal that was never decided.

        Used when a rejected *finding* invalidates the movement it proposed
        (defect C8). A canceled proposal can no longer be approved, which is
        the whole point: a rejection that left the Approve button working was
        not a rejection.
        """
        rma = self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
        if rma is None:
            return ErpResult(False, reason=f"no RMA {rma_id}")
        if rma["status"] != PROPOSED:
            return ErpResult(False, duplicate=True,
                             reason=f"{rma_id} is already {rma['status']}")
        now = store_mod.now()
        self.store.update("rmas", "rma_id", rma_id,
                          {"status": CANCELED, "approved_by": authorizer,
                           "approved_at": now})
        self.store.update("erp_calls", "call_id", rma["call_id"],
                          {"status": CANCELED,
                           "result": json.dumps({"canceled_by": authorizer,
                                                 "note": note})})
        self.store.insert("approvals", {
            "approval_id": self.store.next_id("approval", "APV-{:06d}"),
            "case_id": rma["case_id"], "run_id": run_id, "at": now,
            "subject_type": "rma", "subject_id": rma_id,
            "decision": CANCELED, "authorizer": authorizer, "note": note,
        })
        if rma["case_id"]:
            self.store.update("cases", "case_id", rma["case_id"],
                              {"rma_needed": 0, "rma_outcome": CANCELED,
                               "updated_at": now})
        self.store.audit("rma_canceled", actor=authorizer,
                         case_id=rma["case_id"],
                         summary=f"{rma_id} canceled: {note}",
                         detail={"note": note}, run_id=run_id)
        return ErpResult(True, reason=(f"{rma_id} canceled; it can no longer "
                                       f"be approved or executed"))

    # -- execution -------------------------------------------------------

    @serialized
    def execute(self, rma_id: str, run_id=None, force_failure=False
                ) -> ErpResult:
        """Create the fictional transaction. Only runs for an approved RMA."""
        rma = self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,))
        if rma is None:
            return ErpResult(False, reason=f"no RMA {rma_id}")
        eligible, why = self.replacement_eligibility(rma)
        if not eligible:
            self.store.audit('replacement_dispatch_blocked', actor='erp_sim', case_id=rma['case_id'], summary=why)
            return ErpResult(False, reason=why)
        if rma["status"] not in (APPROVED, EXECUTED):
            return ErpResult(False, reason=(
                f"{rma_id} is {rma['status']}; only an approved movement can "
                f"be executed"))

        existing = self.store.one(
            "SELECT * FROM sim_shipments WHERE rma_id=?", (rma_id,))
        if existing:
            return ErpResult(
                True, self.store.one("SELECT * FROM erp_calls WHERE call_id=?",
                                     (rma["call_id"],)),
                rma, existing, duplicate=True,
                reason="this movement has already been executed")

        identity_reason=self._identity_prerequisite(rma,'erp_sim')
        if identity_reason:
            return ErpResult(False,reason=identity_reason)

        case = self.store.case(rma["case_id"]) or {}
        customer = self.store.customer(case.get("account_id")) or {}
        items = json.loads(rma["items"] or "[]")
        outbound = rma["direction"] == movement_mod.OUTBOUND

        ship_to = ({"name": customer.get("customer_name"),
                    "street": customer.get("street"),
                    "city": customer.get("city"),
                    "state": customer.get("state")} if outbound else FACILITY)
        ship_from = FACILITY if outbound else {
            "name": customer.get("customer_name"),
            "street": customer.get("street"),
            "city": customer.get("city"), "state": customer.get("state")}

        now = store_mod.now()
        missing_address = outbound and not ship_to.get("street")
        if force_failure or missing_address:
            reason = ("no delivery address on the customer record" if
                      missing_address else "simulated carrier failure")
            self.store.update("erp_calls", "call_id", rma["call_id"],
                              {"status": FAILED,
                               'service_status': 'shipping_failed',
                               "result": json.dumps({"error": reason}),
                               "executed_at": now})
            self.store.update("rmas", "rma_id", rma_id, {"status": FAILED})
            if rma["case_id"]:
                # A failed movement stays failed and stays outstanding. It is
                # not quietly presented as done, and it goes back to a person.
                self.store.update("cases", "case_id", rma["case_id"],
                                  {"rma_needed": 1, "rma_outcome": FAILED,
                                   "updated_at": now})
                _case_status(self.store, rma["case_id"],
                             HUMAN_REVIEW_STATUS, "erp_sim",
                             f"{rma_id} execution failed: {reason}",
                             run_id=run_id)
            self.store.audit("erp_movement_failed", actor="erp_sim",
                             case_id=rma["case_id"],
                             summary=f"{rma_id} execution failed: {reason}",
                             detail={"error": reason}, run_id=run_id)
            return ErpResult(False, reason=reason)

        shipment_id = self.store.new_shipment_id()
        tracking = self.store.new_tracking_number()
        label_directory = config.LABEL_DIR if os.path.basename(self.store.path) == 'operations.db' else self.store.path + '.labels'
        label_path = _write_label(shipment_id, tracking, rma, ship_to,
                                  ship_from, items, directory=label_directory)

        self.store.insert("sim_shipments", {
            "shipment_id": shipment_id, "rma_id": rma_id,
            "case_id": rma["case_id"], "run_id": run_id, "created_at": now,
            "direction": rma["direction"], "carrier": CARRIER,
            "service": "Simulated Ground", "tracking_number": tracking,
            "label_path": label_path,
            "contents": json.dumps(items, default=str),
            "ship_to": json.dumps(ship_to), "ship_from": json.dumps(ship_from),
            "status": LABEL_CREATED, "failure_reason": None,
            "idempotency_key": _key("ship", rma_id),
            'return_method': rma.get('return_method') if not outbound else None,
            'pickup_attempts': 0,
        })
        self.store.insert("shipment_events", {
            "shipment_id": shipment_id, "at": now, "status": LABEL_CREATED,
            "location": (ship_from or {}).get("city"),
            "note": "simulated label generated",
        })
        self.store.update("erp_calls", "call_id", rma["call_id"], {
            "status": EXECUTED, "executed_at": now,
            "result": json.dumps({"rma_id": rma_id,
                                  "shipment_id": shipment_id,
                                  "tracking_number": tracking})})
        self.store.update("rmas", "rma_id", rma_id, {"status": EXECUTED})
        self.store.update('erp_calls', 'call_id', rma['call_id'], {
            'service_status': 'awaiting_return' if not outbound else 'outbound_label_created'})
        self.store.update("cases", "case_id", rma["case_id"], {
            "tracking_number": tracking, "tracking_status": LABEL_CREATED,
            "rma_needed": 0, "rma_outcome": EXECUTED,
            "updated_at": now})
        if rma["case_id"]:
            # Action Approved was recorded a moment ago; moving on from it is
            # itself audited rather than silently overwritten.
            _case_status(self.store, rma["case_id"], AWAITING_STATUS,
                         "erp_sim",
                         f"{rma_id} executed; shipment {shipment_id} "
                         f"tracking {tracking}", run_id=run_id)
        self.store.audit("erp_movement_executed", actor="erp_sim",
                         case_id=rma["case_id"],
                         summary=f"{rma_id} executed; shipment {shipment_id} "
                                 f"tracking {tracking}",
                         detail={"direction": rma["direction"],
                                 "items": items}, run_id=run_id)
        from .operations_workflow import shipping_draft
        shipping_draft(self.store, tracking, 'created')
        if not outbound:
            method_status = 'pickup_scheduled' if rma.get('return_method') == 'carrier_pickup' else 'drop_off_pending'
            self.store.insert('shipment_events', {'shipment_id': shipment_id, 'at': store_mod.now(),
                'status': method_status, 'note': 'Simulated return method arranged'})
        return ErpResult(
            True,
            self.store.one("SELECT * FROM erp_calls WHERE call_id=?",
                           (rma["call_id"],)),
            self.store.one("SELECT * FROM rmas WHERE rma_id=?", (rma_id,)),
            self.store.one("SELECT * FROM sim_shipments WHERE shipment_id=?",
                           (shipment_id,)))

    # -- tracking --------------------------------------------------------

    def track(self, tracking_number: str) -> Optional[dict]:
        """Look up a simulated shipment. Returns only recorded state."""
        s = self.store.one(
            "SELECT * FROM sim_shipments WHERE tracking_number=?",
            (tracking_number,))
        if not s:
            return None
        s["events"] = self.store.query(
            "SELECT * FROM shipment_events WHERE shipment_id=? ORDER BY id",
            (s["shipment_id"],))
        s["contents"] = json.loads(s["contents"] or "[]")
        s["ship_to"] = json.loads(s["ship_to"] or "{}")
        s["ship_from"] = json.loads(s["ship_from"] or "{}")
        return s

    @serialized
    def advance(self, tracking_number: str, status: str, location: str = "",
                note: str = "") -> Optional[dict]:
        """Move a simulated shipment to its next state."""
        s = self.store.one(
            "SELECT * FROM sim_shipments WHERE tracking_number=?",
            (tracking_number,))
        if not s:
            return None
        transitions = {
            # A recorded carrier delivery scan can arrive without every prior
            # scan. It still never establishes verified physical receiving.
            'label_created': {'pickup_scheduled', 'drop_off_pending', 'carrier_accepted', 'in_transit', 'delivered'},
            'pickup_scheduled': {'pickup_attempt_failed', 'picked_up'},
            'pickup_attempt_failed': {'pickup_attempt_failed', 'picked_up'},
            'picked_up': {'in_transit'}, 'drop_off_pending': {'carrier_accepted'},
            'carrier_accepted': {'in_transit'}, 'in_transit': {'delivered'}, 'delivered': set(),
        }
        if status == s['status'] and status != 'pickup_attempt_failed':
            return self.track(tracking_number)
        if status == 'received_accepted':
            raise ValueError('Verified receiving requires a named receiving confirmation, not a carrier scan')
        if s.get('return_method') == 'carrier_pickup' and s['status'] == 'label_created':
            allowed = {'pickup_attempt_failed', 'picked_up', 'pickup_scheduled'}
        else:
            allowed = transitions.get(s['status'], set())
            if s.get('return_method') == 'drop_off_label':
                allowed = allowed - {'pickup_scheduled', 'picked_up', 'pickup_attempt_failed'}
        if s['status'] not in ('delivered', 'failed'):
            allowed = allowed | {'failed'}
        if status not in allowed:
            self.store.audit('shipment_transition_blocked',actor='shipping_sim',case_id=s['case_id'],
                summary=f"{tracking_number}: {s['status']} -> {status} blocked",detail={'procedure':'PROC-OPS-RETURN-SHIPPING','shipment_id':s['shipment_id']})
            raise ValueError(f"Invalid shipment transition {s['status']} -> {status}")
        if status in ('pickup_attempt_failed', 'picked_up'):
            attempts = s.get('pickup_attempts') or 0
            if s.get('return_method') != 'carrier_pickup' or attempts >= 3:
                self.store.audit('pickup_attempt_blocked',actor='shipping_sim',case_id=s['case_id'],
                    summary=f'{tracking_number}: pickup blocked after {attempts} attempts',detail={'procedure':'PROC-OPS-RETURN-SHIPPING','shipment_id':s['shipment_id']})
                raise ValueError('At most three pickup attempts are permitted; arrange a new reviewed method')
            self.store.update('sim_shipments', 'shipment_id', s['shipment_id'], {'pickup_attempts': attempts + 1})
        now = store_mod.now()
        self.store.update("sim_shipments", "shipment_id", s["shipment_id"],
                          {"status": status})
        self.store.insert("shipment_events", {
            "shipment_id": s["shipment_id"], "at": now, "status": status,
            "location": location, "note": note})
        if s["case_id"]:
            self.store.update("cases", "case_id", s["case_id"],
                              {"tracking_status": status, "updated_at": now})
        self.store.audit("shipment_status_changed", actor="shipping_sim",
                         case_id=s["case_id"],
                         summary=f"{tracking_number} -> {status}",
                         detail={"location": location, "note": note, 'shipment_id':s['shipment_id'],
                                 'rma_id':s['rma_id'], 'procedure':'PROC-OPS-RETURN-SHIPPING'},
                         run_id=s["run_id"])
        if status in ('pickup_attempt_failed', 'delivered', 'picked_up'):
            from .operations_workflow import shipping_draft
            shipping_draft(self.store, tracking_number, status + ':' + str((s.get('pickup_attempts') or 0)+1))
        if status == DELIVERED:
            rma = self.store.one('SELECT call_id FROM rmas WHERE rma_id=?', (s['rma_id'],))
            self.store.update('erp_calls','call_id',rma['call_id'],{
                'service_status': 'awaiting_receiving' if s['direction']==movement_mod.INBOUND else 'awaiting_customer_confirmation'})
        return self.track(tracking_number)

    @serialized
    def select_return_method(self, rma_id, method, authorizer, note):
        rma = self.store.one('SELECT * FROM rmas WHERE rma_id=?', (rma_id,))
        if not rma or rma['direction'] != movement_mod.INBOUND:
            raise ValueError('Select the return method on an inbound return')
        if method not in ('carrier_pickup', 'drop_off_label') or not authorizer.strip() or not note.strip():
            raise ValueError('Choose a supported method and supply a named representative and rationale')
        if rma['status'] != PROPOSED:
            ship=self.store.one('SELECT * FROM sim_shipments WHERE rma_id=?',(rma_id,))
            recovery=(rma['status']==EXECUTED and ship and ship['return_method']=='carrier_pickup'
                and ship['status']=='pickup_attempt_failed' and (ship.get('pickup_attempts') or 0)>=3 and method=='drop_off_label')
            if not recovery:
                raise ValueError('An executed return method can change only to reviewed drop-off after three failed pickups')
            blocked=self._identity_prerequisite(rma,authorizer)
            if blocked:
                raise ValueError(blocked)
            self.store.update('sim_shipments','shipment_id',ship['shipment_id'],{'return_method':method,'status':'drop_off_pending'})
            self.store.insert('shipment_events',{'shipment_id':ship['shipment_id'],'at':store_mod.now(),
                'status':'drop_off_pending','note':f'Reviewed alternative after three failed pickups: {authorizer}: {note}'})
            self.store.update('cases','case_id',rma['case_id'],{'tracking_status':'drop_off_pending','updated_at':store_mod.now()})
        self.store.update('rmas', 'rma_id', rma_id, {'return_method': method})
        self.store.audit('return_method_selected', actor=authorizer, case_id=rma['case_id'],
                         summary=f'{rma_id}: {method}', detail={'before': rma.get('return_method'), 'after': method, 'note': note,
                         'procedure': 'PROC-OPS-RETURN-SHIPPING'})
        if rma['status']==EXECUTED:
            from .operations_workflow import shipping_draft
            shipping_draft(self.store,ship['tracking_number'],'reviewed_dropoff_alternative')

    @serialized
    def receive_return(self, rma_id, authorizer, note):
        rma = self.store.one('SELECT * FROM rmas WHERE rma_id=?', (rma_id,))
        ship = self.store.one('SELECT * FROM sim_shipments WHERE rma_id=?', (rma_id,))
        if not rma or rma['direction'] != movement_mod.INBOUND or not ship or ship['status'] != DELIVERED:
            raise ValueError('Receiving requires a delivered inbound shipment; delivery alone does not confirm receipt')
        if not authorizer.strip() or len(note.strip()) < 3:
            raise ValueError('Named receiving and acceptance evidence are required')
        if rma.get('received_at'):
            return self.track(ship['tracking_number'])
        self.store.update('rmas', 'rma_id', rma_id, {'received_at': store_mod.now(), 'received_by': authorizer, 'receipt_note': note})
        self.store.insert('shipment_events', {'shipment_id': ship['shipment_id'], 'at': store_mod.now(),
            'status': 'received_accepted', 'note': f'{authorizer}: {note}'})
        self.store.audit('return_received_accepted', actor=authorizer, case_id=rma['case_id'],
            summary=f'{rma_id} physically received and accepted; linked replacement may now be reviewed',
            detail={'note': note, 'call_id': rma['call_id'], 'procedure': 'PROC-OPS-RETURN-SHIPPING'})
        self.store.update('erp_calls','call_id',rma['call_id'],{'service_status':'return_received_replacement_review'})
        from .operations_workflow import shipping_draft
        shipping_draft(self.store, ship['tracking_number'], 'received_accepted')
        return self.track(ship['tracking_number'])


# --- fictional label -------------------------------------------------------

LABEL_TEMPLATE = """\
+----------------------------------------------------------------+
|  *** SIMULATED LABEL - NOT VALID FOR SHIPPING - DO NOT USE ***  |
|  {carrier:<60}|
+----------------------------------------------------------------+
|  DIRECTION : {direction:<49}|
|  RMA       : {rma_id:<49}|
|  ERP CALL  : {call_id:<49}|
|  CASE      : {case_id:<49}|
+----------------------------------------------------------------+
|  FROM: {from_name:<55}|
|        {from_street:<55}|
|        {from_city:<55}|
|                                                                 |
|  TO:   {to_name:<55}|
|        {to_street:<55}|
|        {to_city:<55}|
+----------------------------------------------------------------+
|  CONTENTS:                                                      |
{contents}
+----------------------------------------------------------------+
|  TRACKING: {tracking:<51}|
|                                                                 |
|   |||| | ||| || |||| | || ||| | |||| || | ||| |||| | || ||||    |
|            (decorative - encodes nothing)                       |
+----------------------------------------------------------------+
|  This label is part of an educational simulation. It carries no |
|  postage, no real tracking, and no carrier has been contacted.  |
+----------------------------------------------------------------+
"""


def _write_label(shipment_id, tracking, rma, ship_to, ship_from, items, directory=None) -> str:
    directory = directory or config.LABEL_DIR
    os.makedirs(directory, exist_ok=True)
    lines = []
    for it in items or [{"name": "unspecified"}]:
        lines.append(f"|    - {str(it.get('name'))[:40]:<40} "
                     f"{str(it.get('part_number', '')):<16}|")
    body = LABEL_TEMPLATE.format(
        carrier=CARRIER, direction=rma["direction"], rma_id=rma["rma_id"],
        call_id=rma["call_id"], case_id=rma["case_id"] or "-",
        from_name=str(ship_from.get("name") or "-")[:55],
        from_street=str(ship_from.get("street") or "-")[:55],
        from_city=f"{ship_from.get('city') or '-'} "
                  f"{ship_from.get('state') or ''}"[:55],
        to_name=str(ship_to.get("name") or "-")[:55],
        to_street=str(ship_to.get("street") or "-")[:55],
        to_city=f"{ship_to.get('city') or '-'} "
                f"{ship_to.get('state') or ''}"[:55],
        contents="\n".join(lines), tracking=tracking)
    path = os.path.join(directory, f"{shipment_id}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path
