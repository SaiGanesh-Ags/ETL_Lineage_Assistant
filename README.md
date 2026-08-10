# ETL Lineage Assistant

A chatbot that answers column-level data lineage questions over a Teradata
BTEQ SQL codebase — both **backward** ("where does this column come from?")
and **forward** ("where does this column's data flow to downstream?").

The core design principle: **the LLM never invents a lineage fact from its
own memory.** All lineage is computed deterministically by parsing your real
SQL scripts with `sqlglot`. The LLM's only job is to understand your
question, call the right tool, and phrase the tool's exact result in plain
English. Every answer traces back to real SQL you can inspect.

---

## How it works

There are two pipelines. The first prepares your scripts; the second
answers questions against what the first one built.

### 1. Preparing the scripts (run once, or whenever you add files)

```
raw_sql/*.sql  --->  preprocess.py  --->  verify_parse.py  --->  build_registry.py
  (as-is BTEQ)       strips BTEQ           tries sqlglot on         classifies each
                      control lines,        every statement,         statement, fills
                      comments, ${MACRO}    writes                   the TableRegistry
                      placeholders          reports/parse_report.txt
```

- **`lineage/preprocess.py`** — BTEQ files aren't valid SQL as-is: they
  contain control lines like `.IF ERRORCODE <> 0 THEN .GOTO QUIT_KO`,
  comment banners, trailing `--SRSK-1234` ticket references, and
  `${DB_SRC_WORK}` macro placeholders. This strips/rewrites all of that
  (quote-aware, so it never truncates a string literal containing `--`)
  and writes clean, parseable SQL to `cleaned_sql/`.
- **`lineage/verify_parse.py`** — actually tries `sqlglot.parse_one(...,
  dialect="teradata")` on every statement, one at a time, so one bad
  statement never hides results for the rest of a file. Writes
  `reports/parse_report.txt`.
- **`lineage/build_registry.py`** — classifies every parseable statement
  (`CREATE VOLATILE TABLE`, `CREATE VIEW`, `INSERT...SELECT`) and registers
  it into a `TableRegistry`. Persistent tables (always schema-qualified,
  e.g. `DB_SRC_WORK.X`) live in one global namespace; volatile tables
  (never schema-qualified) are scoped to whichever file created them, since
  two different scripts can reuse the same volatile table name.

Run both steps with:
```bash
python run_pipeline.py
```

### 2. Answering a question (every time you ask something)

```
your question  --->  LLM picks a tool  --->  resolver.py / graph.py  --->  chat reply + table
(chat.py/app.py)      get_column_lineage       walks the registry           built from the
                       or get_forward_          backward or forward          same raw JSON
                       lineage
```

- **`lineage/resolver.py`** — the core engine. `resolve_column()` walks
  *backward* from a target column: through inline subqueries, views,
  volatile tables, and other scripts' `INSERT` targets, until it hits a
  base table (nothing defines it further) or a fixed literal (e.g. `0.0`,
  flagged explicitly via `terminal_literal`).
- **`lineage/graph.py`** — *forward* lineage. Resolves every column of
  every registered table backward once, records each hop as a dependency
  edge, inverts that into "who depends on me," and answers forward
  questions with a breadth-first search over that index.
- **`lineage/llm_tools.py`** — the 3 tools the model can call
  (`get_column_lineage`, `get_forward_lineage`, `list_known_tables`), and
  the dispatcher that actually executes them against the registry.
- **`lineage/chat.py`** — the LLM tool-calling loop and system prompt
  (works with any OpenAI-compatible endpoint — Ollama, vLLM, Groq,
  Together, etc.). This is the terminal chatbot (`python -m lineage.chat`)
  and is also imported directly by `app.py`, so both interfaces share
  identical logic.
- **`app.py`** — the Streamlit browser UI. Same engine as `chat.py`; adds a
  chat view plus a deterministic table/CSV export built straight from the
  tool's raw JSON (so the table is never at the mercy of the LLM's
  phrasing).

### Debugging tools (skip the LLM entirely)

- **`lineage/ask.py`** — ask a backward question directly from the CLI.
- **`lineage/ask_forward.py`** — ask a forward question directly from the CLI.
- **`lineage/debug_column.py`** — dumps the raw alias/table-matching
  internals for one column, for diagnosing "why didn't this resolve".

---

## Setup

```bash
pip install -r requirements.txt
```

Point the chatbot at your LLM (any OpenAI-compatible endpoint):

```bash
# Windows (cmd)
set LLM_BASE_URL=http://localhost:11434/v1
set LLM_API_KEY=ollama
set LLM_MODEL=gpt-oss:120b

# macOS/Linux
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_API_KEY=ollama
export LLM_MODEL=gpt-oss:120b
```
(Defaults to a local Ollama endpoint if you don't set these.)

---

## Usage

**1. Add your scripts and prepare them** (do this first, and again any
time you add/change files in `raw_sql/`):
```bash
# copy your .sql files into raw_sql/
python run_pipeline.py
```
Check `reports/parse_report.txt` for any `[FAIL]` lines before moving on.

**2. Chat in the terminal:**
```bash
python -m lineage.chat
> where does CD_CLA_EXP come from in EXP_ARC_EFPB3
> what uses CD_PROFIL_ALIM from WRK_ARRIMAGE_SRSK_ARC_EXP downstream
> list tables
> quit
```

**3. Or use the browser UI:**
```bash
streamlit run app.py
```

**4. Or skip the LLM and query directly:**
```bash
python -m lineage.ask --table DB_SRC_WORK.EXP_ARC_EFPB3 --column CD_CLA_EXP
python -m lineage.ask_forward --table DB_SRC_WORK.WRK_ARRIMAGE_SRSK_ARC_EXP --column CD_PROFIL_ALIM
```

---

## Trying it with the example scripts

`examples/` contains 4 small, self-contained BTEQ scripts covering every
pattern the resolver understands — a view, a plain `INSERT...SELECT`, a
nested subquery pulling from a view, and a volatile table joined into an
`INSERT`. They're reconstructed/illustrative demo fixtures, not real
production data, so they're safe to share.

```bash
copy examples\*.sql raw_sql\        (Windows)
cp examples/*.sql raw_sql/          (macOS/Linux)
python run_pipeline.py
python -m lineage.chat
> where does CD_CLA_EXP come from in EXP_ARC_EFPB3
```
Expected chain: `EXP_ARC_EFPB3.CD_CLA_EXP` → inline subquery → the view
`V_COUR_SRS_O_ARC.CD_CLA_EXP` → base table (no further script defines it).

---

## Known limitations / things to know

- **Statement splitting is naive** (`text.split(";")`) — doesn't account
  for `;` inside string literals. Not an issue in scripts observed so far.
- **Forward lineage only knows about loaded scripts.** An empty result
  means "nothing loaded consumes this," not "this is unused system-wide."
- **You must re-run `run_pipeline.py` after adding files to `raw_sql/`** —
  neither `chat.py` nor `app.py` re-cleans automatically; they only read
  from `cleaned_sql/`.
- **Volatile tables are scoped per-file** by design — the same volatile
  table name in two different scripts is treated as two unrelated tables.

## Possible next steps

- Wider coverage testing against the full real script folder (dozens+ of
  files) to catch any remaining `sqlglot`/Teradata dialect edge cases.
- A "why not found" explainer when a column name is close-but-not-exact.
- Caching `cleaned_sql/`/registry state to disk so large codebases don't
  need a full re-parse every process restart.
