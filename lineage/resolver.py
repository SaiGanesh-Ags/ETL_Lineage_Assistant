"""
Phase 2/3 - Registry + recursive column-lineage resolver.

Two-namespace registry design:

  PERSISTENT tables - always schema-qualified (${DB_SRC_WORK}.TABLE, i.e.
  "DB_SRC_WORK.TABLE" after cleaning). Globally unique, so stored in one
  flat dict keyed by "SCHEMA.TABLE". Covers: views, other scripts'
  INSERT targets.

  VOLATILE tables - created with CREATE VOLATILE [MULTISET] TABLE, always
  referenced WITHOUT a schema prefix (bare name). They only exist for the
  lifetime of the script that created them (create -> use -> drop), so
  two different scripts can reuse the same volatile table name for
  unrelated things. Stored keyed by (source_file, NAME) so a lookup for
  a bare name only ever searches the CURRENT file's volatile tables,
  never another script's.

A column's origin can require walking through several DIFFERENT kinds
of hops:
  (a) an inline subquery within the SAME statement, e.g. "from (select ...) as s"
      -> recurse straight into that subquery's AST, no registry needed.
  (b) a bare (unqualified) table name -> look up in the CURRENT FILE's
      volatile-table namespace.
  (c) a schema-qualified table name -> look up in the global persistent
      namespace (view / another script's INSERT target).
  (d) not found in either -> terminal/base source, stop.
  (e) a literal or multi-column expression (CASE, concatenation, etc.)
      -> not a traceable single source column; stop and show the raw
      expression instead. Flagged explicitly via terminal_literal=True.
"""

from dataclasses import dataclass
from enum import Enum

# see get_column_source() docstring - caches per-select alias/projection
# scans so a wide table's columns aren't all independently re-scanning the
# same SELECT tree from scratch
_SELECT_SCAN_CACHE: dict[int, tuple] = {}


class DefKind(Enum):
    VIEW = "VIEW"
    VOLATILE_TABLE = "VOLATILE_TABLE"
    INSERT_TARGET = "INSERT_TARGET"      # a persistent table populated by another script
    INLINE_SUBQUERY = "INLINE_SUBQUERY"  # same-statement derived table, e.g. "as s"


@dataclass
class TableDefinition:
    name: str                  # "SCHEMA.TABLE" for persistent, bare "NAME" for volatile
    kind: DefKind
    select_ast: object          # sqlglot Select/Union expression that defines this table
    source_file: str            # which script this came from


class TableRegistry:
    def __init__(self):
        self._persistent: dict[str, TableDefinition] = {}         # "SCHEMA.TABLE" -> def
        self._volatile: dict[tuple[str, str], TableDefinition] = {}  # (file, NAME) -> def

    def register_persistent(self, d: TableDefinition):
        self._persistent[d.name.upper()] = d

    def register_volatile(self, source_file: str, d: TableDefinition):
        self._volatile[(source_file, d.name.upper())] = d

    def lookup(self, table_name: str, current_file: str):
        """A dotted name (SCHEMA.TABLE) is always persistent/global.
        A bare name is always volatile, scoped to current_file."""
        if not table_name:
            return None
        if "." in table_name:
            return self._persistent.get(table_name.upper())
        return self._volatile.get((current_file, table_name.upper()))

    def all_persistent_names(self):
        return sorted(self._persistent.keys())

    def all_volatile_names(self):
        return sorted(self._volatile.keys())


@dataclass
class LineageHop:
    table: str
    column: str
    source_file: str | None
    kind: str
    transform: str | None = None
    terminal_literal: bool = False  # True = fixed/computed literal (0.0, etc.), not from a table


def _fq_name(table_exp) -> str:
    """exp.Table -> 'SCHEMA.NAME' if schema-qualified, else bare 'NAME'."""
    name = table_exp.name
    if table_exp.db:
        return f"{table_exp.db}.{name}"
    return name


def get_column_source(select_ast, column_alias: str):
    """
    Find the projection in select_ast matching column_alias and classify
    where it comes from. Returns a dict:
        {
          "kind": "TABLE" | "SUBQUERY" | "LITERAL_OR_MULTI" | "NOT_FOUND",
          "table_name": str or None,           # set when kind == TABLE
          "subquery_ast": Expression or None,  # set when kind == SUBQUERY
          "column": str or None,
          "raw_sql": str or None,
        }

    PERFORMANCE NOTE: this gets called once per column being resolved, and
    forward-lineage resolves every column of every table - so for a wide
    table (80-100+ columns), the SAME select_ast would otherwise get its
    alias/table/subquery structure re-scanned from scratch on every single
    call. Fixed by caching the alias maps and a name->projection lookup
    per select_ast (keyed by id(), safe since these ASTs live for the
    registry's lifetime), so each SELECT only ever gets scanned once no
    matter how many of its columns are queried.
    """
    from sqlglot import exp

    key = id(select_ast)
    cached = _SELECT_SCAN_CACHE.get(key)
    if cached is None:
        alias_to_table = {}
        for src in select_ast.find_all(exp.Table):
            alias_to_table[src.alias_or_name] = _fq_name(src)

        alias_to_subquery = {}
        for sq in select_ast.find_all(exp.Subquery):
            alias = sq.alias_or_name
            if alias:
                alias_to_subquery[alias] = sq.this

        proj_by_name = {}
        for proj in select_ast.selects:
            name = proj.alias_or_name
            if name:
                proj_by_name[name.upper()] = proj

        cached = (alias_to_table, alias_to_subquery, proj_by_name)
        _SELECT_SCAN_CACHE[key] = cached

    alias_to_table, alias_to_subquery, proj_by_name = cached
    target_expr = proj_by_name.get(column_alias.upper())

    if target_expr is None:
        return {"kind": "NOT_FOUND", "table_name": None, "subquery_ast": None,
                "column": None, "raw_sql": None}

    raw_sql = target_expr.sql(dialect="teradata")
    col_refs = list(target_expr.find_all(exp.Column))

    if len(col_refs) != 1:
        return {"kind": "LITERAL_OR_MULTI", "table_name": None, "subquery_ast": None,
                "column": None, "raw_sql": raw_sql}

    col = col_refs[0]
    alias = col.table

    if alias in alias_to_subquery:
        return {"kind": "SUBQUERY", "table_name": None,
                "subquery_ast": alias_to_subquery[alias],
                "column": col.name, "raw_sql": raw_sql}

    if alias in alias_to_table:
        real_table = alias_to_table[alias]
    elif not alias and len(alias_to_table) == 1:
        real_table = next(iter(alias_to_table.values()))
    elif not alias and len(alias_to_subquery) == 1:
        return {"kind": "SUBQUERY", "table_name": None,
                "subquery_ast": next(iter(alias_to_subquery.values())),
                "column": col.name, "raw_sql": raw_sql}
    else:
        real_table = alias or col.table

    return {"kind": "TABLE", "table_name": real_table, "subquery_ast": None,
            "column": col.name, "raw_sql": raw_sql}


def resolve_column(
    registry: TableRegistry,
    start_table: str,
    start_column: str,
    start_file: str,
    _select_ast=None,
    _current_file=None,
    _chain: list | None = None,
    _depth: int = 0,
    _max_depth: int = 40,
) -> list:
    """
    Recursively walk from (start_table, start_column) back to its origin.
    start_file: the file the CALLER already knows start_table lives in
    (needed so a bare/volatile start_table resolves in the right scope).
    """
    chain = _chain if _chain is not None else []
    current_file = _current_file if _current_file is not None else start_file

    if _depth >= _max_depth:
        chain.append(LineageHop(start_table, start_column, None, "MAX_DEPTH_STOPPED"))
        return chain

    if _select_ast is not None:
        select_ast = _select_ast
        source_file = current_file
        kind_for_this_hop = "INLINE_SUBQUERY"
    else:
        definition = registry.lookup(start_table, current_file)
        if definition is None:
            chain.append(LineageHop(start_table, start_column, None, "BASE"))
            return chain
        select_ast = definition.select_ast
        source_file = definition.source_file
        kind_for_this_hop = definition.kind.value

    result = get_column_source(select_ast, start_column)

    if result["kind"] == "NOT_FOUND":
        chain.append(LineageHop(
            start_table, start_column, source_file, kind_for_this_hop,
            transform=f"[column '{start_column}' not found in this SELECT's output list]",
        ))
        return chain

    if result["kind"] == "TABLE" and not result.get("table_name"):
        # Safety net: get_column_source thought this was a single-column
        # table reference but couldn't pin down which table (e.g. an alias
        # mismatch). Report honestly instead of recursing with a broken name.
        chain.append(LineageHop(
            start_table, start_column, source_file, kind_for_this_hop,
            transform=f"[could not resolve source table for this column - "
                      f"raw expression: {result.get('raw_sql')}]",
        ))
        return chain

    chain.append(LineageHop(
        table=start_table,
        column=start_column,
        source_file=source_file,
        kind=kind_for_this_hop,
        transform=None if result["kind"] == "TABLE" and result["raw_sql"].strip().upper()
        == f"{result['table_name']}.{result['column']}".upper() else result["raw_sql"],
        terminal_literal=(result["kind"] == "LITERAL_OR_MULTI"),
    ))

    if result["kind"] == "LITERAL_OR_MULTI":
        return chain

    if result["kind"] == "SUBQUERY":
        return resolve_column(
            registry, "<inline subquery>", result["column"], start_file,
            _select_ast=result["subquery_ast"], _current_file=source_file,
            _chain=chain, _depth=_depth + 1, _max_depth=_max_depth,
        )

    return resolve_column(
        registry, result["table_name"], result["column"], start_file,
        _current_file=source_file,
        _chain=chain, _depth=_depth + 1, _max_depth=_max_depth,
    )


def format_lineage(chain: list) -> str:
    lines = []
    for i, hop in enumerate(chain):
        prefix = "  " * i + ("-> " if i else "")
        if hop.terminal_literal:
            lines.append(f"{prefix}{hop.table}.{hop.column}  "
                          f"[FIXED/DEFAULT VALUE, not sourced from any table: {hop.transform}]")
        elif hop.kind == "BASE":
            lines.append(f"{prefix}{hop.table}.{hop.column}  [BASE TABLE - no script defines it further]")
        elif hop.table == "<inline subquery>":
            extra = f"   ({hop.transform})" if hop.transform else ""
            lines.append(f"{prefix}(inline subquery).{hop.column}  [in {hop.source_file}]{extra}")
        else:
            extra = f"   (transform: {hop.transform})" if hop.transform else ""
            lines.append(f"{prefix}{hop.table}.{hop.column}  [{hop.kind}, from {hop.source_file}]{extra}")
    return "\n".join(lines)
