# Private Hookup Tracker

A private web app for two people to track hookups and view shared + individual stats.

## Features
- Two-user authentication (defaults: `Scott/puta1` and `Juan/puta2`).
- Mobile-first dashboard layout:
  - stats
  - weekly dashboard (last 7 days, including loads taken)
  - individual summary
  - add encounter
  - latest 5 entries
- Encounter type flow:
  - pick **single / group / orgy / cruising** immediately after date
  - single mode has one person form
  - group + orgy + cruising modes ask count and generate that many per-person forms
- Additional tracking fields:
  - protection used 🛡️
  - substances 💊
  - repeat partner 🔁
  - load taken 💦
  - mood (amazing/good/mid/regret)
- Photo gallery (`/gallery`) with lazy-loaded thumbnails.
- Gallery warns when DB references files that are missing on disk (usually non-persistent deploy storage).
- CSV backup page (`/backup`) with download + restore/append import.
- STD checks page (`/health`) with 90-day countdown until next test for each user.
- Group entries are stored as individual rows linked by `encounter_group_id`.

## Run
```bash
export APP_USER_1="Scott"
export APP_PASS_1="puta1"
export APP_USER_2="Juan"
export APP_PASS_2="puta2"
export PASSWORD_SALT="very-long-random-salt"
python3 app.py
```

Open `http://localhost:8000`.

## Data persistence
- Default storage:
  - DB: `data.db`
  - uploads: `uploads/`
- For Railway volume mounts:
```bash
DATA_DIR=/data
UPLOAD_DIR=/data/uploads
DB_PATH=/data/data.db
```

## Railway
Includes:
- `Procfile` (`web: python3 app.py`)
- `railway.json` (explicit start + healthcheck)

### Enable persistent storage on Railway (beginner steps)
If you do **not** add a volume, Railway can wipe uploaded photos / DB files when redeploying.

1. Open your Railway project.
2. Click your service (the one running this app).
3. Go to the **Volumes** tab.
4. Click **New Volume**.
5. Mount path: `/data`
6. Save/apply.
7. Go to service **Variables** and set:
   - `DATA_DIR=/data`
   - `UPLOAD_DIR=/data/uploads`
   - `DB_PATH=/data/data.db`
8. Redeploy the service.
9. Verify in app:
   - Add a test entry with a photo.
   - Redeploy again.
   - Confirm entry + photo are still there.

Tip: keep using CSV export from `/backup` as an extra safety backup.

## Optional Cloudflare Access
```bash
CF_ACCESS_EMAILS=you@example.com,friend@example.com
```
