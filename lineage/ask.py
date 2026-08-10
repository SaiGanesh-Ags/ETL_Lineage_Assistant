"""
Phase 2 - CLI entry point to ask a lineage question directly (no LLM needed).

Usage:
    python -m lineage.ask --table DB_SRC_WORK.EXP_ARC_EFPB3 --column CD_CLA_EXP

The table name must be schema-qualified (as it appears after cleaning,
e.g. DB_SRC_WORK.TABLE_NAME) since that's how INSERT targets are
registered. Column name matches the column list you'd see in the
INSERT INTO (...) clause.
"""

import argparse

from lineage.build_registry import build_registry
from lineage.resolver import resolve_column, format_lineage


def main():
    parser = argparse.ArgumentParser(description="Ask where a column's data comes from.")
    parser.add_argument("--table", required=True, help="e.g. DB_SRC_WORK.EXP_ARC_EFPB3")
    parser.add_argument("--column", required=True, help="e.g. CD_CLA_EXP")
    parser.add_argument("--quiet", action="store_true", help="suppress registry build summary")
    args = parser.parse_args()

    registry = build_registry(verbose=not args.quiet)

    definition = registry.lookup(args.table, current_file=None)
    if definition is None:
        print(f"\n'{args.table}' was never found as an INSERT target or VIEW in any "
              f"loaded script - nothing to trace.")
        return

    chain = resolve_column(registry, args.table, args.column, start_file=definition.source_file)

    print(f"\nLineage for {args.table}.{args.column}:\n")
    print(format_lineage(chain))


if __name__ == "__main__":
    main()
