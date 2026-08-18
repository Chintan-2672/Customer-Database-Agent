"""
Streamlit chatbot UI for the Customer DB Agent (LangGraph).

Shows a normal chat interface up top. While the agent works, a live
"execution trace" status box shows what it's doing (extracted criteria,
generated SQL, row counts) — same idea as the terminal trace in agent.py.
Once done, only the synthesized natural-language answer is shown in the
chat itself, same as how an LLM chat reply looks.

Run with:
    streamlit run streamlit_app.py
"""

import uuid
import streamlit as st

# Import your compiled graph + state schema from agent.py
# (rename this import if your file is named differently)
from agent import workflow, AgentState

st.set_page_config(page_title="Customer DB Agent", page_icon="🗂️", layout="wide")


# ---------------------------------------------------------------------------
# Session state setup
# ---------------------------------------------------------------------------
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []  # list of {"role": "user"/"assistant", "content": str}

if "pending_clarification" not in st.session_state:
    st.session_state.pending_clarification = None  # holds the clarifying question, if any

if "clarification_options" not in st.session_state:
    st.session_state.clarification_options = []  # list of option strings to choose from

CONFIG = {"configurable": {"thread_id": st.session_state.thread_id}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def node_label(node_name: str) -> str:
    """Pretty label for each node, matching your terminal print style."""
    labels = {
        "query_analyzer": "Query Analyzer",
        "sql_generator": "SQL Generator",
        "query_executor": "Query Executor",
        "response_synthesizer": "Writing Answer",
        "clarification_handler": "Clarification Handler",
    }
    return labels.get(node_name, node_name)


def run_graph_streamed(inputs, status_box):
    """
    Streams the LangGraph execution and writes live updates into the
    given st.status() box — this is the "thinking" trace only.
    The chat message itself is populated separately from final_answer.
    """
    final_chunk = None

    for chunk in workflow.stream(inputs, config=CONFIG, stream_mode="updates"):
        # `chunk` looks like: {"query_analyzer": {...partial state...}}
        for node_name, node_output in chunk.items():
            status_box.write(f"**{node_label(node_name)}**")

            if node_name == "query_analyzer":
                if node_output.get("is_ambiguous"):
                    status_box.write(f"⚠️ Ambiguity detected: {node_output.get('clarifying_question')}")
                else:
                    crit = node_output.get("parsed_criteria", {})
                    status_box.write(f"Extracted criteria: `{crit}`")

            elif node_name == "sql_generator":
                sql = node_output.get("sql_query", "")
                status_box.write("Generated SQL:")
                status_box.code(sql, language="sql")

            elif node_name == "query_executor":
                if node_output.get("error_message"):
                    status_box.write(f"❌ Execution failed: {node_output['error_message']}")
                else:
                    n_rows = len(node_output.get("query_results", []) or [])
                    status_box.write(f"✅ Execution successful: {n_rows} rows returned.")

            elif node_name == "response_synthesizer":
                status_box.write("Turning results into a plain-language answer...")

            final_chunk = node_output

    return final_chunk


def format_final_answer(state_snapshot) -> str:
    """Turn the final graph state into a chat-friendly assistant reply.
    This is the ONLY thing shown in the chat bubble — no raw SQL, no JSON dumps."""
    values = state_snapshot.values

    if values.get("is_ambiguous"):
        return values.get("clarifying_question", "Could you clarify which one you mean?")

    if values.get("error_message"):
        return "Sorry, I ran into a problem answering that. Mind rephrasing the question?"

    # This is set by the response_synthesizer node in agent.py
    final_answer = values.get("final_answer")
    if final_answer:
        return final_answer

    # Fallback, shouldn't normally hit this if the graph ran to completion
    return "I wasn't able to generate an answer for that."


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
st.title("🗂️ Customer DB Agent")
st.caption("Ask about customers, orders, products, or interactions.")

# Render past chat history
for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# If we're waiting on the user to resolve an ambiguous customer match,
# show quick-select buttons instead of (or in addition to) free text.
if st.session_state.pending_clarification:
    st.info(st.session_state.pending_clarification)
    cols = st.columns(len(st.session_state.clarification_options) or 1)
    for i, option in enumerate(st.session_state.clarification_options):
        if cols[i].button(option, key=f"clarify_{i}"):
            st.session_state["_resume_choice"] = option

user_input = st.chat_input("Ask about a customer, order, product, or interaction...")

# Resolve either a typed message or a button click as the next input
resume_choice = st.session_state.pop("_resume_choice", None)
incoming = resume_choice or user_input

if incoming:
    # Echo the user's message
    st.session_state.chat_history.append({"role": "user", "content": incoming})
    with st.chat_message("user"):
        st.markdown(incoming)

    with st.chat_message("assistant"):
        status_box = st.status("Thinking...", expanded=False)

        if st.session_state.pending_clarification:
            # Resuming a paused graph after ambiguity was resolved
            workflow.update_state(CONFIG, {"resolved_selection": incoming})
            run_graph_streamed(None, status_box)
            st.session_state.pending_clarification = None
            st.session_state.clarification_options = []
        else:
            # Fresh turn
            inputs = {"user_query": incoming}
            run_graph_streamed(inputs, status_box)

        # Inspect final state to decide what to show / whether we paused again
        snapshot = workflow.get_state(CONFIG)
        values = snapshot.values

        if values.get("is_ambiguous"):
            # Graph paused again waiting on clarification
            st.session_state.pending_clarification = values.get("clarifying_question")
            crit = values.get("parsed_criteria", {})
            st.session_state.clarification_options = crit.get("matching_details", [])
            reply = values.get("clarifying_question", "Could you clarify?")
            status_box.update(label="Waiting on clarification", state="complete")
        else:
            reply = format_final_answer(snapshot)
            status_box.update(label="Done thinking", state="complete")

        st.markdown(reply)

    st.session_state.chat_history.append({"role": "assistant", "content": reply})
    st.rerun()