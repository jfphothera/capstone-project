"""Command-line demonstration runner for Checkpoint 3.1.

    python run_demo.py                 # the four mandatory demonstrations
    python run_demo.py --all           # plus the two extra scenarios
    python run_demo.py --scenario SCN-02
    python run_demo.py --no-llm        # force the deterministic writer
    python run_demo.py --json out.json # also dump machine-readable evidence

Runs without Streamlit, so the evidence can always be regenerated.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import agent as agent_mod, config, generation  # noqa: E402

RULE = "=" * 78
THIN = "-" * 78


def load_scenarios():
    with open(os.path.join(config.ERP_DIR, "scenarios.json"), encoding="utf-8") as fh:
        return {s["scenario_id"]: s for s in json.load(fh)}


def show_result(result, show_trace=True):
    ident = result.identity.as_dict()
    print(f"\n[1] ERP lookup                              ({'deterministic'})")
    if ident["confirmed"]:
        print(f"    customer : {ident['customer_name']}  ({ident['account_id']})")
        print(f"    device   : {ident['model']} Rev {ident['revision']}  "
              f"serial {ident['serial_number']}  status {ident['status']}")
    else:
        print(f"    NOT CONFIRMED: {ident['reason']}")
        if ident["candidate_serials"]:
            print(f"    candidates: {', '.join(ident['candidate_serials'])}")

    if result.retrieval_query:
        print(f"\n[2] Semantic QMS search                     (real embeddings)")
        print(f"    query    : {result.retrieval_query[:150]!r}")
        print(f"    filters  : model={result.filters['model']} "
              f"revision={result.filters['revision']} "
              f"status in {result.filters['approval_status_in']}")
        print(f"    authorised procedures for this device: "
              f"{result.filters['authorised_procedures'] or 'NONE'}")
        all_v = sorted(result.accepted + result.rejected, key=lambda x: x.rank)
        print(f"\n    {'rank':<5}{'sim':<9}{'status':<10}chunk_id")
        print(f"    {THIN[:74]}")
        for v in all_v:
            print(f"    {v.rank:<5}{v.similarity:<9.4f}{v.status:<10}{v.chunk_id}")
        rejected = [v for v in all_v if not v.accepted]
        if rejected:
            print("\n    rejected, and why:")
            for v in rejected:
                print(f"      {v.chunk_id}")
                for f in v.failures:
                    print(f"        - {f}")
        spare = [v for v in all_v if v.accepted and not v.used]
        if spare:
            print("\n    authorised but not used:")
            for v in spare:
                print(f"      {v.chunk_id}: {v.reason}")
    else:
        print("\n[2] Semantic QMS search                     SKIPPED")

    print(f"\n[3] Validation                              (deterministic)")
    print(f"    {result.verdict.result}: {result.verdict.reason}")
    for c in result.verdict.checks:
        print(f"      [{c['result']:<4}] {c['check']}: {c['detail']}")

    print(f"\n[4] Response                                ({result.generation_engine})")
    print(f"    selected procedure: {result.selected_procedure or 'NONE'}")
    print(THIN)
    for line in result.response.splitlines():
        print(f"    {line}")
    print(THIN)

    print(f"\n[5] Ticket recorded: {result.ticket_id}   outcome={result.outcome}")

    if show_trace:
        print(f"\n    action trace:")
        for i, s in enumerate(result.steps, 1):
            print(f"      {i}. {s.name:<32} [{s.kind}] {s.summary}")


def run_scenario(ag, scn, header, show_trace=True):
    print(f"\n{RULE}\n{header}\n{RULE}")
    print(f"scenario   : {scn['scenario_id']} - {scn['name']}")
    print(f"expected   : {scn['expected_behaviour']}")
    print(f"subject    : {scn['subject']}")
    print(f"\ncustomer message:")
    for line in scn["customer_message"].splitlines():
        print(f"    {line}")

    result = ag.handle(
        scn["customer_message"],
        account_id=scn["account_id"],
        scenario_id=scn["scenario_id"],
    )
    show_result(result, show_trace=show_trace)
    return result


def run_comparison(ag, scn):
    print(f"\n{RULE}\nTEST 4 - RETRIEVAL CHANGES THE RESPONSE\n{RULE}")
    print(f"Same synthetic question, two modes. Scenario {scn['scenario_id']}.")
    print(f"\ncustomer message:")
    for line in scn["customer_message"].splitlines():
        print(f"    {line}")

    print(f"\n{THIN}\nMODE A - no QMS retrieval (index never consulted)\n{THIN}")
    gen_a, ident_a = ag.answer_without_retrieval(
        scn["customer_message"], account_id=scn["account_id"]
    )
    print(f"engine: {gen_a.engine}")
    print(f"sources cited: NONE (no retrieval performed)")
    print()
    for line in gen_a.text.splitlines():
        print(f"    {line}")

    print(f"\n{THIN}\nMODE B - with semantic QMS retrieval\n{THIN}")
    result_b = ag.handle(
        scn["customer_message"], account_id=scn["account_id"],
        scenario_id=scn["scenario_id"] + "-B",
    )
    print(f"engine: {result_b.generation_engine}")
    print(f"retrieval query  : {result_b.retrieval_query[:120]!r}")
    print(f"accepted chunks  : {[v.chunk_id for v in result_b.accepted]}")
    print(f"selected procedure: {result_b.selected_procedure}")
    print(f"validation       : {result_b.verdict.result}")
    print()
    for line in result_b.response.splitlines():
        print(f"    {line}")

    print(f"\n{THIN}\nWHAT CHANGED\n{THIN}")
    print(f"    Mode A had no procedure, cited nothing, and gave no "
          f"device-specific steps.")
    print(f"    Mode B retrieved {len(result_b.accepted)} chunk(s) from "
          f"{result_b.selected_procedure}, which is the approved procedure for")
    print(f"    {ident_a.device.model} Rev {ident_a.device.revision} "
          f"specifically, and the reply now contains those steps and cites the "
          f"source.")
    return gen_a, result_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="run one scenario id, e.g. SCN-02")
    ap.add_argument("--all", action="store_true",
                    help="also run the two extra scenarios")
    ap.add_argument("--no-llm", action="store_true",
                    help="force the deterministic template writer")
    ap.add_argument("--json", help="write machine-readable evidence here")
    ap.add_argument("--no-trace", action="store_true")
    args = ap.parse_args()

    scenarios = load_scenarios()

    print(RULE)
    print("CAPSTONE 3.1 - RAG DEMONSTRATION")
    print(RULE)
    prefer_llm = not args.no_llm
    have_llm = generation.llm_available() if prefer_llm else False
    print(f"embedding model : {config.EMBEDDING_MODEL}  (local, CPU)")
    if have_llm:
        print(f"generation      : local LLM {config.OLLAMA_MODEL} via Ollama")
    elif prefer_llm:
        print(f"generation      : Ollama/{config.OLLAMA_MODEL} NOT reachable -> "
              f"deterministic template writer (not a model)")
    else:
        print(f"generation      : deterministic template writer "
              f"(--no-llm; not a model)")

    print("\nbuilding / loading vector index ...")
    ag = agent_mod.Agent(prefer_llm=prefer_llm, verbose=True)
    print(f"  index: {len(ag.index.chunks)} chunks, "
          f"{ag.index.matrix.shape[1]}-dim vectors")

    results = {}

    if args.scenario:
        scn = scenarios.get(args.scenario)
        if not scn:
            sys.exit(f"unknown scenario {args.scenario}; "
                     f"have {sorted(scenarios)}")
        results[args.scenario] = run_scenario(
            ag, scn, f"SCENARIO {args.scenario}", not args.no_trace
        )
    else:
        results["SCN-01"] = run_scenario(
            ag, scenarios["SCN-01"],
            "TEST 1 - SUCCESSFUL GROUNDED RETRIEVAL", not args.no_trace)
        results["SCN-02"] = run_scenario(
            ag, scenarios["SCN-02"],
            "TEST 2 - WRONG REVISION", not args.no_trace)
        results["SCN-03"] = run_scenario(
            ag, scenarios["SCN-03"],
            "TEST 3 - NO APPLICABLE SOURCE", not args.no_trace)
        gen_a, result_b = run_comparison(ag, scenarios["SCN-01"])
        results["SCN-01-noretrieval"] = gen_a
        results["SCN-01-retrieval"] = result_b

        if args.all:
            results["SCN-04"] = run_scenario(
                ag, scenarios["SCN-04"],
                "EXTRA - MISSING SERIAL NUMBER", not args.no_trace)
            results["SCN-05"] = run_scenario(
                ag, scenarios["SCN-05"],
                "EXTRA - POTENTIAL SAFETY COMPLAINT", not args.no_trace)

    print(f"\n{RULE}\nSUMMARY\n{RULE}")
    for key, r in results.items():
        if hasattr(r, "verdict"):
            print(f"  {key:<22} outcome={r.outcome:<10} "
                  f"validation={r.verdict.result:<5} "
                  f"procedure={r.selected_procedure or '-'}")

    if args.json:
        payload = {
            "embedding_model": config.EMBEDDING_MODEL,
            "generation": (f"ollama:{config.OLLAMA_MODEL}" if have_llm
                           else "deterministic-template"),
            "chunks_indexed": len(ag.index.chunks),
            "embedding_dim": int(ag.index.matrix.shape[1]),
            "results": {
                k: (r.as_dict() if hasattr(r, "as_dict")
                    else {"mode": r.mode, "engine": r.engine, "text": r.text})
                for k, r in results.items()
            },
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote machine-readable evidence to {args.json}")


if __name__ == "__main__":
    main()
