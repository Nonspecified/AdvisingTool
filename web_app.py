"""
AdvisingBot Web Interface
-------------------------
Run:  python web_app.py
Open: http://localhost:5000  (or replace localhost with your machine's LAN IP)

Requires: pip install flask
Student data is processed entirely in memory — nothing is written to permanent storage.
"""

import os
import json
import time
import uuid
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from flask import Flask, request, render_template_string, redirect, make_response, jsonify, send_file

# Import the three pipeline steps from the main application
from AdvisingBot import convert_pdf_to_csv, fill_pathway, generate_html, _load_minor_index, _load_registry

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


APPINSIGHTS_CONNECTION_STRING  = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
BUG_TABLE_NAME = "advisingbotbugreports"


def _get_appinsights_logger():
  if not APPINSIGHTS_CONNECTION_STRING:
    return None
  try:
    from opencensus.ext.azure.log_exporter import AzureLogHandler  # type: ignore
  except Exception:
    return None

  logger = logging.getLogger("advisingbot.bugreports")
  logger.setLevel(logging.INFO)
  if not any(h.__class__.__name__ == "AzureLogHandler" for h in logger.handlers):
    logger.addHandler(AzureLogHandler(connection_string=APPINSIGHTS_CONNECTION_STRING))
  return logger


def _persist_bug_report(report: dict) -> None:
    saved = False

    # ── Azure Table Storage (primary, easy to browse in Portal) ──
    if AZURE_STORAGE_CONNECTION_STRING:
        try:
            from azure.data.tables import TableServiceClient  # type: ignore
            svc   = TableServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
            table = svc.create_table_if_not_exists(BUG_TABLE_NAME)
            entity = {
                "PartitionKey": datetime.now(timezone.utc).strftime("%Y-%m"),
                "RowKey":       str(uuid.uuid4()),
            }
            for k, v in report.items():
                entity[k] = str(v)[:32000]   # Table Storage max string length
            table.upsert_entity(entity)
            saved = True
        except Exception as exc:
            logging.getLogger("advisingbot.bugreports").error("Table Storage write failed: %s", exc)

    # ── Application Insights (secondary, for alerting/monitoring) ──
    ai_logger = _get_appinsights_logger()
    if ai_logger:
        ai_logger.info(
            "AdvisingBot bug report",
            extra={"custom_dimensions": {k: str(v)[:2000] for k, v in report.items()}},
        )
        saved = True

    # ── Local fallback (no Azure configured) ──
    if not saved:
        logging.getLogger("advisingbot.bugreports").warning("BUG REPORT (no Azure): %s", report)


def _track_options():
    registry = _load_registry()
    name_map = {"ME": "Mechanical Engineering", "IE": "Industrial Engineering"}
    opts = []
    for major, info in registry.items():
        display = name_map.get(major, major)
        variants = info.get("variants", ["default"])
        for variant in variants:
            label = f"{display} — {variant}"
            opts.append((f"{major}_{variant}", label))
    return opts


def _render_upload_form(error=None, selected_track=""):
    return render_template_string(UPLOAD_FORM,
                                  password_required=bool(ACCESS_PASSWORD),
                                  error=error,
                                  track_options=_track_options(),
                                  selected_track=selected_track)


def _normalize_track_choice(choice: str) -> str:
    if not choice:
        return ""
    options = {opt for opt, _ in _track_options()}
    return choice if choice in options else ""


def _first_valid_value(df: pd.DataFrame, columns: list) -> str:
    for col in columns:
        if col not in df.columns:
            continue
        values = df[col].dropna().astype(str).str.strip()
        values = values[~values.str.lower().isin({"", "nan", "none"})]
        if not values.empty:
            return values.iloc[0]
    return ""


def _update_session_outputs(session_id: str, csv_path: Path, extra_minor_codes=None) -> str:
    session = _sessions[session_id]
    minors = extra_minor_codes if extra_minor_codes is not None else session.get("extra_minor_codes", [])
    track_choice = session.get("track_choice") or None
    filled_path = fill_pathway(csv_path, extra_minor_codes=minors or None, track_override=track_choice)
    html_path = generate_html(filled_path)
    session["filled_csv"] = Path(filled_path).read_text(encoding="utf-8")
    session["filled_csv_filename"] = Path(filled_path).name
    session["html_content"] = Path(html_path).read_text(encoding="utf-8")
    session["html_filename"] = Path(html_path).name
    session["extra_minor_codes"] = minors
    session["ts"] = time.time()
    session["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return session["html_content"]


def _build_session_from_csv(session_id: str, extra_minor_codes=None) -> str:
    session = _sessions[session_id]
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        csv_path = tmp / "transcript.csv"
        csv_path.write_text(session["transcript_csv"], encoding="utf-8")
        return _update_session_outputs(session_id, csv_path, extra_minor_codes)


def _inject_chrome(html_content: str, session_id: str) -> str:
    """Inject top nav bar, minor slide-in panel, export dropdown, and bug report controls."""
    session = _sessions.get(session_id, {})
    student_name = session.get("student_name") or "Student"
    plan         = session.get("plan_display") or "—"
    generated    = session.get("generated_at", "")
    meta_line    = f"Student: {student_name} · Plan: {plan}"
    if generated:
        meta_line += f" · {generated}"

    download_html_url = f"/download-html?session={session_id}"
    download_csv_url  = f"/download-csv?session={session_id}"

    applied_codes    = session.get("extra_minor_codes", [])
    minor_index      = _load_minor_index()
    minor_index_json = json.dumps(minor_index)
    applied_json     = json.dumps(applied_codes)

    html_content = html_content.replace("__MINOR_SESSION_ID__", session_id)

    injection = f"""
<style>
/* ── top nav bar ── */
#session-meta-bar{{position:fixed;top:0;left:0;right:0;z-index:9998;
  background:rgba(13,19,40,.96);backdrop-filter:blur(4px);color:#e0e0e0;
  padding:5px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;
  font-family:"Segoe UI",Arial,sans-serif;border-bottom:1px solid #0f3460;
  min-height:38px;box-sizing:border-box}}
#session-meta-text{{font-size:.7rem;color:#8090b0;letter-spacing:.02em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}}
#nav-controls{{display:flex;align-items:center;gap:6px;flex-shrink:0}}
.nav-btn{{background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;
  padding:4px 11px;font-size:.75rem;cursor:pointer;font-family:inherit;white-space:nowrap;
  text-decoration:none;display:inline-flex;align-items:center;gap:4px}}
.nav-btn:hover{{background:#1565c0;color:#fff}}
#map-controls{{display:flex;justify-content:flex-end;align-items:center;margin:6px 0 10px}}
#arrow-toggle-label{{font-size:.75rem;color:#8090b0;display:flex;align-items:center;
  gap:5px;cursor:pointer;user-select:none;padding:4px 2px}}
#arrow-toggle-label input{{accent-color:#a0c4ff;cursor:pointer;margin:0}}
/* export dropdown */
#export-wrap{{position:relative}}
#export-dropdown{{display:none;position:absolute;top:calc(100% + 5px);right:0;
  background:#0d1b2e;border:1px solid #1565c0;border-radius:6px;padding:4px 0;
  z-index:10002;min-width:180px;box-shadow:0 4px 18px rgba(0,0,0,.65)}}
#export-wrap.open #export-dropdown{{display:block}}
.export-item{{display:block;padding:7px 14px;font-size:.78rem;color:#a0c4ff;
  text-decoration:none;cursor:pointer;white-space:nowrap;background:none;
  border:none;width:100%;text-align:left;font-family:inherit}}
.export-item:hover{{background:#0f3460;color:#fff}}
/* bug report controls */
#bug-icon{{font-size:1.05rem;line-height:1;padding:2px;background:none;border:none;
  color:#fff;opacity:.45;cursor:pointer}}
#bug-icon:hover{{opacity:1}}
#bug-fab{{position:fixed;right:18px;bottom:18px;z-index:10003;
  width:46px;height:46px;border-radius:999px;border:1px solid #ff4d4d;
  background:#9d1010;color:#fff;display:flex;align-items:center;justify-content:center;
  font-size:1.15rem;box-shadow:0 8px 20px rgba(0,0,0,.45);
  transition:transform .12s ease,background .12s ease,box-shadow .12s ease}}
#bug-fab:hover{{background:#c31717;transform:translateY(-1px);
  box-shadow:0 10px 24px rgba(0,0,0,.5)}}
#bug-fab,#bug-icon{{font-family:inherit;line-height:1}}
/* bug modal */
#bug-modal-overlay{{display:none;position:fixed;inset:0;z-index:10004;background:rgba(0,0,0,.58);backdrop-filter:blur(2px)}}
#bug-modal{{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:10005;
  width:min(620px,92vw);background:#16213e;border:1px solid #0f3460;border-radius:10px;
  box-shadow:0 16px 44px rgba(0,0,0,.55);font-family:"Segoe UI",Arial,sans-serif;color:#e0e0e0;
  display:none;padding:14px}}
#bug-modal h3{{margin:0 0 10px;color:#a0c4ff;font-size:1rem}}
#bug-modal .bug-sub{{font-size:.76rem;color:#8090b0;margin-bottom:10px}}
.bug-field{{margin-bottom:9px}}
.bug-field label{{display:block;font-size:.72rem;color:#9fb0cd;margin-bottom:4px}}
.bug-field textarea{{width:100%;min-height:86px;resize:vertical;padding:8px 10px;box-sizing:border-box;
  border-radius:6px;border:1px solid #0f3460;background:#0d1b2e;color:#e0e0e0;font-family:inherit;font-size:.82rem}}
.bug-actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}}
.bug-btn{{background:#0f3460;color:#a0c4ff;border:1px solid #1565c0;border-radius:6px;padding:6px 12px;
  cursor:pointer;font-size:.78rem}}
.bug-btn:hover{{background:#1565c0;color:#fff}}
.bug-btn.primary{{background:#9d1010;border-color:#ff4d4d;color:#fff}}
.bug-btn.primary:hover{{background:#c31717}}
#bug-save-note{{font-size:.72rem;color:#8090b0;margin-top:8px;display:none}}
/* minor overlay & panel */
#minor-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);
  z-index:10000;backdrop-filter:blur(2px)}}
#minor-panel{{position:fixed;top:0;right:0;bottom:0;width:310px;max-width:90vw;
  background:#16213e;border-left:1px solid #0f3460;z-index:10001;
  display:flex;flex-direction:column;font-family:"Segoe UI",Arial,sans-serif;
  transform:translateX(100%);transition:transform .22s ease}}
#minor-overlay.open #minor-panel{{transform:translateX(0)}}
#minor-panel-hdr{{display:flex;align-items:center;justify-content:space-between;
  padding:11px 14px;border-bottom:1px solid #0f3460;flex-shrink:0}}
#minor-panel-hdr h2{{font-size:.9rem;color:#a0c4ff;margin:0}}
#minor-close{{background:none;border:none;color:#8090b0;font-size:1.15rem;
  cursor:pointer;padding:0 4px;line-height:1}}
#minor-close:hover{{color:#fff}}
#minor-applied-sec{{padding:9px 14px;border-bottom:1px solid #0f3460;flex-shrink:0;min-height:38px}}
.minor-sec-lbl{{font-size:.64rem;color:#8090b0;text-transform:uppercase;
  letter-spacing:.06em;margin-bottom:6px}}
#minor-applied-chips{{display:flex;flex-wrap:wrap;gap:5px}}
.minor-chip{{display:inline-flex;align-items:center;gap:5px;background:#0f3460;
  border:1px solid #1565c0;border-radius:4px;padding:3px 8px;
  font-size:.72rem;color:#a0c4ff}}
.minor-chip button{{background:none;border:none;color:#8090b0;cursor:pointer;
  padding:0;font-size:.8rem;line-height:1}}
.minor-chip button:hover{{color:#ff8080}}
#minor-search-row{{flex-shrink:0;padding:9px 14px;border-bottom:1px solid #0f3460}}
#minor-search-input{{width:100%;padding:6px 10px;background:#0d1b2e;
  border:1px solid #0f3460;border-radius:5px;color:#e0e0e0;
  font-size:.82rem;box-sizing:border-box;outline:none}}
#minor-search-input:focus{{border-color:#1565c0}}
#minor-list{{flex:1;overflow-y:auto;padding:4px 0}}
.minor-row{{display:flex;align-items:center;justify-content:space-between;
  padding:7px 14px;font-size:.78rem;color:#c9d1d9}}
.minor-row:hover{{background:#0f2a4a}}
.minor-row.is-added{{color:#8090b0;font-style:italic}}
.minor-add-btn{{background:none;border:none;color:#a0c4ff;cursor:pointer;
  font-size:1rem;padding:0 4px;line-height:1;flex-shrink:0}}
.minor-add-btn:hover:not([disabled]){{color:#fff}}
.minor-add-btn[disabled]{{color:#444;cursor:default}}
#minor-spinner{{display:none;text-align:center;padding:20px;
  color:#8090b0;font-size:.8rem}}
/* body top padding so content clears the nav */
body{{padding-top:44px!important}}
@media print{{
  #session-meta-bar,#minor-overlay{{display:none!important}}
  body{{padding-top:0!important}}
}}
</style>

<div id="session-meta-bar">
  <div id="session-meta-text">{meta_line}</div>
  <div id="nav-controls">
    <button class="nav-btn" type="button" onclick="window.print()">PDF</button>
    <div id="export-wrap">
      <button class="nav-btn" id="export-btn" type="button">Export &#9660;</button>
      <div id="export-dropdown">
        <a class="export-item" href="{download_csv_url}" download>Download CSV</a>
        <a class="export-item" href="{download_html_url}" download>Download CPR (HTML)</a>
      </div>
    </div>
    <button class="nav-btn" id="open-minor-btn" type="button">Minors (beta)</button>
    <button class="nav-btn" type="button" onclick="location.href='/'">New Student</button>
    <button id="bug-icon" type="button" title="Report a bug"><svg width="17" height="17" viewBox="0 0 28 28" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M15 2H5a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h10.5"/><path d="M15 2v6h6" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="6" y="10" width="6" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="13.5" width="8" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="17" width="5" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><circle cx="21" cy="17" r="2.5"/><ellipse cx="21" cy="23" rx="4.5" ry="5"/><path d="M16.5 20l-3.5-1.5M16.5 23l-3.5 0M16.5 26l-3.5 1.5M25.5 20l3.5-1.5M25.5 23l3.5 0M25.5 26l3.5 1.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/><ellipse cx="21" cy="23.5" rx="1.2" ry="2.2" fill="white" fill-opacity=".35"/></svg></button>
  </div>
</div>

<button id="bug-fab" type="button" title="Report a bug"><svg width="22" height="22" viewBox="0 0 28 28" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M15 2H5a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h10.5"/><path d="M15 2v6h6" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="6" y="10" width="6" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="13.5" width="8" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="17" width="5" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><circle cx="21" cy="17" r="2.5"/><ellipse cx="21" cy="23" rx="4.5" ry="5"/><path d="M16.5 20l-3.5-1.5M16.5 23l-3.5 0M16.5 26l-3.5 1.5M25.5 20l3.5-1.5M25.5 23l3.5 0M25.5 26l3.5 1.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/><ellipse cx="21" cy="23.5" rx="1.2" ry="2.2" fill="white" fill-opacity=".35"/></svg></button>

<div id="bug-modal-overlay"></div>
<div id="bug-modal" role="dialog" aria-modal="true" aria-labelledby="bug-modal-title">
  <h3 id="bug-modal-title">What is wrong?</h3>
  <div class="bug-sub">This logs a debug report to Azure Application Insights for later debugging.</div>
  <div class="bug-field">
    <label for="bug-what">What happened</label>
    <textarea id="bug-what" placeholder="Describe the issue and what you clicked"></textarea>
  </div>
  <div class="bug-field">
    <label for="bug-expected">What you expected</label>
    <textarea id="bug-expected" placeholder="What should have happened?"></textarea>
  </div>
  <div class="bug-actions">
    <button class="bug-btn" id="bug-cancel" type="button">Cancel</button>
    <button class="bug-btn primary" id="bug-submit" type="button">Save Report</button>
  </div>
  <div id="bug-save-note"></div>
</div>

<div id="minor-overlay">
  <div id="minor-panel">
    <div id="minor-panel-hdr">
      <h2>Add / Edit Minors</h2>
      <button id="minor-close" title="Close">&#x2715;</button>
    </div>
    <div id="minor-applied-sec">
      <div class="minor-sec-lbl">Applied minors</div>
      <div id="minor-applied-chips"></div>
    </div>
    <div id="minor-search-row">
      <input type="text" id="minor-search-input" placeholder="Search minors&#8230;" autocomplete="off">
    </div>
    <div id="minor-list"></div>
    <div id="minor-spinner">Updating CPR&#8230;</div>
  </div>
</div>

<script>
(function(){{
  var SESSION  = '{session_id}';

  /* ── Arrow toggle ── */
  var gridEl = document.getElementById('curriculum-grid');
  if(gridEl){{
    var mapControls = document.createElement('div');
    mapControls.id = 'map-controls';
    mapControls.innerHTML = '<label id="arrow-toggle-label" title="Toggle prerequisite arrows">' +
      '<input type="checkbox" id="arrow-toggle-cb"> Toggle prereq arrows</label>';
    gridEl.parentNode.insertBefore(mapControls, gridEl);
  }}
  var arCb = document.getElementById('arrow-toggle-cb');
  if(arCb) arCb.addEventListener('change', function(){{
    if(typeof toggleArrows === 'function') toggleArrows();
  }});
  if(typeof toggleArrows === 'function') toggleArrows();

  /* ── Export dropdown ── */
  var expWrap = document.getElementById('export-wrap');
  document.getElementById('export-btn').addEventListener('click', function(e){{
    e.stopPropagation(); expWrap.classList.toggle('open');
  }});
  document.addEventListener('click', function(){{ expWrap.classList.remove('open'); }});

  /* ── Bug report modal ── */
  var bugOverlay = document.getElementById('bug-modal-overlay');
  var bugModal = document.getElementById('bug-modal');
  var bugWhat = document.getElementById('bug-what');
  var bugExpected = document.getElementById('bug-expected');
  var bugSaveNote = document.getElementById('bug-save-note');
  var bugSubmit = document.getElementById('bug-submit');

  function openBugModal(){{
    bugSaveNote.style.display = 'none';
    bugSaveNote.textContent = '';
    bugOverlay.style.display = 'block';
    bugModal.style.display = 'block';
    setTimeout(function(){{ bugWhat.focus(); }}, 20);
  }}
  function closeBugModal(){{
    bugOverlay.style.display = 'none';
    bugModal.style.display = 'none';
  }}

  ['bug-icon','bug-fab'].forEach(function(id){{
    var el = document.getElementById(id);
    if(el) el.addEventListener('click', openBugModal);
  }});
  document.getElementById('bug-cancel').addEventListener('click', closeBugModal);
  bugOverlay.addEventListener('click', closeBugModal);

  bugSubmit.addEventListener('click', function(){{
    var whatWrong = (bugWhat.value || '').trim();
    var expected = (bugExpected.value || '').trim();
    if(!whatWrong){{
      bugSaveNote.style.display = 'block';
      bugSaveNote.style.color = '#ff8080';
      bugSaveNote.textContent = 'Please describe what happened.';
      return;
    }}
    bugSubmit.disabled = true;
    bugSubmit.textContent = 'Saving...';

    fetch('/report-bug', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        session: SESSION,
        what_wrong: whatWrong,
        expected: expected,
        page_url: location.href,
        user_agent: navigator.userAgent
      }})
    }})
    .then(function(r){{ return r.json(); }})
    .then(function(data){{
      if(!data.ok) throw new Error(data.error || 'Unable to save report');

      bugSaveNote.style.display = 'block';
      bugSaveNote.style.color = '#a0c4ff';
      bugSaveNote.textContent = 'Saved report to Azure.';
      setTimeout(closeBugModal, 250);
    }})
    .catch(function(err){{
      bugSaveNote.style.display = 'block';
      bugSaveNote.style.color = '#ff8080';
      bugSaveNote.textContent = err.message || 'Failed to save report.';
    }})
    .finally(function(){{
      bugSubmit.disabled = false;
      bugSubmit.textContent = 'Save Report';
    }});
  }});

  /* ── Minor panel ── */
  var overlay  = document.getElementById('minor-overlay');
  var minorIdx = {minor_index_json};
  var applied  = {applied_json};

  function openPanel(){{
    overlay.style.display = 'block';
    setTimeout(function(){{ overlay.classList.add('open'); }}, 10);
    renderPanel();
  }}
  function closePanel(){{
    overlay.classList.remove('open');
    setTimeout(function(){{ overlay.style.display = 'none'; }}, 230);
  }}

  document.getElementById('open-minor-btn').addEventListener('click', openPanel);
  document.getElementById('minor-close').addEventListener('click', closePanel);
  overlay.addEventListener('click', function(e){{ if(e.target === overlay) closePanel(); }});

  /* auto-reopen after add/remove */
  if(sessionStorage.getItem('reopen-minors')){{
    sessionStorage.removeItem('reopen-minors');
    openPanel();
  }}

  function renderApplied(){{
    var el = document.getElementById('minor-applied-chips');
    if(!applied.length){{
      el.innerHTML = '<span style="font-size:.72rem;color:#445">None</span>';
      return;
    }}
    el.innerHTML = applied.map(function(code){{
      return '<span class="minor-chip">' + (minorIdx[code] || code) +
        '<button data-code="' + code + '" title="Remove">&#x2715;</button></span>';
    }}).join('');
    el.querySelectorAll('button[data-code]').forEach(function(btn){{
      btn.addEventListener('click', function(){{ removeMinor(btn.dataset.code); }});
    }});
  }}

  function renderList(q){{
    var el = document.getElementById('minor-list');
    q = (q || '').toLowerCase().trim();
    var entries = Object.entries(minorIdx).sort(function(a, b){{ return a[1].localeCompare(b[1]); }});
    if(q) entries = entries.filter(function(e){{ return e[1].toLowerCase().includes(q) || e[0].toLowerCase().includes(q); }});
    if(!entries.length){{
      el.innerHTML = '<div style="padding:14px;font-size:.78rem;color:#445">No matches</div>';
      return;
    }}
    var added = new Set(applied);
    el.innerHTML = entries.map(function(e){{
      var isAdded = added.has(e[0]);
      return '<div class="minor-row' + (isAdded ? ' is-added' : '') + '">' +
        '<span>' + e[1] + '</span>' +
        '<button class="minor-add-btn" data-code="' + e[0] + '"' +
        (isAdded ? ' disabled title="Already added">' + '&#x2713;' : ' title="Add">+') +
        '</button></div>';
    }}).join('');
    el.querySelectorAll('.minor-add-btn:not([disabled])').forEach(function(btn){{
      btn.addEventListener('click', function(){{ addMinor(btn.dataset.code); }});
    }});
  }}

  function renderPanel(){{
    renderApplied();
    renderList(document.getElementById('minor-search-input').value);
  }}

  document.getElementById('minor-search-input').addEventListener('input', function(){{
    renderList(this.value);
  }});

  function setLoading(on){{
    document.getElementById('minor-list').style.display    = on ? 'none' : 'block';
    document.getElementById('minor-spinner').style.display = on ? 'block' : 'none';
  }}

  function reloadPage(html){{
    document.open(); document.write(html); document.close();
  }}

  function addMinor(code){{
    setLoading(true);
    sessionStorage.setItem('reopen-minors', '1');
    var fd = new FormData(); fd.append('session', SESSION); fd.append('minor_code', code);
    fetch('/apply-minor', {{method:'POST', body:fd}})
      .then(function(r){{ return r.text(); }})
      .then(reloadPage)
      .catch(function(){{ sessionStorage.removeItem('reopen-minors'); setLoading(false); }});
  }}

  function removeMinor(code){{
    setLoading(true);
    sessionStorage.setItem('reopen-minors', '1');
    fetch('/remove-minor?session=' + SESSION + '&code=' + encodeURIComponent(code))
      .then(function(r){{ return r.text(); }})
      .then(reloadPage)
      .catch(function(){{ sessionStorage.removeItem('reopen-minors'); setLoading(false); }});
  }}
}})();
history.replaceState({{}}, '', '/');
</script>
"""
    return html_content.replace("</body>", injection + "</body>", 1)


UPLOAD_FORM = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>CPR Filler</title>
<style>
  body{font-family:"Segoe UI",Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:24px 30px;
        max-width:360px;width:100%;text-align:center}
  h1{font-size:1.1rem;color:#a0c4ff;margin-bottom:4px}
  p{font-size:.8rem;color:#8090b0;margin-bottom:16px}
  input[type=file]{display:block;width:100%;padding:7px 10px;margin-bottom:10px;
                   border:1px solid #1565c0;border-radius:6px;background:#0f3460;
                   color:#a0c4ff;font-size:.82rem;box-sizing:border-box;cursor:pointer}
  input[type=file]::file-selector-button{background:#1565c0;color:#fff;border:none;
    border-radius:4px;padding:4px 10px;font-size:.8rem;cursor:pointer;margin-right:10px}
  button[type=submit]{display:block;width:100%;background:#0f3460;color:#a0c4ff;
    border:1px solid #1565c0;border-radius:6px;padding:8px 20px;font-size:.85rem;
    cursor:pointer;margin-top:2px;font-family:inherit}
  button[type=submit]:hover{background:#1565c0;color:#fff}
  input[type=password]{display:block;width:100%;padding:7px 10px;margin-bottom:14px;
                       border:1px solid #0f3460;border-radius:5px;background:#0d1b2e;
                       color:#e0e0e0;font-size:.85rem;box-sizing:border-box}
  .manual-row{display:flex;align-items:center;gap:8px;margin-bottom:8px;
              font-size:.8rem;color:#8090b0;cursor:pointer;text-align:left}
  .manual-row input[type=checkbox]{accent-color:#a0c4ff;width:14px;height:14px;
                                   cursor:pointer;flex-shrink:0}
  #track-select-wrap{display:none;margin-bottom:6px;text-align:left}
  select{display:block;width:100%;padding:7px 10px;border:1px solid #0f3460;border-radius:5px;
         background:#0d1b2e;color:#e0e0e0;font-size:.85rem;box-sizing:border-box}
  .track-note{font-size:.65rem;color:#8090b0;margin-top:5px}
  .err{color:#ff8080;font-size:.82rem;margin-top:12px}
  .note{font-size:.72rem;color:#556;margin-top:18px}
</style>
</head>
<body>
<div class="card">
  <h1>CPR Filler</h1>
  <p>Upload a student transcript PDF to generate a Curriculum Progress Report.</p>
  <form id="upload-form" method="POST" action="/process" enctype="multipart/form-data">
    <input type="file" id="file-input" name="transcript" accept=".pdf" required>
    <button type="submit">Generate CPR</button>
    <label class="manual-row">
      <input type="checkbox" id="manual-track-toggle"> Manually select track (if auto-detect fails)
    </label>
    <div id="track-select-wrap">
      <select name="track_choice">
        <option value="">Auto-detect ME/IE track</option>
        {% for value, label in track_options %}
        <option value="{{ value }}" {% if value == selected_track %}selected{% endif %}>{{ label }}</option>
        {% endfor %}
      </select>
      <div class="track-note">Select a known track if the transcript’s plan/program is missing or incorrectly parsed.</div>
    </div>
    {% if password_required %}
    <input type="password" name="password" placeholder="Access password" required>
    {% endif %}
  </form>
  {% if error %}
  <div class="err">{{ error }}</div>
  {% endif %}
  <div class="note">No student data is stored. Files are processed in memory and immediately discarded.</div>
</div>
<script>
(function(){
  var cb   = document.getElementById(‘manual-track-toggle’);
  var wrap = document.getElementById(‘track-select-wrap’);

  cb.addEventListener(‘change’, function(){
    wrap.style.display = cb.checked ? ‘block’ : ‘none’;
  });
  {% if selected_track %}cb.checked = true; wrap.style.display = ‘block’;{% endif %}
})();
history.replaceState({}, ‘’, ‘/’);
</script>
</body>
</html>"""



@app.route("/", methods=["GET"])
def index():
    return _render_upload_form()


@app.route("/process", methods=["POST"])
def process():
    if ACCESS_PASSWORD:
        submitted = request.form.get("password", "")
        if submitted != ACCESS_PASSWORD:
            return _render_upload_form(error="Incorrect password."), 403

    uploaded = request.files.get("transcript")
    if not uploaded or not uploaded.filename.lower().endswith(".pdf"):
        return _render_upload_form(error="Please upload a PDF file."), 400

    track_choice = _normalize_track_choice(
        (request.form.get("track_choice") or "").strip()
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        pdf_in = tmp / "transcript.pdf"
        uploaded.save(str(pdf_in))

        csv_path = convert_pdf_to_csv(pdf_in)
        df_check = pd.read_csv(csv_path, engine="python")
        sid_blank = df_check.get("student_id", pd.Series(dtype=str)).astype(str).str.strip().eq("").all()
        name_blank = df_check.get("full_name", pd.Series(dtype=str)).astype(str).str.strip().replace("nan", "").eq("").all()
        if df_check.empty or (sid_blank and name_blank):
            return _render_upload_form(
                error="This doesn't look like a UMass transcript. Please upload an official transcript PDF.",
                selected_track=track_choice,
            ), 400

        student_name = _first_valid_value(df_check, ["student_name", "full_name"]) or "Student"
        plan_display = _first_valid_value(df_check, ["plan_short", "plan"]) or "—"
        transcript_text = Path(csv_path).read_text(encoding="utf-8")

        _cleanup_sessions()
        session_id = str(uuid.uuid4())
        _sessions[session_id] = {
            "transcript_csv": transcript_text,
            "extra_minor_codes": [],
            "track_choice": track_choice,
            "student_name": student_name,
            "plan_display": plan_display,
            "ts": time.time(),
        }

        try:
            html_content = _update_session_outputs(session_id, csv_path, extra_minor_codes=[])
        except FileNotFoundError as exc:
            return _render_upload_form(
                error=f"{exc} Please select the matching track from the dropdown.",
                selected_track=track_choice,
            ), 400

    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}



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
    html_content = _build_session_from_csv(session_id, extra_minor_codes=codes)
    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/apply-minor", methods=["POST"])
def apply_minor():
    session_id = request.form.get("session", "")
    minor_code = request.form.get("minor_code", "").strip().upper()

    if not session_id or session_id not in _sessions:
        return redirect("/")
    if not minor_code:
        return redirect("/")

    # Append to list (avoid duplicates)
    codes = _sessions[session_id].get("extra_minor_codes", [])
    if minor_code not in codes:
        codes.append(minor_code)
    _sessions[session_id]["extra_minor_codes"] = codes
    html_content = _build_session_from_csv(session_id, extra_minor_codes=codes)
    return _inject_chrome(html_content, session_id), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/report-bug", methods=["POST"])
def report_bug():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session") or "").strip()
    session = _sessions.get(session_id, {})

    what_wrong = str(payload.get("what_wrong", "")).strip()
    expected = str(payload.get("expected", "")).strip()
    if not what_wrong:
      return jsonify({"ok": False, "error": "Missing what_wrong"}), 400

    report = {
        "reported_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "session_id": session_id,
        "student_name": session.get("student_name", ""),
        "plan": session.get("plan_display", ""),
        "generated_at": session.get("generated_at", ""),
        "track_choice": session.get("track_choice", ""),
        "minor_codes": session.get("extra_minor_codes", []),
        "html_filename": session.get("html_filename", ""),
        "filled_csv_filename": session.get("filled_csv_filename", ""),
        "what_wrong": what_wrong,
        "expected": expected,
        "page_url": str(payload.get("page_url", "")),
        "user_agent": str(payload.get("user_agent", "")),
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
    }

    try:
      _persist_bug_report(report)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to save report: {exc}"}), 500

    return jsonify({"ok": True, "message": "Saved report to Azure Application Insights."})


@app.route("/bug-icon-image", methods=["GET"])
def bug_icon_image():
    for name in ("bug_icon.png", "bug_icon.webp", "bug_icon.jpg", "bug_icon.jpeg", "bug_icon.svg"):
        p = Path(__file__).parent / name
        if p.exists() and p.is_file():
            return send_file(p)
    return ("", 404)


@app.route("/download-html", methods=["GET"])
def download_html():
    session_id = request.args.get("session", "")
    session = _sessions.get(session_id)
    if not session or "html_content" not in session:
        return redirect("/")

    response = make_response(session["html_content"])
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    fname = session.get("html_filename", "cpr.html")
    response.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


@app.route("/download-csv", methods=["GET"])
def download_csv():
    session_id = request.args.get("session", "")
    session = _sessions.get(session_id)
    if not session or "filled_csv" not in session:
        return redirect("/")

    response = make_response(session["filled_csv"])
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    fname = session.get("filled_csv_filename", "filled_pathway.csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
