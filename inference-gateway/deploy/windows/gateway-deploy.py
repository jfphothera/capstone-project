"""Release-package entry: run the Gateway installer straight from this package.

<machine-wide python.exe> -I <package>\\bin\\gateway-deploy.py <verb> --settings <deployment-settings.json>

The pure-Python wheel is imported in place (zipimport); nothing is installed
before the verb runs. Verbs: plan certificate install verify update rollback repair uninstall.
"""
import os
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
wheels=sorted((root/'wheel').glob('inference_gateway-*-py3-none-any.whl'))
if len(wheels)!=1:
    print('{"ok": false, "error": "package must contain exactly one Gateway wheel"}');raise SystemExit(2)
os.environ['INFERENCE_GATEWAY_PACKAGE']=str(root)
sys.path.insert(0,str(wheels[0]))
from inference_gateway.deployment import main
raise SystemExit(main())
