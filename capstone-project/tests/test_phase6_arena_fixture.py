from capstone_rag import telemetry


def test_phase6_arena_fixture_is_schema_valid_sanitized_and_truthful():
    value=telemetry.phase6_fixture()
    assert value['schema_version']==telemetry.SCHEMA_VERSION and value['fixture_only']
    required={'sequence','event_id','schema_version','timestamp','event_type','case_id',
        'workflow_id','task_id','agent_id','sender_agent_id','receiver_agent_id',
        'correlation_id','logical_home_node','execution_node','tool_node','inference_node',
        'model_id','endpoint_id','task_status','summary','evidence_refs','detail'}
    assert all(required==set(row) for row in value['events'])
    assert all(row['detail']['contains_customer_data'] is False for row in value['events'])
    physical=next(row for row in value['events'] if row['event_type']=='inference.completed')
    assert physical['detail']['physical_inference'] is True
    assert physical['inference_node'] and physical['detail']['gateway_request_id']
    interrupted=next(row for row in value['events'] if row['event_type']=='experiment.trial.failed')
    assert interrupted['detail']['completion_inferred'] is False


def test_synthetic_candidate_events_never_claim_physical_execution():
    value=telemetry.phase6_fixture()
    for row in value['events']:
        if row['detail'].get('synthetic') and row['event_type']!='inference.completed':
            assert row['detail'].get('physical_inference') is not True
