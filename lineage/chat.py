"""
Phase 3/4 - LLM-driven lineage chatbot.

This is the single, merged chatbot entry point (ChatGPT-style: ask
anything, free-form). The model answers using ONLY facts it retrieves
via the get_column_lineage / get_forward_lineage / list_known_tables
tools (see llm_tools.py) - the actual lineage computation is still the
deterministic resolver, never the LLM's own guess.

Works with ANY OpenAI-compatible endpoint - Ollama, vLLM, LM Studio,
Groq, Together, Fireworks, OpenAI itself, etc. Configure via
environment variables, no code changes needed to switch providers:

    LLM_BASE_URL   e.g. http://localhost:11434/v1   (Ollama default)
    LLM_API_KEY    dummy value is fine for local Ollama/vLLM
    LLM_MODEL      e.g. gpt-oss:120b

Usage:
    pip install openai
    set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL as needed (or accept defaults)
    python -m lineage.chat
"""

import json
import os
import traceback

from lineage.build_registry import build_registry
from lineage.llm_tools import TOOLS, execute_tool

SYSTEM_PROMPT = """You are an ETL data-lineage assistant for a Teradata BTEQ codebase.

You have exactly two reliable sources of truth, and you must use the right
one for the direction being asked:

- get_column_lineage(table, column): BACKWARD lineage - "where does this
  column's data COME FROM". Traces upstream through views, volatile tables,
  and other scripts' INSERT targets to the ultimate base source.

- get_forward_lineage(table, column): FORWARD lineage - "where does this
  column's data GO TO / who uses it downstream". Given a source table.column,
  returns every other table.column that directly or transitively depends on
  it. Use this for questions like "what uses X", "what depends on X", "if I
  change this column what else is affected", "downstream impact of X".

Both tools are deterministic and exhaustive over whatever scripts have been
loaded - they are never wrong within what they've seen, and you must never
state a source, a downstream consumer, or a transformation that did not come
from one of these tool results.

Rules you must follow:
- NEVER state a source table, source column, or transformation that did not
  come from a tool result in this conversation. If you don't know, call a tool.
- Always call get_column_lineage before answering a lineage question, even if
  you think you recall the answer from earlier in the conversation - call the
  tool again to be sure, registries can be large.
- If get_column_lineage returns a hop with kind "BASE", say plainly that no
  loaded script defines that table further - do NOT speculate about what its
  true origin might be beyond that.
- If a hop has "terminal_literal": true, the column is NOT sourced from any
  table at all - it's set to a fixed/computed value (e.g. 0.0, a string
  constant, CURRENT_TIMESTAMP). State this clearly and explicitly, e.g.
  "MT_RWA_VR_NR_OF is hardcoded to the literal value 0.0 in this script - it
  isn't sourced from any table." Do not describe a terminal_literal hop as if
  it came from a table.
- If get_forward_lineage returns an empty downstream_usages list, say plainly
  that no loaded script was found to consume that column further - do not
  assume it's genuinely unused system-wide, only that nothing in what's
  loaded uses it.
- If a question is ambiguous about which table or column is meant, ask a
  clarifying question rather than guessing, or call list_known_tables to check
  what's actually available and suggest close matches.
- When presenting a multi-hop chain, explain it in plain language, e.g.
  "CD_CLA_EXP in EXP_ARC_EFPB3 comes from an inline subquery, which pulls it
  directly from the view V_COUR_SRS_O_ARC.CD_CLA_EXP - and no script defines
  that further, so that view is the ultimate known source."
- If asked something unrelated to lineage (e.g. general SQL help), answer
  normally, you don't need a tool for that.
"""


def get_client():
    from openai import OpenAI

    base_url = os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")
    api_key = os.environ.get("LLM_API_KEY", "ollama")
    model = os.environ.get("LLM_MODEL", "gpt-oss:120b")
    return OpenAI(base_url=base_url, api_key=api_key), model, base_url


def run_turn(client, model, messages, registry, on_tool_result=None, _depth=0, _max_depth=8):
    """Calls the model; if it requests tool calls, executes them and loops
    until it produces a final text answer (or hits the safety depth cap).

    on_tool_result(tool_name, args, result): optional callback invoked with
    every tool call's raw JSON result, in call order - lets a caller (e.g.
    the Streamlit UI) capture the exact deterministic data behind the
    LLM's eventual prose, for rendering as a table separately.
    """
    if _depth >= _max_depth:
        return "(stopped - too many tool-call rounds without a final answer)"

    response = client.chat.completions.create(
        model=model, messages=messages, tools=TOOLS, tool_choice="auto",
    )
    msg = response.choices[0].message
    messages.append(msg.model_dump(exclude_none=True))

    if not msg.tool_calls:
        return msg.content

    for tc in msg.tool_calls:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        result = execute_tool(tc.function.name, args, registry)
        if on_tool_result is not None:
            on_tool_result(tc.function.name, args, result)
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": json.dumps(result),
        })

    return run_turn(client, model, messages, registry, on_tool_result, _depth + 1, _max_depth)


def main():
    try:
        client, model, base_url = get_client()
    except ImportError:
        print("The 'openai' package isn't installed. Run: pip install openai")
        return

    print(f"Model endpoint: {base_url}  |  model: {model}")
    print("Building lineage registry from cleaned_sql/ ...")
    registry = build_registry(verbose=True)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    print("\nReady. Ask anything about the ETL lineage. Type 'quit' to exit.\n")

    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if question.lower() in {"quit", "exit"}:
            break
        if not question:
            continue

        messages_len_before = len(messages)
        messages.append({"role": "user", "content": question})
        try:
            answer = run_turn(client, model, messages, registry)
        except Exception as e:  # noqa: BLE001 - surface any API/connection error plainly
            print(f"\n[LLM call failed] {type(e).__name__}: {e}")
            print("----- full traceback (send me this) -----")
            traceback.print_exc()
            print("-------------------------------------------")
            # roll back EVERYTHING added during this failed turn (the user
            # message, and any partial assistant/tool messages appended
            # before the failure), not just the last item - otherwise a
            # mid-tool-call failure leaves a malformed history that breaks
            # every subsequent question too.
            del messages[messages_len_before:]
            continue

        print("\n" + (answer or "(model returned no text)") + "\n")


if __name__ == "__main__":
    main()
