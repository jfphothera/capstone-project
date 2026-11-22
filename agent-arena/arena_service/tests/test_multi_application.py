"""Scenarios E and G (workstation simulation): Ticketing and the synthetic Coder
producer share Head 3 through one independent service while keeping identities,
queue/execution states and ownership separate. A third application joins with
no code change, and one producer's outage leaves the others working."""
import time

from arena_service.clients.synthetic_coder import Script


def test_coder_and_ticketing_share_head_and_return_to_own_orchestrators(env):
    ticketing = env.app("capstone", "Ticketing")
    coder = env.app("coder-synthetic-test", "Coder (SYNTHETIC TEST)", orchestrators=("test",))
    for n in (3, 6):
        env.head(f"head-192-0-2-{n}")
    viewer = env.viewer("head-192-0-2-3")
    ticketing.publish([env.ev("agent.registered", agent_id="independent_validation",
                              display_name="Ticketing Independent Validator", role="validation"),
                       env.ev("assignment.created", assignment_id="EXEC-TASK-1", agent_id="independent_validation",
                              kind="inference", location="head:head-192-0-2-3", state="executing",
                              visual_action="inference")])
    steps = list(Script("head-192-0-2-3", "head-192-0-2-6", run_id="t").steps())
    for step in steps[:4]:                                    # up to: queued on head 3
        assert all(r["status"] == "accepted" for r in coder.publish(step)["results"])
    head3 = viewer.snapshot("location:head:head-192-0-2-3")
    states = sorted((a["assignment_key"], a["state"], a["wait_reason"]) for a in head3["assignments"])
    assert states == [("capstone/EXEC-TASK-1", "executing", None),
                      ("coder-synthetic-test/TEST-t-validate", "waiting", "capacity")]
    names = {a["agent_key"]: a["display_name"] for a in head3["agents"]}
    assert names == {"capstone/independent_validation": "Ticketing Independent Validator",
                     "coder-synthetic-test/python_validator": "Coder Python Validator"}
    for step in steps[4:]:
        coder.publish(step)
    coder_moves = [(e["from_location"], e["to_location"]) for e in viewer.events(0)["events"]
                   if e["app_id"] == "coder-synthetic-test" and e["type"] == "assignment.moved"]
    assert coder_moves == [("orch:coder-synthetic-test/test", "head:head-192-0-2-3"),
                           ("head:head-192-0-2-3", "head:head-192-0-2-6"),
                           ("head:head-192-0-2-6", "orch:coder-synthetic-test/test")]
    # Ownership: each orchestrator view holds only its own application's agents.
    coder_home = viewer.snapshot("location:orch:coder-synthetic-test/test")
    assert {a["app_id"] for a in coder_home["agents"]} == {"coder-synthetic-test"}
    assert {a["app_id"] for a in viewer.snapshot("location:orch:capstone/main")["agents"]} == {"capstone"}
    # Ticketing's work on head 3 was never overwritten by Coder's.
    left = viewer.snapshot("location:head:head-192-0-2-3")["assignments"]
    assert [(a["assignment_key"], a["state"]) for a in left] == [("capstone/EXEC-TASK-1", "executing")]
    labels = [e["payload"].get("label", "") for e in viewer.events(0, view="app:coder-synthetic-test")["events"]
              if e["payload"].get("label")]
    assert labels and all(label.startswith("TEST:") for label in labels)


def test_third_application_and_producer_outage(env):
    ticketing = env.app("capstone", "Ticketing")
    third = env.app("lab-notebook", "Lab Notebook")
    env.head("head-192-0-2-3")
    ticketing.publish([env.ev("orchestrator.heartbeat", heartbeat_seconds=5)])
    third.publish([env.ev("orchestrator.heartbeat", heartbeat_seconds=5),
                   env.ev("agent.registered", agent_id="indexer", display_name="Lab Indexer", role="retrieval")])
    env.store.db.execute("UPDATE orchestrators SET last_seen=? WHERE app_id='capstone'", (time.time() - 60,))
    states = {(o["app_id"]): o["producer_state"] for o in env.store.registry()["orchestrators"]}
    assert states == {"capstone": "offline", "lab-notebook": "online"}
    result = third.publish([env.ev("assignment.created", assignment_id="I1", agent_id="indexer", kind="tool",
                                   state="executing")])
    assert result["results"][0]["status"] == "accepted"


def test_synthetic_tour_visits_every_head_and_finishes(env):
    from arena_service.clients.synthetic_coder import Tour
    heads = [f"head-192-0-2-{n}" for n in (3, 4, 5, 6)]
    for head in heads:
        env.head(head)
    producer = env.app("coder-synthetic-test", "Coder", orchestrators=("test",))
    visited = set()
    for step in Tour(heads, rounds=3).steps():
        assert all(r["status"] == "accepted" for r in producer.publish(step)["results"])
        visited |= {e["to_location"] for e in step if e["type"] == "assignment.moved"}
    assert {"head:" + h for h in heads} <= visited
    assert {a["state"] for a in env.store.snapshot("cluster")["assignments"]} == {"completed"}
