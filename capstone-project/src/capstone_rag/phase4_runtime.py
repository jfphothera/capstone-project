"""Integration bridge from the existing LangGraph coordinator to Phase 4.1."""
from __future__ import annotations

from . import identity, telemetry, triage
from .execution import ConcurrentExecutionEngine, TaskRepository, TaskSpec, stable_task_key
from .mcp_services import MCPGateway


def collect_initial_evidence(store, state, *, max_concurrency=3):
    """Run only the intake tasks that are genuinely independent."""
    workflow_id, case_id = state['workflow_id'], state['cid']
    message_id, body = state['message_id'], state['body']
    engine = ConcurrentExecutionEngine(store, max_concurrency=max(1,min(3,int(max_concurrency))), queue_capacity=16)
    repo = engine.repo
    gateway = MCPGateway(store)
    serials = [row['serial_number'] for row in store.query('SELECT serial_number FROM devices')]
    names = [row['customer_name'] for row in store.query('SELECT customer_name FROM customers')]
    claim = identity.Claim.from_text(body, known_serials=serials, known_names=names)
    case = store.case(case_id)

    agents = (
        ('erp_research', 'identity', ('erp.read',), ('erp.lookup_device',)),
        ('ticket_history', 'history', ('ticket.read',), ('ticket.lookup_history',)),
        ('safety_complaint', 'safety', ('safety.classify',), ()),
        ('evidence_join', 'coordination', ('deterministic.test',), ()),
    )
    for agent_id, role, capabilities, tools in agents:
        telemetry.register(store, agent_id, agent_id.replace('_', ' ').title(), role,
                           capabilities=capabilities, required_tools=tools,
                           execution_constraints={'case_scoped': True,
                                                  'may_authorize': False})

    specs = (
        TaskSpec(workflow_id, case_id, 'erp_research', 'initial.erp', 'erp.read',
                 state['workflow_id'], stable_task_key(workflow_id, 'initial.erp'),
                 required_tools=('erp.lookup_device',),
                 input_evidence_refs=(message_id,), timeout_seconds=20),
        TaskSpec(workflow_id, case_id, 'ticket_history', 'initial.ticket', 'ticket.read',
                 state['workflow_id'], stable_task_key(workflow_id, 'initial.ticket'),
                 required_tools=('ticket.lookup_history',),
                 input_evidence_refs=(message_id,), timeout_seconds=20),
        TaskSpec(workflow_id, case_id, 'safety_complaint', 'initial.safety', 'safety.classify',
                 state['workflow_id'], stable_task_key(workflow_id, 'initial.safety'),
                 input_evidence_refs=(message_id,), timeout_seconds=20),
    )
    tasks = [repo.create(spec) for spec in specs]

    def erp_handler(task):
        payload = gateway.call(agent_id='erp_research', tool_name='erp.lookup_device',
            arguments={'serial': claim.serial or '', 'account_id': case.get('account_id') or ''},
            request_id=task['idempotency_key'] + ':mcp', task_id=task['task_id'],
            workflow_id=workflow_id, case_id=case_id, timeout=10,
            task_repository=repo)
        payload['_evidence_refs'] = [task['idempotency_key'] + ':mcp']
        return payload

    def ticket_handler(task):
        payload = gateway.call(agent_id='ticket_history', tool_name='ticket.lookup_history',
            arguments={'case_id': case_id, 'serial': claim.serial or ''},
            request_id=task['idempotency_key'] + ':mcp', task_id=task['task_id'],
            workflow_id=workflow_id, case_id=case_id, timeout=10,
            task_repository=repo)
        payload['_evidence_refs'] = [task['idempotency_key'] + ':mcp']
        return payload

    def safety_handler(task):
        finding = triage.triage(body)
        return {'hold': bool(triage.requires_immediate_escalation(finding)),
                'complaint': finding.complaint.as_dict(),
                'screen': finding.adverse_event.as_dict(),
                'authority': 'deterministic_safety_classifier',
                '_evidence_refs': [message_id]}

    engine.run([task['task_id'] for task in tasks], {
        'initial.erp': erp_handler, 'initial.ticket': ticket_handler,
        'initial.safety': safety_handler})
    remote_observations=collect_remote_observations(store,state)

    join = repo.create(TaskSpec(
        workflow_id, case_id, 'evidence_join', 'initial.join', 'deterministic.test',
        workflow_id, stable_task_key(workflow_id, 'initial.join'),
        input_evidence_refs=(message_id,),
        dependency_set=tuple(task['task_id'] for task in tasks), timeout_seconds=20))

    def join_handler(task):
        results = {}
        for predecessor in task['dependency_set']:
            predecessor_task = repo.get(predecessor)
            result = repo.result(predecessor)
            if not result or result['status'] != 'accepted':
                raise ValueError('Join predecessor does not have a current accepted result')
            results[predecessor_task['task_type'].split('.')[-1]] = result['payload']
        results['task_ids'] = list(task['dependency_set'])
        results['remote_advisory_observations']=remote_observations
        results['_evidence_refs'] = [repo.get(dep)['result_reference'] for dep in task['dependency_set']]
        telemetry.emit(store, 'join.completed', case_id=case_id,
            workflow_id=workflow_id, task_id=task['task_id'], agent_id='evidence_join',
            correlation_id=workflow_id, task_status='completed',
            summary='Independent intake evidence joined deterministically')
        return results

    engine.run([join['task_id']], {'initial.join': join_handler})
    result = repo.result(join['task_id'])
    if not result or result['status'] != 'accepted':
        return {'status': 'blocked', 'task_ids': [task['task_id'] for task in tasks],
                'join_task_id': join['task_id']}
    return {**result['payload'], 'status': 'completed', 'join_task_id': join['task_id']}


def collect_remote_observations(store,state):
    """Optional qualified model observations supplement, never authorize, evidence."""
    service=getattr(store,'cluster_service',None)
    routes=service.settings().get('workflow_routes',{}) if service else {}
    if not routes:return {}
    from . import inference
    import copy
    parent=inference.current()
    pinned=copy.deepcopy(parent['snapshot'] if parent else getattr(store,'inference_profile_snapshot',None))
    engine=ConcurrentExecutionEngine(store,max_concurrency=len(routes),queue_capacity=max(16,len(routes)))
    specs=[]
    for role,route in routes.items():
        specs.append(engine.repo.create(TaskSpec(state['workflow_id'],state['cid'],role,
            'cluster.'+role,'model.inference',state['workflow_id'],stable_task_key(state['workflow_id'],'cluster',role),
            required_model=route['model'],model_eligibility=() if route['nodes']==['@authorized'] else tuple(route['nodes']),timeout_seconds=120)))
    def observe(task):
        role=task['logical_agent_id'];route=routes[role]
        registry=getattr(store,'inference_registry',None) or inference.Registry()
        snapshot=copy.deepcopy(pinned or registry.snapshot())
        snapshot['id']='phase42-distributed-advisory'
        snapshot['routes'][role]={'endpoint':'local','model':route['model'],'enabled':True,
            'options':{'num_ctx':2048,'num_predict':120,'temperature':0,'seed':42}}
        # The durable task engine already reserved the exact worker. Avoid a
        # second capacity reservation by using its selected explicit endpoint.
        worker=service.enrollment.workers.get(task['assigned_worker_id'])
        snapshot['endpoints'][worker['worker_id']]={'id':worker['worker_id'],'url':worker['endpoint'],'resource_id':worker['node_id'],'timeout':90}
        snapshot['routes'][role]['endpoint']=worker['worker_id']
        if route['nodes']==['@authorized']:snapshot['routes'][role]['cluster_nodes']=['@authorized']
        snapshot['configuration_hash']=inference.fingerprint(snapshot)
        with inference.session(store,task['task_id'],case_id=state['cid'],snapshot=snapshot) as routed,inference.specialist(role):
            response=inference.infer('/api/generate',{'prompt':
                'Review this synthetic support message as '+role+'. State uncertainties. Do not approve actions or claim verified evidence.\n'+state['body'],
                'think':False,'options':{'num_predict':120}},90)
        from .gateway_transport import enabled
        return {'advisory_only':True,'authority':'none','text':response.get('response',''),
                'inference_node':routed.get('last_inference_node',worker['node_id']) if enabled() else worker['node_id'],
                'inference_endpoint':None if enabled() else worker['endpoint'],'model':route['model']}
    for _ in range(max(1,len(specs)*2)):
        pending=[t['task_id'] for t in specs if engine.repo.get(t['task_id'])['state'] in {'eligible','queued'}]
        if not pending:break
        before=sum(engine.repo.get(t)['attempt_count'] for t in pending)
        engine.run(pending,{t['task_type']:observe for t in specs})
        if sum(engine.repo.get(t)['attempt_count'] for t in pending)==before:break
    return {t['logical_agent_id']:engine.repo.result(t['task_id']) for t in specs}


def qms_mcp_evidence(store, state, *, model, revision, query):
    """Invoke QMS through MCP while leaving deterministic authorization intact."""
    gateway = MCPGateway(store)
    task_id = state.get('delegation_task_id')
    return gateway.call(agent_id='qms_research', tool_name='qms.search_authorized',
        arguments={'query': query, 'model': model or '', 'revision': revision or ''},
        request_id=stable_task_key(state['workflow_id'], 'qms.mcp', model, revision, query),
        task_id=task_id, workflow_id=state['workflow_id'], case_id=state['cid'],
        timeout=15)
