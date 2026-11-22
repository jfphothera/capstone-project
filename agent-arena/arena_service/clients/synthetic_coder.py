"""Synthetic Coder-compatible producer — TEST DATA ONLY, not Coder workload.

    python -E -s -m arena_service.clients.synthetic_coder --url http://192.0.2.1:8790 \
        --credential-file coder-test.dpapi --head head-192-0-2-3 --second-head head-192-0-2-6

Authenticates with its own producer credential for the app `coder-synthetic-test`
(register it with arena-admin first). Every label starts with "TEST:" and the
application display name contains "SYNTHETIC TEST", so the Arena never presents
this as real Coder activity. It imports neither Capstone nor the Coder project.
"""
from __future__ import annotations

import argparse
import time
import uuid

from .. import secretfile
from ..client import Client, RateLimited, iso

APP_DISPLAY = "Coder (SYNTHETIC TEST)"
ORCHESTRATOR = "test"
AGENTS = (("python_validator", "Coder Python Validator", "validation"),
          ("architect", "Coder Architect", "design"),
          ("code_writer", "Coder Code Writer", "code-generation"),
          ("security_reviewer", "Coder Security Reviewer", "review"))


class Script:
    """Builds the scripted event sequence; each call to `next_events` yields one step."""

    def __init__(self, head, second_head, run_id=None):
        self.run = run_id or uuid.uuid4().hex[:8]
        self.head, self.second = "head:" + head, "head:" + second_head
        self.home = f"orch:coder-synthetic-test/{ORCHESTRATOR}"
        self.n = 0

    def ev(self, type_, **fields):
        self.n += 1
        return dict(type=type_, event_id=f"syn-{self.run}-{self.n}", occurred_at=iso(), orchestrator_id=ORCHESTRATOR,
                    **fields)

    def steps(self):
        a = f"TEST-{self.run}-validate"
        yield [self.ev("application.updated", display_name=APP_DISPLAY),
               self.ev("orchestrator.heartbeat", display_name="Coder test orchestrator", heartbeat_seconds=30)]
        yield [self.ev("agent.registered", agent_id=agent, display_name=name, role=role) for agent, name, role in AGENTS]
        yield [self.ev("assignment.created", assignment_id=a, agent_id="python_validator", kind="orchestration",
                       state="executing", workflow_id=f"TEST-WF-{self.run}", task_id=a, visual_action="validation",
                       label="TEST: plan validation at orchestrator")]
        yield [self.ev("assignment.moved", assignment_id=a, from_location=self.home, to_location=self.head,
                       state="waiting", wait_reason="capacity", visual_action="waiting_model", kind="inference",
                       label="TEST: dispatched, queued for head capacity")]
        yield [self.ev("assignment.state", assignment_id=a, state="executing", visual_action="inference",
                       lease_seconds=120, model_id="test-model", label="TEST: inference executing")]
        yield [self.ev("assignment.state", assignment_id=a, state="executing", visual_action="testing",
                       label="TEST: operation complete, next step assigned")]
        yield [self.ev("assignment.moved", assignment_id=a, from_location=self.head, to_location=self.second,
                       state="executing", visual_action="inference", lease_seconds=120,
                       label="TEST: moved to second head")]
        yield [self.ev("assignment.moved", assignment_id=a, from_location=self.second, to_location=self.home,
                       state="executing", kind="orchestration", visual_action="review",
                       label="TEST: returned to orchestrator")]
        yield [self.ev("assignment.state", assignment_id=a, state="completed", visual_action="completion",
                       label="TEST: complete")]


TOUR_ACTIONS = ("inference", "code_generation", "testing", "review", "retrieval", "tool_use", "validation", "evaluation")


class Tour(Script):
    """Every test agent visits every head in turn, one move per step, so each head display
    shows arrivals, work and departures. Agents start on different heads (concurrent work)."""

    def __init__(self, heads, rounds=3, run_id=None):
        super().__init__(heads[0], heads[-1], run_id)
        self.heads, self.rounds = ["head:" + h for h in heads], rounds

    def steps(self):
        agents = [a for a, _, _ in AGENTS]
        task = {a: f"TEST-{self.run}-{a}" for a in agents}
        where = {}
        yield [self.ev("application.updated", display_name=APP_DISPLAY),
               self.ev("orchestrator.heartbeat", display_name="Coder test orchestrator", heartbeat_seconds=30)]
        yield [self.ev("agent.registered", agent_id=agent, display_name=name, role=role) for agent, name, role in AGENTS]
        yield [self.ev("assignment.created", assignment_id=task[a], agent_id=a, kind="orchestration", state="executing",
                       workflow_id=f"TEST-WF-{self.run}", task_id=task[a], visual_action="evaluation",
                       label="TEST: planning at orchestrator") for a in agents]
        for i, a in enumerate(agents):                     # dispatch one by one: a queue, then work
            where[a] = self.heads[i % len(self.heads)]
            yield [self.ev("assignment.moved", assignment_id=task[a], from_location=self.home, to_location=where[a],
                           state="waiting", wait_reason="model", visual_action="waiting_model", kind="inference",
                           lease_seconds=120, label="TEST: dispatched, waiting for model")]
            yield [self.ev("assignment.state", assignment_id=task[a], state="executing", visual_action="inference",
                           model_id="test-model", lease_seconds=120, label="TEST: inference executing")]
        step = 0
        for r in range(1, self.rounds + 1):
            for i, a in enumerate(agents):
                step += 1
                nxt = self.heads[(i + r) % len(self.heads)]
                action = TOUR_ACTIONS[step % len(TOUR_ACTIONS)]
                yield [self.ev("assignment.moved", assignment_id=task[a], from_location=where[a], to_location=nxt,
                               state="executing", visual_action=action, kind="inference", lease_seconds=120,
                               label=f"TEST: round {r}, {action.replace('_', ' ')}"),
                       self.ev("orchestrator.heartbeat")]
                where[a] = nxt
        for a in agents:                                   # everyone comes home and finishes
            yield [self.ev("assignment.moved", assignment_id=task[a], from_location=where[a], to_location=self.home,
                           state="executing", kind="orchestration", visual_action="review",
                           label="TEST: returned to orchestrator")]
        yield [self.ev("assignment.state", assignment_id=task[a], state="completed", visual_action="completion",
                       label="TEST: complete") for a in agents]


class Load:
    """A steady stream of short tasks to random heads, finishing AT the head (so head rooms
    show crates, debris and bulbs): mostly completed, some failed, a few canceled. `agents`
    test agents each run one task at a time; `pace` scales every delay (0.5 = twice as fast).
    `burst` sends every agent's next task to the first head halfway through, to overfill one
    room. On stop (or Ctrl-C) open tasks are canceled and the extra agents retired."""

    def __init__(self, heads, agents=8, minutes=5.0, pace=1.0, fail=0.15, cancel=0.05, burst=False, seed=None, run_id=None):
        import random
        self.random = random.Random(seed)
        self.run = run_id or uuid.uuid4().hex[:8]
        self.heads, self.home, self.n = ["head:" + h for h in heads], f"orch:coder-synthetic-test/{ORCHESTRATOR}", 0
        roles = ("validation", "design", "code-generation", "review", "retrieval", "testing")
        self.agents = [a for a, _, _ in AGENTS][:agents] + [f"load_{i:02d}" for i in range(len(AGENTS) + 1, agents + 1)]
        self.names = {a: n for a, n, _ in AGENTS} | {a: f"Coder Test Agent {a[5:]}" for a in self.agents if a.startswith("load_")}
        self.roles = {a: r for a, _, r in AGENTS} | {a: roles[i % len(roles)] for i, a in enumerate(self.agents) if a.startswith("load_")}
        self.minutes, self.pace, self.fail, self.cancel, self.burst = minutes, pace, fail, cancel, burst
        self.open, self.tally = {}, {"completed": 0, "failed": 0, "canceled": 0}

    ev = Script.ev

    def _later(self, low, high):
        return self.random.uniform(low, high) * self.pace

    def _step(self, agent, step, now, end, half, burst_left, seq):
        """One agent's next step: (events, next step or None, delay before it)."""
        task = self.open.get(agent)
        if step == "dispatch":
            if now >= end:
                return [], None, 0                         # this agent is done
            if agent in burst_left and now >= half:
                head = self.heads[0]
                burst_left.discard(agent)
            else:
                head = self.random.choice(self.heads)
            task = self.open[agent] = f"TEST-{self.run}-{agent}-{seq}"
            return [self.ev("assignment.created", assignment_id=task, agent_id=agent, kind="orchestration", state="executing",
                            workflow_id=f"TEST-LOAD-{self.run}", task_id=task, visual_action="evaluation", label="TEST: planning"),
                    self.ev("assignment.moved", assignment_id=task, from_location=self.home, to_location=head, state="waiting",
                            wait_reason="model", visual_action="waiting_model", kind="inference", lease_seconds=120,
                            label="TEST: dispatched, waiting for model")], "work", self._later(2, 8)
        if step == "work":
            return [self.ev("assignment.state", assignment_id=task, state="executing", lease_seconds=120, model_id="test-model",
                            visual_action=self.random.choice(TOUR_ACTIONS), label="TEST: working")], "finish", self._later(6, 20)
        roll = self.random.random()
        outcome = "failed" if roll < self.fail else "canceled" if roll < self.fail + self.cancel else "completed"
        self.open.pop(agent, None)
        self.tally[outcome] += 1
        return [self.ev("assignment.state", assignment_id=task, state=outcome, label=f"TEST: {outcome}")], "dispatch", self._later(2, 10)

    def run_with(self, client, out=print):
        import heapq
        start = time.time()
        end, half, burst_left = start + self.minutes * 60, start + self.minutes * 30, set(self.agents if self.burst else ())
        def send(events):
            for attempt in range(6):
                try:
                    results = client.publish(events)["results"]
                    break
                except RateLimited as exc:                 # the service's per-address limit: wait and resend
                    out(f"rate limited, waiting {exc.retry_after:g} s")
                    self.tally["rate_limited"] = self.tally.get("rate_limited", 0) + 1
                    time.sleep(exc.retry_after)
            else:
                raise RuntimeError("still rate limited after 6 attempts")
            for r in results:
                if r["status"] != "accepted":
                    out(r["status"], r.get("reason", ""), r["event_id"])
        send([self.ev("application.updated", display_name=APP_DISPLAY),
              self.ev("orchestrator.heartbeat", display_name="Coder test orchestrator", heartbeat_seconds=30)])
        send([self.ev("agent.registered", agent_id=a, display_name=self.names[a], role=self.roles[a]) for a in self.agents])
        queue = [(start + self._later(0, 6), i, a, "dispatch") for i, a in enumerate(self.agents)]
        heapq.heapify(queue)
        beat, tick, seq = start, start, len(queue)
        try:
            while queue:
                time.sleep(max(0.0, queue[0][0] - time.time()))
                now, batch, due = time.time(), [], []
                while queue and queue[0][0] <= now + 0.25:
                    due.append(heapq.heappop(queue))
                if now - beat > 10:
                    batch.append(self.ev("orchestrator.heartbeat"))
                    beat = now
                later = []
                for _, _, agent, step in due:
                    step_events, nxt, delay = self._step(agent, step, now, end, half, burst_left, seq)
                    batch += step_events
                    seq += 1
                    if nxt:
                        later.append((nxt, delay, agent))
                if batch:
                    send(batch)
                for nxt, delay, agent in later:
                    seq += 1
                    heapq.heappush(queue, (time.time() + delay, seq, agent, nxt))
                if time.time() - tick > 30:
                    tick = time.time()
                    out(f"{round((tick - start) / 60, 1)} min: {len(self.open)} open, {self.tally}")
        finally:
            leftover = [self.ev("assignment.state", assignment_id=t, state="canceled", label="TEST: load test stopped") for t in self.open.values()]
            extra = [self.ev("agent.retired", agent_id=a) for a in self.agents if a.startswith("load_")]
            if leftover or extra:
                send(leftover + extra)
            out(f"done: {self.tally}; {len(leftover)} open task(s) canceled, {len(extra)} extra agent(s) retired")
        return self.tally


def main():
    parser = argparse.ArgumentParser(prog="synthetic-coder-producer", description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--head", help="first head (two-head script)")
    parser.add_argument("--second-head", help="second head (two-head script)")
    parser.add_argument("--heads", help="comma list of heads for the tour: every agent visits every head")
    parser.add_argument("--rounds", type=int, default=3, help="tour rounds")
    parser.add_argument("--step-seconds", type=float, default=4.0)
    parser.add_argument("--load", action="store_true", help="steady stream of short tasks to --heads, finishing at the head")
    parser.add_argument("--agents", type=int, default=8, help="load: test agents, one task each at a time")
    parser.add_argument("--minutes", type=float, default=5.0, help="load: how long to keep dispatching")
    parser.add_argument("--pace", type=float, default=1.0, help="load: delay scale (0.5 = twice as fast)")
    parser.add_argument("--fail", type=float, default=0.15, help="load: share of tasks that fail")
    parser.add_argument("--burst", action="store_true", help="load: halfway, send every agent's next task to the first head")
    args = parser.parse_args()
    if args.load:
        if not args.heads:
            parser.error("--load needs --heads")
        credential_id, secret = secretfile.read(args.credential_file)
        Load([h.strip() for h in args.heads.split(",") if h.strip()], args.agents, args.minutes, args.pace, args.fail,
             burst=args.burst).run_with(Client(args.url, credential_id, secret), out=lambda *a: print(*a, flush=True))
        return
    if args.heads:
        script = Tour([h.strip() for h in args.heads.split(",") if h.strip()], args.rounds)
    elif args.head and args.second_head:
        script = Script(args.head, args.second_head)
    else:
        parser.error("give --heads, or --head and --second-head")
    credential_id, secret = secretfile.read(args.credential_file)
    client = Client(args.url, credential_id, secret)
    for step in script.steps():
        for result in client.publish(step)["results"]:
            print(result["status"], result.get("reason", ""), result["event_id"])
        time.sleep(args.step_seconds)


if __name__ == "__main__":
    main()
