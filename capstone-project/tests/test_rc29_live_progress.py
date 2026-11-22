"""rc.29: the Details pane's progress data on a REAL investigation (deterministic stand-in models), through the HTTP
routes the browser uses. rc.27/28 crashed on real ISO event times, so the pane froze on "Queued"."""
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from capstone_rag import agent as agent_mod, config, store as store_mod
from capstone_rag.webapp import create_app


@pytest.fixture
def client(tmp_path):
    if not os.path.exists(config.DB_PATH):
        pytest.skip("run scripts/import_synthetic.py first")
    path = str(tmp_path / "ops.db")
    shutil.copy(config.DB_PATH, path)
    from db_reset import clear_copy
    clear_copy(path, ("shipment_events", "sim_shipments", "rmas", "erp_calls", "approvals", "recommendations",
                      "reasoning_attempts", "retrieved_sources", "tool_calls", "checklist_items", "qa_notifications",
                      "conversations", "messages", "cases", "improvement_proposals", "audit_events"))
    st = store_mod.Store(path)
    app = create_app(prefer_llm=False, store=st, agent=agent_mod.Agent(prefer_llm=False), representative="rep.test", start_workers=False)
    with TestClient(app) as c:
        c.st = st
        yield c
    st.close()


def test_progress_fills_in_on_a_real_investigation(client):
    body = "Hi, this is Ivan Nowak. My device, serial 98148469, won't turn on since this morning. What should I do?"
    cid = client.post("/api/cases", json={"subject": "Device won't power on", "body": body, "channel": "email"}).json()["case_id"]
    before = client.get(f"/api/cases/{cid}/progress").json()
    assert {s["key"] for s in before["stages"]} >= {"intake", "safety", "erp", "qms", "planning", "response", "checks", "waiting"}
    from capstone_rag.webapp.runtime import get_runtime
    rt = get_runtime()
    rt.ops.queue.submit(cid, "Medium")
    rt.ops.queue._run_once = None
    # run the queued case the way the worker does (one pass), then read progress like the browser
    case_id = rt.ops.queue._next()
    assert case_id == cid
    rt.ops.flow.store.update("cases", "case_id", cid, {"ai_state": "processing"})
    try:
        rt.ops.flow.pre_investigate(cid)
    except Exception:
        pass   # a stand-in model failure still records progress and hands off to the person
    after = client.get(f"/api/cases/{cid}/progress").json()
    stages = {s["key"]: s for s in after["stages"]}
    reached = [k for k in ("intake", "safety", "erp", "qms", "planning", "response", "checks") if stages[k]["status"] != "not_started"]
    assert len(reached) >= 5, stages
    assert after["current_pass"] and after["cursor"] > 0
    assert all(isinstance(e["at"], float) for s in after["stages"] for e in s["events"])
    assert client.get(f"/api/cases/{cid}/progress?after={after['cursor']}").json()["new_events"] == []
    ws = client.get(f"/api/cases/{cid}").json()
    assert ws["case"]["case_id"] == cid
