"""Official MCP SDK services and audited in-process application client.

Phase 4.1 deliberately uses in-memory transport.  The same service schemas can
be mounted behind authenticated remote transport in Phase 4.2; no LAN listener
is opened here.
"""
from __future__ import annotations

import asyncio
import json
import site
from pathlib import Path
from typing import Any

from .coordination import timestamp, uid
from .runtime_identity import coordinator_id
from . import qms, telemetry, validation


SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_tool_invocations (
 invocation_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 task_id TEXT, workflow_id TEXT, case_id TEXT, agent_id TEXT NOT NULL,
 service_name TEXT NOT NULL, tool_name TEXT NOT NULL, permission_scope TEXT NOT NULL,
 input_digest TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
 error_category TEXT, endpoint TEXT NOT NULL, transport TEXT NOT NULL,
 started_at TEXT NOT NULL, completed_at TEXT);
"""

TOOL_SCOPES = {
    'erp.lookup_device': 'erp.read',
    'qms.search_authorized': 'qms.read',
    'ticket.lookup_history': 'ticket.read',
    'operations.draft_proposal': 'operations.propose',
}
AGENT_SCOPES = {
    'erp_research': {'erp.read'},
    'qms_research': {'qms.read'},
    'ticket_history': {'ticket.read'},
    'operations': {'operations.propose'},
    'coordinator': set(TOOL_SCOPES.values()),
}


def _sdk():
    """Load the pinned SDK, including the repo-local acceptance installation."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.client import Client
        return MCPServer, Client
    except ModuleNotFoundError:
        root = Path(__file__).resolve().parents[2]
        local = root / 'phase4_deps'
        if local.is_dir():
            site.addsitedir(str(local))
        from mcp.server.mcpserver import MCPServer
        from mcp.client import Client
        return MCPServer, Client


def _digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _clean_text(value, limit=240):
    return ' '.join(str(value or '').split())[:limit]


class MCPServices:
    def __init__(self, store):
        self.store = store
        MCPServer, _ = _sdk()
        self.erp = MCPServer('capstone-erp', version='4.1')
        self.qms = MCPServer('capstone-qms', version='4.1')
        self.ticket = MCPServer('capstone-ticket-history', version='4.1')
        self.operations = MCPServer('capstone-operations-proposals', version='4.1')
        self._register()

    def _register(self):
        store = self.store

        @self.erp.tool(name='erp.lookup_device', structured_output=True)
        def lookup_device(serial: str = '', account_id: str = '') -> dict[str, Any]:
            """Read exact synthetic ERP device and ownership evidence."""
            serial = _clean_text(serial, 40).upper()
            account_id = _clean_text(account_id, 40)
            if not serial and not account_id:
                return {'status': 'verification_failed', 'devices': [],
                        'ambiguity': 'serial or account_id is required',
                        'authority': 'synthetic_erp_read'}
            clauses, values = [], []
            if serial:
                clauses.append('d.serial_number=?'); values.append(serial)
            if account_id:
                clauses.append('d.account_id=?'); values.append(account_id)
            rows = store.query(
                'SELECT d.serial_number,d.model,d.revision,d.revision_status,'
                'd.revision_basis,d.product_family,d.status,d.install_date,d.account_id '
                'FROM devices d WHERE ' + ' AND '.join(clauses), tuple(values))
            return {
                'status': 'matched' if len(rows) == 1 else
                          'ambiguous' if len(rows) > 1 else 'not_found',
                'devices': rows,
                'ambiguity': None if len(rows) <= 1 else 'multiple exact ERP rows',
                'authority': 'synthetic_erp_read',
                'provenance': ['devices.serial_number', 'devices.revision',
                               'devices.revision_basis'],
            }

        @self.qms.tool(name='qms.search_authorized', structured_output=True)
        def search_authorized(query: str, model: str = '', revision: str = '') -> dict[str, Any]:
            """Read controlled QMS metadata; deterministic policy remains authoritative."""
            query = _clean_text(query, 500)
            model = _clean_text(model, 80)
            revision = _clean_text(revision, 80)
            if not query:
                raise ValueError('query is required')
            tokens = {t.lower() for t in query.replace('-', ' ').split() if len(t) > 2}
            records = []
            for chunk in qms.load_chunks():
                if chunk.approval_status not in validation.CURRENT_STATUSES:
                    continue
                if model and chunk.model and model != chunk.model:
                    continue
                scope = list(chunk.applicable_revisions or [])
                if revision and scope and revision not in scope:
                    continue
                haystack = f'{chunk.title} {chunk.text} {chunk.procedure_id}'.lower()
                score = sum(token in haystack for token in tokens)
                if score:
                    records.append({
                        'chunk_id': chunk.chunk_id, 'document_id': chunk.document_id,
                        'document_version': chunk.document_version,
                        'procedure_id': chunk.procedure_id,
                        'approval_status': chunk.approval_status,
                        'models': [chunk.model] if chunk.model else [],
                        'revision_scope': scope,
                        'safety_restrictions': list(chunk.safety_restrictions),
                        'score': score,
                    })
            records.sort(key=lambda item: (-item['score'], item['chunk_id']))
            return {'status': 'matched' if records else 'no_authorized_match',
                    'results': records[:12], 'model': model or None,
                    'revision': revision or None,
                    'authority': 'controlled_qms_evidence_only',
                    'authorization_decision': 'not_granted_by_tool'}

        @self.ticket.tool(name='ticket.lookup_history', structured_output=True)
        def lookup_history(case_id: str = '', serial: str = '') -> dict[str, Any]:
            """Read synthetic chronology; prior outcomes are not QMS authority."""
            case_id = _clean_text(case_id, 40)
            serial = _clean_text(serial, 40).upper()
            if not case_id and not serial:
                raise ValueError('case_id or serial is required')
            chronology = []
            if case_id:
                for row in store.query(
                        'SELECT message_id,created_at,direction,channel,status,subject '
                        'FROM messages WHERE case_id=? ORDER BY created_at', (case_id,)):
                    chronology.append({**row, 'evidence_ref': row['message_id']})
            historical = []
            if serial:
                historical = store.query(
                    'SELECT pool_id,source_ticket_id,archetype,subject,created_at '
                    'FROM ticket_pool WHERE device_serial_number=? ORDER BY created_at DESC LIMIT 20',
                    (serial,))
            return {'status': 'found' if chronology or historical else 'not_found',
                    'chronology': chronology, 'historical_tickets': historical,
                    'authority': 'synthetic_history_observation_only',
                    'qms_authority': False}

        @self.operations.tool(name='operations.draft_proposal', structured_output=True)
        def draft_proposal(case_id: str, proposal_type: str, reason: str) -> dict[str, Any]:
            """Draft a proposal without executing or authorizing an operation."""
            if proposal_type not in {'rma', 'replacement_shipment', 'return_label'}:
                raise ValueError('unsupported proposal type')
            if not store.case(_clean_text(case_id, 40)):
                raise ValueError('unknown case')
            return {'proposal_id': uid('MCP-PROPOSAL'), 'case_id': case_id,
                    'proposal_type': proposal_type, 'reason': _clean_text(reason, 500),
                    'status': 'draft_only', 'executed': False, 'authorized': False,
                    'required_next_step': 'named human approval through existing domain gate'}

    def server_for(self, tool_name):
        return {'erp': self.erp, 'qms': self.qms, 'ticket': self.ticket,
                'operations': self.operations}[tool_name.split('.', 1)[0]]


class MCPGateway:
    def __init__(self, store, services=None):
        self.store = store
        self.services = services or MCPServices(store)

    async def _call(self, tool_name, arguments, timeout):
        _, Client = _sdk()
        async with Client(self.services.server_for(tool_name),
                          read_timeout_seconds=timeout) as client:
            return await asyncio.wait_for(
                client.call_tool(tool_name, arguments, read_timeout_seconds=timeout),
                timeout=timeout)

    @staticmethod
    def _payload(result):
        structured = getattr(result, 'structuredContent', None)
        if structured is not None:
            return structured
        for block in getattr(result, 'content', []):
            text = getattr(block, 'text', None)
            if text:
                try: return json.loads(text)
                except ValueError: continue
        raise RuntimeError('MCP tool returned no schema-valid structured output')

    def call(self, *, agent_id, tool_name, arguments, request_id,
             task_id=None, workflow_id=None, case_id=None, timeout=10.0,
             task_repository=None):
        scope = TOOL_SCOPES.get(tool_name)
        if not scope:
            raise ValueError('Unknown MCP tool')
        if scope not in AGENT_SCOPES.get(agent_id, set()):
            raise PermissionError(f'{agent_id} is not permitted to invoke {tool_name}')
        if not isinstance(arguments, dict) or len(json.dumps(arguments, default=str)) > 16_384:
            raise ValueError('MCP input must be a bounded object')
        prior = self.store.one('SELECT * FROM mcp_tool_invocations WHERE request_id=?',
                               (request_id,))
        digest = _digest(arguments)
        if prior:
            if prior['tool_name'] != tool_name or prior['input_digest'] != digest:
                raise ValueError('MCP request ID was reused with different input')
            if prior['status'] == 'completed':
                return json.loads(prior['result'])
            raise RuntimeError('Prior MCP request did not complete successfully')
        invocation_id = uid('MCP-CALL')
        self.store.insert('mcp_tool_invocations', {
            'invocation_id': invocation_id, 'request_id': request_id,
            'task_id': task_id, 'workflow_id': workflow_id, 'case_id': case_id,
            'agent_id': agent_id, 'service_name': tool_name.split('.', 1)[0],
            'tool_name': tool_name, 'permission_scope': scope,
            'input_digest': digest, 'status': 'running', 'result': None,
            'error_category': None, 'endpoint': 'in-process://capstone-mcp',
            'transport': 'official-mcp-sdk-in-memory', 'started_at': timestamp(),
            'completed_at': None})
        if task_repository and task_id:
            task_repository.transition(task_id, 'waiting_for_tool', agent_id,
                                       f'MCP call {tool_name} started')
        telemetry.emit(self.store, 'mcp.call.started', case_id=case_id,
            workflow_id=workflow_id, task_id=task_id, agent_id=agent_id,
            correlation_id=request_id, tool_node=coordinator_id(),
            endpoint_id='in-process://capstone-mcp', task_status='running',
            summary=f'{tool_name} started', detail={'scope': scope})
        try:
            result = asyncio.run(self._call(tool_name, arguments, float(timeout)))
            if getattr(result, 'isError', False):
                raise RuntimeError('MCP tool reported a safe execution error')
            payload = self._payload(result)
            self.store.update('mcp_tool_invocations', 'invocation_id', invocation_id, {
                'status': 'completed', 'result': json.dumps(payload, sort_keys=True, default=str),
                'completed_at': timestamp()})
            if task_repository and task_id:
                task_repository.transition(task_id, 'running', agent_id,
                                           f'MCP call {tool_name} completed')
            telemetry.emit(self.store, 'mcp.call.succeeded', case_id=case_id,
                workflow_id=workflow_id, task_id=task_id, agent_id=agent_id,
                correlation_id=request_id, tool_node=coordinator_id(),
                endpoint_id='in-process://capstone-mcp', task_status='completed',
                summary=f'{tool_name} completed',
                evidence_refs=[invocation_id], detail={'scope': scope})
            return payload
        except asyncio.TimeoutError as exc:
            self._fail(invocation_id, 'mcp_tool_timeout')
            raise TimeoutError(f'{tool_name} timed out') from exc
        except Exception:
            self._fail(invocation_id, 'mcp_tool_error')
            telemetry.emit(self.store, 'mcp.call.failed', case_id=case_id,
                workflow_id=workflow_id, task_id=task_id, agent_id=agent_id,
                correlation_id=request_id, tool_node=coordinator_id(),
                endpoint_id='in-process://capstone-mcp', task_status='failed',
                summary=f'{tool_name} failed safely', detail={'scope': scope})
            raise

    def _fail(self, invocation_id, category):
        self.store.update('mcp_tool_invocations', 'invocation_id', invocation_id, {
            'status': 'failed', 'error_category': category,
            'completed_at': timestamp()})
