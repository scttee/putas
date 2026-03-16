# Private Hookup Tracker

A private web app for exactly two people to track hookups and view shared + individual stats.

## Features
- Two-user authentication (`APP_USER_1/2` + `APP_PASS_1/2`).
- Date-based logging (no time required).
- Group entries supported by comma-separated names (e.g. orgy/group encounters).
- Fields: names, date, location, nationality, photo upload, top/bottom, sucked mode (give/receive/both), rating (1-5), notes.
- Dashboard with stats and emoji summaries.
- Dedicated **By person** and **My list** views.
- CSV export endpoint (`/export.csv`).

## Run
```bash
export APP_USER_1="your_name"
export APP_PASS_1="your_password"
export APP_USER_2="friend_name"
export APP_PASS_2="friend_password"
export PASSWORD_SALT="very-long-random-salt"
python3 app.py
```

Open `http://localhost:8000`.

## Railway
This repo includes:
- `Procfile` (`web: python3 app.py`)
- `railway.json` with explicit start command and healthcheck.

Set variables in Railway:
```bash
APP_USER_1=your_name
APP_PASS_1=strong_password_1
APP_USER_2=friend_name
APP_PASS_2=strong_password_2
PASSWORD_SALT=very-long-random-secret
PORT=8000
```

## Optional Cloudflare Access
If using Cloudflare Access, set:
```bash
CF_ACCESS_EMAILS=you@example.com,friend@example.com
```
When this is set, app login form is bypassed and Cloudflare identity header is used.

## Notes
- Uploaded photos are stored in `uploads/`.
- SQLite data is stored in `data.db`; back it up regularly.
