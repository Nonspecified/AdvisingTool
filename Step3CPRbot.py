# CPDash_dynamic.py — uses flags from filled_pathway.csv produced by CPReval.py
# Colors: green=passed, red=failed, blue=in progress, yellow=ready (prereqs met), grey=untaken
# pip install pandas

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import re

PALETTES = {
    "light": {
        "bg_root": "#ffffff", "fg_text": "#111111",
        "passed": "#2e7d32", "failed": "#c62828",
        "inprog": "#1565c0", "ready":  "#9c8500", "blocked":"#9e9e9e",
        "edge_gray": "#7a7a7a",
        "notes_bg": "#fafafa", "notes_fg": "#111111", "hdr_fg": "#111111",
    },
    "dark": {
        "bg_root": "#121212", "fg_text": "#eaeaea",
        "passed": "#2e7d32", "failed": "#ef5350",
        "inprog": "#1565c0", "ready":  "#9c8500", "blocked":"#4a4a4a",
        "edge_gray": "#6a6a6a",
        "notes_bg": "#1f1f1f", "notes_fg": "#eaeaea", "hdr_fg": "#eaeaea",
    }
}

YEAR_ORDER = ["Freshman", "Sophomore", "Junior", "Senior"]
SEM_ORDER  = ["Fall", "Spring"]

PASS_SPECIAL = {"P","S","T","CR"}
GRADE_SCORE = {
    "A":13,"A-":12,"B+":11,"B":10,"B-":9,"C+":8,"C":7,"C-":6,
    "D+":5,"D":4,"D-":3,"F":0,"W":-1
}

_GTOK_RE = re.compile(r"\b(A-?|B\+?|B-?|C\+?|C-?|D\+?|D-?|F|W)\b", re.I)
COURSE_RE = re.compile(r"\b([A-Z]{2,}\.? ?\d{3,4}[A-Z]?)\b")

def _clean_grade_token(s: str) -> str:
    if not s:
        return ""
    m = _GTOK_RE.search(str(s))
    return m.group(0).upper() if m else ""

def norm_id(s: str) -> str:
    s = str(s or "").upper().replace(".", " ").strip()
    return re.sub(r"\s+", " ", s)

def extract_course_ids(text: str) -> set:
    if not isinstance(text, str) or not text.strip():
        return set()
    return {norm_id(m.group(1)) for m in COURSE_RE.finditer(text.upper())}

def term_from_label(label: str, term_id: str = "") -> str | None:
    t = (label or "").strip().lower()
    ymap = {"freshman":"1","sophomore":"2","junior":"3","senior":"4",
            "year 1":"1","year 2":"2","year 3":"3","year 4":"4"}
    smap = {"fall":"F","spring":"S"}
    y = next((v for k,v in ymap.items() if k in t), None)
    s = next((v for k,v in smap.items() if k in t), None)
    if y and s:
        return f"Y{y}{s}"
    m = re.search(r"Y\s*([1-4])\s*([FS])", (label or "").upper())
    if m:
        return f"Y{m.group(1)}{m.group(2)}"
    term_id = (term_id or "").upper().strip()
    if term_id in {"Y1F","Y1S","Y2F","Y2S","Y3F","Y3S","Y4F","Y4S"}:
        return term_id
    return None

def contrast_fg(bg_hex: str, theme: str) -> str:
    h = bg_hex.lstrip("#")
    if len(h) != 6:
        return "#ffffff" if theme == "dark" else "#111111"
    r, g, b = int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)
    lum = 0.2126*r + 0.7152*g + 0.0722*b
    return "#ffffff" if lum < 140 else "#111111"

class DashboardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Curriculum Dashboard")
        self.theme = "dark"
        self.current_df = None

        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except:
            pass

        self.year_frames = {}
        for col, year in enumerate(YEAR_ORDER):
            frame = ttk.Frame(root, padding=5)
            frame.grid(row=0, column=col, sticky="nsew", padx=6, pady=6)
            tk.Label(frame, text=year, font=("Arial", 12, "bold")).pack()
            self.year_frames[year] = {}
            for sem in SEM_ORDER:
                sf = ttk.Frame(frame, padding=3)
                tk.Label(sf, text=sem, font=("Arial", 10, "bold")).pack(anchor="w")
                sf.pack(side="left", expand=True, fill="both", padx=4)
                self.year_frames[year][sem] = sf

        nf = ttk.Frame(root, padding=(4,2))
        nf.grid(row=1, column=0, columnspan=len(YEAR_ORDER),
                sticky="nsew", padx=6, pady=(0,6))
        tk.Label(nf, text="Notes / Unmapped", font=("Arial", 10, "bold")).pack(anchor="w")
        self.notes = tk.Text(nf, height=6, wrap="word", bd=0)
        self.notes.pack(fill="both", expand=True, padx=2, pady=2)
        self.notes.configure(state="disabled")

        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open filled_pathway.csv", command=self.open_csv)
        filemenu.add_separator(); filemenu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        thememenu = tk.Menu(menubar, tearoff=0)
        thememenu.add_radiobutton(label="Light", command=lambda: self.set_theme("light"))
        thememenu.add_radiobutton(label="Dark", command=lambda: self.set_theme("dark"))
        menubar.add_cascade(label="Theme", menu=thememenu)
        root.config(menu=menubar)

        for c in range(len(YEAR_ORDER)): root.columnconfigure(c, weight=1)
        root.rowconfigure(0, weight=1); root.rowconfigure(1, weight=0)

        self.apply_theme(); self.open_csv()

    def set_theme(self, name: str):
        if name not in PALETTES: return
        self.theme = name; self.apply_theme()
        if self.current_df is not None: self.populate(self.current_df)

    def apply_theme(self):
        p = PALETTES[self.theme]
        self.root.configure(bg=p["bg_root"])
        self.style.configure("TFrame", background=p["bg_root"])
        self.style.configure("TLabel", background=p["bg_root"], foreground=p["hdr_fg"])
        self.notes.configure(bg=p["notes_bg"], fg=p["notes_fg"], insertbackground=p["notes_fg"])

    def open_csv(self):
        path = filedialog.askopenfilename(title="Select filled_pathway.csv",
                                          filetypes=[("CSV files","*.csv")])
        if not path: return
        try:
            df = pd.read_csv(path, engine="python"); self.current_df = df
            self.populate(df)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def clear_all(self):
        for y in YEAR_ORDER:
            for s in SEM_ORDER:
                for w in self.year_frames[y][s].winfo_children()[1:]:
                    w.destroy()
        self.notes.configure(state="normal"); self.notes.delete("1.0","end"); self.notes.configure(state="disabled")

    def colored_block_button(self, parent, text, fill_hex, outline_hex, cb):
        outer = tk.Frame(parent, bg=PALETTES[self.theme]["bg_root"],
                         highlightthickness=2, highlightbackground=outline_hex, bd=0)
        outer.pack(side="top", fill="x", padx=2, pady=3)
        inner = tk.Frame(outer, bg=fill_hex, bd=0)
        inner.pack(fill="x", padx=2, pady=2)
        fg = contrast_fg(fill_hex, self.theme)
        tk.Button(inner, text=text, bg=fill_hex, fg=fg,
                  activebackground=fill_hex, activeforeground=fg,
                  relief="flat", bd=0, wraplength=180, justify="left",
                  command=cb).pack(fill="x")

    def populate(self, df: pd.DataFrame):
        need = [
            "term_id","term_label","slot_course_id","slot_course_name",
            "match_status","match_grade","match_term_code",
            "match_course_id","min_grade","prereq","coreq","notes",
            "taken_flag","in_progress_flag","meets_min_grade","prereqs_met_flag"
        ]
        for c in need:
            if c not in df.columns:
                df[c] = ""
        df = df.copy()

        p = PALETTES[self.theme]
        self.clear_all()
        unmapped = []
        placed = 0

        for _, r in df.iterrows():
            term = term_from_label(r.get("term_label",""), r.get("term_id",""))
            cname = str(r.get("slot_course_name","")).strip() or "(Unnamed)"
            grade = str(r.get("match_grade","")).strip()
            min_g = _clean_grade_token(str(r.get("min_grade","")).strip())
            taken = str(r.get("taken_flag","")).upper() == "Y"
            inprog = str(r.get("in_progress_flag","")).upper() == "Y"
            passed_min = str(r.get("meets_min_grade","")).upper() == "Y"
            prereq_met = str(r.get("prereqs_met_flag","")).upper() == "Y"
            status = str(r.get("match_status","")).lower().strip()

            text = cname
            if grade:
                text += f" [{_clean_grade_token(grade)}]"
            if min_g == "C-":
                text += "  min:C-"

            frame = None
            if term in {"Y1F","Y1S","Y2F","Y2S","Y3F","Y3S","Y4F","Y4S"}:
                y_idx = int(term[1])
                s_name = "Fall" if term.endswith("F") else "Spring"
                frame = self.year_frames[YEAR_ORDER[y_idx-1]][s_name]

            # default grey
            color = p["blocked"]
            edge = p["edge_gray"]

            if taken:
                if grade and passed_min:
                    color = edge = p["passed"]      # green
                elif grade and not passed_min:
                    color = edge = p["failed"]      # red
                elif inprog or status == "in_progress":
                    color = edge = p["inprog"]      # blue
                else:
                    color = edge = p["inprog"]      # fallback blue for matched without grade
            else:
                if prereq_met:
                    color = p["ready"]              # yellow fill
                    edge = p["edge_gray"]
                else:
                    color = p["blocked"]            # grey fill
                    edge = p["edge_gray"]

            if frame is not None:
                self.colored_block_button(frame, text, color, edge, cb=(lambda row=r: self.show_details(row)))
                placed += 1
            else:
                unmapped.append(f"- {cname} [{status}]")

        self.notes.configure(state="normal")
        self.notes.insert("1.0", "Unmapped / Out-of-map courses:\n\n" +
                          ("\n".join(unmapped) if unmapped else "None"))
        self.notes.configure(state="disabled")

        if placed == 0:
            messagebox.showwarning("No courses placed",
                                   "No valid terms (Y1F/Y1S/...). Check CSV columns.")

    def show_details(self, row):
        parts = [
            f"Course: {row.get('slot_course_id','')} - {row.get('slot_course_name','')}",
            f"Term: {row.get('term_label','')} ({row.get('term_id','')})",
            f"Match status: {row.get('match_status','')}",
            f"Taken: {row.get('taken_flag','')}, In progress: {row.get('in_progress_flag','')}",
            f"Grade: {_clean_grade_token(row.get('match_grade',''))}  Min: {_clean_grade_token(row.get('min_grade',''))}  Meets min: {row.get('meets_min_grade','')}",
            f"Prereqs met: {row.get('prereqs_met_flag','')}",
            f"Prereq: {row.get('prereq','')}",
            f"Coreq: {row.get('coreq','')}",
            f"Notes: {row.get('notes','')}",
        ]
        messagebox.showinfo("Course Details", "\n".join([p for p in parts if str(p).strip()]))

def main():
    root = tk.Tk()
    DashboardApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()