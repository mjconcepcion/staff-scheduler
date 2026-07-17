# Deploying online ($0/mo): Streamlit Community Cloud + Neon Postgres

The app runs in two modes automatically:

| Mode | Where state lives | When |
|---|---|---|
| Local | `scheduler.db` (SQLite file) | `DATABASE_URL` not set — running `run.ps1` as always |
| Hosted | Neon Postgres | `DATABASE_URL` set (via Streamlit secrets) |

## 1. Create the database (Neon — free)

1. Sign up at **neon.tech** (GitHub login works).
2. Create a project (any name, e.g. `staff-scheduler`; region close to you).
3. On the project dashboard, copy the **connection string** — choose the **pooled**
   one (host contains `-pooler`). It looks like:
   `postgresql://user:password@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require`

## 2. Migrate your existing data (one command)

From this folder in PowerShell (paste your own connection string):

```powershell
$env:DATABASE_URL = "postgresql://...your connection string..."
.venv\Scripts\python.exe tools\migrate_to_postgres.py
```

It copies every table, verifies row counts, and carries your password over.
(Skip this if you'd rather start fresh online — the app will show the first-run
password setup instead.)

Close the terminal afterwards so the connection string doesn't linger in the session.

## 3. Deploy the app (Streamlit Community Cloud — free)

1. Sign up at **share.streamlit.io** with your GitHub account.
2. **New app** → repo `mjconcepcion/staff-scheduler`, branch `main`, file `app.py`.
3. Before (or after) deploying, open the app's **Settings → Secrets** and paste:

   ```toml
   DATABASE_URL = "postgresql://...your connection string..."
   ```

4. Deploy. You get a URL like `https://<name>.streamlit.app` — pick the subdomain in
   settings. Share that URL + the password with whoever needs it.

## Good to know

- **Cold starts**: the free tier puts the app to sleep after inactivity. First visit
  of the day takes ~30–60 s to wake; after that it's normal speed.
- **Neon idle**: the free database also suspends when idle; the app reconnects
  automatically (first query after a long gap adds a second or two).
- **Your machine is now irrelevant**: the app runs from GitHub, the data lives in Neon.
  Local `run.ps1` still works against the local SQLite file — handy as a sandbox, but
  it is a *separate* copy of the data.
- **Backups**: Neon keeps point-in-time history on free tier. For belt-and-suspenders,
  re-run the migration in reverse someday or export CSV/Excel from the Schedule page.
- **Updating the app**: `git push` — Streamlit Cloud redeploys automatically.
