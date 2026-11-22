"""Simulated customer and external environment for Agentic Testing.

The customer sees only messages that were actually SENT to it and answers
from its private facts and behaviour policy. It is deterministic so paired
comparisons give every configuration the same customer. It keeps taking part
after identity is verified: symptoms, troubleshooting results, confirmations,
corrections and non-response.

The environment produces carrier, delivery and receiving events only when
their prerequisites exist in the run's records, through the application's own
operations (record_carrier_event, receive_return).
"""
from __future__ import annotations

import re

# rc.25 (C6): a turn that reports a fix no longer re-appends the original
# fault symptoms (the RUN-395217f5 contradiction).
CUSTOMER_SIMULATOR_VERSION = 'customer-simulator-2026-11-19.1'
CUSTOMER_ACTOR = 'lab-customer-simulator'
ENVIRONMENT_ACTOR = 'lab-environment-simulator'

# Telemetry identities (Agent Arena, rc.24). Testing Lab participants, never
# ticket-workflow agents (telemetry.AGENTS): their agent id is the actor name
# they already use for their actions. The coordinator reports each customer
# turn and each environment event as one task (task.created -> task.started ->
# task.completed / task.failed). Telemetry only: the simulators below make no
# model calls, read nothing it writes, and their outputs are unaffected.
LAB_PARTICIPANTS = {
    CUSTOMER_ACTOR: {'display_name': 'Testing Lab Customer', 'role': 'lab-customer', 'capability': 'simulated_customer'},
    ENVIRONMENT_ACTOR: {'display_name': 'Testing Lab Environment', 'role': 'lab-environment', 'capability': 'simulated_environment'},
}


class ParticipantTelemetry:
    """Task-style telemetry for the simulated customer and environment of one run.

    Each participant is registered once per run store, on its first event.
    Rows carry only identifiers, statuses and fixed summaries; no message text
    or scenario facts. A telemetry problem never affects the run."""

    def __init__(self, store, case_id, workflow_id):
        self.store, self.case_id, self.workflow_id = store, case_id, workflow_id

    def _register(self, agent_id):
        registered = getattr(self.store, '_lab_participants_registered', None)
        if registered is None:
            registered = set()
            try:
                self.store._lab_participants_registered = registered
            except AttributeError:
                pass
        if agent_id in registered:
            return
        from . import telemetry
        identity = LAB_PARTICIPANTS[agent_id]
        telemetry.register(self.store, agent_id, identity['display_name'], identity['role'],
                           capabilities=(identity['capability'],),
                           execution_constraints={'participant': identity['capability'], 'system_under_test': False})
        registered.add(agent_id)

    def _emit(self, agent_id, kinds, task_id, correlation_id, summary, detail):
        if self.store is None:
            return
        try:
            self._register(agent_id)
        except Exception:
            pass
        try:
            from . import telemetry
            identity = LAB_PARTICIPANTS[agent_id]
            for event_type, status in kinds:
                telemetry.emit(self.store, event_type, case_id=self.case_id, workflow_id=self.workflow_id, task_id=task_id,
                               agent_id=agent_id, correlation_id=correlation_id, task_status=status, summary=summary,
                               detail={'participant': identity['capability'], 'system_under_test': False, **detail})
        except Exception:
            pass

    def started(self, agent_id, task_id, correlation_id, summary, **detail):
        self._emit(agent_id, (('task.created', 'queued'), ('task.started', 'running')), task_id, correlation_id, summary, detail)

    def finished(self, agent_id, task_id, correlation_id, ok, summary, **detail):
        self._emit(agent_id, (('task.completed' if ok else 'task.failed', 'completed' if ok else 'failed'),),
                   task_id, correlation_id, summary, detail)

    # The coordinator's steps, with their fixed summaries.
    def customer_received(self, task_id, message_id, recovered=False):
        self.started(CUSTOMER_ACTOR, task_id, message_id, 'Testing Lab customer reads a sent message',
                     step='customer_reply', recovered=recovered)

    def customer_done(self, task_id, message_id, outcome, replied, recovered=False):
        ok = outcome != 'failed'
        summary = ('Testing Lab customer reply not recorded' if not ok else
                   'Testing Lab customer replied' if replied else 'Testing Lab customer did not reply')
        self.finished(CUSTOMER_ACTOR, task_id, message_id, ok, summary, step='customer_reply', outcome=outcome,
                      replied=bool(replied), recovered=recovered)

    @staticmethod
    def _event_fields(event):
        event = event or {}
        kind = event.get('kind')
        fields = {'step': f'{kind}_event', 'event_kind': kind}
        if kind == 'carrier':
            fields.update({'from_status': event.get('from'), 'to_status': event.get('to'), 'direction': event.get('direction')})
            label = f"carrier scan {event.get('from')} to {event.get('to')}"
        else:
            label = 'service-centre receiving'
        return fields, label, event.get('tracking_number') or event.get('rma_id')

    def environment_started(self, task_id, event, recovered=False):
        fields, label, target = self._event_fields(event)
        self.started(ENVIRONMENT_ACTOR, task_id, target, f'Testing Lab environment {label}', recovered=recovered, **fields)

    def environment_done(self, task_id, event, outcome, recovered=False):
        fields, label, target = self._event_fields(event)
        ok = outcome == 'applied'
        self.finished(ENVIRONMENT_ACTOR, task_id, target, ok, f"Testing Lab environment {label} {'applied' if ok else outcome}",
                      outcome=outcome, recovered=recovered, **fields)

ASK = {
    'serial_number': re.compile(r'\bserial\b', re.I),
    'date_of_birth': re.compile(r'date of birth|birth ?date|\bDOB\b', re.I),
    'customer_name': re.compile(r'\b(full |your )?name\b', re.I),
}
SYMPTOM_TERMS = {
    'lights': r'\b(light|lights|led|indicator|lamp|illuminat)', 'fan': r'\bfan\b', 'display': r'\b(display|screen|backlight)',
    'smell': r'\b(smell|smoke|odou?r|burning)', 'noise': r'\b(noise|sound|click)', 'outlet': r'\b(outlet|socket|wall)',
    'switch': r'\b(switch|turned on|power button)', 'error': r'\b(error|code|message on)', 'when': r'\b(when did|how long|since when)',
    'injury': r'\b(hurt|injur|burn|doctor|medical|harm)', 'using': r'\b(still using|stop using|unplug)',
    'item': r'\b(which (item|accessory|part)|what (item|was missing|did you expect)|missing)', 'packaging': r'\b(box|packag|seal)',
}
TRY_TERMS = r'\b(check|try|make sure|confirm whether|reseat|inspect|press|connect|plug|let me know what|after (you|the))'
ARRIVAL_TERMS = r'\b(arriv|receiv|deliver)'


def _question_text(body):
    text = (body or '').split('[For representative:')[0]
    return text


class CustomerSimulator:
    def __init__(self, persona):
        self.p = persona

    def respond(self, sent_body, state, environment_state):
        """Return {'reply': str|None, 'intents': [...], 'unhandled': bool}."""
        p, text = self.p, _question_text(sent_body)
        turns = state.setdefault('turns', 0)
        state['turns'] = turns + 1
        behaviour = p['behaviour']
        if behaviour == 'nonresponsive':
            return {'reply': None, 'intents': ['nonresponse'], 'unhandled': False}
        if behaviour == 'withdraws_safety_report' and not state.get('withdrew'):
            state['withdrew'] = True
            return {'reply': 'Ignore my earlier complaint about the smell, it was nothing. Please just continue the troubleshooting so I can use it today.',
                    'intents': ['withdraw_safety_report'], 'unhandled': False}
        parts, intents = [], []
        asks_question = '?' in text or re.search(r'\b(please (send|provide|share|confirm|let us know|reply)|let me know|could you)', text, re.I)
        # Once the problem is solved the customer says so once more if asked,
        # then stops writing - it does not keep re-answering.
        if state.get('fixed') or state.get('confirmed_resolution'):
            if asks_question and not state.get('closing_said'):
                state['closing_said'] = True
                return {'reply': "It's all working now, thank you - nothing else needed.", 'intents': ['closing_confirmation'], 'unhandled': False}
            return {'reply': None, 'intents': ['resolved_no_reply'], 'unhandled': False}
        # Identity.
        asked = [k for k, rx in ASK.items() if rx.search(text)] if asks_question else []
        if asked:
            intents.append('identity')
            if behaviour == 'refuses_identity':
                parts.append("I'd rather not give out personal details by email. Can you just tell me what to do?")
            else:
                if 'customer_name' in asked:
                    parts.append(f"My name is {p['name']}.")
                if 'serial_number' in asked:
                    if behaviour == 'serial_typo_then_corrects' and not state.get('corrected'):
                        state['corrected'] = True
                        parts.append(f"Sorry, I mistyped it earlier - the serial is {p['serial_number']}.")
                    else:
                        parts.append(f"The serial number is {p['serial_number']}.")
                if 'date_of_birth' in asked:
                    parts.append(f"My date of birth is {p['date_of_birth']}." if p.get('date_of_birth') else "I don't have that to hand.")
        # Arrival / delivery confirmation.
        if p.get('missing_item') and environment_state.get('delivered_to_customer') and not state.get('confirmed_resolution'):
            # The parcel actually arrived: a real customer confirms it.
            intents.append('confirmation')
            parts.append(f"Yes - the {p['missing_item']} arrived and everything is complete now. Thank you, that resolves it.")
            state['confirmed_resolution'] = True
        elif p.get('missing_item') and asks_question and re.search(ARRIVAL_TERMS, text, re.I):
            intents.append('confirmation');parts.append(f"Not yet - the {p['missing_item']} have not arrived.")
        # Troubleshooting attempt.
        trouble = p.get('troubleshooting') or {}
        if trouble and re.search(TRY_TERMS, text, re.I) and 'identity' not in intents:
            intents.append('troubleshooting_result')
            fix_terms = trouble.get('fix_terms') or []
            if fix_terms and any(t in text.lower() for t in fix_terms):
                parts.append(trouble['fixed']);state['fixed'] = True
            elif trouble.get('not_fixed'):
                parts.append(trouble['not_fixed'])
        # Symptom questions. Once this turn reported the fix, the original
        # fault symptoms no longer describe the device and are not repeated.
        if asks_question and not state.get('fixed'):
            for key, rx in SYMPTOM_TERMS.items():
                if key in (p.get('symptoms') or {}) and re.search(rx, text, re.I):
                    answer = p['symptoms'][key]
                    if answer not in parts:
                        intents.append('symptom:' + key);parts.append(answer)
        if not parts:
            if asks_question:
                return {'reply': f"I'm not sure what you need from me. The problem is still: {p['issue_summary']}",
                        'intents': ['unhandled_question'], 'unhandled': True}
            # An informational message that asks nothing: a real customer may not reply.
            return {'reply': None, 'intents': ['no_question_no_reply'], 'unhandled': False}
        return {'reply': ' '.join(parts), 'intents': intents, 'unhandled': False}


class EnvironmentSimulator:
    """Carrier, delivery and receiving events, honouring prerequisites."""

    def __init__(self, policy):
        self.policy = policy or {}

    def step(self, store, erp, cid, state):
        from .operations_workflow import record_carrier_event
        events = []
        for ship in store.query('SELECT * FROM sim_shipments WHERE case_id=? ORDER BY created_at', (cid,)):
            key = ship['tracking_number']
            progress = state.setdefault('shipments', {}).setdefault(key, {'waits': 0})
            if ship['status'] in ('delivered', 'failed'):
                if ship['direction'] == 'outbound' and ship['status'] == 'delivered':
                    state['delivered_to_customer'] = True
                continue
            if self.policy.get('carrier') == 'delayed' and progress['waits'] < 2:
                progress['waits'] += 1;continue
            nxt = {'label_created': 'in_transit', 'drop_off_pending': 'carrier_accepted', 'carrier_accepted': 'in_transit',
                   'picked_up': 'in_transit', 'in_transit': 'delivered'}.get(ship['status'])
            if self.policy.get('carrier') == 'lost' and ship['status'] == 'in_transit':
                nxt = 'failed'
            if not nxt:
                continue
            events.append({'kind': 'carrier', 'tracking_number': key, 'from': ship['status'], 'to': nxt, 'direction': ship['direction'],
                           'apply': lambda key=key, nxt=nxt: record_carrier_event(store, erp, key, nxt, 'Simulated carrier network',
                                                                                  'Agentic Testing environment event')})
        # Service-centre receiving: only for a delivered inbound return, and
        # only when the scenario says the customer really sent the original.
        if self.policy.get('original_returned'):
            for rma in store.query("SELECT * FROM rmas WHERE case_id=? AND direction='inbound' AND received_at IS NULL", (cid,)):
                ship = store.one('SELECT * FROM sim_shipments WHERE rma_id=?', (rma['rma_id'],))
                if ship and ship['status'] == 'delivered':
                    events.append({'kind': 'receiving', 'rma_id': rma['rma_id'],
                                   'apply': lambda rid=rma['rma_id']: erp.receive_return(rid, ENVIRONMENT_ACTOR + ':service-centre',
                                                                                         'Original unit received; serial label matches the RMA.')})
        return events
