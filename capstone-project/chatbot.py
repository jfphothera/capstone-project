"""Customer-facing support chatbot.

    streamlit run chatbot.py

What it may do on its own: answer routine questions, confirm order and
shipment status, and give troubleshooting steps **only** when they come from
a currently approved procedure that applies to the customer's confirmed
model and revision, and only after the reply has passed source verification.

What it may never do: invent a troubleshooting step, create a finalized RMA,
issue a shipping label, or commit to any consequential action. When the
conversation reaches any of those, it hands over to the representative by
opening a support case that carries the whole conversation, the customer's
details, the sources that were retrieved, the steps already tried, any safety
flags, and the reason for the handoff.

It asks for identification early, and does not ask again for anything already
supplied.
"""

from __future__ import annotations

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from capstone_rag import (  # noqa: E402
    caseflow, chat, generation, store as store_mod,
)

st.set_page_config(page_title="Device Support", layout="centered")


@st.cache_resource(show_spinner="Starting the assistant...")
def get_bot(prefer_llm: bool):
    store = store_mod.get_store()
    agent = caseflow.make_agent(prefer_llm=prefer_llm)
    return chat.ChatBot(store, agent)


def main():
    st.title("Device Support")
    st.caption("Synthetic demonstration. No real customers, devices or "
               "orders. This assistant can help with routine questions and "
               "approved troubleshooting; anything else goes to a person.")

    bot = get_bot(generation.llm_available())

    if not bot.store.one("SELECT 1 c FROM customers LIMIT 1"):
        st.error("No synthetic data loaded. Run "
                 "`python scripts/import_synthetic.py`")
        return

    if "chat_session" not in st.session_state:
        st.session_state["chat_session"] = bot.new_session()
        st.session_state["chat_log"] = []

    session = st.session_state["chat_session"]

    with st.sidebar:
        st.subheader("What the assistant knows")
        st.caption(f"Conversation `{session.conversation_id}`")
        st.write({
            "name given": session.claim.name,
            "date of birth given": bool(session.claim.dob),
            "serial given": session.claim.serial,
            "identity verified": session.verified,
            "device": (f"{session.device['model']} Rev "
                       f"{session.device['revision']}"
                       if session.device else None),
            "steps already tried": session.steps_tried,
        })
        if session.handed_off:
            st.warning(f"Handed to support: {session.handoff_case_id}")
        st.markdown("---")
        if st.button("Start a new conversation", width="stretch"):
            st.session_state["chat_session"] = bot.new_session()
            st.session_state["chat_log"] = []
            st.rerun()
        st.caption("Try: *my unit is completely dead* · *the goggles were "
                   "missing* · *I got a burn during a session* · "
                   "*where is my order*")

    for role, text in st.session_state["chat_log"]:
        with st.chat_message(role):
            st.write(text)

    if not st.session_state["chat_log"]:
        with st.chat_message("assistant"):
            st.write(chat.OPENING)
        st.session_state["chat_log"].append(("assistant", chat.OPENING))

    prompt = st.chat_input("Type your message")
    if prompt:
        st.session_state["chat_log"].append(("user", prompt))
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Checking..."):
                reply = bot.respond(session, prompt)
            st.write(reply.text)
            if reply.sources:
                with st.expander("Where this came from"):
                    for s in reply.sources:
                        st.caption(f"{s['procedure_id']} · "
                                   f"{s['document_id']} · {s['section']}")
            if reply.handed_off:
                st.info(f"Passed to our support team as "
                        f"{reply.handoff_case_id}. They have the whole "
                        f"conversation.")
        st.session_state["chat_log"].append(("assistant", reply.text))


if __name__ == "__main__":
    main()
