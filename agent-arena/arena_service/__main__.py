"""python -I -m arena_service --config <file>: run the Arena Event Service."""
import argparse
import json
from pathlib import Path

from .server import serve

MOVED = "moved-to-hub.json"     # written by PREPARE-HUB-USB --move-data next to the database it copied


def main():
    parser = argparse.ArgumentParser(prog="arena_service")
    parser.add_argument("--config", required=True)
    parser.add_argument("--after-move", action="store_true",
                        help="start even though this service's data was moved to the hub (development only)")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as handle:
        config = json.load(handle)
    marker = Path(config["db_path"]).parent / MOVED
    if marker.exists() and not args.after_move:
        moved = json.loads(marker.read_text(encoding="utf-8"))
        # The hub has the same identity and keys: two services answering as one would split the heads.
        raise SystemExit(f"This service's data was moved to the hub on {moved.get('moved_at')} (service "
                         f"{moved.get('service_id')}), so it does not start here. The hub at {moved.get('hub')} is the "
                         f"event service now. To use this machine for development, give it a fresh data folder; "
                         f"--after-move overrides this check. ({marker})")
    serve(config)


if __name__ == "__main__":
    main()
