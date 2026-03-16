# Private Hookup Tracker

A private web app for two people to track hookups and view shared + individual stats.

## Features
- Two-user authentication (defaults: `Scott/puta1` and `Juan/puta2`).
- Date-based logging (no time required).
- Group/orgy flow:
  - choose encounter type (`single` or `orgy`)
  - set orgy count
  - add comma-separated names (auto-splits to individual entries)
  - optional per-person/group details
- Fields: names, date, location, nationality, photo upload, top/bottom, sucked mode (give/receive/both), rating (1-5), notes.
- Dashboard with emoji summaries and **most popular nationality**.
- By-person summary now includes totals for top/bottom/suck aggregate.
- Dedicated **By person** and **My list** views.
- Dashboard is organized as: stats → individual summary → add encounter → latest 5 entries.
- CSV export endpoint (`/export.csv`).
- Photo gallery page (`/gallery`) for all uploaded photos (lazy-loaded thumbnails).
- Backup safeguard page (`/backup`) for CSV download and CSV restore/append upload.
- In group/orgy mode, each submitted name is saved as its own entry, linked by a shared group id.

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
- Default storage paths:
  - DB: `data.db`
  - uploads: `uploads/`
- You can override paths with env vars for Railway volume mounts:
```bash
DATA_DIR=/data
UPLOAD_DIR=/data/uploads
DB_PATH=/data/data.db
```

## Railway
This repo includes:
- `Procfile` (`web: python3 app.py`)
- `railway.json` with explicit start command and healthcheck.

Set variables in Railway:
```bash
APP_USER_1=Scott
APP_PASS_1=puta1
APP_USER_2=Juan
APP_PASS_2=puta2
PASSWORD_SALT=very-long-random-secret
PORT=8000
DATA_DIR=/data
UPLOAD_DIR=/data/uploads
DB_PATH=/data/data.db
```

## Optional Cloudflare Access
If using Cloudflare Access, set:
```bash
CF_ACCESS_EMAILS=you@example.com,friend@example.com
```
When this is set, app login form is bypassed and Cloudflare identity header is used.


## Group / Orgy input format
In **Group / Orgy** mode, enter one person per line in this format:

`Name|top|bottom|sucked|rating|nationality|notes`

Example:
`Marco|1|0|both|5|Italian|great kisser`
