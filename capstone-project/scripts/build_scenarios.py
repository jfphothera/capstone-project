"""Build and validate the 1,000-case labelled evaluation dataset."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import config, scenarios  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=config.DB_PATH)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--seed", type=int, default=20261026)
    p.add_argument("--output", default=scenarios.DATASET_PATH)
    args = p.parse_args()
    _, report = scenarios.build(db_path=args.db, count=args.count,
                                seed=args.seed, output_path=args.output)
    print(json.dumps(report, indent=2))
    return 0 if report["referential_integrity"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
