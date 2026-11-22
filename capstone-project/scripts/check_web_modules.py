"""Parse every shipped browser JavaScript file using Node's ES-module grammar."""
from __future__ import annotations

import json,subprocess
from pathlib import Path

ROOT=Path(__file__).parents[1]
STATIC=ROOT/'src/capstone_rag/webapp/static/js'

def main():
    results=[]
    for path in sorted(STATIC.rglob('*.js')):
        completed=subprocess.run(['node','--input-type=module','--check'],input=path.read_text(encoding='utf-8'),
            text=True,encoding='utf-8',capture_output=True)
        row={'file':path.relative_to(ROOT).as_posix(),'passed':completed.returncode==0}
        if completed.returncode:row['error']=(completed.stderr or completed.stdout).strip()
        results.append(row)
    report={'passed':all(row['passed'] for row in results),'modules':results}
    print(json.dumps(report,indent=2))
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
