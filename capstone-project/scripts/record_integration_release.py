"""Capture the final source inventory and configuration without changing cases."""
import hashlib,json,sys,zipfile,urllib.request
from datetime import datetime,timezone
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'src'))
from capstone_rag.experiments import source_revision
evidence=root/'docs/evidence/local_integration'
with urllib.request.urlopen('http://127.0.0.1:8000/api/ai/configuration',timeout=60) as response:configuration=json.load(response)
(evidence/'release-configuration.json').write_text(json.dumps(configuration,indent=2)+'\n',encoding='utf-8')
files=[]
for directory in ['src','tests','scripts','.run']:
    files.extend(p for p in (root/directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo'))
files.extend(root/name for name in ['README.md','requirements.txt','requirements.runtime.lock','run_app.py','.idea/misc.xml','.idea/Capstone.iml','docs/LOCAL_MODEL_INTEGRATION_20261028.md'] if (root/name).exists())
files=sorted(set(files));archive=evidence/'release-source.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
    for path in files:bundle.write(path,path.relative_to(root).as_posix())
manifest={'at':datetime.now(timezone.utc).isoformat(),'source_revision':source_revision(),'active_profile':configuration['active']['id'],'qwen_retrieval_enabled':next(x['enabled'] for x in configuration['retrieval_profiles'] if x['id']=='qwen3-experimental'),'tests':'436 passed; release-final-regression.txt','archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':[{'path':p.relative_to(root).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
(evidence/'release-source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in manifest.items() if k!='files'}))
