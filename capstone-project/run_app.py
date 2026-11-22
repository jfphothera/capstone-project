"""Launch the local customer-service application.

    python run_app.py

Then open:

    http://127.0.0.1:8000/        representative workspace
    http://127.0.0.1:8000/chat    customer chat
    http://127.0.0.1:8000/trace   live Agent Activity and QMS inventory
    http://127.0.0.1:8000/api/docs   the API, for inspection

One process. One inference worker. One arrival simulator. Opening more
browser tabs does not create more of either.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "src"))

from capstone_rag import config, generation, store as store_mod  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=None,
                   help="use a different database file (keeps the "
                        "demonstration data untouched)")
    args = p.parse_args()

    from capstone_rag import startup
    diagnostic=startup.require()
    print('interpreter    '+diagnostic['interpreter'])

    if args.db:
        os.environ["CAPSTONE_DB"] = args.db
        config.DB_PATH = args.db
        store_mod.reset_default()

    if not os.path.exists(config.DB_PATH):
        print(f"No database at {config.DB_PATH}.\n"
              f"Run these first:\n"
              f"  python scripts/build_revision_register.py\n"
              f"  python scripts/import_synthetic.py", file=sys.stderr)
        return 1

    from capstone_rag.ai_settings import Registry
    registry=Registry();profile=registry.active();availability=registry.validate_available(profile)
    available=availability['passed']
    prefer=True
    print('configuration  '+profile['name'])
    if not available:print('model routes unavailable through the Inference Gateway: '+str(availability['errors']))
    print(f"database       {config.DB_PATH}")
    print(f"model routes   {'available' if available else 'unavailable'}")
    print('replies        authenticated Inference Gateway routing enabled; failures are recorded, no silent substitution')
    print(f"workspace      http://{args.host}:{args.port}/")
    print(f"customer chat  http://{args.host}:{args.port}/chat")
    print(f"agent activity http://{args.host}:{args.port}/trace")
    print(f"api            http://{args.host}:{args.port}/api/docs")

    from capstone_rag.webapp import main as serve
    serve(host=args.host, port=args.port, prefer_llm=prefer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
