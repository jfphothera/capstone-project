"""Read a Capstone operations database schema without opening the application."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--minimum", type=int, required=True)
    parser.add_argument("--maximum", type=int, required=True)
    args = parser.parse_args()
    path = Path(args.database)
    if not path.is_file():
        print(json.dumps({"compatible": False, "error": "database_missing", "path": str(path)}))
        return 2
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as database:
            row = database.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        version = int(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError) as exc:
        print(json.dumps({"compatible": False, "error": str(exc), "path": str(path)}))
        return 2
    compatible = version is not None and args.minimum <= version <= args.maximum
    print(json.dumps({
        "compatible": compatible,
        "schema_version": version,
        "minimum": args.minimum,
        "maximum": args.maximum,
        "path": str(path.resolve()),
    }))
    return 0 if compatible else 3


if __name__ == "__main__":
    raise SystemExit(main())
