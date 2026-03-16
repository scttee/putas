# Private Hookup Tracker

A private web app for two people to track hookups and view shared + individual stats.

## Features
- Two-user authentication (defaults: `Scott/puta1` and `Juan/puta2`).
- Mobile-first dashboard layout:
  - stats
  - individual summary
  - add encounter
  - latest 5 entries
- Encounter type flow:
  - pick **single** or **group** immediately after date
  - single mode has one person form
  - group mode asks count and generates that many per-person forms
- Additional tracking fields:
  - protection used 🛡️
  - substances 💊
  - repeat partner 🔁
  - mood (amazing/good/mid/regret)
- Photo gallery (`/gallery`) with lazy-loaded thumbnails.
- CSV backup page (`/backup`) with download + restore/append import.
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

## Optional Cloudflare Access
```bash
CF_ACCESS_EMAILS=you@example.com,friend@example.com
```
