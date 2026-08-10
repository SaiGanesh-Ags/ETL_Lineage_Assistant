"""
Entry point. Run this after dropping your raw .sql files into raw_sql/.

    python run_pipeline.py

Does, in order:
    1. Clean every file in raw_sql/  -> cleaned_sql/  + reports/manifest.json
    2. Verify every cleaned statement parses with sqlglot -> reports/parse_report.txt
"""

from lineage import preprocess, verify_parse


def main():
    print("Step 1/2: cleaning raw scripts...")
    manifests = preprocess.run()
    if not manifests:
        return

    print("\nStep 2/2: verifying cleaned scripts parse with sqlglot...")
    verify_parse.run()


if __name__ == "__main__":
    main()
