"""Streamlit interface for the capstone agent.

    streamlit run app.py

Functional, not styled. Five tabs:
  Agent          - hold a multi-turn conversation and watch the reasoning loop
  Review queue   - the simulated human escalation queue
  Approvals      - return authorisations awaiting a human decision
  Memory review  - proposed resolutions awaiting review
  Audit          - the joined record of everything above

Everything shown comes from the same Agent the command-line runners use, so
the interface and the evidence cannot drift apart.
"""

from __future__ import annotations

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    agent as agent_mod, audit as audit_mod, cases, config, generation,
)

st.set_page_config(page_title="Capstone - Grounded Support Agent",
                   layout="wide")


@st.cache_resource(show_spinner="Loading embedding model and vector index...")
def get_agent(prefer_llm: bool):
    return agent_mod.Agent(prefer_llm=prefer_llm)


@st.cache_data
def get_scenarios():
    with open(os.path.join(config.ERP_DIR, "scenarios.json"),
              encoding="utf-8") as fh:
        return {s["scenario_id"]: s for s in json.load(fh)}


def sidebar():
    with st.sidebar:
        st.header("Configuration")
        llm_up = generation.llm_available()
        if llm_up:
            st.success(f"Local LLM reachable: {config.OLLAMA_MODEL}")
        else:
            st.warning(
                f"Ollama/{config.OLLAMA_MODEL} not reachable. Replies come "
                "from the deterministic writer, which is not a model."
            )
        prefer_llm = st.checkbox("Use the local LLM for generation",
                                 value=llm_up, disabled=not llm_up)
        st.markdown("---")
        st.caption(f"**Embedding model**\n\n`{config.EMBEDDING_MODEL}`")
        st.caption(f"**Retrieval**  search_k={config.SEARCH_K}, "
                   f"accept_k={config.ACCEPT_K}, "
                   f"min_sim={config.MIN_SIMILARITY}")
        st.caption(f"**Escalation budget**  "
                   f"{config.MAX_UNPRODUCTIVE_ATTEMPTS} unproductive attempts")
        st.markdown("---")
        if st.button("Start a new conversation"):
            st.session_state.pop("conv", None)
            st.session_state["history"] = []
            st.rerun()
        return prefer_llm


def render_result(ag, result):
    ident = result.identity.as_dict()

    st.subheader("Device identification")
    if ident["confirmed"]:
        c = st.columns(4)
        c[0].metric("Customer", ident["customer_name"])
        c[1].metric("Model", ident["model"])
        c[2].metric("Revision", ident["revision"])
        c[3].metric("Serial", ident["serial_number"])
        st.caption(ident["reason"])
    else:
        st.error(f"Not confirmed - {ident['reason']}")
        if ident["candidate_serials"]:
            st.caption("Candidates on this account: "
                       + ", ".join(ident["candidate_serials"]))

    st.subheader("Tools the agent chose")
    st.caption("Intent: "
               + (", ".join(k for k, v in result.intent.items() if v) or "-"))
    if result.tool_calls:
        st.dataframe(
            [{"tool": t["tool"], "productive": t["productive"],
              "result": t["summary"]} for t in result.tool_calls],
            width="stretch", hide_index=True)

    st.subheader("Retrieved source passages")
    if result.retrieval_query:
        st.caption(f"Filters: model={result.filters.get('model')}, "
                   f"revision={result.filters.get('revision')}, "
                   f"status must be Approved, procedure must be in "
                   f"{result.filters.get('authorised_procedures') or 'NONE'}")
        rows = sorted(result.accepted + result.rejected, key=lambda v: v.rank)
        st.dataframe(
            [{"rank": v.rank if v.rank < 9999 else "-",
              "similarity": (None if v.similarity != v.similarity
                             else round(v.similarity, 4)),
              "picked_by": v.selected_by,
              "status": v.status, "chunk_id": v.chunk_id,
              "procedure": v.procedure_id, "reason": v.reason}
             for v in rows],
            width="stretch", hide_index=True)
        by_id = {c.chunk_id: c for c in ag.index.chunks}
        for v in result.accepted:
            chunk = by_id.get(v.chunk_id)
            if chunk:
                with st.expander(f"USED  {v.chunk_id}"):
                    st.caption(f"{chunk.document_id} v{chunk.document_version}"
                               f" | {chunk.model} Rev "
                               f"{'/'.join(chunk.applicable_revisions)} | "
                               f"{chunk.approval_status} | {chunk.source_path}")
                    st.write(chunk.text)
    else:
        st.info("No QMS retrieval was performed for this interaction.")

    st.subheader("Verification")
    v1, v2 = st.columns(2)
    with v1:
        st.caption("Structured validation (deterministic)")
        if result.verdict.passed:
            st.success(f"PASS - {result.verdict.reason}")
        else:
            st.error(f"FAIL - {result.verdict.reason}")
        st.dataframe(result.verdict.checks, width="stretch", hide_index=True)
    with v2:
        st.caption("Source grounding of the generated text")
        if result.grounding is None:
            st.info("Not applicable - no generated troubleshooting content.")
        elif result.grounding["grounded"]:
            st.success(f"VERIFIED - {result.grounding['reason']}")
        else:
            st.error(f"REJECTED - {result.grounding['reason']}")
        if result.grounding:
            st.dataframe(
                [{"supported": c["supported"],
                  "similarity": c["best_similarity"],
                  "recall": c["best_recall"], "sentence": c["sentence"]}
                 for c in result.grounding["sentence_checks"]],
                width="stretch", hide_index=True)

    if result.reflexion:
        st.subheader("Reflexion corrections")
        for note in result.reflexion:
            st.warning(f"Attempt {note['attempt']} rejected: {note['failure']}")
            st.code(note["feedback"])

    if result.case_id or result.rma_request_id:
        st.subheader("Human oversight")
        if result.case_id:
            st.error(f"Escalated: {result.case_id} [{result.case_priority}] "
                     f"{result.escalation_reason}")
        if result.rma_request_id:
            st.warning(f"Return authorisation {result.rma_request_id} is "
                       f"{result.rma_status} - the agent cannot approve it")

    st.subheader("Customer-facing response")
    st.caption(f"Written by: {result.generation_engine}  |  outcome: "
               f"{result.outcome}  |  source: "
               f"{result.selected_procedure or 'none'}")
    st.text_area("Response", value=result.response, height=280,
                 key=f"resp-{result.ticket_id}")

    with st.expander("Reasoning trace (thought / action / observation)"):
        for s in result.steps:
            flag = "" if s.productive else " :red[unproductive]"
            st.markdown(f"**{s.n}. `{s.action}`**  `[{s.kind}]`{flag}")
            st.caption(f"thought: {s.thought}")
            st.caption(f"observation: {s.observation}")


def tab_agent(ag):
    scenarios = get_scenarios()
    if "conv" not in st.session_state:
        st.session_state["conv"] = ag.new_conversation()
    if "history" not in st.session_state:
        st.session_state["history"] = []
    conv = st.session_state["conv"]

    st.caption(f"Conversation `{conv.conversation_id}` - turn "
               f"{len(conv.turns) + 1}. Confirmed device: "
               f"{conv.confirmed_serial or 'none yet'}")

    labels = {
        sid: f"{sid} - {s['name']} ({s['device_model']} Rev "
             f"{s['device_revision']}, {s['customer_name']})"
        for sid, s in scenarios.items()
    }
    choice = st.selectbox("Load a synthetic scenario", ["(free text)"]
                          + list(scenarios),
                          format_func=lambda s: labels.get(s, s))
    if choice != "(free text)":
        scn = scenarios[choice]
        st.caption(f"Expected: {scn['expected_behaviour']}")
        default_msg, default_acc = scn["customer_message"], scn["account_id"]
    else:
        default_msg, default_acc = "", conv.account_id or "ACC-00010"

    message = st.text_area("Customer message", value=default_msg, height=150)
    account = st.text_input("ERP account id", value=default_acc or "")

    if st.button("Run agent", type="primary") and message.strip():
        if account and not conv.account_id:
            conv.account_id = account
        with st.spinner("safety -> ERP -> tools -> retrieval -> verify ..."):
            result = ag.handle(message, account_id=account,
                               scenario_id=(None if choice == "(free text)"
                                            else choice),
                               conversation=conv)
        st.session_state["history"].insert(0, result)

    if not st.session_state["history"]:
        st.info("Pick a scenario or type a message, then press Run agent. "
                "Follow-up messages continue the same conversation.")
        return

    render_result(ag, st.session_state["history"][0])

    st.subheader("Conversation history")
    st.dataframe(
        [{"ticket": r.ticket_id, "outcome": r.outcome,
          "device": (f"{r.identity.device.model} Rev "
                     f"{r.identity.device.revision}")
          if r.identity.device else "-",
          "procedure": r.selected_procedure or "-",
          "validation": r.verdict.result,
          "grounded": (r.grounding or {}).get("grounded"),
          "case": r.case_id or "-"}
         for r in st.session_state["history"]],
        width="stretch", hide_index=True)


def tab_queue(ag):
    st.subheader("Simulated human review queue")
    q = ag.escalations
    all_cases = q.all_cases()
    if not all_cases:
        st.info("No cases yet. Run a scenario that escalates.")
        return
    st.dataframe(
        [{"case": c.case_id, "priority": c.priority, "reason": c.reason,
          "status": c.status, "assignee": c.assignee or "-",
          "customer": c.customer_name, "device": f"{c.model} Rev {c.revision}",
          "summary": c.summary} for c in all_cases],
        width="stretch", hide_index=True)

    open_ids = [c.case_id for c in all_cases if c.status != cases.RESOLVED]
    if not open_ids:
        st.success("Every case is resolved.")
        return
    cid = st.selectbox("Case", open_ids)
    c1, c2 = st.columns(2)
    with c1:
        who = st.text_input("Assign to", value="eng.taylor")
        if st.button("Assign"):
            q.assign(cid, who)
            st.rerun()
    with c2:
        note = st.text_input("Resolution note", value="Handled by engineering.")
        if st.button("Resolve"):
            q.resolve(cid, note)
            st.rerun()


def tab_approvals(ag):
    st.subheader("Return authorisations awaiting a human decision")
    st.caption("The agent can request a return. It can never approve one.")
    q = ag.approvals
    reqs = q.all_requests()
    if not reqs:
        st.info("No requests yet. Work a conversation until every approved "
                "step is exhausted.")
        return
    st.dataframe(
        [{"request": r.request_id, "status": r.status,
          "authorised": r.is_authorised, "rma": r.rma_number or "-",
          "customer": r.customer_name,
          "device": f"{r.model} Rev {r.revision}",
          "decided_by": r.decided_by or "-",
          "justification": r.justification} for r in reqs],
        width="stretch", hide_index=True)

    pending = [r.request_id for r in reqs if r.status == cases.PENDING_APPROVAL]
    if not pending:
        st.success("No requests are pending.")
        return
    rid = st.selectbox("Request", pending)
    who = st.text_input("Reviewer", value="svc.manager")
    note = st.text_input("Decision note", value="")
    c1, c2 = st.columns(2)
    if c1.button("Approve return", type="primary"):
        q.approve(rid, who, note)
        st.rerun()
    if c2.button("Reject"):
        q.reject(rid, who, note)
        st.rerun()


def tab_memory(ag):
    st.subheader("Reviewed long-term memory")
    st.caption("A resolution is invisible to the agent until a human approves "
               "it, and is re-checked against document control on recall.")
    mem = ag.reviewed_memory
    rows = mem._all()
    if not rows:
        st.info("Nothing proposed yet.")
        return
    st.dataframe(
        [{"memory": m.memory_id, "status": m.review_status,
          "device": f"{m.model} Rev {m.revision}",
          "procedure": m.procedure_id, "symptom": m.symptom[:80],
          "reviewed_by": m.reviewed_by or "-"} for m in rows],
        width="stretch", hide_index=True)

    pend = [m.memory_id for m in rows if m.review_status == "pending_review"]
    if not pend:
        st.success("Nothing is awaiting review.")
        return
    mid = st.selectbox("Proposed resolution", pend)
    who = st.text_input("Reviewer name", value="qa.reviewer")
    c1, c2 = st.columns(2)
    if c1.button("Approve memory", type="primary"):
        mem.approve(mid, who)
        st.rerun()
    if c2.button("Reject memory"):
        mem.reject(mid, who)
        st.rerun()


def tab_audit(ag):
    st.subheader("Audit")
    a = audit_mod.Auditor(escalations=ag.escalations, approvals=ag.approvals,
                          reviewed_memory=ag.reviewed_memory)
    st.json(a.summary())
    tickets = a.tickets()
    if not tickets:
        st.info("No interactions recorded yet.")
        return
    st.dataframe(
        [{"ticket": t.get("ticket_id"), "conversation": t.get("conversation_id"),
          "customer": t.get("customer"), "outcome": t.get("outcome"),
          "procedure": t.get("selected_procedure") or "-",
          "validation": t.get("validation"),
          "grounded": t.get("grounding_verified"),
          "case": t.get("case_id") or "-"} for t in reversed(tickets)],
        width="stretch", hide_index=True)
    tid = st.selectbox("Full dossier for ticket",
                       [t["ticket_id"] for t in reversed(tickets)])
    st.code(a.render_ticket(tid))


def main():
    st.title("Grounded Support Agent - Capstone")
    st.caption(
        "Synthetic data only. ERP, ticketing, shipments and QMS are mocked "
        "local files; retrieval uses a real embedding model; generation uses "
        "a local LLM when one is reachable, and every generated reply is "
        "verified against its source by code afterwards."
    )
    prefer_llm = sidebar()
    ag = get_agent(prefer_llm)

    t1, t2, t3, t4, t5 = st.tabs(
        ["Agent", "Review queue", "Approvals", "Memory review", "Audit"])
    with t1:
        tab_agent(ag)
    with t2:
        tab_queue(ag)
    with t3:
        tab_approvals(ag)
    with t4:
        tab_memory(ag)
    with t5:
        tab_audit(ag)


if __name__ == "__main__":
    main()
