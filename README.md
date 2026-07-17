# 🗓️ Staff Scheduler

A local, single-user shift scheduler. Describe your team and rules once, click
**Auto-generate**, then hand-tweak the result — lock the shifts you like and re-solve the rest.

Built with **Streamlit** (UI) + **Google OR-Tools / CP-SAT** (constraint solver) + **SQLite** (storage).

## Features
- **Week calendar view** — a Microsoft Teams–style grid: days across the top, time down the
  side, and each shift a **coloured block** (colour = **employee**, with a legend) placed by its
  start time and length. Overlapping blocks stay wide and cascade over each other (hover to
  bring one to the front); filter to a single location with the **Show location** dropdown.
  Light/dark theme aware, with today's date highlighted.
- **Hours budget per employee** — a weekly *target* the solver aims for and a hard *max* it never exceeds.
- **Caveats handled as constraints:**
  - each employee's **allowed locations** and per-weekday **availability**: blank = any time,
    `off` = can't work, or an hourly window like `15-21` (only 3 PM–9 PM) — windows are
    clipped to store hours, capped in the solver, and respected by shift placement
  - each store's **hours per weekday** (`8-20`, blank = closed) — shifts are chained to cover
    the whole open window with **no gaps**, and any uncovered stretch is flagged in red
  - a **request log**: *Time off* / *Unavailable* (hard blocks) and *Prefer on* / *Prefer off* (soft)
- **Auto-fill then tweak** — the solver proposes a full schedule; edit a shift's **Start** time or
  **Hours** in the table and its block moves/resizes live. See budget usage, coverage gaps, and
  rule violations, **🔒 lock** what you like, and re-generate to fill only the rest.
- **Export** to CSV or Excel (weekly grid + shift list with start/end times).

## Run it online ($0/mo)

The app is deployable to **Streamlit Community Cloud** with data in **Neon Postgres** —
see [DEPLOY.md](DEPLOY.md). The storage layer is dual-backend: local runs use the SQLite
file, and setting a `DATABASE_URL` secret switches it to Postgres automatically.

## Run it locally

```powershell
# from this folder
./run.ps1
```

The script creates a `.venv`, installs dependencies, and launches the app in your browser
(usually http://localhost:8501). On later runs it just starts the app.

Or manually:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Click **Load sample data** in the sidebar to explore with a realistic 15-person, 3-location dataset.

## Access & data

- **Password protected**: on first run the app asks you to create a password (stored as a
  salted PBKDF2 hash in the local database — never in code). Log in once per browser
  session; change it from the sidebar. Forgot it? Reset from this folder:

  ```powershell
  python -c "import sqlite3; c=sqlite3.connect('scheduler.db'); c.execute(\"DELETE FROM meta WHERE key='auth_pw'\"); c.commit()"
  ```

- **Your data never leaves your machine** — everything lives in `scheduler.db` next to the
  app, which is **gitignored** so real schedules and staff names can't end up in the repo.
  Back it up by copying that one file.
- The password gate is appropriate for a local or small-team deployment. If you host it
  publicly, put it behind HTTPS (any reasonable host does this for you).

## How the solver decides (priority order)
1. **Fill coverage** — never leave a location understaffed.
2. **Avoid overstaffing** — mild penalty for exceeding a coverage need.
3. **Budget** — keep everyone close to their target hours (max is a hard cap).
4. **Preferences** — honor prefer-on / prefer-off requests.

## Data model
Single planning week (Mon–Sun). Everything is stored in `scheduler.db` (SQLite) next to the app —
back it up by copying that file. Model assumption: **one location per person per day**.

## Project layout
```
app.py                 Streamlit UI (Schedule / Employees / Locations & Coverage / Requests / Help)
scheduler/db.py        SQLite schema + persistence
scheduler/solver.py    CP-SAT model
scheduler/seed.py      sample dataset
requirements.txt
run.ps1                one-command launcher
```
