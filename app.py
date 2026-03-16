from __future__ import annotations

import csv
import hashlib
import hmac
import html
import os
import secrets
import sqlite3
from datetime import date, datetime, timezone
from email.parser import BytesParser
from email.policy import default
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
UPLOAD_DIR = BASE_DIR / "uploads"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "72"))
PASSWORD_SALT = os.environ.get("PASSWORD_SALT", "change-this-salt")
CF_ACCESS_EMAILS = {e.strip().lower() for e in os.environ.get("CF_ACCESS_EMAILS", "").split(",") if e.strip()}

SUCKED_OPTIONS = {"none", "give", "receive", "both"}


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
    return {user_1: hash_password(pass_1), user_2: hash_password(pass_2)}


def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS hookups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorder TEXT NOT NULL,
            partner_names TEXT NOT NULL,
            meetup_date TEXT NOT NULL,
            location TEXT,
            nationality TEXT,
            photo_path TEXT,
            topped INTEGER NOT NULL DEFAULT 0,
            bottomed INTEGER NOT NULL DEFAULT 0,
            sucked_mode TEXT NOT NULL DEFAULT 'none',
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
        CREATE INDEX IF NOT EXISTS idx_hookups_meetup_date ON hookups(meetup_date DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        """
    )
    ensure_columns(db)
    db.commit()
    db.close()


def ensure_columns(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(hookups)").fetchall()}
    migration = {
        "partner_names": "ALTER TABLE hookups ADD COLUMN partner_names TEXT NOT NULL DEFAULT ''",
        "meetup_date": "ALTER TABLE hookups ADD COLUMN meetup_date TEXT NOT NULL DEFAULT '1970-01-01'",
        "nationality": "ALTER TABLE hookups ADD COLUMN nationality TEXT",
        "photo_path": "ALTER TABLE hookups ADD COLUMN photo_path TEXT",
        "sucked_mode": "ALTER TABLE hookups ADD COLUMN sucked_mode TEXT NOT NULL DEFAULT 'none'",
    }
    for name, sql in migration.items():
        if name not in cols:
            db.execute(sql)

    if "partner_name" in cols:
        db.execute("UPDATE hookups SET partner_names = COALESCE(NULLIF(partner_names,''), partner_name)")
    if "meetup_datetime" in cols:
        db.execute("UPDATE hookups SET meetup_date = COALESCE(NULLIF(meetup_date,'1970-01-01'), substr(meetup_datetime,1,10))")
    if "sucked" in cols:
        db.execute("UPDATE hookups SET sucked_mode = CASE WHEN sucked = 1 AND sucked_mode = 'none' THEN 'both' ELSE sucked_mode END")


def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc().isoformat(timespec="seconds"),))


def emoji_summary(row: sqlite3.Row) -> str:
    acts = []
    if row["topped"]:
        acts.append("⬆️")
    if row["bottomed"]:
        acts.append("⬇️")
    suck = row["sucked_mode"]
    if suck == "give":
        acts.append("👄➡️")
    elif suck == "receive":
        acts.append("👄⬅️")
    elif suck == "both":
        acts.append("👄🔁")
    stars = "⭐" * int(row["rating"] or 0)
    return " ".join(acts + ([stars] if stars else [])) or "—"


def page(title: str, body: str) -> bytes:
    styles = (BASE_DIR / "static/styles.css").read_text(encoding="utf-8")
    return f"""<!doctype html><html lang='en'><head>
<meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title><style>{styles}</style></head>
<body><main class='container'>{body}</main></body></html>""".encode("utf-8")


def nav(username: str) -> str:
    return f"""
    <header class='header'>
      <h1>🔥 Hookup Tracker</h1>
      <div class='row'>
        <span class='pill'>👤 {html.escape(username)}</span>
        <a class='btn secondary' href='/'>Dashboard</a>
        <a class='btn secondary' href='/person?name={html.escape(username)}'>My list</a>
        <a class='btn secondary' href='/people'>By person</a>
        <a class='btn secondary' href='/export.csv'>CSV</a>
        <form method='post' action='/logout'><button class='secondary' type='submit'>Logout</button></form>
      </div>
    </header>
    """


def render_login(error: str = "") -> bytes:
    error_html = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    return page(
        "Private Tracker Login",
        f"""
<section class='card narrow'>
  <h1>Private Tracker Login</h1>
  <p>Use one of your two shared accounts to enter.</p>
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
        SELECT COUNT(*) AS total,
               COALESCE(SUM(topped),0) AS topped,
               COALESCE(SUM(bottomed),0) AS bottomed,
               ROUND(AVG(rating),2) AS avg_rating
        FROM hookups
        """
    ).fetchone()

    by_sucked = db.execute(
        """
        SELECT sucked_mode, COUNT(*) AS total
        FROM hookups
        GROUP BY sucked_mode
        """
    ).fetchall()

    per_user = db.execute(
        """
        SELECT recorder, COUNT(*) AS total, ROUND(AVG(rating),2) AS avg_rating
        FROM hookups
        GROUP BY recorder
        ORDER BY total DESC
        """
    ).fetchall()

    entries = db.execute("SELECT * FROM hookups ORDER BY meetup_date DESC, id DESC LIMIT 100").fetchall()
    db.close()

    sucked_map = {r["sucked_mode"]: r["total"] for r in by_sucked}
    feedback = (f"<p class='error'>{html.escape(error)}</p>" if error else "") + (
        f"<p class='success'>{html.escape(success)}</p>" if success else ""
    )
    user_rows = "".join(
        f"<tr><td>{html.escape(r['recorder'])}</td><td>{r['total']}</td><td>{r['avg_rating'] or '-'}</td></tr>" for r in per_user
    ) or "<tr><td colspan='3'>No entries yet.</td></tr>"

    entry_html = "".join(
        f"""
        <article class='entry'>
          <div class='row between'><strong>{html.escape(r['partner_names'])}</strong><span class='muted'>📅 {html.escape(r['meetup_date'])}</span></div>
          <small>📍 {html.escape(r['location'] or 'No location')} · 🌍 {html.escape(r['nationality'] or 'Unknown')} · 👤 {html.escape(r['recorder'])}</small>
          <small>{emoji_summary(r)}</small>
          {f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r['photo_path'] else ''}
          {f"<p>📝 {html.escape(r['notes'])}</p>" if r['notes'] else ''}
        </article>
        """
        for r in entries
    ) or "<p>No entries yet.</p>"

    return page(
        "Dashboard",
        f"""
        {nav(username)}
        {feedback}
        <section class='grid stats'>
          <article class='card'><h3>All Encounters</h3><p class='big'>🔥 {totals['total']}</p></article>
          <article class='card'><h3>Top / Bottom</h3><p class='big'>⬆️ {totals['topped']} · ⬇️ {totals['bottomed']}</p></article>
          <article class='card'><h3>Avg Rating (5)</h3><p class='big'>⭐ {totals['avg_rating'] or '-'}</p></article>
          <article class='card'><h3>Sucked</h3><p class='small'>Give 👄➡️ {sucked_map.get('give', 0)}<br>Receive 👄⬅️ {sucked_map.get('receive', 0)}<br>Both 👄🔁 {sucked_map.get('both', 0)}</p></article>
        </section>

        <section class='card'>
          <h2>Quick add</h2>
          <form method='post' action='/add' enctype='multipart/form-data' class='grid form-grid'>
            <label>Names (comma separated for group) <input name='partner_names' required placeholder='Alex, Marco, Jay'></label>
            <label>Date <input type='date' name='meetup_date' required></label>
            <label>Location <input name='location'></label>
            <label>Nationality <input name='nationality' placeholder='e.g. British, Italian'></label>
            <label>Photo upload <input type='file' name='photo' accept='image/*'></label>
            <label>Rating (1-5) <input type='number' name='rating' min='1' max='5'></label>
            <fieldset class='wide checkbox-row'><legend>Acts</legend>
              <label><input type='checkbox' name='topped'> Topped</label>
              <label><input type='checkbox' name='bottomed'> Bottomed</label>
              <label>Sucked
                <select name='sucked_mode'>
                  <option value='none'>None</option>
                  <option value='give'>Give</option>
                  <option value='receive'>Receive</option>
                  <option value='both'>Both</option>
                </select>
              </label>
            </fieldset>
            <label class='wide'>Notes <textarea name='notes' rows='3' maxlength='3000'></textarea></label>
            <button type='submit'>Save entry</button>
          </form>
        </section>

        <section class='card'>
          <h2>By person summary</h2>
          <table><thead><tr><th>Person</th><th>Count</th><th>Avg ⭐</th></tr></thead><tbody>{user_rows}</tbody></table>
        </section>

        <section class='card'><h2>Latest entries</h2><div class='entries'>{entry_html}</div></section>
        """,
    )


def render_people(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    users = db.execute(
        "SELECT recorder, COUNT(*) AS total, ROUND(AVG(rating),2) AS avg_rating FROM hookups GROUP BY recorder ORDER BY recorder"
    ).fetchall()
    db.close()
    cards = "".join(
        f"<a class='card link-card' href='/person?name={html.escape(u['recorder'])}'><h3>👤 {html.escape(u['recorder'])}</h3><p>{u['total']} entries · ⭐ {u['avg_rating'] or '-'}</p></a>"
        for u in users
    ) or "<p>No entries yet.</p>"
    return page("By person", f"{nav(username)}<section class='grid stats'>{cards}</section>")


def render_person_list(username: str, person: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM hookups WHERE recorder = ? ORDER BY meetup_date DESC, id DESC", (person,)).fetchall()
    db.close()

    items = []
    for r in rows:
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r['photo_path'] else ""
        note_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r['notes'] else ""
        items.append(
            f"<article class='entry'><strong>{html.escape(r['partner_names'])}</strong>"
            f"<small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small>"
            f"<small>🌍 {html.escape(r['nationality'] or 'Unknown')} · {emoji_summary(r)}</small>"
            f"{photo_link}{note_line}</article>"
        )
    entries = "".join(items) or "<p>No entries yet.</p>"
    return page("Person list", f"{nav(username)}<section class='card'><h2>Entries for {html.escape(person)}</h2><div class='entries'>{entries}</div></section>")


def csv_export() -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, recorder, partner_names, meetup_date, location, nationality, photo_path, topped, bottomed, sucked_mode, rating, notes, created_at FROM hookups ORDER BY meetup_date DESC"
    ).fetchall()
    db.close()

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "recorder", "partner_names", "meetup_date", "location", "nationality", "photo_path", "topped", "bottomed", "sucked_mode", "rating", "notes", "created_at"])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])
    return out.getvalue().encode("utf-8")


def parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    ctype = handler.headers.get("Content-Type", "")
    body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    msg = BytesParser(policy=default).parsebytes(
        f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        disp = part.get("Content-Disposition", "")
        if "form-data" not in disp:
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if not name:
            continue
        if filename:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", errors="ignore")
    return fields, files


def safe_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        ext = ".jpg"
    return f"{secrets.token_hex(12)}{ext}"


class Handler(BaseHTTPRequestHandler):
    def current_user(self) -> str | None:
        if CF_ACCESS_EMAILS:
            cf_email = self.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
            if cf_email in CF_ACCESS_EMAILS:
                return cf_email.split("@", 1)[0]
            return None

        raw_cookie = self.headers.get("Cookie")
        if not raw_cookie:
            return None
        sid_cookie = cookies.SimpleCookie(raw_cookie).get("sid")
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
        return {k: v[0] for k, v in parse_qs(payload).items()}

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

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_html(page("404", "<section class='card'><h1>Not found</h1></section>"), 404)
            return
        content = path.read_bytes()
        mime = "image/jpeg"
        if path.suffix.lower() == ".png":
            mime = "image/png"
        elif path.suffix.lower() == ".gif":
            mime = "image/gif"
        elif path.suffix.lower() == ".webp":
            mime = "image/webp"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        user = self.current_user()

        if path == "/":
            if not user:
                self.redirect("/login")
                return
            self.send_html(render_dashboard(user))
            return

        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Access required", "<section class='card'><h1>Access required</h1><p>Cloudflare Access controls this app.</p></section>"), 401)
                return
            self.send_html(render_login())
            return

        if path == "/people":
            if not user:
                self.redirect("/login")
                return
            self.send_html(render_people(user))
            return

        if path == "/person":
            if not user:
                self.redirect("/login")
                return
            person = query.get("name", [user])[0]
            self.send_html(render_person_list(user, person))
            return

        if path == "/export.csv":
            if not user:
                self.redirect("/login")
                return
            self.send_csv(csv_export())
            return

        if path.startswith("/uploads/"):
            if not user:
                self.redirect("/login")
                return
            target = Path(path.removeprefix("/uploads/")).name
            self.send_file(UPLOAD_DIR / target)
            return

        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()

        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Forbidden", "<section class='card'><h1>Forbidden</h1></section>"), 403)
                return

            data = self.form_data()
            username = data.get("username", "").strip()
            expected_hash = configured_users().get(username)
            if expected_hash and verify_password(data.get("password", ""), expected_hash):
                sid = secrets.token_urlsafe(24)
                expires_at = datetime.fromtimestamp(now_utc().timestamp() + SESSION_TTL_HOURS * 3600, tz=timezone.utc).isoformat(timespec="seconds")
                db = sqlite3.connect(DB_PATH)
                cleanup_expired_sessions(db)
                db.execute("INSERT INTO sessions (sid, username, expires_at, created_at) VALUES (?, ?, ?, ?)", (sid, username, expires_at, now_utc().isoformat(timespec="seconds")))
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
            raw_cookie = self.headers.get("Cookie", "")
            sid = cookies.SimpleCookie(raw_cookie).get("sid")
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
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" in ctype:
                data, files = parse_multipart(self)
            else:
                data, files = self.form_data(), {}

            try:
                partner_names = data.get("partner_names", "").strip()
                if not partner_names:
                    raise ValueError("Names are required")

                meetup_date = date.fromisoformat(data.get("meetup_date", "")).isoformat()
                rating_raw = data.get("rating", "").strip()
                rating = int(rating_raw) if rating_raw else None
                if rating is not None and not (1 <= rating <= 5):
                    raise ValueError("Rating must be between 1 and 5")

                sucked_mode = data.get("sucked_mode", "none")
                if sucked_mode not in SUCKED_OPTIONS:
                    sucked_mode = "none"

                saved_photo = None
                photo = files.get("photo")
                if photo and photo[1]:
                    filename = safe_filename(photo[0])
                    (UPLOAD_DIR / filename).write_bytes(photo[1])
                    saved_photo = filename

                db = sqlite3.connect(DB_PATH)
                db.execute(
                    """
                    INSERT INTO hookups (
                      recorder, partner_names, meetup_date, location, nationality, photo_path,
                      topped, bottomed, sucked_mode, rating, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user,
                        partner_names,
                        meetup_date,
                        data.get("location", "").strip(),
                        data.get("nationality", "").strip(),
                        saved_photo,
                        1 if data.get("topped") else 0,
                        1 if data.get("bottomed") else 0,
                        sucked_mode,
                        rating,
                        data.get("notes", "").strip(),
                        now_utc().isoformat(timespec="seconds"),
                    ),
                )
                db.commit()
                db.close()
                self.send_html(render_dashboard(user, success="Entry saved ✅"), 201)
                return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Could not save entry: {exc}"), 400)
                return

        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server running on http://{HOST}:{PORT}")
    server.serve_forever()
