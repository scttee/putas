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
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR))).resolve()
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(DATA_DIR / "uploads"))).resolve()
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "data.db"))).resolve()
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "72"))
PASSWORD_SALT = os.environ.get("PASSWORD_SALT", "change-this-salt")
CF_ACCESS_EMAILS = {e.strip().lower() for e in os.environ.get("CF_ACCESS_EMAILS", "").split(",") if e.strip()}

SUCKED_OPTIONS = {"none", "give", "receive", "both"}
ENCOUNTER_OPTIONS = {"single", "orgy"}


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
    return {
        os.environ.get("APP_USER_1", "Scott"): hash_password(os.environ.get("APP_PASS_1", "puta1")),
        os.environ.get("APP_USER_2", "Juan"): hash_password(os.environ.get("APP_PASS_2", "puta2")),
    }


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
            encounter_type TEXT NOT NULL DEFAULT 'single',
            orgy_count INTEGER,
            orgy_details TEXT,
            encounter_group_id TEXT,
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
        "encounter_type": "ALTER TABLE hookups ADD COLUMN encounter_type TEXT NOT NULL DEFAULT 'single'",
        "orgy_count": "ALTER TABLE hookups ADD COLUMN orgy_count INTEGER",
        "orgy_details": "ALTER TABLE hookups ADD COLUMN orgy_details TEXT",
        "encounter_group_id": "ALTER TABLE hookups ADD COLUMN encounter_group_id TEXT",
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

    db.execute("UPDATE hookups SET encounter_type = COALESCE(NULLIF(encounter_type,''), 'single')")


def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc().isoformat(timespec="seconds"),))


def emoji_summary(row: sqlite3.Row) -> str:
    acts: list[str] = []
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
        <a class='btn secondary' href='/gallery'>Gallery</a>
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
<section class='card narrow stack'>
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
        "SELECT COUNT(*) total, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, ROUND(AVG(rating),2) avg_rating FROM hookups"
    ).fetchone()
    popular_nationality = db.execute(
        "SELECT nationality, COUNT(*) total FROM hookups WHERE COALESCE(TRIM(nationality), '') <> '' GROUP BY nationality ORDER BY total DESC, nationality ASC LIMIT 1"
    ).fetchone()
    by_sucked = db.execute("SELECT sucked_mode, COUNT(*) total FROM hookups GROUP BY sucked_mode").fetchall()
    per_user = db.execute(
        """
        SELECT recorder, COUNT(*) total,
               COALESCE(SUM(topped),0) topped,
               COALESCE(SUM(bottomed),0) bottomed,
               COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total,
               ROUND(AVG(rating),2) avg_rating
        FROM hookups GROUP BY recorder ORDER BY total DESC
        """
    ).fetchall()
    entries = db.execute("SELECT * FROM hookups ORDER BY meetup_date DESC, id DESC LIMIT 100").fetchall()
    db.close()

    sucked_map = {r["sucked_mode"]: r["total"] for r in by_sucked}
    feedback = (f"<p class='error'>{html.escape(error)}</p>" if error else "") + (f"<p class='success'>{html.escape(success)}</p>" if success else "")
    user_rows = "".join(
        f"<tr><td>{html.escape(r['recorder'])}</td><td>{r['total']}</td><td>{r['topped']}</td><td>{r['bottomed']}</td><td>{r['sucked_total']}</td><td>{r['avg_rating'] or '-'}</td></tr>"
        for r in per_user
    ) or "<tr><td colspan='6'>No entries yet.</td></tr>"

    entry_items = []
    for r in entries:
        orgy_tag = f"🎉 Orgy x{r['orgy_count']}" if r["encounter_type"] == "orgy" and r["orgy_count"] else "👥 Standard"
        group_line = f" · 🧷 Group {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        orgy_line = f"<p>📋 {html.escape(r['orgy_details'])}</p>" if r["orgy_details"] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r["notes"] else ""
        entry_items.append(
            f"<article class='entry stack-sm'><div class='row between'><strong>{html.escape(r['partner_names'])}</strong><span class='muted'>📅 {html.escape(r['meetup_date'])}</span></div><small>📍 {html.escape(r['location'] or 'No location')} · 🌍 {html.escape(r['nationality'] or 'Unknown')} · 👤 {html.escape(r['recorder'])}</small><small>{orgy_tag}{group_line} · {emoji_summary(r)}</small>{photo_link}{orgy_line}{notes_line}</article>"
        )
    entry_html = "".join(entry_items) or "<p>No entries yet.</p>"
    top_nat = html.escape(popular_nationality["nationality"]) if popular_nationality else "-"

    return page(
        "Dashboard",
        f"""
        {nav(username)}
        {feedback}

        <section class='grid stats block-gap'>
          <article class='card stack-sm'><h3>All Encounters</h3><p class='big'>🔥 {totals['total']}</p></article>
          <article class='card stack-sm'><h3>Top / Bottom</h3><p class='big'>⬆️ {totals['topped']} · ⬇️ {totals['bottomed']}</p></article>
          <article class='card stack-sm'><h3>Avg Rating (5)</h3><p class='big'>⭐ {totals['avg_rating'] or '-'}</p></article>
          <article class='card stack-sm'><h3>Sucked</h3><p class='small'>Give 👄➡️ {sucked_map.get('give', 0)}<br>Receive 👄⬅️ {sucked_map.get('receive', 0)}<br>Both 👄🔁 {sucked_map.get('both', 0)}</p></article>
          <article class='card stack-sm'><h3>Top Nationality</h3><p class='big'>🌍 {top_nat}</p></article>
        </section>

        <section class='card stack block-gap'>
          <h2>Quick add</h2>
          <form method='post' action='/add' enctype='multipart/form-data' class='grid form-grid'>
            <label>People names (comma-separated) <input name='partner_names' required placeholder='Alex, Marco, Jay'></label>
            <label>Date <input type='date' name='meetup_date' required></label>
            <label>Location <input name='location'></label>
            <label>Nationality <input name='nationality' placeholder='e.g. British, Italian'></label>
            <label>Encounter type
              <select name='encounter_type'>
                <option value='single'>Single / standard</option>
                <option value='orgy'>Orgy / group</option>
              </select>
            </label>
            <label>Orgy count (if group) <input type='number' min='2' name='orgy_count' placeholder='e.g. 4'></label>
            <label>Photo upload <input type='file' name='photo' accept='image/*'></label>
            <label>Rating (1-5) <input type='number' name='rating' min='1' max='5'></label>
            <fieldset class='wide checkbox-row'>
              <legend>Acts</legend>
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
            <label class='wide'>Group details (optional) <textarea name='orgy_details' rows='3' placeholder='Who did what, one line each'></textarea></label>
            <p class='muted wide'>Tip: multiple names are saved as individual entries ✅</p>
            <label class='wide'>Notes <textarea name='notes' rows='3' maxlength='3000'></textarea></label>
            <button type='submit'>Save entry</button>
          </form>
        </section>

        <section class='card stack block-gap'>
          <h2>Backup safeguard</h2>
          <p class='muted'>Download CSV anytime, and if needed upload it back to restore/append entries.</p>
          <div class='row'>
            <a class='btn secondary' href='/export.csv'>Download backup CSV</a>
          </div>
          <form method='post' action='/import.csv' enctype='multipart/form-data' class='stack-sm'>
            <label>Upload backup CSV <input type='file' name='backup_csv' accept='.csv,text/csv' required></label>
            <button type='submit'>Import backup</button>
          </form>
        </section>

        <section class='card stack block-gap'>
          <div class='section-title'><h2>By person summary</h2><a class='btn secondary' href='/people'>Open full view</a></div>
          <div class='table-wrap'><table><thead><tr><th>Person</th><th>Count</th><th>Top</th><th>Bottom</th><th>Suck*</th><th>Avg ⭐</th></tr></thead><tbody>{user_rows}</tbody></table></div>
          <p class='muted'>*Suck total aggregates give/receive/both into one count.</p>
        </section>

        <section class='card stack block-gap'><div class='section-title'><h2>Latest entries</h2><a class='btn secondary' href='/person?name={html.escape(username)}'>My entries</a></div><div class='entries'>{entry_html}</div></section>
        """,
    )


def render_people(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    users = db.execute(
        """
        SELECT recorder, COUNT(*) total,
               COALESCE(SUM(topped),0) topped,
               COALESCE(SUM(bottomed),0) bottomed,
               COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total,
               ROUND(AVG(rating),2) avg_rating
        FROM hookups GROUP BY recorder ORDER BY recorder
        """
    ).fetchall()
    db.close()
    cards = "".join(
        f"<a class='card link-card stack-sm' href='/person?name={html.escape(u['recorder'])}'><h3>👤 {html.escape(u['recorder'])}</h3><p>{u['total']} entries · ⬆️ {u['topped']} · ⬇️ {u['bottomed']} · 👄 {u['sucked_total']} · ⭐ {u['avg_rating'] or '-'}</p></a>"
        for u in users
    ) or "<p>No entries yet.</p>"
    return page("By person", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>By person</h2><span class='muted'>Tap a card for full list</span></div><section class='grid stats'>{cards}</section></section>")


def render_person_list(username: str, person: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM hookups WHERE recorder = ? ORDER BY meetup_date DESC, id DESC", (person,)).fetchall()
    db.close()
    items = []
    for r in rows:
        orgy_tag = f"🎉 Orgy x{r['orgy_count']}" if r["encounter_type"] == "orgy" and r["orgy_count"] else "👥 Standard"
        group_line = f" · 🧷 Group {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        orgy_line = f"<p>📋 {html.escape(r['orgy_details'])}</p>" if r["orgy_details"] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r["notes"] else ""
        items.append(
            f"<article class='entry stack-sm'><strong>{html.escape(r['partner_names'])}</strong><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small><small>🌍 {html.escape(r['nationality'] or 'Unknown')} · {orgy_tag}{group_line} · {emoji_summary(r)}</small>{photo_link}{orgy_line}{notes_line}</article>"
        )
    entries = "".join(items) or "<p>No entries yet.</p>"
    return page("Person list", f"{nav(username)}<section class='card stack block-gap'><h2>Entries for {html.escape(person)}</h2><div class='entries'>{entries}</div></section>")


def render_gallery(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    photos = db.execute(
        """
        SELECT id, partner_names, recorder, meetup_date, photo_path, location
        FROM hookups
        WHERE COALESCE(photo_path, '') <> ''
        ORDER BY meetup_date DESC, id DESC
        """
    ).fetchall()
    db.close()

    tiles = "".join(
        f"<article class='card stack-sm'><a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'><img class='thumb' src='/uploads/{html.escape(r['photo_path'])}' alt='photo {r['id']}'></a><small>👤 {html.escape(r['partner_names'])} · {html.escape(r['recorder'])}</small><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small></article>"
        for r in photos
    ) or "<p>No uploaded photos yet.</p>"

    return page("Gallery", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>Photo gallery</h2><span class='muted'>{len(photos)} photos</span></div><div class='gallery-grid'>{tiles}</div></section>")


def csv_export() -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """
        SELECT id, recorder, partner_names, meetup_date, location, nationality, photo_path,
               encounter_type, orgy_count, orgy_details, encounter_group_id,
               topped, bottomed, sucked_mode, rating, notes, created_at
        FROM hookups ORDER BY meetup_date DESC
        """
    ).fetchall()
    db.close()

    out = StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "id", "recorder", "partner_names", "meetup_date", "location", "nationality", "photo_path",
        "encounter_type", "orgy_count", "orgy_details", "encounter_group_id",
        "topped", "bottomed", "sucked_mode", "rating", "notes", "created_at"
    ])
    for r in rows:
        writer.writerow([r[k] for k in r.keys()])
    return out.getvalue().encode("utf-8")


def import_csv_bytes(content: bytes) -> int:
    text = content.decode("utf-8", errors="replace")
    reader = csv.DictReader(StringIO(text))
    required = {"recorder", "partner_names", "meetup_date", "sucked_mode"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("Invalid CSV format for import")

    inserted = 0
    db = sqlite3.connect(DB_PATH)
    for row in reader:
        try:
            meetup_date = date.fromisoformat((row.get("meetup_date") or "").strip()).isoformat()
            partner_name = (row.get("partner_names") or "").strip()
            recorder = (row.get("recorder") or "").strip()
            if not partner_name or not recorder:
                continue
            sucked_mode = (row.get("sucked_mode") or "none").strip()
            if sucked_mode not in SUCKED_OPTIONS:
                sucked_mode = "none"
            encounter_type = (row.get("encounter_type") or "single").strip()
            if encounter_type not in ENCOUNTER_OPTIONS:
                encounter_type = "single"
            orgy_count_raw = (row.get("orgy_count") or "").strip()
            orgy_count = int(orgy_count_raw) if orgy_count_raw else None
            rating_raw = (row.get("rating") or "").strip()
            rating = int(rating_raw) if rating_raw else None
            if rating is not None and not (1 <= rating <= 5):
                rating = None
            topped = 1 if str(row.get("topped", "0")).strip() in {"1", "true", "True"} else 0
            bottomed = 1 if str(row.get("bottomed", "0")).strip() in {"1", "true", "True"} else 0

            db.execute(
                """
                INSERT INTO hookups (
                  recorder, partner_names, meetup_date, location, nationality, photo_path,
                  encounter_type, orgy_count, orgy_details, encounter_group_id,
                  topped, bottomed, sucked_mode, rating, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recorder,
                    partner_name,
                    meetup_date,
                    (row.get("location") or "").strip(),
                    (row.get("nationality") or "").strip(),
                    (row.get("photo_path") or "").strip() or None,
                    encounter_type,
                    orgy_count,
                    (row.get("orgy_details") or "").strip(),
                    (row.get("encounter_group_id") or "").strip() or None,
                    topped,
                    bottomed,
                    sucked_mode,
                    rating,
                    (row.get("notes") or "").strip(),
                    now_utc().isoformat(timespec="seconds"),
                ),
            )
            inserted += 1
        except Exception:
            continue
    db.commit()
    db.close()
    return inserted


def parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    ctype = handler.headers.get("Content-Type", "")
    body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    msg = BytesParser(policy=default).parsebytes(f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        if "form-data" not in part.get("Content-Disposition", ""):
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
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
            return cf_email.split("@", 1)[0] if cf_email in CF_ACCESS_EMAILS else None

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
            self.send_html(render_person_list(user, query.get("name", [user])[0]))
            return
        if path == "/gallery":
            if not user:
                self.redirect("/login")
                return
            self.send_html(render_gallery(user))
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
            self.send_file(UPLOAD_DIR / Path(path.removeprefix("/uploads/")).name)
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
            sid = cookies.SimpleCookie(self.headers.get("Cookie", "")).get("sid")
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
            data, files = parse_multipart(self) if "multipart/form-data" in self.headers.get("Content-Type", "") else (self.form_data(), {})
            try:
                names = [n.strip() for n in data.get("partner_names", "").replace("\n", ",").split(",") if n.strip()]
                if not names:
                    raise ValueError("Names are required")
                meetup_date = date.fromisoformat(data.get("meetup_date", "")).isoformat()
                rating_raw = data.get("rating", "").strip()
                rating = int(rating_raw) if rating_raw else None
                if rating is not None and not (1 <= rating <= 5):
                    raise ValueError("Rating must be between 1 and 5")
                sucked_mode = data.get("sucked_mode", "none")
                if sucked_mode not in SUCKED_OPTIONS:
                    sucked_mode = "none"
                encounter_type = data.get("encounter_type", "single")
                if encounter_type not in ENCOUNTER_OPTIONS:
                    encounter_type = "single"
                if len(names) > 1:
                    encounter_type = "orgy"
                typed_count = int(data.get("orgy_count", "").strip()) if data.get("orgy_count", "").strip() else None
                if encounter_type == "orgy":
                    orgy_count = len(names)
                    if typed_count is not None and typed_count != orgy_count:
                        raise ValueError(f"You entered {typed_count} but provided {orgy_count} names")
                    if orgy_count < 2:
                        raise ValueError("For orgy mode, provide at least 2 names")
                else:
                    orgy_count = None
                encounter_group_id = secrets.token_hex(3).upper() if encounter_type == "orgy" else None

                saved_photo = None
                photo = files.get("photo")
                if photo and photo[1]:
                    saved_photo = safe_filename(photo[0])
                    (UPLOAD_DIR / saved_photo).write_bytes(photo[1])

                db = sqlite3.connect(DB_PATH)
                for partner_name in names:
                    db.execute(
                        """
                        INSERT INTO hookups (
                          recorder, partner_names, meetup_date, location, nationality, photo_path,
                          encounter_type, orgy_count, orgy_details, encounter_group_id,
                          topped, bottomed, sucked_mode, rating, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user,
                            partner_name,
                            meetup_date,
                            data.get("location", "").strip(),
                            data.get("nationality", "").strip(),
                            saved_photo,
                            encounter_type,
                            orgy_count,
                            data.get("orgy_details", "").strip(),
                            encounter_group_id,
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
                self.send_html(render_dashboard(user, success=f"Saved {len(names)} individual entr{'y' if len(names)==1 else 'ies'} ✅"), 201)
                return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Could not save entry: {exc}"), 400)
                return

        if path == "/import.csv":
            if not user:
                self.redirect("/login")
                return
            if "multipart/form-data" not in self.headers.get("Content-Type", ""):
                self.send_html(render_dashboard(user, error="Please upload a CSV file."), 400)
                return
            _, files = parse_multipart(self)
            backup = files.get("backup_csv")
            if not backup or not backup[1]:
                self.send_html(render_dashboard(user, error="Backup file missing."), 400)
                return
            try:
                imported = import_csv_bytes(backup[1])
                self.send_html(render_dashboard(user, success=f"Imported {imported} rows from backup ✅"), 201)
                return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Import failed: {exc}"), 400)
                return

        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server running on http://{HOST}:{PORT}")
    server.serve_forever()
