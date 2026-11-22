"""Validate, follow and summarize the gateway's published shared.inference.observation/1 document.

The gateway's operator runs `inference-observe` under the gateway's own identity and publishes its
output by atomic file replacement (docs/handoff/GATEWAY-OBSERVATION-INTEGRATION.md). Arena reads
only that publication. Every record is copied through an allowlist, so a field the gateway adds
later is never shown until Arena knows what it means, and a bad record is dropped, not guessed at.
The summary keeps the gateway's own words: allocation utilization is leased eligible heads over
healthy fresh heads, never CPU or GPU load, and an enabled application is authorized, not alive.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from collections import deque

CONTRACT = "shared.inference.observation/1"
MAX_BYTES = 4 * 1024 * 1024
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$")
EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
DIGEST = re.compile(r"^[A-Za-z0-9:._-]{1,100}$")
HEAD_NODE = re.compile(r"^head-192-0-2-(\d{1,3})$")
RESOURCE_TYPES = {"shared_inference", "application_exclusive", "exclusive_development_device", "exclusive_test_device",
                  "shared_test_device", "infrastructure_service", "unknown"}
HEALTH = {"healthy", "degraded", "offline", "quarantined", "maintenance", "unqualified", "retired"}
REQUEST_STATES = ("queued", "leased", "cancel_requested", "completed", "cancelled", "expired")
INVOCATION_STATES = ("active", "completed", "failed", "cancelled", "cancel_requested", "unknown")
LEASE_STATES = {"leased", "cancel_requested"}
TROUBLE_EVENTS = {"overload.rejected": "overload rejections", "authorization.withdrawn": "authorizations withdrawn",
                  "service.admission_drained": "admission drained", "service.restart_reconciled": "restart reconciled"}
RECENT_EVENTS = 200
TROUBLE_WINDOW = 300          # seconds of recent trouble events that raise a warning
FAILURE_ROWS = 50             # the gateway's own cap on last_hour.invocation_failures
# The gateway's failure classes (its recovery.classify), worded as what happened to the request.
FAILURE_KINDS = {"deadline_exceeded": "timed out", "uncertain_execution": "failed mid-call",
                 "head_unreachable": "failed: head unreachable", "model_on_head": "failed: model error on the head",
                 "request_fault": "failed: request fault"}


class ObservationError(ValueError):
    pass


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("number")
    return float(value)


def _int(value, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("integer")
    return value


def _id(value, pattern=ID):
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError("identifier")
    return value


def _choice(value, allowed):
    if value not in allowed:
        raise ValueError("value")
    return value


def _flag(value):
    if value in (True, 1) and not isinstance(value, float):
        return True
    if value in (False, 0) and not isinstance(value, float):
        return False
    raise ValueError("flag")


def parse(doc):
    """One observation document -> allowlisted records. Structural faults reject the document;
    a malformed record is dropped and counted."""
    if not isinstance(doc, dict) or doc.get("contract") != CONTRACT:
        raise ObservationError("not a shared.inference.observation/1 document")
    try:
        observed, next_event = _num(doc.get("observed_at")), _int(doc.get("next_event_id"), 0, 2 ** 63)
    except ValueError as exc:
        raise ObservationError(f"observed_at/next_event_id: {exc}") from None
    if not isinstance(doc.get("admission_open"), bool):
        raise ObservationError("admission_open")
    dropped = 0

    def records(key, limit, build):
        nonlocal dropped
        items = doc.get(key)
        if not isinstance(items, list):
            raise ObservationError(key)
        out = []
        for item in items[:limit]:
            try:
                out.append(build(item))
            except (KeyError, TypeError, ValueError):
                dropped += 1
        dropped += max(0, len(items) - limit)
        return out

    def counts(key, states):
        given = doc.get(key)
        if not isinstance(given, dict):
            raise ObservationError(key)
        valid = {k: v for k, v in given.items() if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2 ** 53}
        out = {s: valid.get(s, 0) for s in states}
        out["other"] = sum(v for k, v in valid.items() if k not in states)
        return out

    def models(value):
        if not isinstance(value, list):
            raise ValueError("models")
        return sorted({m for m in value[:64] if isinstance(m, str) and ID.match(m)})

    def last_hour():
        """Optional: gateway 0.3.0 added the failed count, 0.4.5 the breakdown by head, model and class.
        Absent or malformed means not published (None), never zero failures."""
        nonlocal dropped
        given = doc.get("last_hour")
        out = {"failed": None, "failures": None, "failures_omitted": 0}
        if not isinstance(given, dict):
            return out
        updated = given.get("invocations_updated")
        if isinstance(updated, dict):
            try:
                out["failed"] = _int(updated.get("failed", 0), 0, 2 ** 53)
            except ValueError:
                pass
        rows = given.get("invocation_failures")
        if isinstance(rows, list):
            out["failures"] = []
            for f in rows[:FAILURE_ROWS]:
                try:
                    out["failures"].append({
                        "resource_id": None if f.get("resource_id") is None else _id(f["resource_id"]),
                        "model": None if f.get("model") is None else _id(f["model"]),
                        "app_id": _id(f["app_id"]), "class": f["class"] if f.get("class") in FAILURE_KINDS else None,
                        "count": _int(f["count"], 1, 2 ** 53), "last_at": _num(f["last_at"])})
                except (AttributeError, KeyError, TypeError, ValueError):
                    dropped += 1
            dropped += max(0, len(rows) - FAILURE_ROWS)
            try:
                out["failures_omitted"] = _int(given.get("invocation_failures_omitted", 0), 0, 2 ** 53)
            except ValueError:
                pass
        return out

    return {
        "observed_at": observed, "admission_open": doc["admission_open"], "next_event_id": next_event,
        "applications": records("applications", 256, lambda a: {
            "app_id": _id(a["app_id"]), "enabled": _flag(a["enabled"]), "max_concurrency": _int(a["max_concurrency"], 0, 1024),
            "queue_limit": _int(a["queue_limit"], 0, 10000), "priority": _int(a["priority"], 0, 10)}),
        "resources": records("resources", 1024, lambda r: {
            "resource_id": _id(r["resource_id"]),
            "resource_type": r["resource_type"] if r["resource_type"] in RESOURCE_TYPES else "unknown",
            "qualified_models": models(r["qualified_models"]), "health": r["health"] if r["health"] in HEALTH else "unknown",
            "last_seen": _num(r["last_seen"]), "fresh": _flag(r["fresh"])}),
        "requests": counts("requests", REQUEST_STATES),
        "invocations": counts("invocations", INVOCATION_STATES),
        "leases": records("leases", 1024, lambda l: {
            "request_id": _id(l["request_id"]), "app_id": _id(l["app_id"]), "resource_id": _id(l["resource_id"]),
            "lease_id": _id(l["lease_id"]), "status": _choice(l["status"], LEASE_STATES),
            "expires": _num(l["expires"]), "deadline": _num(l["deadline"]), "executing": _flag(l["executing"])}),
        "qualifications": records("qualification_observations", 1000, lambda q: {
            "evidence_id": _id(q["evidence_id"]), "resource_id": _id(q["resource_id"]), "model": _id(q["model"]),
            "digest": q["digest"] if isinstance(q.get("digest"), str) and DIGEST.match(q["digest"]) else None,
            "passed": _flag(q["passed"]), "at": _num(q["at"])}),
        "events": records("events", 1000, lambda e: {
            "event_id": _int(e["event_id"], 1, 2 ** 63), "app_id": _id(e["app_id"]),
            "request_id": None if e.get("request_id") is None else _id(e["request_id"]),
            "event_type": _id(e["event_type"], EVENT_TYPE),
            "at": _num(e["at"])}),
        "last_hour": last_hour(),
        "dropped": dropped,
    }


def head_for(resource_id, resource_heads):
    """The Arena head a gateway resource is, by the operator's mapping or by sharing the Arena node
    ID (head-192-0-2-N); anything else stays a gateway resource Arena cannot place."""
    node = resource_heads.get(resource_id) or (resource_id if HEAD_NODE.match(resource_id) else None)
    if not node or not HEAD_NODE.match(node):
        return None, None
    return f"head:{node}", f"Head {HEAD_NODE.match(node).group(1)}"


def summarize(obs, now, *, stale_seconds=30, resource_heads=None):
    resource_heads = resource_heads or {}
    leases = {}
    for lease in obs["leases"]:
        head, name = head_for(lease["resource_id"], resource_heads)
        leases.setdefault(lease["resource_id"], []).append(dict(lease, head=head, head_name=name,
                                                                overdue=lease["expires"] < obs["observed_at"]))
    heads, others, models = [], [], {}
    for r in obs["resources"]:
        if r["resource_type"] != "shared_inference":
            others.append({k: r[k] for k in ("resource_id", "resource_type", "health", "fresh")})
            continue
        head, name = head_for(r["resource_id"], resource_heads)
        available = r["health"] == "healthy" and r["fresh"]
        heads.append(dict(r, head=head, head_name=name, available=available,
                          leased_by=[l["app_id"] for l in leases.get(r["resource_id"], [])]))
        for m in r["qualified_models"]:
            entry = models.setdefault(m, {"model": m, "available_on": [], "listed_on": []})
            entry["listed_on"].append(r["resource_id"])
            if available:
                entry["available_on"].append(r["resource_id"])
    eligible = [h for h in heads if h["available"]]
    latest = {}
    for q in sorted(obs["qualifications"], key=lambda q: -q["at"]):
        latest.setdefault((q["resource_id"], q["model"]), q)
    # Requests that failed, by the head they ran on: a request failure, not the head's own health.
    hour, by_id = obs.get("last_hour") or {}, {h["resource_id"]: h for h in heads}
    failures = []
    for f in hour.get("failures") or []:
        head, name = head_for(f["resource_id"], resource_heads) if f["resource_id"] else (None, None)
        failures.append(dict(f, head=head, head_name=name, kind=FAILURE_KINDS.get(f["class"], "failed"),
                             head_available=by_id[f["resource_id"]]["available"] if f["resource_id"] in by_id else None))
    return {
        "state": "stale" if now - obs["observed_at"] > stale_seconds else "ok",
        "observed_at": obs["observed_at"], "age": max(0.0, now - obs["observed_at"]), "admission_open": obs["admission_open"],
        "applications": obs["applications"], "heads": heads, "other_resources": others,
        "models": sorted(models.values(), key=lambda m: m["model"]),
        "requests": obs["requests"], "invocations": obs["invocations"],
        "queued": obs["requests"]["queued"], "active": len(obs["leases"]),
        "executing": sum(l["executing"] for l in obs["leases"]),
        "leases": sorted((l for ls in leases.values() for l in ls), key=lambda l: l["deadline"]),
        "heads_up": len(eligible), "heads_total": len(heads),
        # Allocation, not measured load; no eligible head means unavailable, not 0 %.
        "utilization": sum(1 for h in eligible if h["leased_by"]) / len(eligible) if eligible else None,
        "qualifications": sorted(latest.values(), key=lambda q: (q["resource_id"], q["model"])),
        # Last hour: None = the gateway doesn't publish it (failures: before 0.4.5), never "no failures".
        "failed_last_hour": hour.get("failed"), "breakdown_published": hour.get("failures") is not None,
        "request_failures": sorted(failures, key=lambda f: (-f["count"], -f["last_at"])),
        "request_failures_omitted": hour.get("failures_omitted", 0),
        "dropped": obs["dropped"],
    }


class Observer:
    """Follows the published observation file. Snapshots replace each other; events accumulate,
    deduplicated by this gateway's event IDs (the cursor). A cursor that goes backwards is a reset
    or a foreign ledger: events stop merging until an operator restarts the console."""

    def __init__(self, path, *, gateway_id="gateway", stale_seconds=30, resource_heads=None):
        self.path, self.gateway_id = str(path), gateway_id
        self.stale_seconds, self.resource_heads = float(stale_seconds), dict(resource_heads or {})
        self.cursor, self.recent, self.resync = 0, deque(maxlen=RECENT_EVENTS), False
        self.obs, self.error, self._signature = None, None, None

    def read(self):
        try:
            st = os.stat(self.path)
            signature = (st.st_mtime_ns, st.st_size)
            if signature == self._signature:
                return
            if st.st_size > MAX_BYTES:
                raise ObservationError("observation file is too large")
            with open(self.path, "rb") as handle:
                obs = parse(json.loads(handle.read(MAX_BYTES + 1)))
        except FileNotFoundError:
            self.error = None if self.obs is None else "observation file is gone"
            self._signature = None
            return
        except (OSError, ValueError) as exc:      # unreadable, mid-write by a non-atomic publisher, or not the contract
            self.error, self._signature = f"{type(exc).__name__}: {exc}"[:160], None
            return
        self._signature, self.error = signature, None
        if obs["next_event_id"] < self.cursor:
            self.resync = True
        if not self.resync:
            for event in obs["events"]:
                if event["event_id"] > self.cursor:
                    self.recent.append(event)
                    self.cursor = event["event_id"]
        self.obs = obs

    def view(self, now=None):
        now = now or time.time()
        self.read()
        if self.obs is None:
            return {"state": "invalid" if self.error else "not_published", "gateway_id": self.gateway_id, "error": self.error}
        view = summarize(self.obs, now, stale_seconds=self.stale_seconds, resource_heads=self.resource_heads)
        if self.error:
            view["state"] = "invalid"
        elif self.resync:
            view["state"] = "resync"
        recent = list(self.recent)
        trouble = {}
        for e in recent:
            if e["event_type"] in TROUBLE_EVENTS and now - e["at"] <= TROUBLE_WINDOW:
                trouble[e["event_type"]] = trouble.get(e["event_type"], 0) + 1
        view.update(gateway_id=self.gateway_id, error=self.error, cursor=self.cursor, trouble=trouble,
                    recent_events=recent[-50:][::-1])
        return view


def issues(view):
    """Warnings for the console's issue list, in its {who, what, level} shape."""
    who, out = "GATEWAY", []
    state = view["state"]
    if state == "not_published":
        return [{"who": who, "what": "no observation published yet", "level": "warn"}]
    if state == "invalid":
        out.append({"who": who, "what": "observation unreadable", "level": "warn"})
        if "observed_at" not in view:
            return out
    if state == "stale":
        out.append({"who": who, "what": f"last observation {int(view['age'])} s ago", "level": "warn"})
    if state == "resync":
        out.append({"who": who, "what": "event cursor went backwards: resynchronize", "level": "warn"})
    if not view["admission_open"]:
        out.append({"who": who, "what": "admission closed", "level": "warn"})
    names = {h["resource_id"]: (h["head_name"] or h["resource_id"]).upper() for h in view["heads"]}
    for h in view["heads"]:
        if not h["available"] and h["health"] != "retired":
            what = h["health"] if h["fresh"] or h["health"] != "healthy" else "not reporting"
            out.append({"who": names[h["resource_id"]], "what": f"gateway: {what}", "level": "warn"})
    for q in view["qualifications"]:
        if not q["passed"]:
            out.append({"who": names.get(q["resource_id"], q["resource_id"].upper()),
                        "what": f"failed qualification for {q['model']}", "level": "warn"})
    # Request failures, worded as requests (a head that is down has its own warning above).
    for f in view.get("request_failures", []):
        n, model = f["count"], f["model"] or "unknown model"
        what = f"{n} {model} request{'s' if n != 1 else ''} {f['kind']} in the last hour"
        if f["resource_id"] is None:
            out.append({"who": who, "what": f"{what}, before reaching a head", "level": "warn"})
            continue
        suffix = "; head healthy" if f["head_available"] else ""
        out.append({"who": names.get(f["resource_id"]) or (f["head_name"] or f["resource_id"]).upper(),
                    "what": f"gateway: {what}{suffix}", "level": "warn"})
    if view.get("request_failures_omitted"):
        out.append({"who": who, "what": f"{view['request_failures_omitted']} more failed requests in the last hour", "level": "warn"})
    if not view.get("breakdown_published") and view.get("failed_last_hour"):
        out.append({"who": who, "what": f"{view['failed_last_hour']} failed requests in the last hour (no per-head detail published)",
                    "level": "warn"})
    for kind, n in view.get("trouble", {}).items():
        out.append({"who": who, "what": f"{TROUBLE_EVENTS[kind]} in 5 min: {n}", "level": "warn"})
    return out
