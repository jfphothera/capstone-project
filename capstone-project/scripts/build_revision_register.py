"""Build the fictional hardware-revision verification register.

The synthetic ERP has no revision field. Deriving a revision from the install
date is an *inference*, and the capstone must not authorise revision-specific
troubleshooting on an inference alone.

This script creates `data/revision_register.json`, a fictional controlled
record of revisions that were physically confirmed during a service visit.
It is the only thing that makes a revision VERIFIED.

Fictional business rule RV-1
----------------------------
  verified      the serial appears in this register with a revision
  contradictory the register's revision disagrees with the install-date
                cohort, or the ERP recalibration date precedes install
  missing       no install date and not in the register
  inferred      install-date cohort only  (the default, and NOT sufficient
                on its own to authorise revision-specific instructions)

Deterministic: a seeded hash over the serial decides which devices were
"seen by service", so the register is reproducible. A handful of serials are
pinned so the demonstrations have a stable mix of verified, inferred and
contradictory devices to show.

    python scripts/build_revision_register.py
    python scripts/build_revision_register.py --force   # overwrite
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from capstone_rag import config  # noqa: E402

REGISTER_PATH = os.path.join(config.DATA_DIR, "revision_register.json")

COHORTS = (("A", "", "2022-01-01"), ("B", "2022-01-01", "2024-01-01"),
           ("C", "2024-01-01", "9999-12-31"))

# Pinned so the demonstrations are stable.
PIN_VERIFIED = {
    "98148469":  "A",   # Ivan Nowak, P4800 Rev A - the main grounded path
    "66383977":  "C",   # Ivan Nowak, Home-NB     - no-coverage demo
    "38180-BM2": "C",   # Peter Thomas, Series-M  - safety demo
    "21308910":  "A",   # Peter Thomas, P3410
}
# Left OUT of the register on purpose, so the inference gate is demonstrable.
PIN_INFERRED = {"52385502", "68177-DV5"}
# Register disagrees with the install-date cohort: a real-looking data defect.
PIN_CONTRADICTORY = {"88445165": "A"}   # cohort says C, register says A

VERIFIED_SHARE = 2   # out of 5 -> roughly 40% of the fleet


def cohort(install_date: str) -> str:
    for rev, lo, hi in COHORTS:
        if lo <= (install_date or "") < hi:
            return rev
    return "A"


def seen_by_service(serial: str) -> bool:
    h = hashlib.sha1(f"revreg::{serial}".encode()).hexdigest()
    return int(h, 16) % 5 < VERIFIED_SHARE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.exists(REGISTER_PATH) and not args.force:
        print(f"{REGISTER_PATH} already exists; use --force to rebuild")
        return 0

    if not os.path.exists(config.SANDBOX_DB):
        sys.exit(f"ERROR: synthetic source not found: {config.SANDBOX_DB}")
    con = sqlite3.connect(f"file:{config.SANDBOX_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    entries = {}
    for rec in con.execute("SELECT attrs FROM source_record "
                           "WHERE record_type='Asset' ORDER BY record_id"):
        a = json.loads(rec["attrs"] or "{}")
        serial = a.get("SerialNumber")
        if not serial:
            continue
        install = a.get("InstallDate")

        if serial in PIN_CONTRADICTORY:
            rev = PIN_CONTRADICTORY[serial]
        elif serial in PIN_VERIFIED:
            rev = PIN_VERIFIED[serial]
        elif serial in PIN_INFERRED:
            continue
        elif seen_by_service(serial):
            rev = cohort(install)
        else:
            continue

        entries[serial] = {
            "serial_number": serial,
            "verified_revision": rev,
            "verified_by": "svc.field.engineer",
            "verified_on": a.get("Date_of_Last_Meter_Recal__c") or install,
            "method": "hardware revision plate inspected during service visit",
        }
    con.close()

    payload = {
        "_comment": [
            "Fictional hardware-revision verification register.",
            "A revision is VERIFIED only if its serial appears here. A",
            "revision derived from the ERP install date alone is INFERRED",
            "and does not authorise revision-specific troubleshooting",
            "without human confirmation. See rule RV-1 in",
            "docs/SYNTHETIC_DATA_DICTIONARY.md.",
        ],
        "rule": "RV-1",
        "entries": entries,
    }
    os.makedirs(os.path.dirname(REGISTER_PATH), exist_ok=True)
    with open(REGISTER_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    print(f"wrote {REGISTER_PATH}")
    print(f"  verified entries : {len(entries)}")
    print(f"  pinned verified  : {sorted(PIN_VERIFIED)}")
    print(f"  pinned inferred  : {sorted(PIN_INFERRED)}")
    print(f"  pinned contradictory: {sorted(PIN_CONTRADICTORY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
