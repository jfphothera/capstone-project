"""Compare two scenario reports that use the same denominator."""

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
    p.add_argument("before")
    p.add_argument("after")
    args = p.parse_args()
    print(json.dumps(scenarios.compare(args.before, args.after), indent=2))


if __name__ == "__main__":
    main()
