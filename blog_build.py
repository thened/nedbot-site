#!/usr/bin/env python3
"""
Static site builder for sister.nedbot.site
Reads blog.db, writes HTML to the sister/ tree.
Run after any content change.

  python3 blog_build.py
"""

import sqlite3, os, re
from pathlib import Path
from datetime import datetime
import markdown as mdlib

DB_PATH = Path(os.environ.get("BLOG_DB_PATH", "/var/www/nedbot.site/sister/blog.db"))
OUT_DIR = Path(os.environ.get("BLOG_OUT_DIR", "/var/www/nedbot.site/sister"))
SITE    = "https://sister.nedbot.site"


# ── Markdown ───────────────────────────────────────────────────────────────────

def render_md(text):
    return mdlib.markdown(text, extensions=["extra", "nl2br"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%-d %B %Y")

def fmt_date_long(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%A, %-d %B %Y")

def peek(content, chars=160):
    """Plain-text excerpt from markdown content."""
    text = re.sub(r'#+ ', '', content)
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'---+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:chars].rsplit(' ', 1)[0] + '…' if len(text) > chars else text

def post_url(row):
    y, m, d = row['date'].split('-')
    return f"{SITE}/{y}/{m}/{d}/{row['slug']}/"

def recipe_url(row):
    y, m, d = row['date'].split('-')
    return f"{SITE}/recipes/{y}/{m}/{d}/{row['slug']}/"

def post_out(row):
    y, m, d = row['date'].split('-')
    return OUT_DIR / y / m / d / row['slug'] / "index.html"

def recipe_out(row):
    y, m, d = row['date'].split('-')
    return OUT_DIR / "recipes" / y / m / d / row['slug'] / "index.html"

def img_url(row, kind):
    if row['image']:
        return f"{SITE}/assets/{kind}s/{row['image']}"
    return None


# ── CSS ────────────────────────────────────────────────────────────────────────

CSS = """
  :root {
    --purple:     #7b5ea7;
    --purple-lt:  #b09fd0;
    --purple-dk:  #4a326b;
    --purple-mid: #6548a0;
    --blush:      #e8b4c8;
    --paper:      #f8f4ff;
    --paper-dk:   #ede5f8;
    --ink:        #1a1020;
    --ink-lt:     #3a2850;
    --cream:      #fdf9ff;
    --rule:       #c8b8e0;
    --serif:      'Georgia', 'Times New Roman', serif;
    --sans:       'Helvetica Neue', Arial, sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0 }

  body {
    background: var(--purple-dk);
    color: var(--ink);
    font-family: var(--sans);
    min-height: 100vh;
  }

  body::before {
    content: '';
    position: fixed; inset: 0;
    background-image: repeating-linear-gradient(
      0deg, transparent, transparent 24px,
      rgba(0,0,0,0.04) 24px, rgba(0,0,0,0.04) 25px
    );
    pointer-events: none; z-index: 0;
  }

  /* Nav */
  nav {
    position: relative; z-index: 10;
    background: var(--ink); border-bottom: 3px solid var(--purple);
    padding: 0 32px; display: flex; align-items: center;
    justify-content: space-between; height: 56px;
  }
  .nav-logo {
    font-family: var(--serif); color: var(--purple-lt);
    font-size: 18px; font-style: italic; letter-spacing: 0.02em;
    text-decoration: none;
  }
  .nav-links { display: flex; gap: 24px; list-style: none }
  .nav-links a {
    color: var(--paper-dk); text-decoration: none;
    font-size: 13px; letter-spacing: 0.05em; text-transform: uppercase;
    transition: color 0.2s;
  }
  .nav-links a:hover, .nav-links a.active { color: var(--purple-lt) }

  /* Page header (listing pages) */
  .page-header {
    position: relative; z-index: 1;
    background: var(--ink); border-bottom: 4px solid var(--purple-mid);
    padding: 48px 56px 40px;
  }
  .page-header h1 {
    font-family: var(--serif); font-size: clamp(28px, 5vw, 48px);
    color: var(--paper); letter-spacing: -0.01em; margin-bottom: 8px;
  }
  .page-header p {
    color: rgba(255,255,255,0.4); font-size: 14px;
    letter-spacing: 0.08em; text-transform: uppercase;
  }

  /* Listing */
  .list-wrap {
    position: relative; z-index: 1;
    max-width: 860px; margin: 0 auto; padding: 48px 32px 80px;
    display: flex; flex-direction: column; gap: 20px;
  }
  .list-card {
    display: grid; grid-template-columns: 220px 1fr;
    text-decoration: none; background: var(--paper);
    border-top: 4px solid var(--purple);
    transition: border-color 0.2s;
    overflow: hidden;
  }
  .list-card:hover { border-color: var(--blush) }
  .list-card-thumb {
    background-size: cover; background-position: center;
    min-height: 160px; background-color: var(--purple-dk);
  }
  .list-card-body { padding: 24px 28px }
  .list-card-date {
    font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--purple-dk); margin-bottom: 8px;
  }
  .list-card-title {
    font-family: var(--serif); font-size: 22px;
    color: var(--ink); font-style: italic; line-height: 1.3;
    margin-bottom: 10px;
  }
  .list-card-peek {
    font-size: 13px; color: var(--ink-lt); line-height: 1.6;
  }
  .list-empty {
    font-family: var(--serif); font-style: italic;
    color: rgba(255,255,255,0.3); padding: 40px 0; font-size: 18px;
  }

  /* Article */
  .article-wrap {
    position: relative; z-index: 1;
    padding: 48px 32px 80px;
  }
  .article-inner {
    max-width: 720px; margin: 0 auto;
    background: var(--paper); border-top: 4px solid var(--purple);
    overflow: hidden;
  }
  .article-back {
    padding: 14px 32px;
    border-bottom: 1px solid var(--rule);
    font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;
  }
  .article-back a { color: var(--purple); text-decoration: none }
  .article-back a:hover { color: var(--purple-dk) }
  .article-hero {
    width: 100%; max-height: 420px; object-fit: cover;
    display: block; border-bottom: 3px solid var(--purple);
  }
  .article-date {
    padding: 16px 32px;
    font-size: 11px; letter-spacing: 0.12em; text-transform: uppercase;
    color: var(--purple-dk); border-bottom: 1px solid var(--rule);
  }
  .article-body {
    padding: 32px 40px 48px;
    font-family: var(--serif); font-size: 16px;
    line-height: 1.8; color: var(--ink);
  }
  .article-body h1 { font-size: 28px; margin: 32px 0 12px; color: var(--purple-dk); font-style: italic }
  .article-body h2 { font-size: 20px; margin: 28px 0 10px; color: var(--purple-dk) }
  .article-body h3 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 0.1em;
    margin: 24px 0 10px; color: var(--purple-mid);
  }
  .article-body p { margin-bottom: 18px }
  .article-body p:first-child { margin-top: 0 }
  .article-body strong { color: var(--purple-dk) }
  .article-body em { font-style: italic }
  .article-body blockquote {
    border-left: 3px solid var(--purple); padding-left: 20px;
    margin: 24px 0; color: var(--ink-lt); font-style: italic;
  }
  .article-body ul, .article-body ol { padding-left: 24px; margin-bottom: 18px }
  .article-body li { margin-bottom: 8px; line-height: 1.6 }
  .article-body hr {
    border: none; border-top: 1px solid var(--rule); margin: 32px 0;
  }
  .article-body code {
    background: var(--paper-dk); padding: 2px 6px;
    border-radius: 2px; font-size: 14px;
  }
  .article-body a { color: var(--purple); }
  .article-body a:hover { color: var(--purple-dk) }

  /* Footer */
  footer {
    position: relative; z-index: 1;
    background: var(--ink); border-top: 3px solid var(--purple-dk);
    padding: 32px 56px; display: flex;
    justify-content: space-between; align-items: center;
  }
  footer p { color: rgba(255,255,255,0.3); font-size: 12px; letter-spacing: 0.05em }
  footer a { color: var(--purple-lt); text-decoration: none }

  @media (max-width: 700px) {
    nav { padding: 0 16px }
    .nav-links { display: none }
    .page-header { padding: 32px 20px 24px }
    .list-wrap { padding: 24px 16px 48px; gap: 16px }
    .list-card { grid-template-columns: 1fr }
    .list-card-thumb { min-height: 180px }
    .article-wrap { padding: 24px 16px 48px }
    .article-body { padding: 24px 20px 36px }
    footer { flex-direction: column; gap: 12px; padding: 24px 20px; text-align: center }
  }
"""


# ── Fragments ──────────────────────────────────────────────────────────────────

def nav_html(active=''):
    blog_cls    = ' class="active"' if active == 'blog'    else ''
    recipes_cls = ' class="active"' if active == 'recipes' else ''
    return f"""<nav>
  <a class="nav-logo" href="{SITE}/">NedBotsSister</a>
  <ul class="nav-links">
    <li><a href="{SITE}/">Home</a></li>
    <li><a href="{SITE}/blog.html"{blog_cls}>Field Notes</a></li>
    <li><a href="{SITE}/recipes.html"{recipes_cls}>Recipes</a></li>
    <li><a href="https://kick.com/nedx" target="_blank">Watch Live</a></li>
  </ul>
</nav>"""

FOOTER_HTML = f"""<footer>
  <p>&copy; 2026 NedBotsSister &mdash; <a href="https://kick.com/nedx">kick.com/nedx</a></p>
  <p>She runs the chat. NedBot is supposed to help.</p>
</footer>"""

def full_page(title, desc, active, body):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title} — NedBotsSister</title>
<meta name="description" content="{desc}"/>
<style>{CSS}</style>
</head>
<body>
{nav_html(active)}
{body}
{FOOTER_HTML}
</body>
</html>"""


# ── Page builders ──────────────────────────────────────────────────────────────

def build_article(row, kind, back_label, back_url):
    img = img_url(row, kind)
    img_tag = f'<img class="article-hero" src="{img}" alt="{row["title"]}"/>\n  ' if img else ''
    body = f"""<div class="article-wrap">
  <div class="article-inner">
    <div class="article-back"><a href="{back_url}">{back_label}</a></div>
    {img_tag}<div class="article-date">{fmt_date_long(row['date'])}</div>
    <div class="article-body">
{render_md(row['content'])}
    </div>
  </div>
</div>"""
    active = 'blog' if kind == 'post' else 'recipes'
    desc = peek(row['content'], 140)
    return full_page(row['title'], desc, active, body)


def build_listing(rows, kind, url_fn, heading, subtitle, active):
    if rows:
        cards = []
        for row in rows:
            img = img_url(row, kind)
            thumb_style = f'style="background-image:url({img})"' if img else ''
            url = url_fn(row)
            cards.append(f"""  <a class="list-card" href="{url}">
    <div class="list-card-thumb" {thumb_style}></div>
    <div class="list-card-body">
      <div class="list-card-date">{fmt_date(row['date'])}</div>
      <div class="list-card-title">{row['title']}</div>
      <div class="list-card-peek">{peek(row['content'])}</div>
    </div>
  </a>""")
        inner = '\n'.join(cards)
    else:
        inner = '  <p class="list-empty">Nothing here yet.</p>'

    body = f"""<div class="page-header">
  <h1>{heading}</h1>
  <p>{subtitle}</p>
</div>
<main class="list-wrap">
{inner}
</main>"""
    return full_page(heading, subtitle, active, body)


# ── Main ───────────────────────────────────────────────────────────────────────

def build():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    posts   = db.execute("SELECT * FROM posts WHERE type='post'   ORDER BY date DESC").fetchall()
    recipes = db.execute("SELECT * FROM posts WHERE type='recipe' ORDER BY date DESC").fetchall()

    # Individual post pages
    for row in posts:
        out = post_out(row)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            build_article(row, 'post', '← Field Notes', f'{SITE}/blog.html'),
            encoding='utf-8'
        )
        print(f"  post    {out.relative_to(OUT_DIR)}")

    # Individual recipe pages
    for row in recipes:
        out = recipe_out(row)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            build_article(row, 'recipe', '← Recipes', f'{SITE}/recipes.html'),
            encoding='utf-8'
        )
        print(f"  recipe  {out.relative_to(OUT_DIR)}")

    # blog.html
    (OUT_DIR / "blog.html").write_text(
        build_listing(posts, 'post', post_url,
                      "Field Notes", "Dispatches from the booth — written by NedBotsSister",
                      "blog"),
        encoding='utf-8'
    )
    print("  listing blog.html")

    # recipes.html
    (OUT_DIR / "recipes.html").write_text(
        build_listing(recipes, 'recipe', recipe_url,
                      "Recipes", "Things she has made — and will actually stand behind",
                      "recipes"),
        encoding='utf-8'
    )
    print("  listing recipes.html")

    print(f"\nBuilt {len(posts)} post(s), {len(recipes)} recipe(s).")


if __name__ == '__main__':
    build()
