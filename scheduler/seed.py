"""Load a realistic sample dataset so the app is usable the moment it opens."""
from __future__ import annotations

from . import db


def load_sample():
    db.init_db()
    with db.get_conn() as conn:
        for t in ("shifts", "requests", "coverage", "location_hours", "employee_days",
                  "employee_hours", "employee_locations", "locations", "employees"):
            conn.execute(f"DELETE FROM {t}")

        locations = ["Front Desk", "Warehouse", "Showroom"]
        loc_ids = {}
        for name in locations:
            cur = conn.execute("INSERT INTO locations(name) VALUES(?)", (name,))
            loc_ids[name] = cur.lastrowid

        # (name, target, max, allowed_locations, allowed_days)  empty => any
        FD, WH, SR = "Front Desk", "Warehouse", "Showroom"
        WEEKDAYS = [0, 1, 2, 3, 4]
        people = [
            ("Alice",   40, 40, [FD, SR], []),
            ("Ben",     32, 40, [WH], []),
            ("Carla",   24, 30, [FD], WEEKDAYS),
            ("Diego",   40, 40, [WH, SR], []),
            ("Erin",    20, 25, [FD, SR], [5, 6]),      # weekends only
            ("Frank",   32, 40, [WH], []),
            ("Grace",   40, 40, [SR], []),
            ("Hassan",  16, 20, [FD], [0, 2, 4]),
            ("Ivy",     32, 40, [FD, WH, SR], []),
            ("Jack",    24, 32, [WH, SR], WEEKDAYS),
            ("Kira",    40, 40, [SR, FD], []),
            ("Leo",     20, 24, [WH], [4, 5, 6]),
            ("Mona",    32, 40, [FD, SR], []),
            ("Nate",    36, 40, [WH, SR], []),
            ("Omar",    28, 36, [FD], []),
        ]
        for name, target, mx, locs, dayset in people:
            cur = conn.execute(
                "INSERT INTO employees(name,target_hours,max_hours,min_hours,active) VALUES(?,?,?,?,1)",
                (name, target, mx, 0),
            )
            eid = cur.lastrowid
            for ln in locs:
                conn.execute(
                    "INSERT INTO employee_locations(employee_id,location_id) VALUES(?,?)",
                    (eid, loc_ids[ln]),
                )
            for dd in dayset:
                conn.execute(
                    "INSERT INTO employee_days(employee_id,day) VALUES(?,?)", (eid, dd)
                )

        # Store hours per weekday (None = closed that day).
        hours = {
            FD: [(8, 20)] * 7,
            WH: [(6, 18)] * 6 + [None],   # closed Sunday
            SR: [(9, 21)] * 7,
        }
        for name, per_day in hours.items():
            for d, win in enumerate(per_day):
                conn.execute(
                    "INSERT INTO location_hours(location_id,day,open_hour,close_hour) "
                    "VALUES(?,?,?,?)",
                    (loc_ids[name], d, win[0] if win else None, win[1] if win else None),
                )

        # Coverage: staff-hours needed per location per weekday.
        cover = {
            FD: [16, 16, 16, 16, 20, 24, 16],
            WH: [24, 24, 24, 24, 24, 12, 0],
            SR: [16, 16, 16, 16, 24, 32, 24],
        }
        for name, per_day in cover.items():
            for d, hh in enumerate(per_day):
                conn.execute(
                    "INSERT INTO coverage(location_id,day,required_hours) VALUES(?,?,?)",
                    (loc_ids[name], d, hh),
                )

        emp = lambda n: conn.execute("SELECT id FROM employees WHERE name=?", (n,)).fetchone()["id"]

        # Hourly availability: Hassan only after 3 PM; Erin mornings/early afternoon.
        for name, days_, (s, e) in [("Hassan", [0, 2, 4], (15, 20)),
                                    ("Erin", [5, 6], (9, 16))]:
            for dd in days_:
                conn.execute(
                    "INSERT INTO employee_hours(employee_id,day,start_hour,end_hour) "
                    "VALUES(?,?,?,?)", (emp(name), dd, s, e))

        # A few sample requests.
        sample_requests = [
            (emp("Alice"), 4, "Time off", None, "Approved", "Doctor"),
            (emp("Ben"), 2, "Prefer off", None, "Approved", "Class"),
            (emp("Grace"), 5, "Prefer on", loc_ids[SR], "Approved", "Likes weekends"),
            (emp("Ivy"), 0, "Unavailable", None, "Approved", ""),
            (emp("Omar"), 3, "Prefer off", None, "Pending", ""),
        ]
        for eid, d, kind, lid, status, note in sample_requests:
            conn.execute(
                "INSERT INTO requests(employee_id,day,kind,location_id,status,note) "
                "VALUES(?,?,?,?,?,?)",
                (eid, d, kind, lid, status, note),
            )
