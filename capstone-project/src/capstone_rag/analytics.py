"""Trends and improvement analytics over recorded cases.

Everything here reads the operational tables and reports what actually
happened. Two things it is careful about:

**Replay isolation.** Every operational row carries a `run_id` which is NULL
for live operation and set for an evaluation replay. The default view counts
live cases only, so re-running an evaluation twenty times does not make it
look as though twenty more customers complained or twenty more parcels
shipped. A replay can be inspected on its own by passing its run_id.

**Denominators.** A raw count of failures by model says more about how many
of that model are in the field than about reliability. Where a denominator
exists - the number of devices of that model on the books - the rate is
reported alongside the count, and where it does not, the count is labelled
as a count.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from . import caseflow, movement as movement_mod, triage as triage_mod


def _iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _hours(a, b):
    da, db = _iso(a), _iso(b)
    if not da or not db:
        return None
    return round((db - da).total_seconds() / 3600.0, 2)


class Analytics:
    def __init__(self, store, run_id: Optional[str] = None,
                 include_eval: bool = False):
        self.store = store
        self.run_id = run_id
        self.include_eval = include_eval

    # -- scoping ---------------------------------------------------------

    def _where(self, alias="") -> tuple:
        p = f"{alias}." if alias else ""
        if self.include_eval:
            return "1=1", ()
        if self.run_id is None:
            return f"{p}run_id IS NULL", ()
        return f"{p}run_id = ?", (self.run_id,)

    def cases(self) -> list:
        w, params = self._where()
        return self.store.query(f"SELECT * FROM cases WHERE {w}", params)

    def _rows(self, table) -> list:
        w, params = self._where()
        return self.store.query(f"SELECT * FROM {table} WHERE {w}", params)

    # -- sections --------------------------------------------------------

    def safety(self) -> dict:
        cases = self.cases()
        notes = self._rows("qa_notifications")
        confirmed = [c for c in cases if c["is_adverse_event"]
                     and c["ae_certainty"] == "confirmed"]
        suspected = [c for c in cases if c["is_adverse_event"]
                     and c["ae_certainty"] == "suspected"]
        hazards = [c for c in cases
                   if c["classification"] == "device_safety_hazard"]
        return {
            "adverse_events_confirmed": len(confirmed),
            "adverse_events_suspected": len(suspected),
            "device_safety_hazards": len(hazards),
            "qa_notifications": len(notes),
            "notifications_by_kind": dict(Counter(n["kind"] for n in notes)),
            "awaiting_human_determination": len(
                [n for n in notes if n["status"] == "open"]),
            "case_ids": [c["case_id"] for c in confirmed + suspected + hazards],
        }

    def complaints(self) -> dict:
        cases = self.cases()
        comp = [c for c in cases if c["is_complaint"]]
        return {
            "total_cases": len(cases),
            "complaints": len(comp),
            "complaint_rate": (round(len(comp) / len(cases), 3)
                               if cases else None),
            "by_classification": dict(Counter(
                c["classification"] for c in comp)),
            "non_complaints_by_classification": dict(Counter(
                c["classification"] for c in cases if not c["is_complaint"])),
        }

    def failures_by_model(self) -> dict:
        """Counts, and rates against the installed base where one exists."""
        cases = [c for c in self.cases() if c["is_complaint"] and c["model"]]
        counts = Counter(c["model"] for c in cases)
        fleet = {r["model"]: r["n"] for r in self.store.query(
            "SELECT model, COUNT(*) n FROM devices GROUP BY model")}
        out = {}
        for model, n in counts.most_common():
            base = fleet.get(model)
            out[model] = {
                "complaints": n,
                "devices_on_file": base,
                "complaints_per_device": (round(n / base, 4) if base else None),
                "note": ("rate is against devices on file, not a field "
                         "failure rate" if base else
                         "no denominator available; this is a raw count"),
            }
        return out

    def symptom_clusters(self, min_count=2) -> list:
        cases = [c for c in self.cases() if c["is_complaint"]]
        pair = Counter((c["model"], c["classification"]) for c in cases
                       if c["model"])
        return [{"model": m, "classification": k, "count": n}
                for (m, k), n in pair.most_common() if n >= min_count]

    def documentation_gaps(self) -> dict:
        """Where the agent looked for a procedure and found nothing usable."""
        w, params = self._where("c")
        rows = self.store.query(
            f"SELECT c.model, c.revision, COUNT(*) n FROM cases c "
            f"WHERE {w} AND c.is_complaint=1 AND c.case_id NOT IN "
            f"(SELECT DISTINCT case_id FROM retrieved_sources "
            f" WHERE status='USED') AND c.model IS NOT NULL "
            f"GROUP BY c.model, c.revision ORDER BY n DESC", params)
        rejected = self.store.query(
            "SELECT procedure_id, reason, COUNT(*) n FROM retrieved_sources "
            "WHERE status='REJECT' GROUP BY procedure_id, reason "
            "ORDER BY n DESC LIMIT 10")
        return {
            "complaints_with_no_usable_procedure": rows,
            "most_common_rejections": rejected,
        }

    def identification(self) -> dict:
        cases = self.cases()
        with_device = [c for c in cases if c["serial_number"]]
        verified = [c for c in cases if c["identity_verified"]]
        points = Counter()
        for c in cases:
            for p in (json.loads(c["verification_points"] or "[]") or []):
                points[p] += 1
        return {
            "cases": len(cases),
            "device_identified": len(with_device),
            "device_identified_rate": (round(len(with_device) / len(cases), 3)
                                       if cases else None),
            "identity_verified": len(verified),
            "identity_verified_rate": (round(len(verified) / len(cases), 3)
                                       if cases else None),
            "points_used": dict(points),
            "unverified_cases": len(cases) - len(verified),
        }

    def retrieval_quality(self) -> dict:
        w, params = self._where()
        used = self.store.query(
            f"SELECT procedure_id, COUNT(*) n FROM retrieved_sources "
            f"WHERE {w} AND status='USED' GROUP BY procedure_id "
            f"ORDER BY n DESC", params)
        rej = self.store.one(
            f"SELECT COUNT(*) c FROM retrieved_sources WHERE {w} "
            f"AND status='REJECT'", params)["c"]
        tot = self.store.one(
            f"SELECT COUNT(*) c FROM retrieved_sources WHERE {w}",
            params)["c"]
        cases = self.cases()
        grounded = [c for c in cases if c["status"] not in (
            caseflow.NEW,)]
        return {
            "chunks_considered": tot,
            "chunks_rejected": rej,
            "rejection_rate": round(rej / tot, 3) if tot else None,
            "procedures_used": used,
            "cases_reaching_a_procedure": len(
                {r["case_id"] for r in self.store.query(
                    f"SELECT DISTINCT case_id FROM retrieved_sources "
                    f"WHERE {w} AND status='USED'", params)}),
            "cases_investigated": len(grounded),
        }

    def corrections(self) -> dict:
        w, params = self._where()
        items = self.store.query(
            f"SELECT * FROM checklist_items WHERE {w} AND "
            f"approval_decision IS NOT NULL", params)
        rejected = [i for i in items if i["approval_decision"] == "rejected"]
        recs = self.store.query(
            f"SELECT * FROM recommendations WHERE {w}", params)
        proposals = self.store.query("SELECT * FROM improvement_proposals")
        return {
            "checklist_items_decided": len(items),
            "checklist_items_rejected": len(rejected),
            "correction_rate": (round(len(rejected) / len(items), 3)
                                if items else None),
            "most_corrected_actions": dict(Counter(
                i["action"] for i in rejected).most_common(5)),
            "responses_regenerated": len(
                [r for r in recs if r["kind"] == "response"]),
            "improvement_proposals": len(proposals),
            "proposals_pending_review": len(
                [p for p in proposals
                 if p["review_status"] == "pending_review"]),
        }

    def movements(self) -> dict:
        w, params = self._where()
        rmas = self.store.query(f"SELECT * FROM rmas WHERE {w}", params)
        by_type = Counter(r["movement_type"] for r in rmas)
        by_status = Counter(r["status"] for r in rmas)
        approved = [r for r in rmas if r["status"] in ("approved", "executed")]
        decided = [r for r in rmas
                   if r["status"] in ("approved", "executed", "rejected")]

        # Whole-device returns on cases that only ever described a part.
        avoidable = []
        for r in rmas:
            if r["movement_type"] != movement_mod.DEVICE_RETURN:
                continue
            msg = self.store.one(
                "SELECT body FROM messages WHERE case_id=? AND "
                "direction='inbound' ORDER BY created_at LIMIT 1",
                (r["case_id"],))
            text = (msg or {}).get("body") or ""
            parts = [p for p in movement_mod.mentioned_parts(text)
                     if p.get("customer_replaceable")]
            if parts and r["reason"] and "safety" not in r["reason"]:
                avoidable.append({"rma_id": r["rma_id"],
                                  "case_id": r["case_id"],
                                  "parts_mentioned": [p["name"]
                                                      for p in parts]})
        return {
            "rmas": len(rmas),
            "by_movement_type": dict(by_type),
            "by_status": dict(by_status),
            "approval_rate": (round(len(approved) / len(decided), 3)
                              if decided else None),
            "part_shipments": by_type.get(movement_mod.PART_OUTBOUND, 0),
            "device_returns": by_type.get(movement_mod.DEVICE_RETURN, 0),
            "potentially_avoidable_device_returns": avoidable,
        }

    def shipping(self) -> dict:
        w, params = self._where()
        ships = self.store.query(f"SELECT * FROM sim_shipments WHERE {w}",
                                 params)
        return {
            "shipments": len(ships),
            "by_direction": dict(Counter(s["direction"] for s in ships)),
            "by_status": dict(Counter(s["status"] for s in ships)),
            "failed": len([s for s in ships if s["status"] == "failed"]),
        }

    def lifecycle(self) -> dict:
        cases = self.cases()
        open_cases = [c for c in cases
                      if c["status"] not in (caseflow.RESOLVED,
                                             caseflow.CLOSED)]
        resolved = [c for c in cases if c["resolved"] == 1]
        unresolved_closed = [c for c in cases if c["status"] == caseflow.CLOSED
                             and not c["resolved"]]
        ages, times = [], []
        for c in cases:
            a = _hours(c["created_at"], c["closed_at"] or c["updated_at"])
            if a is not None:
                ages.append(a)
            if c["closed_at"]:
                t = _hours(c["created_at"], c["closed_at"])
                if t is not None:
                    times.append(t)
        return {
            "total": len(cases),
            "by_status": dict(Counter(c["status"] for c in cases)),
            "by_priority": dict(Counter(c["priority"] for c in cases)),
            "open": len(open_cases),
            "resolved": len(resolved),
            "closed_without_resolution": len(unresolved_closed),
            "closed_unresolved_ids": [c["case_id"] for c in unresolved_closed],
            "mean_age_hours": (round(sum(ages) / len(ages), 2)
                               if ages else None),
            "mean_time_to_close_hours": (round(sum(times) / len(times), 2)
                                         if times else None),
            "ai_states": dict(Counter(c["ai_state"] for c in cases)),
        }

    def escalation(self) -> dict:
        cases = self.cases()
        needing = [c for c in cases if c["status"] == caseflow.HUMAN_REVIEW]
        chat_handoffs = [c for c in cases if c["channel"] == "chat"]
        by_account = Counter(c["account_id"] for c in cases
                             if c["account_id"])
        repeats = {a: n for a, n in by_account.items() if n > 1}
        return {
            "needing_human_review": len(needing),
            "human_review_rate": (round(len(needing) / len(cases), 3)
                                  if cases else None),
            "chat_handoffs": len(chat_handoffs),
            "customers_contacting_more_than_once": len(repeats),
            "repeat_contact_accounts": dict(
                sorted(repeats.items(), key=lambda kv: -kv[1])[:5]),
        }

    # -- assembly ---------------------------------------------------------

    def report(self) -> dict:
        return {
            "scope": {"run_id": self.run_id,
                      "include_evaluation_runs": self.include_eval,
                      "note": ("live operational records only; evaluation "
                               "replays are excluded so repeated test runs "
                               "cannot inflate business counts")
                      if not self.include_eval else
                      "INCLUDES evaluation replays"},
            "safety": self.safety(),
            "complaints": self.complaints(),
            "failures_by_model": self.failures_by_model(),
            "symptom_clusters": self.symptom_clusters(),
            "documentation_gaps": self.documentation_gaps(),
            "identification": self.identification(),
            "retrieval": self.retrieval_quality(),
            "corrections": self.corrections(),
            "movements": self.movements(),
            "shipping": self.shipping(),
            "lifecycle": self.lifecycle(),
            "escalation": self.escalation(),
        }

    def proposed_actions(self) -> list:
        """What the improvement view should put in front of a supervisor."""
        out = []
        s = self.safety()
        if s["adverse_events_confirmed"] or s["adverse_events_suspected"]:
            out.append({
                "priority": "high",
                "area": "adverse events",
                "action": (f"Review {s['adverse_events_confirmed']} confirmed "
                           f"and {s['adverse_events_suspected']} suspected "
                           f"adverse events; "
                           f"{s['awaiting_human_determination']} notification"
                           f"(s) still need a determination."),
                "evidence": s["case_ids"][:10]})

        gaps = self.documentation_gaps()["complaints_with_no_usable_procedure"]
        if gaps:
            top = gaps[0]
            out.append({
                "priority": "medium",
                "area": "documentation coverage",
                "action": (f"{sum(g['n'] for g in gaps)} complaint(s) found "
                           f"no usable approved procedure. The largest gap is "
                           f"{top['model']} Rev {top['revision']} "
                           f"({top['n']} case(s)). Authoring a procedure "
                           f"there would close the most cases."),
                "evidence": gaps[:5]})

        mv = self.movements()
        if mv["potentially_avoidable_device_returns"]:
            out.append({
                "priority": "medium",
                "area": "avoidable returns",
                "action": (f"{len(mv['potentially_avoidable_device_returns'])}"
                           f" whole-device return(s) were raised on cases "
                           f"that only described a replaceable part. Check "
                           f"whether a part shipment would have done."),
                "evidence": mv["potentially_avoidable_device_returns"][:5]})

        corr = self.corrections()
        if corr["most_corrected_actions"]:
            act, n = next(iter(corr["most_corrected_actions"].items()))
            out.append({
                "priority": "medium",
                "area": "agent accuracy",
                "action": (f"'{act}' has been corrected by a representative "
                           f"{n} time(s). "
                           f"{corr['proposals_pending_review']} improvement "
                           f"proposal(s) are waiting for review. Reviewed "
                           f"proposals do not change controlled procedures."),
                "evidence": corr["most_corrected_actions"]})

        life = self.lifecycle()
        if life["closed_without_resolution"]:
            out.append({
                "priority": "low",
                "area": "unresolved closures",
                "action": (f"{life['closed_without_resolution']} case(s) were "
                           f"closed without being resolved and remain "
                           f"identifiable in reporting."),
                "evidence": life["closed_unresolved_ids"][:10]})

        clusters = self.symptom_clusters(min_count=3)
        if clusters:
            c = clusters[0]
            out.append({
                "priority": "medium",
                "area": "complaint cluster",
                "action": (f"{c['count']} cases of {c['classification']} on "
                           f"{c['model']}. Worth checking against the "
                           f"installed base before drawing a conclusion."),
                "evidence": clusters[:5]})
        return out


def render(report: dict) -> str:
    lines = ["TRENDS AND IMPROVEMENT", "=" * 74,
             report["scope"]["note"], ""]
    for section, data in report.items():
        if section == "scope":
            continue
        lines.append(f"-- {section} " + "-" * (70 - len(section)))
        if isinstance(data, dict):
            for k, v in data.items():
                lines.append(f"   {k:<42} {v}")
        else:
            for row in data:
                lines.append(f"   {row}")
        lines.append("")
    return "\n".join(lines)
