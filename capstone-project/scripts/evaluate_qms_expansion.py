"""Compare fictional QMS coverage and deterministic retrieval before/after.

This evaluator never reads the external reference QMS. It compares a frozen
copy of the earlier fictional Markdown corpus with the current fictional
corpus, using the same labelled synthetic scenarios for both sides.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from capstone_rag import config, scenarios  # noqa: E402


TOKEN_RE = re.compile(r"[a-z0-9]+")
STOP = frozenset(
    "a an and are as at be been but by can do for from has have i if in is it "
    "me my of on or our please that the their this to was we with you your"
    .split()
)


def tokens(text: str) -> set[str]:
    return {word for word in TOKEN_RE.findall((text or "").lower())
            if len(word) > 1 and word not in STOP}


def parse_frontmatter(raw: str) -> dict:
    _, frontmatter, _ = raw.split("---", 2)
    result = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value.startswith("[") and value.endswith("]"):
            value = [part.strip().strip("\"'")
                     for part in value[1:-1].split(",") if part.strip()]
        else:
            value = value.strip("\"'")
        result[key] = value
    return result


def load_sections(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta = parse_frontmatter(raw)
        body = raw.split("---", 2)[2]
        headings = list(re.finditer(r"^(#{2,3})\s+(.+?)\s*$", body,
                                    flags=re.MULTILINE))
        parents = {}
        for index, match in enumerate(headings):
            level = len(match.group(1))
            title = match.group(2).strip()
            parents[level] = title
            for deeper in tuple(k for k in parents if k > level):
                parents.pop(deeper)
            end = (headings[index + 1].start()
                   if index + 1 < len(headings) else len(body))
            text = body[match.end():end].strip()
            if not text:
                continue
            section_path = " > ".join(parents[k] for k in sorted(parents))
            searchable = " ".join((meta.get("title", ""), section_path, text))
            rows.append({
                "document_id": meta.get("document_id"),
                "procedure_id": meta.get("procedure_id"),
                "audiences": meta.get("audiences", ["legacy_unspecified"]),
                "section_path": section_path,
                "text": text,
                "tokens": tokens(searchable),
                "characters": len(text),
            })
    return rows


def score(query: str, section: dict) -> float:
    query_tokens = tokens(query)
    if not query_tokens:
        return 0.0
    overlap = query_tokens & section["tokens"]
    return len(overlap) / math.sqrt(len(query_tokens) *
                                    max(len(section["tokens"]), 1))


def retrieve(query: str, sections: list[dict], *, k: int,
             procedure_diverse: bool) -> list[dict]:
    ranked = sorted(sections, key=lambda row: score(query, row), reverse=True)
    if not procedure_diverse:
        return ranked[:k]
    result, seen = [], set()
    for row in ranked:
        if row["procedure_id"] in seen:
            continue
        seen.add(row["procedure_id"])
        result.append(row)
        if len(result) == k:
            break
    return result


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    if not ordered:
        return 0
    return ordered[min(math.ceil(len(ordered) * fraction) - 1,
                       len(ordered) - 1)]


def corpus_stats(sections: list[dict]) -> dict:
    lengths = [row["characters"] for row in sections]
    return {
        "documents": len({row["document_id"] for row in sections}),
        "procedures": len({row["procedure_id"] for row in sections}),
        "chunks": len(sections),
        "chunk_characters": {
            "mean": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "p95": percentile(lengths, 0.95),
            "maximum": max(lengths, default=0),
        },
        "audiences": dict(Counter(
            audience for row in sections for audience in row["audiences"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-qms", required=True)
    parser.add_argument("--baseline-scenarios",
                        default=str(ROOT / "data" / "evaluation" /
                                    "scenarios.jsonl"))
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    baseline_rows = scenarios.load(path=args.baseline_scenarios)
    revised_rows, revised_validation = scenarios.build(
        db_path=args.db, count=len(baseline_rows), persist=False)
    before = {row.scenario_id: row for row in baseline_rows}
    after = {row.scenario_id: row for row in revised_rows}

    baseline_sections = load_sections(Path(args.baseline_qms))
    revised_sections = load_sections(ROOT / "data" / "qms")

    required = [row for row in baseline_rows if row.qms_required]
    revised_required = [row for row in revised_rows if row.qms_required]
    common = [sid for sid in before
              if before[sid].expected_procedure and after[sid].expected_procedure]
    expanded = [row for row in revised_required if row.expected_procedure]

    def recalled(row, sections, k, diverse):
        found = retrieve(row.initial_message, sections, k=k,
                         procedure_diverse=diverse)
        return row.expected_procedure in {item["procedure_id"] for item in found}

    before_common = sum(recalled(before[sid], baseline_sections, 8, False)
                        for sid in common)
    after_common = sum(recalled(after[sid], revised_sections, 20, True)
                       for sid in common)
    after_expanded = sum(recalled(row, revised_sections, 20, True)
                         for row in expanded)

    result = {
        "method": {
            "scope": "fictional QMS files and synthetic scenarios only",
            "ranking": "deterministic token-overlap diagnostic",
            "before": "top 8 section chunks",
            "after": "best chunk per procedure, top 20 procedures",
            "note": "This diagnostic supplements, but does not replace, the local embedding tests.",
        },
        "coverage": {
            "before": {"required": len(required),
                       "covered": sum(bool(row.expected_procedure)
                                      for row in required)},
            "after": revised_validation["qms"],
        },
        "retrieval": {
            "common_covered_scenarios": len(common),
            "before_recalled": before_common,
            "after_recalled": after_common,
            "expanded_covered_scenarios": len(expanded),
            "expanded_recalled": after_expanded,
        },
        "corpus": {
            "before": corpus_stats(baseline_sections),
            "after": corpus_stats(revised_sections),
        },
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "qms_comparison.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")

    coverage = result["coverage"]
    retrieval = result["retrieval"]
    corpus = result["corpus"]
    markdown = f"""# Fictional QMS before/after evaluation

This evaluation reads only the fictional Capstone corpus and labelled synthetic
scenarios. It does not read or reproduce the external reference QMS.

| Measure | Before | After |
|---|---:|---:|
| Procedures required | {coverage['before']['required']} | {coverage['after']['required']} |
| Applicable fictional coverage | {coverage['before']['covered']} | {coverage['after']['covered']} |
| Deliberate gaps | {coverage['before']['required'] - coverage['before']['covered']} | {coverage['after']['deliberate_gaps']} |
| Controlled documents | {corpus['before']['documents']} | {corpus['after']['documents']} |
| Structure-aware chunks | {corpus['before']['chunks']} | {corpus['after']['chunks']} |

On the {retrieval['common_covered_scenarios']} scenarios covered in both versions,
the deterministic retrieval diagnostic recalled the expected procedure in
{retrieval['before_recalled']} cases before and {retrieval['after_recalled']} cases
after. Across all {retrieval['expanded_covered_scenarios']} scenarios covered after
expansion, it recalled the expected procedure in {retrieval['expanded_recalled']}.

The diagnostic uses token overlap for reproducibility. Application-level semantic
retrieval remains covered by the local embedding tests.
"""
    (output_dir / "qms_comparison.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

