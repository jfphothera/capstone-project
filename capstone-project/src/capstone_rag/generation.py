"""Response generation.

Model generation is served only by the authenticated Inference Gateway. A
labelled deterministic writer remains available for bounded non-model text
assembly, but routed transport failures stop the session and can never select
it as a substitute for a failed model call.

Abstentions are always produced by deterministic code, in both modes. When the
system has no applicable approved source, the safest possible generator is one
that cannot invent anything.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import threading
import time
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

from . import config, inference


def _cap(purpose, default):
    """rc.27: per-purpose output allowance of the operational ticketing profile (else the code default)."""
    from . import operational_profile
    return operational_profile.cap(purpose, default)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_MODEL_LOCK = threading.RLock()
_CASE_DEADLINE = contextvars.ContextVar('case_deadline',default=None)

INDEPENDENT_REVIEW_CONTRACT=('The passed flag judges the CUSTOMER DRAFT, not whether you completed the review. '
    'Set passed=false if the draft has any factual, safety or authorization problem. '
    'issues contains only actual problems, never successful checks or restated evidence. '
    'If the draft passes, return passed=true with issues=[]. Be concise.')

def review_conclusion(proposal):
    """Fail closed on contradictory review flags, retaining the raw conclusion."""
    if not isinstance(proposal.get('passed'),bool) or not isinstance(proposal.get('issues'),list) or any(not isinstance(x,str) for x in proposal['issues']):raise ValueError('Invalid independent reviewer schema')
    issues=[x.strip() for x in proposal['issues'] if x.strip()]
    consistent=not (proposal['passed'] and issues)
    return {**proposal,'model_reported_passed':proposal['passed'],'issues':issues,
        'contract_consistent':bool(consistent),'passed':bool(proposal['passed'] and not issues),
        'contract_note':'A model cannot pass a draft while reporting actual problems. Raw response remains in inference evidence.'}

@contextmanager
def case_budget(seconds):
    token=_CASE_DEADLINE.set(time.monotonic()+seconds)
    try:yield
    finally:_CASE_DEADLINE.reset(token)


@dataclass
class Generation:
    text: str
    mode: str          # "llm" | "template" | "deterministic-abstention"
    engine: str        # human-readable description of what produced the text
    prompt: str = ""


# --- local model plumbing --------------------------------------------------

def _post(path: str, payload: dict, timeout: float):
    deadline=_CASE_DEADLINE.get()
    if deadline is not None:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Shared case inference budget exhausted')
        timeout=min(timeout,remaining)
    if inference.current():return inference.infer(path,payload,timeout)
    raise RuntimeError('Model generation requires a pinned inference session; live investigations and Research Lab workers must supply their approved Gateway profile snapshot')



def llm_available() -> bool:
    """Route eligibility in a pinned session; actual POST proves availability.

    Startup, activation and experiment preflight discover installed models.
    Repeating a short /tags probe before every routed POST can falsely reject
    a busy endpoint without even recording the real inference attempt. Routed
    transport errors propagate through the session and cannot select templates.
    """
    if inference.current():
        state=inference.current();role=inference._ROLE.get();route=state['snapshot']['routes'].get(role,state['snapshot']['routes']['investigation'])
        if not route.get('enabled',True):return False
        return bool(route.get('model'))
    try:
        registry=inference.Registry();profile=registry.active();route=profile['routes']['investigation'];health=registry.health(profile['default_endpoint'] if route['endpoint']=='default' else route['endpoint'])
        return health['reachable'] and route['model'] in {m['name'] for m in health['installed_models']}
    except Exception:return False



def model_status():
    registry=inference.Registry();profile=registry.active();route=profile['routes']['investigation'];health=registry.health(profile['default_endpoint'] if route['endpoint']=='default' else route['endpoint'])
    return {'model':route['model'],'profile_id':profile['id'],'profile_name':profile['name'],'endpoint':health['url'],
        'ollama_available':health['reachable'],'installed':route['model'] in {m['name'] for m in health['installed_models']},
        'loaded':route['model'] in {m['name'] for m in health['loaded_models']},'error':health['last_error'],
        'note':'Installed/loaded are separate; actual per-role usage is recorded on each call.'}



class _MediatedOnly(Exception):
    """Internal signal: skip native tool calling for this selection."""


TOOL_SELECTION_SYSTEM=('You are the local customer-service investigation agent. '
    'Consult the authorized internal procedure evidence and current facts. '
    'Choose exactly one available tool to advance the actual customer request. '
    'Ask a symptom question if vague; consult a public manual for ordinary operation; '
    'use protected records only when verified; propose but never approve inventory actions. '
    'Return a tool call, not customer prose or reasoning.')


def _native_tool_call(message, procedures, facts, tools):
    """One native tool-calling request on the active route's model."""
    try:
        response = _post('/api/chat', {
            'model': inference.model(), 'stream': False, 'think': False,
            'messages': [{'role': 'system', 'content': TOOL_SELECTION_SYSTEM},
                         {'role': 'user', 'content': json.dumps({'request': message, 'facts': facts,
                          'authorized_operational_procedures': procedures}, default=str)}],
            'tools': tools, 'options': {'temperature': 0, 'num_predict': _cap('native_tool_selection', 128)}}, timeout=config.OLLAMA_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:raise
        return None, 'native_tools_unsupported'
    except OSError as exc:
        if not inference.native_tools_rejected(exc):raise
        return None, 'native_tools_unsupported'
    calls = response.get('message', {}).get('tool_calls') or []
    call = calls[0].get('function', {}) if calls else {}
    if call.get('name') not in {t['function']['name'] for t in tools}:
        return None, 'no_authorized_tool_call'
    return call, None


def tool_call_contract(call, tools, mechanism):
    """rc.25 (T2): attempted vs contract-valid vs executed tool calls.

    The whole call (name AND arguments) is checked against the offered tool's
    declared parameter schema. Model-supplied arguments are never executed:
    the workflow acts on trusted case facts, so an invalid call can at most
    select an offered tool name, and it is recorded as contract-invalid.
    """
    declared = {t['function']['name']: t['function'].get('parameters') or {'type': 'object', 'properties': {}} for t in tools}
    arguments = call.get('arguments') or {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return {'call_attempted': True, 'contract_valid': False, 'argument_errors': ['arguments are not JSON'],
                    'executed_tool': call.get('name'), 'model_arguments_executed': False, 'argument_source': 'trusted_case_facts'}
    model_call = mechanism in ('native', 'delegated_native', 'native_then_mediated')
    errors = schema_errors(arguments, declared.get(call.get('name')) or {}) if model_call else []
    return {'call_attempted': True, 'contract_valid': not errors, 'argument_errors': errors[:6],
            'extra_arguments': sorted(set(arguments) - set((declared.get(call.get('name')) or {}).get('properties') or {})) if model_call else [],
            'executed_tool': call.get('name'), 'model_arguments_executed': False, 'argument_source': 'trusted_case_facts'}


def _record_tool_selection(record):
    from . import activity
    activity.emit('tool_interaction', 'Tool selection: ' + (record.get('tool') or 'none') + ' via ' + record['mechanism'],
                  'completed' if record.get('tool') else 'failed', record)


@inference.task('investigation')
def choose_support_tool(message, procedures, facts, tools, *, mechanism='auto'):
    """Ask a model for a real, bounded tool selection; never grant authority.

    The selection mechanism is DECLARED on the investigation route
    (``tool_strategy``) or passed explicitly, and is never switched silently:

    * ``native``    native tool calling on the role's own model;
    * ``mediated``  the role's model chooses a validated tool name through
                    structured output; application code executes it;
    * ``delegated`` a separately configured tool-capable model (route
                    ``tool_delegate``) makes the native tool call.

    ``auto`` (legacy, no declared strategy) tries native and then mediated on
    the same model, and records which one produced the selection. Every
    selection attempt is recorded as a ``tool_interaction`` event.
    """
    from . import activity
    strategy = mechanism if mechanism != 'auto' else inference.route_setting('tool_strategy', 'auto')
    names = [t['function']['name'] for t in tools]
    record = {'strategy': strategy, 'mechanism': strategy, 'selecting_role': inference.current_role(),
              'selecting_model': inference.model(), 'delegated_model': None, 'tools_offered': names,
              'tool': None, 'arguments': {}, 'error': None}
    if strategy == 'auto':
        # rc.25 (T1): the legacy automatic mode never sends native tools to a
        # model whose capability evidence says it has none; recorded as such.
        from .model_evidence import native_tools_evidence
        native, basis = native_tools_evidence(inference.model())
        if native is False:
            strategy = 'mediated'
            record.update(strategy='mediated', mechanism='mediated', auto_resolution='native skipped: ' + basis)
    activity.emit('model', 'Tool selection waiting for shared inference slot', 'queued', {'model': inference.model()})
    with _MODEL_LOCK:
        start = time.perf_counter()
        activity.emit('model', 'Model support tool selection requested', 'awaiting_model_response',
                      {'model': inference.model(), 'tools': names, 'strategy': strategy,
                       'think': False, 'num_predict': 128, 'purpose': 'agentic_tool_selection'})
        try:
            if strategy == 'delegated':
                with inference.specialist('tool_delegate'):
                    record.update(mechanism='delegated_native', delegated_model=inference.model())
                    call, failure = _native_tool_call(message, procedures, facts, tools)
                if failure:
                    record['error'] = failure;_record_tool_selection(record);return None
                call['_selection_mode'] = 'delegated_native_tool_call'
            elif strategy == 'native':
                record['mechanism'] = 'native'
                call, failure = _native_tool_call(message, procedures, facts, tools)
                if failure:
                    # Declared native: no silent switch to another mechanism.
                    record['error'] = failure;_record_tool_selection(record);return None
            else:
                call, failure = (None, 'mediated_selection_requested') if strategy == 'mediated' else _native_tool_call(message, procedures, facts, tools)
                if failure and strategy == 'auto':
                    record['native_failure'] = failure
                    activity.emit('model', 'Native tool response did not contain an authorized tool call', 'rejected',
                                  {'purpose': 'agentic_tool_selection', 'reason': failure})
                if strategy == 'auto' and call:
                    record['mechanism'] = 'native'
                calls = [call] if call else []
                call = call or {}
            if call.get('name') not in set(names):
                record['mechanism'] = 'mediated' if strategy == 'mediated' else 'native_then_mediated'
                activity.emit('model','Model structured tool-selection correction requested','awaiting_model_response',
                    {'model':inference.model(),'purpose':'agentic_tool_selection','num_predict':_cap('mediated_tool_selection',96),'selection_kind':'structured_model_choice'})
                corrected=_post('/api/chat',{'model':inference.model(),'stream':False,'think':False,
                    'messages':[{'role':'system','content':'Choose the next authorized investigation tool for this customer request. Return only the tool name in the name field. Use the operational procedure evidence and facts. Do not explain.'},
                        {'role':'user','content':json.dumps({'request':message,'facts':facts,'procedures':procedures,'available_tools':names},default=str)}],
                    'format':{'type':'object','properties':{'name':{'type':'string','enum':names}},'required':['name'],'additionalProperties':False},
                    'options':{'temperature':0,'num_predict':_cap('mediated_tool_selection',96)}},timeout=config.OLLAMA_TIMEOUT)
                choice=json.loads(corrected.get('message',{}).get('content',''))['name']
                if choice not in names:
                    raise ValueError('model correction selected an unavailable tool')
                call={'name':choice,'arguments':{},'_selection_mode':'qwen3_structured_tool_selection'}
            record.update(tool=call['name'], arguments=call.get('arguments') or {}, duration_ms=round((time.perf_counter()-start)*1000,3))
            record.update(tool_call_contract(call, tools, record['mechanism']))
            call['_tool_record'] = record
            _record_tool_selection(record)
            activity.emit('model', 'Model returned a tool selection', 'validating',
                          {'model': inference.model(), 'tool_call': {k: v for k, v in call.items() if k != '_tool_record'},
                         'selection_kind':call.get('_selection_mode','native_tool_call'),
                         'purpose': 'agentic_tool_selection'}, (time.perf_counter()-start)*1000)
            return call
        except Exception as exc:
            record['error'] = str(exc)[:300];_record_tool_selection(record)
            activity.emit('model', 'Tool selection failed; using authorized document plan', 'failed',
                          {'error': str(exc), 'model': inference.model()},
                          (time.perf_counter()-start)*1000)
            return None


# Model ends the email with this marker and the server stops there, which
# keeps the model's post-hoc commentary out of the reply.
END_MARKER = "---END---"


@inference.task('customer_communication')
def llm_complete(system: str, user: str, prefill: str = "") -> str:
    from . import activity
    activity.emit('model', 'Generation waiting for the shared local inference slot', 'queued',
                  {'model': inference.model()})
    with _MODEL_LOCK:
        started = time.perf_counter()
        activity.emit('model', 'Model generation request initiated', 'awaiting_model_response',
                      {'model': inference.model(), 'temperature': 0.2,
                       'num_predict': 600, 'think': False, 'endpoint': '/api/chat'})
        try:
            text = _llm_complete(system, user, prefill)
        except Exception as exc:
            activity.emit('model', 'Local generation failed', 'failed',
                          {'model': inference.model(), 'error': str(exc)},
                          (time.perf_counter() - started) * 1000)
            raise
        activity.emit('model', 'Local generation returned; validation pending', 'validating',
                      {'model': inference.model(), 'response_characters': len(text)},
                      (time.perf_counter() - started) * 1000)
        if re.search(r'we are given|decided_response|conversation history|the task is|output only|the instruction says|we must|the user asks|we are to output', text, re.I):
            activity.emit('model', 'Model output rejected: task commentary instead of a customer response', 'rejected',
                          {'model': inference.model(), 'response_characters': len(text),
                           'response_omitted': 'task commentary is not customer response evidence'})
            raise ValueError('Local model returned task commentary; customer response blocked')
        return text


def _llm_complete(system: str, user: str, prefill: str = "") -> str:
    """One chat completion against the local model.

    Three things are needed to get a clean reply out of Model 4B on Ollama
    0.40.x, all of them learned by testing against this machine:

    1. "think": false. Without it the server returns reasoning in a separate
       `thinking` field and leaves `content` empty until the reasoning budget
       is spent, which on CPU blows the timeout.
    2. An assistant prefill. With `think` off the model otherwise writes a
       paragraph of analysis before the email; seeding the greeting makes it
       start inside the email instead.
    3. A stop marker. The model keeps going after the sign-off, so it is asked
       to emit END_MARKER and the server stops there.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    out = _post(
        "/api/chat",
        {
            "model": inference.model(),
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.2,
                "num_predict": _cap("grounded_writer", 600),
                "stop": [END_MARKER],
            },
        },
        timeout=config.OLLAMA_TIMEOUT,
    )
    body = out.get("message", {}).get("content", "")
    return _clean(prefill + body)


_GREETING_RE = re.compile(r"^\s*(hello|hi|dear|good morning|good afternoon)\b",
                          re.IGNORECASE)
# If the stop marker is ever missed, these are where the model starts talking
# about the task instead of to the customer.
_COMMENTARY_RE = re.compile(
    r"^\s*(okay|ok|alright|now|so|let me|looking at|the user|we are given|"
    r"i need to|i should|first,|note:|wait)\b",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    """Trim the model's output down to the customer-facing email."""
    text = _THINK_RE.sub("", text or "").strip()
    text = text.split(END_MARKER)[0].strip()
    if not text:
        return ""

    lines = text.splitlines()

    # Drop any analysis that precedes the greeting.
    for i, line in enumerate(lines):
        if _GREETING_RE.match(line):
            lines = lines[i:]
            break

    # Drop any commentary that follows the sign-off.
    for i, line in enumerate(lines):
        if i and _COMMENTARY_RE.match(line) and len(line.split()) > 4:
            lines = lines[:i]
            break

    out = "\n".join(lines).strip()
    return re.sub(r"\[(Your Name|Customer Name|Agent Name)\]",
                  "Customer Support", out)


# --- prompts ---------------------------------------------------------------

GROUNDED_SYSTEM = """\
You are a customer support agent for a fictional medical-device company in an \
educational demonstration.

Output ONLY the finished email to the customer. Do not explain your reasoning, \
do not restate the rules, do not quote the extracts back, do not write any \
preamble or commentary. Your entire output is the email itself, starting with \
"Hello" and ending with a sign-off.

Content rules:
- Use ONLY the approved procedure extracts given to you. Every troubleshooting \
instruction you write must come from those extracts.
- Do not add steps, part numbers, fuse ratings, timings or measurements that \
are not in the extracts.
- Do not mention any device model or hardware revision other than the \
confirmed one.
- Keep document IDs and scope paragraphs out of the customer prose. Source
  identifiers are recorded separately by the application.
- Never tell the customer to open the enclosure, bypass a fuse, or use a \
metallic probe.
- Address the customer by the name you are given. Never write a placeholder \
such as [Customer Name].
- 60 to 110 words. Acknowledge the symptom, give the next safe check, and ask for
  its result before dependent checks. Do not copy scope paragraphs or section headings.

End the email with exactly these three lines and nothing after them:
Kind regards,
Customer Support
---END---"""

UNGROUNDED_SYSTEM = """\
You are a customer support agent for a fictional medical-device company in an \
educational demonstration.

You have NO approved service procedure for this device.

Output ONLY the finished email to the customer. Do not explain your reasoning, \
do not restate the rules, do not write any preamble or commentary. Your entire \
output is the email itself, starting with "Hello" and ending with a sign-off.

Content rules:
- You must NOT give device-specific troubleshooting steps. You have no approved \
source for them, so anything specific would be guesswork.
- Do not invent procedure IDs, part numbers, fuse ratings, interlock locations \
or step sequences.
- You may acknowledge the problem, confirm what the account record shows, and \
say what happens next.
- Address the customer by the name you are given. Never write a placeholder \
such as [Customer Name].
- 80 to 150 words. Plain prose.

End the email with exactly these three lines and nothing after them:
Kind regards,
Customer Support
---END---"""


def _sources_block(accepted_chunks) -> str:
    out = []
    for c in accepted_chunks:
        out.append(
            f"[{c.procedure_id} | {c.document_id} v{c.document_version} | "
            f"{c.model} Rev {'/'.join(c.applicable_revisions)} | "
            f"section: {c.section}]\n{c.text}"
        )
    return "\n\n".join(out)


# --- public API ------------------------------------------------------------

CUSTOMER_SYSTEM = """You are a helpful customer-service assistant. Compose the next
reply from the customer context and authorized manual evidence.
Acknowledge the actual symptom in ordinary language. Ask a useful question when
necessary. Give only the next applicable safe check and ask what the customer
observes before a dependent check. Do not dump manual sections.
Customer-reported facts may be acknowledged as reports, not verified diagnoses.
Operating instructions must come only from the supplied authorized sections.
Honor every safety restriction and prerequisite. Never invent a repair, approval,
account fact, tracking number, measurement, or cause. Do not expose document IDs,
scope statements, classification, internal working notes, or technical status.
Use 35-90 words, plain sentences, no headings, numbered sections, or email sign-off.
Write only the customer reply. End with ---END---."""


class RoleOutputError(ValueError):
    """A structured role output that is unusable, with an explicit category.

    ``category``: ``truncated`` (cut off at the output limit, finish reason
    length - its own category, never a wrong decision), ``parse`` (not JSON),
    ``schema`` (complete JSON that violates the declared schema).
    Transport failures are not RoleOutputError; they keep their own class.
    """
    def __init__(self, category, message, *, purpose=None, finish_reason=None, raw=None, attempts=0):
        super().__init__(f'{purpose or "role"} output {category}: {message}')
        self.category, self.purpose, self.finish_reason, self.attempts = category, purpose, finish_reason, attempts
        self.raw_excerpt = (raw or '')[:600]   # protected diagnostic: inference evidence, never customer text

    def as_dict(self):
        return {'category': self.category, 'purpose': self.purpose, 'finish_reason': self.finish_reason,
                'attempts': self.attempts, 'message': str(self)[:300]}


def schema_errors(value, schema, path='$'):
    """Full (bounded) JSON-schema check for the subset used by role contracts."""
    errors = []
    kind = schema.get('type')
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: {value!r} not in enum')
    if kind == 'object':
        if not isinstance(value, dict):
            return [f'{path}: expected object']
        props = schema.get('properties') or {}
        for key in schema.get('required', []):
            if key not in value:errors.append(f'{path}.{key}: required')
        if schema.get('additionalProperties') is False:
            errors += [f'{path}.{k}: not allowed' for k in value if k not in props]
        for key, item in value.items():
            if key in props:errors += schema_errors(item, props[key], f'{path}.{key}')
    elif kind == 'array':
        if not isinstance(value, list):
            return [f'{path}: expected array']
        if 'maxItems' in schema and len(value) > schema['maxItems']:errors.append(f'{path}: more than {schema["maxItems"]} items')
        for i, item in enumerate(value):errors += schema_errors(item, schema.get('items') or {}, f'{path}[{i}]')
    elif kind == 'string':
        # Length is not an error: the grammar already bounds it, and a few
        # characters over a cap is not a failed decision (rc.25 T2).
        if not isinstance(value, str):errors.append(f'{path}: expected string')
    elif kind == 'boolean' and not isinstance(value, bool):errors.append(f'{path}: expected boolean')
    elif kind in ('integer', 'number') and (isinstance(value, bool) or not isinstance(value, (int, float))):errors.append(f'{path}: expected {kind}')
    return errors


@inference.task('structured')
def compose_structured(system, context, schema, *, purpose, max_tokens=320, timeout=None):
    """Bounded specialist proposals; schema structure never confers authority.

    rc.25 (T2): the whole object is checked against its schema; a failure is a
    RoleOutputError with category length / parse / schema after one bounded
    repair attempt, and the finish reason and a protected excerpt are kept.
    """
    from . import activity
    with _MODEL_LOCK:
        started=time.perf_counter()
        activity.emit('model',f'Model {purpose} requested','awaiting_model_response',
                      {'model':inference.model(),'purpose':purpose,'num_predict':max_tokens,'think':False})
        try:
            request={'model':inference.model(),'stream':False,'think':False,
                'messages':[{'role':'system','content':system+' Return only the requested JSON object. No private reasoning transcript.'},
                            {'role':'user','content':json.dumps(context)}],
                'format':schema,'options':{'temperature':0.1,'num_predict':max_tokens}}
            for attempt in range(2):
                result=_post('/api/chat',request,timeout=timeout or config.OLLAMA_TIMEOUT)
                raw=(result.get('message') or {}).get('content','')
                finish=result.get('done_reason')
                try:
                    try:
                        value=json.loads(raw)
                    except (ValueError,TypeError) as exc:
                        raise RoleOutputError('truncated' if finish=='length' else 'parse',str(exc),purpose=purpose,finish_reason=finish,raw=raw,attempts=attempt+1) from exc
                    problems=schema_errors(value,schema) if isinstance(value,dict) else ['$: expected object']
                    if problems:
                        raise RoleOutputError('truncated' if finish=='length' else 'schema','; '.join(problems[:4]),purpose=purpose,finish_reason=finish,raw=raw,attempts=attempt+1)
                    break
                except RoleOutputError as exc:
                    activity.emit('correction','Structured specialist response rejected','failed',{
                        'purpose':purpose,'attempt':attempt+1,'error':str(exc),'done_reason':finish,'category':exc.category,
                        'output_truncated':finish == 'length',
                        'malformed_non_truncated':finish != 'length',
                        'raw_response':raw})
                    if attempt:raise
                    request['messages'].append({'role':'user','content':'The previous response was incomplete or invalid. Return a complete, very compact object within the output limit. List only actual problems, at most 3 short issues; use an empty issues array if none. Do not restate evidence or explain successful checks.'})
            activity.emit('model',f'Model {purpose} returned; validation pending','validating',
                {'purpose':purpose,'response':value},(time.perf_counter()-started)*1000)
            return value
        except Exception as exc:
            activity.emit('model',f'Model {purpose} failed','failed',{'purpose':purpose,'error':str(exc)},(time.perf_counter()-started)*1000)
            raise


@inference.task('customer_communication')
def compose_customer(system, context):
    """Constrain the response envelope, not the customer's natural language."""
    from . import activity
    activity.emit('model','Customer composition waiting for shared inference slot','queued',{'model':inference.model()})
    with _MODEL_LOCK:
        started=time.perf_counter()
        activity.emit('model','Model customer composition requested','awaiting_model_response',
                      {'model':inference.model(),'purpose':'customer_composition','num_predict':_cap('customer_composition',320),'think':False,'format':'reply JSON envelope'})
        try:
            result=_post('/api/chat',{'model':inference.model(),'stream':False,'think':False,
                'messages':[{'role':'system','content':system.replace('End with ---END---.','')+' Return a JSON object containing only the finished reply in the reply field. Do not include analysis.'},
                            {'role':'user','content':context}],
                'format':{'type':'object','properties':{'reply':{'type':'string'}},'required':['reply'],'additionalProperties':False},
                'options':{'temperature':0.2,'num_predict':_cap('customer_composition',320)}},timeout=config.OLLAMA_TIMEOUT)
            content=result.get('message',{}).get('content','')
            decoded=json.loads(content)
            if not isinstance(decoded,dict) or not isinstance(decoded.get('reply'),str):
                raise ValueError('Model composition did not provide a reply string')
            reply=_clean(decoded['reply'])
            if not reply or re.search(r'we are given|the task is|we must|output only|the user asks|decided_response',reply,re.I):
                raise ValueError('Model returned task commentary instead of a customer reply')
            activity.emit('model','Model customer composition returned; validation pending','validating',
                          {'model':inference.model(),'purpose':'customer_composition','response_characters':len(reply)},(time.perf_counter()-started)*1000)
            return reply
        except Exception as exc:
            activity.emit('model','Customer composition failed','failed',{'model':inference.model(),'purpose':'customer_composition','error':str(exc)},(time.perf_counter()-started)*1000)
            raise


def customer_text(text):
    """Presentation cleanup; engineering evidence retains source identifiers."""
    text = re.sub(r'\b(?:Source|Reference procedure):\s*PROC-[A-Z0-9-]+[.]?', '',text,flags=re.I)
    text = re.sub(r'(?im)^\s*(?:source|reference procedure|this procedure is covered by|this follows)[^\n]*PROC-[^\n]*\n?', '', text)
    text = re.sub(r'\s*\(?(?:PROC-[A-Z0-9-]+|QMS-DOC-\d+)\)?[.]?', '', text)
    return text.strip()


def generate_grounded(question, customer_name, device, accepted_chunks,
                      procedure_id, prefer_llm: bool = True,
                      extra_context: str = "",
                      correction_feedback: str = "", public_context: bool = False) -> Generation:
    """Write a customer reply grounded in the accepted procedure extracts.

    `correction_feedback` is the Reflexion channel: on a retry it carries the
    exact sentences that failed verification, so the second attempt is told
    what to fix rather than simply asked again.
    """
    sources = _sources_block(accepted_chunks)
    user = (
        f"Customer: {customer_name}\n"
        f"Confirmed device: {device.model} revision {device.revision}, "
        f"serial {device.serial_number}\n\n"
        f"Customer message:\n{question}\n\n"
        f"Approved procedure extracts (the ONLY permitted source):\n{sources}\n\n"
    )
    if public_context:
        from .response_policy import reported_observations
        user = (f'Customer supplied name: {customer_name or "not supplied"}\n'
                f'Public equipment model: {device.model}. Account identity is not asserted.\n'
                'These sections are independently authorized for all supported hardware variants. '
                'Do not claim the account is verified, expose account details, or invent a hardware revision.\n'
                f'Customer request: {question}\n'
                f'Already received observations: {json.dumps(reported_observations(question))}. '
                'Do not ask the customer to provide an observation already received. Focus on the next unknown.\n\n'
                f'Approved customer-disclosable manual sections:\n{sources}\n')
    if extra_context:
        user += f"Background for your awareness (do NOT quote):\n{extra_context}\n\n"
    if procedure_id == 'PROC-GEN-MISSING-ACCESSORY':
        user += 'The procedure lists examples of possible missing items, not a packing list for this customer. Do not claim that these accessories normally or typically come with the device. Ask which item was expected and what was actually received.\n'
    if correction_feedback:
        user += (
            "Your previous draft was REJECTED by an automated source check. "
            "Fix exactly these problems and change nothing else:\n"
            f"{correction_feedback}\n\n"
            "Validate reported facts against the customer message and instructions against the manual. "
            "Remove the unsupported claim; preserve natural customer language.\n\n"
        )
    user += ('Compose the next helpful reply without printing source identifiers.' if public_context else
        f"Write the next helpful reply without source IDs or scope paragraphs. Write the "
        f"serial number exactly as {device.serial_number} if you mention it."
    )

    if prefer_llm and llm_available():
        try:
            text = (compose_customer(CUSTOMER_SYSTEM,user) if public_context else
                    llm_complete(GROUNDED_SYSTEM,user,prefill=f"Hello {customer_name or 'there'},\n\n"))
            if text:
                if not public_context and procedure_id not in text:
                    text += f"\n\nReference procedure: {procedure_id}."
                return Generation(
                    text=text,
                    mode="llm",
                    engine=f"local LLM via Ollama ({inference.model()})",
                    prompt=user,
                )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            pass

    return Generation(
        text=_template_grounded(customer_name, device, accepted_chunks,
                                procedure_id, public_context=public_context, reported_text=question),
        mode="template",
        engine="deterministic template writer (not a model)",
        prompt=user,
    )


def generate_ungrounded(question, customer_name, device,
                        prefer_llm: bool = True) -> Generation:
    """Reply with NO QMS retrieval. Used only by the comparison demonstration."""
    known = (
        f"Confirmed device from the account record: {device.model} revision "
        f"{device.revision}, serial {device.serial_number}"
        if device else "No device could be confirmed from the account record"
    )
    user = (
        f"Customer: {customer_name}\n{known}\n\n"
        f"Customer message:\n{question}\n\n"
        "You have no approved procedure available. Write the reply."
    )

    if prefer_llm and llm_available():
        try:
            text = llm_complete(
                UNGROUNDED_SYSTEM, user,
                prefill=f"Hello {customer_name or 'there'},\n\n",
            )
            if text:
                return Generation(
                    text=text,
                    mode="llm",
                    engine=f"local LLM via Ollama ({inference.model()})",
                    prompt=user,
                )
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

    return Generation(
        text=_template_ungrounded(customer_name, device),
        mode="template",
        engine="deterministic template writer (not a model)",
        prompt=user,
    )


def generate_logistics_answer(customer_name, device, orders,
                              shipments) -> Generation:
    """Answer an order or delivery question from ERP records only.

    Deterministic on purpose. These are facts straight out of a record, and
    there is nothing here a language model would improve - it could only get a
    tracking number wrong.
    """
    who = customer_name or "there"
    lines = []

    if shipments:
        s = shipments[0]
        lines.append(
            f"Your most recent shipment is {s['tracking_number']} with "
            f"{s['carrier']}, currently showing as {s['status']} "
            f"({s['last_scan']})."
        )
        if s.get("shipped_date"):
            lines.append(f"It was raised on {s['shipped_date']}.")
    if orders:
        o = orders[0]
        lines.append(
            f"That is against order {o['order_id']} for a "
            f"{o.get('product_prescribed') or device.model}, which our system "
            f"shows as {o.get('distributor_status') or 'open'}."
        )
    if not lines:
        lines.append(
            "I could not find an open order or shipment against your account "
            "for this device."
        )

    body = " ".join(lines)
    return Generation(
        text=(
            f"Hello {who},\n\n"
            f"Thanks for getting in touch. I have looked up the records for "
            f"your {device.model} (serial {device.serial_number}).\n\n"
            f"{body}\n\n"
            "If that does not match what you are seeing, tell me and I will "
            "check further.\n\n"
            "Kind regards,\nCustomer Support"
        ),
        mode="template",
        engine="deterministic ERP record writer (not a model)",
    )


def generate_abstention(customer_name, device, reason: str, next_step: str,
                        closing: str = "") -> Generation:
    """Always deterministic. Cannot invent troubleshooting content."""
    who = customer_name or "there"
    if device:
        dev_line = (
            f"I can see your {device.model} revision {device.revision} "
            f"(serial {device.serial_number}) on your account."
        )
    else:
        dev_line = "I was not able to confirm which device this is about."

    body = f"I am not able to give you troubleshooting steps for this one, " \
           f"because {reason}"
    if closing:
        body += " " + closing

    text = (
        f"Hello {who},\n\n"
        f"Thanks for getting in touch. {dev_line}\n\n"
        f"{body}\n\n"
        f"{next_step}\n\n"
        "Thanks for your patience."
    )
    return Generation(
        text=text,
        mode="deterministic-abstention",
        engine="deterministic abstention writer (not a model)",
    )


# --- deterministic writers -------------------------------------------------

def _first_sentences(text: str, n: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return " ".join(parts[:n]).strip()


def _template_grounded(customer_name, device, accepted_chunks,
                       procedure_id, public_context=False, reported_text='') -> str:
    """Build a reply only from sentences lifted out of the accepted extracts.

    This writer is the last line of defence when the language model's draft
    cannot be verified, so it must not add device content of its own. Every
    substantive line below is copied from an accepted chunk; the surrounding
    text is process framing, which `grounding.is_process_statement` recognises
    and excludes from source checking.
    """
    who = customer_name or "there"
    steps = []
    for c in accepted_chunks:
        steps.append(f"- {c.section}: {_first_sentences(c.text, 2)}")

    task_chunks = [c for c in accepted_chunks if not c.section.lower().startswith(('scope', 'safety', 'symptom'))]
    task = next((c for c in task_chunks if c.section.lower().startswith('step')), task_chunks[0] if task_chunks else None)
    body = _first_sentences(task.text, 1) if task else 'Could you describe the visible power and lamp indicators so I can establish the appropriate next check?'
    body = re.sub(r'\bAsk the customer to\s+', 'Please ', body, flags=re.I)
    if procedure_id == 'PROC-GEN-MISSING-ACCESSORY':
        return (f'Could you confirm which items you received?' +
                ('' if public_context else f' Source: {procedure_id}.'))
    if public_context:
        task = next((c for c in accepted_chunks if not c.section.lower().startswith(('scope', 'safety'))), None)
        if task is None:
            return 'Could you describe what happens when you use the unit, including any visible indicator or message?'
        from .response_policy import reported_observations
        count=1 if reported_observations(reported_text)['all_lamps_affected'] else 2
        return _first_sentences(task.text, count) + ' Let me know what you observe before we continue.'
    return (
        f"Hello {who},\n\n"
        f"{body}\n\n"
        f"Let me know what you observe before we continue.\n\n"
        f"Source: {procedure_id}."
    )


def _template_ungrounded(customer_name, device) -> str:
    who = customer_name or "there"
    dev = (f"your {device.model} revision {device.revision} (serial "
           f"{device.serial_number})") if device else "your device"
    return (
        f"Hello {who},\n\n"
        f"Thanks for getting in touch about {dev}. I do not have an approved "
        f"service procedure available for this device, so I am not able to give "
        f"you specific troubleshooting steps - anything I offered would be "
        f"guesswork, and that is not something I am willing to do with a "
        f"medical device.\n\n"
        f"I am passing this to a support engineer who can work from the "
        f"controlled documentation for your exact model and revision.\n\n"
        f"Thanks for your patience."
    )
