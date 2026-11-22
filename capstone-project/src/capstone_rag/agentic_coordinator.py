"""Agentic Testing lab coordinator: drives one ticket through its lifecycle.

Loop (each domain action journaled before and after it happens):

1. the ticket has entered through the real intake and the configured workflow
   has run (experiment_worker);
2. pending human decisions go to the reviewer agent, one at a time, with the
   complete artifact and its evidence (withheld material visible and
   requestable; approval of uninspected content refused);
3. permitted reviewer commands are applied through the application, each
   command journaled separately;
4. messages actually SENT go to the simulated customer;
5. the reply enters through the application and the case is re-investigated
   under the run's selected methodology;
6. external events (carrier, receiving) are produced when prerequisites exist;
7. until a classified terminal state or an execution limit.

Recovery (rc.21): every started-but-not-completed step is reconciled with an
action-specific check of its exact effect (action, target and actor). An
effect that is present is marked applied; an effect that is absent is
completed now; an uncertain one stays explicitly unresolved. Counters and
participant state are rebuilt from the journal and the domain records, and
the original limits and wall-clock deadline are kept.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import agentic_reviewer as reviewer_mod
from .agentic_customer import CUSTOMER_ACTOR, ENVIRONMENT_ACTOR, CustomerSimulator, EnvironmentSimulator, ParticipantTelemetry
from .lab_faults import crash_point

DEFAULT_LIMITS = {'max_customer_turns': 6, 'max_reviewer_decisions': 16, 'max_cycles': 14, 'max_minutes': 25}
MAX_EVIDENCE_ROUNDS = 3
SHIPMENT_ORDER = ['label_created', 'drop_off_pending', 'pickup_scheduled', 'carrier_accepted', 'picked_up', 'in_transit', 'delivered']


class Journal:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / 'lifecycle.jsonl'
        self.state_path = Path(run_dir) / 'lifecycle-state.json'
        self.applied, self.started, self.unresolved = {}, {}, {}
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    self._index(json.loads(line))

    def _index(self, row):
        key, status = row['key'], row['status']
        if status == 'applied':
            self.applied[key] = row;self.started.pop(key, None);self.unresolved.pop(key, None)
        elif status == 'started':
            if key not in self.applied:self.started[key] = row
        elif status == 'unresolved':
            self.unresolved[key] = row;self.started.pop(key, None)
        elif status == 'abandoned':
            self.started.pop(key, None)

    def write(self, key, status, **detail):
        row = {'key': key, 'status': status, 'at': time.time(), **detail}
        with open(self.path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, default=str) + '\n');stream.flush()
        self._index(row)
        return row

    def load_state(self):
        return json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}

    def save_state(self, state):
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, default=str), encoding='utf-8');tmp.replace(self.state_path)

    def records(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _sha(text):
    return hashlib.sha256((text or '').encode()).hexdigest()[:16]


# --- action-specific effect checks -------------------------------------------------

def effect(store, cid, cmd):
    """'present' | 'absent' | 'uncertain' (+ reason) for one command's exact effect."""
    action, target, who = cmd.get('action'), cmd.get('target_id'), cmd.get('authorizer')
    one = store.one
    if action in ('send_email', 'reject_email', 'revise_email'):
        m = one('SELECT * FROM messages WHERE message_id=?', (target,))
        if not m:
            return 'uncertain', 'message not found'
        if m['status'] == 'draft':
            return 'absent', 'draft still pending'
        if action == 'send_email':
            return ('present', 'sent by this reviewer') if m['status'] == 'sent' and m['approved_by'] == who else ('uncertain', f"message is {m['status']} by {m['approved_by']}")
        if action == 'reject_email':
            return ('present', 'rejected by this reviewer') if m['status'] == 'rejected' and m['approved_by'] == who else ('uncertain', f"message is {m['status']}")
        if m['status'] == 'rejected' and m['approved_by'] == who:
            rec = one("SELECT 1 FROM recommendations WHERE case_id=? AND decided_by=? AND original_payload=?", (cid, who, m['body']))
            newer = one("SELECT 1 FROM messages WHERE case_id=? AND direction='outbound' AND author='ai (after correction)' AND created_at>=?", (cid, m['approved_at'] or ''))
            return ('present', 'original rejected and corrected draft created') if rec and newer else ('uncertain', 'original rejected but corrected draft missing (partial)')
        return 'uncertain', f"message is {m['status']}"
    if action in ('approve_movement', 'reject_movement', 'retry_movement'):
        r = one('SELECT * FROM rmas WHERE rma_id=?', (target,))
        if not r:
            return 'uncertain', 'movement not found'
        if action == 'approve_movement':
            if r['status'] == 'proposed':return 'absent', 'still proposed'
            return ('present', 'approved by this reviewer') if r['approved_by'] == who else ('uncertain', f"status {r['status']} by {r['approved_by']}")
        if action == 'reject_movement':
            if r['status'] == 'proposed':return 'absent', 'still proposed'
            by_me = one("SELECT 1 FROM audit_events WHERE case_id=? AND actor=? AND summary LIKE ?", (cid, who, f'%{target}%'))
            return ('present', 'rejected by this reviewer') if r['status'] in ('rejected', 'canceled', 'cancelled') and by_me else ('uncertain', f"status {r['status']}")
        if r['status'] == 'failed':return 'absent', 'still failed'
        by_me = one("SELECT 1 FROM audit_events WHERE case_id=? AND actor=? AND summary LIKE ?", (cid, who, f'%{target}%'))
        return ('present', 'retried by this reviewer') if by_me else ('uncertain', f"status {r['status']}")
    if action == 'determine_qa':
        q = one('SELECT * FROM qa_notifications WHERE notification_id=?', (target,))
        if not q:return 'uncertain', 'notification not found'
        if q['status'] == 'open':return 'absent', 'still open'
        return ('present', 'determined by this reviewer') if q['determined_by'] == who and q['determination'] == cmd.get('determination') else ('uncertain', f"determined by {q['determined_by']}")
    if action in ('attest_identity', 'decline_identity'):
        item = one('SELECT * FROM checklist_items WHERE item_id=?', (target,))
        if not item:return 'uncertain', 'item not found'
        if not item['approval_decision']:return 'absent', 'undecided'
        want = 'approved' if action == 'attest_identity' else 'rejected'
        return ('present', 'decided by this reviewer') if item['approval_decision'] == want and item['authorizer'] == who else ('uncertain', f"{item['approval_decision']} by {item['authorizer']}")
    if action in ('confirm_revision', 'reject_revision'):
        case = store.case(cid) or {}
        if not case.get('revision_confirmation'):return 'absent', 'no revision decision'
        want = 'confirmed' if action == 'confirm_revision' else 'rejected'
        return ('present', 'decided by this reviewer') if case['revision_confirmation'] == want and case['revision_confirmed_by'] == who else ('uncertain', 'decided differently')
    if action in ('resolve_case', 'close_unresolved'):
        case = store.case(cid) or {}
        if not case.get('closed_at'):return 'absent', 'case open'
        closed_by_me = one("SELECT 1 FROM audit_events WHERE case_id=? AND event_type='case_closed' AND actor=?", (cid, who))
        want = 1 if action == 'resolve_case' else 0
        return ('present', 'closed by this reviewer') if closed_by_me and int(case.get('resolved') or 0) == want else ('uncertain', 'closed differently')
    if action == 'assign':
        case = store.case(cid) or {}
        mine = one("SELECT 1 FROM audit_events WHERE case_id=? AND event_type='case_assigned' AND actor=? AND summary=?", (cid, who, f"assigned to {cmd.get('assignee')}"))
        if mine and case.get('assigned_to') == cmd.get('assignee'):return 'present', 'assigned by this reviewer'
        if not mine:return 'absent', 'no assignment by this reviewer'
        return 'uncertain', 'assignment changed afterwards'
    if action == 'add_note':
        return ('present', 'note recorded') if one('SELECT 1 FROM case_events WHERE command_id=?', (cmd.get('command_id'),)) else ('absent', 'no note')
    if action == 'add_customer_reply':
        key = hashlib.sha256((cid + target + (cmd.get('body') or '').strip()).encode()).hexdigest()
        return ('present', 'reply recorded') if one('SELECT 1 FROM messages WHERE idempotency_key=?', (key,)) else ('absent', 'no reply')
    if action == 'investigate_followup':
        last = one("SELECT message_id FROM messages WHERE case_id=? AND direction='inbound' ORDER BY created_at DESC LIMIT 1", (cid,))
        run = one('SELECT status FROM workflow_runs WHERE case_id=? AND message_id=? ORDER BY at DESC LIMIT 1', (cid, (last or {}).get('message_id')))
        if not run:return 'absent', 'no workflow for the latest message'
        if run['status'] in ('completed', 'paused'):return 'present', 'workflow ' + run['status']
        return 'interrupted', 'workflow ' + run['status']
    if action == 'carrier':
        s = one('SELECT status FROM sim_shipments WHERE tracking_number=?', (target,))
        if not s:return 'uncertain', 'shipment not found'
        reached = one('SELECT 1 FROM shipment_events e JOIN sim_shipments s ON s.shipment_id=e.shipment_id WHERE s.tracking_number=? AND e.status=?', (target, cmd['to']))
        if reached:return 'present', 'carrier event recorded'
        if s['status'] == cmd.get('from'):return 'absent', 'shipment unchanged'
        return 'uncertain', f"shipment is {s['status']}"
    if action == 'receiving':
        r = one('SELECT received_at,received_by FROM rmas WHERE rma_id=?', (target,))
        if not r:return 'uncertain', 'movement not found'
        if not r['received_at']:return 'absent', 'not received'
        return ('present', 'received') if r['received_by'] == cmd.get('authorizer') else ('uncertain', f"received by {r['received_by']}")
    return 'uncertain', f'no reconciliation rule for {action}'


def approval_blocked(choice, withheld_ids, presented_ids):
    """Draft parts withheld from every presentation so far block approval."""
    unseen = {i for i in (withheld_ids or ()) if i.startswith('draft:part')} - set(presented_ids or ())
    return sorted(unseen) if choice == 'approve_send' else []


def classify(store, cid, state, *, limit_hit=None, error=None, unresolved=None):
    case = store.case(cid) or {}
    if error:
        return 'execution_failure', str(error)
    if case.get('closed_at'):
        return ('resolved', case.get('resolution')) if case.get('resolved') else ('closed_unresolved', case.get('resolution'))
    if state.get('escalated_to'):
        return 'escalated', f"assigned to {state['escalated_to']}"
    if unresolved:
        return 'limit_reached', 'recovery left unresolved actions: ' + ', '.join(unresolved)
    if state.get('revision_unresolved'):
        r = state['revision_unresolved']
        return 'unresolved_needs_authorized_review', f"revision of {r.get('message_id')} stopped: {r.get('reason')}"
    if case.get('safety_hold'):
        # A held safety report that was neither escalated nor closed is not a
        # routine limit: the required escalation did not happen.
        return 'unresolved_needs_authorized_review', 'safety hold without a recorded escalation' + (f' ({limit_hit})' if limit_hit else '')
    if limit_hit:
        return 'limit_reached', limit_hit
    if state.get('awaiting_customer'):
        return 'pending_external_response', 'customer has not replied to the last sent message'
    return 'limit_reached', 'no further progress possible (no pending decision, customer turn or external event)'


def _rebuild_state(store, cid, journal, saved, limits):
    """Counters and participant state from durable records, not the state file."""
    records = [r for r in journal.records() if r.get('status') == 'applied']
    decisions = [r for r in records if r.get('kind') == 'reviewer' and r.get('decision_point')]
    customer = [r for r in records if r.get('kind') == 'customer' and '#' not in r['key']]
    escalated = next((cmd.get('assignee') for d in decisions for cmd in d.get('commands') or [] if cmd.get('action') == 'assign'
                      and effect(store, cid, cmd)[0] == 'present'), None)
    state = {
        'started_wall': saved.get('started_wall') or time.time(),
        'limits': saved.get('limits') or limits,
        'cycles': saved.get('cycles', 0),
        'reviewer_decisions': len(decisions),
        'customer_turns': sum(1 for r in customer if r.get('reply')),
        'delivered_messages': [r['target_id'] for r in customer],
        'customer': (customer[-1].get('customer_state_after') if customer else None) or saved.get('customer') or {},
        'environment': {**(saved.get('environment') or {}),
                        'delivered_to_customer': bool(store.one("SELECT 1 FROM sim_shipments WHERE case_id=? AND direction='outbound' AND status='delivered'", (cid,)))},
        'disposition_round': sum(1 for d in decisions if d['decision_point'] == 'disposition'),
        'awaiting_customer': bool(customer and 'nonresponse' in (customer[-1].get('intents') or [])),
        'escalated_to': escalated,
        'evidence_rounds': saved.get('evidence_rounds', {}),
        'revision_unresolved': saved.get('revision_unresolved'),
    }
    return state


def run_lifecycle(store, flow, cid, run, run_dir, apply_command, *, resume=False):
    from . import activity
    scenario = run['scenario']
    journal = Journal(run_dir)
    saved = journal.load_state()
    limits = {**DEFAULT_LIMITS, **(run.get('agentic_limits') or {})}
    state = _rebuild_state(store, cid, journal, saved, limits)
    limits = state['limits']                       # original limits survive restarts
    deadline = state['started_wall'] + 60 * float(limits['max_minutes'])  # original budget, not reset
    customer = CustomerSimulator(scenario['customer'])
    environment = EnvironmentSimulator(scenario.get('environment'))
    from . import inference
    # Agent Arena telemetry of the simulated customer and environment (rc.24):
    # workflow_id is the run id, as for the reviewer; nothing here feeds back.
    lab = ParticipantTelemetry(store, cid, (inference.current() or {}).get('investigation_id') or run.get('id'))
    model = (run.get('profile', {}).get('routes', {}).get('lab_reviewer') or {}).get('model')
    architecture = run.get('architecture')

    def apply(cmd):
        value = apply_command(store, flow, cid, cmd, architecture)
        from dataclasses import asdict, is_dataclass
        return asdict(value) if is_dataclass(value) else value

    def run_commands(key, commands, record, start=0, responses=None):
        responses = list(responses or [])
        for i, cmd in enumerate(commands):
            if i < start or f'{key}#{i}' in journal.applied:
                continue
            journal.write(f'{key}#{i}', 'started', kind='command', command=cmd)
            crash_point('before_domain_action')
            value = None
            try:
                value = apply(cmd)
                blocked = isinstance(value, dict) and value.get('ok') is False
                responses.append({'action': cmd['action'], 'blocked': blocked, 'response': _summarise(value)})
            except Exception as exc:
                blocked = True;responses.append({'action': cmd['action'], 'blocked': True, 'response': {'error': str(exc)[:300]}})
            if cmd['action'] == 'revise_email':crash_point('revision_after_action')
            crash_point('after_domain_action')
            journal.write(f'{key}#{i}', 'applied', kind='command', command=cmd, blocked=blocked, response=responses[-1]['response'])
            if cmd['action'] == 'assign' and not blocked:
                state['escalated_to'] = cmd['assignee']
            if cmd['action'] == 'revise_email' and isinstance(value, dict) and value.get('unresolved'):
                # rc.25 (C2): a revision that stopped without progress ends the
                # attempt truthfully; it is not retried until the budget is gone.
                state['revision_unresolved'] = {'message_id': cmd.get('target_id'), 'reason': value.get('stop_reason') or value.get('reason'),
                                                'revision_id': value.get('revision_id')}
            if i < len(commands) - 1:crash_point('between_commands')
        return responses

    def reviewer_step(point):
        from .agentic_review_session import review_decision
        view = reviewer_mod.observe(store, cid, point)
        view.pop('_presentation', None)
        if point['kind'] == 'draft':
            point['reviewed_sha256'] = hashlib.sha256((store.one('SELECT body FROM messages WHERE message_id=?', (point['target_id'],)) or {}).get('body', '').encode()).hexdigest()
        point['reviewed_case_state'] = reviewer_mod.case_state(store, cid)
        record = {'kind': 'reviewer', 'decision_point': point['kind'], 'target_id': point['target_id'], 'view_hash': reviewer_mod.view_hash(view),
                  'reviewer_actor': reviewer_mod.actor(model), 'reviewer_model': model, 'policy_version': reviewer_mod.REVIEWER_POLICY_VERSION,
                  'reviewed_sha256': point.get('reviewed_sha256'),
                  'observation_summary': {'case_status': view['case'].get('status'), 'identity_verified': view['case'].get('identity_verified'),
                                          'safety_hold': view['case'].get('safety_hold'), 'options': view['options'],
                                          'draft_validation': (view.get('draft') or {}).get('validation_disposition'),
                                          'draft_kind': (view.get('draft') or {}).get('kind'), 'draft_characters': (view.get('draft') or {}).get('characters'),
                                          'proposal': (view.get('proposal') or {}).get('movement_type')}}
        try:
            # A persistent review session carries findings across every call
            # (sections, evidence requests, final decision) for this artifact version.
            with reviewer_mod.review_step(point['key']):   # telemetry task id of the reviewer's calls
                decision, executed_model, info = review_decision(store, cid, point, run_dir)
            record.update(decision=decision, executed_model=executed_model, review_session=info['session'],
                          presentation=info['presentation'], multipart_review=info['multipart'])
        except Exception as exc:
            record.update(decision=None, reviewer_error=str(exc)[:400])
            journal.write(point['key'], 'applied', **record, outcome='reviewer_failed')
            activity.emit('lab_reviewer', 'Reviewer agent failed to decide', 'failed', record)
            return record
        choice = decision.get('decision')
        if info['blocked']:
            # Approval refused: not every part reviewed, findings did not fit,
            # open items unresolved, or no synthesis. Reconsidered once.
            record.update(review_blocked=info['blocked'], responses=[{'action': 'approve_send', 'blocked': True, 'response': {'ok': False, 'reason': info['blocked']}}])
            journal.write(point['key'], 'applied', **record, outcome='blocked')
            return record
        if choice == 'request_evidence':
            journal.write(point['key'], 'applied', **record, outcome='deferred', note='evidence-request rounds exhausted without a decision')
            return record
        point['presented_source_ids'] = sorted({i.split(':', 1)[1] for i in (info.get('presentation') or {}).get('presented_ids', [])
                                                if i.startswith('source:')})
        try:
            commands = reviewer_mod.command_for(point, decision, model)
        except Exception as exc:
            record.update(outcome='invalid_decision', error=str(exc)[:300])
            journal.write(point['key'], 'applied', **record)
            return record
        commands = commands if isinstance(commands, list) else ([commands] if commands else [])
        record['commands'] = commands
        journal.write(point['key'], 'started', **record)
        responses = run_commands(point['key'], commands, record)
        record.update(responses=responses, outcome='blocked' if any(r['blocked'] for r in responses) else ('deferred' if not commands else 'applied'))
        journal.write(point['key'], 'applied', **record)
        crash_point('after_journal_applied')
        activity.emit('lab_reviewer', f"Reviewer {choice} on {point['kind']}", 'completed', record)
        return record

    def customer_continue(key, message_id, record):
        """Workflow continuation for a delivered customer reply (resumable)."""
        if f'{key}#workflow' in journal.applied:
            return
        journal.write(f'{key}#workflow', 'started', kind='command')
        status, why = effect(store, cid, {'action': 'investigate_followup'})
        if status != 'present':
            try:
                value = apply({'action': 'investigate_followup', 'authorizer': 'lab-coordinator', 'trigger': 'customer_follow_up' if status == 'absent' else 'recovery',
                               'rationale': 'Process the customer reply through the same case workflow.'})
                record['workflow'] = _summarise(value)
            except Exception as exc:
                record['workflow_error'] = str(exc)[:300]
        journal.write(f'{key}#workflow', 'applied', kind='command', prior_state=status)

    # --- recovery: reconcile every started step --------------------------------
    reconciliation = []
    for key, row in sorted(journal.started.items(), key=lambda kv: kv[1].get('at', 0)):
        if '#' in key:
            continue  # handled through its parent step
        kind = row.get('kind')
        if kind == 'reviewer':
            done, unresolved = [], None
            for i, cmd in enumerate(row.get('commands') or []):
                if f'{key}#{i}' in journal.applied:
                    done.append({'command': i, 'state': 'journaled'});continue
                status, why = effect(store, cid, cmd)
                if status == 'present':
                    journal.write(f'{key}#{i}', 'applied', kind='command', command=cmd, reconciled=True, evidence=why)
                    if cmd['action'] == 'assign':state['escalated_to'] = cmd['assignee']
                    done.append({'command': i, 'state': 'effect present', 'evidence': why})
                elif status == 'absent':
                    responses = run_commands(key, row['commands'], row, start=i)
                    done.append({'command': i, 'state': 'completed during recovery', 'responses': responses});break
                else:
                    unresolved = why;journal.write(f'{key}#{i}', 'unresolved', kind='command', command=cmd, reason=why);break
            if unresolved:
                journal.write(key, 'unresolved', kind='reviewer', reason=unresolved, decision_point=None)
                reconciliation.append({'key': key, 'result': 'unresolved', 'reason': unresolved, 'commands': done})
            else:
                journal.write(key, 'applied', **{k: v for k, v in row.items() if k not in ('key', 'status', 'at')}, recovered=True,
                              outcome=row.get('outcome') or 'applied', responses=row.get('responses') or [])
                reconciliation.append({'key': key, 'result': 'recovered', 'commands': done})
        elif kind == 'customer':
            lab.customer_received(key, row['target_id'], recovered=True)
            cmd = {'action': 'add_customer_reply', 'target_id': row['target_id'], 'body': row.get('reply') or ''}
            status, why = effect(store, cid, cmd)
            if status == 'absent':
                apply({**cmd, 'authorizer': CUSTOMER_ACTOR, 'rationale': 'Simulated customer reply (recovery: not recorded before the interruption).'})
            journal.write(f'{key}#reply', 'applied', kind='command', reconciled=status)
            customer_continue(key, row['target_id'], row)
            journal.write(key, 'applied', **{k: v for k, v in row.items() if k not in ('key', 'status', 'at')}, recovered=True, outcome='applied')
            lab.customer_done(key, row['target_id'], 'applied', bool(row.get('reply')), recovered=True)
            reconciliation.append({'key': key, 'result': 'recovered', 'reply': status, 'evidence': why})
        elif kind == 'environment':
            event = row.get('event') or {}
            cmd = {'action': event.get('kind'), 'target_id': event.get('tracking_number') or event.get('rma_id'), 'to': event.get('to'),
                   'from': event.get('from'), 'authorizer': ENVIRONMENT_ACTOR + ':service-centre'}
            status, why = effect(store, cid, cmd)
            if status == 'present':
                journal.write(key, 'applied', **{k: v for k, v in row.items() if k not in ('key', 'status', 'at')}, recovered=True, outcome='applied')
                lab.environment_started(key, event, recovered=True);lab.environment_done(key, event, 'applied', recovered=True)
                reconciliation.append({'key': key, 'result': 'effect present', 'evidence': why})
            elif status == 'absent':
                journal.write(key, 'abandoned', kind='environment', reason='event not applied before interruption; regenerated by the environment')
                reconciliation.append({'key': key, 'result': 'not applied; environment will produce it again', 'evidence': why})
            else:
                journal.write(key, 'unresolved', kind='environment', reason=why)
                lab.environment_started(key, event, recovered=True);lab.environment_done(key, event, 'unresolved', recovered=True)
                reconciliation.append({'key': key, 'result': 'unresolved', 'reason': why})
        else:
            journal.write(key, 'unresolved', kind=kind, reason='no reconciliation rule for this step kind')
            reconciliation.append({'key': key, 'result': 'unresolved', 'reason': 'unknown step kind'})
    state = _rebuild_state(store, cid, journal, {**saved, **state}, limits)
    journal.save_state(state)
    unresolved_keys = [k for k in journal.unresolved if '#' not in k]

    limit_hit = error = None
    try:
        while True:
            if time.time() > deadline:
                limit_hit = 'wall-clock limit'; break
            if state['cycles'] >= limits['max_cycles']:
                limit_hit = 'cycle limit'; break
            state['cycles'] += 1
            progressed = False
            for _ in range(12):
                points = []
                for p in reviewer_mod.pending_decisions(store, cid):
                    if p['key'] not in journal.applied and p['key'] not in journal.unresolved:
                        points.append(p);continue
                    prior = journal.applied.get(p['key']) or {}
                    retry_key = p['key'] + ':reconsider'
                    if prior.get('outcome') == 'blocked' and retry_key not in journal.applied:
                        points.append({**p, 'key': retry_key, 'previous_attempt': {
                            'decision': (prior.get('decision') or {}).get('decision'),
                            'application_response': [r.get('response') for r in prior.get('responses') or []]}})
                if not points:
                    break
                point = points[0]
                if state['reviewer_decisions'] >= limits['max_reviewer_decisions']:
                    limit_hit = 'reviewer decision limit'; break
                state['reviewer_decisions'] += 1
                reviewer_step(point);progressed = True
                journal.save_state(state)
                if state.get('escalated_to') or state.get('revision_unresolved') or (store.case(cid) or {}).get('closed_at'):
                    break
            if limit_hit or state.get('escalated_to') or state.get('revision_unresolved') or (store.case(cid) or {}).get('closed_at'):
                break
            sent = store.query("SELECT * FROM messages WHERE case_id=? AND direction='outbound' AND status='sent' AND channel!='qa_notification' "
                               "ORDER BY approved_at,created_at", (cid,))
            for message in [m for m in sent if m['message_id'] not in state['delivered_messages']]:
                if state['customer_turns'] >= limits['max_customer_turns']:
                    limit_hit = 'customer turn limit'; break
                state['delivered_messages'].append(message['message_id'])
                lab.customer_received('customer:' + message['message_id'], message['message_id'])
                reply = customer.respond(message['body'], state['customer'], state['environment'])
                key = 'customer:' + message['message_id']
                record = {'kind': 'customer', 'target_id': message['message_id'], 'actor': CUSTOMER_ACTOR, 'intents': reply['intents'],
                          'unhandled': reply['unhandled'], 'message_hash': _sha(message['body']), 'reply': reply['reply'],
                          'customer_state_after': dict(state['customer'])}
                if not reply['reply']:
                    state['awaiting_customer'] = 'nonresponse' in reply['intents']
                    journal.write(key, 'applied', **record, outcome='no_reply')
                    lab.customer_done(key, message['message_id'], 'no_reply', False)
                    continue
                state['awaiting_customer'] = False;state['customer_turns'] += 1
                journal.write(key, 'started', **record)
                cmd = {'action': 'add_customer_reply', 'target_id': message['message_id'], 'body': reply['reply'],
                       'authorizer': CUSTOMER_ACTOR, 'rationale': 'Simulated customer reply (Agentic Testing).'}
                reported = False
                try:
                    record['application'] = _summarise(apply(cmd))
                    journal.write(f'{key}#reply', 'applied', kind='command')
                    lab.customer_done(key, message['message_id'], 'applied', True);reported = True   # the reply is in the case
                    crash_point('customer_after_reply')
                    customer_continue(key, message['message_id'], record)
                    record['outcome'] = 'applied'
                except Exception as exc:
                    record.update(outcome='failed', error=str(exc)[:300])
                    if not reported:
                        lab.customer_done(key, message['message_id'], 'failed', True)
                journal.write(key, 'applied', **record)
                progressed = True
                journal.save_state(state)
            if limit_hit:
                break
            from .erp_sim import ErpSimulator
            for event in environment.step(store, ErpSimulator(store), cid, state['environment']):
                key = f"environment:{event['kind']}:{event.get('tracking_number') or event.get('rma_id')}:{event.get('to', 'received')}"
                if key in journal.applied or key in journal.unresolved:
                    continue
                record = {'kind': 'environment', 'actor': ENVIRONMENT_ACTOR, 'event': {k: v for k, v in event.items() if k != 'apply'}}
                journal.write(key, 'started', **record)
                lab.environment_started(key, record['event'])
                try:
                    result = event['apply']();record.update(outcome='applied', result=_summarise(result))
                    if event['kind'] == 'carrier' and event['to'] == 'delivered' and event.get('direction') == 'outbound':
                        state['environment']['delivered_to_customer'] = True
                except Exception as exc:
                    record.update(outcome='blocked', error=str(exc)[:300])
                lab.environment_done(key, record['event'], record['outcome'])
                crash_point('environment_after_event')
                journal.write(key, 'applied', **record)
                progressed = True
            journal.save_state(state)
            if progressed:
                continue
            if state['reviewer_decisions'] >= limits['max_reviewer_decisions']:
                limit_hit = 'reviewer decision limit'; break
            if state['awaiting_customer']:
                break
            state['disposition_round'] += 1
            if state['disposition_round'] > 3:
                break
            case = store.case(cid) or {}
            point = {'kind': 'disposition', 'target_id': cid, 'key': f"disposition:{case.get('case_version')}:{state['disposition_round']}"}
            state['reviewer_decisions'] += 1
            record = reviewer_step(point)
            journal.save_state(state)
            if (record.get('decision') or {}).get('decision') == 'defer':
                break
    except Exception as exc:  # recorded; never hidden
        error = exc
    terminal, reason = classify(store, cid, state, limit_hit=limit_hit, error=error, unresolved=unresolved_keys)
    state['terminal'] = {'state': terminal, 'reason': reason}
    journal.save_state(state)
    return {'terminal': terminal, 'terminal_reason': reason, 'reconciliation': reconciliation, 'unresolved_recovery': unresolved_keys,
            'state': state, 'journal': journal.records(), 'limits': limits, 'deadline_wall': deadline}


def _summarise(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v)[:200]) for k, v in list(value.items())[:12]}
    return str(value)[:300]
