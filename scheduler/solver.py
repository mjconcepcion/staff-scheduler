"""OR-Tools CP-SAT scheduling model.

Model summary
-------------
Decision (per employee e, per day d):
  * work[e,d]      : employee works that day (bool)
  * loc[e,d,l]     : employee works at location l that day (bool); sum over l == work
  * hrs[e,d]       : hours worked that day, in quarter-hour units (int)
  * hloc[e,d,l]    : hours worked at location l that day (linearized hrs * loc)

Hard constraints:
  * one location per working day; hrs within [min_shift, max_shift] when working
  * respect each employee's allowed locations & allowed days
  * approved Time-off / Unavailable requests force the day off
  * weekly hours <= employee max
  * locked shifts are pinned exactly as the user left them

Objective (minimize, weighted):
  * coverage shortfall   (highest priority — don't leave a location understaffed)
  * over-coverage        (mild — avoid pointless overstaffing)
  * budget deviation     (pull each person toward their target hours)
  * unmet preferences    (Prefer off worked / Prefer on not honored)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

Q = 4  # quarter-hour resolution: 1 hour == 4 units


def _q(hours: float) -> int:
    return int(round(hours * Q))


@dataclass
class Employee:
    id: int
    name: str
    target_hours: float
    max_hours: float
    min_hours: float = 0.0
    allowed_locations: set[int] = field(default_factory=set)  # empty => any location
    allowed_days: set[int] = field(default_factory=set)       # empty => any day
    # day -> (earliest, latest) hours the person can be at work; missing day => any time
    hours_by_day: dict[int, tuple[float, float]] = field(default_factory=dict)


@dataclass
class Request:
    employee_id: int
    day: int
    kind: str                 # 'Time off' | 'Unavailable' | 'Prefer off' | 'Prefer on'
    location_id: int | None = None
    status: str = "Approved"  # 'Approved' | 'Pending' | 'Denied'


@dataclass
class LockedShift:
    employee_id: int
    day: int
    location_id: int
    hours: float
    start_hour: float = 9.0


@dataclass
class SolveConfig:
    min_shift: float = 3.0
    max_shift: float = 8.0
    time_limit_s: float = 12.0
    w_short: int = 100
    w_over: int = 2
    w_budget: int = 5
    w_pref: int = 25


@dataclass
class Assignment:
    employee_id: int
    day: int
    location_id: int
    hours: float
    locked: bool = False
    start_hour: float | None = None  # filled for locked shifts; None => caller assigns


@dataclass
class SolveResult:
    status: str
    assignments: list[Assignment]
    shortfall: dict[tuple[int, int], float]      # (location_id, day) -> unfilled hours
    employee_hours: dict[int, float]             # employee_id -> scheduled hours
    feasible: bool
    message: str = ""


HARD_OFF_KINDS = {"Time off", "Unavailable"}


def solve(
    employees: list[Employee],
    location_ids: list[int],
    coverage: dict[tuple[int, int], float],
    requests: list[Request],
    locked: list[LockedShift],
    config: SolveConfig | None = None,
    days: range = range(7),
    windows: dict[tuple[int, int], tuple[float, float]] | None = None,
) -> SolveResult:
    """`windows` maps (location_id, day) -> (open, close); a missing key with `windows`
    provided means the store is closed that day. Hours assigned to an employee at a
    location are capped to the overlap of the store window and the employee's own
    hourly availability, so shift placement can always fit them in."""
    cfg = config or SolveConfig()
    model = cp_model.CpModel()

    max_q = _q(cfg.max_shift)
    min_q = _q(cfg.min_shift)

    # --- Pre-process the request log -------------------------------------------------
    hard_off: set[tuple[int, int]] = set()
    prefer_off: set[tuple[int, int]] = set()
    prefer_on: dict[tuple[int, int], int | None] = {}  # (e,d) -> optional location
    for r in requests:
        key = (r.employee_id, r.day)
        if r.status == "Denied":
            continue
        if r.kind in HARD_OFF_KINDS and r.status == "Approved":
            hard_off.add(key)
        elif r.kind == "Prefer off":
            prefer_off.add(key)
        elif r.kind == "Prefer on":
            prefer_on[key] = r.location_id

    locked_keys = {(l.employee_id, l.day) for l in locked}
    locked_by_key = {(l.employee_id, l.day): l for l in locked}

    work: dict[tuple[int, int], cp_model.IntVar] = {}
    hrs: dict[tuple[int, int], cp_model.IntVar] = {}
    loc: dict[tuple[int, int, int], cp_model.IntVar] = {}
    hloc: dict[tuple[int, int, int], cp_model.IntVar] = {}

    emp_by_id = {e.id: e for e in employees}

    def window_span(e: Employee, d: int, l: int) -> float:
        """Usable hours at location l on day d for employee e: overlap of the store's
        open window and the employee's hourly availability."""
        win = windows.get((l, d)) if windows is not None else None
        if windows is not None and win is None:
            return 0.0  # store closed that day
        lo, hi = win if win else (0.0, 24.0)
        av = e.hours_by_day.get(d)
        if av:
            lo, hi = max(lo, av[0]), min(hi, av[1])
        return max(0.0, hi - lo)

    for e in employees:
        allowed_locs = e.allowed_locations or set(location_ids)
        allowed_locs = [l for l in location_ids if l in allowed_locs]
        for d in days:
            key = (e.id, d)
            day_ok = (not e.allowed_days) or (d in e.allowed_days)
            is_locked = key in locked_keys
            # An employee can be scheduled if the day is allowed and not a hard day off,
            # OR the user explicitly locked a shift there (manual override wins).
            if not is_locked and (not day_ok or key in hard_off):
                continue

            if is_locked:
                # Manual override: keep every allowed location plus the locked one,
                # with no window caps (the lock is pinned verbatim below).
                loc_choices = list(allowed_locs)
                ll = locked_by_key[key].location_id
                if ll not in loc_choices:
                    loc_choices = loc_choices + [ll]
                spans = {l: None for l in loc_choices}
            else:
                # Drop locations where the usable window can't fit a minimum shift.
                spans = {l: window_span(e, d, l) for l in allowed_locs}
                loc_choices = [l for l in allowed_locs
                               if spans[l] >= cfg.min_shift - 1e-9]
                if not loc_choices:
                    continue  # nowhere this person can work that day

            w = model.NewBoolVar(f"work_{e.id}_{d}")
            h = model.NewIntVar(0, max_q, f"hrs_{e.id}_{d}")
            work[key] = w
            hrs[key] = h

            lvars = []
            for l in loc_choices:
                lv = model.NewBoolVar(f"loc_{e.id}_{d}_{l}")
                loc[(e.id, d, l)] = lv
                lvars.append(lv)
                # hloc = h if lv else 0  (linearized product)
                hl = model.NewIntVar(0, max_q, f"hloc_{e.id}_{d}_{l}")
                hloc[(e.id, d, l)] = hl
                model.Add(hl <= h)
                model.Add(hl <= max_q * lv)
                model.Add(hl >= h - max_q * (1 - lv))
                if spans[l] is not None:
                    # Working hours here must fit the store window ∩ availability.
                    model.Add(h <= _q(spans[l]) + max_q * (1 - lv))

            model.Add(sum(lvars) == w)               # exactly one location iff working
            model.Add(h >= min_q * w)                # min shift length when working
            model.Add(h <= max_q * w)                # zero hours when not working

    # --- Locked shifts: pin them exactly ---------------------------------------------
    for lk in locked:
        key = (lk.employee_id, lk.day)
        if key not in work:
            continue
        model.Add(work[key] == 1)
        model.Add(hrs[key] == _q(lk.hours))
        model.Add(loc[(lk.employee_id, lk.day, lk.location_id)] == 1)

    # --- Weekly budget cap (hard) ----------------------------------------------------
    for e in employees:
        day_hrs = [hrs[(e.id, d)] for d in days if (e.id, d) in hrs]
        if day_hrs:
            model.Add(sum(day_hrs) <= _q(e.max_hours))

    obj_terms = []

    # --- Coverage: shortfall (primary) and over-coverage (mild) ----------------------
    shortfall_vars: dict[tuple[int, int], cp_model.IntVar] = {}
    for l in location_ids:
        for d in days:
            req_q = _q(coverage.get((l, d), 0.0))
            scheduled = [hloc[(e.id, d, l)] for e in employees if (e.id, d, l) in hloc]
            sched_sum = sum(scheduled) if scheduled else 0
            short = model.NewIntVar(0, max(req_q, 1), f"short_{l}_{d}")
            over = model.NewIntVar(0, max_q * max(len(employees), 1), f"over_{l}_{d}")
            model.Add(short >= req_q - sched_sum)
            model.Add(over >= sched_sum - req_q)
            shortfall_vars[(l, d)] = short
            obj_terms.append(cfg.w_short * short)
            obj_terms.append(cfg.w_over * over)

    # --- Budget deviation: pull toward each person's target --------------------------
    for e in employees:
        day_hrs = [hrs[(e.id, d)] for d in days if (e.id, d) in hrs]
        total = sum(day_hrs) if day_hrs else 0
        target_q = _q(e.target_hours)
        under = model.NewIntVar(0, _q(e.max_hours) + target_q, f"under_{e.id}")
        over_t = model.NewIntVar(0, _q(e.max_hours) + target_q, f"overt_{e.id}")
        model.Add(under >= target_q - total)
        model.Add(over_t >= total - target_q)
        obj_terms.append(cfg.w_budget * under)
        obj_terms.append(cfg.w_budget * over_t)

    # --- Preferences (soft) ----------------------------------------------------------
    for (eid, d) in prefer_off:
        if (eid, d) in work:
            obj_terms.append(cfg.w_pref * work[(eid, d)])
    for (eid, d), want_loc in prefer_on.items():
        if (eid, d) not in work:
            continue
        if want_loc is not None and (eid, d, want_loc) in loc:
            obj_terms.append(cfg.w_pref * (1 - loc[(eid, d, want_loc)]))
        else:
            obj_terms.append(cfg.w_pref * (1 - work[(eid, d)]))

    model.Minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(cfg.time_limit_s)
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    status_name = solver.StatusName(status)
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    assignments: list[Assignment] = []
    employee_hours: dict[int, float] = {e.id: 0.0 for e in employees}
    shortfall: dict[tuple[int, int], float] = {}

    if feasible:
        for (eid, d), w in work.items():
            if solver.Value(w) == 1:
                h = solver.Value(hrs[(eid, d)]) / Q
                chosen = None
                for l in location_ids:
                    if (eid, d, l) in loc and solver.Value(loc[(eid, d, l)]) == 1:
                        chosen = l
                        break
                if chosen is None:
                    continue
                is_locked = (eid, d) in locked_keys
                start = locked_by_key[(eid, d)].start_hour if is_locked else None
                assignments.append(
                    Assignment(eid, d, chosen, h, locked=is_locked, start_hour=start)
                )
                employee_hours[eid] += h
        for key, sv in shortfall_vars.items():
            val = solver.Value(sv) / Q
            if val > 1e-9:
                shortfall[key] = val

    msg = f"Solver status: {status_name}"
    if not feasible:
        msg += " — no schedule found. Try relaxing constraints (shift lengths, budgets, or coverage)."
    return SolveResult(
        status=status_name,
        assignments=sorted(assignments, key=lambda a: (a.day, a.location_id, a.employee_id)),
        shortfall=shortfall,
        employee_hours=employee_hours,
        feasible=feasible,
        message=msg,
    )
