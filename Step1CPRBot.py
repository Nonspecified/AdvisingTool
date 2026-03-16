# Transcript PDF → One CSV (courses + student header per row) with transfer fallback
# pip install PyPDF2

import csv
import re
from pathlib import Path
from tkinter import Tk, Button, messagebox, filedialog
from PyPDF2 import PdfReader
from datetime import datetime, date

HEADERS = [
    "course_id","course_name","term","term_code","grade","status",
    "attempted_credits","earned_credits","grade_points",
    "is_transfer","transfer_from","transfer_effective_term","transfer_effective_date",
    "program","plan","plan_short",
    "full_name","first_name","last_name","student_id","email","transcript_date","source_file"
]

# -------- regex --------
TERM_HEADER_RE = re.compile(r"^\s*(\d{4})\s+(Fall|Spring|Summer|Winter)\s*$", re.I)
PROGRAM_RE = re.compile(r"^\s*Program:\s*(?P<program>.+?)\s*$", re.I)
PLAN_RE = re.compile(r"^\s*Plan:\s*(?P<plan>.+?)\s*$", re.I)

COURSE_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<earned>\d+\.\d{2})"
    r"(?:\s+(?P<grade>(?:A|B|C|D|F|P|S|U|IP|I|W)(?:[+-])?))?"
    r"\s+(?P<points>\d+\.\d{3})\s*$"
)
# Target-course after "Transferred to Term ..."
COURSE_TRANSFER_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<grade>(?:T|A|B|C|D|F|P|S|U)(?:[+-])?)\s*$"
)
# Incoming course fallback when no "Transferred to Term ..." is present
INCOMING_TRANSFER_ROW_RE = re.compile(
    r"^(?P<course_id>[A-Z]{2,5}\s+\d{4}[A-Z]?)\s+"
    r"(?P<course_name>.+?)\s+"
    r"(?P<attempted>\d+\.\d{2})\s+"
    r"(?P<grade>(?:T|A|B|C|D|F|P|S|U)(?:[+-])?)"
    r"(?:\s+.*)?$"  # ignore trailing source-term tokens like "2025 SUMR SEM"
)

TRANSFER_FROM_RE = re.compile(r"^Transfer Credit from\s+(?P<inst>.+?)\s*$", re.I)
TRANSFER_TO_TERM_RE = re.compile(
    r"^Transferred\s+to\s+Term\s+(?P<year>\d{4})\s+(?P<term>Fall|Spring|Summer|Winter)\s+as\s*$",
    re.I
)
REPEAT_FLAG_RE = re.compile(r"^Repeated:", re.I)

NAME_RE = re.compile(r"^\s*Name:\s*(?P<name>.+?)\s*$", re.I)
STUDENT_ID_RE = re.compile(r"^\s*Student ID:\s*(?P<sid>.+?)\s*$", re.I)
EMAIL_INLINE_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
DATE_LINE_RE = re.compile(r"^\s*(?P<mdy>\d{1,2}/\d{1,2}/\d{4})\s*$")

SKIP_LINE_HINTS = (
    "Course Description Attempted Earned Grade Points",
    "Attempted Earned GPA", "UnitsPoints",
    "Term GPA:", "Cum GPA:", "Undergraduate Career Totals",
    "End of", "Beginning of Undergraduate Record",
    "Incoming  Course",
)

# -------- helpers --------
def term_to_code(year: int, term: str) -> str:
    mm = {"Spring": "SP", "Summer": "SU", "Fall": "FA", "Winter": "WI"}
    return f"{year}{mm[term]}"

def term_start_date(year: int, term: str) -> date:
    month = {"Spring": 1, "Summer": 5, "Fall": 9, "Winter": 12}[term]
    return date(year, month, 1)

def classify_status(grade: str | None, points: str | None, is_latest_term: bool) -> str:
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

def infer_plan_short(plan: str) -> str:
    p = plan.lower() if plan else ""
    if "industrial engineering" in p:
        return "IE"
    if "mechanical engineering" in p:
        return "ME"
    return "OTHER" if p else ""

def extract_text(pdf_path: Path) -> list[str]:
    reader = PdfReader(str(pdf_path))
    lines = []
    for page in reader.pages:
        t = page.extract_text() or ""
        lines.extend([ln.rstrip() for ln in t.splitlines()])
    return lines

# -------- parsing --------
def parse_student_header(lines: list[str], source_file: str) -> dict:
    full_name = ""
    student_id = ""
    email = ""
    tdate_iso = ""

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

    first_name, last_name = "", ""
    if full_name:
        parts = [p for p in full_name.replace(",", " ").split() if p]
        if len(parts) >= 2:
            first_name, last_name = parts[0], parts[-1]
        elif len(parts) == 1:
            first_name = parts[0]

    return {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "student_id": student_id,
        "email": email,
        "transcript_date": tdate_iso,
        "source_file": source_file,
    }

def parse_courses(lines: list[str]) -> list[dict]:
    rows = []
    current_term = None
    current_term_code = None

    term_positions = [i for i, ln in enumerate(lines) if TERM_HEADER_RE.match(ln)]
    latest_term_index = term_positions[-1] if term_positions else -1

    in_transfer_block = False
    transfer_from = ""
    transfer_effective_term = ""
    transfer_effective_date = ""
    pending_transfer_target = False

    current_program = ""
    current_plan = ""

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
            current_term = f"{year} {term}"
            current_term_code = term_to_code(year, term)
            in_transfer_block = False
            transfer_from = ""
            pending_transfer_target = False
            current_program = ""
            current_plan = ""
            continue

        mprog = PROGRAM_RE.match(ln)
        if mprog:
            current_program = mprog.group("program").strip()
            continue
        mplan = PLAN_RE.match(ln)
        if mplan:
            current_plan = mplan.group("plan").strip()
            continue

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
                transfer_effective_date = term_start_date(y, t).isoformat()
                pending_transfer_target = True
                continue

            # If we have a target term pending, parse the target course
            if pending_transfer_target:
                mtc = COURSE_TRANSFER_RE.match(ln)
                if mtc:
                    course_id = mtc.group("course_id").strip()
                    course_name = mtc.group("course_name").strip()
                    attempted = mtc.group("attempted").strip()
                    grade = mtc.group("grade").strip()
                    y, t = transfer_effective_term.split()
                    term_code = term_to_code(int(y), t)
                    rows.append({
                        "course_id": course_id,
                        "course_name": course_name,
                        "term": transfer_effective_term,
                        "term_code": term_code,
                        "grade": grade,
                        "status": "transfer",
                        "attempted_credits": attempted,
                        "earned_credits": attempted,
                        "grade_points": "",
                        "is_transfer": "1",
                        "transfer_from": transfer_from,
                        "transfer_effective_term": transfer_effective_term,
                        "transfer_effective_date": transfer_effective_date,
                        "program": "",
                        "plan": "",
                        "plan_short": "",
                    })
                    pending_transfer_target = False
                continue

            # Fallback: no "Transferred to Term" seen, but we see an incoming course line
            minc = INCOMING_TRANSFER_ROW_RE.match(ln)
            if minc:
                course_id = minc.group("course_id").strip()
                course_name = minc.group("course_name").strip()
                attempted = minc.group("attempted").strip()
                grade = minc.group("grade").strip()
                rows.append({
                    "course_id": course_id,
                    "course_name": course_name,
                    "term": "Transfer",
                    "term_code": "TR",
                    "grade": grade,
                    "status": "transfer",
                    "attempted_credits": attempted,
                    "earned_credits": attempted,
                    "grade_points": "",
                    "is_transfer": "1",
                    "transfer_from": transfer_from,
                    "transfer_effective_term": "Unknown",
                    "transfer_effective_date": "",
                    "program": "",
                    "plan": "",
                    "plan_short": "",
                })
                continue

            continue  # other transfer lines ignored

        mcourse = COURSE_RE.match(ln)
        if mcourse and current_term:
            course_id = mcourse.group("course_id").strip()
            course_name = mcourse.group("course_name").strip()
            attempted = mcourse.group("attempted").strip()
            earned = mcourse.group("earned").strip()
            grade = (mcourse.group("grade") or "").strip()
            points = mcourse.group("points").strip()

            preceding_terms = [p for p in term_positions if p <= idx]
            is_latest_term = bool(preceding_terms and preceding_terms[-1] == latest_term_index)

            status = classify_status(grade or None, points or None, is_latest_term)
            rows.append({
                "course_id": course_id,
                "course_name": course_name,
                "term": current_term,
                "term_code": current_term_code,
                "grade": grade,
                "status": status,
                "attempted_credits": attempted,
                "earned_credits": earned,
                "grade_points": points,
                "is_transfer": "0",
                "transfer_from": "",
                "transfer_effective_term": "",
                "transfer_effective_date": "",
                "program": current_program,
                "plan": current_plan,
                "plan_short": infer_plan_short(current_plan) if current_plan else "",
            })
            continue

        if REPEAT_FLAG_RE.search(ln):
            continue

    return rows

# -------- IO --------
def save_csv(rows: list[dict], out_path: Path, student_meta: dict):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        for r in rows:
            r_out = r.copy()
            r_out.update(student_meta)
            w.writerow({k: r_out.get(k, "") for k in HEADERS})

def convert_pdf_to_csv(pdf_path: Path) -> Path:
    lines = extract_text(pdf_path)
    student_meta = parse_student_header(lines, source_file=str(pdf_path.name))
    rows = parse_courses(lines)
    out_path = pdf_path.with_suffix(".csv")
    save_csv(rows, out_path, student_meta)
    return out_path

# -------- GUI --------
def pick_and_convert():
    pdf_file = filedialog.askopenfilename(
        title="Select transcript PDF",
        filetypes=[("PDF files", "*.pdf")],
    )
    if not pdf_file:
        return
    try:
        out_path = convert_pdf_to_csv(Path(pdf_file))
        messagebox.showinfo("Done", f"Saved CSV:\n{out_path}")
    except Exception as e:
        messagebox.showerror("Error", str(e))

def main():
    root = Tk()
    root.title("Transcript PDF → CSV")
    root.geometry("360x120")
    Button(root, text="Choose PDF and convert", command=pick_and_convert, width=32, height=2).pack(pady=25)
    root.mainloop()

if __name__ == "__main__":
    main()