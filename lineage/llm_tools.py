"""
Phase 4 - Tool definitions the LLM can call, and their execution.

Design principle: the LLM NEVER states a source table/column/transform
that didn't come from one of these tool results. These tools are thin
wrappers around the already-tested deterministic resolver
(resolve_column, forward_lineage) - the LLM's job is only to (a) figure
out which table/column the user means, (b) call the right tool,
(c) phrase the result in natural language. It cannot invent lineage
facts because it has no other way to know them.
"""

import re

from lineage.resolver import resolve_column
from lineage.graph import get_or_build_edges, forward_lineage

_TABLE_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def match_table(candidate: str, registry) -> str | None:
    """Fuzzy-match a bare or partial name against known persistent tables,
    e.g. 'EXP_ARC_EFPB3' matches 'DB_SRC_WORK.EXP_ARC_EFPB3'."""
    if not candidate:
        return None
    cand_upper = candidate.strip().upper()
    for name in registry.all_persistent_names():
        bare = name.split(".")[-1]
        if bare == cand_upper or name == cand_upper:
            return name
    return None


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_column_lineage",
            "description": (
                "Get the real, verified lineage of a column in a table by tracing "
                "through the actual ETL scripts (views, volatile tables, nested "
                "subqueries, other scripts' INSERT targets) until it reaches a base "
                "table or runs out of loaded scripts. This is the ONLY reliable way "
                "to know where a column's data comes from - never guess this from "
                "general knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name, fully-qualified or bare, e.g. "
                                        "'DB_SRC_WORK.EXP_ARC_EFPB3' or 'EXP_ARC_EFPB3'.",
                    },
                    "column": {
                        "type": "string",
                        "description": "Column name, e.g. 'CD_CLA_EXP'.",
                    },
                },
                "required": ["table", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_known_tables",
            "description": (
                "List every table currently known to the lineage registry (every "
                "INSERT target or VIEW found across all loaded/cleaned scripts). "
                "Use this to check whether a table the user mentioned actually "
                "exists before answering, or to help them if they're unsure of "
                "the exact name."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_forward_lineage",
            "description": (
                "Find everywhere a column's data flows TO downstream - the opposite "
                "of get_column_lineage. Given a source table.column, returns every "
                "other table.column in the loaded scripts that directly or "
                "transitively consumes it (e.g. 'if I change this column, what else "
                "gets affected'). This is the ONLY reliable way to know downstream "
                "impact - never guess this from general knowledge."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Source table name, fully-qualified or bare, "
                                        "e.g. 'DB_SRC_WORK.WRK_ARRIMAGE_SRSK_ARC_EXP'.",
                    },
                    "column": {
                        "type": "string",
                        "description": "Source column name, e.g. 'CD_PROFIL_ALIM'.",
                    },
                },
                "required": ["table", "column"],
            },
        },
    },
]


def _hop_to_dict(hop) -> dict:
    return {
        "table": hop.table,
        "column": hop.column,
        "source_file": hop.source_file,
        "kind": hop.kind,
        "transform": hop.transform,
        "terminal_literal": hop.terminal_literal,
    }


def execute_tool(name: str, args: dict, registry) -> dict:
    if name == "get_column_lineage":
        table_input = args.get("table", "")
        column = args.get("column", "")

        matched = match_table(table_input, registry)
        if matched is None and registry.lookup(table_input, current_file=None):
            matched = table_input

        if matched is None:
            return {
                "error": f"'{table_input}' was not found in the lineage registry. "
                         f"Call list_known_tables to see what's available."
            }

        definition = registry.lookup(matched, current_file=None)
        chain = resolve_column(registry, matched, column, start_file=definition.source_file)
        return {
            "table": matched,
            "column": column,
            "hops": [_hop_to_dict(h) for h in chain],
        }

    if name == "list_known_tables":
        return {"tables": registry.all_persistent_names()}

    if name == "get_forward_lineage":
        table_input = args.get("table", "")
        column = args.get("column", "")

        matched = match_table(table_input, registry)
        if matched is None and registry.lookup(table_input, current_file=None):
            matched = table_input
        node_table = matched or table_input.upper()

        edges = get_or_build_edges(registry, verbose=False)
        downstream = forward_lineage(edges, node_table, column)

        if not downstream:
            return {
                "table": node_table,
                "column": column,
                "downstream_usages": [],
                "note": "No loaded script was found to consume this column downstream "
                        "(it may not be used further, or the consuming script hasn't "
                        "been loaded into raw_sql/ yet).",
            }
        return {"table": node_table, "column": column, "downstream_usages": downstream}

    return {"error": f"unknown tool '{name}'"}
