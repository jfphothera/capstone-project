"""Stable Phase 5-readable comparative reports; missing measurements stay null."""
import statistics


def compare(runs,store):
    workers={w['endpoint']:w for w in store.query('SELECT * FROM worker_registry WHERE endpoint IS NOT NULL')}
    groups={};trial_groups={}
    for run in runs:
        metric=run.get('metrics',{})
        profile=run.get('profile',{})
        trial_key=(profile.get('id'),run.get('specialist'),
                   run.get('execution_strategy','sequential'))
        trial=trial_groups.setdefault(trial_key,{'profile_id':trial_key[0],
            'agent_arrangement':trial_key[1],
            'execution_strategy':trial_key[2],'campaign_concurrency':run.get('campaign_concurrency'),
            'configuration_hashes':set(),'parameter_sets':{},
            'runs':0,'completed':0,'failed':0,'paused':0,'scored':0,'correct':0,
            'critical_violations':0,'source_revisions':set(),'snapshot_hashes':set(),
            'seeds':set(),'total_ms':[],'resource_measurements':[]})
        trial['runs']+=1;trial[run.get('status')]=trial.get(run.get('status'),0)+1
        configuration_hash=profile.get('configuration_hash')
        if configuration_hash:
            trial['configuration_hashes'].add(configuration_hash)
            trial['parameter_sets'][configuration_hash]={name:route.get('options',{}) for name,route in profile.get('routes',{}).items()}
        outcome=metric.get('all_scored_checks_passed');trial['scored']+=outcome is not None;trial['correct']+=outcome is True
        trial['critical_violations']+=len(metric.get('critical_violations',[]))
        trial['source_revisions'].add(run.get('source_revision'));trial['snapshot_hashes'].add(run.get('source_snapshot_sha256'))
        trial['seeds'].add(run.get('seed'))
        if metric.get('total_ms') is not None:trial['total_ms'].append(metric['total_ms'])
        if metric.get('host_resources') is not None:trial['resource_measurements'].append(metric['host_resources'])
        for usage in metric.get('model_usage',[]):
            endpoint=run['profile'].get('endpoints',{}).get(usage['endpoint_id'],{}).get('url')
            worker=workers.get(endpoint,{})
            key=(worker.get('node_id',endpoint or usage['endpoint_id']),usage['model'])
            group=groups.setdefault(key,{'node_id':key[0],'model':key[1],'hardware_tier':worker.get('compute_class','unavailable'),
                'calls':0,'trials':0,'correct':0,'scored':0,'critical_violations':0,'wall_ms':[],'generated_tokens':0,
                'generation_ms':0,'load_ms':0,'host_utilization':None,'cost_per_success':None})
            group['calls']+=usage['calls'];group['trials']+=1
            outcome=metric.get('all_scored_checks_passed');group['scored']+=outcome is not None;group['correct']+=outcome is True
            group['critical_violations']+=len(metric.get('critical_violations',[]))
            group['wall_ms'].append(usage['wall_ms']);group['generated_tokens']+=usage['generated_tokens']
            group['generation_ms']+=usage['generation_ms'];group['load_ms']+=usage['load_ms']
    for group in groups.values():
        values=sorted(group.pop('wall_ms'))
        group['latency_ms']={'min':min(values),'median':statistics.median(values),'max':max(values),
            'p95_observed':values[max(0,__import__('math').ceil(len(values)*.95)-1)]}
        group['tokens_per_second']=1000*group['generated_tokens']/group['generation_ms'] if group['generation_ms'] else None
    trials=[]
    for trial in trial_groups.values():
        values=trial.pop('total_ms');resources=trial.pop('resource_measurements')
        trial['source_revisions']=sorted(x for x in trial['source_revisions'] if x)
        trial['snapshot_hashes']=sorted(x for x in trial['snapshot_hashes'] if x)
        trial['seeds']=sorted(x for x in trial['seeds'] if x is not None)
        trial['configuration_hashes']=sorted(trial['configuration_hashes'])
        trial['latency_ms']={'min':min(values),'median':statistics.median(values),'max':max(values)} if values else None
        trial['resource_metrics']=resources or None
        trials.append(trial)
    return {'format':'capstone-experiment-comparison-v1','comparisons':list(groups.values()),
        'trial_groups':trials,
        'limitations':['Trial correctness includes whole-workflow deterministic checks; it is not isolated causal model quality.',
                       'No statistical significance or hardware superiority is inferred.',
                       'Host utilization/cost remain unavailable unless measured and supplied separately.']}
