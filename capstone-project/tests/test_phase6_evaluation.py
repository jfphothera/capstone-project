from capstone_rag.evaluation_engine import compare


def run(candidate,scenario,seed,passed,latency,violations=(),execution='offline_deterministic_execution'):
    return {'id':f'{candidate}-{scenario}-{seed}','status':'completed','seed':seed,'iteration':1,
        'scenario':{'id':scenario},'source_snapshot_sha256':'source-1',
        'architecture':{'id':'candidate-a' if candidate=='base' else candidate,
            'configuration_hash':candidate},
        'metrics':{'all_scored_checks_passed':passed,'total_ms':latency,
            'critical_violations':list(violations),'execution_classification':execution}}


def test_hard_safety_gate_excludes_fast_unsafe_candidate_before_ranking():
    runs=[run('base','one',1,True,100),run('safe','one',1,True,80),
        run('unsafe','one',1,True,1,['Customer-used QMS source fails current authorization: X'])]
    report=compare(runs)
    rows={row['candidate_hash']:row for row in report['candidates']}
    assert not rows['unsafe']['safety_eligible']
    assert rows['unsafe']['safety_gate']['categories']==['unauthorized_disclosure']
    assert 'unsafe' not in report['performance_ranking_eligible_only']
    assert report['performance_ranking_eligible_only'][0]=='safe'


def test_paired_comparison_preserves_variability_and_execution_evidence():
    runs=[]
    for seed,base,candidate in [(1,False,True),(2,True,True)]:
        runs.extend([run('base','same',seed,base,100+seed,execution='physical_gateway_inference_observed'),
            run('candidate','same',seed,candidate,90+seed,execution='physical_gateway_inference_observed')])
    report=compare(runs)
    pair=report['paired_comparisons'][0]
    assert pair=={'baseline_hash':'base','candidate_hash':'candidate','paired_samples':2,
        'candidate_wins':1,'baseline_wins':0,'ties':1,'unscored_pairs':0}
    candidate=next(row for row in report['candidates'] if row['candidate_hash']=='candidate')
    assert candidate['latency']['stdev_ms'] is not None
    assert candidate['execution_classifications']==['physical_gateway_inference_observed']


def test_unknown_execution_evidence_is_not_upgraded_to_physical():
    value=run('base','one',1,None,10,execution='not_recorded')
    report=compare([value])
    assert report['candidates'][0]['execution_classifications']==['not_recorded']
    assert report['candidates'][0]['accuracy'] is None


def test_physical_performance_requires_observed_model_resource_gateway_id_and_digest():
    value=run('base','one',1,True,10,execution='physical_gateway_inference_observed')
    value['metrics']['inference_correlations']=[{'observed_model':'actual:1',
        'resource_id':'head-1','gateway_request_id':'GW-1','executed_digest':'sha256:actual'}]
    report=compare([value]);row=report['candidates'][0]
    assert row['physical_performance_attributable'] is True
    value['metrics']['inference_correlations'][0]['resource_id']=None
    row=compare([value])['candidates'][0]
    assert row['physical_performance_attributable'] is False
    assert row['execution_evidence_gaps']==['resource_id']
