from __future__ import annotations

import csv
import hashlib
import hmac
import html
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "72"))
PASSWORD_SALT = os.environ.get("PASSWORD_SALT", "change-this-salt")
CF_ACCESS_EMAILS = {e.strip().lower() for e in os.environ.get("CF_ACCESS_EMAILS", "").split(",") if e.strip()}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=PASSWORD_SALT.encode("utf-8"),
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return digest.hex()


def verify_password(password: str, password_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), password_hash)


def configured_users() -> dict[str, str]:
    user_1 = os.environ.get("APP_USER_1", "you")
    pass_1 = os.environ.get("APP_PASS_1", "pass1")
    user_2 = os.environ.get("APP_USER_2", "friend")
    pass_2 = os.environ.get("APP_PASS_2", "pass2")
    return {
        user_1: hash_password(pass_1),
        user_2: hash_password(pass_2),
    }


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS hookups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorder TEXT NOT NULL,
            partner_name TEXT NOT NULL,
            meetup_datetime TEXT NOT NULL,
            location TEXT,
            photo_url TEXT,
            topped INTEGER NOT NULL DEFAULT 0,
            bottomed INTEGER NOT NULL DEFAULT 0,
            sucked INTEGER NOT NULL DEFAULT 0,
            rating INTEGER,
            notes TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_hookups_recorder ON hookups(recorder);
        CREATE INDEX IF NOT EXISTS idx_hookups_meetup_datetime ON hookups(meetup_datetime DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        """
    )
    db.commit()
    db.close()


def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc().isoformat(timespec="seconds"),))


def page(title: str, body: str) -> bytes:
    styles = (BASE_DIR / "static/styles.css").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title>
<style>{styles}</style>
</head>
<body>
<main class='container'>{body}</main>
</body>
</html>""".encode("utf-8")


def render_login(error: str = "") -> bytes:
    error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    return page(
        "Private Tracker Login",
        f"""
<section class='card login'>
  <h1>Private Tracker Login</h1>
  <p>Only the two configured users can access this page.</p>
  {error_html}
  <form method='post' action='/login'>
    <label>Username <input name='username' required autocomplete='username'></label>
    <label>Password <input type='password' name='password' required autocomplete='current-password'></label>
    <button type='submit'>Sign in</button>
  </form>
</section>
        """,
    )


def render_dashboard(username: str, error: str = "", success: str = "") -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    totals = db.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(topped), 0) AS topped,
            COALESCE(SUM(bottomed), 0) AS bottomed,
            COALESCE(SUM(sucked), 0) AS sucked,
            ROUND(AVG(rating), 2) AS avg_rating
        FROM hookups
        """
    ).fetchone()

    per_user = db.execute(
        """
        SELECT
            recorder,
            COUNT(*) AS total,
            COALESCE(SUM(topped), 0) AS topped,
            COALESCE(SUM(bottomed), 0) AS bottomed,
            COALESCE(SUM(sucked), 0) AS sucked,
            ROUND(AVG(rating), 2) AS avg_rating
        FROM hookups
        GROUP BY recorder
        ORDER BY total DESC
        """
    ).fetchall()

    latest_entries = db.execute(
        """
        SELECT *
        FROM hookups
        ORDER BY meetup_datetime DESC
        LIMIT 100
        """
    ).fetchall()
    db.close()

    per_user_map = {row["recorder"]: row for row in per_user}
    p1_name = os.environ.get("APP_USER_1", "you")
    p2_name = os.environ.get("APP_USER_2", "friend")

    def person_card(name: str) -> str:
        row = per_user_map.get(name)
        if not row:
            return f"<article class='card'><h3>{html.escape(name)}</h3><p class='big'>0</p><p class='muted'>No entries yet.</p></article>"
        return (
            f"<article class='card'><h3>{html.escape(name)}</h3>"
            f"<p class='big'>{row['total']}</p>"
            f"<p class='muted'>T/B/S: {row['topped']}/{row['bottomed']}/{row['sucked']} · Avg: {row['avg_rating'] or '-'}</p>"
            "</article>"
        )

    rows = "".join(
        f"<tr><td>{html.escape(r['recorder'])}</td><td>{r['total']}</td><td>{r['topped']}</td><td>{r['bottomed']}</td><td>{r['sucked']}</td><td>{r['avg_rating'] or '-'}</td></tr>"
        for r in per_user
    ) or "<tr><td colspan='6'>No entries yet.</td></tr>"

    entries_html = "".join(
        f"""
<article class='entry'>
  <div class='row between'><strong>{html.escape(r['partner_name'])}</strong><span class='badge'>#{r['id']}</span></div>
  <small>{html.escape(r['meetup_datetime'])} · {html.escape(r['location'] or 'No location')}</small>
  <small>By {html.escape(r['recorder'])} | T/B/S: {r['topped']}/{r['bottomed']}/{r['sucked']} | Rating: {r['rating'] if r['rating'] else '-'}</small>
  {f"<a href='{html.escape(r['photo_url'])}' target='_blank' rel='noopener'>Open photo</a>" if r['photo_url'] else ''}
  {f"<p>{html.escape(r['notes'])}</p>" if r['notes'] else ''}
</article>
        """
        for r in latest_entries
    ) or "<p>No entries yet.</p>"

    feedback = ""
    if error:
        feedback += f"<p class='error'>{html.escape(error)}</p>"
    if success:
        feedback += f"<p class='success'>{html.escape(success)}</p>"

    return page(
        "Hookup Dashboard",
        f"""
<header class='row between'>
  <h1>Hookup Dashboard</h1>
  <div class='row'>
    <span class='pill'>Signed in as {html.escape(username)}</span>
    <form method='post' action='/logout'><button class='secondary' type='submit'>Logout</button></form>
  </div>
</header>

{feedback}

<section class='grid stats'>
  <article class='card'><h3>Total Entries</h3><p class='big'>{totals['total']}</p></article>
  <article class='card'><h3>Top / Bottom / Sucked</h3><p class='big'>{totals['topped']} / {totals['bottomed']} / {totals['sucked']}</p></article>
  <article class='card'><h3>Average Rating</h3><p class='big'>{totals['avg_rating'] or '-'}</p></article>
</section>

<section class='grid stats'>
  {person_card(p1_name)}
  {person_card(p2_name)}
</section>

<section class='card'>
  <h2>Per-person breakdown</h2>
  <table>
    <thead><tr><th>Recorder</th><th>Total</th><th>Topped</th><th>Bottomed</th><th>Sucked</th><th>Avg rating</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</section>

<section class='card'>
  <h2>Add entry</h2>
  <form method='post' action='/add' class='grid form-grid'>
    <label>Partner name <input name='partner_name' required></label>
    <label>Date & time <input type='datetime-local' name='meetup_datetime' required></label>
    <label>Location <input name='location'></label>
    <label>Photo URL <input type='url' name='photo_url' placeholder='https://...'></label>
    <label>Rating (1-10) <input type='number' name='rating' min='1' max='10'></label>
    <label class='wide'>Notes <textarea name='notes' rows='3' maxlength='3000'></textarea></label>
    <fieldset class='wide checkbox-row'>
      <legend>Acts</legend>
      <label><input type='checkbox' name='topped'> Topped</label>
      <label><input type='checkbox' name='bottomed'> Bottomed</label>
      <label><input type='checkbox' name='sucked'> Sucked</label>
    </fieldset>
    <button type='submit'>Save entry</button>
  </form>
</section>

<section class='card row between'>
  <h2>Export data</h2>
  <a class='btn-link' href='/export.csv'>Download CSV</a>
</section>

<section class='card'>
  <h2>Latest entries</h2>
  <div class='entries'>{entries_html}</div>
</section>
        """,
    )


def csv_export() -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT id, recorder, partner_name, meetup_datetime, location, photo_url, topped, bottomed, sucked, rating, notes, created_at
        FROM hookups
        ORDER BY meetup_datetime DESC
        """
    ).fetchall()
    db.close()

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "recorder", "partner_name", "meetup_datetime", "location", "photo_url", "topped", "bottomed", "sucked", "rating", "notes", "created_at"])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])
    return out.getvalue().encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def current_user(self) -> str | None:
        # Optional Cloudflare Access gate: if set, require known email in Access header.
        if CF_ACCESS_EMAILS:
            cf_email = self.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
            if cf_email in CF_ACCESS_EMAILS:
                # Map email local part to app username, preserving two-user model.
                return cf_email.split("@", 1)[0]
            return None

        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        jar = cookies.SimpleCookie(raw_cookie)
        sid_cookie = jar.get("sid")
        if not sid_cookie:
            return None

        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        cleanup_expired_sessions(db)
        row = db.execute("SELECT username FROM sessions WHERE sid = ?", (sid_cookie.value,)).fetchone()
        db.commit()
        db.close()
        return row["username"] if row else None

    def form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length).decode("utf-8")
        parsed = parse_qs(payload)
        return {k: v[0] for k, v in parsed.items()}

    def send_html(self, content: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_csv(self, content: bytes, filename: str = "hookups.csv") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()

        if path == "/":
            if not user:
                self.redirect("/login")
                return
            self.send_html(render_dashboard(user))
            return

        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Access required", "<section class='card'><h1>Access required</h1><p>This app is protected by Cloudflare Access. Log in through your Access policy.</p></section>"), 401)
                return
            self.send_html(render_login())
            return

        if path == "/export.csv":
            if not user:
                self.redirect("/login")
                return
            self.send_csv(csv_export())
            return

        self.send_html(page("Not found", "<section class='card'><h1>404</h1></section>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()

        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Forbidden", "<section class='card'><h1>Forbidden</h1><p>Direct login disabled while Cloudflare Access mode is enabled.</p></section>"), 403)
                return

            data = self.form_data()
            username = data.get("username", "").strip()
            password = data.get("password", "")

            users = configured_users()
            expected_hash = users.get(username)
            if expected_hash and verify_password(password, expected_hash):
                sid = secrets.token_urlsafe(24)
                expires = (now_utc().timestamp() + SESSION_TTL_HOURS * 3600)
                expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(timespec="seconds")

                db = sqlite3.connect(DB_PATH)
                cleanup_expired_sessions(db)
                db.execute(
                    "INSERT INTO sessions (sid, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                    (sid, username, expires_at, now_utc().isoformat(timespec="seconds")),
                )
                db.commit()
                db.close()

                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
                return

            self.send_html(render_login("Invalid credentials."), 401)
            return

        if path == "/logout":
            if not CF_ACCESS_EMAILS:
                raw_cookie = self.headers.get("Cookie", "")
                jar = cookies.SimpleCookie(raw_cookie)
                sid = jar.get("sid")
                if sid:
                    db = sqlite3.connect(DB_PATH)
                    db.execute("DELETE FROM sessions WHERE sid = ?", (sid.value,))
                    db.commit()
                    db.close()

            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "sid=; Max-Age=0; Path=/")
            self.end_headers()
            return

        if path == "/add":
            if not user:
                self.redirect("/login")
                return

            data = self.form_data()
            try:
                partner_name = data.get("partner_name", "").strip()
                if not partner_name:
                    raise ValueError("Partner name is required")

                meetup = datetime.fromisoformat(data.get("meetup_datetime", "")).isoformat(timespec="minutes")

                rating_raw = data.get("rating", "").strip()
                rating = int(rating_raw) if rating_raw else None
                if rating is not None and not (1 <= rating <= 10):
                    raise ValueError("Rating must be between 1 and 10")

                db = sqlite3.connect(DB_PATH)
                db.execute(
                    """
                    INSERT INTO hookups (
                        recorder, partner_name, meetup_datetime, location, photo_url,
                        topped, bottomed, sucked, rating, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user,
                        partner_name,
                        meetup,
                        data.get("location", "").strip(),
                        data.get("photo_url", "").strip(),
                        1 if data.get("topped") else 0,
                        1 if data.get("bottomed") else 0,
                        1 if data.get("sucked") else 0,
                        rating,
                        data.get("notes", "").strip(),
                        now_utc().isoformat(timespec="seconds"),
                    ),
                )
                db.commit()
                db.close()
                self.send_html(render_dashboard(user, success="Entry saved."), 201)
                return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Could not save entry: {exc}"), 400)
                return

        self.send_html(page("Not found", "<section class='card'><h1>404</h1></section>"), 404)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server running on http://{HOST}:{PORT}")
    server.serve_forever()
