"""The arena.v2 event contract: vocabulary and strict validation.

Only identifiers, a short restricted label and enumerated states are accepted.
There is no field for prompts, message bodies, source code or tool output, and
unknown fields are rejected rather than ignored.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

STATES = ("queued", "arrived", "waiting", "executing", "completed", "failed", "canceled")
TERMINAL = frozenset({"completed", "failed", "canceled"})
KINDS = ("orchestration", "inference", "tool")
WAIT_REASONS = ("model", "human", "dependency", "tool", "capacity")
VISUAL_ACTIONS = ("triage", "retrieval", "evaluation", "validation", "review", "code_generation",
                  "testing", "tool_use", "inference", "waiting_model", "waiting_human",
                  "error_handling", "completion", "handoff", "idle")
TYPES = ("application.updated", "orchestrator.heartbeat", "agent.registered", "agent.updated",
         "agent.retired", "assignment.created", "assignment.moved", "assignment.state",
         "assignment.heartbeat", "handoff.sent", "handoff.received")

APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
IDENT = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
NODE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
LOCATION = re.compile(r"^(orch:[a-z0-9][a-z0-9-]{0,39}/[A-Za-z0-9_.-]{1,64}|head:[A-Za-z0-9_.-]{1,64})$")
TEXT = re.compile(r"^[A-Za-z0-9 .,:;()/_'-]*$")

COMMON = {"event_id", "occurred_at", "type", "orchestrator_id", "label",
          "source_event_type", "source_ref"}
ASSIGNMENT_FIELDS = {"assignment_id", "agent_id", "workflow_id", "task_id", "kind", "state",
                     "wait_reason", "visual_action", "model_id", "lease_seconds", "location",
                     "from_location", "to_location", "correlation_id"}
FIELDS = {
    "application.updated": {"display_name"},
    "orchestrator.heartbeat": {"display_name", "heartbeat_seconds"},
    "agent.registered": {"agent_id", "display_name", "role", "capabilities"},
    "agent.updated": {"agent_id", "display_name", "role", "capabilities"},
    "agent.retired": {"agent_id"},
    "assignment.created": ASSIGNMENT_FIELDS - {"from_location", "to_location"},
    "assignment.moved": ASSIGNMENT_FIELDS - {"location"},
    "assignment.state": ASSIGNMENT_FIELDS - {"location", "from_location", "to_location"},
    "assignment.heartbeat": {"assignment_id", "lease_seconds"},
    "handoff.sent": {"sender_agent_id", "receiver_agent_id", "correlation_id", "assignment_id"},
    "handoff.received": {"sender_agent_id", "receiver_agent_id", "correlation_id", "assignment_id"},
}
REQUIRED = {
    "application.updated": {"display_name"},
    "agent.registered": {"agent_id", "display_name", "role"},
    "agent.updated": {"agent_id"},
    "agent.retired": {"agent_id"},
    "assignment.created": {"assignment_id", "agent_id", "kind"},
    "assignment.moved": {"assignment_id", "to_location"},
    "assignment.state": {"assignment_id", "state"},
    "assignment.heartbeat": {"assignment_id", "lease_seconds"},
    "handoff.sent": {"sender_agent_id", "receiver_agent_id", "correlation_id"},
    "handoff.received": {"sender_agent_id", "receiver_agent_id", "correlation_id"},
}
ID_FIELDS = {"event_id", "orchestrator_id", "agent_id", "assignment_id", "workflow_id", "task_id",
             "model_id", "correlation_id", "sender_agent_id", "receiver_agent_id",
             "source_event_type", "source_ref", "role"}
MAX_AGE_SECONDS = 7 * 86400
MAX_FUTURE_SECONDS = 120


class ContractError(ValueError):
    """A rejected event; the message is a short machine-readable reason."""


def safe_text(value, field, limit):
    if not isinstance(value, str) or len(value) > limit or not TEXT.match(value):
        raise ContractError(f"{field}:invalid_text")
    lowered = value.lower()
    if "://" in lowered or "www." in lowered or re.search(r"\d{7,}", value):
        raise ContractError(f"{field}:sensitive_pattern")
    return value


def parse_time(value) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise ContractError("occurred_at:invalid")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("occurred_at:invalid") from None
    if moment.tzinfo is None:
        raise ContractError("occurred_at:timezone_required")
    epoch = moment.timestamp()
    now = time.time()
    if epoch > now + MAX_FUTURE_SECONDS or epoch < now - MAX_AGE_SECONDS:
        raise ContractError("occurred_at:out_of_window")
    return epoch


def validate(event) -> dict:
    """Return a normalized copy of one event or raise ContractError."""
    if not isinstance(event, dict):
        raise ContractError("event:not_object")
    kind = event.get("type")
    if kind not in TYPES:
        raise ContractError("type:unknown")
    allowed = COMMON | FIELDS[kind]
    extra = set(event) - allowed
    if extra:
        raise ContractError("field:unknown:" + sorted(extra)[0][:40])
    missing = ({"event_id", "occurred_at", "orchestrator_id"} | REQUIRED.get(kind, set())) - set(event)
    if missing:
        raise ContractError("field:missing:" + sorted(missing)[0])
    out = {"type": kind, "occurred_at_epoch": parse_time(event["occurred_at"])}
    for key, value in event.items():
        if key in ("type", "occurred_at"):
            continue
        if key in ID_FIELDS:
            if not isinstance(value, str) or not IDENT.match(value):
                raise ContractError(f"{key}:invalid_identifier")
        elif key in ("location", "from_location", "to_location"):
            if not isinstance(value, str) or not LOCATION.match(value):
                raise ContractError(f"{key}:invalid_location")
        elif key == "label":
            safe_text(value, key, 80)
        elif key == "display_name":
            if not value:
                raise ContractError("display_name:empty")
            safe_text(value, key, 60)
        elif key == "state" and value not in STATES:
            raise ContractError("state:unknown")
        elif key == "kind" and value not in KINDS:
            raise ContractError("kind:unknown")
        elif key == "wait_reason" and value not in WAIT_REASONS:
            raise ContractError("wait_reason:unknown")
        elif key == "visual_action" and value not in VISUAL_ACTIONS:
            raise ContractError("visual_action:unknown")
        elif key in ("lease_seconds", "heartbeat_seconds"):
            low, high = (5, 86400) if key == "lease_seconds" else (5, 300)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ContractError(f"{key}:out_of_range")
        elif key == "capabilities":
            if (not isinstance(value, list) or len(value) > 16
                    or not all(isinstance(c, str) and IDENT.match(c) for c in value)):
                raise ContractError("capabilities:invalid")
        out[key] = value
    if kind == "assignment.state" and out["state"] == "waiting" and "wait_reason" not in out:
        out["wait_reason"] = "dependency"
    return out
