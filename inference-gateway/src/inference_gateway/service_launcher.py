"""Isolated entry for a versioned release folder installed with pip --target.

Run as a script: <base python.exe> -I <release>\\inference_gateway\\service_launcher.py <tool> ...
-I ignores PYTHONPATH, user site-packages and the script directory, so this puts
exactly the release folder first on sys.path. Switching the registered release
is a path change, which makes update and rollback a binPath change.

Tools: service, console, validate, status (Windows service host), and publisher,
lifecycle, credentials, bootstrap, qualify, observe.
"""
TOOLS={'publisher':'observation_publisher','lifecycle':'capacity_lifecycle','credentials':'credentials',
       'bootstrap':'capacity_bootstrap','qualify':'capacity_admin','observe':'capacity_observe',
       'heads':'head_inventory','deploy':'deployment'}

if __name__=='__main__':
    import runpy
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    tool=sys.argv[1] if len(sys.argv)>1 else ''
    if tool in TOOLS:
        sys.argv=[sys.argv[0]]+sys.argv[2:]
        runpy.run_module('inference_gateway.'+TOOLS[tool],run_name='__main__',alter_sys=True)
    else:
        from inference_gateway.windows_service import main
        raise SystemExit(main())
