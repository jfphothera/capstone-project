"""Create a clean first-install database containing synthetic source data only."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SOURCE_TABLES = ("meta", "customers", "devices", "orders", "source_shipments", "ticket_pool")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    source, target = Path(args.source).resolve(), Path(args.target).resolve()
    if target.exists():
        raise SystemExit("Refusing to overwrite release seed: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    from capstone_rag.store import Store
    Store(str(target))
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src, \
            sqlite3.connect(target) as dst:
        for table in SOURCE_TABLES:
            source_columns = [row[1] for row in src.execute(f"PRAGMA table_info({table})")]
            target_columns = {row[1] for row in dst.execute(f"PRAGMA table_info({table})")}
            columns = [name for name in source_columns if name in target_columns]
            if not columns:
                continue
            marks = ",".join("?" for _ in columns)
            names = ",".join(f'"{name}"' for name in columns)
            rows = src.execute(f"SELECT {names} FROM {table}").fetchall()
            dst.executemany(f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({marks})", rows)
        dst.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('release_seed','synthetic-source-only')")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
