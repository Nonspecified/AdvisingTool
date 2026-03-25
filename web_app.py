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
from typing import List, Optional
from urllib.parse import quote

import requests
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

APPINSIGHTS_CONNECTION_STRING = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
BUG_TABLE_NAME = "advisingbotbugreports"
CATALOG_API_URL = "https://www.uml.edu/student-dashboard/api/ClassSchedule/RealTime/Search/"

def _cleanup_sessions():
    cutoff = time.time() - 7200
    for k in list(_sessions):
        if _sessions[k].get("ts", 0) < cutoff:
            del _sessions[k]


def _query_catalog(term: str, subject: str, catalog_number: str) -> dict:
    params = {
        "term": term,
        "subjects": subject,
        "partialCatalogNumber": catalog_number,
    }
    try:
        resp = requests.get(CATALOG_API_URL, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Catalog lookup failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Catalog lookup returned invalid JSON: {exc}") from exc

    if not data or "data" not in data:
        raise RuntimeError("Catalog lookup returned no data.")
    return data["data"]


def _matches_status_filter(status_code: str, status_filter: str) -> bool:
    status_code = (status_code or "").upper()
    if status_filter == "open":
        return status_code == "O"
    if status_filter == "open_wait":
        return status_code in {"O", "W"}
    return True


def _compute_effective_enrollment_status(details: dict) -> tuple[str, str]:
    enrollment = details.get("EnrollmentStatus", {}) or {}
    raw_code = (enrollment.get("Code") or "").upper()
    raw_label = (enrollment.get("Description") or "").strip()

    class_status = (details.get("ClassStatus", {}) or {}).get("Code", "")
    class_status = str(class_status or "").upper().strip()

    def _to_int(v):
        try:
            if v is None or str(v).strip() == "":
                return None
            return int(float(v))
        except (TypeError, ValueError):
            return None

    cap = _to_int(details.get("EnrollmentCapacity"))
    total = _to_int(details.get("EnrollmentTotal"))
    wl_cap = _to_int(details.get("WaitListCapacity"))
    wl_total = _to_int(details.get("WaitListTotal"))

    # If section is not active, treat as closed for advising purposes.
    if class_status and class_status != "A":
        return "C", "Closed"

    if raw_code in {"C", "W", "O"}:
        code = raw_code
    else:
        code = "O"

    # Seat/waitlist heuristics to correct stale/misleading tags.
    if cap is not None and total is not None and total >= cap:
        if wl_cap is not None and wl_total is not None and wl_cap > 0 and wl_total < wl_cap:
            return "W", "Wait List"
        return "C", "Closed"

    if code == "O":
        return "O", "Open"
    if code == "W":
        return "W", "Wait List"
    if code == "C":
        return "C", "Closed"
    return "O", raw_label or "Open"


def _meeting_days(meeting: dict) -> List[str]:
    mapping = [
        ("IsMonday", "Mon"),
        ("IsTuesday", "Tue"),
        ("IsWednesday", "Wed"),
        ("IsThursday", "Thu"),
        ("IsFriday", "Fri"),
        ("IsSaturday", "Sat"),
        ("IsSunday", "Sun"),
    ]
    return [abbr for key, abbr in mapping if meeting.get(key)]


def _minutes_from_time(timestr: Optional[str]) -> Optional[int]:
    if not timestr:
        return None
    try:
        hours, minutes, *_ = timestr.split(":")
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def _sections_conflict(sec_a: dict, sec_b: dict) -> bool:
    for m1 in sec_a.get("meetings", []):
        for m2 in sec_b.get("meetings", []):
            if not m1.get("days") or not m2.get("days"):
                continue
            if set(m1["days"]) & set(m2["days"]):
                s1, e1 = m1.get("start"), m1.get("end")
                s2, e2 = m2.get("start"), m2.get("end")
                if s1 is None or e1 is None or s2 is None or e2 is None:
                    continue
                if not (e1 <= s2 or e2 <= s1):
                    return True
    return False


def _ensure_schedule_state(session: dict) -> dict:
    if "schedule" not in session:
        session["schedule"] = {"locked": []}
    return session["schedule"]


def _drop_conflicting_sections(schedule_state: dict, new_section: dict) -> None:
    locked = schedule_state.get("locked", [])
    retained = []
    for section in locked:
        same_component = (
            section.get("subject") == new_section.get("subject")
            and section.get("catalog") == new_section.get("catalog")
            and section.get("component_code") == new_section.get("component_code")
        )
        if same_component or _sections_conflict(section, new_section):
            continue
        retained.append(section)
    schedule_state["locked"] = retained


def _format_meeting(meeting: dict) -> dict:
    days = _meeting_days(meeting)
    return {
        "days": days,
        "start": _minutes_from_time(meeting.get("StartTime") or meeting.get("StartTimeFormatted")),
        "end": _minutes_from_time(meeting.get("EndTime") or meeting.get("EndTimeFormatted")),
        "start_label": meeting.get("StartTimeFormatted") or meeting.get("StartTime") or "TBD",
        "end_label": meeting.get("EndTimeFormatted") or meeting.get("EndTime") or "TBD",
        "location": meeting.get("Facility", {}).get("Description") or "TBD",
        "meeting_id": meeting.get("Number"),
    }


def _format_section_entry(class_entry: dict) -> dict:
  details = class_entry.get("Details", {})
  status_code, status_label = _compute_effective_enrollment_status(details)
  class_number = class_entry.get("ClassNumber") or details.get("ClassNumber")
  section = details.get("Section") or class_entry.get("Section") or details.get("AssociatedClass")
  meetings = [
    _format_meeting(m)
    for m in class_entry.get("Meetings", [])
  ]
  return {
    "id": f"{class_entry.get('Term', {}).get('Code')}-{class_number or class_entry.get('CourseId')}-{details.get('Component', {}).get('Code', '')}",
    "subject": details.get("Subject", ""),
    "catalog": details.get("CatalogNumber", ""),
    "course_title": details.get("CourseTitle", ""),
    "section": str(section or ""),
    "term": class_entry.get("Term", {}).get("Code", ""),
    "component": details.get("Component", {}).get("Description", ""),
    "component_code": details.get("Component", {}).get("Code", ""),
    "status_code": status_code,
    "status_label": status_label,
    "is_open": status_code == "O",
    "meetings": meetings,
    "enrollment_capacity": details.get("EnrollmentCapacity"),
    "enrollment_total": details.get("EnrollmentTotal"),
    "waitlist_capacity": details.get("WaitListCapacity"),
    "waitlist_total": details.get("WaitListTotal"),
    "prereqs": details.get("EnrollmentRequirements", ""),
    "class_number": class_number,
    "session_desc": class_entry.get("Session", {}).get("Description"),
  }


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
                entity[k] = str(v)[:32000]
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
#bug-icon{{background:#9d1010;color:#fff;border:1px solid #ff4d4d;border-radius:6px;
  padding:4px 10px;cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:5px}}
#bug-icon:hover{{background:#c31717;color:#fff}}
.bug-glyph{{display:inline-flex;align-items:center;justify-content:center;line-height:1;font-size:1.15rem}}
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
/* schedule builder panel */
#schedule-panel{{position:fixed;top:60px;right:-430px;bottom:60px;width:420px;max-width:90vw;
  background:#0c1230;border:1px solid #1f2b54;border-radius:10px;box-shadow:0 20px 45px rgba(0,0,0,.55);
  z-index:10003;transition:right .25s ease;display:flex;flex-direction:column;overflow:hidden}}
#schedule-panel.open{{right:18px}}
#schedule-panel-header{{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #19244b}}
#schedule-panel-header h3{{margin:0;font-size:.95rem;color:#a0c4ff}}
#schedule-panel-close{{background:none;border:none;color:#8090b0;font-size:1.35rem;cursor:pointer}}
#schedule-panel-body{{flex:1;overflow-y:auto;padding:10px 16px;display:flex;flex-direction:column;gap:10px}}
.schedule-field{{display:flex;flex-direction:column;gap:4px;font-size:.75rem;color:#9fb0cd}}
.schedule-field input,.schedule-field select{{background:#0d1b2e;border:1px solid #1f2b54;border-radius:6px;padding:6px 10px;color:#e0e0e0;font-size:.85rem}}
.schedule-status-row{{display:flex;gap:8px;font-size:.72rem}}
.schedule-status-row label{{display:flex;align-items:center;gap:4px;cursor:pointer}}
.schedule-results{{display:flex;flex-direction:column;gap:6px;}}
.schedule-result{{border:1px solid #283452;border-radius:6px;padding:8px;display:flex;flex-direction:column;gap:4px;background:#0f1a33}}
.schedule-result-header{{display:flex;align-items:center;justify-content:space-between;gap:6px}}
.schedule-result-title{{font-size:.8rem;color:#e0e0e0}}
.schedule-result-meta{{font-size:.72rem;color:#8693bf}}
.schedule-result button{{align-self:flex-start;background:#0f3460;border:1px solid #1a56c0;border-radius:6px;padding:4px 10px;color:#a0c4ff;font-size:.72rem;cursor:pointer}}
/* schedule visual grid */
.schedule-calendar{{min-height:40px}}
.sched-cal-wrap{{margin-bottom:8px}}
.sched-term-lbl{{font-size:.65rem;color:#8090b0;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px;font-weight:600;padding:0 2px}}
.sched-grid{{border:1px solid #283452;border-radius:6px;background:#080f1e;overflow:hidden}}
.sched-grid-hdr{{display:flex;border-bottom:1px solid #283452}}
.sched-time-hdr{{width:30px;flex-shrink:0;border-right:1px solid #283452}}
.sched-day-hdr{{flex:1;text-align:center;padding:3px 0;color:#8090b0;border-right:1px solid #1e2d50;font-size:.58rem;font-weight:600}}
.sched-day-hdr:last-child{{border-right:none}}
.sched-grid-body{{display:flex}}
.sched-time-col{{width:30px;flex-shrink:0;position:relative;border-right:1px solid #283452}}
.sched-time-tick{{position:absolute;left:0;right:0;text-align:right;padding-right:3px;font-size:.48rem;color:#4a5a7a;transform:translateY(-50%)}}
.sched-day-cols{{flex:1;display:flex}}
.sched-day-col{{flex:1;position:relative;border-right:1px solid #131e35}}
.sched-day-col:last-child{{border-right:none}}
.sched-hour-line{{position:absolute;left:0;right:0;border-top:1px solid #131e35;pointer-events:none}}
.sched-block{{position:absolute;left:1px;right:1px;border-radius:3px;padding:2px 3px;overflow:hidden;display:flex;flex-direction:column;justify-content:center;cursor:default;border-left:3px solid}}
.sched-block:hover .sched-rm{{opacity:1}}
.sched-block.is-selected{{box-shadow:0 0 0 2px #6fd18a inset,0 0 0 1px rgba(111,209,138,.45)}}
.sched-block .sb-c{{font-size:.55rem;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.25}}
.sched-block .sb-t{{font-size:.5rem;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.25}}
.sched-rm{{position:absolute;top:1px;right:2px;background:none;border:none;color:inherit;cursor:pointer;font-size:.75rem;opacity:0;transition:opacity .1s;padding:0;line-height:1}}
.sched-no-time{{display:flex;flex-direction:column;gap:3px;margin-top:5px}}
.sched-no-time-row{{display:flex;align-items:center;justify-content:space-between;padding:4px 8px;border-radius:4px;font-size:.68rem;border:1px solid}}
.sched-no-time-row.is-selected{{box-shadow:0 0 0 2px #6fd18a inset,0 0 0 1px rgba(111,209,138,.45)}}
.sched-no-time-row .sched-rm{{position:static;opacity:1;font-size:.7rem}}
.schedule-summary{{border:1px solid #283452;border-radius:6px;padding:10px;background:#0f1a33;color:#b3c2e1;font-size:.75rem;margin-top:10px;min-height:80px;display:flex;flex-direction:column;gap:6px}}
.schedule-summary-empty{{color:#607090;font-size:.7rem}}
.schedule-summary-entry{{border:1px solid #16213e;border-radius:6px;padding:6px 8px;background:#11193a}}
.schedule-summary-entry strong{{color:#e0e0e0}}
.schedule-summary-entry span{{display:block;font-size:.72rem;color:#9fb0cd}}
.schedule-note{{font-size:.7rem;color:#8090b0}}
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
    <button class="nav-btn" id="schedule-btn" type="button">Schedule Builder</button>
    <div id="export-wrap">
      <button class="nav-btn" id="export-btn" type="button">Export &#9660;</button>
      <div id="export-dropdown">
        <a class="export-item" href="{download_csv_url}" download>Download CSV</a>
        <a class="export-item" href="{download_html_url}" download>Download CPR (HTML)</a>
      </div>
    </div>
    <button class="nav-btn" id="open-minor-btn" type="button">Minors (beta)</button>
    <button class="nav-btn" type="button" onclick="location.href='/'">New Student</button>
    <button id="bug-icon" type="button" title="Report a bug"><svg width="15" height="15" viewBox="0 0 28 28" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M15 2H5a2 2 0 0 0-2 2v20a2 2 0 0 0 2 2h10.5"/><path d="M15 2v6h6" fill="none" stroke="currentColor" stroke-width="1.5"/><rect x="6" y="10" width="6" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="13.5" width="8" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><rect x="6" y="17" width="5" height="1.8" rx=".9" fill="white" fill-opacity=".55"/><circle cx="21" cy="17" r="2.5"/><ellipse cx="21" cy="23" rx="4.5" ry="5"/><path d="M16.5 20l-3.5-1.5M16.5 23l-3.5 0M16.5 26l-3.5 1.5M25.5 20l3.5-1.5M25.5 23l3.5 0M25.5 26l3.5 1.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/><ellipse cx="21" cy="23.5" rx="1.2" ry="2.2" fill="white" fill-opacity=".35"/></svg><span>Report Bug</span></button>
  </div>
</div>

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

<div id="schedule-panel" aria-live="polite">
  <div id="schedule-panel-header">
    <h3>Schedule Builder (beta)</h3>
    <button id="schedule-panel-close" type="button" aria-label="Close">×</button>
  </div>
  <div id="schedule-panel-body">
    <div class="schedule-field">
      <span>Term</span>
      <select id="schedule-term">
        <option value="3610">2026 Fall</option>
        <option value="3530">2026 Spring</option>
        <option value="3620">2027 Spring</option>
        <option value="3540">2026 Summer</option>
      </select>
    </div>
    <div class="schedule-field">
      <span>Course (manual entry or click a course on the map)</span>
      <div style="display:flex;gap:6px;">
        <input id="schedule-subject" placeholder="Subject (e.g., MECH)" maxlength="6">
        <input id="schedule-catalog" placeholder="Catalog #" maxlength="8">
      </div>
    </div>
    <div class="schedule-field">
      <span>Recommended courses</span>
      <select id="schedule-recommended">
        <option value="">Select a recommended course...</option>
      </select>
    </div>
    <div class="schedule-field" style="flex-direction:row;gap:6px;">
      <input id="schedule-latest-class" placeholder="Class #" readonly>
      <input id="schedule-latest-section" placeholder="Section" readonly>
    </div>
    <div class="schedule-status-row">
      <label><input type="radio" name="schedule-status" value="open" checked> Open only</label>
      <label><input type="radio" name="schedule-status" value="open_wait"> Open + waitlist</label>
      <label><input type="radio" name="schedule-status" value="all"> Include closed</label>
    </div>
    <button class="nav-btn" id="schedule-search-btn" type="button">Search sections</button>
    <div id="schedule-message" class="schedule-note"></div>
    <div class="schedule-results" id="schedule-results"></div>
    <div class="schedule-calendar" id="schedule-calendar">Proposed sections will appear here.</div>
    <div class="schedule-summary" id="schedule-summary">
      <div class="schedule-summary-empty">Proposed section details will appear here once you add something.</div>
    </div>
  </div>
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

  var bugIcon = document.getElementById('bug-icon');
  if(bugIcon) bugIcon.addEventListener('click', openBugModal);
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
    setLatestInfo(sections[0]);
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

  /* ── Schedule builder panel ── */
  var schedulePanel = document.getElementById('schedule-panel');
  var scheduleBtn = document.getElementById('schedule-btn');
  var scheduleClose = document.getElementById('schedule-panel-close');
  var scheduleSearch = document.getElementById('schedule-search-btn');
  var scheduleResults = document.getElementById('schedule-results');
  var scheduleCalendar = document.getElementById('schedule-calendar');
  var scheduleMessage = document.getElementById('schedule-message');
  var scheduleRecommended = document.getElementById('schedule-recommended');
  var scheduleLatestClass = document.getElementById('schedule-latest-class');
  var scheduleLatestSection = document.getElementById('schedule-latest-section');
  var currentSections = [];
  var lockedSections = [];
  var selectedLockedSectionId = '';
  var curriculumGrid = document.getElementById('curriculum-grid');

  function formatMeetingSummary(section){{
    var summaries = section.meetings.map(function(meeting){{
      var days = meeting.days.length ? meeting.days.join('/') : 'TBD';
      return days + ' ' + (meeting.start_label || 'TBD');
    }});
    return summaries.join(' | ') || 'No meeting data';
  }}

  function parseCourseId(cid){{
    if(!cid) return null;
    var trimmed = cid.trim().toUpperCase();
    var strict = trimmed.match(/^([A-Z]{{2,6}})[\s.\-]*([0-9]{{3,4}}[A-Z]?)$/);
    if(strict){{
      return {{subject: strict[1], catalog: strict[2]}};
    }}
    var loose = trimmed.match(/([A-Z]{{2,6}})[^A-Z0-9]*([0-9]{{3,4}}[A-Z]?)/);
    return loose ? {{subject: loose[1], catalog: loose[2]}} : null;
  }}

  function updateSelectedCourse(cid, name){{
    var subjectInput = document.getElementById('schedule-subject');
    var catalogInput = document.getElementById('schedule-catalog');
    if(!subjectInput || !catalogInput) return;
    var parsed = parseCourseId(cid);
    if(parsed){{
      subjectInput.value = parsed.subject;
      catalogInput.value = parsed.catalog;
      scheduleMessage.textContent = 'Ready to search ' + parsed.subject + '.' + parsed.catalog;
    }} else {{
      scheduleMessage.textContent = 'Could not parse that map course code.';
    }}
  }}

  function advisorStorageKey(){{
    var studentNameEl = document.querySelector('.student-name');
    var studentName = studentNameEl ? (studentNameEl.textContent || '').trim() : 'unknown';
    studentName = studentName.replace(/\s+/g, '_');
    var header = document.getElementById('page-header');
    var studentId = header ? (header.getAttribute('data-student-id') || '').trim() : '';
    studentId = (studentId || 'unknown').replace(/\s+/g, '_');
    return 'advisingbot-advisor-' + studentName + '-' + studentId + '-' + SESSION;
  }}

  function getAdvisorState(){{
    try {{
      var raw = sessionStorage.getItem(advisorStorageKey());
      if(!raw) return {{}};
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' ? parsed : {{}};
    }} catch(err) {{
      return {{}};
    }}
  }}

  function setAdvisorState(state){{
    try {{
      sessionStorage.setItem(advisorStorageKey(), JSON.stringify(state || {{}}));
    }} catch(err) {{}}
  }}

  function syncLockedToAdvisorStorage(locked){{
    var state = getAdvisorState();
    state.locked_sections = Array.isArray(locked) ? locked : [];
    setAdvisorState(state);
  }}

  function renderPersistentLockedCalendar(locked){{
    var target = document.getElementById('recommended-calendar');
    var wrap = document.getElementById('proposed-sections-wrap');
    if(!target) return;
    var sections = Array.isArray(locked) ? locked : [];
    if(!sections.length){{
      target.innerHTML = '';
      if(wrap) wrap.style.display = 'none';
      return;
    }}
    if(wrap) wrap.style.display = 'block';

    var TERM_LABELS = {{'3610':'2026 Fall','3530':'2026 Spring','3620':'2027 Spring','3540':'2026 Summer'}};
    var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
    var START_MIN = 480;   // 8:00
    var END_MIN = 1200;    // 20:00
    var PX_PER_MIN = 0.22; // compact but readable scale
    var GRID_H = Math.round((END_MIN - START_MIN) * PX_PER_MIN);

    var grouped = {{}};
    sections.forEach(function(sec){{
      var t = sec && sec.term ? String(sec.term) : 'unknown';
      if(!grouped[t]) grouped[t] = [];
      grouped[t].push(sec || {{}});
    }});

    function asMinutes(v){{
      if(v == null || v === '') return null;
      var n = Number(v);
      return Number.isFinite(n) ? n : null;
    }}

    function toEvent(sec, meeting){{
      var code = (sec.subject || '') + '.' + (sec.catalog || '');
      var start = asMinutes(meeting.start);
      var end = asMinutes(meeting.end);
      return {{
        id: sec.id || '',
        code: code,
        section: sec.section || '',
        classNumber: sec.class_number || '',
        title: sec.course_title || sec.component || 'Section',
        start: start,
        end: end,
        startLabel: meeting.start_label || 'TBD',
      }};
    }}

    function renderTermCalendar(termSections){{
      var dayMap = {{Mon:[], Tue:[], Wed:[], Thu:[], Fri:[]}};
      var asyncRows = [];

      termSections.forEach(function(sec){{
        var meetings = Array.isArray(sec.meetings) ? sec.meetings : [];
        var placed = false;
        meetings.forEach(function(m){{
          var days = Array.isArray(m.days) ? m.days : [];
          if(!days.length) return;
          var event = toEvent(sec, m);
          days.forEach(function(day){{
            if(dayMap[day]){{
              dayMap[day].push(event);
              placed = true;
            }}
          }});
        }});
        if(!placed){{
          asyncRows.push(sec);
        }}
      }});

      var columnsHtml = DAYS.map(function(day){{
        var blocks = dayMap[day].map(function(ev){{
          var s = ev.start;
          var e = ev.end;
          var hasTime = s != null && e != null && e > s;
          var top = hasTime ? Math.max(0, Math.round((Math.max(s, START_MIN) - START_MIN) * PX_PER_MIN)) : 2;
          var height = hasTime ? Math.max(18, Math.round((Math.min(e, END_MIN) - Math.max(s, START_MIN)) * PX_PER_MIN)) : 18;
          if(!hasTime || height <= 0){{
            top = 2;
            height = 18;
          }}
          var removeBtn = ev.id
            ? '<button type="button" class="rec-mini-rm" data-unlock-persist="' + ev.id + '" title="Remove proposed section">×</button>'
            : '';
          var meta = 'Sec ' + (ev.section || '??') + (ev.classNumber ? ' · #' + ev.classNumber : '');
          var tt = ev.code + ' ' + meta + ' · ' + ev.title + ' · ' + ev.startLabel;
          var idAttr = ev.id ? ' data-proposed-id="' + ev.id + '"' : '';
          return '<div class="rec-mini-block"' + idAttr + ' style="top:' + top + 'px;height:' + height + 'px" title="' + tt + '">'
            + '<span class="rec-mini-code">' + ev.code + '</span>'
            + '<span class="rec-mini-meta">' + meta + '</span>'
            + removeBtn
            + '</div>';
        }}).join('');
        return '<div class="rec-mini-day">'
          + '<div class="rec-mini-day-hdr">' + day.substring(0,2) + '</div>'
          + '<div class="rec-mini-day-body" style="height:' + GRID_H + 'px">' + blocks + '</div>'
          + '</div>';
      }}).join('');

      var asyncHtml = '';
      if(asyncRows.length){{
        var shown = asyncRows.slice(0, 3);
        asyncHtml = '<div class="rec-mini-async">' + shown.map(function(sec){{
          var code = (sec.subject || '') + '.' + (sec.catalog || '');
          var meta = 'Sec ' + (sec.section || '??') + (sec.class_number ? ' · #' + sec.class_number : '');
          var removeBtn = sec.id
            ? '<button type="button" class="rec-mini-rm" data-unlock-persist="' + sec.id + '" title="Remove proposed section">×</button>'
            : '';
          var idAttr = sec.id ? ' data-proposed-id="' + sec.id + '"' : '';
          return '<span class="rec-mini-async-chip"' + idAttr + '>' + code + ' · ' + meta + removeBtn + '</span>';
        }}).join('');
        if(asyncRows.length > shown.length){{
          asyncHtml += '<span class="rec-mini-more">+' + (asyncRows.length - shown.length) + ' more</span>';
        }}
        asyncHtml += '</div>';
      }}

      return '<div class="rec-mini-grid">' + columnsHtml + '</div>' + asyncHtml;
    }}

    var html = Object.keys(grouped).map(function(term){{
      var label = TERM_LABELS[term] || term;
      var cal = renderTermCalendar(grouped[term]);
      return '<div class="rec-cal-wrap rec-mini-wrap"><div class="rec-cal-term">' + label + '</div>' + cal + '</div>';
    }}).join('');
    target.innerHTML = html;

    function selectProposed(id){{
      if(!id) return;
      target.querySelectorAll('.rec-mini-block.is-selected,.rec-mini-async-chip.is-selected').forEach(function(el){{
        el.classList.remove('is-selected');
      }});
      target.querySelectorAll('[data-proposed-id="' + id + '"]').forEach(function(el){{
        el.classList.add('is-selected');
      }});
    }}

    target.querySelectorAll('.rec-mini-block[data-proposed-id],.rec-mini-async-chip[data-proposed-id]').forEach(function(el){{
      el.addEventListener('click', function(evt){{
        if(evt.target && evt.target.closest('.rec-mini-rm')) return;
        selectProposed(el.getAttribute('data-proposed-id') || '');
      }});
    }});

    target.querySelectorAll('[data-unlock-persist]').forEach(function(btn){{
      btn.addEventListener('click', function(){{
        var id = btn.getAttribute('data-unlock-persist');
        if(!id) return;
        fetch('/schedule/lock', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{session: SESSION, action: 'unlock', section: {{id: id}}}})
        }})
        .then(function(r){{ return r.json(); }})
        .then(function(data){{
          if(!data.ok){{ throw new Error(data.error || 'Unable to remove proposed section'); }}
          scheduleMessage.textContent = 'Removed';
          renderCalendar(data.locked || []);
        }})
        .catch(function(err){{
          scheduleMessage.textContent = err.message || 'Unable to remove proposed section';
        }});
      }});
    }});
  }}

  function loadRecommendedIntoSearch(){{
    if(!scheduleRecommended) return;
    scheduleRecommended.innerHTML = '<option value="">Select a recommended course...</option>';
    var items = [];
    try {{
      var parsed = getAdvisorState();
      if(Array.isArray(parsed.items)) items = parsed.items;
    }} catch(err) {{
      items = [];
    }}

    items.forEach(function(it){{
      if(!it || !it.cid) return;
      var opt = document.createElement('option');
      var cid = String(it.cid).trim();
      var cname = String(it.cname || '').trim();
      var termLabel = String(it.termLabel || '').trim();
      opt.value = cid;
      opt.textContent = cid + (cname ? ' — ' + cname : '') + (termLabel ? ' (' + termLabel + ')' : '');
      opt.dataset.cname = cname;
      scheduleRecommended.appendChild(opt);
    }});
  }}

  function setLatestInfo(section){{
    var classText = '';
    var sectionText = '';
    if(section){{
      if(section.class_number){{
        classText = 'Class #' + section.class_number;
      }}
      if(section.section){{
        sectionText = 'Section ' + section.section;
      }}
    }}
    if(scheduleLatestClass){{
      scheduleLatestClass.value = classText;
    }}
    if(scheduleLatestSection){{
      scheduleLatestSection.value = sectionText;
    }}
  }}

  function sectionsConflict(a, b){{
    var meetingsA = Array.isArray(a.meetings) ? a.meetings : [];
    var meetingsB = Array.isArray(b.meetings) ? b.meetings : [];
    for(var i = 0; i < meetingsA.length; i++){{
      var m1 = meetingsA[i] || {{}};
      var d1 = Array.isArray(m1.days) ? m1.days : [];
      if(!d1.length) continue;
      for(var j = 0; j < meetingsB.length; j++){{
        var m2 = meetingsB[j] || {{}};
        var d2 = Array.isArray(m2.days) ? m2.days : [];
        if(!d2.length) continue;
        var sameDay = d1.some(function(day){{ return d2.indexOf(day) >= 0; }});
        if(!sameDay) continue;
        if(m1.start == null || m1.end == null || m2.start == null || m2.end == null) continue;
        if(!(m1.end <= m2.start || m2.end <= m1.start)) return true;
      }}
    }}
    return false;
  }}

  function conflictsWithProposed(section){{
    return lockedSections.some(function(locked){{
      if(!locked) return false;
      var sameComponent =
        (locked.subject || '') === (section.subject || '') &&
        (locked.catalog || '') === (section.catalog || '') &&
        (locked.component_code || '') === (section.component_code || '');
      if(sameComponent) return true;
      return sectionsConflict(locked, section);
    }});
  }}

  function formatSeatSummary(section){{
    function asNum(v){{
      if(v === null || v === undefined || String(v).trim() === '') return null;
      var n = Number(v);
      return Number.isFinite(n) ? n : null;
    }}
    var cap = asNum(section.enrollment_capacity);
    var total = asNum(section.enrollment_total);
    var wlCap = asNum(section.waitlist_capacity);
    var wlTotal = asNum(section.waitlist_total);

    var enrolled = (total !== null && cap !== null) ? ('Enrolled ' + total + '/' + cap) : 'Enrolled —';
    var waitlist = (wlTotal !== null && wlCap !== null) ? ('Waitlist ' + wlTotal + '/' + wlCap) : 'Waitlist —';
    return enrolled + ' • ' + waitlist;
  }}

  function handleCourseMapClick(evt){{
    var box = evt.target.closest('.course-box');
    if(!box) return;
    var cid = (box.dataset.cid || box.dataset.origCid || (box.querySelector('.cid') && box.querySelector('.cid').textContent) || '').trim();
    if(!cid) return;
    var cname = (box.dataset.origCname || (box.querySelector('.cname') && box.querySelector('.cname').textContent) || '').trim();
    updateSelectedCourse(cid, cname);
  }}

  if(curriculumGrid){{
    curriculumGrid.addEventListener('click', handleCourseMapClick);
  }}

  function renderScheduleResults(sections){{
    currentSections = sections;
    if(!sections.length){{
      setLatestInfo(null);
      scheduleResults.innerHTML = '<div class="schedule-note">No sections match that query.</div>';
      return;
    }}
    scheduleResults.innerHTML = sections.map(function(section){{
      var alreadyProposed = lockedSections.some(function(locked){{ return locked && locked.id === section.id; }});
      var hasConflict = !alreadyProposed && conflictsWithProposed(section);
      var actionHtml = '<button type="button" data-section-id="' + section.id + '">Propose section</button>';
      if(alreadyProposed){{
        actionHtml = '<div class="schedule-note">Already proposed</div>';
      }} else if(hasConflict){{
        actionHtml = '<div class="schedule-note">Conflicts with proposed sections</div>';
      }}
      return '<div class="schedule-result">'
        + '<div class="schedule-result-header">'
        + '<span class="schedule-result-title">' + (section.subject || '') + '.' + (section.catalog || '') + ' · ' + (section.course_title || section.component || 'Section') + '</span>'
        + '<span>' + (section.status_label || section.status_code || '') + '</span>'
        + '</div>'
        + '<div class="schedule-result-meta">Section ' + (section.section || '??') + ' • ' + (section.component || 'Component') + '</div>'
        + '<div class="schedule-result-meta">Class #' + (section.class_number || '??') + ' · ' + (section.session_desc || 'Session') + '</div>'
        + '<div class="schedule-result-meta">' + formatSeatSummary(section) + '</div>'
        + '<div class="schedule-result-meta">' + formatMeetingSummary(section) + '</div>'
        + actionHtml
        + '</div>';
    }}).join('');
    scheduleResults.querySelectorAll('[data-section-id]').forEach(function(btn){{
      btn.addEventListener('click', function(){{
        var id = btn.dataset.sectionId;
        var section = currentSections.find(function(sec){{ return sec.id === id; }});
        if(!section){{
          scheduleMessage.textContent = 'Section data missing, refresh search.';
          return;
        }}
        fetch('/schedule/lock', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{session: SESSION, section: section}})
        }})
        .then(function(r){{ return r.json(); }})
        .then(function(data){{
          if(!data.ok){{ throw new Error(data.error || 'Lock failed'); }}
          selectedLockedSectionId = section.id || '';
          setLatestInfo(section);
          scheduleMessage.textContent = 'Proposed ' + (section.subject || '') + '.' + (section.catalog || '') + ' · Sec ' + (section.section || '');
          renderCalendar(data.locked);
        }})
        .catch(function(err){{
          scheduleMessage.textContent = err.message;
        }});
      }});
    }});
  }}

  function renderCalendar(locked){{
    lockedSections = Array.isArray(locked) ? locked : [];
    if(selectedLockedSectionId && !lockedSections.some(function(s){{ return s && s.id === selectedLockedSectionId; }})){{
      selectedLockedSectionId = '';
    }}
    if(!selectedLockedSectionId && lockedSections.length){{
      selectedLockedSectionId = lockedSections[0].id || '';
    }}
    if(!lockedSections.length){{
      scheduleCalendar.innerHTML = '<div class="schedule-note">No proposed sections yet.</div>';
      renderSummary([]);
      syncLockedToAdvisorStorage([]);
      renderPersistentLockedCalendar([]);
      return;
    }}

    var TERM_LABELS = {{'3610':'2026 Fall','3530':'2026 Spring','3620':'2027 Spring','3540':'2026 Summer'}};
    var COLORS = [
      {{bg:'#0d2a54',bd:'#2a5ab0',tx:'#a0caff'}},
      {{bg:'#0d3a1d',bd:'#2a7a4a',tx:'#90dfa0'}},
      {{bg:'#3a0d3a',bd:'#8a2a8a',tx:'#dfa0df'}},
      {{bg:'#3a2a0d',bd:'#7a5a1a',tx:'#dfb870'}},
      {{bg:'#0d2a3a',bd:'#1a7a9a',tx:'#70c8df'}},
      {{bg:'#3a0d0d',bd:'#8a2a2a',tx:'#df9090'}},
    ];
    var DAYS = ['Mon','Tue','Wed','Thu','Fri','Sat'];
    var S_MIN = 480;   /* 8 AM */
    var E_MIN = 1260;  /* 9 PM */
    var PPM   = 0.85;  /* px per minute */
    var TOTAL_H = Math.round((E_MIN - S_MIN) * PPM);

    /* assign a stable color to each unique course */
    var cmap = {{}};
    var ci = 0;
    lockedSections.forEach(function(s){{
      var k = (s.subject||'')+'.'+(s.catalog||'');
      if(!(k in cmap)){{ cmap[k] = COLORS[ci % COLORS.length]; ci++; }}
    }});

    /* group by term, max 2 */
    var tOrder = [], tMap = {{}};
    lockedSections.forEach(function(s){{
      var t = s.term || 'unknown';
      if(!(t in tMap)){{ tOrder.push(t); tMap[t] = []; }}
      tMap[t].push(s);
    }});
    tOrder = tOrder.slice(0,2);

    function fmtMin(m){{
      if(m==null) return 'TBD';
      var h=Math.floor(m/60), mn=m%60, suf=h<12?'am':'pm';
      if(h>12) h-=12; if(h===0) h=12;
      return h+':'+(mn<10?'0'+mn:mn)+suf;
    }}

    function buildGrid(label, sections){{
      var dayMap = {{}};
      DAYS.forEach(function(d){{ dayMap[d]=[]; }});
      var noTime = [];

      sections.forEach(function(s){{
        var k = (s.subject||'')+'.'+(s.catalog||'');
        var c = cmap[k];
        var hasMtg = s.meetings && s.meetings.some(function(m){{
          return m.days && m.days.length && m.start!=null && m.end!=null;
        }});
        if(!hasMtg){{ noTime.push({{s:s,c:c}}); return; }}
        s.meetings.forEach(function(m){{
          if(!m.days||!m.days.length||m.start==null||m.end==null) return;
          m.days.forEach(function(d){{
            if(dayMap[d]) dayMap[d].push({{s:s,m:m,c:c}});
          }});
        }});
      }});

      /* time column ticks */
      var ticks='';
      for(var h=8;h<=21;h++){{
        var top=Math.round((h*60-S_MIN)*PPM);
        var lbl=h<12?h+'am':h===12?'12pm':(h-12)+'pm';
        ticks+='<div class="sched-time-tick" style="top:'+top+'px">'+lbl+'</div>';
      }}

      /* day columns */
      var dcols=DAYS.map(function(day){{
        var lines='',bks='';
        for(var h=8;h<=21;h++){{
          lines+='<div class="sched-hour-line" style="top:'+Math.round((h*60-S_MIN)*PPM)+'px"></div>';
        }}
        dayMap[day].forEach(function(e){{
          var top=Math.max(0,Math.round((e.m.start-S_MIN)*PPM));
          var ht =Math.max(16,Math.round((e.m.end-e.m.start)*PPM));
          var k  =(e.s.subject||'')+'.'+(e.s.catalog||'');
          var selectedCls = (e.s.id && e.s.id === selectedLockedSectionId) ? ' is-selected' : '';
          bks+='<div class="sched-block'+selectedCls+'" data-select-locked="'+(e.s.id || '')+'" style="top:'+top+'px;height:'+ht+'px;background:'+e.c.bg+';border-color:'+e.c.bd+';color:'+e.c.tx+'"'
              +' title="'+k+' Sec '+(e.s.section||'')+' '+fmtMin(e.m.start)+'–'+fmtMin(e.m.end)+'">'
              +'<div class="sb-c">'+k+'</div>'
              +'<div class="sb-t">'+fmtMin(e.m.start)+'–'+fmtMin(e.m.end)+'</div>'
              +'<button class="sched-rm" data-unlock="'+e.s.id+'">×</button>'
              +'</div>';
        }});
        return '<div class="sched-day-col">'+lines+bks+'</div>';
      }}).join('');

      var hdrs=DAYS.map(function(d){{
        return '<div class="sched-day-hdr">'+d.substring(0,2)+'</div>';
      }}).join('');

      /* online/async (no meeting time) rows */
      var noHtml='';
      if(noTime.length){{
        noHtml='<div class="sched-no-time">'
          +noTime.map(function(e){{
            var k=(e.s.subject||'')+'.'+(e.s.catalog||'')+' Sec '+(e.s.section||'');
            var selectedCls = (e.s.id && e.s.id === selectedLockedSectionId) ? ' is-selected' : '';
            return '<div class="sched-no-time-row'+selectedCls+'" data-select-locked="'+(e.s.id || '')+'" style="background:'+e.c.bg+';border-color:'+e.c.bd+';color:'+e.c.tx+'">'
              +'<span>'+k+' (online/async)</span>'
              +'<button class="sched-rm" data-unlock="'+e.s.id+'" style="opacity:1">× Remove</button>'
              +'</div>';
          }}).join('')
          +'</div>';
      }}

      return '<div class="sched-cal-wrap">'
        +'<div class="sched-term-lbl">'+label+'</div>'
        +'<div class="sched-grid">'
        +'<div class="sched-grid-hdr"><div class="sched-time-hdr"></div>'+hdrs+'</div>'
        +'<div class="sched-grid-body">'
        +'<div class="sched-time-col" style="height:'+TOTAL_H+'px">'+ticks+'</div>'
        +'<div class="sched-day-cols" style="height:'+TOTAL_H+'px">'+dcols+'</div>'
        +'</div></div>'+noHtml
        +'</div>';
    }}

    scheduleCalendar.innerHTML = tOrder.map(function(t){{
      return buildGrid(TERM_LABELS[t]||t, tMap[t]);
    }}).join('');

    scheduleCalendar.querySelectorAll('[data-select-locked]').forEach(function(el){{
      el.addEventListener('click', function(evt){{
        if(evt.target && evt.target.closest('.sched-rm')) return;
        var id = el.getAttribute('data-select-locked') || '';
        if(!id) return;
        selectedLockedSectionId = id;
        renderCalendar(lockedSections);
      }});
    }});

    /* attach unlock handlers to all × buttons */
    scheduleCalendar.querySelectorAll('.sched-rm[data-unlock]').forEach(function(btn){{
      btn.addEventListener('click', function(e){{
        e.stopPropagation();
        var id = btn.dataset.unlock;
        fetch('/schedule/lock', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{session: SESSION, action: 'unlock', section: {{id: id}}}})
        }})
        .then(function(r){{ return r.json(); }})
        .then(function(data){{
          if(!data.ok){{ throw new Error(data.error || 'Unable to unlock'); }}
          scheduleMessage.textContent = 'Removed';
          renderCalendar(data.locked);
        }})
        .catch(function(err){{
          scheduleMessage.textContent = err.message;
        }});
      }});
    }});
    syncLockedToAdvisorStorage(lockedSections);
    renderPersistentLockedCalendar(lockedSections);
    renderSummary(lockedSections);
  }}

  function renderSummary(locked){{
    var summary = document.getElementById('schedule-summary');
    if(!summary) return;
    if(!locked.length){{
      summary.innerHTML = '<div class="schedule-summary-empty">Proposed section details will appear here once you add something.</div>';
      return;
    }}
    summary.innerHTML = locked.map(function(section){{
      return '<div class="schedule-summary-entry">'
        + '<strong>' + (section.subject || '') + '.' + (section.catalog || '') + ' · Sec ' + (section.section || '') + '</strong>'
        + '<span>' + (section.course_title || section.component || 'Section') + '</span>'
        + '<span>Class #' + (section.class_number || 'TBD') + ' · ' + (section.session_desc || 'Session') + '</span>'
        + '<span>' + formatMeetingSummary(section) + '</span>'
        + '<span>Status: ' + (section.status_label || section.status_code || 'Unknown') + '</span>'
        + '</div>';
    }}).join('');
  }}

  function refreshCalendar(){{
    fetch('/schedule/calendar?session=' + encodeURIComponent(SESSION))
      .then(function(r){{ return r.json(); }})
      .then(function(data){{
        if(!data.ok){{ throw new Error(data.error || 'Unable to load calendar'); }}
        renderCalendar(data.locked);
      }})
      .catch(function(err){{
        scheduleCalendar.innerHTML = '<div class="schedule-note">' + err.message + '</div>';
      }});
  }}

  function getStatusFilter(){{
    var checked = document.querySelector('input[name="schedule-status"]:checked');
    return checked ? checked.value : 'open';
  }}

  function runSearch(subject, catalog){{
    var term = document.getElementById('schedule-term').value;
    if(!subject || !catalog){{
      scheduleMessage.textContent = 'Enter both subject and catalog number.';
      return;
    }}
    scheduleMessage.textContent = 'Searching...';
    scheduleResults.innerHTML = '';
    fetch('/schedule/search', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        session: SESSION,
        term: term,
        subject: subject,
        catalog: catalog,
        status_filter: getStatusFilter(),
      }})
    }})
    .then(function(r){{ return r.json(); }})
    .then(function(data){{
      if(!data.ok){{ throw new Error(data.error || 'Search failed'); }}
      scheduleMessage.textContent = data.sections.length + ' section(s) found.';
      renderScheduleResults(data.sections);
    }})
    .catch(function(err){{
      scheduleMessage.textContent = err.message;
    }});
  }}

  scheduleBtn.addEventListener('click', function(){{
    loadRecommendedIntoSearch();
    schedulePanel.classList.add('open');
    refreshCalendar();
  }});
  scheduleClose.addEventListener('click', function(){{
    schedulePanel.classList.remove('open');
  }});
  scheduleSearch.addEventListener('click', function(){{
    runSearch(document.getElementById('schedule-subject').value, document.getElementById('schedule-catalog').value);
  }});
  if(scheduleRecommended){{
    scheduleRecommended.addEventListener('change', function(){{
      var selected = scheduleRecommended.options[scheduleRecommended.selectedIndex];
      if(!selected || !selected.value) return;
      updateSelectedCourse(selected.value, selected.dataset.cname || '');
    }});
  }}

  (function initPersistentLockedCalendar(){{
    var state = getAdvisorState();
    renderPersistentLockedCalendar(Array.isArray(state.locked_sections) ? state.locked_sections : []);
  }})();

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


@app.route("/schedule/search", methods=["POST"])
def schedule_search():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session") or "").strip()
    if not session_id or session_id not in _sessions:
        return jsonify({"ok": False, "error": "Invalid session."}), 400

    term = (payload.get("term") or "").strip()
    subject = (payload.get("subject") or "").strip().upper()
    catalog = (payload.get("catalog") or "").strip()
    status_filter = payload.get("status_filter", "open")
    if not term or not subject or not catalog:
        return jsonify({"ok": False, "error": "Term, subject, and catalog number are required."}), 400

    try:
        catalog_data = _query_catalog(term, subject, catalog)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    sections = []
    for cls in catalog_data.get("Classes", []):
        details = cls.get("Details", {})
        status_code, _ = _compute_effective_enrollment_status(details)
        if not _matches_status_filter(status_code, status_filter):
            continue
        sections.append(_format_section_entry(cls))

    session = _sessions[session_id]
    schedule_state = _ensure_schedule_state(session)
    schedule_state["last_search"] = {
        "term": term,
        "subject": subject,
        "catalog": catalog,
        "status_filter": status_filter,
        "sections": sections,
    }

    return jsonify({
        "ok": True,
        "sections": sections,
        "filters_used": catalog_data.get("SearchFiltersUsed", {}),
        "quick_filters": catalog_data.get("QuickSearchFilterData", {}),
    })


@app.route("/schedule/lock", methods=["POST"])
def schedule_lock():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get("session") or "").strip()
    action = (payload.get("action") or "lock").strip().lower()
    section = payload.get("section")

    if not session_id or session_id not in _sessions:
        return jsonify({"ok": False, "error": "Invalid session."}), 400

    session = _sessions[session_id]
    schedule_state = _ensure_schedule_state(session)

    if action == "unlock" and section and section.get("id"):
        schedule_state["locked"] = [s for s in schedule_state["locked"] if s.get("id") != section.get("id")]
        return jsonify({"ok": True, "locked": schedule_state["locked"]})

    if not section or not section.get("id"):
        return jsonify({"ok": False, "error": "Section payload missing or malformed."}), 400

    _drop_conflicting_sections(schedule_state, section)
    if not any(s.get("id") == section.get("id") for s in schedule_state.get("locked", [])):
        schedule_state.setdefault("locked", []).append(section)

    return jsonify({"ok": True, "locked": schedule_state["locked"]})


@app.route("/schedule/calendar", methods=["GET"])
def schedule_calendar():
    session_id = (request.args.get("session") or "").strip()
    if not session_id or session_id not in _sessions:
        return jsonify({"ok": False, "error": "Invalid session."}), 400

    schedule_state = _ensure_schedule_state(_sessions[session_id])
    return jsonify({"ok": True, "locked": schedule_state.get("locked", [])})


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
