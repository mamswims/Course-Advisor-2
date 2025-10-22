from __future__ import annotations
import json, os, lzma, re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from mcp.server.fastmcp import FastMCP

app = FastMCP("course-sections-service")

# ────────────────────────────────────────────────────────────────────
# Dataset config & loader
# ────────────────────────────────────────────────────────────────────

@dataclass
class SectionsConfig:
    """Configuration for locating the sections dataset."""
    path_env: str = "SECTIONS_PATH"
    default_paths: Tuple[str, ...] = (
        "Sections-26SP.json.lzma", "Sections-26SP.json", "sections.json.lzma", "sections.json"
    )

CFG = SectionsConfig()

@lru_cache(maxsize=1)
def load_sections() -> List[Dict[str, Any]]:
    """
    Load the sections dataset (JSON or JSON-LZMA). Supports common top-level schemas:
    - raw list[section]
    - {"report": {"rows" | "data" | "sections": list[section]}}
    """
    path = os.environ.get(CFG.path_env)
    if not path:
        for c in CFG.default_paths:
            if os.path.exists(c):
                path = c
                break
    if not path:
        raise FileNotFoundError(
            "Missing sections dataset. Set SECTIONS_PATH or place a known filename "
            "in the working directory (Sections-26SP.json.lzma / sections.json …)."
        )

    if path.endswith(".lzma"):
        with lzma.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)

    if isinstance(raw, dict) and "report" in raw:
        rep = raw["report"]
        for k in ("rows", "data", "sections"):
            if isinstance(rep, dict) and isinstance(rep.get(k), list):
                return rep[k]
        if isinstance(rep, list):
            return rep
    if isinstance(raw, list):
        return raw
    raise ValueError("Unrecognized schema for sections JSON")

# ────────────────────────────────────────────────────────────────────
# Normalization helpers — MAPPED TO YOUR KEYS
# (from your probe)
# ['AcadPeriod','Section_Name','Section_Title','Subject','Crs_Number','Sec_Number',
#  'Instructors','MtgPattern','StartDt','EndDt','Locations','InstrFormat','Delivery',
#  'AcadLevel','Hours','EnrollCap','Campus','Status','Desc','Period_RefID']
# ────────────────────────────────────────────────────────────────────

def _norm(s): return (s or "").strip()
def _contains(a, b): return b.lower() in a.lower()

def sec_raw_id(s) -> str:
    """
    Build a stable ID (dataset lacks a dedicated 'SectionId').
    Prefer: '{Subject} {Crs_Number}-{Sec_Number}', else Section_Name, else title|period.
    """
    subj = _norm(s.get("Subject"))
    num  = _norm(str(s.get("Crs_Number") or ""))
    sec  = _norm(str(s.get("Sec_Number") or ""))
    name = _norm(s.get("Section_Name"))
    if subj and num and sec:
        return f"{subj} {num}-{sec}"
    if name:
        return name
    return f"{_norm(s.get('Section_Title'))}|{_norm(s.get('Period_RefID'))}"

def sec_id(s): return sec_raw_id(s)

def sec_name(s):
    name = _norm(s.get("Section_Name"))
    if name:
        return name
    subj = _norm(s.get("Subject"))
    num  = _norm(str(s.get("Crs_Number") or ""))
    sec  = _norm(str(s.get("Sec_Number") or ""))
    return f"{subj} {num}-{sec}".strip()

def sec_title(s):    return _norm(s.get("Section_Title") or s.get("CourseTitle") or s.get("course_title") or "")
def sec_desc(s):     return _norm(s.get("Desc") or s.get("CourseDescription") or s.get("description") or "")
def sec_dept(s):     return _norm(s.get("Subject") or s.get("Department") or s.get("department") or "")

def sec_level(s):
    """
    Use explicit 'AcadLevel' (e.g., '300') when present; otherwise infer from course number.
    Returns canonical '100'/'200'/'300'/'400' or '' if unknown.
    """
    lvl = _norm(str(s.get("AcadLevel") or ""))
    if re.fullmatch(r"[1-4]00", lvl):
        return lvl
    num = _norm(str(s.get("Crs_Number") or ""))
    m = re.search(r"(\d)", num)
    return (m.group(1) + "00") if m else ""

def sec_time_str(s):
    """
    Prefer 'MtgPattern' (often like 'MWF 09:30 AM - 10:20 AM').
    Fall back to a simple 'StartDt – EndDt' range if needed.
    """
    mtg = _norm(s.get("MtgPattern"))
    if mtg:
        return mtg
    start = _norm(s.get("StartDt"))
    end   = _norm(s.get("EndDt"))
    return f"{start} – {end}".strip(" –")

# Buckets for time-of-day. Parse the first HH:MM found (24h or 12h with AM/PM).
_DEF_BINS = {
    "morning":   ("06:00", "11:59"),
    "afternoon": ("12:00", "17:00"),
    "evening":   ("17:01", "22:59"),
}

def _time_in_bin(t: str, label: str) -> bool:
    if label not in _DEF_BINS or not t:
        return False
    # capture HH:MM from 'TTH | 10:20 AM - 12:00 PM' → '10:20'
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if not m:
        return False
    hh = int(m.group(1)); mm = m.group(2)
    # try to detect PM if present before the dash
    pre = t[:t.find("-")] if "-" in t else t
    if re.search(r"\bPM\b", pre, re.IGNORECASE) and hh != 12:
        hh += 12
    if re.search(r"\bAM\b", pre, re.IGNORECASE) and hh == 12:
        hh = 0
    start = f"{hh:02d}:{mm}"
    lo, hi = _DEF_BINS[label]
    return lo <= start <= hi

# ────────────────────────────────────────────────────────────────────
# MCP tools
# ────────────────────────────────────────────────────────────────────

@app.tool()
def find_courses(query: str) -> List[Dict[str, Any]]:
    """
    Text search over Section_Title + Desc.
    Args:
        query: free text (e.g., "AI", "ethics", "security", "writing").
    Returns: list of concise section summaries.
    """
    q = _norm(query)
    out = []
    for s in load_sections():
        if _contains(sec_title(s) + "\n" + sec_desc(s), q):
            out.append({
                "section_id": sec_id(s),
                "section_name": sec_name(s),
                "course_title": sec_title(s),
                "department": sec_dept(s),
                "level": sec_level(s),
                "time": sec_time_str(s),
            })
    return out

@app.tool()
def find_sections(course_title: str) -> List[Dict[str, Any]]:
    """
    Return all sections whose Section_Title contains the given string (case-insensitive).
    Args:
        course_title: e.g., "Machine Learning", "Software Eng".
    """
    q = _norm(course_title)
    out = []
    for s in load_sections():
        if _contains(sec_title(s), q):
            out.append({
                "section_id": sec_id(s),
                "section_name": sec_name(s),
                "course_title": sec_title(s),
                "time": sec_time_str(s),
            })
    return out

@app.tool()
def find_sections_by_department(department: str) -> List[Dict[str, Any]]:
    """
    Filter by department/subject code (e.g., "CS", "ENGL", "CSCI").
    Uses *prefix* match so 'CS' matches 'CS' and 'CSCI'.
    """
    d = _norm(department).upper()
    out = []
    for s in load_sections():
        subj = sec_dept(s).upper()
        if subj == d or subj.startswith(d):
            out.append({
                "section_id": sec_id(s),
                "section_name": sec_name(s),
                "course_title": sec_title(s),
                "time": sec_time_str(s),
            })
    return out

@app.tool()
def find_sections_by_level(level: str) -> List[Dict[str, Any]]:
    """
    Filter by level ("100","200","300","400" or "1xx"/"3xx").
    """
    m = re.search(r"(\d)00", level) or re.search(r"(\d)xx", level, re.I)
    lvl = m.group(1) + "00" if m else (re.sub(r"\D", "", level)[:1] + "00" if re.sub(r"\D", "", level) else "")
    return [{
        "section_id": sec_id(s),
        "section_name": sec_name(s),
        "course_title": sec_title(s),
    } for s in load_sections() if sec_level(s) == lvl]

@app.tool()
def find_sections_by_time(time_of_day: str) -> List[Dict[str, Any]]:
    """
    Filter by time bucket:
      - "morning"   (06:00–11:59)
      - "afternoon" (12:00–17:00)
      - "evening"   (17:01–22:59)
    """
    b = _norm(time_of_day).lower()
    return [{
        "section_id": sec_id(s),
        "section_name": sec_name(s),
        "course_title": sec_title(s),
    } for s in load_sections() if _time_in_bin(sec_time_str(s), b)]

@app.tool()
def get_section_details(section_id: str) -> Dict[str, Any]:
    """
    Return the raw section record for a specific section_id (our synthesized ID).
    Example accepted IDs: 'ENGL 300-A', 'CSCI 262-01'.
    """
    sid = _norm(section_id)
    for s in load_sections():
        if sec_id(s) == sid:
            return json.loads(json.dumps(s))
    return {"error": "not found", "section_id": sid}

@app.tool()
def find_sections_filtered(
    query: Optional[str] = None,
    department: Optional[str] = None,
    level: Optional[str] = None,
    time_of_day: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Combined filter over text/department/level/time_of_day.
    Department uses prefix match so 'CS' finds 'CSCI', etc.
    """
    def _n(s): return (s or "").strip()
    dep = _n(department).upper() if department else None
    q   = _n(query) if query else None

    lvl = None
    if level:
        m = (re.search(r"(\d)\s*00", level) or
             re.search(r"(\d)\s*xx", level, re.I) or
             re.search(r"level\s*(\d)", level, re.I))
        if m:
            lvl = m.group(1) + "00"
        else:
            digits = re.sub(r"\D", "", level)
            lvl = (digits[0] + "00") if digits else None

    bucket = _n(time_of_day).lower() if time_of_day else None

    out: List[Dict[str, Any]] = []
    for s in load_sections():
        title = sec_title(s)
        desc  = sec_desc(s)

        if q and (q.lower() not in (title + "\n" + desc).lower()):
            continue

        if dep:
            subj = sec_dept(s).upper()
            if not (subj == dep or subj.startswith(dep)):
                continue

        if lvl and sec_level(s) != lvl:
            continue

        if bucket and not _time_in_bin(sec_time_str(s), bucket):
            continue

        out.append({
            "section_id": sec_id(s),
            "section_name": sec_name(s),
            "course_title": title,
            "department": sec_dept(s),
            "level": sec_level(s),
            "time": sec_time_str(s),
            "preview": (desc[:220] + "…") if desc else "",
        })
        if len(out) >= max(1, int(limit)):
            break
    return out

if __name__ == "__main__":
    app.run()
