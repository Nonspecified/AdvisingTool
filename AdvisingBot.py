# AdvisingBot.py — One-click transcript PDF → HTML curriculum map
# Select a PDF, all three steps run automatically, browser opens with result.
# pip install PyPDF2 pandas
from __future__ import annotations

import csv
import re
import os
import sys
import json
import webbrowser
import threading
from pathlib import Path
from datetime import datetime, date
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False

# Prevent host Python/Conda env leakage from breaking frozen pandas imports.
os.environ.pop("_PYTHON_SYSCONFIGDATA_NAME", None)
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)

from PyPDF2 import PdfReader
import pandas as pd


def _resource_path(relative: str) -> Path:
    """Resolve a data-file path whether running from source or PyInstaller bundle."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED CONSTANTS & HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

# Step 1 CSV output columns
TRANSCRIPT_HEADERS = [
    "course_id", "course_name", "term", "term_code", "grade", "status",
    "attempted_credits", "earned_credits", "grade_points",
    "is_transfer", "transfer_from", "transfer_effective_term", "transfer_effective_date",
    "program", "plan", "plan_short",
    "full_name", "first_name", "last_name", "student_id", "email",
    "transcript_date", "source_file", "cum_gpa",
]

# Step 2 grade / status tables
PASS_LETTERS  = {"A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "P", "S", "T", "CR"}
PASS_SPECIAL  = {"P", "S", "T", "CR"}
STATUS_PRIO   = {"passed": 4, "transfer": 4, "in_progress": 3, "completed": 3,
                 "failed": 2, "withdrawn": 1, "unknown": 0}
TC_ORDER      = {"WI": 1, "SP": 2, "SU": 3, "FA": 4}
GRADE_SCORE   = {"A": 13, "A-": 12, "B+": 11, "B": 10, "B-": 9,
                 "C+": 8, "C": 7, "C-": 6, "D+": 5, "D": 4, "D-": 3, "F": 0, "W": -1}

# HTML output constants
YEAR_LABELS      = {1: "Freshman Year", 2: "Sophomore Year", 3: "Junior Year", 4: "Senior Year"}
TERM_ORDER_HTML  = ["Y1F", "Y1S", "Y2F", "Y2S", "Y3F", "Y3S", "Y4F", "Y4S"]
SEM_LABELS       = {"F": "Fall", "S": "Spring"}
ELECTIVE_BUCKETS = {"GENED_AH", "GENED_SS", "TechElective"}

# Course equivalencies: modern label should be used in curriculum display,
# but any course in the group can satisfy requirements/prereqs.
COURSE_EQUIV_GROUPS = [
    {"MECH 3220", "MECH 3230"},
]


# ── shared utility functions ──────────────────────────────────────────────────

def norm_id(s: str) -> str:
    """Normalize a course ID to uppercase, single-spaced, no dots."""
    t = str(s or "").upper().replace(".", " ").strip()
    t = re.sub(r"\s+", " ", t)
    # Collapse "CHEM 1230 L" → "CHEM 1230L" (PDF sometimes splits suffix with space)
    t = re.sub(r"(\d{3,4})\s+([A-Z])$", r"\1\2", t)
    t = re.sub(r"^([A-Z&]+)\s*([0-9][0-9A-Z]*)$", r"\1 \2", t)
    return t


_GTOK_RE = re.compile(r"\b(A-?|B\+?|B-?|C\+?|C-?|D\+?|D-?|F|W|P|S|T|CR)\b", re.I)


def _clean_grade_token(s) -> str:
    s = "" if s is None else str(s)
    m = _GTOK_RE.search(s)
    return m.group(0).upper() if m else ""


def grade_meets_min(grade: str, min_grade: str) -> bool:
    g = _clean_grade_token(grade)
    m = _clean_grade_token(min_grade)
    if not g:
        return False
    if not m:
        return g in PASS_LETTERS or g in PASS_SPECIAL
    if g in PASS_SPECIAL:
        return True
    if g not in GRADE_SCORE or m not in GRADE_SCORE:
        return False
    return GRADE_SCORE[g] >= GRADE_SCORE[m]


# Regex for extracting course IDs from prereq/coreq text
PREREQ_COURSE_RE = re.compile(r"\b([A-Z]{2,}\.? ?\d{3,4}[A-Z]?)\b")


def extract_course_ids(text: str) -> list:
    if not isinstance(text, str) or not text.strip():
        return []
    return [norm_id(m.group(1)) for m in PREREQ_COURSE_RE.finditer(text.upper())]


def css_id(course_id: str) -> str:
    return "c-" + re.sub(r"[^A-Za-z0-9]", "-", str(course_id)).strip("-")


def equiv_ids(course_id_norm: str) -> set[str]:
    cid = norm_id(course_id_norm)
    if not cid:
        return set()
    for grp in COURSE_EQUIV_GROUPS:
        if cid in grp:
            return set(grp)
    return {cid}


def expand_equiv_ids(course_ids) -> set[str]:
    expanded = set()
    for cid in (course_ids or []):
        expanded |= equiv_ids(cid)
    return expanded


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — PDF → CSV
# ═══════════════════════════════════════════════════════════════════════════════

TERM_HEADER_RE = re.compile(r"^\s*(\d{4})\s+(Fall|Spring|Summer|Winter)\s*$", re.I)
PROGRAM_RE     = re.compile(r"^\s*Program:\s*(?P<program>.+?)\s*$", re.I)
PLAN_RE        = re.compile(r"^\s*Plan:\s*(?P<plan>.+?)\s*$", re.I)

TRANSCRIPT_COURSE_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}\s*[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<earned>\d+\.\d{2})"
    r"(?:\s+(?P<grade>(?:A|B|C|D|F|P|S|U|IP|I|W)(?:[+-])?))?"
    r"\s+(?P<points>\d+\.\d{3})\s*$"
)
COURSE_TRANSFER_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}\s*[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<grade>(?:T|A|B|C|D|F|P|S|U)(?:[+-]?))"
    r"(?:\s+.*)?$"
)
INCOMING_TRANSFER_ROW_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}\s*[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<grade>(?:T|A|B|C|D|F|P|S|U)(?:[+-])?)"
    r"(?:\s+.*)?$"
)
TRANSFER_FROM_RE    = re.compile(r"^Transfer Credit from\s+(?P<inst>.+?)\s*$", re.I)
TRANSFER_TO_TERM_RE = re.compile(
    r"^Transferred\s+to\s+Term\s+(?P<year>\d{4})\s+(?P<term>Fall|Spring|Summer|Winter)\s+as\s*$",
    re.I,
)
REPEAT_FLAG_RE  = re.compile(r"^Repeated:", re.I)
NAME_RE         = re.compile(r"^\s*Name:\s*(?P<name>.+?)\s*$", re.I)
STUDENT_ID_RE   = re.compile(r"^\s*Student ID:\s*(?P<sid>.+?)\s*$", re.I)
EMAIL_INLINE_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
DATE_LINE_RE    = re.compile(r"^\s*(?P<mdy>\d{1,2}/\d{1,2}/\d{4})\s*$")

SKIP_LINE_HINTS = (
    "Course Description Attempted Earned Grade Points",
    "Attempted Earned GPA", "UnitsPoints",
    "Term GPA:", "Cum GPA:", "Undergraduate Career Totals",
    "End of", "Beginning of Undergraduate Record",
    "Incoming  Course",
)


def _term_to_code(year: int, term: str) -> str:
    mm = {"Spring": "SP", "Summer": "SU", "Fall": "FA", "Winter": "WI"}
    return f"{year}{mm[term]}"


def _term_start_date(year: int, term: str) -> date:
    month = {"Spring": 1, "Summer": 5, "Fall": 9, "Winter": 12}[term]
    return date(year, month, 1)


def _classify_status(grade, points, is_latest_term: bool) -> str:
    if grade:
        g = grade.upper()
        if g == "W":
            return "withdrawn"
        if g == "T":
            return "transfer"
        if g in {"IP", "I"}:
            return "in_progress"
        return "completed"
    if is_latest_term:
        return "in_progress"
    return "completed" if (points and float(points) > 0.0) else "in_progress"


def _infer_plan_short(plan: str) -> str:
    p = plan.lower() if plan else ""
    if "industrial engineering" in p:
        return "IE"
    if "mechanical engineering" in p:
        return "ME"
    return "OTHER" if p else ""


def _extract_pdf_text(pdf_path: Path) -> list:
    reader = PdfReader(str(pdf_path))
    lines = []
    for page in reader.pages:
        t = page.extract_text() or ""
        lines.extend([ln.rstrip() for ln in t.splitlines()])
    return lines


_CUM_GPA_RE = re.compile(r"Cum\s+GPA:\s*([\d.]+)", re.I)


def _parse_student_header(lines: list, source_file: str) -> dict:
    full_name = student_id = email = tdate_iso = ""
    for ln in lines[:200]:
        if TERM_HEADER_RE.match(ln) or ln.strip() == "Transfer Credits":
            break
        m = NAME_RE.match(ln)
        if m:
            full_name = m.group("name").strip()
        m = STUDENT_ID_RE.match(ln)
        if m:
            student_id = m.group("sid").strip()
        if not email:
            em = EMAIL_INLINE_RE.search(ln)
            if em:
                email = em.group(0)
        if not tdate_iso:
            dm = DATE_LINE_RE.match(ln)
            if dm:
                try:
                    tdate_iso = datetime.strptime(dm.group("mdy"), "%m/%d/%Y").date().isoformat()
                except ValueError:
                    pass

    # Scan ALL lines for the last Cum GPA (Undergraduate Career Totals appears near end)
    cum_gpa = ""
    for ln in lines:
        m = _CUM_GPA_RE.search(ln)
        if m:
            cum_gpa = m.group(1)

    first_name = last_name = ""
    if full_name:
        parts = [p for p in full_name.replace(",", " ").split() if p]
        if len(parts) >= 2:
            first_name, last_name = parts[0], parts[-1]
        elif parts:
            first_name = parts[0]

    return {
        "full_name": full_name, "first_name": first_name, "last_name": last_name,
        "student_id": student_id, "email": email,
        "transcript_date": tdate_iso, "source_file": source_file,
        "cum_gpa": cum_gpa,
    }


def _parse_courses(lines: list) -> list:
    rows = []
    current_term = current_term_code = None
    term_positions = [i for i, ln in enumerate(lines) if TERM_HEADER_RE.match(ln)]
    latest_term_index = term_positions[-1] if term_positions else -1

    in_transfer_block = False
    transfer_from = transfer_effective_term = transfer_effective_date = ""
    pending_transfer_target = False
    current_program = current_plan = ""

    for idx, ln in enumerate(lines):
        if not ln:
            continue
        if ln.strip() == "Transfer Credits":
            in_transfer_block = True
            continue

        mh = TERM_HEADER_RE.match(ln)
        if mh:
            year = int(mh.group(1))
            term = mh.group(2).title()
            current_term      = f"{year} {term}"
            current_term_code = _term_to_code(year, term)
            in_transfer_block = False
            transfer_from     = ""
            pending_transfer_target = False
            current_program   = current_plan = ""
            continue

        mprog = PROGRAM_RE.match(ln)
        if mprog:
            current_program = mprog.group("program").strip()
            continue
        mplan = PLAN_RE.match(ln)
        if mplan:
            current_plan = mplan.group("plan").strip()
            continue
        # PyPDF2 sometimes concatenates a course row with the next column/page
        # header (e.g. "CHEM 1230L ... 3.000Course Description Attempted…").
        # Strip any SKIP_LINE_HINT that appears after real content so the
        # course data at the front of the line is still parsed.
        for _h in SKIP_LINE_HINTS:
            _pos = ln.find(_h)
            if _pos > 0:          # hint is a suffix/middle, not the whole line
                ln = ln[:_pos].rstrip()
                break
        if any(h in ln for h in SKIP_LINE_HINTS):
            continue

        if in_transfer_block:
            mfrom = TRANSFER_FROM_RE.match(ln)
            if mfrom:
                transfer_from = mfrom.group("inst").strip()
                continue
            mto = TRANSFER_TO_TERM_RE.match(ln)
            if mto:
                y = int(mto.group("year"))
                t = mto.group("term").title()
                transfer_effective_term = f"{y} {t}"
                transfer_effective_date = _term_start_date(y, t).isoformat()
                pending_transfer_target = True
                continue
            if pending_transfer_target:
                mtc = COURSE_TRANSFER_RE.match(ln)
                if mtc:
                    y, t = transfer_effective_term.split()
                    rows.append({
                        "course_id": mtc.group("course_id").strip(),
                        "course_name": mtc.group("course_name").strip(),
                        "term": transfer_effective_term,
                        "term_code": _term_to_code(int(y), t),
                        "grade": mtc.group("grade").strip(),
                        "status": "transfer",
                        "attempted_credits": mtc.group("attempted").strip(),
                        "earned_credits": mtc.group("attempted").strip(),
                        "grade_points": "", "is_transfer": "1",
                        "transfer_from": transfer_from,
                        "transfer_effective_term": transfer_effective_term,
                        "transfer_effective_date": transfer_effective_date,
                        "program": "", "plan": "", "plan_short": "",
                    })
                # Always clear the flag after one attempt — the UML equivalent
                # is always the very next non-blank line after "Transferred to Term … as".
                pending_transfer_target = False
                continue
            minc = INCOMING_TRANSFER_ROW_RE.match(ln)
            if minc:
                rows.append({
                    "course_id": minc.group("course_id").strip(),
                    "course_name": minc.group("course_name").strip(),
                    "term": "Transfer", "term_code": "TR",
                    "grade": minc.group("grade").strip(),
                    "status": "transfer",
                    "attempted_credits": minc.group("attempted").strip(),
                    "earned_credits": minc.group("attempted").strip(),
                    "grade_points": "", "is_transfer": "1",
                    "transfer_from": transfer_from,
                    "transfer_effective_term": "Unknown",
                    "transfer_effective_date": "",
                    "program": "", "plan": "", "plan_short": "",
                })
            continue

        mc = TRANSCRIPT_COURSE_RE.match(ln)
        if mc and current_term:
            grade  = (mc.group("grade") or "").strip()
            points = mc.group("points").strip()
            prec   = [p for p in term_positions if p <= idx]
            is_lat = bool(prec and prec[-1] == latest_term_index)
            rows.append({
                "course_id": mc.group("course_id").strip(),
                "course_name": mc.group("course_name").strip(),
                "term": current_term, "term_code": current_term_code,
                "grade": grade,
                "status": _classify_status(grade or None, points or None, is_lat),
                "attempted_credits": mc.group("attempted").strip(),
                "earned_credits": mc.group("earned").strip(),
                "grade_points": points, "is_transfer": "0",
                "transfer_from": "", "transfer_effective_term": "",
                "transfer_effective_date": "",
                "program": current_program, "plan": current_plan,
                "plan_short": _infer_plan_short(current_plan) if current_plan else "",
            })
        elif REPEAT_FLAG_RE.search(ln):
            continue

    return rows


def convert_pdf_to_csv(pdf_path: Path) -> Path:
    """Parse a transcript PDF and write a CSV. Returns the CSV path."""
    lines       = _extract_pdf_text(pdf_path)
    student_meta = _parse_student_header(lines, source_file=str(pdf_path.name))
    rows        = _parse_courses(lines)
    out_path    = pdf_path.with_suffix(".csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRANSCRIPT_HEADERS)
        w.writeheader()
        for r in rows:
            r_out = r.copy()
            r_out.update(student_meta)
            w.writerow({k: r_out.get(k, "") for k in TRANSCRIPT_HEADERS})
    return out_path


# ── advising report text parsing constants ────────────────────────────────────
_AR_NOISE = frozenset({
    "*** view multiple offerings", "Table Pagination",
    "First", "Previous", "Next", "Last", "Table Options",
    "View All", "View 100", "Collapse All", "Expand All",
    "View Course Details", "View Course List",
})
_AR_COURSE_ID_RE  = re.compile(r"^([A-Z]{2,5})\s{1,2}(\d{3,4}[A-Z]?)$")
_AR_UNITS_RE      = re.compile(r"^\d+\.\d{2}$")
_AR_TERM_RE       = re.compile(r"^(\d{4})\s+(Fall|Spring|Summer|Winter)$", re.I)
_AR_GRADE_RE      = re.compile(r"^(A[+-]?|B[+-]?|C[+-]?|D[+-]?|F|W|P|S|T|CR)$", re.I)
_AR_PAGINATION_RE = re.compile(r"^\d+-\d+ of \d+$")
_AR_TERM_SUFFIX   = {"fall": "FA", "spring": "SP", "summer": "SU", "winter": "WI"}
_AR_HEADER_RE     = re.compile(r"Advisee Requirements\s*(.+?)\s*(?:\([^)]*\))?\s*University", re.I)
_AR_STUDENT_ID_RE = re.compile(r"Advisee Requirements.*?\((\d{6,9})\)", re.I)
_AR_GPA_RE        = re.compile(r"GPA:.*?([\d.]+)\s+actual", re.I)


def parse_advising_report_text(text: str, out_path: Path) -> Path:
    """Parse a copied UML Advisee Requirements page into a transcript CSV.

    The user must click 'Expand All' on the page before copying so that all
    individual requirement-satisfaction rows are visible.  Returns out_path.
    """
    # ── clean lines ───────────────────────────────────────────────────────────
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s in _AR_NOISE or _AR_PAGINATION_RE.match(s):
            continue
        lines.append(s)

    # ── extract student header & GPA ──────────────────────────────────────────
    full_name = student_id = cum_gpa = ""
    header_text = "\n".join(lines[:10])
    m = _AR_HEADER_RE.search(header_text)
    if m:
        full_name = m.group(1).strip()
    m2 = _AR_STUDENT_ID_RE.search(header_text)
    if m2:
        student_id = m2.group(1).strip()
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    for ln in lines:
        m = _AR_GPA_RE.search(ln)
        if m:
            cum_gpa = m.group(1)
            break

    # ── detect major from section headers ─────────────────────────────────────
    text_lower = text.lower()
    if "mechanical engineering" in text_lower:
        plan, plan_short = "Mechanical Engineering BSE", "ME"
    elif "industrial engineering" in text_lower:
        plan, plan_short = "Industrial Engineering BSE", "IE"
    else:
        plan, plan_short = "", ""

    # ── state machine: collect course rows ───────────────────────────────────
    # After stripping blank/noise lines, each taken/enrolled course appears as
    # a contiguous sequence:  ID · Name · Units · Term · [Grade] · Status
    course_rows: list[dict] = []
    state = 0
    cid = name = units = term = term_code = grade = ""

    for ln in lines:
        if state == 0:
            if _AR_COURSE_ID_RE.match(ln):
                cid = ln
                state = 1

        elif state == 1:          # course_id found → next line = name
            name  = ln
            state = 2

        elif state == 2:          # name found → look for units
            if _AR_UNITS_RE.match(ln):
                units = ln
                state = 3
            elif _AR_COURSE_ID_RE.match(ln):
                cid = ln; state = 1
            else:
                state = 0

        elif state == 3:          # units found → look for term
            m = _AR_TERM_RE.match(ln)
            if m:
                term      = ln
                term_code = m.group(1) + _AR_TERM_SUFFIX[m.group(2).lower()]
                state     = 4
            elif _AR_COURSE_ID_RE.match(ln):
                cid = ln; state = 1
            else:
                state = 0

        elif state == 4:          # term found → look for grade or "Enrolled"
            if ln == "Enrolled":
                course_rows.append({"course_id": norm_id(cid), "course_name": name,
                                    "term": term, "term_code": term_code,
                                    "grade": "", "status_token": "Enrolled", "units": units})
                state = 0
            elif _AR_GRADE_RE.match(ln):
                grade = ln.upper()
                state = 5
            elif _AR_COURSE_ID_RE.match(ln):
                cid = ln; state = 1
            else:
                state = 0

        elif state == 5:          # grade found → look for "Taken"
            if ln == "Taken":
                course_rows.append({"course_id": norm_id(cid), "course_name": name,
                                    "term": term, "term_code": term_code,
                                    "grade": grade, "status_token": "Taken", "units": units})
                state = 0
            elif _AR_COURSE_ID_RE.match(ln):
                cid = ln; state = 1
            else:
                state = 0

    # ── deduplicate: same (course_id, term_code) → prefer Taken over Enrolled ─
    seen: dict[tuple, dict] = {}
    for row in course_rows:
        key = (row["course_id"], row["term_code"])
        existing = seen.get(key)
        if existing is None or (existing["status_token"] == "Enrolled" and row["status_token"] == "Taken"):
            seen[key] = row

    # ── convert to transcript CSV rows ────────────────────────────────────────
    def _to_csv_row(row: dict) -> dict:
        g, tok, u = row["grade"], row["status_token"], row["units"]
        if tok == "Enrolled":
            status, is_xfer, earned = "in_progress", "0", "0.00"
        elif g == "T":
            status, is_xfer, earned = "transfer", "1", u
        elif g == "W":
            status, is_xfer, earned = "withdrawn", "0", "0.00"
        elif g == "F":
            status, is_xfer, earned = "failed", "0", "0.00"
        else:
            status, is_xfer, earned = "passed", "0", u
        return {
            "course_id": row["course_id"], "course_name": row["course_name"],
            "term": row["term"], "term_code": row["term_code"],
            "grade": g, "status": status,
            "attempted_credits": u, "earned_credits": earned, "grade_points": "",
            "is_transfer": is_xfer,
            "transfer_from": "Transfer" if is_xfer == "1" else "",
            "transfer_effective_term": "", "transfer_effective_date": "",
            "program": plan, "plan": plan, "plan_short": plan_short,
            "full_name": full_name, "first_name": first_name, "last_name": last_name,
            "student_id": student_id, "email": "",
            "transcript_date": "", "source_file": "pasted_report", "cum_gpa": cum_gpa,
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRANSCRIPT_HEADERS)
        w.writeheader()
        for row in seen.values():
            w.writerow(_to_csv_row(row))
    return out_path


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — CSV → filled_pathway.csv
# ═══════════════════════════════════════════════════════════════════════════════

def _term_sort_key(tc: str):
    """Sort key for term codes like '2025FA' → (2025, 4)."""
    if not isinstance(tc, str) or len(tc) < 6:
        return (9999, 9)
    y = int(tc[:4])
    t = TC_ORDER.get(tc[4:].upper(), 9)
    return (y, t)


def _attempt_status(row) -> str:
    st = str(row.get("status", "")).lower().strip()
    g  = _clean_grade_token(row.get("grade", ""))
    if st == "transfer" or g == "T":
        return "passed"
    if st in {"withdrawn", "w"} or g == "W":
        return "withdrawn"
    if st in {"in_progress", "ip", "inprogress"}:
        return "in_progress"
    if g in PASS_LETTERS:
        return "passed"
    if g:
        return "failed"
    return "unknown"


def _latest_by_term(df: pd.DataFrame):
    if df.empty:
        return pd.Series({})
    return df.sort_values("term_code", key=lambda s: s.astype(str).map(_term_sort_key)).iloc[-1]


def _first_class_year(df: pd.DataFrame):
    if "term_code" not in df.columns:
        return None
    is_tr = pd.Series(False, index=df.index)
    if "is_transfer" in df.columns:
        is_tr |= df["is_transfer"].astype(str).str.lower().isin({"1", "true", "t"})
    if "status" in df.columns:
        is_tr |= df["status"].astype(str).str.lower().eq("transfer")
    pool = df.loc[~is_tr, "term_code"].dropna().astype(str)
    if pool.empty:
        pool = df["term_code"].dropna().astype(str)
    if pool.empty:
        return None
    return int(sorted(pool, key=_term_sort_key)[0][:4])


_REGISTRY_CACHE = None

def _load_registry() -> dict:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    p = _resource_path("curricula_registry.json")
    if p.exists():
        with open(p, encoding="utf-8") as f:
            _REGISTRY_CACHE = json.load(f)
    else:
        # Built-in fallback so the app works without the JSON
        _REGISTRY_CACHE = {
            "ME": {"plan_patterns": ["mechanical engineering", "mech eng"],
                   "plan_short_values": ["ME"], "has_te": True,
                   "variants": ["pre2025", "2025plus"], "variant_cutoff_year": 2025},
            "IE": {"plan_patterns": ["industrial engineering", "indust eng"],
                   "plan_short_values": ["IE"], "has_te": True,
                   "variants": ["pre2025", "2025plus"], "variant_cutoff_year": 2025},
        }
    return _REGISTRY_CACHE


def _infer_major(df: pd.DataFrame) -> str:
    reg = _load_registry()
    # Check plan_short first (exact match)
    if "plan_short" in df.columns:
        vals = df["plan_short"].dropna().astype(str).str.upper()
        for key, info in reg.items():
            for short in info.get("plan_short_values", [key]):
                if (vals == short.upper()).any():
                    return key
    # Fall back to plan text contains
    plan_vals = df.get("plan", pd.Series(dtype=str)).dropna().astype(str).str.lower()
    for key, info in reg.items():
        for pat in info.get("plan_patterns", []):
            if plan_vals.str.contains(pat.lower(), regex=False).any():
                return key
    return "UNKNOWN"


def _detect_track(major: str, first_year) -> str:
    reg = _load_registry()
    info = reg.get(major)
    if not info:
        return "UNKNOWN"
    variants = info.get("variants", ["default"])
    cutoff   = info.get("variant_cutoff_year")
    if cutoff and len(variants) >= 2 and first_year is not None:
        period = variants[1] if first_year >= cutoff else variants[0]
    else:
        period = variants[0]
    return f"{major}_{period}"


_MINOR_RE = re.compile(
    r"(?:([A-Za-z][\w ]+?)\s+[Mm]inor|[Mm]inor\s+in\s+([A-Za-z][\w ]+))"
)


def _infer_minor(df: pd.DataFrame):
    """Detect a minor from plan/program columns.
    Returns (code, display_name) or None.  code is used for minor_XX.csv lookup."""
    for col in ["plan", "program", "plan_short"]:
        if col not in df.columns:
            continue
        for val in df[col].dropna().astype(str):
            m = _MINOR_RE.search(val)
            if m:
                name = (m.group(1) or m.group(2) or "").strip().title()
                if len(name) < 2:
                    continue
                # Derive a short file-safe code: uppercase letters/digits only, max 12 chars
                code = re.sub(r"[^A-Z0-9]", "", name.upper())[:12]
                if not code:
                    code = "MINOR"
                return (code, name)
    return None


def _parse_pool_req(req_str):
    """Parse pool_requirement like '2 courses' or '9 credits'. Returns (type, count)."""
    s = str(req_str or "").strip().lower()
    m = re.match(r"(\d+)\s*(course|credit)", s)
    if m:
        return (m.group(2) + "s", int(m.group(1)))
    return ("courses", 1)


def _process_minor(minor_csv_path, tx: pd.DataFrame, ever_met_min: set,
                   minor_code: str, minor_display_name: str, track: str) -> list:
    """Match student courses to a minor CSV. Returns list of row dicts."""
    mc = pd.read_csv(str(minor_csv_path), engine="python")
    for col in ["slot_type", "course_id", "course_name", "credits",
                "pool_id", "pool_label", "pool_requirement", "prereq", "coreq", "min_grade", "notes"]:
        if col not in mc.columns:
            mc[col] = ""
    mc["course_id_norm"] = mc["course_id"].astype(str).map(norm_id)

    tx = tx.copy()
    if "course_id_norm" not in tx.columns:
        tx["course_id_norm"] = tx["course_id"].map(norm_id)
    if "grade_tok" not in tx.columns:
        tx["grade_tok"] = tx["grade"].map(_clean_grade_token)
    if "attempt_status" not in tx.columns:
        tx["attempt_status"] = tx.apply(_attempt_status, axis=1)

    # Pre-compute pool stats: for each pool_id, how many student courses are satisfied
    pool_stats = {}  # pool_id -> {"done": int, "needed": int, "count_type": str, "label": str}
    for pool_id, grp in mc[mc["slot_type"].str.lower().str.strip() == "pool"].groupby("pool_id"):
        ct, cn = _parse_pool_req(grp["pool_requirement"].iloc[0])
        pool_cids = set(grp["course_id_norm"])
        taken = tx[tx["course_id_norm"].isin(pool_cids) &
                   tx["attempt_status"].isin(["passed", "in_progress", "transfer"])]
        if ct == "credits":
            done_val = tx.loc[
                tx["course_id_norm"].isin(pool_cids) &
                tx["attempt_status"].isin(["passed", "transfer"]),
                "earned_credits"
            ].fillna(0).astype(float).sum()
        else:
            done_val = len(taken["course_id_norm"].unique())
        label_val = str(grp["pool_label"].iloc[0]).strip()
        pool_stats[pool_id] = {"done": done_val, "needed": cn, "count_type": ct,
                               "total_avail": len(pool_cids), "label": label_val}

    out_rows = []
    for _, row in mc.iterrows():
        slot_type = str(row.get("slot_type", "required")).lower().strip() or "required"
        cidn      = row["course_id_norm"]
        pool_id   = str(row.get("pool_id", "")).strip()

        att = tx[tx["course_id_norm"] == cidn].copy()
        match_grade = match_term = match_cid = match_cname = match_status = ""
        meets_min = ""
        if not att.empty:
            meets = att[att["grade_tok"].map(
                lambda g: grade_meets_min(g, str(row.get("min_grade", ""))))]
            chosen = _latest_by_term(meets) if not meets.empty else _latest_by_term(att)
            if not chosen.empty:
                match_cid    = str(chosen.get("course_id", ""))
                match_cname  = str(chosen.get("course_name", ""))
                match_term   = str(chosen.get("term_code", ""))
                match_grade  = str(chosen.get("grade", ""))
                ok           = grade_meets_min(match_grade, str(row.get("min_grade", "")))
                match_status = chosen.get("attempt_status", "unknown") if ok else "below_min_grade"
                meets_min    = "Y" if ok else "N"

        pre = str(row.get("prereq", "")).upper().strip()
        prereqs_ok = True
        if pre:
            found_pres = re.findall(r"[A-Z]{2,}\.? ?\d{3,4}", pre)
            if found_pres:
                prereqs_ok = all(norm_id(c) in ever_met_min for c in found_pres)

        ps = pool_stats.get(pool_id, {})
        filled = {
            "term_id": "MINOR", "term_label": f"{minor_display_name} Minor",
            "slot_course_id":   str(row.get("course_id", "")),
            "slot_course_name": str(row.get("course_name", "")),
            "credits":          str(row.get("credits", "")),
            "bucket":           f"Minor_{minor_code}",
            "prereq":           str(row.get("prereq", "")),
            "coreq":            str(row.get("coreq", "")),
            "min_grade":        str(row.get("min_grade", "")),
            "notes":            str(row.get("notes", "")),
            "minor_slot_type":       slot_type,
            "minor_pool_id":         pool_id,
            "minor_pool_label":      str(row.get("pool_label", "")).strip(),
            "minor_pool_requirement": str(row.get("pool_requirement", "")),
            "minor_pool_slots_done":   ps.get("done", 0),
            "minor_pool_slots_needed": ps.get("needed", 1),
            "minor_pool_count_type":   ps.get("count_type", "courses"),
            "minor_pool_total_avail":  ps.get("total_avail", 0),
            "match_course_id":   match_cid,
            "match_course_name": match_cname,
            "match_term_code":   match_term,
            "match_grade":       match_grade,
            "match_status":      match_status,
            "meets_min_grade":   meets_min,
            "prereqs_met_flag":  "Y" if prereqs_ok else "N",
            "source_track":      track,
        }

        has_grade = match_grade.strip() not in ("", "nan")
        has_term  = match_term.strip() not in ("", "nan")
        if has_term and not has_grade:
            filled["viz_status"] = "blue"
        elif meets_min == "Y" or match_status in ("passed", "transfer"):
            filled["viz_status"] = "green"
        elif match_status == "below_min_grade":
            filled["viz_status"] = "yellow"
        elif prereqs_ok:
            filled["viz_status"] = "yellow"
        else:
            filled["viz_status"] = "grey"

        out_rows.append(filled)
    return out_rows


def _best_attempts(tx: pd.DataFrame) -> pd.DataFrame:
    df = tx.copy()
    df["course_id_norm"] = df["course_id"].map(norm_id)
    if "attempt_status" not in df.columns:
        df["attempt_status"] = df.apply(_attempt_status, axis=1)
    # Count failed/withdrawn attempts per course before deduplicating
    fail_mask = df["attempt_status"].isin({"failed", "withdrawn"})
    fail_rows = df[fail_mask].copy()
    fail_counts = fail_rows.groupby("course_id_norm").size().rename("prior_fail_count")
    # Build per-course list of failed attempt records (grade + term) for tooltips
    def _fail_records(g):
        recs = []
        for _, r in g.sort_values("term_code", key=lambda s: s.astype(str).map(_term_sort_key)).iterrows():
            grade = str(r.get("grade", "")).strip()
            term  = str(r.get("term",  "")).strip()
            if not grade or grade.lower() in ("nan", "none"):
                grade = "W"
            recs.append({"grade": grade, "term": term})
        return json.dumps(recs[:4])
    if fail_rows.empty:
        fail_records = pd.Series(dtype="object", name="prior_fail_records")
    else:
        try:
            fail_records = fail_rows.groupby("course_id_norm").apply(
                _fail_records, include_groups=False
            )
        except TypeError:
            fail_records = fail_rows.groupby("course_id_norm").apply(_fail_records)
        fail_records.name = "prior_fail_records"
    df["__prio"] = df["attempt_status"].map(STATUS_PRIO).fillna(0)
    df["__y"]   = df["term_code"].astype(str).map(lambda s: _term_sort_key(s)[0])
    df["__t"]   = df["term_code"].astype(str).map(lambda s: _term_sort_key(s)[1])
    df = df.sort_values(["course_id_norm", "__prio", "__y", "__t"],
                        ascending=[True, False, True, True])
    best = df.drop_duplicates("course_id_norm", keep="first").drop(columns=["__prio", "__y", "__t"])
    best = best.join(fail_counts, on="course_id_norm")
    best = best.join(fail_records, on="course_id_norm")
    best["prior_fail_count"]   = best["prior_fail_count"].fillna(0).astype(int)
    best["prior_fail_records"] = best["prior_fail_records"].fillna("[]")
    return best.set_index("course_id_norm")


def _pick_curriculum_csv(track: str):
    """Find curriculum_{track}.csv using registry naming convention."""
    if not track or track == "UNKNOWN":
        return None
    fname = f"curriculum_{track}.csv"
    p = _resource_path(fname)
    return p if p.exists() else None


def _load_catalog_ids(csv_path: Path) -> set:
    if not csv_path.exists():
        return set()
    df = pd.read_csv(csv_path, engine="python")
    col = "course_id" if "course_id" in df.columns else df.columns[0]
    return set(df[col].dropna().astype(str).map(norm_id))


def _choose_bucket_assignment(candidates_df: pd.DataFrame, allowed_ids: set, used: set):
    if candidates_df.empty or not allowed_ids:
        return None
    pool = candidates_df[candidates_df["course_id_norm"].isin(allowed_ids)]
    if pool.empty:
        return None
    pool = pool.sort_values(
        ["attempt_status", "term_code"],
        ascending=[False, True],
        key=lambda c: c.map(STATUS_PRIO) if c.name == "attempt_status" else c.map(_term_sort_key),
    )
    for cid in pool["course_id_norm"]:
        if cid not in used:
            return cid
    return None


def _te_catalog_path(major: str):
    fname = f"{major}_TE.csv"
    p = _resource_path(fname)
    return p if p.exists() else None


def _load_te_ids(csv_path) -> set:
    if not csv_path or not Path(csv_path).exists():
        return set()
    df = pd.read_csv(csv_path, engine="python")
    col = "course_id" if "course_id" in df.columns else df.columns[0]
    ids = df[col].dropna().astype(str).map(norm_id)
    if "category" in df.columns:
        ids = ids[df["category"].astype(str).str.strip().str.lower().eq("techelective")]
    return set(ids)


def _load_te_rules(path=None):
    if path is None:
        path = _resource_path("TE_Rules.csv")
    path = Path(path)
    if not path.exists():
        return None
    df = pd.read_csv(path, engine="python")
    for c in ["major", "rule_type", "value", "effect", "applies_to_bucket", "priority"]:
        if c not in df.columns:
            df[c] = ""
    df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(0).astype(int)
    df["major"]    = df["major"].astype(str).str.upper().str.strip()
    df["rule_type"] = df["rule_type"].astype(str).str.lower().str.strip()
    df["effect"]   = df["effect"].astype(str).str.lower().str.strip()
    df["applies_to_bucket"] = df["applies_to_bucket"].astype(str).str.strip()
    df["value"]    = df["value"].astype(str)
    return df


def _te_allowed(course_id_norm: str, major: str, rules_df) -> bool:
    if rules_df is None:
        return True
    rsub = rules_df[
        (rules_df["major"].isin([major.upper(), ""])) &
        (rules_df["applies_to_bucket"].isin(["TechElective", ""]))
    ].copy()
    if rsub.empty:
        return True
    rsub = rsub.sort_values("priority", ascending=False)
    for _, r in rsub.iterrows():
        rt, val = r["rule_type"], str(r["value"])
        if rt == "course_id":
            if norm_id(val) == course_id_norm:
                return r["effect"] != "ban"
        elif rt == "prefix":
            pref = norm_id(val).split(" ")[0]
            if course_id_norm.startswith(pref + " "):
                return r["effect"] != "ban"
        elif rt == "regex":
            try:
                if re.search(val, course_id_norm):
                    return r["effect"] != "ban"
            except re.error:
                continue
    return True


def _load_minor_index() -> dict:
    """Return code → display_name mapping from minors/index.json."""
    p = _resource_path("minors/index.json")
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def fill_pathway(transcript_csv: Path, extra_minor_codes: list = None, track_override: str = None) -> Path:
    """Map transcript courses to curriculum slots. Returns filled_pathway CSV path."""
    tx = pd.read_csv(transcript_csv, engine="python")
    if "course_id" not in tx.columns:
        raise ValueError("Transcript missing 'course_id' column.")

    tx["course_id_norm"] = tx["course_id"].map(norm_id)
    tx["grade_tok"]      = tx["grade"].map(_clean_grade_token)
    tx["attempt_status"] = tx.apply(_attempt_status, axis=1)

    major      = _infer_major(tx)
    start_year = _first_class_year(tx)
    track      = track_override or _detect_track(major, start_year)

    cur_path = _pick_curriculum_csv(track)
    if cur_path is None:
        raise FileNotFoundError(f"No curriculum CSV found for track '{track}'.")
    cur = pd.read_csv(cur_path, engine="python")
    for c in ["term_id", "term_label", "course_id", "course_name", "credits",
              "bucket", "prereq", "coreq", "min_grade", "notes"]:
        if c not in cur.columns:
            cur[c] = ""
    cur["course_id_norm"] = cur["course_id"].map(norm_id)
    cur["course_name_str"] = cur["course_name"].astype(str).str.strip()
    cur["is_alt_row"] = cur["course_name_str"].str.contains(r"\bALT\b", case=False, regex=True)
    cur["alt_base_name"] = cur["course_name_str"].str.replace(r"\s+ALT\b", "", case=False, regex=True).str.upper().str.strip()

    # Build equivalency groups for rows labeled as ALT so alt courses satisfy the primary slot.
    alt_equiv = {}
    for _, grp in cur.groupby(["term_id", "alt_base_name"]):
        ids = set(grp["course_id_norm"].dropna().astype(str).str.strip())
        ids = {cid for cid in ids if cid and cid.lower() not in {"nan", "none"}}
        if len(ids) > 1:
            for cid in ids:
                alt_equiv[cid] = ids

    best_map = _best_attempts(tx)

    ah_ids   = _load_catalog_ids(_resource_path("AHelectives.csv"))
    ss_ids   = _load_catalog_ids(_resource_path("SSelectives.csv"))
    te_ids   = _load_te_ids(_te_catalog_path(major))
    te_rules = _load_te_rules(_resource_path("TE_Rules.csv"))
    if te_ids:
        te_ids = {cid for cid in te_ids if _te_allowed(cid, major, te_rules)}

    tx_usable = tx[tx["attempt_status"].isin(["passed", "in_progress"])].copy()

    out_rows = []
    for _, row in cur.iterrows():
        if bool(row.get("is_alt_row", False)):
            # ALT entries are alternatives to the primary slot, not extra required slots.
            continue
        cidn   = row["course_id_norm"]
        bucket = str(row.get("bucket", "")).strip()
        filled = {
            "term_id": row["term_id"], "term_label": row["term_label"],
            "slot_course_id": row["course_id"], "slot_course_name": row["course_name"],
            "credits": row.get("credits", ""), "bucket": bucket,
            "prereq": row.get("prereq", ""), "coreq": row.get("coreq", ""),
            "min_grade": row.get("min_grade", ""), "notes": row.get("notes", ""),
            "match_course_id": "", "match_course_name": "",
            "match_term_code": "", "match_grade": "",
            "match_status": "open", "meets_min_grade": "",
            "prior_fail_count": 0,
            "source_track": track,
        }
        if bucket not in {"GENED_AH", "GENED_SS", "TechElective"}:
            candidate_ids = expand_equiv_ids(alt_equiv.get(cidn, {cidn}))
            att = tx[tx["course_id_norm"].isin(candidate_ids)].copy()
            if att.empty and not re.search(r"[A-Z]$", cidn):
                # Fallback: match transcript IDs that have a letter suffix but otherwise
                # equal this slot (e.g., "ENGL 1010S" matches curriculum slot "ENGL 1010").
                # Only applies when the slot itself has no letter suffix (avoiding labs like PHYS 1410L).
                base_norm = tx["course_id_norm"].str.replace(r"(?<=[0-9]{4})[A-Z]+$", "", regex=True)
                att = tx[base_norm.isin({re.sub(r"(?<=[0-9]{4})[A-Z]+$", "", x) for x in candidate_ids})].copy()
            if not att.empty:
                meets  = att[att["grade_tok"].map(lambda g: grade_meets_min(g, row.get("min_grade", "")))]
                chosen = _latest_by_term(meets) if not meets.empty else _latest_by_term(att)
                if not chosen.empty:
                    raw_grade = chosen.get("grade", "")
                    # Sanitize: NaN from pandas must become empty string, not "nan"
                    if raw_grade is None or (isinstance(raw_grade, float) and pd.isna(raw_grade)):
                        raw_grade = ""
                    raw_grade = str(raw_grade).strip()
                    if raw_grade.lower() in ("nan", "none"):
                        raw_grade = ""
                    chosen_status = str(chosen.get("attempt_status", "unknown")).lower()
                    filled["match_course_id"]   = chosen.get("course_id", "")
                    filled["match_course_name"] = chosen.get("course_name", "")
                    filled["match_term_code"]   = chosen.get("term_code", "")
                    filled["match_grade"]       = raw_grade
                    if chosen_status == "in_progress":
                        filled["match_status"]    = "in_progress"
                        filled["meets_min_grade"] = ""
                    else:
                        ok = grade_meets_min(raw_grade, row.get("min_grade", ""))
                        filled["match_status"]    = chosen_status if ok else "below_min_grade"
                        filled["meets_min_grade"] = "Y" if ok else "N"
        if cidn in best_map.index and "prior_fail_count" in best_map.columns:
            pfc = best_map.loc[cidn, "prior_fail_count"]
            filled["prior_fail_count"] = int(pfc) if pd.notna(pfc) else 0
            pfr = best_map.loc[cidn, "prior_fail_records"] if "prior_fail_records" in best_map.columns else "[]"
            filled["prior_fail_records"] = pfr if pd.notna(pfr) else "[]"
        out_rows.append(filled)

    filled_df = pd.DataFrame(out_rows)

    used_bucket_ids = set()
    fixed_matched = set(best_map.index).intersection(
        set(filled_df.loc[filled_df["match_status"] != "open", "slot_course_id"].map(norm_id))
    )
    used_bucket_ids |= fixed_matched

    for idx in filled_df.index[filled_df["bucket"].isin(["GENED_AH", "GENED_SS"])]:
        bucket   = filled_df.at[idx, "bucket"]
        allowed  = ah_ids if bucket == "GENED_AH" else ss_ids
        chosen_n = _choose_bucket_assignment(tx_usable, allowed, used_bucket_ids)
        if chosen_n:
            b = best_map.loc[chosen_n]
            filled_df.loc[idx, ["match_course_id", "match_course_name",
                                 "match_term_code", "match_grade", "match_status"]] = [
                b.get("course_id", ""), b.get("course_name", ""),
                b.get("term_code", ""), b.get("grade", ""), b.get("attempt_status", "unknown"),
            ]
            used_bucket_ids.add(chosen_n)

    if te_ids:
        te_allowed_tx = {cid for cid in tx_usable["course_id_norm"].unique()
                         if cid in te_ids and _te_allowed(cid, major, te_rules)}
        te_row_idx = filled_df.index[filled_df["bucket"].eq("TechElective")]
        used_bucket_ids |= set(filled_df.loc[filled_df["match_status"] != "open",
                                              "match_course_id"].map(norm_id))
        for idx in te_row_idx:
            chosen_n = _choose_bucket_assignment(tx_usable, te_allowed_tx, used_bucket_ids)
            if not chosen_n:
                continue
            b = best_map.loc[chosen_n]
            filled_df.loc[idx, ["match_course_id", "match_course_name",
                                 "match_term_code", "match_grade", "match_status"]] = [
                b.get("course_id", ""), b.get("course_name", ""),
                b.get("term_code", ""), b.get("grade", ""), b.get("attempt_status", "unknown"),
            ]
            used_bucket_ids.add(chosen_n)

    # unmapped
    curriculum_fixed = set(cur.loc[~cur["bucket"].isin(
        ["GENED_AH", "GENED_SS", "TechElective"]), "course_id_norm"])
    assigned = set(filled_df.loc[filled_df["match_status"] != "open",
                                  "match_course_id"].map(norm_id))
    tx_unmapped = tx_usable[
        (~tx_usable["course_id_norm"].isin(curriculum_fixed)) &
        (~tx_usable["course_id_norm"].isin(assigned))
    ]
    unmapped_rows = []
    for _, r in tx_unmapped.sort_values(["course_id_norm", "term_code"]).iterrows():
        _cid_n = str(r.get("course_id_norm", norm_id(r.get("course_id", "")))).strip()
        if _cid_n in ah_ids:
            _hint = "May count as Gen Ed: Arts & Humanities"
        elif _cid_n in ss_ids:
            _hint = "May count as Gen Ed: Social Science"
        elif _cid_n in te_ids:
            _hint = "May count as Technical Elective"
        else:
            _hint = ""
        unmapped_rows.append({
            "term_id": "UNMAPPED", "term_label": "Unmapped from Transcript",
            "slot_course_id": "", "slot_course_name": "", "credits": "", "bucket": "",
            "prereq": "", "coreq": "", "min_grade": "", "notes": "",
            "match_course_id": r.get("course_id", ""), "match_course_name": r.get("course_name", ""),
            "match_term_code": r.get("term_code", ""), "match_grade": r.get("grade", ""),
            "match_status": r.get("attempt_status", ""), "source_track": track,
            "unmapped_hint": _hint,
        })

    filled_df["__order"] = 1
    if unmapped_rows:
        unmapped_df = pd.DataFrame(unmapped_rows)
        unmapped_df["__order"] = 0
        combined = pd.concat([unmapped_df, filled_df], ignore_index=True)
    else:
        combined = filled_df

    combined = combined.sort_values(
        ["__order", "term_id", "slot_course_id"], ascending=[True, True, True]
    ).drop(columns="__order")

    # prereq flags
    min_by_course = (
        cur.loc[~cur["bucket"].isin(["GENED_AH", "GENED_SS", "TechElective"]),
                ["course_id_norm", "min_grade"]]
        .set_index("course_id_norm")["min_grade"].to_dict()
    )
    ever_met_min = set()
    for _, r in tx.iterrows():
        cidn = r.get("course_id_norm", "")
        if not cidn:
            continue
        if grade_meets_min(r.get("grade_tok", ""), min_by_course.get(cidn, "")):
            ever_met_min |= equiv_ids(cidn)
    for cid in tx.loc[tx["attempt_status"].isin(["passed", "transfer"]), "course_id_norm"]:
        ever_met_min |= equiv_ids(cid)

    def _prereq_check(row) -> str:
        pre = str(row.get("prereq", "")).upper().strip()
        if not pre:
            return "Y"
        found = re.findall(r"[A-Z]{2,}\.? ?\d{3,4}", pre)
        if not found:
            return "Y"
        for c in found:
            if not (equiv_ids(c) & ever_met_min):
                return "N"
        return "Y"

    combined["prereqs_met_flag"] = combined.apply(_prereq_check, axis=1)

    # viz status
    def _viz(row) -> str:
        has_term   = str(row.get("match_term_code", "")).strip() not in ("", "nan", "none")
        grade_raw  = str(row.get("match_grade", "")).strip().lower()
        has_grade  = grade_raw not in ("", "nan", "none")
        mstatus    = str(row.get("match_status", "")).lower().strip()
        min_g      = _clean_grade_token(row.get("min_grade", ""))
        prereqs_ok = str(row.get("prereqs_met_flag", "")).upper() == "Y"
        passed     = has_grade and grade_meets_min(row.get("match_grade", ""), min_g)
        taken_pass = passed or mstatus in {"passed", "transfer", "completed"}
        if mstatus == "in_progress" or (has_term and not has_grade):
            return "blue"
        if taken_pass:
            return "green"
        if has_grade and not passed:
            return "amber"   # taken but did not meet min grade
        if not has_term and not prereqs_ok:
            return "grey"
        if prereqs_ok:
            return "yellow"  # not yet taken, prereqs met
        return "grey"

    combined["viz_status"] = combined.apply(_viz, axis=1)

    # Second pass: mark courses that would be unlocked next semester if in-progress pass
    inprog_ids = set(
        combined.loc[combined["viz_status"] == "blue", "match_course_id"]
        .dropna().astype(str).map(norm_id)
    ) - {"", "nan"}
    if inprog_ids:
        future_met = ever_met_min | inprog_ids
        grey_mask = combined["viz_status"] == "grey"
        for idx in combined.index[grey_mask]:
            pre_str = str(combined.at[idx, "prereq"]).upper()
            prereq_ids_found = re.findall(r"[A-Z]{2,}\.? ?\d{3,4}", pre_str)
            if prereq_ids_found and all(norm_id(c) in future_met for c in prereq_ids_found):
                combined.at[idx, "viz_status"] = "next_eligible"

    # ── minor detection & processing ──────────────────────────────────────────
    minor_info = _infer_minor(tx)
    if minor_info:
        minor_code, minor_display_name = minor_info
        minor_csv = _resource_path(f"minors/minor_{minor_code}.csv")
        if minor_csv.exists():
            minor_rows = _process_minor(minor_csv, tx, ever_met_min,
                                        minor_code, minor_display_name, track)
            if minor_rows:
                minor_df = pd.DataFrame(minor_rows)
                combined = pd.concat([combined, minor_df], ignore_index=True)
        else:
            placeholder = {c: "" for c in combined.columns}
            placeholder.update({
                "term_id": "MINOR", "term_label": f"{minor_display_name} Minor",
                "slot_course_name": f"⚠ minor_{minor_code}.csv not found — create it to track minor",
                "bucket": f"Minor_{minor_code}",
                "viz_status": "grey", "source_track": track,
            })
            combined = pd.concat([combined, pd.DataFrame([placeholder])], ignore_index=True)

    # ── extra (undeclared) minors added by user ───────────────────────────────
    if extra_minor_codes:
        _idx = _load_minor_index()
        for _code in extra_minor_codes:
            _code = str(_code).strip().upper()
            if not _code:
                continue
            _dname = _idx.get(_code, _code.title())
            _mcsv = _resource_path(f"minors/minor_{_code}.csv")
            if _mcsv.exists():
                _mrows = _process_minor(_mcsv, tx, ever_met_min, _code, _dname, track)
                if _mrows:
                    combined = pd.concat([combined, pd.DataFrame(_mrows)], ignore_index=True)

    # ── propagate student identity into every row ──────────────────────────────
    _sname = ""
    for _col in ["full_name", "student_name"]:
        if _col in tx.columns:
            _vals = tx[_col].dropna().astype(str)
            _vals = _vals[_vals.str.strip().ne("") & _vals.str.lower().ne("nan")]
            if not _vals.empty:
                _sname = _vals.iloc[0]; break
    _sid = ""
    if "student_id" in tx.columns:
        _sv = tx["student_id"].dropna()
        if not _sv.empty:
            _sid = str(_sv.iloc[0])
    combined["student_name"]       = _sname
    combined["student_id"]         = _sid
    combined["student_start_year"] = start_year if start_year else ""

    _cum_gpa = ""
    if "cum_gpa" in tx.columns:
        _gv = tx["cum_gpa"].dropna().astype(str).str.strip()
        _gv = _gv[_gv.ne("") & _gv.ne("nan")]
        if not _gv.empty:
            _cum_gpa = _gv.iloc[0]
    combined["cum_gpa"] = _cum_gpa

    out_path = transcript_csv.with_suffix(".filled_pathway.csv")
    combined.to_csv(out_path, index=False)
    return out_path


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — filled_pathway.csv → self-contained HTML
# ═══════════════════════════════════════════════════════════════════════════════

def _canonical_term(term_id: str, term_label: str):
    """Convert term_id / term_label to canonical key like Y1F, Y2S, …"""
    tid = str(term_id or "").strip().upper()
    if re.match(r"Y[1-4][FS]$", tid):
        return tid
    label = str(term_label or "").lower()
    ymap  = {"freshman": 1, "sophomore": 2, "junior": 3, "senior": 4,
             "year 1": 1, "year 2": 2, "year 3": 3, "year 4": 4}
    smap  = {"fall": "F", "spring": "S"}
    y = next((v for k, v in ymap.items() if k in label), None)
    s = next((v for k, v in smap.items() if k in label), None)
    return f"Y{y}{s}" if y and s else None


def _html_box_status(row: dict) -> str:
    """Return one of: completed | inprog | belowmin | eligible | locked"""
    taken   = str(row.get("taken_flag",   "")).upper() == "Y"
    inprog  = str(row.get("in_progress_flag", "")).upper() == "Y"
    passes  = str(row.get("meets_min_grade", "")).upper() == "Y"
    prereq  = (str(row.get("prereqs_met_flag",      "")).upper() == "Y" or
               str(row.get("prerequisite_met_flag", "")).upper() == "Y")
    status  = str(row.get("match_status", "")).lower().strip()
    grade   = str(row.get("match_grade", "")).strip()
    viz     = str(row.get("viz_status", "")).lower().strip()

    # Use viz_status when present (it's the authoritative field from Step 2)
    if viz == "green":
        return "completed"
    if viz == "blue":
        return "inprog"
    if viz == "yellow":
        return "eligible"
    if viz in ("amber", "yellow_taken"):
        return "belowmin"
    if viz == "grey":
        return "locked"
    if viz == "next_eligible":
        return "nextelig"

    # Fallback: derive from flags
    if inprog or status == "in_progress":
        return "inprog"
    if taken or status in {"passed", "transfer", "completed"}:
        if grade and not passes:
            return "belowmin"
        return "completed"
    return "eligible" if prereq else "locked"


def build_cpr_table(df: pd.DataFrame) -> str:
    """Build a printable CPR table (Fall | Spring side-by-side per year)."""
    year_names   = {1: "Freshman Year", 2: "Sophomore Year", 3: "Junior Year", 4: "Senior Year"}
    bucket_class = {"GENED_AH": "row-ah", "GENED_SS": "row-ss", "TechElective": "row-te"}

    # Group all curriculum slots (including electives) by term
    grid_all = {t: [] for t in TERM_ORDER_HTML}
    for _, r in df.iterrows():
        row = r.to_dict()
        if str(row.get("term_id", "")).upper() == "UNMAPPED":
            continue
        if not str(row.get("slot_course_id", "")).strip():
            continue
        tk = _canonical_term(str(row.get("term_id", "")), str(row.get("term_label", "")))
        if tk in grid_all:
            grid_all[tk].append(row)

    def _fmt_cr(v):
        try:
            f = float(str(v or "0"))
            return str(int(f)) if f == int(f) else str(f)
        except Exception:
            return str(v)

    def _clean_str(v) -> str:
        s = "" if v is None else str(v).strip()
        return "" if s.lower() in {"", "nan", "none", "nat"} else s

    def _clean_cid(v) -> str:
        s = _clean_str(v)
        return norm_id(s) if s else ""

    def make_cells(row):
        if row is None:
            return '<td></td><td></td><td class="cr"></td><td class="t-grade"></td><td class="term-col"></td>'
        bucket     = str(row.get("bucket", "")).strip()
        slot_cid   = _clean_cid(row.get("slot_course_id", ""))
        slot_cname = _clean_str(row.get("slot_course_name", ""))
        match_cid  = _clean_cid(row.get("match_course_id", ""))
        match_cname = _clean_str(row.get("match_course_name", ""))
        if bucket in ELECTIVE_BUCKETS and match_cid:
            cid = match_cid
            cname = match_cname or slot_cname
        else:
            cid = slot_cid
            cname = slot_cname
        if bucket == "TechElective" and not match_cid:
            cname = "Tech Elective"
        counts_as = ""
        if bucket in ELECTIVE_BUCKETS and match_cid and slot_cid and slot_cid != match_cid:
            counts_as = f' <span class="counts-as-inline">(counts as {slot_cid})</span>'
        creds      = _fmt_cr(row.get("credits", ""))
        grade      = str(row.get("match_grade", "")).strip()
        term_taken = str(row.get("match_term_code", "")).strip()
        min_g      = str(row.get("min_grade", "")).strip()
        status     = _html_box_status(row)
        cid_cls    = "cid-req" if min_g and min_g.upper() not in ("", "NAN") else ""
        sc         = f"cell-{status}"

        # Grade display — clean up "nan" values
        grade_disp = grade if grade and grade.lower() != "nan" else ""

        # Status badge shown inline with course name
        badge = ""
        if status == "eligible":
            badge = ' <span class="badge badge-next">\u2192 Up Next</span>'
        elif status == "inprog":
            badge = ' <span class="badge badge-inprog">In Progress</span>'
        elif status == "belowmin":
            badge = ' <span class="badge badge-belowmin">Below Min</span>'

        return (
            f'<td class="t-cid {cid_cls} {sc}" data-slot-cid="{cid}">{cid}</td>'
            f'<td class="t-name {sc}">{cname}{counts_as}{badge}</td>'
            f'<td class="cr {sc}">{creds}</td>'
            f'<td class="t-grade {sc}">{grade_disp}</td>'
            f'<td class="term-col {sc}">{term_taken}</td>'
        )

    body = ""
    total_all = 0.0

    for y in range(1, 5):
        fall_rows   = grid_all[f"Y{y}F"]
        spring_rows = grid_all[f"Y{y}S"]
        n = max(len(fall_rows), len(spring_rows), 1)

        body += (
            f'<tr class="year-label-row"><td colspan="11">{year_names[y]}</td></tr>'
            f'<tr class="sem-hdr-row">'
            f'<th class="t-cid">Course</th><th class="t-name">Fall</th>'
            f'<th class="cr">Cr.</th><th class="t-grade">Grade</th><th class="term-col">Term</th>'
            f'<th class="sep"></th>'
            f'<th class="t-cid">Course</th><th class="t-name">Spring</th>'
            f'<th class="cr">Cr.</th><th class="t-grade">Grade</th><th class="term-col">Term</th>'
            f'</tr>'
        )

        fall_total = spring_total = 0.0
        for i in range(n):
            fr = fall_rows[i]   if i < len(fall_rows)   else None
            sr = spring_rows[i] if i < len(spring_rows) else None
            fb = str((fr or {}).get("bucket", ""))
            sb = str((sr or {}).get("bucket", ""))
            row_cls = bucket_class.get(fb or sb, "")
            body += f'<tr class="course-row {row_cls}">{make_cells(fr)}<td class="sep"></td>{make_cells(sr)}</tr>'
            for row, which in [(fr, "f"), (sr, "s")]:
                if row:
                    try:
                        v = float(str(row.get("credits", "0") or "0"))
                        if which == "f":
                            fall_total += v
                        else:
                            spring_total += v
                    except Exception:
                        pass

        body += (
            f'<tr class="total-row">'
            f'<td colspan="2" class="total-label">Total</td>'
            f'<td class="cr total-val">{_fmt_cr(fall_total)}</td><td></td><td></td>'
            f'<td class="sep"></td>'
            f'<td colspan="2" class="total-label">Total</td>'
            f'<td class="cr total-val">{_fmt_cr(spring_total)}</td><td></td><td></td>'
            f'</tr>'
        )
        total_all += fall_total + spring_total

    body += (
        f'<tr class="grand-total-row">'
        f'<td colspan="6"></td>'
        f'<td colspan="5">Total Required Credits: {_fmt_cr(total_all)}</td>'
        f'</tr>'
    )

    return (
        f'<div id="cpr-table-section">'
        f'<h2 class="table-title">Curriculum Progress \u2014 Table View</h2>'
        f'<table class="cpr-table"><tbody>{body}</tbody></table>'
        f'</div>'
    )


def build_html(df: pd.DataFrame) -> str:
    # ── metadata ──────────────────────────────────────────────────────────────
    def find_col(target):
        tgt = target.replace("_", "").replace(" ", "").lower()
        for c in df.columns:
            if str(c).replace("_", "").replace(" ", "").lower() == tgt:
                return c
        return None

    def first_nonempty(col):
        if col is None:
            return None
        s = df[col].astype(str).str.strip()
        s = s[~s.str.lower().isin(["", "nan", "none"])]
        return s.iloc[0] if not s.empty else None

    student_name = first_nonempty(find_col("student_name") or find_col("full_name")) or "Student"
    major        = first_nonempty(find_col("source_track") or find_col("plan_short") or
                                   find_col("plan")) or "—"
    student_id   = first_nonempty(find_col("student_id")) or ""
    ssy_col = find_col("student_start_year")
    if ssy_col:
        _syv = df[ssy_col].dropna().astype(str).str.strip()
        _syv = _syv[_syv.ne("") & _syv.ne("nan")]
        start_year = _syv.iloc[0] if not _syv.empty else "—"
    else:
        start_year = "—"
        for cand in ["match_term_code", "term_code", "term_label", "term"]:
            col = find_col(cand)
            if col:
                years = df[col].dropna().astype(str).str.extract(r"(\d{4})")[0].dropna()
                if not years.empty:
                    start_year = int(years.astype(int).min())
                break

    # ── sort rows ─────────────────────────────────────────────────────────────
    grid     = {t: [] for t in TERM_ORDER_HTML}
    elects   = {"GENED_AH": [], "GENED_SS": [], "TechElective": []}
    minors   = {}   # minor_code -> {"display_name": str, "rows": list, "pools": dict}
    unmapped = []

    for _, r in df.iterrows():
        row    = r.to_dict()
        bucket = str(row.get("bucket", "")).strip()
        tk     = _canonical_term(str(row.get("term_id", "")), str(row.get("term_label", "")))
        if bucket.startswith("Minor_"):
            mcode = bucket[len("Minor_"):]
            if mcode not in minors:
                dname = str(row.get("term_label", mcode + " Minor")).replace(" Minor", "").strip()
                minors[mcode] = {"display_name": dname, "rows": [], "pools": {}}
            minors[mcode]["rows"].append(row)
            # Collect pool stats once per pool_id
            pid = str(row.get("minor_pool_id", "")).strip()
            if pid and pid not in minors[mcode]["pools"]:
                minors[mcode]["pools"][pid] = {
                    "done":       row.get("minor_pool_slots_done", 0),
                    "needed":     row.get("minor_pool_slots_needed", 1),
                    "count_type": row.get("minor_pool_count_type", "courses"),
                    "total_avail":row.get("minor_pool_total_avail", 0),
                    "label":      str(row.get("minor_pool_label", "")).strip(),
                }
        elif bucket in ELECTIVE_BUCKETS:
            elects[bucket].append(row)
        elif tk in grid:
            grid[tk].append(row)
        else:
            unmapped.append(row)

    # ── credit / GPA stats ────────────────────────────────────────────────────
    cum_gpa = first_nonempty(find_col("cum_gpa")) or ""
    _stat_rows = sum(grid.values(), []) + sum(elects.values(), [])
    _earned_cr = _inprog_cr = _total_cr = 0.0
    for _r in _stat_rows:
        try:
            cr = float(_r.get("credits", 0) or 0)
        except (ValueError, TypeError):
            cr = 0.0
        _total_cr += cr
        vs = str(_r.get("viz_status", "")).lower()
        if vs == "green":
            _earned_cr += cr
        elif vs == "blue":
            _inprog_cr += cr
    _remaining_cr = max(_total_cr - _earned_cr - _inprog_cr, 0.0)

    def _cr(v):
        return int(v) if v == int(v) else round(v, 1)

    # ── course box renderer ───────────────────────────────────────────────────
    def course_box(row: dict) -> str:
        def _clean_str(v) -> str:
            s = "" if v is None else str(v).strip()
            return "" if s.lower() in {"", "nan", "none", "nat"} else s

        def _clean_cid(v) -> str:
            s = _clean_str(v)
            return norm_id(s) if s else ""

        bucket = str(row.get("bucket", "")).strip()
        slot_cid = _clean_cid(row.get("slot_course_id", ""))
        slot_cname = _clean_str(row.get("slot_course_name", ""))
        match_cid = _clean_cid(row.get("match_course_id", ""))
        match_cname = _clean_str(row.get("match_course_name", ""))
        if bucket in ELECTIVE_BUCKETS and match_cid:
            cid = match_cid
            cname = match_cname or slot_cname or "(Unnamed)"
        else:
            cid = slot_cid
            cname = slot_cname or "(Unnamed)"
        if bucket == "TechElective" and not match_cid:
            cname = "Tech Elective"
        creds = str(row.get("credits", "")).strip()
        grade = str(row.get("match_grade", "")).strip()
        prereq_ids = json.dumps(extract_course_ids(str(row.get("prereq", ""))))
        coreq_ids  = json.dumps(extract_course_ids(str(row.get("coreq",  ""))))
        status  = _html_box_status(row)
        elem_id = css_id(cid) if cid else "c-" + re.sub(r"\W+", "-", cname)[:20]
        recommendable = status in {"eligible", "nextelig", "belowmin"}
        grade_h = f'<span class="grade">{grade}</span>' if grade not in ("", "nan") else ""
        creds_h = f'<span class="credits">{creds} cr</span>' if creds not in ("", "nan") else ""
        counts_as_h = ""
        if bucket in ELECTIVE_BUCKETS and match_cid and slot_cid and slot_cid != match_cid:
            counts_as_h = f'<span class="counts-as-note">Counts as: {slot_cid}</span>'
        rec_btn_h = '<button class="rec-btn" title="Mark as recommended next semester">+ Next</button>' if recommendable else ""
        _pf = row.get("prior_fail_count", 0)
        n_dots = min(int(_pf if (_pf == _pf and _pf) else 0), 4)
        if n_dots > 0:
            try:
                _recs = json.loads(str(row.get("prior_fail_records", "[]")) or "[]")
            except (ValueError, TypeError):
                _recs = []
            _dot_spans = ""
            for _i in range(n_dots):
                _rec = _recs[_i] if _i < len(_recs) else {}
                _tip = (_rec.get("grade") or "?") + (" · " + _rec["term"] if _rec.get("term") else "")
                _dot_spans += f'<span class="attempt-dot" data-attempt="{_tip}" title="{_tip}"></span>'
            dots_h = f'<div class="attempt-dots">{_dot_spans}</div>'
        else:
            dots_h = ""
        return (
            f'<div class="course-box {status}" id="{elem_id}" '
            f'data-cid="{cid}" data-prereqs=\'{prereq_ids}\' data-coreqs=\'{coreq_ids}\' '
            f'data-term-id="{row.get("term_id", "")}" data-term-label="{str(row.get("term_label", "")).strip()}" '
            f'data-orig-status="{status}" data-orig-cid="{cid}" data-orig-cname="{cname}">'
            f'<div class="cid">{cid}</div>'
            f'<div class="cname">{cname}</div>'
            f'<div class="meta">{creds_h}{grade_h}{counts_as_h}</div>'
            f'{rec_btn_h}'
            f'{dots_h}'
            f'</div>'
        )

    # ── grid ──────────────────────────────────────────────────────────────────
    grid_html = ""
    for y in range(1, 5):
        terms_html = ""
        for s in ["F", "S"]:
            key   = f"Y{y}{s}"
            boxes = "".join(course_box(r) for r in grid[key]) or '<div class="empty-slot">—</div>'
            terms_html += (
                f'<div class="semester" id="sem-{key}">'
                f'<div class="sem-header">{SEM_LABELS[s]}</div>'
                f'{boxes}</div>'
            )
        grid_html += (
            f'<div class="year-group" id="year-{y}">'
            f'<div class="year-header">{YEAR_LABELS[y]}</div>'
            f'<div class="semesters">{terms_html}</div></div>'
        )

    # ── electives ─────────────────────────────────────────────────────────────
    bucket_labels = {"GENED_AH": "Arts & Humanities",
                     "GENED_SS": "Social Science",
                     "TechElective": "Tech Electives"}
    elects_inner = ""
    for bucket, rows in elects.items():
        if not rows:
            continue
        boxes = "".join(course_box(r) for r in rows)
        elects_inner += (
            f'<div class="elective-group">'
            f'<div class="elective-header">{bucket_labels[bucket]}</div>'
            f'<div class="elective-boxes">{boxes}</div></div>'
        )
    elects_html = (f'<div id="electives"><div class="elects-inner">{elects_inner}</div></div>'
                   if elects_inner else "")

    # ── minors ────────────────────────────────────────────────────────────────
    minors_html = ""
    for mcode, mdata in minors.items():
        dname = mdata["display_name"]
        rows  = mdata["rows"]
        pools = mdata["pools"]

        def _pid(r):
            v = r.get("minor_pool_id", "")
            s = str(v).strip() if v is not None else ""
            return s if s.lower() not in ("", "nan") else ""
        req_rows  = [r for r in rows if not _pid(r)]
        pool_rows = [r for r in rows if _pid(r)]

        all_boxes = ""

        # Required courses — plain boxes in order
        for r in req_rows:
            all_boxes += course_box(r)

        # Pool groups — filled boxes first, then dashed elective slots
        if pool_rows:
            pool_ids_seen = []
            for r in pool_rows:
                pid = str(r.get("minor_pool_id","")).strip()
                if pid not in pool_ids_seen:
                    pool_ids_seen.append(pid)

            for pid in pool_ids_seen:
                pid_rows = [r for r in pool_rows if str(r.get("minor_pool_id","")).strip() == pid]
                ps       = pools.get(pid, {})
                needed   = int(ps.get("needed", 1) or 1)
                label    = ps.get("label", "") or str(pid_rows[0].get("minor_pool_label","")).strip()
                total    = ps.get("total_avail", len(pid_rows))
                short_label = label if label and label.lower() not in ("","nan") else "Elective"
                box_label = short_label[:28] + ("…" if len(short_label) > 28 else "")

                # Matched = student has taken this specific course (match_grade present)
                matched   = [r for r in pid_rows
                             if str(r.get("match_grade","")).strip() not in ("","nan")
                             or str(r.get("viz_status","")).strip() in ("green","blue")]
                # Options = pool rows with a real course_id (exclude blank descriptor rows)
                options   = [r for r in pid_rows
                             if str(r.get("slot_course_id","")).strip()
                             and r not in matched]
                still_needed = max(0, needed - len(matched))

                # Courses the student already took from this pool
                for r in matched:
                    all_boxes += course_box(r)

                # Empty elective slots
                if still_needed > 0:
                    # Auto-fill when only one option remains (no real choice)
                    if len(options) == 1:
                        for _ in range(still_needed):
                            all_boxes += course_box(options[0])
                    elif options:
                        opts_html = "".join(
                            f'<div class="pool-opt-item" data-cid="{norm_id(str(r.get("slot_course_id",""))).strip()}"'
                            f' data-cname="{str(r.get("slot_course_name","")).strip()}"'
                            f' data-credits="{str(r.get("credits","")).strip()}"'
                            f' onclick="poolOptSelect(event,this)">'
                            f'<span class="pool-opt-cid">{norm_id(str(r.get("slot_course_id",""))).strip()}</span>'
                            f' {str(r.get("slot_course_name","")).strip()}'
                            f'</div>'
                            for r in options
                        )
                        meta = f'<span class="credits">{needed} of {total}</span>'
                        for _ in range(still_needed):
                            all_boxes += (
                                f'<div class="course-box pool-slot" data-pool-id="{pid}" data-chosen=""'
                                f' onclick="poolSlotClick(event,this)">'
                                f'<div class="cid">▾ Elective</div>'
                                f'<div class="cname">{box_label}</div>'
                                f'<div class="meta">{meta}</div>'
                                f'<div class="pool-dropdown">{opts_html}</div>'
                                f'</div>'
                            )
                    else:
                        # No specific options known — show a generic advisory slot
                        for _ in range(still_needed):
                            all_boxes += (
                                f'<div class="course-box pool-slot">'
                                f'<div class="cid">Elective</div>'
                                f'<div class="cname">{box_label}</div>'
                                f'<div class="meta"><span class="credits">see advisor</span></div>'
                                f'</div>'
                            )

        remove_btn = (
            f'<a href="/remove-minor?session=__MINOR_SESSION_ID__&code={mcode}"'
            f' class="minor-remove-btn">✕ Remove</a>'
        )
        minors_html += (
            f'<div class="minor-section">'
            f'<h3 class="minor-title"><span>{dname} Minor</span>{remove_btn}</h3>'
            f'<div class="minor-flat-row">{all_boxes}</div>'
            f'</div>'
        )

    add_minor_html = ""  # minor management moved to nav bar panel

    # ── unmapped ──────────────────────────────────────────────────────────────
    unmapped_html = ""
    if unmapped:
        items = ""
        for r in unmapped:
            # Unmapped rows store actual course data in match_* fields, not slot_* fields
            cid   = norm_id(r.get("match_course_id","") or r.get("slot_course_id",""))
            cname = str(r.get("match_course_name","") or r.get("slot_course_name","")).strip()
            if not cid or cid.lower() in ("nan", ""):
                continue
            hint = str(r.get("unmapped_hint","")).strip()
            hint_html = f'<span class="chip-hint">{hint}</span>' if hint else ""
            items += (
            f'<div class="unmapped-chip" draggable="true" '
            f'data-cid="{cid}" data-cname="{cname}" '
            f'title="Drag onto a course slot to assign as override">'
            f'{cid} \u2014 {cname}{hint_html}</div>'
        )
        unmapped_html = (
            f'<div id="unmapped">'
            f'<strong>Unmapped / out-of-map courses</strong>'
            f'<span class="unmapped-hint">\u2014 drag onto a slot to override</span>'
            f'<div class="unmapped-chips">{items}</div>'
            f'</div>'
        )

    # ── CPR table ─────────────────────────────────────────────────────────────
    table_html = build_cpr_table(df)

    # ── full document ─────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CPR \u2014 {student_name}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:"Segoe UI",Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;min-height:100vh;padding:16px}}
#page-header{{display:flex;flex-direction:column;align-items:center;text-align:center;padding:14px 20px;background:#16213e;border:1px solid #0f3460;border-radius:8px;margin-bottom:14px}}
#page-header h1{{font-size:1.35rem;color:#e0e0e0}}
#page-header .student-name{{font-size:1.1rem;font-weight:700;color:#ffffff;margin-top:6px}}
#page-header .meta{{font-size:.88rem;color:#8090b0;margin-top:3px}}
.stats-bar{{display:flex;gap:10px;align-items:center;justify-content:center;margin-top:8px;flex-wrap:wrap}}
.stat{{display:flex;flex-direction:column;align-items:center;background:#0f3460;border-radius:6px;padding:4px 12px}}
.stat-label{{font-size:.65rem;color:#8090b0;text-transform:uppercase;letter-spacing:.05em}}
.stat-val{{font-size:.95rem;font-weight:700;color:#a0c4ff}}
.stat-sep{{color:#3a4a6a;font-size:.85rem}}
.chip-hint{{display:block;font-size:.65rem;color:#8090b0;margin-top:2px;font-style:italic}}
#legend{{display:flex;gap:14px;flex-wrap:wrap;justify-content:center;margin-bottom:12px;font-size:.76rem}}
.legend-item{{display:flex;align-items:center;gap:5px}}
.legend-swatch{{width:13px;height:13px;border-radius:3px;border:1px solid rgba(255,255,255,.2);flex-shrink:0}}
#curriculum-grid{{display:flex;gap:8px;position:relative}}
.year-group{{flex:1;background:#16213e;border:1px solid #0f3460;border-radius:8px;overflow:hidden;min-width:0}}
.year-header{{text-align:center;font-weight:700;font-size:.78rem;color:#a0c4ff;padding:6px 4px;background:#0f3460;text-transform:uppercase;letter-spacing:.06em}}
.semesters{{display:flex}}
.semester{{flex:1;padding:6px 4px;border-right:1px solid #0f3460;min-width:0}}
.semester:last-child{{border-right:none}}
.sem-header{{text-align:center;font-size:.68rem;color:#607090;font-weight:600;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;padding-bottom:3px;border-bottom:1px solid #0f3460}}
.empty-slot{{text-align:center;color:#444;font-size:.68rem;padding:6px 0}}
.course-box{{border-radius:5px;padding:5px 6px;margin-bottom:5px;cursor:pointer;transition:transform .12s,box-shadow .15s;position:relative;z-index:1}}
.course-box:hover{{transform:scale(1.05);z-index:50;box-shadow:0 4px 18px rgba(0,0,0,.6)}}
.course-box.completed{{background:#1b4332;border:1.5px solid #2d6a4f;color:#d8f3dc}}
.course-box.inprog{{background:#0d2137;border:1.5px solid #1565c0;color:#bbdefb}}
.course-box.belowmin{{background:#3e2400;border:1.5px solid #ff9800;color:#ffe0b2}}
.course-box.eligible{{background:#252510;border:2px solid #d4aa00;color:#fff5cc}}
.course-box.locked{{background:#1c1c1c;border:1.5px solid #333;color:#484848;opacity:.55}}
.course-box.nextelig{{background:#1c1500;border:2px dashed #ff8c00;box-shadow:0 0 0 2px #d4aa00;color:#ffe0a0;opacity:.85}}
.attempt-dots{{position:absolute;bottom:4px;left:4px;display:flex;gap:3px}}
.attempt-dot{{width:7px;height:7px;background:#cc2222;border-radius:50%;flex-shrink:0;position:relative;cursor:default}}
.attempt-dot:hover::after{{content:attr(data-attempt);position:absolute;bottom:130%;left:50%;
  transform:translateX(-50%);background:#1a1a2e;color:#e0e0e0;border:1px solid #0f3460;
  border-radius:4px;padding:3px 7px;font-size:.68rem;white-space:nowrap;z-index:500;
  pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.5)}}
.cid{{font-size:.68rem;font-weight:700;letter-spacing:.02em}}
.cname{{font-size:.62rem;line-height:1.3;margin-top:2px}}
.meta{{font-size:.60rem;margin-top:3px;display:flex;gap:6px;opacity:.8}}
.counts-as-note{{font-size:.56rem;opacity:.95;color:#c9d1d9}}
.grade{{font-weight:700}}
#electives{{margin-top:10px;background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:10px}}
.elects-inner{{display:flex;gap:14px;flex-wrap:wrap}}
.elective-group{{flex:1;min-width:160px}}
.elective-header{{font-size:.75rem;font-weight:700;color:#a0c4ff;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;padding-bottom:3px;border-bottom:1px solid #0f3460}}
.elective-boxes{{display:flex;flex-wrap:wrap;gap:5px}}
.elective-boxes .course-box{{flex:0 0 auto;min-width:88px;max-width:145px}}
#minor-pathway-bar{{margin-top:10px;text-align:right}}
#add-minor-btn{{display:inline-block;background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;padding:7px 16px;font-size:.82rem;text-decoration:none;font-family:"Segoe UI",Arial,sans-serif}}
#add-minor-btn:hover{{background:#1565c0;color:#fff}}
.minor-section{{margin-top:10px;background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:10px}}
.minor-title{{font-size:.85rem;font-weight:700;color:#c9d1d9;margin:0 0 8px;padding-bottom:4px;border-bottom:1px solid #0f3460;display:flex;align-items:center;justify-content:space-between}}
.minor-remove-btn{{font-size:.72rem;font-weight:400;color:#8090b0;text-decoration:none;padding:2px 8px;border:1px solid #333;border-radius:4px;white-space:nowrap}}
.minor-remove-btn:hover{{color:#ff8080;border-color:#663333;background:#1c1010}}
.minor-flat-row{{display:flex;flex-wrap:wrap;gap:5px}}
.minor-flat-row .course-box{{flex:0 0 auto;min-width:88px;max-width:145px}}
.pool-slot{{cursor:pointer;position:relative;background:#0d1b2e!important;border-style:dashed!important}}
.pool-slot .cid{{color:#6699cc}}
.pool-slot .cname{{color:#8090b0}}
.pool-chosen{{background:#0d2137!important;border-color:#1565c0!important;border-style:solid!important}}
.pool-chosen .cid{{color:#90caf9}}
.pool-chosen .cname{{color:#c9d1d9}}
.pool-dropdown{{display:none;position:absolute;z-index:200;background:#0d1b2e;border:1px solid #1565c0;border-radius:6px;padding:8px;min-width:220px;top:calc(100% + 4px);left:0;box-shadow:0 4px 16px rgba(0,0,0,.7);max-height:240px;overflow-y:auto}}
.pool-dropdown.show{{display:block}}
.pool-opt-item{{font-size:.65rem;padding:3px 4px;color:#c9d1d9;white-space:nowrap}}
.pool-opt-cid{{font-weight:700;color:#a0c4ff}}
#arrows-svg{{position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5}}
@media print{{
  #arrows-svg,#legend{{display:none!important}}
  body{{background:#fff;color:#111;padding:0}}
  #page-header{{background:#fff;border:1px solid #ccc;color:#111}}
  #page-header h1,#page-header .student-name{{color:#111}}
  #page-header .meta{{color:#555}}
  #curriculum-grid,.year-group{{background:#fff;border-color:#ccc}}
  .year-header{{background:#ddd;color:#111}}
  .semester{{border-color:#ccc}}
  .sem-header{{color:#555;border-color:#ccc}}
  .course-box.completed{{background:#d4edda;border-color:#28a745;color:#111}}
  .course-box.inprog{{background:#cce5ff;border-color:#004085;color:#111}}
  .course-box.belowmin{{background:#fff3cd;border-color:#ff9800;color:#111}}
  .course-box.eligible{{background:#fffbe0;border:2px solid #b89000;color:#111}}
  .course-box.locked{{background:#f0f0f0;border-color:#ccc;color:#888;opacity:1}}
  .course-box.nextelig{{background:#fff5e0;border:2px dashed #e07000;box-shadow:0 0 0 2px #b89000;color:#111;opacity:1}}
  .attempt-dot{{background:#cc2222}}
  #electives{{background:#fff;border-color:#ccc}}
  #cpr-table-section{{margin-top:12px}}
    #cpr-table-section{{break-inside:auto;page-break-inside:auto}}
    .cpr-table{{break-inside:auto;page-break-inside:auto}}
    .cpr-table thead{{display:table-header-group}}
    .cpr-table tfoot{{display:table-footer-group}}
    .cpr-table tr{{break-inside:avoid;page-break-inside:avoid;page-break-after:auto}}
    .year-label-row,.sem-hdr-row{{break-inside:avoid;page-break-inside:avoid}}
  .cell-completed,.cell-completed td{{background-color:#eaf7ee!important}}
  .cell-inprog,.cell-inprog td{{background-color:#e8f0fd!important}}
  .cell-eligible,.cell-eligible td{{background-color:#fffde7!important}}
  .cell-belowmin,.cell-belowmin td{{background-color:#fff3e0!important}}
  .badge{{border:1px solid #999}}
  .course-box.override{{border-top:2px solid #b8960c!important;border-left:2px solid #b8960c!important;border-right:2px solid #993333!important;border-bottom:2px solid #993333!important;background:#fffbf0!important;color:#111!important;opacity:1!important}}
  .cell-override,.cell-override td{{background-color:#fff8e1!important;color:#7a5c00!important}}
  .unmapped-hint{{display:none}}
  .unmapped-chip{{background:#f0f0f0!important;border-color:#ccc!important;color:#333!important}}
  .unmapped-chip.chip-used{{display:none}}
}}
/* ── CPR table ── */
#cpr-table-section{{margin-top:20px;padding:16px;background:#fff;border-radius:8px;color:#111}}
.table-title{{font-size:1rem;font-weight:700;margin-bottom:10px;color:#111}}
.cpr-table{{width:100%;border-collapse:collapse;font-size:.73rem}}
.cpr-table td,.cpr-table th{{border:1px solid #bbb;padding:3px 6px;text-align:left}}
.year-label-row td{{background:#c8c8c8;font-weight:700;font-size:.78rem;text-align:center;padding:4px 6px;border-bottom:2px solid #999}}
.sem-hdr-row th{{background:#e4e4e4;font-weight:700;text-align:center;font-size:.7rem}}
.sep{{border:none!important;width:6px;background:#fff!important}}
.cr{{width:36px;text-align:center}}
.term-col{{width:62px;font-size:.66rem;color:#444}}
.t-cid{{font-weight:700;width:82px}}
.t-name{{min-width:120px}}
.cid-req{{color:#c00}}
.row-ah{{background:#dce8f8}}
.row-ss{{background:#fce4d0}}
.row-te{{background:#d8efd8}}
.total-row td{{background:#efefef;font-weight:700;border-top:2px solid #bbb}}
.total-label{{text-align:right}}
.total-val{{text-align:center}}
.grand-total-row td{{background:#ddd;font-weight:700;text-align:right;padding-right:10px}}
.t-grade{{width:46px;text-align:center;font-weight:700}}
.cell-completed td,.cell-completed{{background-color:#eaf7ee!important;color:#1a4d2e}}
.cell-inprog td,.cell-inprog{{color:#1565c0;font-style:italic;background-color:#e8f0fd!important}}
.cell-belowmin td,.cell-belowmin{{color:#b74000;background-color:#fff3e0!important}}
.cell-eligible td,.cell-eligible{{color:#5a4500;background-color:#fffbe0!important}}
.cell-locked td,.cell-locked{{color:#aaa}}
.badge{{display:inline-block;font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:3px;vertical-align:middle;margin-left:4px;white-space:nowrap}}
.counts-as-inline{{font-size:.58rem;color:#6d5a00;font-style:italic;white-space:nowrap}}
.badge-next{{background:#fffbe0;color:#5a4500;border:1px solid #b89000}}
.badge-inprog{{background:#cce5ff;color:#004085;border:1px solid #004085}}
.badge-belowmin{{background:#ffe0b2;color:#7a2c00;border:1px solid #e65100}}
/* ── cross-link flash & undo ── */
.flash-hl{{outline:2px solid #ff8c00!important;outline-offset:2px}}
.undo-btn{{position:absolute;top:2px;right:2px;background:rgba(180,40,40,.8);color:#fff;border:none;border-radius:3px;width:15px;height:15px;font-size:.65rem;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;line-height:1;z-index:10}}
.undo-btn:hover{{background:#cc2222}}
.ovr-btn{{position:absolute;bottom:3px;right:3px;background:rgba(30,100,30,.75);color:#a8f0a8;border:1px solid #2d6a4f;border-radius:3px;font-size:.52rem;padding:1px 4px;cursor:pointer;display:none;z-index:10;line-height:1.4;white-space:nowrap}}
.course-box.eligible:hover .ovr-btn,.course-box.locked:hover .ovr-btn,.course-box.nextelig:hover .ovr-btn{{display:block}}
.ovr-btn:hover{{background:rgba(30,130,30,.9)}}
.rec-btn{{position:absolute;top:3px;right:3px;background:rgba(18,44,86,.92);color:#a0caff;border:1px solid #2a5ab0;border-radius:3px;font-size:.52rem;padding:1px 5px;cursor:pointer;line-height:1.35;display:none;z-index:11;white-space:nowrap}}
.course-box.eligible:hover .rec-btn,.course-box.nextelig:hover .rec-btn,.course-box.belowmin:hover .rec-btn{{display:block}}
.course-box.is-recommended .rec-btn{{display:block;background:rgba(24,92,42,.95);border-color:#2d6a4f;color:#c8f7d2}}
td[data-slot-cid]{{cursor:pointer}}
/* ── override drag-drop ── */
#unmapped{{margin-top:8px;font-size:.75rem;color:#666;background:#16213e;border:1px solid #0f3460;border-radius:6px;padding:8px 12px}}
.unmapped-hint{{font-size:.68rem;color:#445;margin-left:7px}}
.unmapped-chips{{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}}
.unmapped-chip{{cursor:grab;padding:3px 9px;border-radius:4px;border:1px solid #1565c0;background:#0d2137;color:#a0c4ff;font-size:.72rem;user-select:none;transition:background .1s}}
.unmapped-chip:hover{{background:#1565c0;color:#fff}}
.unmapped-chip.chip-used{{opacity:.35;cursor:default;text-decoration:line-through}}
.course-box.override{{border-top:2px solid #d4aa00!important;border-left:2px solid #d4aa00!important;border-right:2px solid #cc2222!important;border-bottom:2px solid #cc2222!important;background:#1f1a0a!important;color:#ffe8c0!important;opacity:1!important}}
.course-box.drag-over{{outline:2px dashed #fff;outline-offset:2px}}
.cell-override{{background:#fff8e1!important;color:#7a5c00!important}}
#advisor-panel{{margin-top:12px;background:#16213e;border:1px solid #0f3460;border-radius:8px;padding:12px}}
#advisor-panel h3{{font-size:.9rem;color:#a0c4ff;margin-bottom:8px}}
#recommended-courses-list{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}}
.rec-chip{{display:inline-flex;align-items:center;gap:6px;background:#0d2137;border:1px solid #1565c0;border-radius:999px;padding:4px 10px;font-size:.72rem;color:#c9d1d9}}
.rec-chip button{{background:none;border:none;color:#ff9a9a;cursor:pointer;font-size:.8rem;line-height:1;padding:0 0 1px}}
#recommended-empty{{font-size:.72rem;color:#8090b0;margin-bottom:10px}}
#recommended-calendar{{margin-bottom:10px}}
#proposed-sections-wrap{{display:none}}
.rec-cal-empty{{font-size:.72rem;color:#8090b0;margin-bottom:8px}}
.rec-cal-wrap{{border:1px solid #283452;border-radius:6px;background:#0f1a33;padding:8px 9px;margin-bottom:7px}}
.rec-cal-term{{font-size:.66rem;color:#a0c4ff;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px;font-weight:600}}
.rec-cal-item{{position:relative;font-size:.72rem;color:#c9d1d9;padding:4px 18px 4px 0;border-bottom:1px dashed #1b2a4a}}
.rec-cal-item:last-child{{border-bottom:none}}
.rec-cal-item strong{{color:#e0e0e0;font-weight:600}}
.rec-cal-item span{{display:block;color:#8fa0c0;font-size:.68rem;margin-top:1px}}
.rec-cal-rm{{position:absolute;top:5px;right:0;background:none;border:none;color:#ff8b8b;cursor:pointer;font-size:.88rem;line-height:1;padding:0}}
.rec-cal-rm:hover{{color:#ff5f5f}}
.rec-mini-wrap{{padding:7px 8px}}
.rec-mini-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px;max-height:210px;overflow:hidden}}
.rec-mini-day{{border:1px solid #1f2e4f;border-radius:5px;overflow:hidden;background:#0b142a}}
.rec-mini-day-hdr{{font-size:.56rem;color:#8ea4ca;text-align:center;padding:2px 0;border-bottom:1px solid #1b2a4a;background:#0f1a33;font-weight:600}}
.rec-mini-day-body{{position:relative;background:linear-gradient(180deg,#0a1224 0%,#0c1730 100%)}}
.rec-mini-block{{position:absolute;left:1px;right:1px;border-radius:3px;padding:1px 12px 1px 3px;background:#163562;border-left:2px solid #2f6bc4;color:#dbe9ff;overflow:hidden;font-size:.52rem;line-height:1.2;display:flex;flex-direction:column;justify-content:center}}
.rec-mini-block{{cursor:pointer;transition:background .12s ease,border-color .12s ease,color .12s ease}}
.rec-mini-block.is-selected{{background:#1d5a2f;border-left-color:#6fd18a;color:#e9ffef;box-shadow:0 0 0 1px rgba(111,209,138,.35) inset}}
.rec-mini-code{{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.rec-mini-meta{{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.48rem;color:#b8cdf3}}
.rec-mini-rm{{position:absolute;top:1px;right:2px;background:none;border:none;color:#ff9f9f;cursor:pointer;font-size:.65rem;line-height:1;padding:0}}
.rec-mini-rm:hover{{color:#ff6b6b}}
.rec-mini-async{{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}}
.rec-mini-async-chip{{display:inline-flex;align-items:center;gap:4px;border:1px solid #2b3e66;border-radius:999px;background:#0b1730;color:#9fb3d7;font-size:.6rem;padding:2px 7px}}
.rec-mini-async-chip{{cursor:pointer;transition:background .12s ease,border-color .12s ease,color .12s ease}}
.rec-mini-async-chip.is-selected{{background:#194528;border-color:#63bf7c;color:#e9ffef}}
.rec-mini-more{{font-size:.6rem;color:#7f94ba;align-self:center}}
#advisor-notes{{width:100%;min-height:78px;resize:vertical;background:#0d1b2e;border:1px solid #1f2b54;border-radius:6px;color:#e0e0e0;padding:8px 10px;font-size:.8rem;font-family:inherit}}
#advisor-notes-hint{{font-size:.7rem;color:#8090b0;margin-top:6px}}
</style>
</head>
<body>
<svg id="arrows-svg"></svg>
<div id="page-header" data-student-id="{student_id}">
  <h1>Curriculum Progress Report</h1>
  <div class="student-name">{student_name}</div>
  <div class="meta">{major} &bull; Start: {start_year}</div>
  <div class="stats-bar">
    <span class="stat"><span class="stat-label">Earned</span><span class="stat-val">{_cr(_earned_cr)} cr</span></span>
    <span class="stat-sep">&bull;</span>
    <span class="stat"><span class="stat-label">In Progress</span><span class="stat-val">{_cr(_inprog_cr)} cr</span></span>
    <span class="stat-sep">&bull;</span>
    <span class="stat"><span class="stat-label">Remaining</span><span class="stat-val">{_cr(_remaining_cr)} cr</span></span>
    {"<span class='stat-sep'>&bull;</span><span class='stat'><span class='stat-label'>GPA</span><span class='stat-val'>" + cum_gpa + "</span></span>" if cum_gpa else ""}
  </div>
</div>
<div id="legend">
  <div class="legend-item"><div class="legend-swatch" style="background:#1b4332;border-color:#2d6a4f"></div>Completed</div>
  <div class="legend-item"><div class="legend-swatch" style="background:#0d2137;border-color:#1565c0"></div>In Progress</div>
  <div class="legend-item"><div class="legend-swatch" style="background:#3e2400;border-color:#ff9800"></div>Below Min Grade</div>
  <div class="legend-item"><div class="legend-swatch" style="background:#252510;border:2px solid #d4aa00"></div>Eligible Now</div>
  <div class="legend-item"><div class="legend-swatch" style="background:#1c1500;border:2px dashed #ff8c00;box-shadow:0 0 0 2px #d4aa00;opacity:.85"></div>Eligible Next Semester</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#1c1c1c;border-color:#333;opacity:.55"></div>Proposed</div>
  <div class="legend-item" style="display:flex;align-items:center;gap:5px"><span style="width:7px;height:7px;background:#cc2222;border-radius:50%;display:inline-block;flex-shrink:0"></span>Prior attempt</div>
  <div class="legend-item"><div class="legend-swatch" style="border-top:2px solid #d4aa00;border-left:2px solid #d4aa00;border-right:2px solid #cc2222;border-bottom:2px solid #cc2222;background:#1f1a0a"></div>Manual Override</div>
</div>
<div id="curriculum-grid">
{grid_html}
</div>
{elects_html}
{add_minor_html}
{minors_html}
{unmapped_html}
{table_html}
<div id="advisor-panel">
    <h3>Recommended Courses (Next Semester/Summer)</h3>
    <div id="recommended-empty">Mark an eligible course with + Next to build an advised path.</div>
    <div id="recommended-courses-list"></div>
    <div id="proposed-sections-wrap">
        <h3>Proposed Sections (BETA)</h3>
        <div id="recommended-calendar"></div>
    </div>
    <h3 style="margin-top:8px">Advising Notes</h3>
    <textarea id="advisor-notes" placeholder="Notes for this advisee and plan..."></textarea>
    <div id="advisor-notes-hint">Notes and recommendations are saved in this browser.</div>
</div>
<script>
(function(){{
  var svg=document.getElementById('arrows-svg');
  var arrowsVisible=true;
    var advisorStorageKey=(function(){{
        var studentName=((document.querySelector('.student-name')||{{textContent:'unknown'}}).textContent||'unknown').trim().replace(/\s+/g,'_');
        var header=document.getElementById('page-header');
        var studentId=((header&&header.getAttribute('data-student-id'))||'unknown').trim().replace(/\s+/g,'_');
        return 'advisingbot-advisor-'+studentName+'-'+studentId+'-__MINOR_SESSION_ID__';
    }})();
    var recState={{items:[]}};
  var courseMap={{}};
  document.querySelectorAll('.course-box[data-cid]').forEach(function(el){{
    var cid=el.getAttribute('data-cid');
    if(cid) courseMap[cid]=el;
  }});
  function getRight(el){{var r=el.getBoundingClientRect();return{{x:r.right,y:r.top+r.height/2}};}}
  function getLeft(el){{var r=el.getBoundingClientRect();return{{x:r.left,y:r.top+r.height/2}};}}
  function statusColor(el){{
    if(el.classList.contains('completed')) return'#2d6a4f';
    if(el.classList.contains('inprog'))    return'#1565c0';
    if(el.classList.contains('belowmin'))  return'#e65100';
    if(el.classList.contains('eligible'))  return'#b8960c';
    return'#3a3a3a';
  }}
  function ensureMarker(color){{
    var id='ah-'+color.replace('#','');
    if(svg.querySelector('#'+id)) return;
    var defs=svg.querySelector('defs');
    if(!defs){{defs=document.createElementNS('http://www.w3.org/2000/svg','defs');svg.insertBefore(defs,svg.firstChild);}}
    var marker=document.createElementNS('http://www.w3.org/2000/svg','marker');
    marker.setAttribute('id',id);marker.setAttribute('markerWidth','8');marker.setAttribute('markerHeight','8');
    marker.setAttribute('refX','7');marker.setAttribute('refY','4');marker.setAttribute('orient','auto');
    var poly=document.createElementNS('http://www.w3.org/2000/svg','polygon');
    poly.setAttribute('points','0 0, 8 4, 0 8');poly.setAttribute('fill',color);poly.setAttribute('fill-opacity','0.75');
    marker.appendChild(poly);defs.appendChild(marker);
  }}
  function makePath(x1,y1,x2,y2,color,dashed){{
    var dx=Math.min(Math.abs(x2-x1)*.45,80);
    var d='M'+x1+','+y1+' C'+(x1+dx)+','+y1+' '+(x2-dx)+','+y2+' '+x2+','+y2;
    var p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',color);
    p.setAttribute('stroke-width','1.4');p.setAttribute('stroke-opacity','0.5');
    p.setAttribute('marker-end','url(#ah-'+color.replace('#','')+')');
    if(dashed) p.setAttribute('stroke-dasharray','5 3');
    p.classList.add('prereq-arrow');
    return p;
  }}
  function drawArrows(){{
    svg.querySelectorAll('.prereq-arrow').forEach(function(e){{e.remove();}});
    document.querySelectorAll('.course-box[data-prereqs]').forEach(function(targetEl){{
      if(targetEl.closest('.minor-section')) return;
      var prereqs,coreqs;
      try{{prereqs=JSON.parse(targetEl.getAttribute('data-prereqs')||'[]');}}catch(e){{prereqs=[];}}
      try{{coreqs=JSON.parse(targetEl.getAttribute('data-coreqs')||'[]');}}catch(e){{coreqs=[];}}
      prereqs.forEach(function(pid){{
        var src=courseMap[pid];if(!src) return;
        var color=statusColor(src);ensureMarker(color);
        var s=getRight(src),t=getLeft(targetEl);
        if(s.x<t.x) svg.appendChild(makePath(s.x,s.y,t.x,t.y,color,false));
      }});
      coreqs.forEach(function(cid){{
        var src=courseMap[cid];if(!src) return;
        var color='#506070';ensureMarker(color);
        var s=getRight(src),t=getLeft(targetEl);
        if(s.x<t.x) svg.appendChild(makePath(s.x,s.y,t.x,t.y,color,true));
      }});
    }});
  }}
  window.toggleArrows=function(){{
    arrowsVisible=!arrowsVisible;
    svg.style.display=arrowsVisible?'':'none';
    var cb=document.getElementById('arrow-toggle-cb');
    if(cb) cb.checked=arrowsVisible;
  }};
  window.addEventListener('load',drawArrows);
  window.addEventListener('resize',drawArrows);
  window.addEventListener('scroll',drawArrows,{{passive:true}});
  // ── Cross-link: course box ↔ table row ───────────────────────────────────
  var wasDragging=false;
  document.addEventListener('dragend',function(){{wasDragging=true;setTimeout(function(){{wasDragging=false;}},250);}});
  function flashEl(el){{
    el.classList.add('flash-hl');
    setTimeout(function(){{el.classList.remove('flash-hl');}},1100);
  }}
  document.querySelectorAll('.course-box[data-cid]').forEach(function(box){{
    box.addEventListener('click',function(e){{
            if(wasDragging||e.target.closest('.undo-btn,.ovr-btn,.rec-btn,.attempt-dots')) return;
      var cid=box.getAttribute('data-cid');
      var td=document.querySelector('td[data-slot-cid="'+cid+'"]');
      if(!td) return;
      var tr=td.closest('tr');
      tr.scrollIntoView({{behavior:'smooth',block:'center'}});
      flashEl(tr);
    }});
  }});
  document.querySelectorAll('td[data-slot-cid]').forEach(function(td){{
    td.addEventListener('click',function(){{
      var cid=td.getAttribute('data-slot-cid');
      var box=document.getElementById('c-'+cid.replace(/[^A-Za-z0-9]/g,'-'))||courseMap[cid];
      if(!box) return;
      box.scrollIntoView({{behavior:'smooth',block:'center'}});
      flashEl(box);
    }});
  }});
  // ── Override drag-drop ────────────────────────────────────────────────────
  var dragState=null;
  document.querySelectorAll('.unmapped-chip').forEach(function(chip){{
    chip.addEventListener('dragstart',function(e){{
      dragState={{cid:chip.getAttribute('data-cid'),cname:chip.getAttribute('data-cname'),chip:chip}};
      e.dataTransfer.effectAllowed='copy';
    }});
  }});
  document.querySelectorAll('.course-box').forEach(function(box){{
    box.addEventListener('dragover',function(e){{
      if(!dragState) return;
      e.preventDefault();e.dataTransfer.dropEffect='copy';
      box.classList.add('drag-over');
    }});
    box.addEventListener('dragleave',function(){{box.classList.remove('drag-over');}});
    box.addEventListener('drop',function(e){{
      if(!dragState) return;
      e.preventDefault();
      box.classList.remove('drag-over');
      var targetCid=box.getAttribute('data-cid');
      var ovCid=dragState.cid;
      var ovCname=dragState.cname;
      // Apply override styling
      box.classList.remove('completed','inprog','belowmin','eligible','locked','nextelig');
      box.classList.add('override');
      box.setAttribute('data-override-cid',ovCid);
      // Update box display
      var cidEl=box.querySelector('.cid');
      var cnEl=box.querySelector('.cname');
      if(cidEl) cidEl.innerHTML=ovCid+'<span style="font-size:.52rem;color:#cc2222;margin-left:3px">\u25b2OVR</span>';
      if(cnEl)  cnEl.textContent=ovCname;
      // Register override in courseMap for arrow drawing
      courseMap[ovCid]=box;
      if(targetCid) courseMap[targetCid]=box;
      // Mark chip as used
      dragState.chip.classList.add('chip-used');
      dragState.chip.removeAttribute('draggable');
      // Inject undo button into box
      var undoBtn=document.createElement('button');
      undoBtn.className='undo-btn';undoBtn.title='Undo override';undoBtn.textContent='\u00d7';
      undoBtn.addEventListener('click',function(e){{
        e.stopPropagation();
        var origStatus=box.getAttribute('data-orig-status')||'locked';
        var origCidVal=box.getAttribute('data-orig-cid')||'';
        var origCnameVal=box.getAttribute('data-orig-cname')||'';
        var chipCid=box.getAttribute('data-override-cid');
        box.classList.remove('override');box.classList.add(origStatus);
        box.removeAttribute('data-override-cid');
        var cidEl2=box.querySelector('.cid');var cnEl2=box.querySelector('.cname');
        if(cidEl2) cidEl2.textContent=origCidVal;
        if(cnEl2)  cnEl2.textContent=origCnameVal;
        undoBtn.remove();
        if(chipCid){{
          delete courseMap[chipCid];
          document.querySelectorAll('.unmapped-chip[data-cid="'+chipCid+'"]').forEach(function(ch){{
            ch.classList.remove('chip-used');ch.setAttribute('draggable','true');
          }});
        }}
        // Revert any courses unlocked solely by this override
        revertUnlocks();
        drawArrows();
      }});
      box.appendChild(undoBtn);
      dragState=null;
      // Update CPR table rows for this slot
      [targetCid,ovCid].filter(Boolean).forEach(function(cid){{
        document.querySelectorAll('td[data-slot-cid="'+cid+'"]').forEach(function(td){{
          td.closest('tr').querySelectorAll('td').forEach(function(c){{
            c.className=c.className.replace(/\\bcell-\\S+/g,'').trim();
            c.classList.add('cell-override');
          }});
          td.innerHTML=ovCid+'<span style="font-size:.58rem;color:#9a7c00;margin-left:3px">OVR</span>';
        }});
      }});
      // Propagate unlocks to downstream locked courses
      propagateUnlocks();
      drawArrows();
    }});
  }});
  function getSatisfied(){{
    var sat=new Set();
    document.querySelectorAll('.course-box[data-cid]').forEach(function(el){{
      if(el.classList.contains('completed')||el.classList.contains('inprog')||el.classList.contains('override')){{
        var cid=el.getAttribute('data-cid');if(cid) sat.add(cid);
        var ov=el.getAttribute('data-override-cid');if(ov) sat.add(ov);
      }}
    }});
    return sat;
  }}
  function revertUnlocks(){{
    var sat=getSatisfied();
    // Re-lock any eligible box whose original status was locked/nextelig and prereqs no longer met
    document.querySelectorAll('.course-box.eligible[data-orig-status]').forEach(function(el){{
      var orig=el.getAttribute('data-orig-status');
      if(orig==='eligible') return; // was naturally eligible, keep it
      var pre;try{{pre=JSON.parse(el.getAttribute('data-prereqs')||'[]');}}catch(e){{pre=[];}}
      if(!pre.length) return;
      if(!pre.every(function(p){{return sat.has(p);}})){{
        el.classList.remove('eligible');el.classList.add(orig||'locked');
        // Sync table
        var cid2=el.getAttribute('data-cid');
        document.querySelectorAll('td[data-slot-cid="'+cid2+'"]').forEach(function(td2){{
          td2.closest('tr').querySelectorAll('td').forEach(function(c){{
            if(c.classList.contains('cell-eligible')){{
              c.className=c.className.replace(/\\bcell-eligible\\b/,'cell-locked').trim();
            }}
          }});
        }});
      }}
    }});
    // Revert override's table rows back to original status
  }}
  // ── Click-to-override (yellow / grey boxes) ───────────────────────────
  function _applyClickOverride(box){{
    var ovCid=box.getAttribute('data-cid')||'';
    if(!ovCid) return;
    box.classList.remove('completed','inprog','belowmin','eligible','locked','nextelig');
    box.classList.add('override');
    box.setAttribute('data-override-cid',ovCid);
    courseMap[ovCid]=box;
    var cidEl=box.querySelector('.cid');
    if(cidEl) cidEl.innerHTML=ovCid+'<span style="font-size:.52rem;color:#cc2222;margin-left:3px">\u25b2OVR</span>';
    // hide the trigger button
    var ob=box.querySelector('.ovr-btn'); if(ob) ob.style.display='none';
    // undo button
    var undoBtn=document.createElement('button');
    undoBtn.className='undo-btn'; undoBtn.title='Remove override'; undoBtn.textContent='\u00d7';
    undoBtn.addEventListener('click',function(e){{
      e.stopPropagation();
      var origStatus=box.getAttribute('data-orig-status')||'locked';
      var origCid=box.getAttribute('data-orig-cid')||ovCid;
      box.classList.remove('override'); box.classList.add(origStatus);
      box.removeAttribute('data-override-cid');
      var cidEl2=box.querySelector('.cid'); if(cidEl2) cidEl2.textContent=origCid;
      delete courseMap[ovCid];
      undoBtn.remove();
      var ob2=box.querySelector('.ovr-btn'); if(ob2) ob2.style.display='';
      // restore CPR table row colour
      document.querySelectorAll('td[data-slot-cid="'+ovCid+'"]').forEach(function(td){{
        td.closest('tr').querySelectorAll('td').forEach(function(c){{
          c.className=c.className.replace(/\\bcell-override\\b/,'cell-'+origStatus).trim();
        }});
        td.textContent=origCid;
      }});
      revertUnlocks(); drawArrows();
    }});
    box.appendChild(undoBtn);
    // CPR table row
    document.querySelectorAll('td[data-slot-cid="'+ovCid+'"]').forEach(function(td){{
      td.closest('tr').querySelectorAll('td').forEach(function(c){{
        c.className=c.className.replace(/\\bcell-\\S+/g,'').trim();
        c.classList.add('cell-override');
      }});
      td.innerHTML=ovCid+'<span style="font-size:.58rem;color:#9a7c00;margin-left:3px">OVR</span>';
    }});
    propagateUnlocks(); drawArrows();
  }}
  // Inject trigger button into every eligible/locked/nextelig box
  document.querySelectorAll('.course-box.eligible,.course-box.locked,.course-box.nextelig').forEach(function(box){{
    var ob=document.createElement('button');
    ob.className='ovr-btn'; ob.title='Mark as manually verified (advisor confirmed)'; ob.textContent='\u2713 override';
    ob.addEventListener('click',function(e){{ e.stopPropagation(); _applyClickOverride(box); }});
    box.appendChild(ob);
  }});
  function propagateUnlocks(){{
    var sat=getSatisfied();
    var changed=true;
    while(changed){{
      changed=false;
      document.querySelectorAll('.course-box.locked,.course-box.nextelig').forEach(function(el){{
        var pre;try{{pre=JSON.parse(el.getAttribute('data-prereqs')||'[]');}}catch(e){{pre=[];}}
        if(!pre.length) return;
        if(pre.every(function(p){{return sat.has(p);}})){{
          el.classList.remove('locked','nextelig');el.classList.add('eligible');
          changed=true;
        }}
      }});
    }}
    // Sync table: locked→eligible for any newly eligible boxes
    document.querySelectorAll('.course-box.eligible[data-cid]').forEach(function(el){{
      var cid=el.getAttribute('data-cid');
      document.querySelectorAll('td[data-slot-cid="'+cid+'"]').forEach(function(td){{
        td.closest('tr').querySelectorAll('td').forEach(function(c){{
          if(c.classList.contains('cell-locked')){{
            c.className=c.className.replace(/\\bcell-locked\\b/,'cell-eligible').trim();
          }}
        }});
      }});
    }});
  }}

    // ── Advisor recommendations + notes ─────────────────────────────────────
    function saveAdvisorState(){{
        try{{
            var notesEl=document.getElementById('advisor-notes');
            var payload={{items:recState.items,notes:notesEl?notesEl.value:''}};
            sessionStorage.setItem(advisorStorageKey,JSON.stringify(payload));
        }}catch(e){{}}
    }}
    function loadAdvisorState(){{
        try{{
            var raw=sessionStorage.getItem(advisorStorageKey);
            if(!raw) return;
            var parsed=JSON.parse(raw);
            recState.items=Array.isArray(parsed.items)?parsed.items:[];
            var notesEl=document.getElementById('advisor-notes');
            if(notesEl && typeof parsed.notes==='string') notesEl.value=parsed.notes;
        }}catch(e){{ recState.items=[]; }}
    }}
    function upsertRecommended(item){{
        var idx=recState.items.findIndex(function(x){{return x.cid===item.cid;}});
        if(idx>=0) recState.items[idx]=item;
        else recState.items.push(item);
    }}
    function removeRecommended(cid){{
        recState.items=recState.items.filter(function(x){{return x.cid!==cid;}});
    }}
    function currentRecommendableSet(){{
        var s=new Set();
        document.querySelectorAll('.course-box.eligible[data-cid],.course-box.nextelig[data-cid],.course-box.belowmin[data-cid]').forEach(function(el){{
            var cid=(el.getAttribute('data-cid')||'').trim();
            if(cid) s.add(cid);
        }});
        return s;
    }}
    function pruneRecommendationsToCurrent(){{
        var allowed=currentRecommendableSet();
        recState.items=(recState.items||[]).filter(function(it){{
            return it && it.cid && allowed.has(String(it.cid).trim());
        }});
    }}
    function renderRecommended(){{
        var list=document.getElementById('recommended-courses-list');
        var empty=document.getElementById('recommended-empty');
        if(!list||!empty) return;
        if(!recState.items.length){{
            list.innerHTML='';
            empty.style.display='block';
        }}else{{
            empty.style.display='none';
            list.innerHTML=recState.items.map(function(it){{
                var term=it.termLabel?(' · '+it.termLabel):'';
                return '<span class="rec-chip" data-cid="'+it.cid+'">'+it.cid+' — '+it.cname+term
                    +'<button type="button" data-remove="'+it.cid+'">×</button></span>';
            }}).join('');
            list.querySelectorAll('button[data-remove]').forEach(function(btn){{
                btn.addEventListener('click',function(e){{
                    e.stopPropagation();
                    var cid=btn.getAttribute('data-remove');
                    removeRecommended(cid);
                    document.querySelectorAll('.course-box[data-cid="'+cid+'"]').forEach(function(box){{
                        box.classList.remove('is-recommended');
                        var rb=box.querySelector('.rec-btn');
                        if(rb) rb.textContent='+ Next';
                    }});
                    renderRecommended();
                    saveAdvisorState();
                }});
            }});
        }}
    }}
    function bindRecommendButtons(){{
        document.querySelectorAll('.course-box .rec-btn').forEach(function(btn){{
            btn.addEventListener('click',function(e){{
                e.stopPropagation();
                var box=btn.closest('.course-box');
                if(!box) return;
                var cid=(box.getAttribute('data-cid')||'').trim();
                if(!cid) return;
                var cname=((box.querySelector('.cname')||{{textContent:''}}).textContent||'').trim();
                var termLabel=(box.getAttribute('data-term-label')||'').trim();
                var active=box.classList.contains('is-recommended');
                if(active){{
                    box.classList.remove('is-recommended');
                    btn.textContent='+ Next';
                    removeRecommended(cid);
                }}else{{
                    box.classList.add('is-recommended');
                    btn.textContent='Added';
                    upsertRecommended({{cid:cid,cname:cname,termLabel:termLabel}});
                }}
                renderRecommended();
                saveAdvisorState();
            }});
        }});
    }}

    loadAdvisorState();
    pruneRecommendationsToCurrent();
    bindRecommendButtons();
    recState.items.forEach(function(it){{
        document.querySelectorAll('.course-box[data-cid="'+it.cid+'"]').forEach(function(box){{
            if(!(box.classList.contains('eligible')||box.classList.contains('nextelig')||box.classList.contains('belowmin'))) return;
            box.classList.add('is-recommended');
            var rb=box.querySelector('.rec-btn');
            if(rb) rb.textContent='Added';
        }});
    }});
    renderRecommended();
    saveAdvisorState();
    var notesEl=document.getElementById('advisor-notes');
    if(notesEl) notesEl.addEventListener('input',saveAdvisorState);
}})();
// ── Pool slot dropdowns ──────────────────────────────────────────────────
function poolSlotClick(evt, el) {{
  evt.stopPropagation();
  var dd = el.querySelector('.pool-dropdown');
  var wasShown = dd.classList.contains('show');
  document.querySelectorAll('.pool-dropdown.show').forEach(function(d){{d.classList.remove('show');}});
  if (!wasShown) dd.classList.add('show');
}}
function poolOptSelect(evt, optEl) {{
  evt.stopPropagation();
  var slot = optEl.closest('.pool-slot');
  var poolId = slot.getAttribute('data-pool-id');
  var prevCid = slot.getAttribute('data-chosen');
  var cid     = optEl.getAttribute('data-cid');
  var cname   = optEl.getAttribute('data-cname');
  var credits = optEl.getAttribute('data-credits');
  // Restore previously chosen option in all sibling slots
  if (prevCid) {{
    document.querySelectorAll('.pool-slot[data-pool-id="' + poolId + '"] .pool-opt-item[data-cid="' + prevCid + '"]')
      .forEach(function(o){{ o.style.display=''; }});
  }}
  // Update this slot's display
  slot.querySelector('.cid').textContent = cid || '—';
  slot.querySelector('.cname').textContent = cname;
  slot.querySelector('.meta').innerHTML = credits ? '<span class="credits">' + credits + ' cr</span>' : '';
  slot.setAttribute('data-chosen', cid);
  slot.classList.add('pool-chosen');
  slot.querySelector('.pool-dropdown').classList.remove('show');
  // Hide chosen option in all sibling slots
  document.querySelectorAll('.pool-slot[data-pool-id="' + poolId + '"] .pool-opt-item[data-cid="' + cid + '"]')
    .forEach(function(o){{ o.style.display='none'; }});
}}
document.addEventListener('click', function() {{
  document.querySelectorAll('.pool-dropdown.show').forEach(function(d){{d.classList.remove('show');}});
}});
</script>
</body>
</html>
"""


def _safe_filename(s: str) -> str:
    """Strip characters unsafe for filenames."""
    return re.sub(r"[^\w\-]", "_", str(s or "")).strip("_") or "Unknown"


def generate_html(csv_path: str) -> str:
    """Read a filled_pathway CSV and write a _cpr.html next to it. Returns output path."""
    df   = pd.read_csv(str(csv_path), engine="python")
    html = build_html(df)

    # Build a descriptive filename: LastName_Year_Track_cpr.html
    parent = Path(csv_path).parent
    sname  = ""
    for col in ["student_name", "full_name"]:
        if col in df.columns:
            v = df[col].dropna().astype(str)
            v = v[v.str.strip().ne("") & v.str.lower().ne("nan")]
            if not v.empty:
                sname = v.iloc[0]; break
    last_name = _safe_filename(sname.split()[-1]) if sname.strip() else "Unknown"

    yr = ""
    if "student_start_year" in df.columns:
        yv = df["student_start_year"].dropna().astype(str).str.strip()
        yv = yv[yv.ne("") & yv.ne("nan")]
        yr = yv.iloc[0] if not yv.empty else ""

    track = ""
    for col in ["source_track", "plan_short"]:
        if col in df.columns:
            tv = df[col].dropna().astype(str).str.strip()
            tv = tv[tv.ne("") & tv.ne("nan")]
            if not tv.empty:
                track = _safe_filename(tv.iloc[0]); break

    parts = [p for p in [last_name, yr, track] if p]
    fname = "_".join(parts) + "_cpr.html"
    out_path = str(parent / fname)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def run_pipeline(pdf_path: str, log_fn=print) -> str:
    """Run all three steps. Returns path to the generated HTML file."""
    pdf_path = Path(pdf_path)

    log_fn("Step 1/3 — Parsing PDF transcript...")
    csv_path = convert_pdf_to_csv(pdf_path)
    log_fn(f"  \u2713 {csv_path.name}")

    log_fn("Step 2/3 — Mapping to curriculum...")
    filled_path = fill_pathway(csv_path)
    log_fn(f"  \u2713 {filled_path.name}")

    log_fn("Step 3/3 — Generating HTML map...")
    html_path = generate_html(str(filled_path))
    log_fn(f"  \u2713 {Path(html_path).name}")

    return html_path


# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

class AdvisingBotApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("AdvisingBot")
        root.geometry("500x340")
        root.resizable(False, False)
        root.configure(bg="#1a1a2e")

        tk.Label(root, text="AdvisingBot", font=("Segoe UI", 15, "bold"),
                 bg="#1a1a2e", fg="#a0c4ff").pack(pady=(18, 2))
        tk.Label(root, text="Transcript PDF \u2192 Curriculum Map",
                 font=("Segoe UI", 9), bg="#1a1a2e", fg="#607090").pack(pady=(0, 14))

        self.btn = tk.Button(
            root, text="Select PDF & Run",
            font=("Segoe UI", 11, "bold"),
            bg="#0f3460", fg="#a0c4ff",
            activebackground="#1565c0", activeforeground="#ffffff",
            relief="flat", bd=0, padx=20, pady=10,
            command=self._on_click,
        )
        self.btn.pack(pady=(0, 12))

        log_frame = tk.Frame(root, bg="#16213e", bd=0)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word",
            bg="#0d1117", fg="#c9d1d9",
            font=("Consolas", 8), bd=0, insertbackground="#c9d1d9",
            state="disabled",
        )
        self.log_box.pack(fill="both", expand=True, padx=1, pady=1)

    def _log(self, msg: str):
        """Append a line to the log box (thread-safe via after())."""
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _append)

    def _set_btn(self, enabled: bool):
        self.root.after(0, lambda: self.btn.configure(
            state="normal" if enabled else "disabled",
            text="Select PDF & Run" if enabled else "Running…",
        ))

    def _on_click(self):
        pdf_path = filedialog.askopenfilename(
            title="Select transcript PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not pdf_path:
            return
        self._set_btn(False)
        # Clear log
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        threading.Thread(target=self._run, args=(pdf_path,), daemon=True).start()

    def _run(self, pdf_path: str):
        try:
            html_path = run_pipeline(pdf_path, self._log)
            abs_path  = os.path.abspath(html_path)
            webbrowser.open(f"file://{abs_path}")
            self._log(f"\nDone! Opened in browser.\n{abs_path}")
        except Exception as e:
            self._log(f"\nError: {e}")
            self.root.after(0, lambda: messagebox.showerror("AdvisingBot", str(e)))
        finally:
            self._set_btn(True)


def main():
    import time
    try:
        from web_app import app as flask_app
    except ImportError:
        # Flask not installed — fall back to Tkinter GUI
        if not HAS_TKINTER:
            print("Neither Flask nor Tkinter available. Cannot start.")
            return
        root = tk.Tk()
        AdvisingBotApp(root)
        root.mainloop()
        return

    host, port = "127.0.0.1", 5000
    threading.Thread(
        target=lambda: flask_app.run(host=host, port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    time.sleep(0.8)  # brief pause for Flask to bind
    webbrowser.open(f"http://{host}:{port}")
    print(f"AdvisingBot running at http://{host}:{port}  —  press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
