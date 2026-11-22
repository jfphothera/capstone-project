"""Measured evaluation of the agent.

    python run_evaluation.py                 # deterministic writer, fast
    python run_evaluation.py --llm           # with the local LLM
    python run_evaluation.py --out docs/evidence/evaluation

Scores device identification, document selection, troubleshooting outcome and
escalation behaviour independently. Writes both a readable report and JSON.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, cases, config, evaluation, memory as memory_mod,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true",
                    help="use the local LLM for generation (slow on CPU)")
    ap.add_argument("--out", help="write <out>.md and <out>.json")
    ap.add_argument("--isolated", action="store_true", default=True,
                    help="use scratch stores so demo logs are untouched")
    args = ap.parse_args()

    # Evaluation writes a lot of tickets and cases; keep it out of the
    # demonstration logs unless explicitly told otherwise.
    scratch = os.path.join(config.DATA_DIR, "_eval_run")
    os.makedirs(scratch, exist_ok=True)
    config.TICKET_LOG_PATH = os.path.join(scratch, "tickets.jsonl")
    for p in ("tickets.jsonl", "esc.jsonl", "rma.jsonl", "mem.jsonl"):
        fp = os.path.join(scratch, p)
        if os.path.exists(fp):
            os.remove(fp)

    print("building agent ...")
    ag = agent_mod.Agent(
        prefer_llm=args.llm,
        escalations=cases.EscalationQueue(os.path.join(scratch, "esc.jsonl")),
        approvals=cases.ApprovalQueue(os.path.join(scratch, "rma.jsonl")),
        reviewed_memory=memory_mod.ReviewedMemory(
            os.path.join(scratch, "mem.jsonl")),
        verbose=True,
    )
    print(f"generation: {'local LLM' if args.llm else 'deterministic writer'}")
    print()

    # Operational cases need the persistent store. They are written with
    # run_id='EVAL' so they never reach the live trends view.
    op_store = op_flow = None
    try:
        from capstone_rag import caseflow, store as store_mod
        if os.path.exists(config.DB_PATH):
            import shutil
            eval_db = os.path.join(scratch, "operations.db")
            shutil.copy(config.DB_PATH, eval_db)
            op_store = store_mod.Store(eval_db)
            op_flow = caseflow.CaseFlow(op_store, ag)
        else:
            print("NOTE: no operations database; the operational suite is "
                  "skipped. Run scripts/import_synthetic.py to include it.")
    except Exception as exc:                      # noqa: BLE001 - reported
        print(f"NOTE: operational suite unavailable ({exc})")

    report = evaluation.run(ag, store=op_store, flow=op_flow)
    text = evaluation.render(report)
    print(text)

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out + ".md", "w", encoding="utf-8") as fh:
            fh.write("# Evaluation results\n\n```\n" + text + "\n```\n")
        with open(args.out + ".json", "w", encoding="utf-8") as fh:
            fh.write(evaluation.to_json(report))
        print(f"\nwrote {args.out}.md and {args.out}.json")

    failed = report["cases"] - report["cases_passed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
