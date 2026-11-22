"""The synthetic load producer (TEST data only) against an isolated service."""
from arena_service.clients.synthetic_coder import Load


def test_load_finishes_at_heads_and_cleans_up(env):
    producer = env.app("coder-synthetic-test", "Coder (SYNTHETIC TEST)", orchestrators=("test",))
    heads = ["head-192-0-2-3", "head-192-0-2-4"]
    for h in heads:
        env.head(h)
    rejected = []
    load = Load(heads, agents=6, minutes=0.05, pace=0.02, fail=0.3, burst=True, seed=7)
    tally = load.run_with(producer, out=lambda *a: rejected.append(a) if a and a[0] == "rejected" else None)
    assert not rejected and tally["completed"] + tally["failed"] + tally["canceled"] >= 6
    snap = env.viewer("head-192-0-2-3").snapshot("cluster")
    assert all(a["state"] in ("completed", "failed", "canceled") for a in snap["assignments"]), "nothing left open"
    counts = snap["result_counts"]["cluster"]["1h"]
    assert counts == {"pass": tally["completed"], "fail": tally["failed"]}
    live = {a["agent_id"] for a in snap["agents"] if not a["retired"]}
    assert not any(a.startswith("load_") for a in live), "extra agents retired"
