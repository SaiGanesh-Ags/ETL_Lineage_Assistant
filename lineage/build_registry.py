"""
Phase 2 - Build a TableRegistry from every cleaned .sql file.

For each statement in each file, classify it and register a
TableDefinition if it's lineage-relevant:

  CREATE VOLATILE [MULTISET] TABLE name AS (select ...) ...
      -> register_volatile(file, name, select_ast)   (bare name, no schema)

  CREATE VIEW name AS select ...
      -> register_persistent(name, select_ast)         (schema-qualified name)

  INSERT INTO schema.table (...) SELECT ...
      -> register_persistent(schema.table, select_ast)

Everything else (DELETE, DROP, UPDATE, COLLECT STATISTICS, BEGIN/END
TRANSACTION, etc.) is intentionally skipped - it carries no column
lineage information we need. Any statement that fails to parse is
logged and skipped rather than crashing the whole build, since one bad
statement shouldn't block lineage answers for everything else that DID
parse fine.
"""

from pathlib import Path

from lineage.resolver import TableRegistry, TableDefinition, DefKind

BASE_DIR = Path(__file__).resolve().parent.parent
CLEANED_DIR = BASE_DIR / "cleaned_sql"
DIALECT = "teradata"


def naive_split_statements(text: str) -> list[str]:
    parts = [p.strip() for p in text.split(";")]
    return [p for p in parts if p and not p.isspace()]


def _extract_target_table(insert_or_create_this, exp):
    """The 'this' of an Insert/Create node can be a bare Table, or a
    Schema node wrapping a Table + explicit column list. Find the Table
    either way."""
    if isinstance(insert_or_create_this, exp.Table):
        return insert_or_create_this
    found = insert_or_create_this.find(exp.Table)
    return found


def build_registry(verbose: bool = True) -> TableRegistry:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError

    registry = TableRegistry()
    cleaned_files = sorted(CLEANED_DIR.glob("*.sql"))

    stats = {"registered": 0, "skipped_irrelevant": 0, "skipped_error": 0}

    for f in cleaned_files:
        text = f.read_text(encoding="utf-8")
        for stmt in naive_split_statements(text):
            try:
                parsed = sqlglot.parse_one(stmt, dialect=DIALECT)
            except ParseError as e:
                stats["skipped_error"] += 1
                if verbose:
                    print(f"[PARSE ERROR] {f.name}: {e}")
                continue
            except Exception as e:  # noqa: BLE001
                stats["skipped_error"] += 1
                if verbose:
                    print(f"[UNEXPECTED ERROR] {f.name}: {type(e).__name__}: {e}")
                continue

            if isinstance(parsed, exp.Insert):
                table = _extract_target_table(parsed.this, exp)
                select_ast = parsed.expression
                if table is None or select_ast is None:
                    stats["skipped_irrelevant"] += 1
                    continue
                fq_name = f"{table.db}.{table.name}" if table.db else table.name
                registry.register_persistent(TableDefinition(
                    name=fq_name, kind=DefKind.INSERT_TARGET,
                    select_ast=select_ast, source_file=f.name,
                ))
                stats["registered"] += 1

            elif isinstance(parsed, exp.Create):
                kind = (parsed.args.get("kind") or "").upper()
                select_ast = parsed.expression
                table = _extract_target_table(parsed.this, exp)

                if table is None or select_ast is None:
                    # plain DDL with no AS SELECT (column-defined volatile
                    # table with no data source) - nothing to register yet,
                    # it'll be populated by a later INSERT in the same file
                    stats["skipped_irrelevant"] += 1
                    continue

                stmt_upper = stmt.upper()
                is_volatile = "VOLATILE" in stmt_upper

                if is_volatile:
                    registry.register_volatile(f.name, TableDefinition(
                        name=table.name, kind=DefKind.VOLATILE_TABLE,
                        select_ast=select_ast, source_file=f.name,
                    ))
                    stats["registered"] += 1
                elif kind == "VIEW":
                    fq_name = f"{table.db}.{table.name}" if table.db else table.name
                    registry.register_persistent(TableDefinition(
                        name=fq_name, kind=DefKind.VIEW,
                        select_ast=select_ast, source_file=f.name,
                    ))
                    stats["registered"] += 1
                else:
                    stats["skipped_irrelevant"] += 1
            else:
                stats["skipped_irrelevant"] += 1

    if verbose:
        print(f"Registry built: {stats['registered']} definition(s) registered, "
              f"{stats['skipped_irrelevant']} statement(s) not lineage-relevant, "
              f"{stats['skipped_error']} statement(s) failed to parse.")
        print(f"  persistent tables: {registry.all_persistent_names()}")
        print(f"  volatile tables:   {registry.all_volatile_names()}")

    return registry


if __name__ == "__main__":
    build_registry()
