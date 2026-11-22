"""0.4.1 item A end to end: scripts/concurrency_load.py, shortened (three processes, loopback only).

The full acceptance runs (32 flows x 4 calls; 0.4.0 versus 0.4.1) are recorded in
verification/concurrency-0.4.1.json. Here: the same harness with short generations.
"""
import argparse
import importlib.util
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def harness():
    spec=importlib.util.spec_from_file_location('concurrency_load',ROOT/'scripts'/'concurrency_load.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def clean(report):
    import inference_gateway
    assert report['gateway_release']==inference_gateway.__version__ and report['client_version']=='0.4.2' # client 0.4.1 through Gateway 0.4.5; 0.4.2 (additive) with 0.4.6
    assert report['calls_completed']==report['calls_total'] and report['failures']=={} and report['failures_before_request_id']==0
    assert 'lease.expired' not in report['ledger_events'] and 'resource.held' not in report['ledger_events'] and report['suspensions']==[]


def test_32_parallel_flows_through_the_listener_without_timeouts_expiries_or_holds(tmp_path):
    report=harness().run_main(argparse.Namespace(src=str(ROOT/'src'),client_src=None,flows=32,calls=2,slots='1,10',latency='0.3,1',timeout=120,
                                            max_connections=256,max_pending=512,work=str(tmp_path),out=None))
    clean(report)
    assert report['flow_start_spread_ms']<500
    assert max(report['heads_max_concurrent_generations'].values())>=2 # same-head overlap on a slots-10 head


def test_the_503_path_under_a_tiny_connection_limit_loses_no_call(tmp_path):
    report=harness().run_main(argparse.Namespace(src=str(ROOT/'src'),client_src=None,flows=32,calls=2,slots='10',latency='0.3,1',timeout=120,
                                                 max_connections=4,max_pending=512,work=str(tmp_path),out=None))
    clean(report)
    assert report['gateway_load']['counters']['connections_503']>0
