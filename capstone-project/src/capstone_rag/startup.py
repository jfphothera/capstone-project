"""Fail before workers start if the chosen interpreter cannot run orchestration."""
import importlib,importlib.metadata,os,sys
CORE_REQUIRED={'langgraph':'langgraph.graph','langgraph-checkpoint-sqlite':'langgraph.checkpoint.sqlite','langchain-core':'langchain_core.tools','numpy':'numpy','fastapi':'fastapi','uvicorn':'uvicorn','httpx':'httpx'}
LOCAL_MODEL_REQUIRED={'torch':'torch','sentence-transformers':'sentence_transformers'}
def local_only():
    for key in ('LANGCHAIN_TRACING_V2','LANGSMITH_TRACING','LANGCHAIN_TRACING'):os.environ[key]='false'
    os.environ['HF_HUB_OFFLINE']='1';os.environ['TRANSFORMERS_OFFLINE']='1'
def check():
    local_only();versions={};errors={}
    required={**CORE_REQUIRED,**({} if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' else LOCAL_MODEL_REQUIRED)}
    for name,module in required.items():
        try:importlib.import_module(module);versions[name]=importlib.metadata.version(name)
        except Exception as exc:errors[name]=str(exc)
    try:importlib.import_module('capstone_rag.orchestration')
    except Exception as exc:errors['project orchestration']=str(exc)
    return {'interpreter':sys.executable,'versions':versions,'errors':errors,'ok':not errors}
def require():
    report=check()
    if not report['ok']:raise SystemExit('Capstone cannot start with its managed runtime: '+str(report['errors'])+'\nRun Install-Capstone.ps1 again after installing the Microsoft Visual C++ 2015-2022 Redistributable (x64), or contact the Capstone administrator. No alternate runtime was started.')
    return report
