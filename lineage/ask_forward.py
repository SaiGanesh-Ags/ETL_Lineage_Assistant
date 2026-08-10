"""
CLI entry point to ask a FORWARD lineage question directly (no LLM needed) -
"where does this column's data flow TO downstream".

Usage:
    python -m lineage.ask_forward --table DB_SRC_WORK.WRK_ARRIMAGE_SRSK_ARC_EXP --column CD_PROFIL_ALIM
"""

import argparse

from lineage.build_registry import build_registry
from lineage.graph import get_or_build_edges, forward_lineage


def main():
    parser = argparse.ArgumentParser(description="Ask where a column's data flows TO downstream.")
    parser.add_argument("--table", required=True, help="source table, e.g. DB_SRC_WORK.WRK_ARRIMAGE_SRSK_ARC_EXP")
    parser.add_argument("--column", required=True, help="e.g. CD_PROFIL_ALIM")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    registry = build_registry(verbose=not args.quiet)
    edges = get_or_build_edges(registry, verbose=not args.quiet)
    downstream = forward_lineage(edges, args.table, args.column)

    print(f"\nForward lineage for {args.table.upper()}.{args.column.upper()}:\n")
    if not downstream:
        print("  No loaded script consumes this column further.")
        return

    for d in downstream:
        indent = "  " * d["hops_downstream"]
        print(f"{indent}-> {d['table']}.{d['column']}  ({d['hops_downstream']} hop(s) downstream)")


if __name__ == "__main__":
    main()
