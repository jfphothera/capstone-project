"""Benchmark the real local semantic encoder before and after QMS expansion.

This is distinct from ``evaluate_qms_expansion.py``: ranking comes from the
configured sentence-embedding model and exact cosine similarity, not token
overlap. The external company QMS is never read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from capstone_rag import config, embeddings, qms, scenarios, validation  # noqa: E402
from capstone_rag.index import VectorIndex  # noqa: E402


def _meta(raw: str) -> dict:
    _, block, _ = raw.split("---", 2)
    out = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value.startswith("[") and value.endswith("]"):
            value = [p.strip().strip("\"'") for p in value[1:-1].split(",")
                     if p.strip()]
        out[key] = value
    return out


def _baseline_chunks(directory: str):
    """Read the pre-expansion fictional corpus without requiring audiences."""
    chunks = []
    for path in sorted(Path(directory).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta = _meta(raw)
        body = raw.split("---", 2)[2]
        parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE)
        for number in range(1, len(parts), 2):
            title = parts[number].strip()
            text = parts[number + 1].strip() if number + 1 < len(parts) else ""
            if not text:
                continue
            chunks.append(SimpleNamespace(
                chunk_id=f"{meta['document_id']}#B{number}",
                document_id=meta["document_id"],
                procedure_id=meta["procedure_id"], title=meta["title"],
                model=meta["model"],
                applicable_revisions=meta["applicable_revisions"],
                document_version=meta["document_version"],
                approval_status=meta["approval_status"],
                audiences=["customer_service"], section=title,
                section_path=title, heading_level=2, text=text,
                source_path=str(path), effective_date=None,
                superseded_by=None,
                embed_text=(f"{meta['title']}. Model {meta['model']} revision "
                            f"{'/'.join(meta['applicable_revisions'])}. "
                            f"{title}. {text}")))
    return chunks


def _rank(chunks, matrix, query, top_k, diverse):
    vector = embeddings.embed(query)[0]
    scores = matrix @ vector
    order = np.argsort(-scores)
    result, seen = [], set()
    for position in order:
        chunk = chunks[int(position)]
        if diverse and chunk.procedure_id in seen:
            continue
        seen.add(chunk.procedure_id)
        chunk._benchmark_score = float(scores[int(position)])
        result.append(chunk)
        if len(result) >= top_k:
            break
    return result


def _evaluate(rows, chunks, matrix, top_k, diverse, compatibility=None):
    covered = [row for row in rows if row.qms_required and
               row.expected_procedure]
    recalled = selected = 0
    failures = []
    rejection_reasons = Counter()
    for row in covered:
        ranked = _rank(chunks, matrix, row.initial_message, top_k, diverse)
        procedures = [chunk.procedure_id for chunk in ranked]
        hit = row.expected_procedure in procedures
        recalled += hit
        chosen = None
        if compatibility is not None:
            device = SimpleNamespace(
                model=row.device_model, revision=row.device_revision,
                model_revision=f"{row.device_model}|{row.device_revision}")
            best = ranked[0]._benchmark_score if ranked else None
            accepted = []
            for rank, chunk in enumerate(ranked, start=1):
                hit = SimpleNamespace(chunk=chunk,
                                      score=chunk._benchmark_score, rank=rank)
                verdict = validation.validate_chunk(
                    hit, device, compatibility, config.MIN_SIMILARITY,
                    best_similarity=best,
                    max_score_gap=config.MAX_RELATIVE_SCORE_GAP,
                    query=row.initial_message)
                rejection_reasons.update(
                    "wrong_revision" if "revision" in reason else
                    "superseded" if "status" in reason else
                    "restricted_audience" if "audience" in reason else
                    "not_authorized" if "authorised" in reason else
                    "wrong_model" if "model" in reason else
                    "symptom_family" if "symptom family" in reason else
                    "semantic_floor" if "similarity" in reason else
                    "other" for reason in verdict.failures)
                if verdict.accepted:
                    accepted.append((rank, chunk.procedure_id))
            family = validation.expected_procedure_family(row.initial_message)
            if family and family != "UNSUPPORTED_CODE":
                accepted.sort(key=lambda item: (family not in item[1], item[0]))
            if accepted:
                chosen = accepted[0][1]
            selected += chosen == row.expected_procedure
        if not hit or (compatibility is not None and chosen != row.expected_procedure):
            failures.append({"scenario_id": row.scenario_id,
                             "expected": row.expected_procedure,
                             "retrieved": procedures,
                             "selected": chosen})
    return {"covered": len(covered), "recalled_at_k": recalled,
            "authorized_selection_correct": selected if compatibility else None,
            "rejection_reasons": dict(rejection_reasons),
            "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-qms", required=True)
    parser.add_argument("--baseline-scenarios", required=True)
    parser.add_argument("--current-scenarios",
                        default=str(ROOT / "data/evaluation/scenarios.jsonl"))
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    current_rows = scenarios.load(path=args.current_scenarios)
    baseline_rows = scenarios.load(path=args.baseline_scenarios)
    current_chunks = qms.load_chunks()
    baseline_chunks = _baseline_chunks(args.baseline_qms)
    baseline_matrix = embeddings.embed([c.embed_text for c in baseline_chunks])
    current_index = VectorIndex.load_or_build(verbose=True)
    compatibility = qms.load_compatibility()

    baseline_covered_ids = {r.scenario_id for r in baseline_rows
                            if r.qms_required and r.expected_procedure}
    current_covered_ids = {r.scenario_id for r in current_rows
                           if r.qms_required and r.expected_procedure}
    common_ids = baseline_covered_ids & current_covered_ids
    common_current = [r for r in current_rows if r.scenario_id in common_ids
                      and r.qms_required and r.expected_procedure]
    result = {
        "benchmark": "real semantic embedding with exact cosine similarity",
        "embedding_model": embeddings.model_name(), "top_k": args.top_k,
        "before_all_historically_covered": _evaluate(
            [r for r in baseline_rows if r.scenario_id in baseline_covered_ids],
            baseline_chunks, baseline_matrix, 8, False),
        "before_common": _evaluate(
            [r for r in baseline_rows if r.scenario_id in common_ids],
            baseline_chunks, baseline_matrix, 8, False),
        "after_common": _evaluate(common_current, current_chunks,
                                  current_index.matrix, args.top_k, True,
                                  compatibility),
        "after_all_covered": _evaluate(current_rows, current_chunks,
                                        current_index.matrix, args.top_k, True,
                                        compatibility),
        "intentional_gaps": dict(Counter(
            f"{r.device_model}|{r.device_revision}:{r.category}"
            for r in current_rows if r.qms_required and
            not r.expected_procedure)),
        "notes": [
            "Recall measures candidate discovery; authorized selection also applies model, revision, status, audience and compatibility controls.",
            "A missing expected procedure is an abstention case, not a retrieval false negative.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items()
                      if k != "intentional_gaps"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
