# CPReval.py — Fill a curriculum map from a student's transcript
# Adds AH/SS + TechElective mapping, prereq robustness, and viz_status

from tkinter import Tk, Button, filedialog, messagebox
from pathlib import Path
import pandas as pd
import re
import argparse

# -------- helpers --------
PASS_LETTERS = {"A","A-","B+","B","B-","C+","C","C-","D+","D","D-","P","S","T","CR"}
STATUS_PRIO = {"passed": 4, "transfer": 4, "in_progress": 3, "completed": 3, "failed": 2, "withdrawn": 1, "unknown": 0}
TERM_ORDER = {"WI":1,"SP":2,"SU":3,"FA":4}

GRADE_SCORE = {"A":13,"A-":12,"B+":11,"B":10,"B-":9,"C+":8,"C":7,"C-":6,"D+":5,"D":4,"D-":3,"F":0,"W":-1}
PASS_SPECIAL = {"P","S","T","CR"}

_GTOK_RE = re.compile(r"\b(A-?|B\+?|B-?|C\+?|C-?|D\+?|D-?|F|W|P|S|T|CR)\b", re.I)
def _clean_grade_token(s: str) -> str:
    s = "" if s is None else str(s)
    m = _GTOK_RE.search(s)
    return m.group(0).upper() if m else ""

def norm_id(s: str) -> str:
    t = str(s).upper().replace(".", " ").strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^([A-Z&]+)\s*([0-9][0-9A-Z]*)$", r"\1 \2", t)
    return t

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

def latest_by_term(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series({})
    return df.sort_values("term_code", key=lambda s: s.astype(str).map(term_key)).iloc[-1]

def term_key(tc: str):
    if not isinstance(tc, str) or len(tc) < 6:
        return (9999, 9)
    y = int(tc[:4]); t = TERM_ORDER.get(tc[4:].upper(), 9)
    return (y, t)

def attempt_status(row) -> str:
    st = str(row.get("status","")).lower().strip()
    g  = _clean_grade_token(row.get("grade",""))
    if st == "transfer" or g == "T": return "passed"
    if st in {"withdrawn","w"} or g == "W": return "withdrawn"
    if st in {"in_progress","ip","inprogress"}: return "in_progress"
    if g in PASS_LETTERS: return "passed"
    if g: return "failed"
    return "unknown"

def first_class_year(df: pd.DataFrame) -> int | None:
    if "term_code" not in df.columns: return None
    is_tr = pd.Series(False, index=df.index)
    if "is_transfer" in df.columns:
        is_tr |= df["is_transfer"].astype(str).str.lower().isin({"1","true","t"})
    if "status" in df.columns:
        is_tr |= df["status"].astype(str).str.lower().eq("transfer")
    pool = df.loc[~is_tr, "term_code"].dropna().astype(str)
    if pool.empty: pool = df["term_code"].dropna().astype(str)
    if pool.empty: return None
    earliest = sorted(pool, key=term_key)[0]
    return int(earliest[:4])

def infer_major(df: pd.DataFrame) -> str:
    if "plan_short" in df.columns:
        vals = df["plan_short"].dropna().astype(str).str.upper()
        for key in ("ME","IE"):
            if (vals == key).any(): return key
    plan_vals = df.get("plan", pd.Series(dtype=str)).dropna().astype(str).str.lower()
    if plan_vals.str.contains("mechanical engineering").any(): return "ME"
    if plan_vals.str.contains("industrial engineering").any(): return "IE"
    return "UNKNOWN"

def detect_track(major: str, first_year: int | None) -> str:
    if major not in {"ME","IE"} or first_year is None: return "UNKNOWN"
    period = "2025plus" if first_year >= 2025 else "pre2025"
    return f"{major}_{period}"

def best_attempts(transcript_df: pd.DataFrame) -> pd.DataFrame:
    df = transcript_df.copy()
    df["course_id_norm"] = df["course_id"].map(norm_id)
    if "attempt_status" not in df.columns:
        df["attempt_status"] = df.apply(attempt_status, axis=1)
    df["__prio"] = df["attempt_status"].map(STATUS_PRIO).fillna(0)
    df["__y"] = df["term_code"].astype(str).map(lambda s: term_key(s)[0])
    df["__t"] = df["term_code"].astype(str).map(lambda s: term_key(s)[1])
    df = df.sort_values(["course_id_norm","__prio","__y","__t"],
                        ascending=[True, False, True, True])
    best = df.drop_duplicates("course_id_norm", keep="first").drop(columns=["__prio","__y","__t"])
    return best.set_index("course_id_norm")

def pick_curriculum_csv(track: str) -> Path | None:
    fname = {
        "ME_2025plus": "curriculum_ME_2025plus.csv",
        "ME_pre2025": "curriculum_ME_pre2025.csv",
        "IE_2025plus": "curriculum_IE_2025plus.csv",
        "IE_pre2025": "curriculum_IE_pre2025.csv",
    }.get(track)
    if not fname: return None
    p = Path(fname)
    return p if p.exists() else None

# -------- catalogs --------
def load_catalog_ids(csv_path: Path) -> set[str]:
    if not csv_path.exists(): return set()
    df = pd.read_csv(csv_path, engine="python")
    if "course_id" in df.columns:
        ids = df["course_id"].dropna().astype(str)
    else:
        ids = df[df.columns[0]].dropna().astype(str)
    return set(ids.map(norm_id))

def choose_bucket_assignment(candidates_df: pd.DataFrame, allowed_ids: set[str], used: set[str]) -> str | None:
    if candidates_df.empty or not allowed_ids: return None
    pool = candidates_df[candidates_df["course_id_norm"].isin(allowed_ids)]
    if pool.empty: return None
    pool = pool.sort_values(["attempt_status","term_code"],
                            ascending=[False, True],
                            key=lambda c: c.map(STATUS_PRIO) if c.name=="attempt_status" else c.map(term_key))
    for cid in pool["course_id_norm"]:
        if cid not in used:
            return cid
    return None

def te_catalog_path(major: str) -> Path | None:
    p = Path("ME_TE.csv") if major=="ME" else Path("IE_TE.csv") if major=="IE" else None
    return p if (p and p.exists()) else None

def load_te_ids(csv_path: Path) -> set[str]:
    if not csv_path or not csv_path.exists(): return set()
    df = pd.read_csv(csv_path, engine="python")
    col = "course_id" if "course_id" in df.columns else df.columns[0]
    ids = df[col].dropna().astype(str).map(norm_id)
    if "category" in df.columns:
        mask = df["category"].astype(str).str.strip().str.lower().eq("techelective")
        ids = ids[mask]
    return set(ids)

# -------- TE rules --------
def load_te_rules(path: Path = Path("TE_Rules.csv")) -> pd.DataFrame | None:
    if not path.exists(): return None
    df = pd.read_csv(path, engine="python")
    for c in ["major","rule_type","value","effect","applies_to_bucket","priority"]:
        if c not in df.columns: df[c] = ""
    df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(0).astype(int)
    df["major"] = df["major"].astype(str).str.upper().str.strip()
    df["rule_type"] = df["rule_type"].astype(str).str.lower().str.strip()
    df["effect"] = df["effect"].astype(str).str.lower().str.strip()
    df["applies_to_bucket"] = df["applies_to_bucket"].astype(str).str.strip()
    df["value"] = df["value"].astype(str)
    return df

def te_allowed_by_rules(course_id_norm: str, major: str, rules_df: pd.DataFrame | None) -> bool:
    if rules_df is None: return True
    rsub = rules_df[(rules_df["major"].isin([major.upper(),""])) &
                    (rules_df["applies_to_bucket"].isin(["TechElective",""]))].copy()
    if rsub.empty: return True
    rsub = rsub.sort_values("priority", ascending=False)
    for _, r in rsub.iterrows():
        rt = r["rule_type"]; val = str(r["value"])
        if rt == "course_id":
            if norm_id(val) == course_id_norm:
                return r["effect"] != "ban"
        elif rt == "prefix":
            pref = norm_id(val).split(" ")[0]
            if course_id_norm.startswith(pref+" "):
                return r["effect"] != "ban"
        elif rt == "regex":
            try:
                if re.search(val, course_id_norm):
                    return r["effect"] != "ban"
            except re.error:
                continue
    return True

# -------- core --------
def fill_pathway(transcript_csv: Path) -> Path:
    tx = pd.read_csv(transcript_csv, engine="python")
    if "course_id" not in tx.columns:
        raise ValueError("Transcript missing 'course_id' column.")
    tx["course_id_norm"] = tx["course_id"].map(norm_id)
    tx["grade_tok"] = tx["grade"].map(_clean_grade_token)
    tx["attempt_status"] = tx.apply(attempt_status, axis=1)

    major = infer_major(tx)
    start_year = first_class_year(tx)
    track = detect_track(major, start_year)

    cur_path = pick_curriculum_csv(track)
    if cur_path is None:
        raise FileNotFoundError(f"No curriculum CSV found for track '{track}'.")
    cur = pd.read_csv(cur_path, engine="python")
    expected = ["term_id","term_label","course_id","course_name","credits","bucket","prereq","coreq","min_grade","notes"]
    for c in expected:
        if c not in cur.columns: cur[c] = ""
    cur["course_id_norm"] = cur["course_id"].map(norm_id)

    best_map = best_attempts(tx)
    ah_ids = load_catalog_ids(Path("AHElectives.csv"))
    ss_ids = load_catalog_ids(Path("SSelectives.csv"))
    te_ids = load_te_ids(te_catalog_path(major))
    te_rules = load_te_rules(Path("TE_Rules.csv"))
    if te_ids:
        te_ids = {cid for cid in te_ids if te_allowed_by_rules(cid, major, te_rules)}

    tx_usable = tx[tx["attempt_status"].isin(["passed","in_progress"])].copy()

    out_rows = []
    for _, row in cur.iterrows():
        cidn = row["course_id_norm"]
        bucket = str(row.get("bucket","")).strip()
        filled = {
            "term_id": row["term_id"], "term_label": row["term_label"],
            "slot_course_id": row["course_id"], "slot_course_name": row["course_name"],
            "credits": row.get("credits",""), "bucket": bucket,
            "prereq": row.get("prereq",""), "coreq": row.get("coreq",""),
            "min_grade": row.get("min_grade",""), "min_grade_required": row.get("min_grade",""),
            "notes": row.get("notes",""),
            "match_course_id": "", "match_course_name": "",
            "match_term_code": "", "match_grade": "",
            "match_status": "open", "meets_min_grade": "",
            "source_track": track
        }

        if bucket not in {"GENED_AH","GENED_SS","TechElective"}:
            att = tx[tx["course_id_norm"] == cidn].copy()
            if not att.empty:
                meets = att[att["grade_tok"].map(lambda g: grade_meets_min(g, row.get("min_grade","")))]
                chosen = latest_by_term(meets) if not meets.empty else latest_by_term(att)
                if not chosen.empty:
                    filled["match_course_id"] = chosen.get("course_id","")
                    filled["match_course_name"] = chosen.get("course_name","")
                    filled["match_term_code"]  = chosen.get("term_code","")
                    filled["match_grade"]      = chosen.get("grade","")
                    ok = grade_meets_min(filled["match_grade"], row.get("min_grade",""))
                    filled["match_status"] = chosen.get("attempt_status","unknown") if ok else "below_min_grade"
                    filled["meets_min_grade"] = "Y" if ok else "N"

        out_rows.append(filled)

    filled_df = pd.DataFrame(out_rows)

    used_bucket_ids = set()
    fixed_matched_norms = set(best_map.index).intersection(
        set(filled_df.loc[filled_df["match_status"]!="open","slot_course_id"].map(norm_id))
    )
    used_bucket_ids |= fixed_matched_norms

    for idx in filled_df.index[filled_df["bucket"].isin(["GENED_AH","GENED_SS"])]:
        bucket = filled_df.at[idx, "bucket"]
        allowed = ah_ids if bucket=="GENED_AH" else ss_ids
        chosen_norm = choose_bucket_assignment(tx_usable, allowed, used_bucket_ids)
        if chosen_norm:
            b = best_map.loc[chosen_norm]
            filled_df.loc[idx, ["match_course_id","match_course_name","match_term_code","match_grade","match_status"]] = [
                b.get("course_id",""), b.get("course_name",""), b.get("term_code",""),
                b.get("grade",""), b.get("attempt_status","unknown")
            ]
            used_bucket_ids.add(chosen_norm)

    if te_ids:
        te_allowed_tx = {cid for cid in tx_usable["course_id_norm"].unique()
                         if cid in te_ids and te_allowed_by_rules(cid, major, te_rules)}
        te_rows = filled_df.index[filled_df["bucket"].eq("TechElective")]
        used_bucket_ids |= set(filled_df.loc[filled_df["match_status"]!="open","match_course_id"].map(norm_id))
        for idx in te_rows:
            chosen_norm = choose_bucket_assignment(tx_usable, te_allowed_tx, used_bucket_ids)
            if not chosen_norm:
                continue
            b = best_map.loc[chosen_norm]
            filled_df.loc[idx, ["match_course_id","match_course_name","match_term_code","match_grade","match_status"]] = [
                b.get("course_id",""), b.get("course_name",""), b.get("term_code",""),
                b.get("grade",""), b.get("attempt_status","unknown")
            ]
            used_bucket_ids.add(chosen_norm)

    curriculum_fixed_ids = set(cur.loc[~cur["bucket"].isin(["GENED_AH","GENED_SS","TechElective"]), "course_id_norm"])
    assigned_norms = set(filled_df.loc[filled_df["match_status"]!="open", "match_course_id"].map(norm_id))
    tx_unmapped = tx_usable[
        (~tx_usable["course_id_norm"].isin(curriculum_fixed_ids)) &
        (~tx_usable["course_id_norm"].isin(assigned_norms))
    ]

    unmapped_rows = []
    for _, r in tx_unmapped.sort_values(["course_id_norm","term_code"]).iterrows():
        unmapped_rows.append({
            "term_id": "UNMAPPED","term_label": "Unmapped from Transcript",
            "slot_course_id": "","slot_course_name": "","credits": "","bucket": "",
            "prereq": "","coreq": "","min_grade": "","notes": "",
            "match_course_id": r.get("course_id",""), "match_course_name": r.get("course_name",""),
            "match_term_code": r.get("term_code",""), "match_grade": r.get("grade",""),
            "match_status": r.get("attempt_status",""), "source_track": track
        })

    filled_df["__order"] = 1
    if unmapped_rows:
        unmapped_df = pd.DataFrame(unmapped_rows); unmapped_df["__order"] = 0
        combined = pd.concat([unmapped_df, filled_df], ignore_index=True)
    else:
        combined = filled_df

    combined = combined.sort_values(
        ["__order","term_id","slot_course_id"], ascending=[True, True, True]
    ).drop(columns="__order")

    # prereqs: count any past passing attempt that met the slot min
    min_by_course = (
        cur.loc[~cur["bucket"].isin(["GENED_AH","GENED_SS","TechElective"]),
                ["course_id_norm","min_grade"]]
        .set_index("course_id_norm")["min_grade"]
        .to_dict()
    )
    ever_met_min = set()
    for _, r in tx.iterrows():
        cidn = r.get("course_id_norm","")
        if not cidn:
            continue
        min_req = min_by_course.get(cidn, "")
        if grade_meets_min(r.get("grade_tok",""), min_req):
            ever_met_min.add(cidn)
    ever_met_min |= set(tx.loc[tx["attempt_status"].isin(["passed","transfer"]), "course_id_norm"])

    def prereq_check(row) -> str:
        pre = str(row.get("prereq", "")).upper().strip()
        if not pre: return "Y"
        found = re.findall(r"[A-Z]{2,}\.? ?\d{3,4}", pre)
        if not found: return "Y"
        for cid in found:
            if norm_id(cid) not in ever_met_min:
                return "N"
        return "Y"

    combined["prereqs_met_flag"] = combined.apply(prereq_check, axis=1)

    # viz status
    def compute_viz(row) -> str:
        has_term = str(row.get("match_term_code","")).strip() != ""
        has_grade = str(row.get("match_grade","")).strip() != ""
        mstatus = str(row.get("match_status","")).lower().strip()
        min_g = _clean_grade_token(row.get("min_grade",""))
        in_progress = has_term and not has_grade
        passed_by_grade = has_grade and grade_meets_min(row.get("match_grade",""), min_g)
        taken_and_passed = passed_by_grade or (mstatus in {"passed","transfer","completed"})
        not_in_use = (not in_progress) and not taken_and_passed
        prereqs_met = str(row.get("prereqs_met_flag","")).upper() == "Y"

        # Explicit rule: course listed in a term but no grade → in progress
        if has_term and not has_grade:
            return "blue"
        if taken_and_passed:
            return "green"
        if in_progress:
            return "blue"
        if (not has_term) and (not in_progress) and (not prereqs_met):
            return "grey"
        if prereqs_met and not_in_use:
            return "yellow"
        return ""
    combined["viz_status"] = combined.apply(compute_viz, axis=1)

    out_path = transcript_csv.with_suffix(".filled_pathway.csv")
    combined.to_csv(out_path, index=False)

    # diagnostics
    total_fixed = len(cur[~cur["bucket"].isin(["GENED_AH","GENED_SS","TechElective"])])
    fixed_matched = (combined["term_id"] != "UNMAPPED") & (combined["match_status"] != "open")
    fixed_matched = combined[fixed_matched & (~combined["bucket"].isin(["GENED_AH","GENED_SS","TechElective"]))].shape[0]
    ah_filled = combined[(combined["bucket"]=="GENED_AH") & (combined["match_status"]!="open")].shape[0]
    ss_filled = combined[(combined["bucket"]=="GENED_SS") & (combined["match_status"]!="open")].shape[0]
    te_filled = combined[(combined["bucket"]=="TechElective") & (combined["match_status"]!="open")].shape[0]
    below_min = combined[(combined["term_id"]!="UNMAPPED") &
                         (~combined["bucket"].isin(["GENED_AH","GENED_SS","TechElective"])) &
                         (combined["match_status"]=="below_min_grade")].shape[0]
    print(f"[TRACK] {track} | Major={major} StartYear={start_year}")
    print(f"[CURR ] Fixed slots: {total_fixed} matched: {fixed_matched}")
    print(f"[BUCKET] AH: {ah_filled}  SS: {ss_filled}  TE: {te_filled}")
    print("[INFO ] Unmapped transcript courses listed first if any.")
    print(f"[RULE ] Below min-grade: {below_min}")
    print("[VIZ ]", combined["viz_status"].value_counts(dropna=False).to_dict())
    return out_path

# -------- CLI / GUI --------
def gui_run():
    root = Tk()
    root.title("Fill Curriculum Pathway")
    root.geometry("420x160")

    def go():
        path = filedialog.askopenfilename(title="Select transcript CSV", filetypes=[("CSV files","*.csv")])
        if not path: return
        try:
            out = fill_pathway(Path(path))
            messagebox.showinfo("Done", f"Created:\n{out}\nRequires AHElectives.csv, SSelectives.csv, TE_Rules.csv, and ME_TE.csv or IE_TE.csv")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    Button(root, text="Choose transcript CSV and fill map", command=go, width=36, height=2).pack(pady=35)
    root.mainloop()

def run_headless(input_csv: str, out_dir: str | None = None) -> str:
    in_path = Path(input_csv)
    out_path = fill_pathway(in_path)
    if out_dir:
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / out_path.name
        out_path.replace(dest)
        return str(dest)
    return str(out_path)

def cli():
    parser = argparse.ArgumentParser(description="Fill curriculum pathway from a transcript CSV.")
    parser.add_argument("-i", "--input", help="Path to transcript CSV for headless mode")
    parser.add_argument("-o", "--out-dir", help="Optional output directory")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress console output")
    args = parser.parse_args()
    if args.input:
        result = run_headless(args.input, args.out_dir)
        if not args.quiet: print(result)
    else:
        gui_run()

if __name__ == "__main__":
    cli()