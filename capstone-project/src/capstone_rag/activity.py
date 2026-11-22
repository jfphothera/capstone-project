"""Persist observable execution evidence; never hidden model reasoning."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
import json
import time
import uuid

from . import store as store_mod

_current = ContextVar('capstone_activity', default=None)


def active_store():
    context = _current.get()
    return context['store'] if context else None


@contextmanager
def scope(store, investigation_id, *, conversation_id=None, case_id=None, turn_id=None):
    """Bind activity evidence to a store without selecting inference routing.

    Callers that own a longer-lived inference snapshot (notably an isolated
    experiment worker) pair this with one explicit inference.session.
    """
    token = _current.set(dict(store=store, investigation_id=investigation_id,
                              conversation_id=conversation_id, case_id=case_id,
                              turn_id=turn_id))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def serializable(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, 'as_dict'):
        return value.as_dict()
    if hasattr(value, '__dict__'):
        return value.__dict__
    return str(value)


def emit(category, summary, status='completed', evidence=None, duration_ms=None):
    context = _current.get()
    if context is None:
        return
    context['store'].insert('activity_events', {
        'at': store_mod.now(), 'investigation_id': context['investigation_id'],
        'conversation_id': context.get('conversation_id'),
        'case_id': context.get('case_id'), 'turn_id': context.get('turn_id'),
        'category': category, 'status': status, 'summary': summary,
        'duration_ms': duration_ms,
        'evidence': json.dumps(evidence, default=serializable, ensure_ascii=False),
    })


def link_case(case_id):
    from . import inference
    inference.link_case(case_id)
    context = _current.get()
    if context:
        context['case_id'] = case_id
        context['store'].execute(
            'UPDATE activity_events SET case_id=? WHERE conversation_id=? AND case_id IS NULL',
            (case_id, context.get('conversation_id')))


@contextmanager
def investigation(store, *, conversation_id=None, case_id=None, turn_id=None):
    context = dict(store=store, investigation_id='INV-' + uuid.uuid4().hex,
                   conversation_id=conversation_id, case_id=case_id, turn_id=turn_id)
    token = _current.set(context)
    start = time.perf_counter()
    emit('investigation', 'Investigation running', 'running')
    try:
        from . import inference
        with inference.session(store,context['investigation_id'],case_id,conversation_id):
            yield context
    except Exception as exc:
        emit('investigation', 'Investigation failed', 'failed', {'error': str(exc)},
             (time.perf_counter() - start) * 1000)
        raise
    else:
        emit('investigation', 'Investigation completed', duration_ms=(time.perf_counter() - start) * 1000)
    finally:
        _current.reset(token)
