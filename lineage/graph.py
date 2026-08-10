"""
Forward lineage - "where does this column's data flow TO downstream",
the mirror of resolve_column() (which answers "where does it come FROM").

Built entirely on top of the existing backward resolver: every backward
chain resolve_column() produces already IS a sequence of real dependency
edges (consumer.column depends on source.column). We just resolve every
output column of every registered table once, record those edges, invert
them into source -> [consumers], and cache the result. Forward lineage
then becomes a graph walk over that index, not a new SQL analysis.
"""

from lineage.resolver import resolve_column

# cached per-registry, so we only pay the resolution cost once per session
_EDGE_INDEX_CACHE: dict[int, dict] = {}


def _select_output_columns(select_ast) -> list[str]:
    cols = []
    for proj in select_ast.selects:
        name = proj.alias_or_name
        if name:
            cols.append(name)
    return cols


def build_dependency_edges(registry, verbose: bool = True, on_progress=None) -> dict:
    """
    Returns {(SOURCE_TABLE, SOURCE_COLUMN): {(CONSUMER_TABLE, CONSUMER_COLUMN), ...}}
    All keys/values upper-cased for case-insensitive matching (Teradata
    identifiers are effectively case-insensitive throughout these scripts).

    This resolves EVERY output column of EVERY registered table to build
    the index - across a large/growing codebase, some individual column's
    chain may hit an edge case the resolver doesn't handle cleanly yet.
    Rather than let one bad column crash the whole index (and therefore
    every forward-lineage question), we skip just that column/hop, log a
    warning, and keep going - the index ends up 99% complete instead of
    0% complete.

    on_progress(current_table_index, total_tables, table_name): optional
    callback fired once per table processed, so a UI (e.g. Streamlit) can
    show a real progress bar instead of an unexplained multi-second pause.
    """
    edges: dict[tuple, set] = {}
    resolved_count = 0
    skipped_count = 0

    names = registry.all_persistent_names()
    total = len(names)

    for idx, name in enumerate(names):
        if on_progress is not None:
            on_progress(idx + 1, total, name)

        definition = registry.lookup(name, current_file=None)
        if definition is None or definition.select_ast is None:
            continue
        for col in _select_output_columns(definition.select_ast):
            try:
                chain = resolve_column(registry, name, col, start_file=definition.source_file)
            except Exception as e:  # noqa: BLE001 - one bad column must not kill the whole index
                skipped_count += 1
                if verbose:
                    print(f"  [skip] {name}.{col}: {type(e).__name__}: {e}")
                continue

            # keep only real, addressable hops - drop inline-subquery hops,
            # since "<inline subquery>" isn't something a user would ask about
            real_hops = [h for h in chain if h.table != "<inline subquery>"]

            for i in range(len(real_hops) - 1):
                consumer_hop, source_hop = real_hops[i], real_hops[i + 1]
                if not consumer_hop.table or not consumer_hop.column \
                        or not source_hop.table or not source_hop.column:
                    skipped_count += 1
                    if verbose:
                        print(f"  [skip edge] {name}.{col}: malformed hop "
                              f"(consumer={consumer_hop.table}.{consumer_hop.column}, "
                              f"source={source_hop.table}.{source_hop.column}, "
                              f"kind={source_hop.kind})")
                    continue
                consumer = (consumer_hop.table.upper(), consumer_hop.column.upper())
                source = (source_hop.table.upper(), source_hop.column.upper())
                edges.setdefault(source, set()).add(consumer)

            resolved_count += 1

    if verbose:
        print(f"Forward-lineage index built: {resolved_count} column(s) resolved, "
              f"{skipped_count} skipped, {len(edges)} distinct upstream source node(s) indexed.")

    return edges


def get_or_build_edges(registry, verbose: bool = True, on_progress=None) -> dict:
    key = id(registry)
    if key not in _EDGE_INDEX_CACHE:
        _EDGE_INDEX_CACHE[key] = build_dependency_edges(registry, verbose=verbose, on_progress=on_progress)
    return _EDGE_INDEX_CACHE[key]


def forward_lineage(edges: dict, table: str, column: str, max_depth: int = 40) -> list[dict]:
    """
    BFS over the inverted edge index. Returns every (table, column) that
    transitively DEPENDS ON (table, column), each tagged with how many
    hops downstream it is (1 = directly consumes it, 2 = consumes
    something that consumes it, etc.)
    """
    start = (table.upper(), column.upper())
    visited = {start}
    results = []
    frontier = [start]
    depth = 0

    while frontier and depth < max_depth:
        depth += 1
        next_frontier = []
        for node in frontier:
            for consumer in edges.get(node, ()):
                if consumer in visited:
                    continue
                visited.add(consumer)
                results.append({"table": consumer[0], "column": consumer[1], "hops_downstream": depth})
                next_frontier.append(consumer)
        frontier = next_frontier

    return results
