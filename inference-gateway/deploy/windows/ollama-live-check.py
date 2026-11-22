"""Acceptance row 20: check one real Ollama head against Gateway 0.4.0's failure classes.

<machine-wide python.exe> -I <package>\\bin\\ollama-live-check.py --origin http://192.0.2.N:11434 --model <small completion model>

Standard library only; the Gateway wheel in this package is imported in place (no install,
no pytest, no ledger). Uses the Gateway's own transport and classifier. Run from 192.0.2.254
on an IDLE head under an authorized change: it waits up to 150 s for the head to show no
loaded model and refuses otherwise. Sends a handful of short generations, one deliberately
abandoned after 1.5 s. Prints one JSON object. Exit 0 all required checks pass, 1 a check
failed or the head is not idle/reachable, 2 bad arguments.
"""
import argparse,json,ssl,sys,threading,time
from pathlib import Path

root=Path(__file__).resolve().parents[1]
wheels=sorted((root/'wheel').glob('inference_gateway-*-py3-none-any.whl'))
if wheels:sys.path.insert(0,str(wheels[-1]))
from inference_gateway import recovery
from inference_gateway.capacity_gateway import Gateway


def gateway():
    g=Gateway.__new__(Gateway) # transport only: no ledger is opened or created
    g.upstream_tls=ssl.create_default_context();g.transport=g.upstream
    return g


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--origin',required=True);p.add_argument('--model',required=True,help='a small completion model installed on the head, e.g. llama3.2:1b')
    p.add_argument('--skip-parallel',action='store_true',help='skip the OLLAMA_NUM_PARALLEL timing estimate')
    a=p.parse_args(argv);g=gateway();out={'origin':a.origin,'model':a.model,'checks':{},'informational':{}}
    def call(path,payload,timeout=120,origin=None):return g.upstream(origin or a.origin,path,payload,timeout)
    def classify(path,payload,timeout=60,origin=None):
        try:call(path,payload,timeout,origin)
        except Exception as exc:return recovery.classify(exc,'dispatch'),recovery.http_detail(exc)
        return None,'answered'
    def check(name,expected,observed,required=True):
        (out['checks'] if required else out['informational'])[name]={'expected':expected,'observed':observed,'ok':observed==expected}
    def gen(prompt,n,keep='2s'):
        return {'model':a.model,'prompt':prompt,'stream':False,'think':False,'keep_alive':keep,'options':{'num_predict':n,'num_ctx':1024,'temperature':0,'seed':42}}
    try:
        out['ollama_version']=call('/api/version',None,5).get('version')
        if a.model not in {m['name'] for m in call('/api/tags',None,5).get('models',[])}:raise SystemExit(_fail(out,'model not installed on this head'))
        deadline=time.monotonic()+150
        while call('/api/ps',None,5).get('models'):
            if time.monotonic()>deadline:raise SystemExit(_fail(out,'head is not idle (a model stayed loaded for 150 s); choose an idle head'))
            time.sleep(5)
    except SystemExit:raise
    except Exception as exc:raise SystemExit(_fail(out,'head did not answer: '+recovery.http_detail(exc)))
    # 1. Replies -> classes (docs/FAILURES-AND-PLACEMENT.md section 1)
    check('unknown_model',[recovery.REQUEST_FAULT,'HTTP 404'],list(classify('/api/generate',{'model':'no-such-model:1','prompt':'x','stream':False})))
    check('bad_request_body',[recovery.REQUEST_FAULT,'HTTP 400'],list(classify('/api/chat',{'model':a.model,'messages':'not-a-list','stream':False})))
    check('wrong_option_type',[recovery.MODEL_ON_HEAD,'HTTP 500'],list(classify('/api/generate',{**gen('x',1),'options':{'num_ctx':'abc'}})),required=False)
    check('embed_on_completion_model',[recovery.MODEL_ON_HEAD,'HTTP 501'],list(classify('/api/embed',{'model':a.model,'input':['x']})),required=False)
    host=a.origin.rsplit(':',1)[0]
    check('closed_port',recovery.HEAD_UNREACHABLE,classify('/api/generate',gen('x',1),timeout=3,origin=host+':9')[0])
    check('timeout_after_send',recovery.UNCERTAIN,classify('/api/generate',gen('Count slowly from one to three hundred in words.',300),timeout=1.5)[0])
    time.sleep(4)
    # 2. Safe-to-probe signal: a busy runner stays listed past expires_at; an idle one unloads at keep_alive.
    policy={'ps_settle_seconds':2}
    call('/api/generate',gen('x',1))
    done=threading.Event()
    def long_call():
        try:call('/api/generate',gen('Count slowly from one to two hundred in words.',200),300)
        finally:done.set()
    threading.Thread(target=long_call,daemon=True).start();time.sleep(6)
    listed=[m['name'] for m in call('/api/ps',None,5).get('models',[])]
    if done.is_set():out['informational']['busy_runner']='generation finished within 6 s; busy check not conclusive on this head'
    else:check('busy_runner_listed_past_expires_at',[a.model],listed)
    check('busy_runner_not_safe','runner_loaded' if not done.is_set() else 'safe',g._safe_to_probe({'endpoint':a.origin},policy))
    done.wait(300);time.sleep(6)
    check('idle_head_unloads_and_is_safe','safe',g._safe_to_probe({'endpoint':a.origin},policy))
    # 3. OLLAMA_NUM_PARALLEL estimate (timing only; the Gateway is safe with slots 1 whatever it is).
    if not a.skip_parallel:
        began=time.monotonic();call('/api/generate',gen('Name three colours.',60,'30s'));alone=time.monotonic()-began
        began=time.monotonic();threads=[threading.Thread(target=call,args=('/api/generate',gen('Name three colours.',60,'30s'))) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(300)
        both=time.monotonic()-began
        out['informational']['parallel_estimate']={'one_request_s':round(alone,2),'two_concurrent_s':round(both,2),
            'likely_parallel':both<1.5*alone,'meaning':'likely_parallel true suggests the head runs 2+ requests at once; slots above 1 only if Arena confirms the setting'}
    out['ok']=all(c['ok'] for c in out['checks'].values())
    print(json.dumps(out,indent=2))
    return 0 if out['ok'] else 1


def _fail(out,error):
    out.update(ok=False,error=error);print(json.dumps(out,indent=2));return 1


if __name__=='__main__':raise SystemExit(main())
