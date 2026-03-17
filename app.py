from __future__ import annotations

import csv
import hashlib
import hmac
import html
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
ENCOUNTER_OPTIONS = {"single", "group", "orgy", "cruising"}
MOOD_OPTIONS = {"amazing", "good", "mid", "regret"}


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
            protection_used TEXT,
            substances TEXT,
            repeat_partner INTEGER NOT NULL DEFAULT 0,
            load_taken INTEGER NOT NULL DEFAULT 0,
            mood TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entry_likes (
            hookup_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(hookup_id, username)
        );

        CREATE TABLE IF NOT EXISTS entry_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hookup_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS std_checks (
            username TEXT PRIMARY KEY,
            checked_on TEXT NOT NULL,
            notes TEXT,
            updated_at TEXT NOT NULL
        );
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
        "protection_used": "ALTER TABLE hookups ADD COLUMN protection_used TEXT",
        "substances": "ALTER TABLE hookups ADD COLUMN substances TEXT",
        "repeat_partner": "ALTER TABLE hookups ADD COLUMN repeat_partner INTEGER NOT NULL DEFAULT 0",
        "load_taken": "ALTER TABLE hookups ADD COLUMN load_taken INTEGER NOT NULL DEFAULT 0",
        "mood": "ALTER TABLE hookups ADD COLUMN mood TEXT",
    }
    for k, sql in migration.items():
        if k not in cols:
            db.execute(sql)


def cleanup_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc().isoformat(timespec="seconds"),))


def emoji_summary(row: sqlite3.Row) -> str:
    out = []
    if row["topped"]:
        out.append("⬆️")
    if row["bottomed"]:
        out.append("⬇️")
    suck = row["sucked_mode"]
    if suck == "give":
        out.append("👄➡️")
    elif suck == "receive":
        out.append("👄⬅️")
    elif suck == "both":
        out.append("👄🔁")
    if row["repeat_partner"]:
        out.append("🔁")
    if row["load_taken"]:
        out.append("💦")
    stars = "⭐" * int(row["rating"] or 0)
    if stars:
        out.append(stars)
    return " ".join(out) or "—"


def encounter_label(row: sqlite3.Row) -> str:
    kind = (row["encounter_type"] or "single").lower()
    if kind == "group":
        return f"👥 Group x{row['orgy_count']}" if row["orgy_count"] else "👥 Group"
    if kind == "orgy":
        return f"🎉 Orgy x{row['orgy_count']}" if row["orgy_count"] else "🎉 Orgy"
    if kind == "cruising":
        return "🚶 Cruising"
    return "👤 Single"


def std_due_message(checked_on: str) -> str:
    try:
        check_date = date.fromisoformat(checked_on)
    except Exception:
        return "Unknown"
    due = check_date + timedelta(days=90)
    days = (due - now_utc().date()).days
    if days < 0:
        return f"⚠️ Overdue by {abs(days)} day(s)"
    if days == 0:
        return "⏰ Due today"
    return f"✅ {days} day(s) left"


def page(title: str, body: str) -> bytes:
    styles = (BASE_DIR / "static/styles.css").read_text(encoding="utf-8")
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{title}</title><style>{styles}</style></head><body><main class='container fade-in'>{body}</main></body></html>""".encode()


def nav(username: str) -> str:
    return f"""
    <header class='header glass'>
      <div class='brand-row'>
        <h1>🔥 Hookup Tracker</h1>
        <span class='pill'>👤 {html.escape(username)}</span>
      </div>
      <details class='menu'>
        <summary class='menu-trigger'>☰ Menu</summary>
        <nav class='menu-panel'>
          <a class='menu-link' href='/'>🏠 Dashboard</a>
          <a class='menu-link' href='/person?name={html.escape(username)}'>🧾 My list</a>
          <a class='menu-link' href='/people'>👥 By person</a>
          <a class='menu-link' href='/health'>🧪 Health</a>
          <a class='menu-link' href='/gallery'>🖼️ Gallery</a>
          <a class='menu-link' href='/backup'>💾 CSV/Backup</a>
          <form method='post' action='/logout'><button class='menu-logout' type='submit'>🚪 Logout</button></form>
        </nav>
      </details>
      <div class='quick-links'>
        <a class='chip-link' href='/'>Dashboard</a>
        <a class='chip-link' href='/person?name={html.escape(username)}'>My list</a>
        <a class='chip-link' href='/people'>By person</a>
      </div>
    </header>
    """


def render_login(error: str = "") -> bytes:
    return page(
        "Login",
        f"""
<section class='card narrow stack floaty'>
  <h1>Private Tracker Login</h1>
  <p class='muted'>Two users only. Keep it messy, not public 😈</p>
  {'<p class="error">'+html.escape(error)+'</p>' if error else ''}
  <form method='post' action='/login'>
    <label>Username <input name='username' required></label>
    <label>Password <input type='password' name='password' required></label>
    <button type='submit'>Sign in</button>
  </form>
</section>
""",
    )


def dashboard_script() -> str:
    return """
<script>
(function(){
  const mode = document.getElementById('encounter_type');
  const single = document.getElementById('single-fields');
  const group = document.getElementById('group-fields');
  const groupCount = document.getElementById('group_count');
  const groupHost = document.getElementById('group-person-forms');

  function personCard(i){
    return `
    <article class="card mini-card">
      <h4>Person ${i+1}</h4>
      <div class="grid form-grid">
        <label>Name <input name="gp_${i}_name" required></label>
        <label>Nationality <input name="gp_${i}_nationality"></label>
        <label>Rating (1-5) <input type="number" name="gp_${i}_rating" min="1" max="5"></label>
        <label>Sucked
          <select name="gp_${i}_sucked_mode">
            <option value="none">None</option>
            <option value="give">Give</option>
            <option value="receive">Receive</option>
            <option value="both">Both</option>
          </select>
        </label>
        <fieldset class="wide checkbox-row">
          <legend>Acts</legend>
          <label><input type="checkbox" name="gp_${i}_topped"> Topped</label>
          <label><input type="checkbox" name="gp_${i}_bottomed"> Bottomed</label>
          <label><input type="checkbox" name="gp_${i}_repeat"> Repeat partner</label>
          <label><input type="checkbox" name="gp_${i}_load_taken"> Load taken</label>
        </fieldset>
        <label class="wide">Notes <textarea name="gp_${i}_notes" rows="2"></textarea></label>
      </div>
    </article>`;
  }

  function renderGroupForms(){
    const count = Math.max(2, Math.min(12, parseInt(groupCount.value || '2', 10)));
    groupHost.innerHTML = Array.from({length: count}, (_, i) => personCard(i)).join('');
  }

  function toggle(){
    const isGroup = mode.value === 'orgy' || mode.value === 'group' || mode.value === 'cruising';
    single.style.display = isGroup ? 'none' : 'grid';
    group.style.display = isGroup ? 'grid' : 'none';
    if (isGroup) renderGroupForms();
  }

  mode.addEventListener('change', toggle);
  groupCount.addEventListener('input', renderGroupForms);
  toggle();
})();
</script>
"""


def render_dashboard(username: str, error: str = "", success: str = "") -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    totals = db.execute("SELECT COUNT(*) total_all, COALESCE(SUM(CASE WHEN repeat_partner = 0 THEN 1 ELSE 0 END),0) total_new, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, ROUND(AVG(rating),2) avg_rating FROM hookups").fetchone()
    popular_nat = db.execute("SELECT nationality, COUNT(*) total FROM hookups WHERE COALESCE(TRIM(nationality),'')<>'' GROUP BY nationality ORDER BY total DESC, nationality ASC LIMIT 1").fetchone()
    by_sucked = db.execute("SELECT sucked_mode, COUNT(*) total FROM hookups GROUP BY sucked_mode").fetchall()
    weekly = db.execute("SELECT COUNT(*) total_all, COALESCE(SUM(CASE WHEN repeat_partner = 0 THEN 1 ELSE 0 END),0) total_new, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(load_taken),0) loads_taken FROM hookups WHERE meetup_date >= date('now','-6 day')").fetchone()
    weekly_users = db.execute("SELECT recorder, COUNT(*) total FROM hookups WHERE meetup_date >= date('now','-6 day') GROUP BY recorder ORDER BY total DESC, recorder ASC").fetchall()
    per_user = db.execute("SELECT recorder, COALESCE(SUM(CASE WHEN repeat_partner = 0 THEN 1 ELSE 0 END),0) total, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total, COALESCE(SUM(load_taken),0) loads_taken, ROUND(AVG(rating),2) avg_rating FROM hookups GROUP BY recorder ORDER BY total DESC").fetchall()
    entries = db.execute("SELECT * FROM hookups ORDER BY meetup_date DESC, id DESC LIMIT 5").fetchall()
    entry_ids = [r['id'] for r in entries]
    likes = {}
    comments = {}
    if entry_ids:
        placeholders = ','.join('?' for _ in entry_ids)
        for lr in db.execute(f"SELECT hookup_id, COUNT(*) total FROM entry_likes WHERE hookup_id IN ({placeholders}) GROUP BY hookup_id", entry_ids):
            likes[lr['hookup_id']] = lr['total']
        for cr in db.execute(f"SELECT hookup_id, username, comment FROM entry_comments WHERE hookup_id IN ({placeholders}) ORDER BY id DESC", entry_ids):
            comments.setdefault(cr['hookup_id'], []).append((cr['username'], cr['comment']))
    db.close()

    sucked_map = {r["sucked_mode"]: r["total"] for r in by_sucked}
    user_rows = "".join(f"<tr><td>{html.escape(r['recorder'])}</td><td>{r['total']}</td><td>{r['topped']}</td><td>{r['bottomed']}</td><td>{r['sucked_total']}</td><td>{r['loads_taken']}</td><td>{r['avg_rating'] or '-'}</td></tr>" for r in per_user) or "<tr><td colspan='7'>No entries yet.</td></tr>"

    latest = []
    for r in entries:
        orgy_tag = encounter_label(r)
        group_line = f" · 🧷 {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        protection_line = f"<p>🛡️ {html.escape(r['protection_used'])}</p>" if r["protection_used"] else ""
        substances_line = f"<p>💊 {html.escape(r['substances'])}</p>" if r["substances"] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r["notes"] else ""
        repeat_badge = "<span class='pill'>🔁 Repeat</span>" if r['repeat_partner'] else ""
        like_count = likes.get(r['id'], 0)
        comment_rows = comments.get(r['id'], [])[:3]
        comments_html = "".join(
            f"<p class='muted'>💬 <strong>{html.escape(c_user)}</strong>: {html.escape(c_text)}</p>"
            for c_user, c_text in comment_rows
        )
        social_html = f"""
        <div class='entry-social stack-sm'>
          <div class='row'>
            <form method='post' action='/like' class='inline-form'>
              <input type='hidden' name='entry_id' value='{r['id']}'>
              <button type='submit' class='btn secondary'>❤️ Like ({like_count})</button>
            </form>
          </div>
          <form method='post' action='/comment' class='row comment-form'>
            <input type='hidden' name='entry_id' value='{r['id']}'>
            <input name='comment' maxlength='240' placeholder='Add a comment...'>
            <button type='submit' class='btn secondary'>Post</button>
          </form>
          <div class='comments'>{comments_html}</div>
        </div>
        """
        latest.append(
            f"<article class='entry stack-sm hover-up'><div class='row between'><strong>{html.escape(r['partner_names'])}</strong><span class='muted'>📅 {html.escape(r['meetup_date'])}</span></div><small>📍 {html.escape(r['location'] or 'No location')} · 🌍 {html.escape(r['nationality'] or 'Unknown')} · 👤 {html.escape(r['recorder'])}</small><small>{encounter_label(r)}{group_line} · {emoji_summary(r)} · {html.escape(r['mood'] or '—')}</small>{repeat_badge}{photo_link}{protection_line}{substances_line}{notes_line}{social_html}</article>"
        )

    weekly_user_line = " · ".join(f"{html.escape(r['recorder'])}: {r['total']}" for r in weekly_users) or "No logs this week"

    return page(
        "Dashboard",
        f"""
{nav(username)}
{'<p class="error">'+html.escape(error)+'</p>' if error else ''}
{'<p class="success">'+html.escape(success)+'</p>' if success else ''}

<section class='grid stats block-gap'>
  <article class='card stack-sm hover-up'><h3>New People</h3><p class='big'>🆕 {totals['total_new']}</p><small class='muted'>All logs: {totals['total_all']}</small></article>
  <article class='card stack-sm hover-up'><h3>Top / Bottom</h3><p class='big'>⬆️ {totals['topped']} · ⬇️ {totals['bottomed']}</p></article>
  <article class='card stack-sm hover-up'><h3>Avg Rating (5)</h3><p class='big'>⭐ {totals['avg_rating'] or '-'}</p></article>
  <article class='card stack-sm hover-up'><h3>Sucked</h3><p class='small'>Give 👄➡️ {sucked_map.get('give',0)}<br>Receive 👄⬅️ {sucked_map.get('receive',0)}<br>Both 👄🔁 {sucked_map.get('both',0)}</p></article>
  <article class='card stack-sm hover-up'><h3>Top Nationality</h3><p class='big'>🌍 {html.escape(popular_nat['nationality']) if popular_nat else '-'}</p></article>
</section>

<section class='card stack-sm block-gap'>
  <h3>📆 Weekly dashboard (last 7 days)</h3>
  <p class='big'>🆕 {weekly['total_new']} new · 📝 {weekly['total_all']} logs</p>
  <p>⬆️ {weekly['topped']} · ⬇️ {weekly['bottomed']} · 💦 {weekly['loads_taken']} loads taken</p>
  <p class='muted'>{weekly_user_line}</p>
</section>

<section class='card stack block-gap'>
  <div class='section-title'><h2>Individual summary</h2><a class='btn secondary' href='/people'>Open full view</a></div>
  <div class='table-wrap'><table><thead><tr><th>Person</th><th>Count</th><th>Top</th><th>Bottom</th><th>Suck*</th><th>Loads 💦</th><th>Avg ⭐</th></tr></thead><tbody>{user_rows}</tbody></table></div>
  <p class='muted'>*Suck total aggregates give/receive/both.</p>
</section>

<section class='card stack block-gap'>
  <h2>Add new encounter</h2>
  <form method='post' action='/add' enctype='multipart/form-data' class='grid form-grid'>
    <label>Date <input type='date' name='meetup_date' required></label>
    <label>Encounter type
      <select id='encounter_type' name='encounter_type'>
        <option value='single'>Single</option>
        <option value='group'>Group</option>
        <option value='orgy'>Orgy</option>
        <option value='cruising'>Cruising</option>
      </select>
    </label>
    <label>Location <input name='location'></label>
    <label>Photo upload <input type='file' name='photo' accept='image/*'></label>

    <div id='single-fields' class='wide grid form-grid'>
      <label>Person name <input name='single_name'></label>
      <label>Nationality <input name='single_nationality'></label>
      <label>Rating (1-5) <input type='number' name='single_rating' min='1' max='5'></label>
      <label>Mood
        <select name='single_mood'>
          <option value='amazing'>Amazing</option><option value='good'>Good</option><option value='mid'>Mid</option><option value='regret'>Regret</option>
        </select>
      </label>
      <label>Protection used <input name='single_protection' placeholder='Condom, prep, none'></label>
      <label>Substances <input name='single_substances' placeholder='Poppers, weed, none'></label>
      <fieldset class='wide checkbox-row'>
        <legend>Acts</legend>
        <label><input type='checkbox' name='single_topped'> Topped</label>
        <label><input type='checkbox' name='single_bottomed'> Bottomed</label>
        <label><input type='checkbox' name='single_repeat'> Repeat partner</label>
        <label><input type='checkbox' name='single_load_taken'> Load taken</label>
        <label>Sucked
          <select name='single_sucked_mode'>
            <option value='none'>None</option><option value='give'>Give</option><option value='receive'>Receive</option><option value='both'>Both</option>
          </select>
        </label>
      </fieldset>
      <label class='wide'>Notes <textarea name='single_notes' rows='3'></textarea></label>
    </div>

    <div id='group-fields' class='wide stack' style='display:none'>
      <label>How many people?
        <select id='group_count' name='group_count'>
          <option>2</option><option>3</option><option>4</option><option>5</option><option>6</option><option>7</option><option>8</option><option>9</option><option>10</option>
        </select>
      </label>
      <label>Group details (optional)<textarea name='orgy_details' rows='2' placeholder='Party context / venue details'></textarea></label>
      <div id='group-person-forms' class='stack'></div>
    </div>

    <button type='submit'>Save encounter</button>
  </form>
</section>

<section class='card stack block-gap'><div class='section-title'><h2>Latest entries (last 5)</h2><a class='btn secondary' href='/person?name={html.escape(username)}'>My entries</a></div><div class='entries'>{''.join(latest) or '<p>No entries yet.</p>'}</div></section>
{dashboard_script()}
""",
    )


def render_people(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    users = db.execute("SELECT recorder, COALESCE(SUM(CASE WHEN repeat_partner = 0 THEN 1 ELSE 0 END),0) total, COUNT(*) total_logs, COALESCE(SUM(CASE WHEN repeat_partner = 1 THEN 1 ELSE 0 END),0) repeats, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total, COALESCE(SUM(load_taken),0) loads_taken, ROUND(AVG(rating),2) avg_rating, MAX(meetup_date) last_date FROM hookups GROUP BY recorder ORDER BY recorder").fetchall()
    db.close()
    cards = "".join(
        f"<a class='card link-card person-showcase stack-sm hover-up' href='/person?name={html.escape(u['recorder'])}'><div class='row between'><h3>👤 {html.escape(u['recorder'])}</h3><span class='pill'>Last: {html.escape(u['last_date'] or '-')}</span></div><div class='mini-stats'><span>🆕 {u['total']}</span><span>🔁 {u['repeats']}</span><span>📝 {u['total_logs']}</span><span>💦 {u['loads_taken']}</span></div><p>⬆️ {u['topped']} · ⬇️ {u['bottomed']} · 👄 {u['sucked_total']} · ⭐ {u['avg_rating'] or '-'}</p><small class='muted'>Tap for full timeline + notes</small></a>"
        for u in users
    ) or "<p>No entries yet.</p>"
    return page("By person", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>By person</h2><span class='muted'>Tap a card for full list</span></div><p class='muted'>Quick snapshots for each of you ✨</p><section class='grid stats'>{cards}</section></section>")


def render_person_list(username: str, person: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    summary = db.execute("SELECT COALESCE(SUM(CASE WHEN repeat_partner = 0 THEN 1 ELSE 0 END),0) total_new, COUNT(*) total_logs, COALESCE(SUM(CASE WHEN repeat_partner = 1 THEN 1 ELSE 0 END),0) repeats, COALESCE(SUM(topped),0) topped, COALESCE(SUM(bottomed),0) bottomed, COALESCE(SUM(CASE WHEN sucked_mode <> 'none' THEN 1 ELSE 0 END),0) sucked_total, COALESCE(SUM(load_taken),0) loads_taken, ROUND(AVG(rating),2) avg_rating, MAX(meetup_date) last_date FROM hookups WHERE recorder = ?", (person,)).fetchone()
    rows = db.execute("SELECT * FROM hookups WHERE recorder = ? ORDER BY meetup_date DESC, id DESC", (person,)).fetchall()
    db.close()
    items = []
    for r in rows:
        orgy_tag = encounter_label(r)
        group_line = f" · 🧷 {html.escape(r['encounter_group_id'])}" if r["encounter_group_id"] else ""
        photo_link = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'>📸 View photo</a>" if r["photo_path"] else ""
        protection_line = f"<p>🛡️ {html.escape(r['protection_used'])}</p>" if r["protection_used"] else ""
        substances_line = f"<p>💊 {html.escape(r['substances'])}</p>" if r["substances"] else ""
        notes_line = f"<p>📝 {html.escape(r['notes'])}</p>" if r["notes"] else ""
        repeat_badge = "<span class='pill'>🔁 Repeat</span>" if r['repeat_partner'] else ""
        items.append(
            f"<article class='entry stack-sm'><strong>{html.escape(r['partner_names'])}</strong><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small><small>🌍 {html.escape(r['nationality'] or 'Unknown')} · {orgy_tag}{group_line} · {emoji_summary(r)}</small>{repeat_badge}{photo_link}{protection_line}{substances_line}{notes_line}</article>"
        )
    summary_cards = f"""
    <section class='grid stats block-gap'>
      <article class='card stack-sm'><h3>🆕 New people</h3><p class='big'>{summary['total_new']}</p><small class='muted'>All logs: {summary['total_logs']}</small></article>
      <article class='card stack-sm'><h3>🔁 Repeats</h3><p class='big'>{summary['repeats']}</p></article>
      <article class='card stack-sm'><h3>⬆️ / ⬇️ / 👄</h3><p class='big'>{summary['topped']} / {summary['bottomed']} / {summary['sucked_total']}</p></article>
      <article class='card stack-sm'><h3>💦 Loads taken</h3><p class='big'>{summary['loads_taken']}</p></article>
      <article class='card stack-sm'><h3>⭐ Avg rating</h3><p class='big'>{summary['avg_rating'] or '-'}</p><small class='muted'>Last: {html.escape(summary['last_date'] or '-')}</small></article>
    </section>
    """
    return page("Person list", f"{nav(username)}<section class='card stack block-gap'><h2>{html.escape(person)} summary</h2>{summary_cards}</section><section class='card stack block-gap'><h2>Entries for {html.escape(person)}</h2><div class='entries'>{''.join(items) or '<p>No entries yet.</p>'}</div></section>")


def render_gallery(username: str) -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, partner_names, recorder, meetup_date, photo_path, location FROM hookups WHERE COALESCE(photo_path,'') <> '' ORDER BY meetup_date DESC, id DESC").fetchall()
    db.close()
    tiles = []
    missing = 0
    for r in rows:
        file_exists = (UPLOAD_DIR / Path(r['photo_path']).name).exists() if r['photo_path'] else False
        if file_exists:
            preview = f"<a href='/uploads/{html.escape(r['photo_path'])}' target='_blank'><img class='thumb' loading='lazy' decoding='async' src='/uploads/{html.escape(r['photo_path'])}' alt='photo {r['id']}'></a>"
        else:
            missing += 1
            preview = "<div class='thumb missing-thumb'>⚠️ Missing file</div>"
        tiles.append(f"<article class='card stack-sm hover-up'>{preview}<small>👤 {html.escape(r['partner_names'])} · {html.escape(r['recorder'])}</small><small>📅 {html.escape(r['meetup_date'])} · 📍 {html.escape(r['location'] or 'No location')}</small></article>")
    warning = f"<p class='muted'>⚠️ {missing} photo(s) missing on disk. Use persistent storage on Railway to keep uploads across deploys.</p>" if missing else ""
    return page("Gallery", f"{nav(username)}<section class='card stack block-gap'><div class='section-title'><h2>Photo gallery</h2><span class='muted'>{len(rows)} photos</span></div>{warning}<div class='gallery-grid'>{''.join(tiles) or '<p>No uploaded photos yet.</p>'}</div></section>")


def render_backup(username: str, error: str = "", success: str = "") -> bytes:
    err = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    ok = f"<p class='success'>{html.escape(success)}</p>" if success else ""
    return page(
        "CSV / Backup",
        f"""
        {nav(username)}
        {err}
        {ok}
        <section class='card stack block-gap'>
          <h2>CSV Backup</h2>
          <p class='muted'>Download and upload CSV backups safely.</p>
          <a class='btn secondary' href='/export.csv'>Download backup CSV</a>
          <form method='post' action='/import.csv' enctype='multipart/form-data' class='stack-sm'>
            <label>Upload backup CSV <input type='file' name='backup_csv' accept='.csv,text/csv' required></label>
            <button type='submit'>Import backup</button>
          </form>
        </section>
        """,
    )


def render_health(username: str, error: str = "", success: str = "") -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    checks = {r["username"]: r for r in db.execute("SELECT username, checked_on, notes FROM std_checks ORDER BY username").fetchall()}
    db.close()
    users = list(configured_users().keys())
    rows = []
    for u in users:
        row = checks.get(u)
        checked_on = row["checked_on"] if row else "-"
        due = std_due_message(checked_on) if row else "No check logged"
        notes = html.escape(row["notes"] or "") if row else ""
        rows.append(f"<tr><td>{html.escape(u)}</td><td>{html.escape(checked_on)}</td><td>{html.escape(due)}</td><td>{notes or '-'}</td></tr>")
    err = f"<p class='error'>{html.escape(error)}</p>" if error else ""
    ok = f"<p class='success'>{html.escape(success)}</p>" if success else ""
    options = "".join(f"<option value='{html.escape(u)}' {'selected' if u == username else ''}>{html.escape(u)}</option>" for u in users)
    return page(
        "Health",
        f"""
        {nav(username)}
        {err}{ok}
        <section class='card stack block-gap'>
          <h2>🧪 STD checks</h2>
          <p class='muted'>Track last checks and countdown to your next recommended test (90 days).</p>
          <div class='table-wrap'><table><thead><tr><th>User</th><th>Last check</th><th>Due status</th><th>Notes</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        </section>
        <section class='card stack block-gap'>
          <h3>Log / update check</h3>
          <form method='post' action='/health' class='grid form-grid'>
            <label>User<select name='username'>{options}</select></label>
            <label>Check date<input type='date' name='checked_on' required></label>
            <label class='wide'>Notes<textarea name='notes' rows='2' placeholder='Clinic, panel type, reminders'></textarea></label>
            <button type='submit'>Save check</button>
          </form>
        </section>
        """,
    )


def csv_export() -> bytes:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, recorder, partner_names, meetup_date, location, nationality, photo_path, encounter_type, orgy_count, orgy_details, encounter_group_id, topped, bottomed, sucked_mode, rating, protection_used, substances, repeat_partner, load_taken, mood, notes, created_at FROM hookups ORDER BY meetup_date DESC").fetchall()
    db.close()
    out = StringIO(); w = csv.writer(out)
    w.writerow(["id","recorder","partner_names","meetup_date","location","nationality","photo_path","encounter_type","orgy_count","orgy_details","encounter_group_id","topped","bottomed","sucked_mode","rating","protection_used","substances","repeat_partner","load_taken","mood","notes","created_at"])
    for r in rows: w.writerow([r[k] for k in r.keys()])
    return out.getvalue().encode()


def import_csv_bytes(content: bytes) -> int:
    reader = csv.DictReader(StringIO(content.decode("utf-8", errors="replace")))
    if not reader.fieldnames or not {"recorder", "partner_names", "meetup_date"}.issubset(set(reader.fieldnames)):
        raise ValueError("Invalid CSV format")
    db = sqlite3.connect(DB_PATH); inserted = 0
    for row in reader:
        try:
            meetup_date = date.fromisoformat((row.get("meetup_date") or "").strip()).isoformat()
            recorder = (row.get("recorder") or "").strip(); partner = (row.get("partner_names") or "").strip()
            if not recorder or not partner: continue
            sucked = (row.get("sucked_mode") or "none").strip(); sucked = sucked if sucked in SUCKED_OPTIONS else "none"
            encounter = (row.get("encounter_type") or "single").strip(); encounter = encounter if encounter in ENCOUNTER_OPTIONS else "single"
            mood = (row.get("mood") or "").strip(); mood = mood if mood in MOOD_OPTIONS else None
            rating_raw = (row.get("rating") or "").strip(); rating = int(rating_raw) if rating_raw.isdigit() else None
            if rating is not None and not (1 <= rating <= 5): rating = None
            db.execute("""
                INSERT INTO hookups (
                  recorder, partner_names, meetup_date, location, nationality, photo_path,
                  encounter_type, orgy_count, orgy_details, encounter_group_id,
                  topped, bottomed, sucked_mode, rating, protection_used, substances, repeat_partner, load_taken, mood, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                recorder, partner, meetup_date,
                (row.get("location") or "").strip(),
                (row.get("nationality") or "").strip(),
                (row.get("photo_path") or "").strip() or None,
                encounter,
                int((row.get("orgy_count") or "").strip()) if (row.get("orgy_count") or "").strip().isdigit() else None,
                (row.get("orgy_details") or "").strip(),
                (row.get("encounter_group_id") or "").strip() or None,
                1 if str(row.get("topped","0")).strip() in {"1","true","True"} else 0,
                1 if str(row.get("bottomed","0")).strip() in {"1","true","True"} else 0,
                sucked,
                rating,
                (row.get("protection_used") or "").strip(),
                (row.get("substances") or "").strip(),
                1 if str(row.get("repeat_partner","0")).strip() in {"1","true","True"} else 0,
                1 if str(row.get("load_taken","0")).strip() in {"1","true","True"} else 0,
                mood,
                (row.get("notes") or "").strip(),
                now_utc().isoformat(timespec="seconds"),
            ))
            inserted += 1
        except Exception:
            continue
    db.commit(); db.close(); return inserted


def parse_multipart(handler: BaseHTTPRequestHandler) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    ctype = handler.headers.get("Content-Type", "")
    msg = BytesParser(policy=default).parsebytes(f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        if "form-data" not in part.get("Content-Disposition", ""): continue
        name = part.get_param("name", header="content-disposition")
        if not name: continue
        fn = part.get_filename(); payload = part.get_payload(decode=True) or b""
        if fn: files[name] = (fn, payload)
        else: fields[name] = payload.decode("utf-8", errors="ignore")
    return fields, files


def safe_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}: ext = ".jpg"
    return f"{secrets.token_hex(12)}{ext}"


class Handler(BaseHTTPRequestHandler):
    def current_user(self) -> str | None:
        if CF_ACCESS_EMAILS:
            email = self.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
            return email.split("@", 1)[0] if email in CF_ACCESS_EMAILS else None
        sid = cookies.SimpleCookie(self.headers.get("Cookie", "")).get("sid")
        if not sid: return None
        db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
        cleanup_expired_sessions(db)
        row = db.execute("SELECT username FROM sessions WHERE sid = ?", (sid.value,)).fetchone()
        db.commit(); db.close()
        return row["username"] if row else None

    def form_data(self) -> dict[str, str]:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(body).items()}

    def send_html(self, content: bytes, code: int = 200) -> None:
        self.send_response(code); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)

    def send_csv(self, content: bytes) -> None:
        self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", "attachment; filename=hookups.csv"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)

    def send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_html(page("404", "<section class='card'><h1>Not found</h1></section>"), 404); return
        c = path.read_bytes(); mime = {".png":"image/png", ".gif":"image/gif", ".webp":"image/webp"}.get(path.suffix.lower(), "image/jpeg")
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Cache-Control", "public, max-age=86400"); self.send_header("Content-Length", str(len(c))); self.end_headers(); self.wfile.write(c)

    def redirect(self, loc: str) -> None:
        self.send_response(303); self.send_header("Location", loc); self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        user = self.current_user()
        if path == "/":
            if not user: self.redirect('/login'); return
            self.send_html(render_dashboard(user)); return
        if path == "/login":
            if CF_ACCESS_EMAILS: self.send_html(page("Access required", "<section class='card'><h1>Access required</h1></section>"), 401); return
            self.send_html(render_login()); return
        if path == "/people":
            if not user: self.redirect('/login'); return
            self.send_html(render_people(user)); return
        if path == "/person":
            if not user: self.redirect('/login'); return
            self.send_html(render_person_list(user, query.get("name", [user])[0])); return
        if path == "/gallery":
            if not user: self.redirect('/login'); return
            self.send_html(render_gallery(user)); return
        if path == "/backup":
            if not user: self.redirect('/login'); return
            self.send_html(render_backup(user)); return
        if path == "/health":
            if not user: self.redirect('/login'); return
            self.send_html(render_health(user)); return
        if path == "/export.csv":
            if not user: self.redirect('/login'); return
            self.send_csv(csv_export()); return
        if path.startswith('/uploads/'):
            if not user: self.redirect('/login'); return
            self.send_file(UPLOAD_DIR / Path(path.removeprefix('/uploads/')).name); return
        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        user = self.current_user()

        if path == '/login':
            if CF_ACCESS_EMAILS: self.send_html(page("Forbidden", "<section class='card'><h1>Forbidden</h1></section>"), 403); return
            data = self.form_data(); username = data.get('username','').strip(); expected = configured_users().get(username)
            if expected and verify_password(data.get('password',''), expected):
                sid = secrets.token_urlsafe(24)
                exp = datetime.fromtimestamp(now_utc().timestamp()+SESSION_TTL_HOURS*3600, tz=timezone.utc).isoformat(timespec='seconds')
                db = sqlite3.connect(DB_PATH); cleanup_expired_sessions(db)
                db.execute("INSERT INTO sessions (sid, username, expires_at, created_at) VALUES (?, ?, ?, ?)", (sid, username, exp, now_utc().isoformat(timespec='seconds')))
                db.commit(); db.close()
                self.send_response(303); self.send_header('Location','/'); self.send_header('Set-Cookie', f'sid={sid}; HttpOnly; SameSite=Strict; Path=/'); self.end_headers(); return
            self.send_html(render_login("Invalid credentials."), 401); return

        if path == '/logout':
            sid = cookies.SimpleCookie(self.headers.get('Cookie','')).get('sid')
            if sid:
                db=sqlite3.connect(DB_PATH); db.execute("DELETE FROM sessions WHERE sid = ?", (sid.value,)); db.commit(); db.close()
            self.send_response(303); self.send_header('Location','/login'); self.send_header('Set-Cookie','sid=; Max-Age=0; Path=/'); self.end_headers(); return

        if path == '/add':
            if not user: self.redirect('/login'); return
            data, files = parse_multipart(self) if 'multipart/form-data' in self.headers.get('Content-Type','') else (self.form_data(), {})
            try:
                meetup_date = date.fromisoformat(data.get('meetup_date','')).isoformat()
                encounter = data.get('encounter_type','single')
                if encounter not in ENCOUNTER_OPTIONS: encounter='single'
                location = data.get('location','').strip()
                saved_photo = None
                if files.get('photo') and files['photo'][1]:
                    saved_photo = safe_filename(files['photo'][0]); (UPLOAD_DIR / saved_photo).write_bytes(files['photo'][1])

                rows = []
                group_id = None
                orgy_count = None
                orgy_details = data.get('orgy_details','').strip()

                if encounter == 'single':
                    name = data.get('single_name','').strip()
                    if not name: raise ValueError('Single mode needs a name')
                    rating = int(data.get('single_rating','').strip()) if data.get('single_rating','').strip() else None
                    if rating is not None and not (1 <= rating <= 5): raise ValueError('Rating must be 1-5')
                    sucked = data.get('single_sucked_mode','none'); sucked = sucked if sucked in SUCKED_OPTIONS else 'none'
                    mood = data.get('single_mood','').strip(); mood = mood if mood in MOOD_OPTIONS else None
                    rows.append({
                        'partner': name,
                        'nationality': data.get('single_nationality','').strip(),
                        'topped': 1 if data.get('single_topped') else 0,
                        'bottomed': 1 if data.get('single_bottomed') else 0,
                        'sucked': sucked,
                        'rating': rating,
                        'notes': data.get('single_notes','').strip(),
                        'protection': data.get('single_protection','').strip(),
                        'substances': data.get('single_substances','').strip(),
                        'repeat': 1 if data.get('single_repeat') else 0,
                        'load_taken': 1 if data.get('single_load_taken') else 0,
                        'mood': mood,
                    })
                else:
                    count = int(data.get('group_count','2'))
                    count = max(2, min(12, count))
                    group_id = secrets.token_hex(3).upper()
                    orgy_count = count
                    for i in range(count):
                        name = data.get(f'gp_{i}_name','').strip()
                        if not name: raise ValueError(f'Group person {i+1} name is required')
                        sucked = data.get(f'gp_{i}_sucked_mode','none').strip(); sucked = sucked if sucked in SUCKED_OPTIONS else 'none'
                        rating_raw = data.get(f'gp_{i}_rating','').strip(); rating = int(rating_raw) if rating_raw else None
                        if rating is not None and not (1 <= rating <= 5): raise ValueError('Group rating must be 1-5')
                        rows.append({
                            'partner': name,
                            'nationality': data.get(f'gp_{i}_nationality','').strip(),
                            'topped': 1 if data.get(f'gp_{i}_topped') else 0,
                            'bottomed': 1 if data.get(f'gp_{i}_bottomed') else 0,
                            'sucked': sucked,
                            'rating': rating,
                            'notes': data.get(f'gp_{i}_notes','').strip(),
                            'protection': '',
                            'substances': '',
                            'repeat': 1 if data.get(f'gp_{i}_repeat') else 0,
                            'load_taken': 1 if data.get(f'gp_{i}_load_taken') else 0,
                            'mood': None,
                        })

                db = sqlite3.connect(DB_PATH)
                for r in rows:
                    db.execute("""
                        INSERT INTO hookups (
                          recorder, partner_names, meetup_date, location, nationality, photo_path,
                          encounter_type, orgy_count, orgy_details, encounter_group_id,
                          topped, bottomed, sucked_mode, rating, protection_used, substances, repeat_partner, load_taken, mood, notes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        user, r['partner'], meetup_date, location, r['nationality'], saved_photo,
                        encounter, orgy_count, orgy_details, group_id,
                        r['topped'], r['bottomed'], r['sucked'], r['rating'], r['protection'], r['substances'], r['repeat'], r['load_taken'], r['mood'], r['notes'], now_utc().isoformat(timespec='seconds')
                    ))
                db.commit(); db.close()
                self.send_html(render_dashboard(user, success=f"Saved {len(rows)} individual entr{'y' if len(rows)==1 else 'ies'} ✅"), 201); return
            except Exception as exc:
                self.send_html(render_dashboard(user, error=f"Could not save entry: {exc}"), 400); return


        if path == '/like':
            if not user: self.redirect('/login'); return
            data = self.form_data()
            entry_id = (data.get('entry_id') or '').strip()
            if not entry_id.isdigit():
                self.send_html(render_dashboard(user, error='Invalid entry id'), 400); return
            db = sqlite3.connect(DB_PATH)
            found = db.execute("SELECT 1 FROM hookups WHERE id=?", (int(entry_id),)).fetchone()
            if not found:
                db.close()
                self.send_html(render_dashboard(user, error='Entry not found'), 404); return
            db.execute("INSERT OR IGNORE INTO entry_likes (hookup_id, username, created_at) VALUES (?, ?, ?)", (int(entry_id), user, now_utc().isoformat(timespec='seconds')))
            db.commit(); db.close()
            self.send_html(render_dashboard(user, success='Liked ❤️'), 201); return

        if path == '/comment':
            if not user: self.redirect('/login'); return
            data = self.form_data()
            entry_id = (data.get('entry_id') or '').strip()
            text = (data.get('comment') or '').strip()
            if not entry_id.isdigit() or not text:
                self.send_html(render_dashboard(user, error='Comment requires entry and text'), 400); return
            db = sqlite3.connect(DB_PATH)
            found = db.execute("SELECT 1 FROM hookups WHERE id=?", (int(entry_id),)).fetchone()
            if not found:
                db.close()
                self.send_html(render_dashboard(user, error='Entry not found'), 404); return
            db.execute("INSERT INTO entry_comments (hookup_id, username, comment, created_at) VALUES (?, ?, ?, ?)", (int(entry_id), user, text[:240], now_utc().isoformat(timespec='seconds')))
            db.commit(); db.close()
            self.send_html(render_dashboard(user, success='Comment added 💬'), 201); return

        if path == '/health':
            if not user: self.redirect('/login'); return
            data = self.form_data()
            target_user = (data.get('username') or '').strip()
            checked_on = (data.get('checked_on') or '').strip()
            notes = (data.get('notes') or '').strip()
            allowed_users = set(configured_users().keys())
            if target_user not in allowed_users:
                self.send_html(render_health(user, error='Invalid user.'), 400); return
            try:
                date.fromisoformat(checked_on)
            except Exception:
                self.send_html(render_health(user, error='Invalid date format.'), 400); return
            db = sqlite3.connect(DB_PATH)
            db.execute(
                """
                INSERT INTO std_checks (username, checked_on, notes, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET checked_on=excluded.checked_on, notes=excluded.notes, updated_at=excluded.updated_at
                """,
                (target_user, checked_on, notes[:300], now_utc().isoformat(timespec='seconds')),
            )
            db.commit(); db.close()
            self.send_html(render_health(user, success='STD check saved ✅'), 201); return

        if path == '/import.csv':
            if not user: self.redirect('/login'); return
            if 'multipart/form-data' not in self.headers.get('Content-Type',''):
                self.send_html(render_backup(user, error='Please upload a CSV file.'), 400); return
            _, files = parse_multipart(self)
            backup = files.get('backup_csv')
            if not backup or not backup[1]:
                self.send_html(render_backup(user, error='Backup file missing.'), 400); return
            try:
                count = import_csv_bytes(backup[1])
                self.send_html(render_backup(user, success=f"Imported {count} rows from backup ✅"), 201); return
            except Exception as exc:
                self.send_html(render_backup(user, error=f"Import failed: {exc}"), 400); return

        self.send_html(page("404", "<section class='card'><h1>404</h1></section>"), 404)


if __name__ == '__main__':
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Server running on http://{HOST}:{PORT}')
    server.serve_forever()
