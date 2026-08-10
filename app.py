"""
Streamlit UI for the ETL Lineage Chatbot - pure Streamlit, no custom
CSS/HTML. Visual styling comes entirely from:
  - .streamlit/config.toml  (Streamlit's native theme engine)
  - built-in components: st.container(border=True), st.columns,
    st.metric, st.dataframe, st.chat_message, st.info/success/warning/error

Run from the project root (same folder as run_pipeline.py):
    pip install -r requirements.txt
    streamlit run app.py

Reuses the exact same deterministic resolver + LLM tool-calling loop as
lineage/chat.py - this is a UI layer on top, not a separate engine. Every
answer's underlying facts come from lineage/resolver.py and graph.py,
never from the LLM's own memory.
"""

import json
import traceback

import pandas as pd
import streamlit as st

from lineage.build_registry import build_registry
from lineage.chat import SYSTEM_PROMPT, get_client, run_turn
from lineage.graph import get_or_build_edges

st.set_page_config(page_title="ETL Lineage Assistant", layout="wide")

# label + which native Streamlit alert box color to use per hop kind
KIND_INFO = {
    "INSERT_TARGET":     ("Insert target", "info"),
    "VIEW":              ("View", "success"),
    "VOLATILE_TABLE":    ("Volatile table", "warning"),
    "INLINE_SUBQUERY":   ("Inline subquery", "info"),
    "BASE":              ("Base table", None),
    "NOT_FOUND":         ("Not found", "error"),
    "MAX_DEPTH_STOPPED": ("Depth limit reached", "error"),
}


# --------------------------------------------------------------------------
# Registry + LLM client (cached across reruns within one server session)
# --------------------------------------------------------------------------

@st.cache_resource(show_spinner="Building lineage registry from cleaned_sql/ ...")
def load_registry(_cache_key: int):
    return build_registry(verbose=False)


@st.cache_resource
def load_client():
    return get_client()


def build_edges_with_progress(_registry, _cache_key: int):
    """Builds the forward-lineage index once, showing real progress instead
    of an unexplained pause. This is what was previously happening silently
    on the first forward-lineage question - now it happens eagerly, once,
    right after the registry loads, with visible feedback."""
    progress_bar = st.progress(0.0, text="Building forward-lineage index...")

    def _on_progress(current, total, table_name):
        frac = current / total if total else 1.0
        progress_bar.progress(frac, text=f"Indexing {current}/{total}: {table_name}")

    edges = get_or_build_edges(_registry, verbose=False, on_progress=_on_progress)
    progress_bar.empty()
    return edges


if "rebuild_key" not in st.session_state:
    st.session_state.rebuild_key = 0
if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
if "last_tool_results" not in st.session_state:
    st.session_state.last_tool_results = []

try:
    client, model, base_url = load_client()
    client_error = None
except ImportError:
    client, model, base_url = None, None, None
    client_error = "The 'openai' package isn't installed. Run: pip install openai"

registry = load_registry(st.session_state.rebuild_key)

if "edges_ready" not in st.session_state:
    build_edges_with_progress(registry, st.session_state.rebuild_key)
    st.session_state.edges_ready = True


# --------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------

with st.sidebar:
    st.title("Lineage Assistant")
    st.caption("ETL data-lineage chatbot")

    with st.container(border=True):
        st.caption("Model endpoint")
        st.text(base_url)
        st.caption("Model")
        st.text(model)

    tables = registry.all_persistent_names()
    volatiles = registry.all_volatile_names()
    col1, col2 = st.columns(2)
    col1.metric("Persistent tables", len(tables))
    col2.metric("Volatile tables", len(volatiles))

    if st.button("Rebuild registry", use_container_width=True,
                  help="Run this after you've added new files to raw_sql/ AND already "
                       "run 'python run_pipeline.py' to clean them into cleaned_sql/."):
        st.session_state.rebuild_key += 1
        st.session_state.edges_ready = False
        load_registry.clear()
        st.rerun()

    st.divider()
    st.caption("Known tables")
    st.dataframe(pd.DataFrame({"Table": tables}), use_container_width=True,
                 hide_index=True, height=220)

    st.divider()
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        st.session_state.last_tool_results = []
        st.rerun()


# --------------------------------------------------------------------------
# Table rendering for tool results
# --------------------------------------------------------------------------

def hops_to_dataframe(hops: list[dict]) -> pd.DataFrame:
    rows = []
    for i, h in enumerate(hops):
        label, _ = KIND_INFO.get(h.get("kind", ""), (h.get("kind", ""), None))
        note = h.get("transform") or ""
        if h.get("terminal_literal"):
            note = f"FIXED VALUE: {h.get('transform')}"
        rows.append({
            "Step": i + 1,
            "Table": h.get("table"),
            "Column": h.get("column"),
            "Kind": label,
            "Source file": h.get("source_file") or "",
            "Note": note,
        })
    return pd.DataFrame(rows)


def downstream_to_dataframe(usages: list[dict]) -> pd.DataFrame:
    rows = [{"Hops downstream": u["hops_downstream"], "Table": u["table"], "Column": u["column"]}
            for u in sorted(usages, key=lambda x: x["hops_downstream"])]
    return pd.DataFrame(rows)


def render_flow(hops: list[dict]):
    """A left-to-right chain of bordered containers, native Streamlit
    columns only - no HTML/CSS."""
    n = len(hops)
    widths = []
    for i in range(n):
        widths.append(4)
        if i < n - 1:
            widths.append(1)  # narrow arrow column
    cols = st.columns(widths)

    col_idx = 0
    for i, h in enumerate(hops):
        with cols[col_idx]:
            with st.container(border=True):
                label, alert = KIND_INFO.get(h.get("kind", ""), (h.get("kind", ""), None))
                table_short = (h.get("table") or "").split(".")[-1]
                st.caption(label)
                st.markdown(f"**{table_short}**")
                st.text(h.get("column"))
        col_idx += 1
        if i < n - 1:
            with cols[col_idx]:
                st.text("")
                st.markdown("**->**")
            col_idx += 1


def render_tool_result(name: str, args: dict, result: dict):
    if name == "get_column_lineage" and "hops" in result:
        st.subheader(f"Backward lineage: {result.get('table')}.{result.get('column')}")
        render_flow(result["hops"])
        df = hops_to_dataframe(result["hops"])
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download as CSV", df.to_csv(index=False),
            file_name=f"lineage_{result.get('table')}_{result.get('column')}.csv",
            key=f"dl_{name}_{result.get('table')}_{result.get('column')}_{len(st.session_state.last_tool_results)}",
        )

    elif name == "get_forward_lineage" and "downstream_usages" in result:
        st.subheader(f"Forward lineage: {result.get('table')}.{result.get('column')}")
        if not result["downstream_usages"]:
            st.info(result.get("note", "No downstream consumers found in loaded scripts."))
        else:
            df = downstream_to_dataframe(result["downstream_usages"])
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download as CSV", df.to_csv(index=False),
                file_name=f"forward_{result.get('table')}_{result.get('column')}.csv",
                key=f"dlf_{name}_{result.get('table')}_{result.get('column')}_{len(st.session_state.last_tool_results)}",
            )

    elif name == "list_known_tables":
        st.dataframe(pd.DataFrame({"Table": result.get("tables", [])}),
                     use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------
# Main layout
# --------------------------------------------------------------------------

st.title("ETL Lineage Assistant")
st.caption("Ask where a column comes from, where it flows to, or explore the registry - "
           "every answer is backed by deterministic SQL parsing, not guesses.")

if client_error:
    st.error(client_error)
    st.stop()

for msg in st.session_state.messages:
    if msg["role"] == "system":
        continue
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.markdown(msg["content"])
    elif msg["role"] == "assistant" and msg.get("content"):
        with st.chat_message("assistant"):
            st.markdown(msg["content"])

if st.session_state.last_tool_results:
    st.divider()
    st.subheader("Structured result")
    st.caption("Built directly from the tool's raw data - always accurate, independent of how the model phrases its answer.")
    for name, args, result in st.session_state.last_tool_results:
        render_tool_result(name, args, result)

question = st.chat_input("Ask about lineage - e.g. where does CD_CLA_EXP come from in EXP_ARC_EFPB3")

if question:
    with st.chat_message("user"):
        st.markdown(question)

    messages_len_before = len(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": question})
    st.session_state.last_tool_results = []

    def _capture(name, args, result):
        st.session_state.last_tool_results.append((name, args, result))

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                answer = run_turn(client, model, st.session_state.messages, registry,
                                   on_tool_result=_capture)
            except Exception as e:  # noqa: BLE001
                del st.session_state.messages[messages_len_before:]
                st.error(f"LLM call failed: {type(e).__name__}: {e}")
                with st.expander("Full traceback"):
                    st.code(traceback.format_exc())
                st.stop()
        st.markdown(answer or "(model returned no text)")

    st.rerun()
