"""Staff Scheduler — local Streamlit app.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import secrets

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from scheduler import DAY_NAMES, db, seed
from scheduler.solver import (
    Employee,
    LockedShift,
    Request,
    SolveConfig,
    solve,
)

st.set_page_config(page_title="Staff Scheduler", page_icon="🗓️", layout="wide")
db.init_db()

DAY_OPTIONS = list(DAY_NAMES)  # Mon..Sun


# --------------------------------------------------------------------------------------
# Authentication — single password, PBKDF2 hash stored in the local DB (meta table).
# First run asks you to create it; log in once per browser session afterwards.
# Forgot it? Delete the row:  DELETE FROM meta WHERE key='auth_pw';  (see README)
# --------------------------------------------------------------------------------------
def _hash_pw(pw: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)


def _store_pw(pw: str):
    salt = secrets.token_bytes(16)
    db.set_meta("auth_pw", f"{salt.hex()}${_hash_pw(pw, salt).hex()}")


def _verify_pw(pw: str, stored: str) -> bool:
    salt_hex, hash_hex = stored.split("$", 1)
    return hmac.compare_digest(_hash_pw(pw, bytes.fromhex(salt_hex)), bytes.fromhex(hash_hex))


def require_auth():
    if st.session_state.get("authed"):
        return
    stored = db.get_meta("auth_pw")
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("🔐 Staff Scheduler")
        if stored is None:
            st.info("**First run** — create the password that will protect this app.")
            with st.form("auth_setup"):
                p1 = st.text_input("New password", type="password")
                p2 = st.text_input("Confirm password", type="password")
                ok = st.form_submit_button("Set password", type="primary",
                                           use_container_width=True)
            if ok:
                if len(p1) < 8:
                    st.error("Use at least 8 characters.")
                elif p1 != p2:
                    st.error("Passwords don't match.")
                else:
                    _store_pw(p1)
                    st.session_state["authed"] = True
                    st.rerun()
        else:
            with st.form("auth_login"):
                pw = st.text_input("Password", type="password")
                ok = st.form_submit_button("Log in", type="primary",
                                           use_container_width=True)
            if ok:
                if _verify_pw(pw, stored):
                    st.session_state["authed"] = True
                    st.rerun()
                else:
                    st.error("Wrong password.")
    st.stop()


require_auth()


# --------------------------------------------------------------------------------------
# Loaders — read normalized data out of SQLite
# --------------------------------------------------------------------------------------
def load_employees(active_only: bool = False) -> pd.DataFrame:
    df = db.load_df("employees")
    if df.empty:
        df = pd.DataFrame(
            columns=["id", "name", "target_hours", "max_hours", "min_hours", "active"]
        )
    if active_only and not df.empty:
        df = df[df["active"] == 1]
    return df


def load_locations() -> pd.DataFrame:
    return db.load_df("locations")


def loc_maps():
    locs = load_locations()
    id2name = dict(zip(locs["id"], locs["name"]))
    name2id = {v: k for k, v in id2name.items()}
    return locs, id2name, name2id


def allowed_locations_map() -> dict[int, set[int]]:
    df = db.load_df("employee_locations")
    out: dict[int, set[int]] = {}
    for _, r in df.iterrows():
        out.setdefault(int(r["employee_id"]), set()).add(int(r["location_id"]))
    return out


def allowed_days_map() -> dict[int, set[int]]:
    df = db.load_df("employee_days")
    out: dict[int, set[int]] = {}
    for _, r in df.iterrows():
        out.setdefault(int(r["employee_id"]), set()).add(int(r["day"]))
    return out


def coverage_map() -> dict[tuple[int, int], float]:
    df = db.load_df("coverage")
    return {(int(r["location_id"]), int(r["day"])): float(r["required_hours"]) for _, r in df.iterrows()}


def employee_hours_map() -> dict[tuple[int, int], tuple[float, float]]:
    """Hourly availability windows: (employee_id, day) -> (start, end). Missing = any time."""
    df = db.load_df("employee_hours")
    return {
        (int(r["employee_id"]), int(r["day"])): (float(r["start_hour"]), float(r["end_hour"]))
        for _, r in df.iterrows()
    }


def parse_hours_cell(val):
    """'8-20' -> (8.0, 20.0); ''/'closed'/'off' -> None; anything else -> ValueError."""
    s = str(val if val is not None else "").strip().lower()
    s = s.replace("–", "-").replace("—", "-").replace(" ", "")
    if s in ("", "closed", "off", "x", "no", "nan", "none"):
        return None
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(val)
    o, c = float(parts[0]), float(parts[1])
    if not (0 <= o < 24 and 0 < c <= 24 and c > o):
        raise ValueError(val)
    return (o, c)


def open_close_map() -> dict[tuple[int, int], tuple[float, float]]:
    """Per-store, per-weekday open window. Missing key = closed that day."""
    df = db.load_df("location_hours")
    out: dict[tuple[int, int], tuple[float, float]] = {}
    for _, r in df.iterrows():
        if pd.notna(r["open_hour"]) and pd.notna(r["close_hour"]):
            out[(int(r["location_id"]), int(r["day"]))] = (
                float(r["open_hour"]), float(r["close_hour"])
            )
    return out


# --- Time / calendar helpers ----------------------------------------------------------
# One color per EMPLOYEE (dark enough for white text, distinct hues).
PALETTE = [
    "#4F6BED", "#CA5010", "#038387", "#C239B3", "#498205",
    "#5B5FC7", "#D13438", "#B87A00", "#00666D", "#8764B8",
    "#0F6CBD", "#A4262C", "#286C2B", "#986F0B", "#5C2E91",
    "#207868", "#C4314B", "#8E562E", "#556B2F", "#7A5FA8",
]


def emp_color(employee_id: int, ordered_ids: list[int]) -> str:
    try:
        idx = ordered_ids.index(employee_id)
    except ValueError:
        idx = 0
    if idx < len(PALETTE):
        return PALETTE[idx]
    hue = (idx * 137.508) % 360  # golden-angle fallback beyond the curated palette
    return f"hsl({hue:.0f}, 62%, 42%)"


def hours_to_time(h: float) -> dt.time:
    h = max(0.0, min(23.98, float(h)))
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return dt.time(hour=min(hh, 23), minute=mm)


def time_to_hours(t) -> float:
    if t is None or (isinstance(t, float) and pd.isna(t)):
        return 9.0
    if isinstance(t, dt.time):
        return t.hour + t.minute / 60.0
    return float(t)


def fmt_time(h: float) -> str:
    t = hours_to_time(h)
    hr = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{hr}:{t.minute:02d} {ampm}" if t.minute else f"{hr} {ampm}"


def _merge_intervals(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    iv = sorted(iv)
    out: list[tuple[float, float]] = []
    for s, e in iv:
        if out and s <= out[-1][1] + 1e-9:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def assign_default_starts(records: list[dict],
                          oc: dict[tuple[int, int], tuple[float, float]],
                          avail: dict[tuple[int, int], tuple[float, float]] | None = None):
    """Place shifts without a start so each location/day's open->close window is covered
    with NO gaps whenever the assigned hours allow it. Each shift may only start within
    its employee's availability window (intersected with store hours); at each uncovered
    moment we pick, among the people who can be there, the one whose shift extends
    coverage furthest. Once the window is covered, extras are staggered for depth."""
    from collections import defaultdict

    groups: dict[tuple[int, int], dict[str, list[dict]]] = defaultdict(
        lambda: {"fixed": [], "free": []}
    )
    for r in records:
        key = (r["location_id"], r["day"])
        groups[key]["free" if r.get("start") is None else "fixed"].append(r)

    for (lid, d), g in groups.items():
        o, c = oc.get((lid, d), (8.0, 20.0))
        intervals = [(r["start"], r["start"] + r["hours"]) for r in g["fixed"]]
        pending = []
        for r in g["free"]:
            lo, hi = o, c
            av = avail.get((r.get("employee_id"), d)) if avail else None
            if av:
                lo, hi = max(lo, av[0]), min(hi, av[1])
            if hi < lo:  # availability doesn't intersect store hours at all
                lo, hi = av[0], av[1]
            pending.append({
                "r": r,
                "es": lo,                                  # earliest start
                "ls": max(lo, hi - r["hours"]),            # latest start that still fits
            })
        extra_i = 0
        while pending:
            merged = _merge_intervals(intervals)
            p = o  # earliest uncovered moment in the window
            for s, e in merged:
                if s <= p + 1e-9:
                    p = max(p, e)
                else:
                    break
            if p < c - 1e-9:
                # Who can actually cover the frontier moment p?
                cands = [x for x in pending
                         if x["es"] <= p + 1e-9
                         and min(p, x["ls"]) + x["r"]["hours"] > p + 1e-9]
                if cands:
                    best = max(cands, key=lambda x: min(p, x["ls"]) + x["r"]["hours"])
                    start = min(p, best["ls"])
                else:
                    # Nobody available at p — place the earliest-available shift at its
                    # earliest start; the remaining hole is flagged by the gap checker.
                    best = min(pending, key=lambda x: x["es"])
                    start = best["es"]
            else:
                # Window covered — stagger extras inside their own windows for depth.
                best = pending[0]
                span = max(best["ls"] - best["es"], 0.0)
                start = best["es"] + span * ((extra_i % 4) / 4.0)
                extra_i += 1
            start = max(best["es"], min(start, best["ls"]))
            best["r"]["start"] = round(start * 4) / 4  # snap to 15 min
            intervals.append((best["r"]["start"], best["r"]["start"] + best["r"]["hours"]))
            pending.remove(best)


def coverage_time_gaps(records: list[dict],
                       oc: dict[tuple[int, int], tuple[float, float]],
                       required: dict[tuple[int, int], float]):
    """Uncovered stretches of each store's open window, on days coverage is required.
    Returns (location_id, day, gap_start, gap_end, kind) where kind is 'gap' or 'closed'."""
    from collections import defaultdict

    by: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for r in records:
        if r.get("start") is not None and r["hours"] > 0:
            by[(r["location_id"], r["day"])].append((r["start"], r["start"] + r["hours"]))

    out = []
    for (lid, d), req in sorted(required.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if req <= 0:
            continue
        win = oc.get((lid, d))
        if win is None:
            out.append((lid, d, None, None, "closed"))
            continue
        o, c = win
        p = o
        for s, e in _merge_intervals(by.get((lid, d), [])):
            s2, e2 = max(s, o), min(e, c)
            if s2 > p + 1 / 60:
                out.append((lid, d, p, s2, "gap"))
            p = max(p, e2)
            if p >= c - 1e-9:
                break
        if p < c - 1 / 60:
            out.append((lid, d, p, c, "gap"))
    return out


def _pack_day(evs: list[dict]):
    """Assign each event a column index + column count so overlapping blocks sit
    side-by-side (Google/Teams-calendar style)."""
    evs.sort(key=lambda e: (e["start"], e["end"]))
    cluster: list[dict] = []
    cluster_end = None

    def flush(cl):
        cols: list[list[dict]] = []
        for ev in cl:
            for ci, col in enumerate(cols):
                if col[-1]["end"] <= ev["start"] + 1e-9:
                    col.append(ev)
                    ev["col"] = ci
                    break
            else:
                ev["col"] = len(cols)
                cols.append([ev])
        for ev in cl:
            ev["ncols"] = len(cols)

    for ev in evs:
        if cluster_end is not None and ev["start"] >= cluster_end - 1e-9:
            flush(cluster)
            cluster, cluster_end = [], None
        cluster.append(ev)
        cluster_end = ev["end"] if cluster_end is None else max(cluster_end, ev["end"])
    if cluster:
        flush(cluster)


def build_calendar_html(records: list[dict],
                        week_start: dt.date | None) -> tuple[str, int]:
    """Teams/Outlook-style week grid. Each record needs a precomputed `color`.
    Overlapping blocks cascade (wide, offset, stacked) instead of splitting width."""
    PX_PER_HOUR = 56
    HEADER_H = 54
    GUTTER = 64

    if records:
        win_start = min(6.0, min(r["start"] for r in records))
        win_end = max(20.0, max(r["start"] + r["hours"] for r in records))
    else:
        win_start, win_end = 7.0, 20.0
    win_start = float(int(win_start))
    win_end = float(min(24, int(win_end) + (1 if win_end != int(win_end) else 0)))
    total_h = max(1.0, win_end - win_start)
    body_h = int(total_h * PX_PER_HOUR)

    by_day: dict[int, list[dict]] = {d: [] for d in range(7)}
    for r in records:
        ev = dict(r)
        ev["end"] = ev["start"] + ev["hours"]
        by_day[ev["day"]].append(ev)
    for d in range(7):
        _pack_day(by_day[d])

    # Hour grid lines + labels in the gutter
    hour_lines = ""
    labels = ""
    h = win_start
    while h <= win_end + 1e-9:
        top = (h - win_start) * PX_PER_HOUR
        hour_lines += f'<div class="hline" style="top:{top}px"></div>'
        labels += f'<div class="hlabel" style="top:{top}px">{fmt_time(h)}</div>'
        h += 1

    day_cols = ""
    for d in range(7):
        header = DAY_NAMES[d]
        if week_start:
            date = week_start + dt.timedelta(days=d)
            today_cls = " today" if date == dt.date.today() else ""
            header = f'{DAY_NAMES[d]}<span class="dnum{today_cls}">{date.day}</span>'
        blocks = ""
        for ev in by_day[d]:
            top = (ev["start"] - win_start) * PX_PER_HOUR
            height = max(24, ev["hours"] * PX_PER_HOUR - 3)
            ncols = ev.get("ncols", 1)
            col = ev.get("col", 0)
            # Cascade layout: blocks stay wide and overlap, each lane nudged right.
            step = 0.0 if ncols <= 1 else min(16.0, 46.0 / (ncols - 1))
            width = 100.0 - (ncols - 1) * step
            left = col * step
            rng = f'{fmt_time(ev["start"])} – {fmt_time(ev["end"])}'
            lock = "🔒 " if ev.get("locked") else ""
            blocks += (
                f'<div class="block" style="top:{top}px;height:{height}px;'
                f'left:calc({left}% + 2px);width:calc({width}% - 5px);'
                f'--z:{10 + col};background:{ev["color"]}" '
                f'title="{ev["employee"]} · {ev["location"]} · {rng}">'
                f'<div class="bn">{lock}{ev["employee"]}</div>'
                f'<div class="bt">{rng}</div>'
                f'<div class="bl">{ev["location"]}</div>'
                f"</div>"
            )
        day_cols += (
            f'<div class="daycol">'
            f'<div class="dhead">{header}</div>'
            f'<div class="dbody" style="height:{body_h}px">{hour_lines}{blocks}</div>'
            f"</div>"
        )

    html = f"""
<style>
  :root {{
    --bg:#ffffff; --head:#f7f6f5; --line:#e8e8e8; --text:#242424; --muted:#8a8a8a;
    --accent:#4F6BED;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16181d; --head:#1f2229; --line:#2e323b; --text:#e8e8e8; --muted:#8b8f98; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; }}
  .cal {{ font-family:'Segoe UI', system-ui, sans-serif; color:var(--text);
         background:var(--bg); border:1px solid var(--line); border-radius:12px;
         overflow:hidden; }}
  .calgrid {{ display:grid; grid-template-columns:{GUTTER}px repeat(7,1fr); }}
  .gutter {{ position:relative; }}
  .gutter .ghead {{ height:{HEADER_H}px; background:var(--head);
                    border-bottom:1px solid var(--line); }}
  .gutter .gbody {{ position:relative; height:{body_h}px; }}
  .hlabel {{ position:absolute; right:8px; transform:translateY(-50%);
             font-size:11px; color:var(--muted); white-space:nowrap; }}
  .daycol {{ border-left:1px solid var(--line); }}
  .dhead {{ height:{HEADER_H}px; display:flex; flex-direction:column; align-items:center;
            justify-content:center; gap:2px; font-weight:600; font-size:12.5px;
            color:var(--muted); background:var(--head);
            border-bottom:1px solid var(--line); }}
  .dhead .dnum {{ font-size:16px; font-weight:700; color:var(--text); line-height:1;
                  padding:3px 7px; border-radius:999px; }}
  .dhead .dnum.today {{ background:var(--accent); color:#fff; }}
  .dbody {{ position:relative; }}
  .hline {{ position:absolute; left:0; right:0; border-top:1px solid var(--line);
            opacity:.55; }}
  .block {{ position:absolute; border-radius:8px; padding:5px 8px; color:#fff;
            overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.28);
            border:1.5px solid var(--bg); cursor:default; z-index:var(--z,10);
            transition:box-shadow .12s ease; }}
  .block:hover {{ z-index:300; box-shadow:0 4px 14px rgba(0,0,0,.4); }}
  .block .bn {{ font-size:12.5px; font-weight:700; line-height:1.2;
                white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .block .bt {{ font-size:11px; opacity:.95; line-height:1.25; white-space:nowrap;
                overflow:hidden; text-overflow:ellipsis; }}
  .block .bl {{ font-size:10.5px; opacity:.8; white-space:nowrap;
                overflow:hidden; text-overflow:ellipsis; }}
</style>
<div class="cal"><div class="calgrid">
  <div class="gutter"><div class="ghead"></div><div class="gbody">{labels}</div></div>
  {day_cols}
</div></div>
"""
    return html, HEADER_H + body_h + 8


# --------------------------------------------------------------------------------------
# Savers — write back with referential integrity (stable ids)
# --------------------------------------------------------------------------------------
def save_employees(df: pd.DataFrame):
    with db.get_conn() as conn:
        existing = {int(r["id"]) for r in conn.execute("SELECT id FROM employees")}
        kept = set()
        for _, row in df.iterrows():
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            vals = (
                name,
                float(row.get("target_hours") or 0),
                float(row.get("max_hours") or 0),
                float(row.get("min_hours") or 0),
                int(bool(row.get("active", True))),
            )
            rid = row.get("id")
            if pd.notna(rid) and int(rid) in existing:
                rid = int(rid)
                conn.execute(
                    "UPDATE employees SET name=?,target_hours=?,max_hours=?,min_hours=?,active=? WHERE id=?",
                    (*vals, rid),
                )
                kept.add(rid)
            else:
                conn.execute(
                    "INSERT INTO employees(name,target_hours,max_hours,min_hours,active) VALUES(?,?,?,?,?)",
                    vals,
                )
        for rid in existing - kept:
            for t in ("employee_locations", "employee_days", "employee_hours",
                      "requests", "shifts"):
                conn.execute(f"DELETE FROM {t} WHERE employee_id=?", (rid,))
            conn.execute("DELETE FROM employees WHERE id=?", (rid,))


def save_locations(df: pd.DataFrame):
    with db.get_conn() as conn:
        existing = {int(r["id"]) for r in conn.execute("SELECT id FROM locations")}
        kept = set()
        for _, row in df.iterrows():
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            oh = float(row.get("open_hour") if pd.notna(row.get("open_hour")) else 8)
            ch = float(row.get("close_hour") if pd.notna(row.get("close_hour")) else 20)
            rid = row.get("id")
            if pd.notna(rid) and int(rid) in existing:
                rid = int(rid)
                conn.execute("UPDATE locations SET name=?,open_hour=?,close_hour=? WHERE id=?",
                             (name, oh, ch, rid))
                kept.add(rid)
            else:
                conn.execute("INSERT INTO locations(name,open_hour,close_hour) VALUES(?,?,?)",
                             (name, oh, ch))
        for rid in existing - kept:
            for t in ("employee_locations", "coverage", "location_hours", "shifts"):
                conn.execute(f"DELETE FROM {t} WHERE location_id=?", (rid,))
            conn.execute("DELETE FROM locations WHERE id=?", (rid,))


# --------------------------------------------------------------------------------------
# Solver plumbing
# --------------------------------------------------------------------------------------
def build_employee_objects() -> list[Employee]:
    emp = load_employees(active_only=True)
    _, _, _ = loc_maps()
    all_loc_ids = set(load_locations()["id"].tolist())
    aloc = allowed_locations_map()
    aday = allowed_days_map()
    from collections import defaultdict
    hours_by_emp: dict[int, dict[int, tuple[float, float]]] = defaultdict(dict)
    for (eid, d), win in employee_hours_map().items():
        hours_by_emp[eid][d] = win
    out = []
    for _, r in emp.iterrows():
        eid = int(r["id"])
        locs = aloc.get(eid, set())
        # "all" or "none" checked == no restriction
        if len(locs) == 0 or len(locs) == len(all_loc_ids):
            locs = set()
        days = aday.get(eid, set())
        if len(days) == 0 or len(days) == 7:
            days = set()
        out.append(
            Employee(
                id=eid,
                name=str(r["name"]),
                target_hours=float(r["target_hours"]),
                max_hours=float(r["max_hours"]),
                min_hours=float(r["min_hours"]),
                allowed_locations=locs,
                allowed_days=days,
                hours_by_day=dict(hours_by_emp.get(eid, {})),
            )
        )
    return out


def build_requests() -> list[Request]:
    df = db.load_df("requests")
    out = []
    for _, r in df.iterrows():
        out.append(
            Request(
                employee_id=int(r["employee_id"]),
                day=int(r["day"]),
                kind=str(r["kind"]),
                location_id=int(r["location_id"]) if pd.notna(r["location_id"]) else None,
                status=str(r["status"]),
            )
        )
    return out


def locked_shifts() -> list[LockedShift]:
    df = db.load_df("shifts")
    if df.empty:
        return []
    df = df[df["locked"] == 1]
    return [
        LockedShift(int(r["employee_id"]), int(r["day"]), int(r["location_id"]),
                    float(r["hours"]), float(r["start_hour"]))
        for _, r in df.iterrows()
    ]


def write_shifts(assignments):
    """Persist solver assignments, filling a default start time for unlocked shifts."""
    oc = open_close_map()
    recs = [
        {"location_id": a.location_id, "day": a.day, "hours": a.hours,
         "start": a.start_hour, "employee_id": a.employee_id, "a": a}
        for a in assignments
    ]
    assign_default_starts(recs, oc, employee_hours_map())
    rows = [
        (r["a"].employee_id, r["a"].day, r["a"].location_id, r["a"].hours,
         float(r["start"]), int(r["a"].locked))
        for r in recs
    ]
    with db.get_conn() as conn:
        conn.execute("DELETE FROM shifts")
        conn.executemany(
            "INSERT INTO shifts(employee_id,day,location_id,hours,start_hour,locked) "
            "VALUES(?,?,?,?,?,?)",
            rows,
        )


# --------------------------------------------------------------------------------------
# Sidebar
# --------------------------------------------------------------------------------------
st.sidebar.title("🗓️ Staff Scheduler")
page = st.sidebar.radio(
    "Navigate",
    ["Schedule", "Employees", "Locations & Coverage", "Requests", "Help"],
    label_visibility="collapsed",
)
st.sidebar.divider()
if st.sidebar.button("Load sample data", use_container_width=True):
    seed.load_sample()
    st.sidebar.success("Sample data loaded.")
    st.rerun()
if st.sidebar.button("🔒 Log out", use_container_width=True):
    st.session_state["authed"] = False
    st.rerun()
with st.sidebar.expander("🔑 Change password"):
    with st.form("auth_change"):
        cur = st.text_input("Current password", type="password")
        n1 = st.text_input("New password", type="password")
        n2 = st.text_input("Confirm new password", type="password")
        chg = st.form_submit_button("Change password")
    if chg:
        stored = db.get_meta("auth_pw") or ""
        if not stored or not _verify_pw(cur, stored):
            st.error("Current password is wrong.")
        elif len(n1) < 8:
            st.error("Use at least 8 characters.")
        elif n1 != n2:
            st.error("New passwords don't match.")
        else:
            _store_pw(n1)
            st.success("Password changed.")
with st.sidebar.expander("Danger zone"):
    if st.button("Wipe everything", type="secondary"):
        with db.get_conn() as conn:
            for t in ("shifts", "requests", "coverage", "location_hours", "employee_days",
                      "employee_hours", "employee_locations", "locations", "employees"):
                conn.execute(f"DELETE FROM {t}")
        st.rerun()

if db.is_empty() and page != "Help":
    st.info("No data yet. Click **Load sample data** in the sidebar to explore, "
            "or head to **Employees** and **Locations & Coverage** to start from scratch.")


# ======================================================================================
# EMPLOYEES
# ======================================================================================
if page == "Employees":
    st.header("👥 Employees")
    st.caption("Set each person's weekly hours **budget** (target) and hard **max**. "
               "Then set where and when they can work in the matrices below.\n\n"
               "➕ **Add** someone by typing in the blank row at the bottom. "
               "🗑 **Remove** someone by ticking the checkbox on the left of their row and pressing "
               "the trash icon (or Delete). Changes apply when you click **Save employees**.")

    emp = load_employees()
    edited = st.data_editor(
        emp,
        num_rows="dynamic",
        use_container_width=True,
        key="emp_editor",
        column_config={
            "id": st.column_config.NumberColumn("id", disabled=True, width="small"),
            "name": st.column_config.TextColumn("Name", required=True),
            "target_hours": st.column_config.NumberColumn("Target hrs/wk", min_value=0, max_value=80, step=1),
            "max_hours": st.column_config.NumberColumn("Max hrs/wk", min_value=0, max_value=80, step=1),
            "min_hours": st.column_config.NumberColumn("Min hrs/wk", min_value=0, max_value=80, step=1),
            "active": st.column_config.CheckboxColumn("Active"),
        },
    )
    if st.button("💾 Save employees", type="primary"):
        save_employees(edited)
        st.success("Saved.")
        st.rerun()

    locs, id2name, name2id = loc_maps()
    active_emp = load_employees(active_only=True)
    if not active_emp.empty and not locs.empty:
        st.divider()
        st.subheader("Location access")
        st.caption("Check the locations each person is allowed to work. "
                   "Leave a whole row unchecked to mean *no restriction* (can work anywhere).")
        aloc = allowed_locations_map()
        rows = []
        for _, e in active_emp.iterrows():
            eid = int(e["id"])
            row = {"employee_id": eid, "Employee": e["name"]}
            for _, l in locs.iterrows():
                row[l["name"]] = int(l["id"]) in aloc.get(eid, set())
            rows.append(row)
        access_df = pd.DataFrame(rows)
        cfg = {"employee_id": None, "Employee": st.column_config.TextColumn("Employee", disabled=True)}
        for _, l in locs.iterrows():
            cfg[l["name"]] = st.column_config.CheckboxColumn(l["name"])
        access_edit = st.data_editor(access_df, use_container_width=True, key="access_editor",
                                     hide_index=True, column_config=cfg)

        st.subheader("Availability (days & hours)")
        st.caption("One cell per weekday — **blank** = any time that day · **off** = can't work "
                   "that day · a window like **15-21** = only between those hours (24-hour "
                   "numbers: 15-21 = 3 PM–9 PM, 7.5-14 = 7:30 AM–2 PM). Windows are "
                   "automatically clipped to each store's business hours when scheduling.")
        aday = allowed_days_map()
        ahrs = employee_hours_map()
        drows = []
        for _, e in active_emp.iterrows():
            eid = int(e["id"])
            row = {"employee_id": eid, "Employee": e["name"]}
            allowed = aday.get(eid)  # None => any day
            for di, dn in enumerate(DAY_NAMES):
                if allowed is not None and di not in allowed:
                    row[dn] = "off"
                elif (eid, di) in ahrs:
                    s, en = ahrs[(eid, di)]
                    row[dn] = f"{s:g}-{en:g}"
                else:
                    row[dn] = ""
            drows.append(row)
        day_df = pd.DataFrame(drows)
        dcfg = {"employee_id": None, "Employee": st.column_config.TextColumn("Employee", disabled=True)}
        for dn in DAY_NAMES:
            dcfg[dn] = st.column_config.TextColumn(dn, width="small")
        day_edit = st.data_editor(day_df, use_container_width=True, key="day_editor",
                                  hide_index=True, column_config=dcfg)

        if st.button("💾 Save access & availability", type="primary"):
            # Parse everything first; refuse the whole save on any bad cell.
            parsed_days: dict[int, list[int]] = {}
            parsed_hours: list[tuple[int, int, float, float]] = []
            errors = []
            for _, row in day_edit.iterrows():
                eid = int(row["employee_id"])
                allowed_days = []
                for di, dn in enumerate(DAY_NAMES):
                    raw = str(row[dn] if row[dn] is not None else "").strip().lower()
                    if raw in ("off", "x", "no"):
                        continue
                    allowed_days.append(di)
                    if raw in ("", "nan", "none"):
                        continue
                    try:
                        win = parse_hours_cell(row[dn])
                        if win:
                            parsed_hours.append((eid, di, win[0], win[1]))
                    except ValueError:
                        errors.append(f'{row["Employee"]} / {dn}: "{row[dn]}"')
                parsed_days[eid] = allowed_days
            if errors:
                st.error("Couldn't read these availability cells (use blank, `off`, or "
                         "`start-end` like `15-21`): " + "; ".join(errors))
            else:
                shown_ids = [int(x) for x in day_edit["employee_id"]]
                ph = ",".join("?" for _ in shown_ids)
                with db.get_conn() as conn:
                    # Scope deletes to the people shown so inactive employees keep theirs.
                    conn.execute(f"DELETE FROM employee_locations WHERE employee_id IN ({ph})",
                                 shown_ids)
                    conn.execute(f"DELETE FROM employee_days WHERE employee_id IN ({ph})",
                                 shown_ids)
                    conn.execute(f"DELETE FROM employee_hours WHERE employee_id IN ({ph})",
                                 shown_ids)
                    for _, row in access_edit.iterrows():
                        eid = int(row["employee_id"])
                        for _, l in locs.iterrows():
                            if bool(row[l["name"]]):
                                conn.execute(
                                    "INSERT INTO employee_locations(employee_id,location_id) VALUES(?,?)",
                                    (eid, int(l["id"])),
                                )
                    for eid, allowed_days in parsed_days.items():
                        if len(allowed_days) == 7:
                            continue  # no day restriction
                        if allowed_days:
                            for di in allowed_days:
                                conn.execute(
                                    "INSERT INTO employee_days(employee_id,day) VALUES(?,?)",
                                    (eid, di))
                        else:
                            # every day off: sentinel row meaning "no days at all"
                            conn.execute(
                                "INSERT INTO employee_days(employee_id,day) VALUES(?,-1)",
                                (eid,))
                    conn.executemany(
                        "INSERT INTO employee_hours(employee_id,day,start_hour,end_hour) "
                        "VALUES(?,?,?,?)", parsed_hours)
                st.success("Saved access & availability.")
                st.rerun()


# ======================================================================================
# LOCATIONS & COVERAGE
# ======================================================================================
elif page == "Locations & Coverage":
    st.header("🏢 Locations & Coverage")
    locs = load_locations()
    st.caption("✏️ **Rename** a store by editing its name cell. Add a store in the blank bottom "
               "row; remove one via its row checkbox + trash icon. Click **Save locations** to apply. "
               "Then set each store's daily hours and staffing needs below.")
    edited = st.data_editor(
        locs, num_rows="dynamic", use_container_width=True, key="loc_editor",
        column_config={
            "id": st.column_config.NumberColumn("id", disabled=True, width="small"),
            "name": st.column_config.TextColumn("Location name", required=True),
            "open_hour": None,
            "close_hour": None,
        },
    )
    if st.button("💾 Save locations", type="primary"):
        save_locations(edited)
        st.success("Saved.")
        st.rerun()

    # ---- Store hours: per store, per weekday ------------------------------------------
    locs = load_locations()
    if not locs.empty:
        st.divider()
        st.subheader("🕐 Store hours")
        st.caption("Each store's opening window per weekday, as `open-close` in 24-hour numbers "
                   "— e.g. `8-20` (8 AM–8 PM) or `7.5-16.5` (7:30 AM–4:30 PM). "
                   "Leave a cell **blank** (or type `closed`) for days the store is closed. "
                   "Auto-generated shifts are placed to cover this window without gaps.")
        oc = open_close_map()
        hrows = []
        for _, l in locs.iterrows():
            lid = int(l["id"])
            row = {"location_id": lid, "Location": l["name"]}
            for di, dn in enumerate(DAY_NAMES):
                win = oc.get((lid, di))
                row[dn] = f"{win[0]:g}-{win[1]:g}" if win else ""
            hrows.append(row)
        hdf = pd.DataFrame(hrows)
        hcfg = {"location_id": None,
                "Location": st.column_config.TextColumn("Location", disabled=True)}
        for dn in DAY_NAMES:
            hcfg[dn] = st.column_config.TextColumn(dn, width="small")
        hours_edit = st.data_editor(hdf, use_container_width=True, key="hours_editor",
                                    hide_index=True, column_config=hcfg)

        if st.button("💾 Save store hours", type="primary"):
            parsed, errors = [], []
            for _, row in hours_edit.iterrows():
                for di, dn in enumerate(DAY_NAMES):
                    try:
                        win = parse_hours_cell(row[dn])
                        parsed.append((int(row["location_id"]), di,
                                       win[0] if win else None, win[1] if win else None))
                    except ValueError:
                        errors.append(f'{row["Location"]} / {dn}: "{row[dn]}"')
            if errors:
                st.error("Couldn't read these cells (use `open-close`, e.g. `8-20`, or leave "
                         "blank for closed): " + "; ".join(errors))
            else:
                with db.get_conn() as conn:
                    conn.execute("DELETE FROM location_hours")
                    conn.executemany(
                        "INSERT INTO location_hours(location_id,day,open_hour,close_hour) "
                        "VALUES(?,?,?,?)", parsed)
                st.success("Saved store hours.")
                st.rerun()

    locs = load_locations()
    if not locs.empty:
        st.divider()
        st.subheader("Coverage needs")
        st.caption("Required **staff-hours** per location per weekday — e.g. a store open 8h "
                   "needing 2 people at once = 16 staff-hours. ⚠️ For gap-free coverage, this "
                   "must be **at least the store's open hours** that day (12h open → ≥ 12).")
        cov = coverage_map()
        rows = []
        for _, l in locs.iterrows():
            lid = int(l["id"])
            row = {"location_id": lid, "Location": l["name"]}
            for di, dn in enumerate(DAY_NAMES):
                row[dn] = float(cov.get((lid, di), 0.0))
            rows.append(row)
        cov_df = pd.DataFrame(rows)
        ccfg = {"location_id": None, "Location": st.column_config.TextColumn("Location", disabled=True)}
        for dn in DAY_NAMES:
            ccfg[dn] = st.column_config.NumberColumn(dn, min_value=0, max_value=200, step=1)
        cov_edit = st.data_editor(cov_df, use_container_width=True, key="cov_editor",
                                  hide_index=True, column_config=ccfg)
        total_req = cov_edit[DAY_NAMES].to_numpy().sum()
        st.metric("Total required staff-hours / week", f"{total_req:g}")

        # Cross-check live coverage numbers against saved store hours.
        oc_saved = open_close_map()
        cross = []
        for _, row in cov_edit.iterrows():
            lid = int(row["location_id"])
            lname = row["Location"]
            for di, dn in enumerate(DAY_NAMES):
                req = float(row[dn] or 0)
                if req <= 0:
                    continue
                win = oc_saved.get((lid, di))
                if win is None:
                    cross.append(f"**{lname} {dn}**: coverage required but the store is "
                                 "marked closed above.")
                elif req < (win[1] - win[0]) - 1e-9:
                    cross.append(f"**{lname} {dn}**: {req:g} staff-hours can't span the "
                                 f"{win[1] - win[0]:g}h open window ({fmt_time(win[0])}–"
                                 f"{fmt_time(win[1])}) — gaps guaranteed. Raise it to at least "
                                 f"{win[1] - win[0]:g}.")
        if cross:
            st.warning("⏱ Gap risk:\n\n" + "\n\n".join(f"- {c}" for c in cross))
        if st.button("💾 Save coverage", type="primary"):
            with db.get_conn() as conn:
                conn.execute("DELETE FROM coverage")
                for _, row in cov_edit.iterrows():
                    lid = int(row["location_id"])
                    for di, dn in enumerate(DAY_NAMES):
                        conn.execute(
                            "INSERT INTO coverage(location_id,day,required_hours) VALUES(?,?,?)",
                            (lid, di, float(row[dn])),
                        )
            st.success("Saved coverage.")
            st.rerun()


# ======================================================================================
# REQUESTS
# ======================================================================================
elif page == "Requests":
    st.header("📋 Request log")
    st.caption(
        "**Time off** / **Unavailable** (status *Approved*) are **hard** — the person won't be scheduled that day.\n\n"
        "**Prefer off** / **Prefer on** are **soft** — the solver honors them when it can. *Denied* requests are ignored."
    )
    emp = load_employees()
    locs, id2name, name2id = loc_maps()
    if emp.empty:
        st.warning("Add employees first.")
    else:
        name_by_id = dict(zip(emp["id"], emp["name"]))
        id_by_name = {v: k for k, v in name_by_id.items()}
        req = db.load_df("requests")
        # Present with friendly labels
        disp = pd.DataFrame({
            "id": req["id"] if not req.empty else [],
            "Employee": [name_by_id.get(i, "?") for i in req["employee_id"]] if not req.empty else [],
            "Day": [DAY_NAMES[int(d)] for d in req["day"]] if not req.empty else [],
            "Kind": req["kind"] if not req.empty else [],
            "Location": [id2name.get(l, "") if pd.notna(l) else "" for l in req["location_id"]] if not req.empty else [],
            "Status": req["status"] if not req.empty else [],
            "Note": req["note"] if not req.empty else [],
        })
        edited = st.data_editor(
            disp, num_rows="dynamic", use_container_width=True, key="req_editor", hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("id", disabled=True, width="small"),
                "Employee": st.column_config.SelectboxColumn("Employee", options=list(id_by_name.keys()), required=True),
                "Day": st.column_config.SelectboxColumn("Day", options=DAY_OPTIONS, required=True),
                "Kind": st.column_config.SelectboxColumn(
                    "Kind", options=["Time off", "Unavailable", "Prefer off", "Prefer on"], required=True),
                "Location": st.column_config.SelectboxColumn("Location (opt.)", options=[""] + list(name2id.keys())),
                "Status": st.column_config.SelectboxColumn("Status", options=["Approved", "Pending", "Denied"], required=True),
                "Note": st.column_config.TextColumn("Note"),
            },
        )
        if st.button("💾 Save requests", type="primary"):
            with db.get_conn() as conn:
                conn.execute("DELETE FROM requests")
                for _, row in edited.iterrows():
                    if not row.get("Employee") or not row.get("Kind") or not row.get("Day"):
                        continue
                    lid = name2id.get(row.get("Location")) if row.get("Location") else None
                    conn.execute(
                        "INSERT INTO requests(employee_id,day,kind,location_id,status,note) VALUES(?,?,?,?,?,?)",
                        (
                            int(id_by_name[row["Employee"]]),
                            DAY_NAMES.index(row["Day"]),
                            row["Kind"],
                            lid,
                            row.get("Status") or "Approved",
                            row.get("Note") or "",
                        ),
                    )
            st.success("Saved requests.")
            st.rerun()


# ======================================================================================
# SCHEDULE
# ======================================================================================
elif page == "Schedule":
    st.header("🗓️ Schedule")
    emp = load_employees(active_only=True)
    emp_all = load_employees()  # includes inactive: their old shifts must still resolve
    locs, id2name, name2id = loc_maps()

    if emp.empty or locs.empty:
        st.warning("You need at least one active employee and one location. "
                   "Use **Load sample data** or fill in the other tabs.")
        st.stop()

    # Name maps cover ALL employees so shifts belonging to now-inactive people still
    # display correctly instead of crashing; rule checks flag them below.
    name_by_id = dict(zip(emp_all["id"].astype(int), emp_all["name"]))
    id_by_name = {v: k for k, v in name_by_id.items()}
    inactive_ids = set(emp_all.loc[emp_all["active"] != 1, "id"].astype(int))

    dup_names = emp_all["name"][emp_all["name"].duplicated()].unique().tolist()
    if dup_names:
        st.warning("⚠️ Duplicate employee names: **" + ", ".join(dup_names) +
                   "**. The shift table matches people by name, so edits may hit the "
                   "wrong person — please make names unique on the Employees tab.")

    with st.expander("⚙️ Solver settings"):
        c1, c2, c3 = st.columns(3)
        min_shift = c1.number_input("Min shift (hrs)", 0.0, 12.0, 3.0, 0.5)
        max_shift = c2.number_input("Max shift (hrs)", 1.0, 14.0, 8.0, 0.5)
        time_limit = c3.number_input("Solver time limit (s)", 2.0, 60.0, 12.0, 1.0)

    cfg = SolveConfig(min_shift=min_shift, max_shift=max_shift, time_limit_s=time_limit)

    b1, b2, b3, _ = st.columns([1.4, 1.2, 1, 3])
    gen = b1.button("⚡ Auto-generate", type="primary", use_container_width=True,
                    help="Fill the schedule respecting all hard rules, budgets and requests. Locked shifts are kept.")
    clear_unlocked = b2.button("Clear unlocked", use_container_width=True)
    clear_all = b3.button("Clear all", use_container_width=True)

    if clear_unlocked:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM shifts WHERE locked=0")
        st.rerun()
    if clear_all:
        with db.get_conn() as conn:
            conn.execute("DELETE FROM shifts")
        st.rerun()

    if gen:
        with st.spinner("Solving…"):
            result = solve(
                employees=build_employee_objects(),
                location_ids=list(locs["id"].astype(int)),
                coverage=coverage_map(),
                requests=build_requests(),
                locked=locked_shifts(),
                config=cfg,
                windows=open_close_map(),
            )
        if result.feasible:
            write_shifts(result.assignments)
            unmet = sum(result.shortfall.values())
            if unmet > 0:
                st.warning(f"Generated, but {unmet:g} staff-hours of coverage could not be filled "
                           "(not enough available/eligible staff). See gaps below.")
            else:
                st.success("Schedule generated — all coverage met.")
            st.rerun()
        else:
            st.error(result.message)

    # ---- Week-start date + placeholder for the calendar (rendered from live edits) ---
    ordered_emp_ids = sorted(int(i) for i in emp_all["id"])
    today = dt.date.today()
    default_monday = today - dt.timedelta(days=today.weekday())
    saved_ws = db.get_meta("week_start")
    if saved_ws:
        try:
            default_monday = dt.date.fromisoformat(saved_ws)
        except ValueError:
            pass
    wc1, wc2, _ = st.columns([1.2, 1.6, 3])
    week_start = wc1.date_input("Week of", value=default_monday, format="YYYY-MM-DD")
    db.set_meta("week_start", week_start.isoformat())
    loc_filter = wc2.selectbox("Show location", ["All locations"] + list(name2id.keys()))

    legend_box = st.container()    # employee colour legend (filled once we know who's scheduled)
    calendar_box = st.container()  # filled after we read the editor's live state

    # ---- Editable shift table --------------------------------------------------------
    shifts = db.load_df("shifts")
    if shifts.empty:
        editor_df = pd.DataFrame(columns=["Employee", "Day", "Location", "Start", "Hours", "Locked"])
    else:
        editor_df = pd.DataFrame({
            "Employee": [name_by_id.get(int(e), "?") for e in shifts["employee_id"]],
            "Day": [DAY_NAMES[int(d)] for d in shifts["day"]],
            "Location": [id2name.get(int(l), "?") for l in shifts["location_id"]],
            "Start": [hours_to_time(float(s)) for s in shifts["start_hour"]],
            "Hours": shifts["hours"].astype(float),
            "Locked": shifts["locked"].astype(bool),
        }).sort_values(["Day", "Location", "Start"]).reset_index(drop=True)

    st.subheader("Shifts")
    st.caption("Edit the **Start** time and **Hours** to move/resize a block, swap people or "
               "locations, add or delete rows — the calendar above updates live. "
               "**🔒 Lock** shifts you're happy with, then *Auto-generate* to fill only the rest.")
    edited = st.data_editor(
        editor_df, num_rows="dynamic", use_container_width=True, key="shift_editor", hide_index=True,
        column_config={
            "Employee": st.column_config.SelectboxColumn("Employee", options=list(id_by_name.keys()), required=True),
            "Day": st.column_config.SelectboxColumn("Day", options=DAY_OPTIONS, required=True),
            "Location": st.column_config.SelectboxColumn("Location", options=list(name2id.keys()), required=True),
            "Start": st.column_config.TimeColumn("Start", format="h:mm a", step=900),
            "Hours": st.column_config.NumberColumn("Hours", min_value=0.0, max_value=14.0, step=0.5),
            "Locked": st.column_config.CheckboxColumn("🔒 Lock"),
        },
    )

    # ---- Build records from the CURRENT editor state (reflects unsaved edits) --------
    records = []
    orphan_rows = []
    for _, r in edited.iterrows():
        if not r.get("Employee") or not r.get("Day") or not r.get("Location"):
            continue
        # A row can stop resolving if its employee was deleted or a name no longer
        # matches (e.g. after renames). Skip it with a warning instead of crashing.
        if r["Employee"] not in id_by_name or r["Location"] not in name2id:
            orphan_rows.append(f'{r["Employee"]} — {r["Day"]} — {r["Location"]}')
            continue
        raw_start = r.get("Start")
        start = None if (raw_start is None or (isinstance(raw_start, float) and pd.isna(raw_start))) \
            else time_to_hours(raw_start)
        eid = int(id_by_name[r["Employee"]])
        records.append({
            "employee_id": eid,
            "employee": r["Employee"],
            "day": DAY_NAMES.index(r["Day"]),
            "location_id": int(name2id[r["Location"]]),
            "location": r["Location"],
            "hours": float(r.get("Hours") or 0),
            "start": start,
            "locked": bool(r.get("Locked", False)),
            "color": emp_color(eid, ordered_emp_ids),
        })
    assign_default_starts(records, open_close_map(), employee_hours_map())
    rec_df = pd.DataFrame(records)

    if orphan_rows:
        st.warning("⚠️ These shifts no longer match a known employee/location and are being "
                   "ignored (they'll be dropped on the next **Save edits**): " +
                   "; ".join(orphan_rows))

    # ---- Employee colour legend (only people scheduled this week) --------------------
    with legend_box:
        seen: dict[str, str] = {}
        for r in sorted(records, key=lambda x: x["employee"]):
            seen.setdefault(r["employee"], r["color"])
        if seen:
            chips = "".join(
                f'<span style="display:inline-flex;align-items:center;gap:5px;font-size:12.5px;'
                f'padding:2px 0">'
                f'<span style="width:11px;height:11px;border-radius:3px;background:{c};'
                f'display:inline-block"></span>{n}</span>'
                for n, c in seen.items()
            )
            st.markdown(
                f'<div style="display:flex;flex-wrap:wrap;gap:4px 16px;margin:2px 0 10px">{chips}</div>',
                unsafe_allow_html=True,
            )

    # ---- Render the calendar into the reserved container -----------------------------
    cal_records = records
    if loc_filter != "All locations":
        cal_records = [r for r in records if r["location"] == loc_filter]
    with calendar_box:
        if cal_records:
            html, height = build_calendar_html(cal_records, week_start)
            components.html(html, height=min(height, 820), scrolling=True)
        elif records:
            st.info(f"No shifts at **{loc_filter}** this week.")
        else:
            st.info("No shifts yet — hit **⚡ Auto-generate** above, or add rows in the table below.")

    if st.button("💾 Save edits", type="primary"):
        rows = []
        for r in records:
            if r["hours"] <= 0:
                continue
            rows.append((
                r["employee_id"], r["day"], r["location_id"], r["hours"],
                float(r["start"]), int(r["locked"]),
            ))
        with db.get_conn() as conn:
            conn.execute("DELETE FROM shifts")
            conn.executemany(
                "INSERT INTO shifts(employee_id,day,location_id,hours,start_hour,locked) "
                "VALUES(?,?,?,?,?,?)", rows)
        st.success("Saved.")
        st.rerun()

    st.divider()
    colA, colB = st.columns(2)

    # Budget usage
    with colA:
        st.subheader("Hours vs budget")
        used = rec_df.groupby("employee_id")["hours"].sum().to_dict() if not rec_df.empty else {}
        brows = []
        for _, e in emp.iterrows():
            eid = int(e["id"])
            u = float(used.get(eid, 0.0))
            brows.append({
                "Employee": e["name"],
                "Used": u,
                "Target": float(e["target_hours"]),
                "Max": float(e["max_hours"]),
                "Over max": u > float(e["max_hours"]) + 1e-9,
            })
        # Inactive people who still have shifts on the board
        active_ids = set(emp["id"].astype(int))
        for _, e in emp_all.iterrows():
            eid = int(e["id"])
            if eid in active_ids or eid not in used:
                continue
            u = float(used[eid])
            brows.append({
                "Employee": f'{e["name"]} (inactive)',
                "Used": u,
                "Target": 0.0,
                "Max": 0.0,
                "Over max": True,
            })
        bdf = pd.DataFrame(brows)

        def hl_over(row):
            color = "background-color: #ffd6d6" if row["Over max"] else ""
            return [color] * len(row)

        st.dataframe(
            bdf.style.apply(hl_over, axis=1).format({"Used": "{:g}", "Target": "{:g}", "Max": "{:g}"}),
            use_container_width=True, hide_index=True,
        )
        total_used = bdf["Used"].sum()
        total_target = bdf["Target"].sum()
        st.caption(f"Scheduled **{total_used:g}h** across the team (sum of targets: {total_target:g}h).")

    # Coverage
    with colB:
        st.subheader("Coverage (scheduled / required)")
        cov = coverage_map()
        sched = {}
        if not rec_df.empty:
            for (lid, d), h in rec_df.groupby(["location_id", "day"])["hours"].sum().items():
                sched[(int(lid), int(d))] = float(h)
        crows = []
        gaps = 0.0
        for _, l in locs.iterrows():
            lid = int(l["id"])
            row = {"Location": l["name"]}
            for di, dn in enumerate(DAY_NAMES):
                req = cov.get((lid, di), 0.0)
                got = sched.get((lid, di), 0.0)
                gaps += max(0.0, req - got)
                row[dn] = f"{got:g}/{req:g}" if req else (f"{got:g}" if got else "")
            crows.append(row)
        cdf = pd.DataFrame(crows)

        def hl_gap(val):
            if isinstance(val, str) and "/" in val:
                got, req = val.split("/")
                try:
                    if float(got) < float(req) - 1e-9:
                        return "background-color: #ffd6d6"
                    if float(got) > float(req) + 1e-9:
                        return "background-color: #fff3cd"
                except ValueError:
                    return ""
            return ""

        st.dataframe(
            cdf.style.map(hl_gap, subset=DAY_NAMES),
            use_container_width=True, hide_index=True,
        )
        # Time-of-day gaps: is someone actually present for every open minute?
        tgaps = coverage_time_gaps(records, open_close_map(), cov)
        if gaps > 0:
            st.warning(f"⚠️ {gaps:g} staff-hours of coverage are unfilled (red cells).")
        if tgaps:
            lines = []
            for lid, d, a, b, kind in tgaps:
                lname = id2name.get(lid, "?")
                if kind == "closed":
                    lines.append(f"- **{lname} {DAY_NAMES[d]}**: coverage required but the "
                                 "store is marked closed (fix on Locations & Coverage).")
                else:
                    lines.append(f"- **{lname} {DAY_NAMES[d]}**: nobody scheduled "
                                 f"{fmt_time(a)}–{fmt_time(b)}.")
            st.error("🕳 Open-hours gaps:\n\n" + "\n\n".join(lines) +
                     "\n\nFix by adjusting **Start** times in the table, or raise that day's "
                     "required staff-hours and re-generate.")
        if gaps <= 0 and not tgaps:
            st.success("All coverage met — no gaps during open hours. 🎉")

    # ---- Rule violations -------------------------------------------------------------
    st.subheader("Rule checks")
    violations = []
    aloc = allowed_locations_map()
    aday = allowed_days_map()
    all_loc_ids = set(locs["id"].astype(int))
    reqs = build_requests()
    hard_off = {(r.employee_id, r.day) for r in reqs
                if r.kind in ("Time off", "Unavailable") and r.status == "Approved"}
    max_by_id = dict(zip(emp_all["id"].astype(int), emp_all["max_hours"].astype(float)))
    oc_rules = open_close_map()
    ahrs_rules = employee_hours_map()
    EPS = 1 / 60

    if not rec_df.empty:
        # over max
        for eid, h in rec_df.groupby("employee_id")["hours"].sum().items():
            mx = max_by_id.get(int(eid), 0)
            if h > mx + 1e-9:
                violations.append(f"❌ **{name_by_id.get(int(eid))}** scheduled {h:g}h > max {mx:g}h")
        # multiple locations / rows per day
        dup = rec_df.groupby(["employee_id", "day"]).size()
        for (eid, d), n in dup.items():
            if n > 1:
                violations.append(f"⚠️ **{name_by_id.get(int(eid))}** has {n} shifts on {DAY_NAMES[int(d)]} "
                                  "(model expects one location per day).")
        for _, r in rec_df.iterrows():
            eid = int(r["employee_id"])
            # scheduled but marked inactive
            if eid in inactive_ids:
                violations.append(f"⚠️ **{r['employee']}** is scheduled on {DAY_NAMES[r['day']]} "
                                  "but is marked **inactive** on the Employees tab.")
            # location not allowed
            allowed = aloc.get(eid, set())
            if allowed and allowed != all_loc_ids and r["location_id"] not in allowed:
                violations.append(f"❌ **{r['employee']}** works **{r['location']}** "
                                  f"({DAY_NAMES[r['day']]}) — not an allowed location.")
            # day not allowed
            days = aday.get(eid, set())
            if days and len(days) != 7 and r["day"] not in days:
                violations.append(f"❌ **{r['employee']}** works **{DAY_NAMES[r['day']]}** — not an available day.")
            # hard request conflict
            if (eid, r["day"]) in hard_off:
                violations.append(f"❌ **{r['employee']}** is scheduled on {DAY_NAMES[r['day']]} "
                                  "despite an approved time-off/unavailable request.")
            # outside store hours / store closed
            shift_end = r["start"] + r["hours"]
            win = oc_rules.get((int(r["location_id"]), int(r["day"])))
            if win is None:
                violations.append(f"❌ **{r['employee']}** is scheduled at **{r['location']}** "
                                  f"on {DAY_NAMES[r['day']]}, but the store is closed that day.")
            elif r["start"] < win[0] - EPS or shift_end > win[1] + EPS:
                violations.append(f"⚠️ **{r['employee']}**'s {DAY_NAMES[r['day']]} shift "
                                  f"({fmt_time(r['start'])}–{fmt_time(shift_end)}) falls outside "
                                  f"**{r['location']}** hours ({fmt_time(win[0])}–{fmt_time(win[1])}).")
            # outside the employee's own hourly availability
            av = ahrs_rules.get((eid, int(r["day"])))
            if av and (r["start"] < av[0] - EPS or shift_end > av[1] + EPS):
                violations.append(f"❌ **{r['employee']}**'s {DAY_NAMES[r['day']]} shift "
                                  f"({fmt_time(r['start'])}–{fmt_time(shift_end)}) is outside "
                                  f"their availability ({fmt_time(av[0])}–{fmt_time(av[1])}).")

    if violations:
        for v in violations:
            st.markdown(v)
    else:
        st.success("No rule violations. ✅")

    # ---- Text grid + export ----------------------------------------------------------
    st.divider()
    if not rec_df.empty:
        rec_df = rec_df.sort_values(["day", "start"]).reset_index(drop=True)
        rec_df["cell"] = rec_df.apply(
            lambda r: f'{r["location"]} {fmt_time(r["start"])}–{fmt_time(r["start"] + r["hours"])}', axis=1)
        grid = rec_df.pivot_table(
            index="employee", columns="day", values="cell",
            aggfunc=lambda s: " + ".join(s), fill_value="",
        )
        grid = grid.reindex(columns=range(7), fill_value="")
        grid.columns = DAY_NAMES

        with st.expander("📋 Table grid (person × day)"):
            st.dataframe(grid, use_container_width=True)

        # CSV + Excel export
        exp = rec_df[["employee", "day", "location", "start", "hours"]].copy()
        exp["day"] = exp["day"].map(lambda d: DAY_NAMES[d])
        exp["end"] = (rec_df["start"] + rec_df["hours"]).map(fmt_time)
        exp["start"] = rec_df["start"].map(fmt_time)
        exp = exp[["employee", "day", "location", "start", "end", "hours"]]
        csv = exp.to_csv(index=False).encode()
        e1, e2 = st.columns(2)
        e1.download_button("⬇️ Export shifts (CSV)", csv, "schedule.csv", "text/csv",
                           use_container_width=True)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            grid.to_excel(xw, sheet_name="Grid")
            exp.to_excel(xw, sheet_name="Shifts", index=False)
        e2.download_button("⬇️ Export grid (Excel)", buf.getvalue(), "schedule.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)


# ======================================================================================
# HELP
# ======================================================================================
elif page == "Help":
    st.header("❓ How it works")
    st.markdown(
        """
### The idea
This is a **local, single-user** scheduler. You describe your team and rules once, click
**Auto-generate**, then hand-tweak the result — locking the parts you like and re-solving the rest.

### Workflow
1. **Employees** — add people, set each one's weekly **target** and **max** hours, then set
   their allowed **locations** and **availability**: per weekday, blank = any time, `off` =
   can't work, or an hourly window like `15-21` (only 3 PM–9 PM). Hourly windows are clipped
   to each store's business hours automatically.
2. **Locations & Coverage** — list your locations, set each store's **hours per weekday**
   (`8-20`, blank = closed), and the **staff-hours** each needs per weekday. For gap-free
   days, required staff-hours must be at least the open window's length.
3. **Requests** — log time-off and shift preferences. *Approved* time-off is a hard block;
   *prefer on/off* are honored when possible.
4. **Schedule** — click **⚡ Auto-generate**. The solver respects every hard rule, keeps each
   person under their max, pulls everyone toward their target hours, and honors preferences.
   Shifts appear as coloured blocks on a **week calendar** (colour = **employee**, so you can
   spot at a glance who works which days — even across locations). Edit a shift's **Start** time
   or **Hours** in the table and the block moves/resizes live. **🔒 Lock** good shifts and
   re-generate to fill the gaps.

### The week calendar
Days run across the top, time down the side, and each shift is a block placed by its start time
and length — overlapping shifts stay wide and cascade over each other (hover a block to bring it
to the front). Use **Show location** to focus one store. The solver assigns **daily
staff-hours**; start times are then **chained to cover each store's open window with no
gaps** (openers → mids → closers), and any remaining uncovered stretch is flagged in red
under Coverage. Fine-tune starts in the table — the gap check updates live.

### What the solver optimizes (in priority order)
1. **Fill coverage** — don't leave a location understaffed.
2. **Avoid overstaffing** — mild penalty for exceeding a coverage need.
3. **Budget** — keep everyone close to their target hours (max is a hard cap).
4. **Preferences** — honor prefer-on / prefer-off requests.

### Good to know
- Model assumption: **one location per person per day**. The rule-check flags anything that breaks it.
- Everything lives in `scheduler.db` next to the app — back it up by copying that file.
- If **Auto-generate** says *infeasible*, a hard constraint is impossible (e.g. more required
  hours than eligible staff can cover, or a locked shift conflicts with a rule). Relax shift
  lengths, budgets, coverage, or a lock.
        """
    )
    st.caption("Built with Streamlit + Google OR-Tools (CP-SAT).")
