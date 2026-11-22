"""Emit reproducible Phase 1 data, corpus, and source reconciliation facts."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from capstone_rag import identity, index, qms


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "operations.db"
OUT = ROOT / "docs" / "phase1" / "reconciliation_facts.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    scalar = lambda sql, params=(): connection.execute(sql, params).fetchone()[0]
    known = tuple(row[0] for row in connection.execute(
        "SELECT serial_number FROM devices ORDER BY serial_number"))

    tables = {name: scalar(f"SELECT count(*) FROM {name}") for name in
              ("customers", "devices", "orders", "ticket_pool", "cases",
               "messages", "retrieved_sources", "audit_events")}
    models = dict(connection.execute(
        "SELECT model,count(*) FROM devices GROUP BY model ORDER BY model"))
    revisions = dict(connection.execute(
        "SELECT revision_status,count(*) FROM devices GROUP BY revision_status "
        "ORDER BY revision_status"))
    serial_formats = {
        "eight_digit": sum(bool(re.fullmatch(r"[0-9]{8}", value)) for value in known),
        "five_digit_hyphen_two_letters_digit": sum(
            bool(re.fullmatch(r"[0-9]{5}-[A-Z]{2}[0-9]", value)) for value in known),
        "other": sum(not (re.fullmatch(r"[0-9]{8}", value) or
                          re.fullmatch(r"[0-9]{5}-[A-Z]{2}[0-9]", value))
                     for value in known),
    }
    multi_device = scalar(
        "SELECT count(*) FROM (SELECT account_id FROM devices GROUP BY account_id "
        "HAVING count(*)>1)")

    pool = [dict(row) for row in connection.execute(
        "SELECT pool_id,archetype,subject,body,product FROM ticket_pool")]
    identifier_states = Counter()
    for row in pool:
        claim = identity.Claim.from_text(
            (row["subject"] or "") + "\n" + (row["body"] or ""),
            known_serials=known)
        if claim.serial_candidates or claim.serial_issue:
            identifier_states[claim.serial_status or claim.serial_issue or "unclassified"] += 1
    unsupported = {name: sum(bool(re.search(pattern, (row["subject"] or "") + "\n" +
                                                   (row["body"] or ""), re.I)) for row in pool)
                   for name, pattern in {
                       "P100": r"\bP[ -]?100\b", "100XL": r"\b100XL\b",
                       "Aquex": r"\bAquex\b"}.items()}
    archetypes = dict(Counter(row["archetype"] for row in pool).most_common())
    products = dict(Counter(row["product"] or "<null>" for row in pool).most_common())

    broad = re.compile(r"phone|voicemail|ivr|callback|call-back|missed call|voice message", re.I)
    explicit = re.compile(
        r"transcription not available|no further transcript|no transcript|"
        r"automated call-back request|"
        r"no customer reply|no reply required|informational only", re.I)
    case_rows = []
    for case in connection.execute("SELECT case_id,subject FROM cases"):
        bodies = "\n".join(row[0] or "" for row in connection.execute(
            "SELECT body FROM messages WHERE case_id=?", (case["case_id"],)))
        case_rows.append((case["case_id"], (case["subject"] or "") + "\n" + bodies))

    def case_summary(pattern):
        ids = [case_id for case_id, text in case_rows if pattern.search(text)]
        placeholders = ",".join("?" for _ in ids) or "NULL"
        counts = dict(connection.execute(
            f"SELECT status,count(*) FROM messages WHERE direction='outbound' "
            f"AND case_id IN ({placeholders}) GROUP BY status", ids)) if ids else {}
        return {"cases": len(ids), "outbound_by_status": counts}

    chunks = qms.load_chunks()
    documents = {chunk.document_id: chunk for chunk in chunks}
    qms_scopes = dict(Counter(chunk.scope for chunk in documents.values()))
    qms_types = dict(Counter(chunk.document_type for chunk in documents.values()))
    compatibility = qms.load_compatibility()
    exact_power = sorted({key: [p for p in value if "PWR" in p]
                          for key, value in compatibility.items()
                          if key.startswith("P4800|")}.items())
    index_path = ROOT / "data" / "index" / "qms_index.npz"
    with __import__("numpy").load(index_path, allow_pickle=False) as blob:
        cached_meta = json.loads(str(blob["meta"]))

    result = {
        "database_counts": tables,
        "device_models": models,
        "serial_formats": serial_formats,
        "revision_provenance": revisions,
        "multi_device_accounts": multi_device,
        "ticket_identifier_states": dict(identifier_states),
        "unsupported_references": unsupported,
        "ticket_archetypes": archetypes,
        "source_product_labels": products,
        "phone_reconciliation": {
            "operational_broad": case_summary(broad),
            "operational_explicit_no_reply": case_summary(explicit),
            "pool_phone_ai_silent": sum(row["archetype"] == "phone_ai_silent" for row in pool),
            "pool_broad_phone_like": sum(bool(broad.search((row["subject"] or "") + "\n" +
                                                           (row["body"] or ""))) for row in pool),
        },
        "qms": {"documents": len(documents), "chunks": len(chunks),
                "scopes": qms_scopes, "document_types": qms_types,
                "p4800_power_compatibility": exact_power},
        "fingerprints": {
            "corpus": index.corpus_fingerprint(chunks),
            "cached_index_corpus": cached_meta.get("fingerprint"),
            "index_sha256": sha256(index_path),
            "challenge_sha256": sha256(ROOT / "data" / "phase1" /
                                        "challenge_scenarios.jsonl"),
            "operations_db_sha256": sha256(DB),
        },
    }
    connection.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
