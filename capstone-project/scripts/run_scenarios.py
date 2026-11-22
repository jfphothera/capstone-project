"""Run fast deterministic or selected sequential Qwen3 evaluation cases."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from capstone_rag import scenarios  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("fast", "real"), default="fast")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--category")
    p.add_argument("--split", choices=("development", "held_out"))
    p.add_argument("--scenario", action="append", dest="scenario_ids")
    p.add_argument("--failure-category")
    p.add_argument("--dataset", help="scenario JSONL to evaluate")
    p.add_argument("--out-dir", default=os.path.join("docs", "evidence",
                                                      "scenario_runs"))
    args = p.parse_args()
    result = scenarios.run(
        scenario_ids=args.scenario_ids, category=args.category,
        split=args.split,
        limit=args.limit, seed=args.seed, out_dir=args.out_dir,
        mode=args.mode, prefer_llm=args.mode == "real",
        failure_category=args.failure_category, dataset_path=args.dataset)
    print(json.dumps({k: v for k, v in result.items() if k != "details"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
