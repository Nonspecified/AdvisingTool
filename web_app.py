"""
AdvisingBot Web Interface
-------------------------
Run:  python web_app.py
Open: http://localhost:5000  (or replace localhost with your machine's LAN IP)

Requires: pip install flask weasyprint
Student data is processed entirely in memory — nothing is written to permanent storage.
"""

import os
import tempfile
from pathlib import Path

from flask import Flask, request, render_template_string

# Import the three pipeline steps from the main application
from AdvisingBot import convert_pdf_to_csv, fill_pathway, generate_html

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload limit

# Optional: set a password to restrict access.
# Leave empty ("") to disable authentication.
ACCESS_PASSWORD = os.environ.get("ADVISINGBOT_PASSWORD", "")

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
    <button type="submit">Generate CPR PDF</button>
  </form>
  {% if error %}
  <div class="err">{{ error }}</div>
  {% endif %}
  <div class="note">No student data is stored. Files are processed in memory and immediately discarded.</div>
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

        # Step 2: CSV → filled pathway CSV
        filled_csv = fill_pathway(csv_path)

        # Step 3: filled CSV → interactive HTML CPR map
        html_path = generate_html(filled_csv)
        html_content = Path(html_path).read_text(encoding="utf-8")

    # Return the interactive HTML page directly in the browser
    return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
