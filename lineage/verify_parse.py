"""
Step 3 - Verify every cleaned statement actually parses with sqlglot.

Reads etl_lineage/cleaned_sql/*.sql, splits into individual statements,
tries sqlglot.parse_one(dialect="teradata") on EACH ONE separately (so
one bad statement doesn't hide results for the rest of the file), and
writes a report showing exactly which statements parsed, which didn't,
and why.

Run this on YOUR machine where sqlglot is actually installed:
    pip install -r requirements.txt
    python lineage/verify_parse.py
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CLEANED_DIR = BASE_DIR / "cleaned_sql"
REPORTS_DIR = BASE_DIR / "reports"

DIALECT = "teradata"


def naive_split_statements(text: str) -> list[str]:
    parts = [p.strip() for p in text.split(";")]
    return [p for p in parts if p and not p.isspace()]


def classify(parsed) -> str:
    return type(parsed).__name__.upper()


def verify_file(path: Path, report_lines: list[str]) -> dict:
    import sqlglot
    from sqlglot.errors import ParseError

    text = path.read_text(encoding="utf-8")
    statements = naive_split_statements(text)

    results = {"file": path.name, "total": len(statements), "passed": 0, "failed": 0}

    report_lines.append(f"\n=== {path.name} ({len(statements)} statement(s)) ===")

    for i, stmt in enumerate(statements):
        try:
            parsed = sqlglot.parse_one(stmt, dialect=DIALECT)
            kind = classify(parsed)
            results["passed"] += 1
            report_lines.append(f"  [OK]   stmt#{i} kind={kind}")
        except ParseError as e:
            results["failed"] += 1
            report_lines.append(f"  [FAIL] stmt#{i}: {e}")
            report_lines.append(f"         --- statement text (first 200 chars) ---")
            report_lines.append(f"         {stmt[:200].replace(chr(10), ' ')}")
        except Exception as e:  # noqa: BLE001 - want to catch and log everything here
            results["failed"] += 1
            report_lines.append(f"  [FAIL-UNEXPECTED] stmt#{i}: {type(e).__name__}: {e}")

    return results


def run():
    try:
        import sqlglot  # noqa: F401
    except ImportError:
        print("sqlglot is not installed. Run: pip install -r requirements.txt")
        return

    cleaned_files = sorted(CLEANED_DIR.glob("*.sql"))
    if not cleaned_files:
        print(f"No cleaned files found in {CLEANED_DIR}. Run lineage/preprocess.py first.")
        return

    report_lines = ["SQLGLOT PARSE VERIFICATION REPORT", f"dialect={DIALECT}"]
    summary = []
    for f in cleaned_files:
        summary.append(verify_file(f, report_lines))

    total = sum(s["total"] for s in summary)
    passed = sum(s["passed"] for s in summary)
    failed = sum(s["failed"] for s in summary)

    report_lines.insert(2, f"TOTAL: {total} statements | PASSED: {passed} | FAILED: {failed}\n")

    report_path = REPORTS_DIR / "parse_report.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    print(f"Verified {len(cleaned_files)} file(s): {passed}/{total} statements parsed OK.")
    print(f"Full report -> {report_path}")
    if failed:
        print(f"{failed} statement(s) FAILED to parse - see report for details.")


if __name__ == "__main__":
    run()
