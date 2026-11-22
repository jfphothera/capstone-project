"""Pure translation of Capstone `arena_events` rows (schema 1.0) into arena.v2.

The translator is deterministic: the same input rows always produce the same
event IDs, so re-publishing after a restart is idempotent at the service.

Known Capstone instrumentation gaps are worked around here and named in each
event's `source_ref` (see CODEX-INTEGRATION-REPORT.md C1-C4):
  E1  inference events carry the task instance in workflow_id, not task_id
  E3  task.started.execution_node names a head; the logical task stays home
  E4  inference agent_id is the route role, which may not be a registered agent
"""
from __future__ import annotations

import json
import re

from ...appearance import CAPSTONE_WORKFLOW, SUB_ROLES

APP_ID = "capstone"
APP_DISPLAY = "Ticketing"
ORCHESTRATOR = "main"
HOME = f"orch:{APP_ID}/{ORCHESTRATOR}"
HEAD = re.compile(r"^head-[0-9]+-[0-9]+-[0-9]+-[0-9]+$")
INFERENCE_LEASE = 120          # Capstone endpoint timeout default
HUMAN_LEASE = 86400            # human review legitimately waits a long time
LAB_REVIEW_LEASE = 1800        # the Testing Lab's review is the simulated reviewer: minutes, not a day (2.5.1)

# Capstone registry role -> shared visual-action taxonomy (TARGET-ARCHITECTURE section 4).
ROLE_ACTIONS = {
    "intake": "triage", "identity": "retrieval", "controlled-evidence": "retrieval",
    "safety": "evaluation", "case-evidence": "review", "planning": "evaluation",
    "operations": "evaluation", "communication": "review", "validation": "validation",
    "artifact-rework": "review", "reviewed-recall": "retrieval", "coordination": "triage",
    "lab-reviewer": "review", "lab-probe": "testing", "lab-customer": "review", "lab-environment": "tool_use",
    "lab-role-screening": "evaluation", "chat": "review",
}

# Testing Lab participants: Capstone agents that are NOT part of the ticket workflow under test (Capstone
# 6.0.0-rc.23: the agentic Testing Lab's simulated human reviewer; then its probe, simulated customer and
# environment, and the role-screening calls that carry no agent). Declared here so each figure keeps its own
# name and role whatever registration rows the adapter has seen. Their calls are never folded into a workflow
# agent's task, and their assignments carry "<workflow>/<agent>" so a workflow's own agent count is unchanged.
LAB_SCREENING = "lab_role_screening"
LAB_PARTICIPANTS = {
    "lab_reviewer": {"display_name": "Testing Lab Reviewer", "role": "lab-reviewer"},
    "testing_lab_probe": {"display_name": "Testing Lab Probe", "role": "lab-probe"},
    "lab-customer-simulator": {"display_name": "Testing Lab Customer", "role": "lab-customer"},
    "lab-environment-simulator": {"display_name": "Testing Lab Environment", "role": "lab-environment"},
    LAB_SCREENING: {"display_name": "Testing Lab Role Screening", "role": "lab-role-screening"},
}

# Figures Arena derives itself (Arena 2.4.0). A model call with no agent outside the Testing Lab is a chat turn;
# a workflow agent's call whose route_role is a sub-role (appearance.SUB_ROLES) is drawn on "<agent>.<route_role>",
# its own figure, so the parent's task neither moves nor closes for it. Both use the "<workflow>/<agent>" scope too.
CHAT_AGENT = "chat_assistant"
DERIVED = {CHAT_AGENT: {"display_name": "Chat", "role": "chat"}}
SUB_ROLE_ACTIONS = {"retrieval_embedding": "retrieval", "tool_delegate": "tool_use", "reflection": "review",
                    "peer_reviewer": "review", "supervisor": "evaluation", "investigation": "tool_use",
                    "customer_communication": "review"}
# The contract's display_name alphabet (contract.TEXT) has no middle dot, so "Operations · Investigation" is sent as:
SUB_ROLE_SEPARATOR = " - "
LAB_SUMMARY = "EXPERIMENT "     # Capstone telemetry.emit: every Testing Lab mirrored row; its workflow_id is the run id

QUEUED = {"task.created", "task.eligible", "task.queued", "task.assigned", "dependency.released"}
WAITING = {"task.waiting": "dependency", "task.waiting_tool": "tool", "dependency.blocked": "dependency",
           "task.blocked": "dependency", "task.escalated": "human"}
TERMINAL = {"task.completed": "completed", "task.failed": "failed", "task.canceled": "canceled",
            "join.completed": "completed", "workflow.completed": "completed"}
IGNORED = {"worker.registered", "worker.recovered", "worker.unavailable", "task.late_result_rejected"}
HEARTBEATS = {"task.heartbeat", "inference.heartbeat"}   # every 30-60 s while long work continues
HEARTBEAT_LEASE = 120                                     # unless the row's detail gives lease_seconds
INTERRUPTED = {"inference.interrupted", "task.interrupted"}   # Capstone rc.25 A1: a stopped worker's open work (2.5.1)
INFERENCE_KINDS = {"inference.queued", "inference.started", "inference.completed", "inference.failed"}
SAFE_LABEL = re.compile(r"[^A-Za-z0-9 .,:;()/_'-]")


def _label(text):
    text = SAFE_LABEL.sub(" ", str(text or ""))[:80].strip()
    if "://" in text or "www." in text.lower() or re.search(r"\d{7,}", text):
        return None
    return text or None


def _ident(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) else None


def _detail(row):
    detail = row.get("detail")
    try:
        detail = json.loads(detail) if isinstance(detail, str) and detail else (detail or {})
    except ValueError:
        return {}
    return detail if isinstance(detail, dict) else {}


def _lab_row(row):
    """A Testing Lab row mirrored into the live database (Capstone telemetry.emit): summary "EXPERIMENT ...", and
    workflow_id replaced by the run id "RUN-..."."""
    return (str(row.get("summary") or "").startswith(LAB_SUMMARY)
            or str(row.get("workflow_id") or "").startswith("RUN-"))


def _sub_role(agent_id):
    parent, _, sub = (agent_id or "").partition(".")
    return (parent, sub) if parent and sub in SUB_ROLES else (None, None)


def _own_scope(agent_id):
    """Figures outside the workflow under test: Testing Lab participants and the figures Arena derives."""
    return agent_id in LAB_PARTICIPANTS or agent_id in DERIVED or _sub_role(agent_id)[0] is not None


# Arena 2.5.0: every known Capstone agent stands in its home room from the start, idle until called on (the
# operator: "they should all be there"). Before, a figure appeared only after its first piece of work.
DECLARED = (*CAPSTONE_WORKFLOW, *LAB_PARTICIPANTS, CHAT_AGENT)


class Translator:
    def __init__(self, registry=None, state=None, declare_all=False):
        self.registry = registry or {}          # agent_id -> {display_name, role}
        state = state or {}
        self.agents = dict(state.get("agents", {}))            # agent_id -> role
        self.assignments = dict(state.get("assignments", {}))  # key -> {location, kind, terminal, agent}
        self.skipped = dict(state.get("skipped", {}))
        self.open_tools = dict(state.get("open_tools", {}))  # "agent@head:..." -> key of a call without correlation
        self.reviews = dict(state.get("reviews", {}))          # workflow -> key of its open review request (2.5.1)
        self.declare_all = declare_all                         # the live adapter (runner) turns this on

    def state(self):
        return {"agents": self.agents, "assignments": self.assignments, "skipped": self.skipped,
                "open_tools": self.open_tools, "reviews": self.reviews}

    def _declare(self, row, out):
        """Registers every declared Capstone agent not registered yet (once; later rows find them all known)."""
        for agent_id in DECLARED:
            self._ensure_agent(row, agent_id, out)

    # Finished work is remembered only so late rows for it are recognised; before Arena 2.3.6 nothing was ever
    # dropped, and the adapter deep-copied and rewrote its whole history after every batch.
    KEEP_FINISHED, KEEP_TOTAL, KEEP_OPEN_TOOLS = 500, 5000, 200

    def prune(self):
        """Keep the newest KEEP_FINISHED finished tasks, at most KEEP_TOTAL tasks and KEEP_OPEN_TOOLS unpaired calls."""
        if len(self.assignments) > 2 * self.KEEP_FINISHED:
            finished = [k for k, a in self.assignments.items() if a["terminal"]]
            for k in finished[:max(0, len(finished) - self.KEEP_FINISHED)]:
                del self.assignments[k]
        if len(self.assignments) > self.KEEP_TOTAL:          # work whose end never arrived (Capstone stopped mid-task)
            for k in list(self.assignments)[:len(self.assignments) - self.KEEP_TOTAL]:
                del self.assignments[k]
        for wf in [w for w, k in self.reviews.items() if k not in self.assignments or self.assignments[k]["terminal"]]:
            del self.reviews[wf]
        if len(self.open_tools) > self.KEEP_OPEN_TOOLS:
            for k in list(self.open_tools)[:len(self.open_tools) - self.KEEP_OPEN_TOOLS]:
                del self.open_tools[k]

    # --------------------------------------------------------------- helpers
    def _base(self, row, suffix, type_, **fields):
        event = {"type": type_, "event_id": f"capstone-{row['event_id']}:{suffix}", "occurred_at": row["timestamp"],
                 "orchestrator_id": ORCHESTRATOR, "source_event_type": row["event_type"],
                 "source_ref": f"arena_events:{row['sequence']}"}
        event.update({k: v for k, v in fields.items() if v is not None})
        return event

    def _action(self, agent_id):
        sub = _sub_role(agent_id)[1]
        if sub and self.agents.get(agent_id) == "sub-role":
            return SUB_ROLE_ACTIONS.get(sub)
        return ROLE_ACTIONS.get(self.agents.get(agent_id) or "")

    def _identity(self, agent_id):
        """(name, role) of a declared, derived or registered agent; None when nothing is known of it."""
        known = LAB_PARTICIPANTS.get(agent_id) or DERIVED.get(agent_id) or self.registry.get(agent_id)
        if known:
            return known.get("display_name") or agent_id, known.get("role") or "unspecified"
        parent, sub = _sub_role(agent_id)
        if parent:
            name = (self._identity(parent) or (parent.replace("_", " ").title(),))[0]
            return f"{name}{SUB_ROLE_SEPARATOR}{sub.replace('_', ' ').title()}", "sub-role"
        return None

    def _workflow_agent(self, agent_id):
        return agent_id not in LAB_PARTICIPANTS and _sub_role(agent_id)[0] is None and (
            agent_id in CAPSTONE_WORKFLOW or agent_id in self.registry)

    def _ensure_agent(self, row, agent_id, out, *, route_role=False):
        if agent_id in self.agents:
            return
        known = self._identity(agent_id)
        if known:
            name, role = known
        elif route_role:
            name, role = agent_id.replace("_", " ").title() + " (route role)", "route-role"
        else:
            name, role = agent_id.replace("_", " ").title(), "unspecified"
        self.agents[agent_id] = role
        display = (_label(f"{APP_DISPLAY} {name}") or f"{APP_DISPLAY} agent")[:60]
        out.append(self._base(row, "reg-" + agent_id, "agent.registered", agent_id=agent_id,
                              display_name=display, role=_ident(role) or "unspecified"))

    def _ensure_assignment(self, row, key, agent_id, kind, out, state="queued", note=None):
        current = self.assignments.get(key)
        if current:
            return current
        self._ensure_agent(row, agent_id, out, route_role=kind == "inference")
        workflow = _ident(row.get("workflow_id"))
        if workflow and _own_scope(agent_id):
            workflow = _ident(f"{workflow}/{agent_id}")
        out.append(self._base(row, "create", "assignment.created", assignment_id=key, agent_id=agent_id, kind=kind,
                              state=state, workflow_id=workflow, task_id=_ident(row.get("task_id")),
                              visual_action=self._action(agent_id), label=_label(row.get("summary")),
                              source_ref=f"arena_events:{row['sequence']}" + (f"/{note}" if note else "")))
        current = self.assignments[key] = {"location": HOME, "kind": kind, "terminal": False, "agent": agent_id}
        return current

    def _state(self, row, key, state, out, suffix="state", **fields):
        current = self.assignments[key]
        if current["terminal"]:
            return
        out.append(self._base(row, suffix, "assignment.state", assignment_id=key, state=state, **fields))
        if state in ("completed", "failed", "canceled"):
            current["terminal"] = True

    def _move(self, row, key, location, out, suffix="move", **fields):
        current = self.assignments[key]
        if current["terminal"] or current["location"] == location:
            return False
        out.append(self._base(row, suffix, "assignment.moved", assignment_id=key, from_location=current["location"],
                              to_location=location, **fields))
        current["location"] = location
        return True

    def _interrupted(self, row, out):
        """Capstone rc.25 (A1): after a CONFIRMED worker exit, each work item the trial still owned gets one
        inference.interrupted / task.interrupted row (detail.reconciliation: exit kind, reason, request ids). Its figure
        comes home and its assignment ends "canceled" (neither pass nor fail), labelled with the exit kind. Before 2.5.1
        an unknown inference row fell through to "completed" and would have counted as a pass."""
        kind = row["event_type"]
        key = self._inference_key(row)[0] if kind == "inference.interrupted" else _ident(row.get("task_id"))
        if not key or key not in self.assignments or self.assignments[key]["terminal"]:
            self._skip(kind + ":unknown_or_finished")
            return out
        rec = _detail(row).get("reconciliation") or {}
        label = _label("Interrupted " + str(rec.get("exit_kind") or "worker stopped").replace("_", " "))
        # Ended where it is (at the head, if it was there): the head's room sees its visitor leave as canceled and the
        # admin hub's head traffic board shows the trip "canceled". Walking it home with error_handling first would
        # have shown that trip as "failed", which an interruption is not.
        self._state(row, key, "canceled", out, suffix="interrupted", label=label)
        return out

    def _close_review(self, row, workflow, out, why):
        """Arena 2.5.1: a workflow's open review request ends ("canceled", so it never counts as a pass) when the
        workflow asks again, resumes work or finishes. Capstone sends no row when a review is handled, and on the hub
        881 review figures waited out their 24 h lease."""
        key = self.reviews.pop(workflow, None)
        if key and key in self.assignments and not self.assignments[key]["terminal"]:
            self._state(row, key, "canceled", out, suffix="review-" + why, label=_label(why.replace("_", " ")))

    def _skip(self, kind):
        self.skipped[kind] = self.skipped.get(kind, 0) + 1

    # ------------------------------------------------------------- translate
    def translate(self, row):
        """Return arena.v2 events for one Capstone row (possibly none)."""
        kind, agent, out = row["event_type"], _ident(row.get("agent_id")), []
        if self.declare_all and any(a not in self.agents for a in DECLARED):
            self._declare(row, out)
        if kind in ("agent.registered", "agent.reinstated"):
            if agent in LAB_PARTICIPANTS and agent in self.agents:
                return out              # declared identity: every Testing Lab run registers it again, unchanged
            if agent:
                detail = row.get("detail")
                detail = json.loads(detail) if isinstance(detail, str) and detail else (detail or {})
                known = self.registry.setdefault(agent, {})
                known.setdefault("role", detail.get("role"))
                summary = str(row.get("summary") or "")
                known.setdefault("display_name", re.sub(r" (registered|reinstated)$", "", summary) or None)
                self.agents.pop(agent, None)
                self._ensure_agent(row, agent, out)
            return out
        if kind == "agent.retired":
            if agent in self.agents:
                out.append(self._base(row, "retire", "agent.retired", agent_id=agent))
            return out
        if kind in ("handoff.sent", "handoff.received", "correction.requested"):
            sender, receiver = _ident(row.get("sender_agent_id")), _ident(row.get("receiver_agent_id"))
            correlation = _ident(row.get("correlation_id")) or _ident(row.get("task_id"))
            if not (sender and receiver and correlation):
                self._skip(kind)
                return out
            self._ensure_agent(row, sender, out)
            self._ensure_agent(row, receiver, out)
            type_ = "handoff.received" if kind == "handoff.received" else "handoff.sent"
            out.append(self._base(row, "handoff", type_, sender_agent_id=sender, receiver_agent_id=receiver,
                                  correlation_id=correlation, assignment_id=_ident(row.get("task_id"))))
            return out
        if kind in HEARTBEATS:
            # Long work stays "running" rather than lapsing to "status unknown" (gap C5).
            key = _ident(row.get("task_id")) if kind == "task.heartbeat" else self._inference_key(row)[0]
            if not key or key not in self.assignments or self.assignments[key]["terminal"]:
                self._skip(kind + ":unknown_work")
                return out
            detail = row.get("detail")
            detail = json.loads(detail) if isinstance(detail, str) and detail else (detail or {})
            lease = detail.get("lease_seconds")
            lease = lease if isinstance(lease, int) and not isinstance(lease, bool) and 30 <= lease <= 3600 else HEARTBEAT_LEASE
            out.append(self._base(row, "hb", "assignment.heartbeat", assignment_id=key, lease_seconds=lease))
            return out
        if kind in INTERRUPTED:
            return self._interrupted(row, out)
        if kind == "inference.late_result":
            # Capstone rc.25: a stopped worker's call that the Gateway finished later. A separate fact Capstone keeps
            # (linked to the interruption); the room never replays it, and it is never a pass or fail.
            self._skip(kind + ":kept_by_capstone")
            return out
        if kind.startswith("inference.") and kind not in IGNORED:
            figure, own = self._figure(row)
            if figure:
                return self._inference(row, figure, own, out)
        workflow = _ident(row.get("workflow_id"))
        same_review = kind == "human_review.requested" and self.reviews.get(workflow) == (_ident(row.get("task_id")) or f"REVIEW-{row['event_id']}")
        if workflow in self.reviews and not same_review and (kind == "task.started" or kind in TERMINAL or kind == "human_review.requested"):
            self._close_review(row, workflow, out, "review_superseded" if kind == "human_review.requested" else
                               "workflow_resumed" if kind == "task.started" else "workflow_ended")
        if kind in IGNORED or not agent:
            self._skip(kind)
            return out
        if kind.startswith("mcp.call.") or kind.startswith("tool."):
            return self._tool(row, agent, out)
        task = _ident(row.get("task_id"))
        if kind == "human_review.requested":
            key = task or f"REVIEW-{row['event_id']}"
            self._ensure_assignment(row, key, agent, "orchestration", out)
            self._state(row, key, "waiting", out, wait_reason="human", visual_action="waiting_human",
                        lease_seconds=LAB_REVIEW_LEASE if _lab_row(row) else HUMAN_LEASE, label=_label(row.get("summary")))
            if workflow:
                self.reviews[workflow] = key
            return out
        if kind == "human_review.resolved":
            # Capstone rc.25: the review request's own closing row (same task_id), outcome approved, rejected, mixed,
            # superseded or case_closed. It ends "canceled" with the outcome as its label: a review is not a task
            # result, so it is never counted as a pass or a fail.
            key = task if task in self.assignments else None
            if not key or self.assignments[key]["terminal"]:
                self._skip(kind + ":unknown_or_finished")
                return out
            outcome = str(_detail(row).get("outcome") or row.get("task_status") or "resolved").replace("_", " ")
            self._state(row, key, "canceled", out, suffix="review-resolved", label=_label("Review " + outcome))
            if self.reviews.get(workflow) == key:
                del self.reviews[workflow]
            return out
        if not task:
            self._skip(kind)
            return out
        if kind in QUEUED:
            if task not in self.assignments:
                self._ensure_assignment(row, task, agent, "orchestration", out)
            else:
                self._state(row, task, "queued", out)
        elif kind == "task.started":
            current = self._ensure_assignment(row, task, agent, "orchestration", out, state="executing",
                                              note="E3:execution_node_ignored")
            if current["location"] != HOME:
                self._move(row, task, HOME, out, state="executing")
            elif not out or out[-1]["type"] != "assignment.created":
                self._state(row, task, "executing", out, visual_action=self._action(agent))
        elif kind in WAITING:
            self._ensure_assignment(row, task, agent, "orchestration", out)
            reason = WAITING[kind]
            self._state(row, task, "waiting", out, wait_reason=reason,
                        visual_action="waiting_human" if reason == "human" else None)
        elif kind in TERMINAL:
            if task in self.assignments:
                current = self.assignments[task]
                if current["location"] != HOME:
                    self._move(row, task, HOME, out, suffix="return")
                self._state(row, task, TERMINAL[kind], out,
                            visual_action="completion" if TERMINAL[kind] == "completed" else "error_handling")
            else:
                self._skip(kind + ":unknown_task")
        elif kind in ("validation.failed", "correction.completed"):
            if task in self.assignments:
                self._state(row, task, "executing", out,
                            visual_action="error_handling" if kind == "validation.failed" else "review",
                            label=_label(row.get("summary")))
        else:
            self._skip(kind)
        return out

    def _figure(self, row):
        """(agent an inference row is drawn on, whether the call is that figure's own assignment). The agent is None
        for a row with neither an agent nor a route_role (skipped, never guessed)."""
        agent, route_role = _ident(row.get("agent_id")), _ident(_detail(row).get("route_role"))
        if not agent:
            if not route_role:
                return None, False
            return (LAB_SCREENING if _lab_row(row) else CHAT_AGENT), True
        if agent in LAB_PARTICIPANTS:
            return agent, True
        if route_role in SUB_ROLES and self._workflow_agent(agent):
            return _ident(f"{agent}.{route_role}") or agent, True
        return agent, False

    def _inference_key(self, row):
        workflow, task, call = _ident(row.get("workflow_id")), _ident(row.get("task_id")), _ident(row.get("correlation_id"))
        if self._figure(row)[1]:
            return call, None           # its own call, never a workflow agent's task
        if task and task in self.assignments:
            return task, None
        if workflow and workflow.startswith("EXEC-TASK-") and workflow in self.assignments:
            return workflow, "E1:task_from_workflow_id"
        return call, None

    def _inference(self, row, agent, own, out):
        if row["event_type"] not in INFERENCE_KINDS:
            # Arena 2.5.1: an inference row this adapter does not know is skipped and counted, never taken as
            # "completed" (before, any unknown inference.* row ended its call as a pass).
            self._skip(row["event_type"] + ":unknown_inference_type")
            return out
        key, note = self._inference_key(row)
        if not key:
            self._skip(row["event_type"] + ":no_key")
            return out
        nested = key in self.assignments and self.assignments[key]["kind"] == "orchestration"
        node = row.get("inference_node") or ""
        target = "head:" + node if HEAD.match(node) else HOME
        model = _ident(row.get("model_id"))
        kind = row["event_type"]
        if not nested:
            self._ensure_assignment(row, key, agent, "inference", out,
                                    note=None if own or agent in self.registry else "E4:route_role")
        if kind == "inference.queued":
            if not self._move(row, key, target, out, state="waiting", wait_reason="model",
                              visual_action="waiting_model", model_id=model, kind="inference"):
                self._state(row, key, "waiting", out, wait_reason="model", visual_action="waiting_model", model_id=model)
        elif kind == "inference.started":
            self._move(row, key, target, out, state="executing", visual_action="inference", model_id=model)
            self._state(row, key, "executing", out, suffix="run", visual_action="inference", model_id=model,
                        lease_seconds=INFERENCE_LEASE)
        else:
            failed = kind == "inference.failed"
            if nested:
                # The logical task resumes at the orchestrator; its next real event decides what follows.
                if not self._move(row, key, HOME, out, suffix="return", state="executing", kind="orchestration",
                                  visual_action="error_handling" if failed else self._action(agent)):
                    self._state(row, key, "executing", out, visual_action="error_handling" if failed else None)
            else:
                self._state(row, key, "failed" if failed else "completed", out,
                            visual_action="error_handling" if failed else "completion")
        if note:
            for event in out:
                event["source_ref"] += "/" + note
        return out

    def _tool_head(self, row):
        """The head a business-service call runs on (Capstone 6.0.0-rc.4: QMS on head 3, ERP on head 4), from
        tool_node or execution_node; None for work that stays at the orchestrator."""
        for field in ("tool_node", "execution_node"):
            node = row.get(field) or ""
            if HEAD.match(node):
                return "head:" + node
        return None

    def _tool(self, row, agent, out):
        task = _ident(row.get("task_id"))
        kind = row["event_type"]
        head = self._tool_head(row) if kind.startswith("tool.") else None
        started = kind in ("tool.started", "mcp.call.started")
        ended = kind.endswith("failed") or kind.endswith("succeeded") or kind == "tool.completed"
        failed = kind.endswith("failed")
        if task and task in self.assignments and (agent not in LAB_PARTICIPANTS or self.assignments[task].get("agent") == agent):
            if head and started:
                # The task travels to the service's head for the call and comes back when it ends.
                if not self._move(row, task, head, out, state="executing", visual_action="tool_use"):
                    self._state(row, task, "executing", out, visual_action="tool_use", label=_label(row.get("summary")))
                return out
            if head and ended and self.assignments[task]["location"] == head:
                self._move(row, task, HOME, out, suffix="return", state="executing",
                           visual_action="error_handling" if failed else self._action(agent))
                return out
            if kind == "mcp.call.started" or (started and head):
                self._state(row, task, "executing", out, visual_action="tool_use", label=_label(row.get("summary")))
            elif failed:
                self._state(row, task, "executing", out, visual_action="error_handling", label=_label(row.get("summary")))
            else:
                self._state(row, task, "executing", out, visual_action=self._action(agent))
            return out
        key = _ident(row.get("correlation_id"))
        pair = f"{agent}@{head}" if head else None
        if not key and pair and ended:
            key = self.open_tools.pop(pair, None)       # the started row of this call had no correlation either
        key = key or f"TOOL-{row['event_id']}"
        if pair and started:
            self.open_tools[pair] = key
        self._ensure_assignment(row, key, agent, "tool", out, state="executing")
        if head and started:
            self._move(row, key, head, out, state="executing", visual_action="tool_use")
            return out
        if head and self.assignments[key]["location"] == head and ended:
            self._move(row, key, HOME, out, suffix="return", state="executing")
        if ended and not failed:
            self._state(row, key, "completed", out, visual_action="completion")
        elif failed:
            self._state(row, key, "failed", out, visual_action="error_handling")
        else:
            self._state(row, key, "executing", out, visual_action="tool_use")
        return out
