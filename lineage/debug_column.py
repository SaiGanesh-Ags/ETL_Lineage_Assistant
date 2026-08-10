"""
Debug helper - shows exactly what get_column_source() sees for a given
table.column, without going through the LLM at all. Use this whenever
the chatbot reports "could not resolve source table for this column" -
it'll show you the alias-to-table mapping it built, and the raw
expression it matched, so we can see WHY a match failed.

Usage:
    python -m lineage.debug_column --table DB_SRC_WORK.WRK_ARRIMAGE_AJUST_MT_PERIM --column MT_MA_RWA_VR_NR_OF
"""

import argparse

from lineage.build_registry import build_registry
from lineage.resolver import get_column_source
from lineage.llm_tools import match_table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--table", required=True)
    parser.add_argument("--column", required=True)
    args = parser.parse_args()

    from sqlglot import exp

    registry = build_registry(verbose=False)
    matched = match_table(args.table, registry) or args.table
    definition = registry.lookup(matched, current_file=None)
    if definition is None:
        print(f"'{args.table}' not found as a registered table.")
        return

    select_ast = definition.select_ast
    print(f"Inspecting top-level SELECT in {definition.source_file} for column '{args.column}'\n")

    alias_to_table = {}
    for src in select_ast.find_all(exp.Table):
        alias_to_table[src.alias_or_name] = f"{src.db}.{src.name}" if src.db else src.name
    print("alias_to_table found in this SELECT:")
    for k, v in alias_to_table.items():
        print(f"  {k!r} -> {v}")

    alias_to_subquery = {}
    for sq in select_ast.find_all(exp.Subquery):
        if sq.alias_or_name:
            alias_to_subquery[sq.alias_or_name] = "<subquery>"
    print("\nalias_to_subquery found in this SELECT:")
    for k in alias_to_subquery:
        print(f"  {k!r}")

    target_expr = None
    for proj in select_ast.selects:
        if proj.alias_or_name.upper() == args.column.upper():
            target_expr = proj
            break

    if target_expr is None:
        print(f"\nNo projection found with output alias '{args.column}' at this level.")
        return

    print(f"\nMatched projection: {target_expr.sql(dialect='teradata')!r}")
    col_refs = list(target_expr.find_all(exp.Column))
    print(f"Column node(s) found inside it: {len(col_refs)}")
    for c in col_refs:
        print(f"  table alias on this Column node: {c.table!r}   column name: {c.name!r}")

    print("\nresult = get_column_source(...) ->")
    print(get_column_source(select_ast, args.column))


if __name__ == "__main__":
    main()
