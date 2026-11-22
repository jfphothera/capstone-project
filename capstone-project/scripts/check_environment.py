import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from capstone_rag.startup import check
report=check();print(json.dumps(report,indent=2));result=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True);print(result.stdout,result.stderr);raise SystemExit(bool(not report['ok'] or result.returncode))
