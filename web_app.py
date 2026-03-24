"""
AdvisingBot Web Interface
-------------------------
Run:  python web_app.py
Open: http://localhost:5000  (or replace localhost with your machine's LAN IP)

Requires: pip install flask
Student data is processed entirely in memory — nothing is written to permanent storage.
"""

import os
import time
import uuid
import json
import tempfile
from pathlib import Path

import pandas as pd
from flask import Flask, request, render_template_string, redirect

# Import the three pipeline steps from the main application
from AdvisingBot import convert_pdf_to_csv, fill_pathway, generate_html, _load_minor_index

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload limit

# Optional: set a password to restrict access.
# Leave empty ("") to disable authentication.
ACCESS_PASSWORD = os.environ.get("ADVISINGBOT_PASSWORD", "")

# In-memory session store: uuid → {transcript_csv: str, ts: float}
# Sessions expire after 2 hours.
_sessions: dict = {}

def _cleanup_sessions():
    cutoff = time.time() - 7200
    for k in list(_sessions):
        if _sessions[k].get("ts", 0) < cutoff:
            del _sessions[k]


def _inject_chrome(html_content: str, session_id: str) -> str:
    """Inject New Student button, URL reset, and session ID into CPR HTML."""
    html_content = html_content.replace(
        "__MINOR_SESSION_ID__", session_id
    )
    injection = """
<style>
#new-student-btn{position:fixed;top:12px;right:16px;z-index:9999;
  background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;
  padding:7px 16px;font-size:.85rem;cursor:pointer;font-family:"Segoe UI",Arial,sans-serif;}
#new-student-btn:hover{background:#1565c0;color:#fff;}
</style>
<button id="new-student-btn" onclick="location.href='/'">New Student</button>
<script>history.replaceState({}, '', '/');</script>
"""
    return html_content.replace("</body>", injection + "</body>", 1)


UPLOAD_FORM = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AdvisingBot — CPR Generator</title>
<style>
  body{font-family:"Segoe UI",Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:36px 40px;
        max-width:420px;width:100%;text-align:center}
  h1{font-size:1.3rem;color:#a0c4ff;margin-bottom:6px}
  p{font-size:.85rem;color:#8090b0;margin-bottom:22px}
  input[type=file]{display:block;width:100%;margin-bottom:14px;color:#e0e0e0;
                   font-size:.85rem}
  input[type=password]{display:block;width:100%;padding:7px 10px;margin-bottom:14px;
                       border:1px solid #0f3460;border-radius:5px;background:#0d1b2e;
                       color:#e0e0e0;font-size:.85rem;box-sizing:border-box}
  button{background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;
         padding:9px 24px;font-size:.9rem;cursor:pointer;width:100%}
  button:hover{background:#1565c0;color:#fff}
  .err{color:#ff8080;font-size:.82rem;margin-top:10px}
  .note{font-size:.72rem;color:#556;margin-top:18px}
</style>
</head>
<body>
<div class="card">
  <h1>AdvisingBot</h1>
  <p>Upload a student transcript PDF to generate a Curriculum Progress Report.</p>
  <form method="POST" action="/process" enctype="multipart/form-data">
    <input type="file" name="transcript" accept=".pdf" required>
    {% if password_required %}
    <input type="password" name="password" placeholder="Access password" required>
    {% endif %}
    <button type="submit">Generate CPR</button>
  </form>
  {% if error %}
  <div class="err">{{ error }}</div>
  {% endif %}
  <div class="note">No student data is stored. Files are processed in memory and immediately discarded.</div>
</div>
<script>history.replaceState({}, '', '/');</script>
</body>
</html>"""

MINOR_SELECTOR = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>AdvisingBot — Add Minor Pathway</title>
<style>
  body{font-family:"Segoe UI",Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:36px 40px;
        max-width:460px;width:100%;text-align:center}
  h1{font-size:1.2rem;color:#a0c4ff;margin-bottom:6px}
  p{font-size:.85rem;color:#8090b0;margin-bottom:22px}
  select{display:block;width:100%;padding:8px 10px;margin-bottom:16px;
         border:1px solid #0f3460;border-radius:5px;background:#0d1b2e;
         color:#e0e0e0;font-size:.88rem;box-sizing:border-box}
  .btns{display:flex;gap:10px}
  button{flex:1;background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;
         padding:9px 20px;font-size:.88rem;cursor:pointer}
  button:hover{background:#1565c0;color:#fff}
  button.cancel{background:#1c1c2e;border-color:#333;color:#8090b0}
  button.cancel:hover{background:#252535;color:#e0e0e0}
</style>
</head>
<body>
<div class="card">
  <h1>Add Minor Pathway</h1>
  <p>Select a minor to add it to this student's CPR map.</p>
  <form method="POST" action="/apply-minor">
    <input type="hidden" name="session" value="{{ session_id }}">
    <select name="minor_code">
      {% for code, name in minors %}
      <option value="{{ code }}">{{ name }}</option>
      {% endfor %}
    </select>
    <div class="btns">
      <button type="button" onclick="history.back()" class="cancel">Cancel</button>
      <button type="submit">Add to CPR</button>
    </div>
  </form>
</div>
</body>
</html>"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(UPLOAD_FORM,
                                  password_required=bool(ACCESS_PASSWORD),
                                  error=None)


@app.route("/process", methods=["POST"])
def process():
    # Optional password check
    if ACCESS_PASSWORD:
        submitted = request.form.get("password", "")
        if submitted != ACCESS_PASSWORD:
            return render_template_string(UPLOAD_FORM,
                                          password_required=True,
                                          error="Incorrect password."), 403

    uploaded = request.files.get("transcript")
    if not uploaded or not uploaded.filename.lower().endswith(".pdf"):
        return render_template_string(UPLOAD_FORM,
                                      password_required=bool(ACCESS_PASSWORD),
                                      error="Please upload a PDF file."), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pdf_in = tmp / "transcript.pdf"
        uploaded.save(str(pdf_in))

        # Step 1: PDF → CSV
        csv_path = convert_pdf_to_csv(pdf_in)

        # Validate: reject PDFs that don't look like a UMass transcript
        df_check = pd.read_csv(csv_path, engine="python")
        sid_blank = df_check.get("student_id", pd.Series(dtype=str)).astype(str).str.strip().eq("").all()
        name_blank = df_check.get("full_name", pd.Series(dtype=str)).astype(str).str.strip().replace("nan", "").eq("").all()
        if df_check.empty or (sid_blank and name_blank):
            return render_template_string(UPLOAD_FORM,
                                          password_required=bool(ACCESS_PASSWORD),
                                          error="This doesn't look like a UMass transcript. Please upload an official transcript PDF."), 400

        # Store transcript CSV in session so the user can add minors later
        _cleanup_sessions()
        session_id = str(uuid.uuid4())
        _sessions[session_id] = {
            "transcript_csv": Path(csv_path).read_text(encoding="utf-8"),
            "extra_minor_codes": [],
            "ts": time.time(),
        }

        # Step 2: CSV → filled pathway CSV
        filled_csv = fill_pathway(csv_path)

        # Step 3: filled CSV → interactive HTML CPR map
        html_path = generate_html(filled_csv)
        html_content = Path(html_path).read_text(encoding="utf-8")

    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/select-minor", methods=["GET"])
def select_minor():
    session_id = request.args.get("session", "")
    if not session_id or session_id not in _sessions:
        return redirect("/")

    idx = _load_minor_index()
    minors = sorted(idx.items(), key=lambda x: x[1])  # sort by display name

    return render_template_string(MINOR_SELECTOR,
                                  session_id=session_id,
                                  minors=minors)


@app.route("/remove-minor", methods=["GET"])
def remove_minor():
    session_id = request.args.get("session", "")
    minor_code = request.args.get("code", "").strip().upper()
    if not session_id or session_id not in _sessions:
        return redirect("/")

    # Remove the specific minor code from the session list
    codes = _sessions[session_id].get("extra_minor_codes", [])
    codes = [c for c in codes if c != minor_code]
    _sessions[session_id]["extra_minor_codes"] = codes

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        csv_path = tmp / "transcript.csv"
        csv_path.write_text(_sessions[session_id]["transcript_csv"], encoding="utf-8")

        filled_csv = fill_pathway(csv_path, extra_minor_codes=codes if codes else None)
        html_path = generate_html(filled_csv)
        html_content = Path(html_path).read_text(encoding="utf-8")

    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/apply-minor", methods=["POST"])
def apply_minor():
    session_id = request.form.get("session", "")
    minor_code = request.form.get("minor_code", "").strip().upper()

    if not session_id or session_id not in _sessions:
        return redirect("/")
    if not minor_code:
        return redirect(f"/select-minor?session={session_id}")

    # Append to list (avoid duplicates)
    codes = _sessions[session_id].get("extra_minor_codes", [])
    if minor_code not in codes:
        codes.append(minor_code)
    _sessions[session_id]["extra_minor_codes"] = codes

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        csv_path = tmp / "transcript.csv"
        csv_path.write_text(_sessions[session_id]["transcript_csv"], encoding="utf-8")

        # Step 2: filled pathway CSV (with extra minors)
        filled_csv = fill_pathway(csv_path, extra_minor_codes=codes)

        # Step 3: filled CSV → interactive HTML CPR map
        html_path = generate_html(filled_csv)
        html_content = Path(html_path).read_text(encoding="utf-8")

    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
