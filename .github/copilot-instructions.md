## Purpose
This project converts transcript PDFs into a single-row-per-course CSV and contains several curriculum and planner CSVs used downstream. The goal of these instructions is to give AI coding agents the minimal, focused knowledge to be productive quickly in this repository.

## High-level architecture
- Single Python script (primary): `Step1CPRBot.py` — parses PDF text (via `PyPDF2`) and writes course rows to CSV.
- Data files at repo root: multiple curriculum CSVs (e.g., `curriculum_IE_2025plus.csv`, `plan_master.csv`), `planner_rules.json` and various elective CSVs. These are treated as static data sources.
- Output: per-transcript CSVs (script uses the same PDF basename, `.csv` output).

## Key files to inspect
- `Step1CPRBot.py` — main parser. Important symbols:
  - `HEADERS` — canonical CSV column order; do not change column names or order without updating consumers.
  - Regexes: `TERM_HEADER_RE`, `COURSE_RE`, `COURSE_TRANSFER_RE`, `INCOMING_TRANSFER_ROW_RE`, `TRANSFER_FROM_RE`, `TRANSFER_TO_TERM_RE` — changes here affect parsing behavior across PDFs.
  - Helpers: `parse_student_header`, `parse_courses`, `convert_pdf_to_csv` (GUI wrapper `main` exists).
- `planner_rules.json` — contains planner rules referenced elsewhere; treat as authoritative rules data.
- Curriculum CSVs (`curriculum_*.csv`, `plan_master.csv`, `TE_Rules.csv`) — used as lookup/reference data. Keep CSV schemas stable.

## Common workflows & commands
- Run the GUI converter (desktop):
  - `python3 Step1CPRBot.py` (opens a small Tk GUI to select a PDF)
- Run conversion programmatically (headless):
  - From REPL or another script: `from Step1CPRBot import convert_pdf_to_csv; convert_pdf_to_csv(Path('transcript.pdf'))`
- Dependencies: only `PyPDF2` is required for PDF text extraction. Pip install: `pip install PyPDF2` or add to a `requirements.txt` if you modify dependencies.

## Project-specific conventions and patterns
- Parsing is line-oriented: the code calls `page.extract_text()` and splits on lines. Expect PDF text variability; regexes are intentionally permissive.
- Transfer handling: the parser supports two flows — explicit `Transferred to Term ...` blocks and incoming transfer rows fallback. Look for `in_transfer_block` logic in `parse_courses` if modifying transfer handling.
- `plan_short` is inferred by `infer_plan_short(plan)` (e.g., contains "industrial engineering" -> `IE`). Respect this lightweight heuristic where present.
- CSV header order is authoritative (`HEADERS`); tests and downstream tools expect these exact keys.

## Guidance for edits and PRs
- If you need to change parsing rules, add a small unit test that feeds representative `lines` into `parse_courses` or `parse_student_header` and asserts expected rows. Place tests under `tests/` and use `pytest`.
- Avoid broad reformatting of `Step1CPRBot.py`. Keep regex updates isolated and add comments describing the PDF samples the expression targets.
- When adding dependencies, update a `requirements.txt` (create one if missing) instead of relying solely on README instructions.

## Error modes and edge cases (what to look for)
- PDFs with missing or malformed student header lines — check `parse_student_header` which scans first ~200 lines for name/ID/email/date.
- Term-detection mistakes — `TERM_HEADER_RE` and `term_to_code` assume term names Fall/Spring/Summer/Winter and specific year formats.
- Transfer mapping failures — if downstream rows are missing `transfer_effective_term` or `transfer_from`, inspect `in_transfer_block` logic.

## When to ask for human help
- If PDF variations produce incorrect rows after reasonable regex tweaks, provide representative PDF text samples and open an issue describing the mismatch.

If anything here is unclear or you'd like more examples (unit-test stubs, sample parsed lines, or a `requirements.txt`), tell me which part to expand.
