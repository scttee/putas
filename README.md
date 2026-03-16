# Private Hookup Tracker

A private web app for exactly two people to track hookups and view shared + individual stats.

## What it includes
- Two-user authentication (`APP_USER_1/2` + `APP_PASS_1/2`).
- Optional **Cloudflare Access** mode (`CF_ACCESS_EMAILS`) so only approved emails can reach the app.
- Add entries with:
  - name
  - date/time
  - location
  - photo URL
  - topped / bottomed / sucked
  - rating (1-10)
  - notes
- Dashboard with overall totals, per-user summary cards, detailed breakdown table.
- CSV export endpoint (`/export.csv`).
- SQLite storage.

## Run locally

```bash
export APP_USER_1="your_name"
export APP_PASS_1="your_password"
export APP_USER_2="friend_name"
export APP_PASS_2="friend_password"
export PASSWORD_SALT="very-long-random-salt"
python3 app.py
```

Open `http://localhost:8000`.

## Cloudflare-ready setup (recommended)
If you host this app behind Cloudflare, use **Cloudflare Access** for stronger access control than a random URL alone.

1. Put this origin behind Cloudflare (Tunnel, reverse proxy, or VM behind orange-cloud DNS).
2. In Cloudflare Zero Trust, create an Access policy for the app domain.
3. Allow only your two emails.
4. Set this env var in the app:

```bash
export CF_ACCESS_EMAILS="you@example.com,friend@example.com"
```

When `CF_ACCESS_EMAILS` is set, app login form is disabled and access is validated via `Cf-Access-Authenticated-User-Email`.

## Security notes
- Always use HTTPS in production.
- Use strong app passwords and unique `PASSWORD_SALT`.
- Consider running behind Cloudflare Access + WAF.
- SQLite is fine for low-volume private use; back up `data.db`.


## Railway deployment (fix for build/start failures)
If Railway says build/deploy failed, this repo now includes:
- `Procfile` with `web: python3 app.py`
- `railway.json` with explicit `startCommand` and healthcheck.

In Railway service settings, confirm these variables are set:

```bash
APP_USER_1=your_name
APP_PASS_1=strong_password_1
APP_USER_2=friend_name
APP_PASS_2=strong_password_2
PASSWORD_SALT=very-long-random-secret
PORT=8000
```

Optional (recommended when using Cloudflare Access):

```bash
CF_ACCESS_EMAILS=you@example.com,friend@example.com
```

If deploy still fails, check logs for:
- missing env vars
- Python startup command mismatch
- service not listening on expected port
