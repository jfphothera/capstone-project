"""Versioned Testing Lab contract; engineering modes are not Phase 5 research."""
CONTRACT_VERSION='1.2'
_MODES=(
 ('model_microbenchmark','Model microbenchmarks',False),
 ('specialist_benchmark','Specialist benchmarks',True),
 ('model_combination','Model-combination comparisons',False),
 ('agent_decomposition','Agent decomposition (2–5 tasks)',False),
 ('planning_strategy','Planning and branch-factor comparisons',False),
 ('synthetic_conversation','Synthetic customer conversations',False),
 ('human_review_simulation','Simulated human review',False),
 ('full_case_replay','Full case scenario replay',True),
 ('distributed_node','Distributed-node testing',False),
 ('high_load','High-load pressure testing',True),
 ('failure_retest','Failure analysis and retesting',False),
 ('controlled_learning','Controlled learning experiments',False),
 ('concurrent_execution','Concurrent task execution',True),
 ('dependency_graph','Task dependency graphs',True),
 ('worker_selection','Worker selection and exhaustion',True),
 ('queue_saturation','Queue saturation',True),
 ('simulated_worker_failure','Simulated worker failure and recovery',True),
 ('task_cancellation','Task cancellation',True),
 ('restart_recovery','Coordinator restart recovery',True),
 ('mcp_invocation','MCP tool invocation',True),
 ('cross_case_isolation','Cross-case evidence isolation',True),
 ('validation_conflict','Validation conflict',True))

_EXECUTABLE={
 'model_microbenchmark','specialist_benchmark','model_combination','full_case_replay',
 'high_load','concurrent_execution','dependency_graph','restart_recovery',
 'cross_case_isolation','validation_conflict'}
_PARTIAL={'agent_decomposition','planning_strategy','synthetic_conversation',
 'human_review_simulation','distributed_node','failure_retest','controlled_learning',
 'worker_selection','queue_saturation','simulated_worker_failure','task_cancellation'}

def contract():
    return {'contract_version':CONTRACT_VERSION,'modes':[{
        'id':key,'label':label,'available_in_phase3':available,
        'available_in_phase4_1':available,
        'implementation_status':('executable' if key in _EXECUTABLE else
                                 'partial' if key in _PARTIAL else 'not_implemented'),
        'selectable':key in _EXECUTABLE,
        'availability_note':('Runnable through a documented Testing Lab category or campaign control.'
            if key in _EXECUTABLE else 'Observable primitives exist, but no dedicated selectable runner is claimed.'
            if key in _PARTIAL else 'Contract-reserved; no executable Testing Lab runner is exposed.'),
        'evidence_class':'local_engineering_verification' if key in {
            'concurrent_execution','dependency_graph','worker_selection','queue_saturation',
            'simulated_worker_failure','task_cancellation','restart_recovery','mcp_invocation',
            'cross_case_isolation','validation_conflict'} else 'existing_or_future_research',
        'execution_boundary':'isolated_synthetic_database',
        'promotion_policy':'never_automatic'} for key,label,available in _MODES]}
