"""
NedBotsSister blog API server.
Runs on port 5001 on the droplet. nginx proxies /api/blog/* to here.
Images served statically by nginx from /var/www/nedbot.site/sister/posts/images/.
"""
import os, sqlite3, re, datetime
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
        db.commit()

init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

def require_key():
    key = request.headers.get("X-Blog-Key") or request.args.get("key")
    if key != API_KEY:
        abort(401)


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


# ── Routes ────────────────────────────────────────────────────────────────────

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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001)
