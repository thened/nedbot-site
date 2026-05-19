"""
NedBotsSister blog + tickets API server.
Runs on port 5001 on the droplet.
nginx proxies /api/blog/* and /api/tickets/* to here.
Images served statically by nginx from /var/www/nedbot.site/sister/posts/images/.
"""
import os, sqlite3, re, datetime, secrets, hashlib
from pathlib import Path
from flask import Flask, request, jsonify, abort
from werkzeug.utils import secure_filename

API_KEY     = os.environ["BLOG_API_KEY"]
DB_PATH     = Path(os.environ.get("BLOG_DB_PATH", "/var/www/nedbot.site/sister/blog.db"))
IMG_DIR     = Path(os.environ.get("BLOG_IMG_DIR",  "/var/www/nedbot.site/sister/posts/images"))
ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp"}

app = Flask(__name__)
IMG_DIR.mkdir(parents=True, exist_ok=True)


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                slug       TEXT    UNIQUE NOT NULL,
                title      TEXT    NOT NULL,
                date       TEXT    NOT NULL,
                type       TEXT    NOT NULL DEFAULT 'post',
                image      TEXT,
                content    TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Migration: add type column if missing
        cols = [r[1] for r in db.execute("PRAGMA table_info(posts)").fetchall()]
        if "type" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN type TEXT NOT NULL DEFAULT 'post'")

        db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token       TEXT    UNIQUE NOT NULL,
                title       TEXT    NOT NULL,
                category    TEXT    NOT NULL DEFAULT 'bug',
                description TEXT    NOT NULL,
                reporter    TEXT    NOT NULL DEFAULT 'anonymous',
                kick_user   TEXT,
                status      TEXT    NOT NULL DEFAULT 'open',
                priority    TEXT    NOT NULL DEFAULT 'medium',
                admin_notes TEXT,
                ip_hash     TEXT,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        db.commit()

init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_key():
    key = request.headers.get("X-Blog-Key") or request.args.get("key")
    if key != API_KEY:
        abort(401)

def _is_admin():
    key = request.headers.get("X-Blog-Key") or request.args.get("key")
    return key == API_KEY


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

def _ip_hash():
    """Hash the remote IP so we can rate-limit without storing raw IPs."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    ip = ip.split(",")[0].strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]

def _rate_limit_ok(ip_hash: str, window_minutes: int = 30, max_tickets: int = 3) -> bool:
    """Return True if this IP is allowed to submit another ticket."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(minutes=window_minutes)).isoformat()
    with get_db() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM tickets WHERE ip_hash=? AND created_at>=?",
            (ip_hash, cutoff)
        ).fetchone()[0]
    return count < max_tickets

def _make_token() -> str:
    """Generate a short human-readable ticket token, e.g. NED-4F2A1C."""
    return "NED-" + secrets.token_hex(3).upper()

def _ticket_dict(row) -> dict:
    d = dict(row)
    d.pop("ip_hash", None)   # never expose hashed IP to callers
    return d


# ── Blog Routes ───────────────────────────────────────────────────────────────

@app.get("/api/blog/posts")
def list_posts():
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, title, date, type, image FROM posts WHERE type='post' ORDER BY date DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/blog/recipes")
def list_recipes():
    with get_db() as db:
        rows = db.execute(
            "SELECT slug, title, date, type, image FROM posts WHERE type='recipe' ORDER BY date DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/blog/posts/<slug>")
def get_post(slug):
    with get_db() as db:
        row = db.execute("SELECT * FROM posts WHERE slug = ?", (slug,)).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.post("/api/blog/posts")
def create_post():
    require_key()
    data    = request.json or {}
    title   = data.get("title",   "").strip()
    content = data.get("content", "").strip()
    date    = data.get("date")   or datetime.date.today().isoformat()
    type_   = data.get("type",   "post")
    image   = data.get("image")  or None
    slug    = data.get("slug")   or f"{date}-{slugify(title)}"

    if not title or not content:
        abort(400)

    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO posts (slug, title, date, type, image, content) VALUES (?,?,?,?,?,?)",
            (slug, title, date, type_, image, content)
        )
        db.commit()
    return jsonify({"slug": slug}), 201


@app.post("/api/blog/posts/<slug>/image")
def upload_image(slug):
    require_key()
    f = request.files.get("image")
    if not f:
        abort(400)
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        abort(400)
    filename = secure_filename(f"{slug}.{ext}")
    f.save(IMG_DIR / filename)
    with get_db() as db:
        db.execute("UPDATE posts SET image = ? WHERE slug = ?", (filename, slug))
        db.commit()
    return jsonify({"image": filename})


@app.delete("/api/blog/posts/<slug>")
def delete_post(slug):
    require_key()
    with get_db() as db:
        db.execute("DELETE FROM posts WHERE slug = ?", (slug,))
        db.commit()
    return jsonify({"deleted": slug})


# ── Ticket Routes ─────────────────────────────────────────────────────────────

VALID_CATEGORIES = {"bug", "feature", "question", "other"}
VALID_STATUSES   = {"open", "in_progress", "resolved", "closed", "wontfix"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}


@app.post("/api/tickets")
def submit_ticket():
    """Public — anyone can submit a ticket (rate-limited by IP)."""
    data = request.json or {}

    # Honeypot: bots fill this field, humans don't see it
    if data.get("_trap"):
        return jsonify({"token": _make_token()}), 201  # silent drop

    title       = (data.get("title")       or "").strip()[:120]
    category    = (data.get("category")    or "bug").strip().lower()
    description = (data.get("description") or "").strip()[:4000]
    reporter    = (data.get("reporter")    or "anonymous").strip()[:60]
    kick_user   = (data.get("kick_user")   or "").strip()[:60] or None

    if not title or not description:
        return jsonify({"error": "title and description are required"}), 400
    if len(title) < 5:
        return jsonify({"error": "title too short (5 chars min)"}), 400
    if len(description) < 10:
        return jsonify({"error": "description too short (10 chars min)"}), 400
    if category not in VALID_CATEGORIES:
        category = "other"

    ip_hash = _ip_hash()
    if not _rate_limit_ok(ip_hash):
        return jsonify({"error": "Too many tickets submitted recently. Please wait before submitting again."}), 429

    # Generate a unique token (retry on collision, extremely unlikely)
    for _ in range(5):
        token = _make_token()
        with get_db() as db:
            existing = db.execute("SELECT 1 FROM tickets WHERE token=?", (token,)).fetchone()
            if not existing:
                break

    with get_db() as db:
        db.execute(
            """INSERT INTO tickets
               (token, title, category, description, reporter, kick_user, ip_hash)
               VALUES (?,?,?,?,?,?,?)""",
            (token, title, category, description, reporter, kick_user, ip_hash)
        )
        db.commit()

    return jsonify({"token": token, "status": "open"}), 201


@app.get("/api/tickets/<token>")
def get_ticket(token):
    """Public — look up a ticket by its token. Admin gets full details incl. notes."""
    token = token.upper().strip()
    with get_db() as db:
        row = db.execute("SELECT * FROM tickets WHERE token=?", (token,)).fetchone()
    if not row:
        return jsonify({"error": "Ticket not found"}), 404

    d = _ticket_dict(row)
    if not _is_admin():
        # Public callers see everything except admin_notes
        d.pop("admin_notes", None)
    return jsonify(d)


@app.get("/api/tickets")
def list_tickets():
    """Admin only — list all tickets, optionally filtered by status/category."""
    require_key()
    status   = request.args.get("status")
    category = request.args.get("category")
    q        = "SELECT * FROM tickets"
    params   = []
    filters  = []
    if status:
        filters.append("status=?"); params.append(status)
    if category:
        filters.append("category=?"); params.append(category)
    if filters:
        q += " WHERE " + " AND ".join(filters)
    q += " ORDER BY created_at DESC"
    with get_db() as db:
        rows = db.execute(q, params).fetchall()
    return jsonify([_ticket_dict(r) for r in rows])


@app.patch("/api/tickets/<token>")
def update_ticket(token):
    """Admin only — update status, priority, or admin_notes."""
    require_key()
    token = token.upper().strip()
    data  = request.json or {}

    with get_db() as db:
        row = db.execute("SELECT id FROM tickets WHERE token=?", (token,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404

        allowed = {}
        if "status" in data and data["status"] in VALID_STATUSES:
            allowed["status"] = data["status"]
        if "priority" in data and data["priority"] in VALID_PRIORITIES:
            allowed["priority"] = data["priority"]
        if "admin_notes" in data:
            allowed["admin_notes"] = str(data["admin_notes"])[:2000]

        if not allowed:
            return jsonify({"error": "nothing to update"}), 400

        allowed["updated_at"] = datetime.datetime.utcnow().isoformat()
        sets   = ", ".join(f"{k}=?" for k in allowed)
        vals   = list(allowed.values()) + [token]
        db.execute(f"UPDATE tickets SET {sets} WHERE token=?", vals)
        db.commit()

    return jsonify({"token": token, "updated": list(allowed.keys())})


@app.delete("/api/tickets/<token>")
def delete_ticket(token):
    """Admin only — permanently delete a ticket."""
    require_key()
    token = token.upper().strip()
    with get_db() as db:
        db.execute("DELETE FROM tickets WHERE token=?", (token,))
        db.commit()
    return jsonify({"deleted": token})


@app.get("/api/tickets/stats")
def ticket_stats():
    """Admin only — counts by status and category."""
    require_key()
    with get_db() as db:
        by_status   = db.execute(
            "SELECT status, COUNT(*) as n FROM tickets GROUP BY status"
        ).fetchall()
        by_category = db.execute(
            "SELECT category, COUNT(*) as n FROM tickets GROUP BY category"
        ).fetchall()
        total = db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    return jsonify({
        "total":       total,
        "by_status":   {r["status"]:   r["n"] for r in by_status},
        "by_category": {r["category"]: r["n"] for r in by_category},
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
