"""
Step 2 - Clean raw BTEQ/Teradata scripts so sqlglot can tokenize them.

Reads:   etl_lineage/raw_sql/*.sql
Writes:  etl_lineage/cleaned_sql/*.sql   (one cleaned file per input file)
         etl_lineage/reports/manifest.json  (what was stripped/substituted, per file)

Three things are removed/rewritten:

1. BTEQ control lines - anything where the first non-whitespace char on
   the line is a dot, e.g. ".IF ERRORCODE <> 0 THEN .GOTO QUIT_KO",
   ".LOGON", ".QUIT". These are BTEQ shell commands, not SQL statements,
   and would break the tokenizer.

2. ALL "--" comments, not just decorative banners. This also covers
   trailing per-column ticket references like:
       , SO.MT_CAL_EXPO_RISQ_PNU_OF   AS MT_CAL_EXPO_RISQ_PNU_OF  --SRSK-7559
   These parse fine as far as sqlglot is concerned, but they're pure
   noise for lineage answers (a Jira ticket ID isn't a source table),
   so they're dropped during cleaning rather than filtered later.
   Comment-stripping is done character-by-character while tracking
   whether we're inside a single-quoted string literal, so a literal
   like 'A--B' is never accidentally truncated.

3. ${MACRO_NAME} placeholders, e.g. ${DB_SRC_WORK}, ${DB_SRC_STG_VUE}.
   "${" and "}" are not valid characters in Teradata SQL identifiers,
   so the tokenizer would fail on these. We rewrite ${DB_SRC_WORK} to
   plain DB_SRC_WORK - since it's always used as ${DB_SRC_WORK}.TABLE,
   stripping just the "${" "}" gives DB_SRC_WORK.TABLE, which is valid
   schema-qualified SQL AND reads exactly like the real logical
   database name when lineage answers are shown to you. No extra
   tagging/suffix needed - the manifest still records which macros
   were found per file, for auditing.
"""

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "raw_sql"
CLEANED_DIR = BASE_DIR / "cleaned_sql"
REPORTS_DIR = BASE_DIR / "reports"

BTEQ_CONTROL_LINE = re.compile(r"^[ \t]*\.\w+.*$", re.MULTILINE)
MACRO_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


@dataclass
class FileManifest:
    source_file: str
    cleaned_file: str
    macros_found: list = field(default_factory=list)
    bteq_lines_removed: int = 0
    comment_lines_stripped: int = 0
    statement_count: int = 0


def strip_bteq_lines(text: str) -> tuple[str, int]:
    matches = len(BTEQ_CONTROL_LINE.findall(text))
    return BTEQ_CONTROL_LINE.sub("", text), matches


def strip_comments_outside_strings(text: str) -> tuple[str, int]:
    """
    Remove everything from '--' to end of line, UNLESS that '--' occurs
    inside a single-quoted string literal (Teradata string quoting).
    Line-by-line, tracking quote state within each line (these scripts
    don't have multi-line string literals in the observed patterns).
    """
    out_lines = []
    stripped_count = 0
    for line in text.split("\n"):
        in_string = False
        cut_at = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_string = not in_string
                i += 1
                continue
            if not in_string and ch == "-" and i + 1 < len(line) and line[i + 1] == "-":
                cut_at = i
                break
            i += 1
        if cut_at is not None:
            stripped_count += 1
            line = line[:cut_at].rstrip()
        out_lines.append(line)
    return "\n".join(out_lines), stripped_count


def substitute_macros(text: str) -> tuple[str, list]:
    found = []

    def _repl(m: re.Match) -> str:
        name = m.group(1)
        found.append(name)
        return name  # ${DB_SRC_WORK} -> DB_SRC_WORK  (used as DB_SRC_WORK.TABLE)

    return MACRO_PATTERN.sub(_repl, text), sorted(set(found))


def naive_split_statements(text: str) -> list[str]:
    """Split on ';' terminators. Known limitation: does not account for
    ';' inside string literals. Not observed in these ETL scripts so
    far (they don't build dynamic SQL strings), but flagged here in
    case a future script does."""
    parts = [p.strip() for p in text.split(";")]
    return [p for p in parts if p and not p.isspace()]


def clean_one_file(raw_path: Path) -> FileManifest:
    raw_text = raw_path.read_text(encoding="utf-8", errors="replace")
    text, n_bteq = strip_bteq_lines(raw_text)
    text, n_comments = strip_comments_outside_strings(text)
    text, macros = substitute_macros(text)

    # collapse blank lines left behind by stripped lines, purely cosmetic
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    cleaned_path = CLEANED_DIR / raw_path.name
    cleaned_path.write_text(text, encoding="utf-8")

    stmt_count = len(naive_split_statements(text))

    return FileManifest(
        source_file=raw_path.name,
        cleaned_file=cleaned_path.name,
        macros_found=macros,
        bteq_lines_removed=n_bteq,
        comment_lines_stripped=n_comments,
        statement_count=stmt_count,
    )


def run():
    RAW_DIR.mkdir(exist_ok=True, parents=True)
    CLEANED_DIR.mkdir(exist_ok=True, parents=True)
    REPORTS_DIR.mkdir(exist_ok=True, parents=True)

    raw_files = sorted(RAW_DIR.glob("*.sql"))
    if not raw_files:
        print(f"No .sql files found in {RAW_DIR}. Put your raw scripts there first.")
        return []

    manifests = [clean_one_file(f) for f in raw_files]

    manifest_path = REPORTS_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps([asdict(m) for m in manifests], indent=2), encoding="utf-8"
    )

    print(f"Cleaned {len(manifests)} file(s) -> {CLEANED_DIR}")
    for m in manifests:
        print(
            f"  {m.source_file}: {m.statement_count} statement(s), "
            f"stripped {m.bteq_lines_removed} BTEQ line(s), "
            f"{m.comment_lines_stripped} comment(s), "
            f"macros found: {m.macros_found or 'none'}"
        )
    print(f"Manifest written -> {manifest_path}")
    return manifests


if __name__ == "__main__":
    run()
