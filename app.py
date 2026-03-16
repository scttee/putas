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
    return hashlib.scrypt(password.encode(), salt=PASSWORD_SALT.encode(), n=2**14, r=8, p=1, dklen=32).hex()


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


def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc().isoformat(timespec="seconds"),))


def emoji_summary(row: sqlite3.Row) -> str:
    parts = []
    if row["topped"]:
        parts.append("⬆️")
    if row["bottomed"]:
        parts.append("⬇️")
    if row["sucked_mode"] == "give":
        parts.append("👄➡️")
    elif row["sucked_mode"] == "receive":
        parts.append("👄⬅️")
    elif row["sucked_mode"] == "both":
        parts.append("👄🔁")
    stars = "⭐" * int(row["rating"] or 0)
    if stars:
        parts.append(stars)
    return " ".join(parts) or "—"


def page(title: str, body: str) -> bytes:
    styles = (BASE_DIR / "static/styles.css").read_text(encoding="utf-8")
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title><style>{styles}</style></head><body><main class='container'>{body}</main></body></html>""".encode()


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
        <a class='btn secondary' href='/backup'>CSV/Backup</a>
        <form method='post' action='/logout'><button class='secondary' type='submit'>Logout</button></form>
      </div>
    </header>
    """


def render_login(error: str = "") -> bytes:
    return page(
        "Login",
        f"""
<section class='card narrow stack'>
  <h1>Private Tracker Login</h1>
  {'<p class="error">'+html.escape(error)+'</p>' if error else ''}
  <form method='post' action='/login'>
    <label>Username <input name='username' required></label>
    <label>Password <input type='password' name='password' required></label>
    <button type='submit'>Sign in</button>
  </form>
</section>
""",
    )


def render_dashboard(username: str, error: str = "", success: str = "") -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    totals = db.execute("SELECT COUNT(*) total, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, ROUND(AVG(rating),2) avg_rating FROM hookups").fetchone()
    popular_nat = db.execute("SELECT nationality, COUNT(*) total FROM hookups WHERE COALESCE(TRIM(nationality),'')<>'' GROUP BY nationality ORDER BY total DESC LIMIT 1").fetchone()
    by_sucked = db.execute("SELECT sucked_mode, COUNT(*) total FROM hookups GROUP BY sucked_mode").fetchall()
    per_user = db.execute("SELECT recorder, COUNT(*) total, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total, ROUND(AVG(rating),2) avg_rating FROM hookups GROUP BY recorder ORDER BY total DESC").fetchall()
    entries = db.execute("SELECT * FROM hookups ORDER BY meetup_date DESC, id DESC LIMIT 5").fetchall()
    db.close()

    sucked_map = {r["sucked_mode"]: r["total"] for r in by_sucked}
    user_rows = "".join(f"<tr><td>{html.escape(r['recorder'])}</td><td>{r['total']}</td><td>{r['topped']}</td><td>{r['bottomed']}</td><td>{r['sucked_total']}</td><td>{r['avg_rating'] or '-'}</td></tr>" for r in per_user) or "<tr><td colspan='6'>No entries yet.</td></tr>"

    cards = []
    for r in entries:
        orgy_tag = f"🎉 Orgy x{r['orgy_count']}" if r["encounter_type"] == "orgy" and r["orgy_count"] else "👥 Standard"
        group_line = f" · 🧷 {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        orgy_line = f"<p>📋 {html.escape(r['orgy_details'])}</p>" if r["orgy_details"] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r["notes"] else ""
        cards.append(
            f"<article class='entry stack-sm'><div class='row between'><strong>{html.escape(r['partner_names'])}</strong><span class='muted'>📅 {html.escape(r['meetup_date'])}</span></div><small>📍 {html.escape(r['location'] or 'No location')} · 🌍 {html.escape(r['nationality'] or 'Unknown')} · 👤 {html.escape(r['recorder'])}</small><small>{orgy_tag}{group_line} · {emoji_summary(r)}</small>{photo_link}{orgy_line}{notes_line}</article>"
        )
    entry_html = "".join(cards) or "<p>No entries yet.</p>"

    js = """
<script>
(function(){
  const mode = document.getElementById('encounter_type');
  const single = document.getElementById('single-fields');
  const group = document.getElementById('group-fields');
  function toggle(){
    const isGroup = mode.value === 'orgy';
    single.style.display = isGroup ? 'none' : 'grid';
    group.style.display = isGroup ? 'grid' : 'none';
  }
  mode.addEventListener('change', toggle);
  toggle();
})();
</script>
"""

    return page(
        "Dashboard",
        f"""
{nav(username)}
{'<p class="error">'+html.escape(error)+'</p>' if error else ''}
{'<p class="success">'+html.escape(success)+'</p>' if success else ''}

<section class='grid stats block-gap'>
  <article class='card stack-sm'><h3>All Encounters</h3><p class='big'>🔥 {totals['total']}</p></article>
  <article class='card stack-sm'><h3>Top / Bottom</h3><p class='big'>⬆️ {totals['topped']} · ⬇️ {totals['bottomed']}</p></article>
  <article class='card stack-sm'><h3>Avg Rating (5)</h3><p class='big'>⭐ {totals['avg_rating'] or '-'}</p></article>
  <article class='card stack-sm'><h3>Sucked</h3><p class='small'>Give 👄➡️ {sucked_map.get('give',0)}<br>Receive 👄⬅️ {sucked_map.get('receive',0)}<br>Both 👄🔁 {sucked_map.get('both',0)}</p></article>
  <article class='card stack-sm'><h3>Top Nationality</h3><p class='big'>🌍 {html.escape(popular_nat['nationality']) if popular_nat else '-'}</p></article>
</section>

<section class='card stack block-gap'>
  <div class='section-title'><h2>Individual summary</h2><a class='btn secondary' href='/people'>Open full view</a></div>
  <div class='table-wrap'><table><thead><tr><th>Person</th><th>Count</th><th>Top</th><th>Bottom</th><th>Suck*</th><th>Avg ⭐</th></tr></thead><tbody>{user_rows}</tbody></table></div>
  <p class='muted'>*Suck total aggregates give/receive/both.</p>
</section>

<section class='card stack block-gap'>
  <h2>Add new encounter</h2>
  <form method='post' action='/add' enctype='multipart/form-data' class='grid form-grid'>
    <label>Date <input type='date' name='meetup_date' required></label>
    <label>Location <input name='location'></label>
    <label>Encounter type
      <select id='encounter_type' name='encounter_type'>
        <option value='single'>Single</option>
        <option value='orgy'>Group / Orgy</option>
      </select>
    </label>
    <label>Photo upload <input type='file' name='photo' accept='image/*'></label>

    <div id='single-fields' class='wide grid form-grid'>
      <label>Person name <input name='single_name' placeholder='Alex'></label>
      <label>Nationality <input name='single_nationality' placeholder='e.g. Spanish'></label>
      <label>Rating (1-5) <input type='number' name='single_rating' min='1' max='5'></label>
      <fieldset class='wide checkbox-row'>
        <legend>Acts</legend>
        <label><input type='checkbox' name='single_topped'> Topped</label>
        <label><input type='checkbox' name='single_bottomed'> Bottomed</label>
        <label>Sucked
          <select name='single_sucked_mode'>
            <option value='none'>None</option><option value='give'>Give</option><option value='receive'>Receive</option><option value='both'>Both</option>
          </select>
        </label>
      </fieldset>
      <label class='wide'>Notes <textarea name='single_notes' rows='3'></textarea></label>
    </div>

    <div id='group-fields' class='wide stack' style='display:none'>
      <label>Group rows (1 line each):<textarea name='group_rows' rows='5' placeholder='Name|top|bottom|sucked|rating|nationality|notes&#10;Marco|1|0|both|5|Italian|hot'></textarea></label>
      <p class='muted'>Format: Name|top(0/1)|bottom(0/1)|sucked(none/give/receive/both)|rating(1-5)|nationality|notes</p>
      <label>Group details (optional) <textarea name='orgy_details' rows='2'></textarea></label>
    </div>

    <button type='submit'>Save encounter</button>
  </form>
</section>

<section class='card stack block-gap'><div class='section-title'><h2>Latest entries (last 5)</h2><a class='btn secondary' href='/person?name={html.escape(username)}'>My entries</a></div><div class='entries'>{entry_html}</div></section>
{js}
""",
    )


def render_people(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    users = db.execute("SELECT recorder, COUNT(*) total, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total, ROUND(AVG(rating),2) avg_rating FROM hookups GROUP BY recorder ORDER BY recorder").fetchall()
    db.close()
    cards = "".join(f"<a class='card link-card stack-sm' href='/person?name={html.escape(u['recorder'])}'><h3>👤 {html.escape(u['recorder'])}</h3><p>{u['total']} entries · ⬆️ {u['topped']} · ⬇️ {u['bottomed']} · 👄 {u['sucked_total']} · ⭐ {u['avg_rating'] or '-'}</p></a>" for u in users) or "<p>No entries yet.</p>"
    return page("By person", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>By person</h2><span class='muted'>Tap a card for full list</span></div><section class='grid stats'>{cards}</section></section>")


def render_person_list(username: str, person: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM hookups WHERE recorder = ? ORDER BY meetup_date DESC, id DESC", (person,)).fetchall()
    db.close()
    items = []
    for r in rows:
        orgy_tag = f"🎉 Orgy x{r['orgy_count']}" if r["encounter_type"] == "orgy" and r["orgy_count"] else "👥 Standard"
        group_line = f" · 🧷 {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        orgy_line = f"<p>📋 {html.escape(r['orgy_details'])}</p>" if r['orgy_details'] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r['notes'] else ""
        items.append(f"<article class='entry stack-sm'><strong>{html.escape(r['partner_names'])}</strong><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small><small>🌍 {html.escape(r['nationality'] or 'Unknown')} · {orgy_tag}{group_line} · {emoji_summary(r)}</small>{photo_link}{orgy_line}{notes_line}</article>")
    return page("Person list", f"{nav(username)}<section class='card stack block-gap'><h2>Entries for {html.escape(person)}</h2><div class='entries'>{''.join(items) or '<p>No entries yet.</p>'}</div></section>")


def render_gallery(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, partner_names, recorder, meetup_date, photo_path, location FROM hookups WHERE COALESCE(photo_path,'') <> '' ORDER BY meetup_date DESC, id DESC").fetchall()
    db.close()
    tiles = "".join(
        f"<article class='card stack-sm'><a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'><img class='thumb' loading='lazy' decoding='async' src='/uploads/{html.escape(r['photo_path'])}' alt='photo {r['id']}'></a><small>👤 {html.escape(r['partner_names'])} · {html.escape(r['recorder'])}</small><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small></article>"
        for r in rows
    ) or "<p>No uploaded photos yet.</p>"
    return page("Gallery", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>Photo gallery</h2><span class='muted'>{len(rows)} photos</span></div><div class='gallery-grid'>{tiles}</div></section>")


def render_backup(username: str, error: str = "", success: str = "") -> bytes:
    return page(
        "CSV / Backup",
        f"""
        {nav(username)}
        {'<p class="error">'+html.escape(error)+'</p>' if error else ''}
        {'<p class="success">'+html.escape(success)+'</p>' if success else ''}
        <section class='card stack block-gap'>
          <h2>CSV Backup</h2>
          <p class='muted'>Use export regularly. You can upload a previous export to restore/append rows.</p>
          <a class='btn secondary' href='/export.csv'>Download backup CSV</a>
          <form method='post' action='/import.csv' enctype='multipart/form-data' class='stack-sm'>
            <label>Upload backup CSV <input type='file' name='backup_csv' accept='.csv,text/csv' required></label>
            <button type='submit'>Import backup</button>
          </form>
        </section>
        """,
    )


def csv_export() -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, recorder, partner_names, meetup_date, location, nationality, photo_path, encounter_type, orgy_count, orgy_details, encounter_group_id, topped, bottomed, sucked_mode, rating, notes, created_at FROM hookups ORDER BY meetup_date DESC").fetchall()
    db.close()
    out = StringIO()
    w = csv.writer(out)
    w.writerow(["id","recorder","partner_names","meetup_date","location","nationality","photo_path","encounter_type","orgy_count","orgy_details","encounter_group_id","topped","bottomed","sucked_mode","rating","notes","created_at"])
    for r in rows:
        w.writerow([r[k] for k in r.keys()])
    return out.getvalue().encode()


def import_csv_bytes(content: bytes) -> int:
    reader = csv.DictReader(StringIO(content.decode("utf-8", errors="replace")))
    if not reader.fieldnames or not {"recorder", "partner_names", "meetup_date"}.issubset(set(reader.fieldnames)):
        raise ValueError("Invalid CSV format")
    inserted = 0
    db = sqlite3.connect(DB_PATH)
    for row in reader:
        try:
            meetup_date = date.fromisoformat((row.get("meetup_date") or "").strip()).isoformat()
            recorder = (row.get("recorder") or "").strip()
            partner = (row.get("partner_names") or "").strip()
            if not recorder or not partner:
                continue
            sucked = (row.get("sucked_mode") or "none").strip()
            if sucked not in SUCKED_OPTIONS:
                sucked = "none"
            encounter_type = (row.get("encounter_type") or "single").strip()
            if encounter_type not in ENCOUNTER_OPTIONS:
                encounter_type = "single"
            rating = int((row.get("rating") or "").strip()) if (row.get("rating") or "").strip().isdigit() else None
            if rating is not None and not (1 <= rating <= 5):
                rating = None
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
                    partner,
                    meetup_date,
                    (row.get("location") or "").strip(),
                    (row.get("nationality") or "").strip(),
                    (row.get("photo_path") or "").strip() or None,
                    encounter_type,
                    int((row.get("orgy_count") or "").strip()) if (row.get("orgy_count") or "").strip().isdigit() else None,
                    (row.get("orgy_details") or "").strip(),
                    (row.get("encounter_group_id") or "").strip() or None,
                    1 if str(row.get("topped", "0")).strip() in {"1", "true", "True"} else 0,
                    1 if str(row.get("bottomed", "0")).strip() in {"1", "true", "True"} else 0,
                    sucked,
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
    msg = BytesParser(policy=default).parsebytes(f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
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


def parse_group_rows(raw: str) -> list[dict[str, str]]:
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        while len(parts) < 7:
            parts.append("")
        rows.append({
            "name": parts[0],
            "topped": parts[1],
            "bottomed": parts[2],
            "sucked_mode": parts[3],
            "rating": parts[4],
            "nationality": parts[5],
            "notes": parts[6],
        })
    return rows


class Handler(BaseHTTPRequestHandler):
    def current_user(self) -> str | None:
        if CF_ACCESS_EMAILS:
            cf_email = self.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
            return cf_email.split("@", 1)[0] if cf_email in CF_ACCESS_EMAILS else None
        sid = cookies.SimpleCookie(self.headers.get("Cookie", "")).get("sid")
        if not sid:
            return None
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        cleanup_expired_sessions(db)
        row = db.execute("SELECT username FROM sessions WHERE sid = ?", (sid.value,)).fetchone()
        db.commit(); db.close()
        return row["username"] if row else None

    def form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(payload).items()}

    def send_html(self, content: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)

    def send_csv(self, content: bytes, filename: str = "hookups.csv") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_html(page("404", "<section class='card'><h1>Not found</h1></section>"), 404)
            return
        content = path.read_bytes()
        mime = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers(); self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(303); self.send_header("Location", location); self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()
        query = parse_qs(urlparse(self.path).query)
        if path == "/":
            if not user: self.redirect("/login"); return
            self.send_html(render_dashboard(user)); return
        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Access required", "<section class='card'><h1>Access required</h1></section>"), 401); return
            self.send_html(render_login()); return
        if path == "/people":
            if not user: self.redirect("/login"); return
            self.send_html(render_people(user)); return
        if path == "/person":
            if not user: self.redirect("/login"); return
            self.send_html(render_person_list(user, query.get("name", [user])[0])); return
        if path == "/gallery":
            if not user: self.redirect("/login"); return
            self.send_html(render_gallery(user)); return
        if path == "/backup":
            if not user: self.redirect("/login"); return
            self.send_html(render_backup(user)); return
        if path == "/export.csv":
            if not user: self.redirect("/login"); return
            self.send_csv(csv_export()); return
        if path.startswith("/uploads/"):
            if not user: self.redirect("/login"); return
            self.send_file(UPLOAD_DIR / Path(path.removeprefix('/uploads/')).name); return
        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()

        if path == "/login":
            if CF_ACCESS_EMAILS:
                self.send_html(page("Forbidden", "<section class='card'><h1>Forbidden</h1></section>"), 403); return
            data = self.form_data()
            username = data.get("username", "").strip()
            expected = configured_users().get(username)
            if expected and verify_password(data.get("password", ""), expected):
                sid = secrets.token_urlsafe(24)
                expires = datetime.fromtimestamp(now_utc().timestamp() + SESSION_TTL_HOURS * 3600, tz=timezone.utc).isoformat(timespec="seconds")
                db = sqlite3.connect(DB_PATH)
                cleanup_expired_sessions(db)
                db.execute("INSERT INTO sessions (sid, username, expires_at, created_at) VALUES (?, ?, ?, ?)", (sid, username, expires, now_utc().isoformat(timespec='seconds')))
                db.commit(); db.close()
                self.send_response(303); self.send_header("Location", "/"); self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/"); self.end_headers(); return
            self.send_html(render_login("Invalid credentials."), 401); return

        if path == "/logout":
            sid = cookies.SimpleCookie(self.headers.get("Cookie", "")).get("sid")
            if sid:
                db = sqlite3.connect(DB_PATH); db.execute("DELETE FROM sessions WHERE sid = ?", (sid.value,)); db.commit(); db.close()
            self.send_response(303); self.send_header("Location", "/login"); self.send_header("Set-Cookie", "sid=; Max-Age=0; Path=/"); self.end_headers(); return

        if path == "/add":
            if not user: self.redirect("/login"); return
            data, files = parse_multipart(self) if "multipart/form-data" in self.headers.get("Content-Type", "") else (self.form_data(), {})
            try:
                meetup_date = date.fromisoformat(data.get("meetup_date", "")).isoformat()
                encounter_type = data.get("encounter_type", "single")
                if encounter_type not in ENCOUNTER_OPTIONS:
                    encounter_type = "single"
                location = data.get("location", "").strip()
                group_details = data.get("orgy_details", "").strip()

                saved_photo = None
                photo = files.get("photo")
                if photo and photo[1]:
                    saved_photo = safe_filename(photo[0]); (UPLOAD_DIR / saved_photo).write_bytes(photo[1])

                rows_to_insert = []
                if encounter_type == "single":
                    name = data.get("single_name", "").strip()
                    if not name:
                        raise ValueError("Single mode needs a person name")
                    rating = int(data.get("single_rating", "").strip()) if data.get("single_rating", "").strip() else None
                    if rating is not None and not (1 <= rating <= 5):
                        raise ValueError("Rating must be between 1 and 5")
                    sucked = data.get("single_sucked_mode", "none")
                    if sucked not in SUCKED_OPTIONS:
                        sucked = "none"
                    rows_to_insert.append({
                        "partner": name,
                        "nationality": data.get("single_nationality", "").strip(),
                        "topped": 1 if data.get("single_topped") else 0,
                        "bottomed": 1 if data.get("single_bottomed") else 0,
                        "sucked": sucked,
                        "rating": rating,
                        "notes": data.get("single_notes", "").strip(),
                    })
                    orgy_count = None
                    group_id = None
                else:
                    parsed = parse_group_rows(data.get("group_rows", ""))
                    if len(parsed) < 2:
                        raise ValueError("Group mode needs at least 2 valid rows")
                    orgy_count = len(parsed)
                    group_id = secrets.token_hex(3).upper()
                    for row in parsed:
                        sucked = row["sucked_mode"] if row["sucked_mode"] in SUCKED_OPTIONS else "none"
                        rating = int(row["rating"]) if row["rating"].isdigit() else None
                        if rating is not None and not (1 <= rating <= 5):
                            rating = None
                        rows_to_insert.append({
                            "partner": row["name"],
                            "nationality": row["nationality"],
                            "topped": 1 if row["topped"] in {"1", "true", "True", "yes", "y"} else 0,
                            "bottomed": 1 if row["bottomed"] in {"1", "true", "True", "yes", "y"} else 0,
                            "sucked": sucked,
                            "rating": rating,
                            "notes": row["notes"],
                        })

                db = sqlite3.connect(DB_PATH)
                for row in rows_to_insert:
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
                            row["partner"],
                            meetup_date,
                            location,
                            row["nationality"],
                            saved_photo,
                            encounter_type,
                            orgy_count,
                            group_details,
                            group_id,
                            row["topped"],
                            row["bottomed"],
                            row["sucked"],
                            row["rating"],
                            row["notes"],
                            now_utc().isoformat(timespec="seconds"),
                        ),
                    )
                db.commit(); db.close()
                self.send_html(render_dashboard(user, success=f"Saved {len(rows_to_insert)} individual entr{'y' if len(rows_to_insert)==1 else 'ies'} ✅"), 201)
                return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Could not save entry: {exc}"), 400)
                return

        if path == "/import.csv":
            if not user: self.redirect('/login'); return
            if "multipart/form-data" not in self.headers.get("Content-Type", ""):
                self.send_html(render_backup(user, error="Please upload a CSV file."), 400); return
            _, files = parse_multipart(self)
            backup = files.get("backup_csv")
            if not backup or not backup[1]:
                self.send_html(render_backup(user, error="Backup file missing."), 400); return
            try:
                count = import_csv_bytes(backup[1])
                self.send_html(render_backup(user, success=f"Imported {count} rows from backup ✅"), 201); return
            except Exception as exc:
                self.send_html(render_backup(user, error=f"Import failed: {exc}"), 400); return

        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server running on http://{HOST}:{PORT}")
    server.serve_forever()
