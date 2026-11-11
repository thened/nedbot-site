"""
tableorder — personal assistant for QR table-order sites (built around AirREGI Order).

Use it only at a table you are sitting at.  It drives the same mobile web page your
phone would, records every request/response on your side, extracts the menu, and keeps
a ledger of what you ordered and paid.  It will NOT press a button that sends an order
to the kitchen (or checks out) unless you pass --confirm-real-order.

  python tableorder/tableorder.py open  "<qr url>" --label cafe-x [--agree] [--guests 2]
  python tableorder/tableorder.py step  <visit> "click:2名" "click:人数を確定して、注文へ進む" shot
  python tableorder/tableorder.py menu  <visit>              # extract menu.json from captured traffic + page
  python tableorder/tableorder.py step  <visit> "click:注文する" --confirm-real-order
  python tableorder/tableorder.py ledger [--add <visit> <total> "note"]
  python tableorder/tableorder.py visits

Steps:  click:<visible text>   click-nth:<n>:<text>   fill:<label or placeholder>=<value>
        scroll   back   wait:<ms>   shot[:<name>]   text   goto:<url>

Data lives in tableorder/visits/ (gitignored: it holds your table token and cookies).
Setup on your PC:  pip install playwright && python -m playwright install chromium
"""
import argparse, datetime, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(os.environ.get("TABLEORDER_DATA", Path(__file__).with_name("visits")))
LEDGER = ROOT / "ledger.jsonl"
CHROMIUM = os.environ.get("TABLEORDER_CHROMIUM")  # optional executable path
RECORD_HOSTS = ("airregi.jp",) + tuple(filter(None, os.environ.get("TABLEORDER_HOSTS", "").split(",")))

# Buttons that commit something real at the shop. Matched against the clicked text.
GUARDED = re.compile(r"注文(する|を確定|確定|を送信|送信)|送信する|会計(する|へ|を)|お会計|支払|checkout|place order|submit order",
                     re.I)


# ── storage ───────────────────────────────────────────────────────────────────

def visit_dir(name):
    d = ROOT / name
    if not d.is_dir():
        sys.exit(f"No visit '{name}'. Try: tableorder.py visits")
    return d


def meta(d, update=None):
    p = d / "visit.json"
    m = json.loads(p.read_text("utf-8")) if p.exists() else {}
    if update:
        m.update(update); p.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
    return m


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]


# ── browser session ───────────────────────────────────────────────────────────

class Session:
    """One browser run against a visit: restores cookies + sessionStorage, records traffic."""

    def __init__(self, d):
        self.d, self.m = d, meta(d)
        self.shots = 0

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(**({"executable_path": CHROMIUM} if CHROMIUM else {}))
        state = self.d / "state.json"
        ctx_args = dict(self.pw.devices["iPhone 13"], locale="ja-JP", timezone_id="Asia/Tokyo")
        if state.exists():
            ctx_args["storage_state"] = str(state)
        self.ctx = self.browser.new_context(**ctx_args)
        ss = self.m.get("sessionStorage") or {}
        if ss:  # Playwright's storage_state doesn't cover sessionStorage; restore it per origin
            self.ctx.add_init_script("(s=>{const o=s[location.origin];if(o)for(const k in o)"
                                     "try{sessionStorage.setItem(k,o[k])}catch(e){}})(%s)" % json.dumps(ss))
        self.page = self.ctx.new_page()
        self.page.on("response", self._record)
        return self

    def _record(self, resp):
        req = resp.request
        host = urlparse(req.url).hostname or ""
        if not any(host.endswith(h) for h in RECORD_HOSTS) or req.resource_type not in ("xhr", "fetch", "document"):
            return
        entry = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "method": req.method,
                 "url": req.url, "status": resp.status, "requestBody": None, "response": None}
        try:
            entry["requestBody"] = req.post_data_json if req.post_data else None
        except Exception:
            entry["requestBody"] = req.post_data
        if "json" in (resp.headers.get("content-type") or ""):
            try:
                entry["response"] = resp.json()
            except Exception:
                pass
        append_jsonl(self.d / "network.jsonl", entry)

    def goto(self, url):
        self.page.goto(url, wait_until="networkidle", timeout=45000)
        self.settle()

    def settle(self, ms=1500):
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        self.page.wait_for_timeout(ms)

    def shot(self, name=None):
        self.shots += 1
        n = len(list(self.d.glob("*.png"))) + 1
        p = self.d / f"{n:02d}-{name or 'shot'}.png"
        self.page.screenshot(path=str(p), full_page=True)
        return p

    def text(self):
        return self.page.inner_text("body")

    def save(self):
        self.ctx.storage_state(path=str(self.d / "state.json"))
        try:
            ss = self.page.evaluate("JSON.stringify(Object.assign({}, sessionStorage))")
            origin = self.page.evaluate("location.origin")
            all_ss = self.m.get("sessionStorage") or {}
            all_ss[origin] = json.loads(ss)
            self.m = meta(self.d, {"sessionStorage": all_ss, "lastUrl": self.page.url})
        except Exception:
            self.m = meta(self.d, {"lastUrl": self.page.url})
        (self.d / "last_page.txt").write_text(self.text(), "utf-8")

    def __exit__(self, *exc):
        try:
            self.save()
        finally:
            self.browser.close(); self.pw.stop()


def click_text(page, text, nth=0):
    for loc in (page.get_by_role("button", name=text), page.get_by_role("link", name=text),
                page.get_by_text(text, exact=True), page.get_by_text(text)):
        if loc.count() > nth:
            loc.nth(nth).scroll_into_view_if_needed(); loc.nth(nth).click()
            return True
    return False


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_open(a):
    q = parse_qs(urlparse(a.url).query)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    slug = re.sub(r"[^\w-]+", "-", a.label or "visit")
    name = f"{stamp}-{slug}"
    d = ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    meta(d, {"label": a.label, "qrUrl": a.url, "seatToken": (q.get("t") or [None])[0],
             "openedAt": datetime.datetime.now().isoformat(timespec="seconds")})
    with Session(d) as s:
        s.goto(a.url)
        body = s.text()
        if "同意" in body and "はじめる" in body:
            if a.agree:
                click_text(s.page, "同意してはじめる") or click_text(s.page, "はじめる")
                s.settle(2000); body = s.text()
                meta(d, {"agreedTerms": True})
            else:
                print("Terms screen (Airレジ オーダー terms / Recruit privacy policy). Read it, then rerun "
                      "with --agree, or: step <visit> 'click:同意してはじめる'")
        if "人数" in body and a.guests:
            label = f"{a.guests}名" if a.guests < 7 else "7名以上"
            if not click_text(s.page, label):
                print(f"Couldn't find the '{label}' button")
            s.page.wait_for_timeout(500)
            click_text(s.page, "人数を確定して、注文へ進む") or click_text(s.page, "注文へ進む")
            s.settle(2500)
            meta(d, {"guests": a.guests})
        elif "人数" in body:
            print("Headcount screen: rerun with --guests N, or use: step <visit> 'click:2名' "
                  "'click:人数を確定して、注文へ進む'")
        p = s.shot("open")
        print(f"visit: {name}\nurl:   {s.page.url}\nshot:  {p}\n\n{s.text()[:1500]}")


def cmd_step(a):
    d = visit_dir(a.visit)
    with Session(d) as s:
        s.goto(s.m.get("lastUrl") or s.m["qrUrl"])
        for st in a.steps:
            kind, _, arg = st.partition(":")
            if kind in ("click", "click-nth"):
                nth = 0
                if kind == "click-nth":
                    n, _, arg = arg.partition(":"); nth = int(n)
                if GUARDED.search(arg) and not a.confirm_real_order:
                    s.shot("blocked"); print(f"BLOCKED '{arg}': this would commit a real order/payment. "
                                              "Rerun with --confirm-real-order once you've said OK.")
                    return 3
                if GUARDED.search(arg):
                    pre = s.shot("before-submit")
                    append_jsonl(d / "actions.jsonl", {"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                                       "action": "confirmed-click", "text": arg,
                                                       "pageText": s.text(), "shot": pre.name})
                if not click_text(s.page, arg, nth):
                    print(f"No element with text '{arg}'"); s.shot("notfound"); return 2
                s.settle()
            elif kind == "fill":
                field, _, val = arg.partition("=")
                loc = s.page.get_by_label(field)
                if not loc.count():
                    loc = s.page.get_by_placeholder(field)
                loc.first.fill(val)
            elif kind == "scroll":
                s.page.mouse.wheel(0, 1500); s.settle(800)
            elif kind == "back":
                s.page.go_back(); s.settle()
            elif kind == "wait":
                s.page.wait_for_timeout(int(arg or 1000))
            elif kind == "goto":
                s.goto(arg)
            elif kind == "shot":
                print("shot:", s.shot(arg or None))
            elif kind == "text":
                print(s.text())
            else:
                print(f"Unknown step '{st}'"); return 2
        p = s.shot("step")
        print(f"url:  {s.page.url}\nshot: {p}\n\n{s.text()[:2000]}")


NAME_KEYS = ("name", "itemName", "menuName", "productName", "displayName", "title")
PRICE_KEYS = ("price", "unitPrice", "taxIncludedPrice", "priceWithTax", "sellingPrice", "amount")


def find_items(obj, path="", out=None):
    """Walk captured JSON; collect dicts that look like menu items (a name + a numeric price)."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        name = next((obj[k] for k in NAME_KEYS if isinstance(obj.get(k), str)), None)
        price = next((obj[k] for k in PRICE_KEYS if isinstance(obj.get(k), (int, float))), None)
        if name and price is not None:
            out.append({"name": name, "price": price, "path": path,
                        "soldOut": any(obj.get(k) for k in ("soldOut", "isSoldOut", "outOfStock")),
                        "id": next((obj[k] for k in ("id", "itemId", "menuId", "productId") if k in obj), None)})
        for k, v in obj.items():
            find_items(v, f"{path}.{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            find_items(v, f"{path}[{i}]", out)
    return out


def fmt_opts(it):
    if not it.get("options"):
        return ""
    parts = [o["name"] + (f" +{o['price']}" if o["price"] else "") for o in it["options"]]
    return "   (" + ", ".join(parts) + ")"


def cmd_menu(a):
    d = visit_dir(a.visit)
    items, seen = [], set()
    entries = [e for e in read_jsonl(d / "network.jsonl") if e["method"] == "GET"]
    def with_items(pat):
        return [e for e in entries if re.search(pat, e["url"], re.I) and not re.search(r"cart|order|bill", e["url"], re.I)
                and find_items(e.get("response"))]
    for e in (with_items(r"menu") or with_items(r"item|product|categor") or entries):  # prefer menu endpoints so cart/bill lines don't leak in
        found = find_items(e.get("response"))
        # Anything nested inside another item (e.g. option choices with their own price) is an option.
        tops = [it for it in found if not any(it["path"].startswith(o["path"] + ".") for o in found if o is not it)]
        for it in tops:
            it["options"] = [{"name": o["name"], "price": o["price"]} for o in found
                             if o["path"].startswith(it["path"] + ".")]
            key = (it["name"], it["price"])
            if key not in seen:
                seen.add(key); items.append({**it, "source": e["url"]})
    menu = {"capturedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "label": meta(d).get("label"), "items": items}
    (d / "menu.json").write_text(json.dumps(menu, ensure_ascii=False, indent=2), "utf-8")
    if not items:
        print("No item-like data in captured API responses yet. Open the menu pages with `step` "
              "(click each category, scroll) and run `menu` again. Last page text is in last_page.txt.")
        return 1
    for it in items:
        print(f"¥{it['price']:>6,}  {it['name']}" + ("  [sold out]" if it["soldOut"] else "") + fmt_opts(it))
    print(f"\n{len(items)} items -> {d / 'menu.json'}")


def cmd_ledger(a):
    if a.add:
        visit, total, note = a.add
        m = meta(visit_dir(visit))
        append_jsonl(LEDGER, {"t": datetime.datetime.now().isoformat(timespec="seconds"), "visit": visit,
                              "label": m.get("label"), "total": int(total), "note": note})
    rows = read_jsonl(LEDGER)
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir()) if ROOT.exists() else []:
        orders = [e for e in read_jsonl(d / "network.jsonl")
                  if e["method"] in ("POST", "PUT") and re.search(r"order|cart|bill|check|account|payment", e["url"], re.I)]
        confirms = read_jsonl(d / "actions.jsonl")
        if orders or confirms:
            print(f"{d.name}: {len(confirms)} confirmed submit(s), {len(orders)} order/bill API call(s)")
            for e in orders:
                print(f"   {e['t']}  {e['method']} {urlparse(e['url']).path}  -> {e['status']}")
    total = 0
    for r in rows:
        total += r["total"]
        print(f"{r['t'][:16]}  {r.get('label') or r['visit']:<20} ¥{r['total']:>7,}  {r['note']}")
    if rows:
        print(f"{'':16}  {'TOTAL':<20} ¥{total:>7,}")


def cmd_visits(a):
    if not ROOT.exists():
        print("No visits yet."); return
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        m = meta(d)
        n = len(read_jsonl(d / "network.jsonl"))
        print(f"{d.name:<32} {m.get('label') or '':<16} guests={m.get('guests', '-')}  {n} requests")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open"); p.add_argument("url"); p.add_argument("--label"); p.add_argument("--guests", type=int)
    p.add_argument("--agree", action="store_true", help="accept the site's terms screen for you")
    p.set_defaults(fn=cmd_open)
    p = sub.add_parser("step"); p.add_argument("visit"); p.add_argument("steps", nargs="+")
    p.add_argument("--confirm-real-order", action="store_true"); p.set_defaults(fn=cmd_step)
    p = sub.add_parser("menu"); p.add_argument("visit"); p.set_defaults(fn=cmd_menu)
    p = sub.add_parser("ledger"); p.add_argument("--add", nargs=3, metavar=("VISIT", "TOTAL", "NOTE"))
    p.set_defaults(fn=cmd_ledger)
    p = sub.add_parser("visits"); p.set_defaults(fn=cmd_visits)
    a = ap.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
