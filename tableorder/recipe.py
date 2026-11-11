"""
Recipe runner: treats what a table-order site shows you as an API.

A recipe (tableorder/recipes/<system>.yaml) describes, per ordering system, how to do
each operation (open, menu, add_item, cart, submit, bill) as steps that target the
page's accessibility tree: roles and accessible names, the same thing a screen reader uses.

Python (for the bot):
    from recipe import RecipeClient
    with RecipeClient.open_table(qr_url, label="cafe", guests=1, agree=True) as c:
        c.menu()                                    # -> [{name, price, category, soldOut}]
        c.add("Cafe Latte", qty=2, options=["Iced", "Large"])
        c.cart()                                    # -> {"lines": [...], "total": 1300}
        c.submit()                                  # raises NeedsConfirmation(summary)
        c.submit(confirm=True)                      # only after the human said OK
        c.bill()

CLI:
    python tableorder/recipe.py open "<qr url>" --label cafe --guests 1 [--agree]
    python tableorder/recipe.py menu   [visit]          # visit defaults to "last"
    python tableorder/recipe.py add    "Cafe Latte" --qty 2 --opt Iced --opt Large [--visit V]
    python tableorder/recipe.py cart   [visit]
    python tableorder/recipe.py submit [visit] [--confirm-real-order]
    python tableorder/recipe.py bill   [visit]
    python tableorder/recipe.py paid   1700 [--note "..."] [--visit V]
    python tableorder/recipe.py places                  # what the knowledge store knows
    python tableorder/recipe.py recipes                 # installed recipes + which steps are verified

Step syntax (one key per step; extra keys: `if: <param>`, `note:`):
    goto: <url>                      click: <target>        check: <target>
    select: {target, value}          fill: {target, value}  choose: {each: <list param>, target}
    scroll_until_end: true           wait: <ms>             expect: <target>
    read: {into, each, within?, parse?, from?: name|text}
    read_one: {into, target, parse?, from?}
  Suffix `?` = optional (skip if not on screen).  Suffix `!` = commits something real at the
  shop (order, payment); never runs without confirm=True.  Any click whose name looks like
  ordering/paying is treated as `!` even if the recipe forgot to mark it.
  Target: {role, name | name_regex, exact?} | {label} | {text} | {css}; {param} placeholders
  are filled from the call (regex-escaped inside name_regex).
"""
import argparse, datetime, json, re, sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import tableorder as T  # noqa: E402
import knowledge as K   # noqa: E402

RECIPES = Path(__file__).with_name("recipes")
INT_FIELDS = {"price", "qty", "subtotal", "total", "tax", "unitPrice"}


class RecipeError(Exception):
    pass


class NeedsConfirmation(Exception):
    def __init__(self, summary):
        super().__init__(summary)
        self.summary = summary


# ── recipes ───────────────────────────────────────────────────────────────────

def load_recipes():
    return {p.stem: yaml.safe_load(p.read_text("utf-8")) for p in sorted(RECIPES.glob("*.yaml"))}


def detect(url):
    host = urlparse(url).netloc
    for rid, r in load_recipes().items():
        for h in r["match"]["hosts"]:
            if host == h or host.endswith("." + h):
                return rid, r
    raise RecipeError(f"No recipe matches {host}. Known: {', '.join(load_recipes())}")


def fill(v, params, regex=False):
    if isinstance(v, str):
        def rep(m):
            k = m.group(1)
            if k not in params:
                return m.group(0)
            return re.escape(str(params[k])) if regex else str(params[k])
        return re.sub(r"\{(\w+)\}", rep, v)
    if isinstance(v, dict):
        return {k: fill(x, params, regex or k == "name_regex") for k, x in v.items()}
    if isinstance(v, list):
        return [fill(x, params, regex) for x in v]
    return v


def to_value(k, v):
    if v is None:
        return None
    v = v.strip()
    if k in INT_FIELDS:
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else None
    return v


# ── runner ────────────────────────────────────────────────────────────────────

class RecipeClient:
    def __init__(self, visit_dir, resume=True):
        self.d = Path(visit_dir)
        self.m = T.meta(self.d)
        self.recipe_id, self.r = detect(self.m["qrUrl"])
        T.meta(self.d, {"recipe": self.recipe_id, "system": self.r["system"]})
        self.resume = resume
        self.s = None

    # lifecycle
    @classmethod
    def open_table(cls, qr_url, label=None, guests=None, agree=False):
        c = cls(T.new_visit(qr_url, label), resume=False)
        c.__enter__()
        try:
            c.run("open", qr_url=qr_url, guests=guests, guests_num=min(guests or 0, 7), agree=agree)
            T.meta(c.d, {"guests": guests})
            c._identify_place()
        except Exception:
            c.__exit__(None, None, None)
            raise
        return c

    @classmethod
    def for_visit(cls, visit="last"):
        return cls(T.visit_dir(visit))

    def __enter__(self):
        if self.s is None:
            self.s = T.Session(self.d).__enter__()
            self.s.page.set_default_timeout(10000)
            if self.resume:
                self.s.goto(self.m.get("lastUrl") or self.m["qrUrl"])
        return self

    def __exit__(self, *exc):
        if self.s is not None:
            self.s.__exit__(*exc)
            self.s = None

    @property
    def page(self):
        return self.s.page

    # locating
    def locate(self, spec, scope=None):
        scope = scope or self.page
        if isinstance(spec, str):
            spec = {"text": spec}
        if "role" in spec:
            kw = {}
            if "name_regex" in spec:
                kw["name"] = re.compile(spec["name_regex"])
            elif "name" in spec:
                kw["name"] = spec["name"]
                kw["exact"] = spec.get("exact", True)
            return scope.get_by_role(spec["role"], **kw)
        if "label" in spec:
            return scope.get_by_label(spec["label"], exact=spec.get("exact", False))
        if "text" in spec:
            return scope.get_by_text(spec["text"], exact=spec.get("exact", False))
        if "css" in spec:
            return scope.locator(spec["css"])
        raise RecipeError(f"Bad target {spec}")

    def find(self, spec, timeout, scope=None):
        """Resolve a target (or a list of fallback targets) to a visible locator, else None."""
        specs = spec if isinstance(spec, list) else [spec]
        each = max(500, timeout // len(specs))
        for sp in specs:
            loc = self.locate(sp, scope)
            if self._present(loc, each):
                return loc
        return None

    def _present(self, loc, timeout):
        try:
            loc.first.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    def _fail(self, msg):
        shot = self.s.shot("recipe-failed")
        raise RecipeError(f"{msg}  (screenshot: {shot}). The site may have changed; the recipe needs an update.")

    def _accessible_text(self, el, source):
        if source == "text":
            return el.inner_text()
        return el.get_attribute("aria-label") or el.inner_text()

    def _parse(self, text, pattern):
        text = " ".join(text.split())
        if not pattern:
            return {"text": text}
        m = re.search(pattern, text)
        if not m:
            return None
        row = {}
        for k, v in m.groupdict().items():  # "price2" etc. = alternative spellings of "price"
            base = re.sub(r"\d+$", "", k)
            if row.get(base) is None:
                row[base] = to_value(base, v)
        return row

    def _site_error(self):
        err = self.r.get("site_error")
        if not err:
            return None
        loc = self.locate(err)
        if loc.count():
            t = loc.first.inner_text().strip()
            return t or None
        return None

    def _commit_click(self, loc, label, confirm):
        if not confirm:
            raise NeedsConfirmation(f"'{label}' would send something real to the shop")
        pre = self.s.shot("before-commit")
        T.append_jsonl(self.d / "actions.jsonl", {"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                                  "action": "confirmed-commit", "target": label,
                                                  "recipe": self.recipe_id, "pageText": self.s.text(),
                                                  "shot": pre.name})
        loc.first.click()

    def run(self, op, confirm=False, **params):
        if op not in self.r["ops"]:
            raise RecipeError(f"Recipe {self.recipe_id} has no '{op}' operation")
        status = str(self.r.get("verified", {}).get(op, "unverified"))
        if status.startswith("unverified") and not op.startswith("_"):
            print(f"[recipe] {self.recipe_id}.{op} is unverified; stopping at the first step that doesn't match.",
                  file=sys.stderr)
        out = {}
        for raw in self.r["ops"][op]:
            step = dict(raw)
            cond = step.pop("if", None)
            step.pop("note", None)
            if cond and not params.get(cond):
                continue
            (key, arg), = step.items()
            optional, commit = key.endswith("?"), key.endswith("!")
            action = key.rstrip("?!")
            arg = fill(arg, params)
            timeout = 1500 if optional else 8000

            if action == "goto":
                self.s.goto(arg)
            elif action in ("click", "check"):
                loc = self.find(arg, timeout)
                if loc is None:
                    if optional:
                        continue
                    self._fail(f"{op}: couldn't find {arg}")
                label = loc.first.get_attribute("aria-label") or loc.first.inner_text()
                if not loc.first.is_enabled():
                    raise RecipeError(f"{op}: '{' '.join(label.split())}' is on screen but disabled (sold out?)")
                if commit or (action == "click" and T.GUARDED.search(label or "")):
                    self._commit_click(loc, label.strip(), confirm)
                elif action == "check":
                    loc.first.check()
                else:
                    loc.first.scroll_into_view_if_needed()
                    loc.first.click()
                self.s.settle(600)
                e = self._site_error()
                if e:
                    raise RecipeError(f"{op}: the site says: {e}")
            elif action == "choose":
                for choice in params.get(arg["each"]) or []:
                    loc = self.find(fill(arg["target"], {**params, "choice": choice}), timeout)
                    if loc is None:
                        self._fail(f"{op}: no option '{choice}'")
                    loc.first.check()
            elif action in ("select", "fill"):
                loc = self.find(arg["target"], timeout)
                if loc is None:
                    if optional:
                        continue
                    self._fail(f"{op}: couldn't find {arg['target']}")
                if action == "select":
                    loc.first.select_option(label=str(arg["value"]))
                else:
                    loc.first.fill(str(arg["value"]))
            elif action == "scroll_until_end":
                last = -1
                for _ in range(30):
                    h = self.page.evaluate("document.scrollingElement.scrollHeight")
                    if h == last:
                        break
                    last = h
                    self.page.mouse.wheel(0, 2000)
                    self.page.wait_for_timeout(400)
            elif action == "wait":
                self.page.wait_for_timeout(int(arg))
            elif action == "expect":
                if self.find(arg, timeout) is None and not optional:
                    self._fail(f"{op}: expected {arg} on screen")
            elif action == "read":
                rows = []
                groups = [(None, self.page)]
                if arg.get("within"):
                    gl = self.find(arg["within"], timeout)
                    groups = [] if gl is None else [(g.get_attribute("aria-label") or g.inner_text().split("\n")[0], g) for g in gl.all()]
                for gname, scope in groups:
                    for el in self.locate(arg["each"], scope).all():
                        row = self._parse(self._accessible_text(el, arg.get("from", "name")), arg.get("parse"))
                        if row is None:
                            continue
                        if gname is not None and arg.get("group_field", "group"):
                            row[arg.get("group_field", "group")] = gname
                        rows.append(row)
                if not rows and not optional:
                    self._fail(f"{op}: read '{arg['into']}' found nothing")
                out[arg["into"]] = rows
            elif action == "read_one":
                loc = self.find(arg["target"], timeout)
                if loc is None:
                    if optional:
                        out[arg["into"]] = None
                        continue
                    self._fail(f"{op}: couldn't read {arg['into']}")
                row = self._parse(self._accessible_text(loc.first, arg.get("from", "name")), arg.get("parse"))
                out[arg["into"]] = row
            else:
                raise RecipeError(f"Unknown step '{key}' in {self.recipe_id}.{op}")
        T.append_jsonl(self.d / "recipe_log.jsonl", {"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                                     "op": op, "params": params, "result": out})
        return out

    # place identity
    def _identify_place(self):
        pl = self.r.get("place", {})
        name = place_id = None
        if "_place_name" in self.r["ops"]:
            try:
                row = self.run("_place_name").get("place") or {}
                name = row.get("name") or row.get("text")
            except RecipeError:
                name = None
        rule = pl.get("id_from_request")
        if rule:
            for e in T.read_jsonl(self.d / "network.jsonl"):
                if re.search(rule["url_regex"], e["url"]) and isinstance(e.get("requestBody"), dict):
                    place_id = e["requestBody"].get(rule["body_key"]) or place_id
        name = name or self.m.get("label")
        key = K.place_key(self.r["system"], place_id, name)
        T.meta(self.d, {"placeKey": key, "placeName": name, "placeId": place_id})
        self.m = T.meta(self.d)
        K.record_visit(key, name=name, place_id=place_id, system=self.r["system"], system_name=self.r.get("name"),
                       host=urlparse(self.m["qrUrl"]).netloc, visit=self.d.name, guests=self.m.get("guests"))

    @property
    def place_key(self):
        if not self.m.get("placeKey"):
            self._identify_place()
        return self.m["placeKey"]

    # API
    def menu(self):
        items = self.run("menu").get("items", [])
        for it in items:
            it.setdefault("soldOut", False)
            it["soldOut"] = bool(it.get("soldOut"))
        try:  # cross-check with what the site's own data said (also picks up option prices)
            net = T.extract_menu(self.d)
        except Exception:
            net = []
        if not items and net:  # screen read found nothing: fall back to the site's data
            items = [{"name": n["name"], "price": n["price"], "category": None, "soldOut": n["soldOut"],
                      "options": n.get("options", [])} for n in net]
        by_name = {n["name"]: n for n in net}
        for it in items:
            if it["name"] in by_name and by_name[it["name"]].get("options"):
                it["options"] = by_name[it["name"]]["options"]
        (self.d / "menu_seen.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")
        K.record_menu(self.place_key, items)
        return items

    def add(self, item, qty=1, options=()):
        seen = self.d / "menu_seen.json"
        if seen.exists():
            menu = {m["name"]: m for m in json.loads(seen.read_text("utf-8"))}
            if menu and item not in menu:
                close = [n for n in menu if item.lower() in n.lower() or n.lower() in item.lower()]
                raise RecipeError(f"'{item}' isn't on the menu" + (f"; did you mean {', '.join(close)}?" if close else ""))
            if menu.get(item, {}).get("soldOut"):
                raise RecipeError(f"'{item}' is sold out")
        return self.run("add_item", item=item, qty=qty, options=list(options))

    def cart(self):
        r = self.run("cart")
        return {"lines": r.get("lines") or [], "total": (r.get("total") or {}).get("total")}

    def submit(self, confirm=False):
        c = self.cart()
        if not c["lines"]:
            raise RecipeError("Cart is empty")
        summary = "; ".join(f"{l['name']} ×{l.get('qty', 1)}" + (f" ({l['options']})" if l.get("options") else "")
                            for l in c["lines"]) + f" — total ¥{c['total']:,}"
        if not confirm:
            raise NeedsConfirmation(f"Send this order? {summary}")
        self.run("submit", confirm=True)
        K.record_order(self.place_key, self.d.name, c["lines"], c["total"])
        return {"sent": c, "summary": summary}

    def bill(self):
        r = self.run("bill")
        return {"orders": r.get("orders") or [], "total": (r.get("total") or {}).get("total")}

    def paid(self, total, note=""):
        K.record_payment(self.place_key, self.d.name, int(total), note)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_menu(items):
    cat = None
    for it in items:
        if it.get("category") != cat:
            cat = it.get("category"); print(f"\n[{cat}]")
        opts = ", ".join(o["name"] + (f" +{o['price']}" if o.get("price") else "") for o in it.get("options") or [])
        print(f"  ¥{it['price']:>6,}  {it['name']}" + ("  (sold out)" if it["soldOut"] else "") + (f"   [{opts}]" if opts else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open"); p.add_argument("url"); p.add_argument("--label"); p.add_argument("--guests", type=int)
    p.add_argument("--agree", action="store_true")
    for name in ("menu", "cart", "bill"):
        sub.add_parser(name).add_argument("visit", nargs="?", default="last")
    p = sub.add_parser("submit"); p.add_argument("visit", nargs="?", default="last")
    p.add_argument("--confirm-real-order", action="store_true")
    p = sub.add_parser("add"); p.add_argument("item"); p.add_argument("--qty", type=int, default=1)
    p.add_argument("--opt", action="append", default=[]); p.add_argument("--visit", default="last")
    p = sub.add_parser("paid"); p.add_argument("total", type=int); p.add_argument("--note", default="")
    p.add_argument("--visit", default="last")
    sub.add_parser("places"); sub.add_parser("recipes")
    a = ap.parse_args()
    T.ROOT.mkdir(parents=True, exist_ok=True)

    try:
        if a.cmd == "recipes":
            for rid, r in load_recipes().items():
                print(f"{rid}: {r.get('name')}  hosts={r['match']['hosts']}")
                for op, steps in r["ops"].items():
                    if not op.startswith("_"):
                        status = r.get("verified", {}).get(op, "unverified")
                        print(f"   {op:<9} {len(steps):>2} steps  {status}")
            return
        if a.cmd == "places":
            for p in K.all_places():
                cur = [e for e in p["menu"].values() if "missingSince" not in e]
                print(f"{p['name'] or p['key']:<24} {p['systemName'] or p['system']:<28} visits={len(p['visits'])} "
                      f"menu={len(cur)} last={p['lastVisit']}")
            print(f"\nNotes: {K.NOTES}")
            return
        if a.cmd == "open":
            with RecipeClient.open_table(a.url, a.label, a.guests, a.agree) as c:
                print(f"visit: {c.d.name}\nplace: {c.m.get('placeName')}  system: {c.r.get('name')}")
                print(f"shot:  {c.s.shot('opened')}")
            return
        visit = getattr(a, "visit", "last")
        with RecipeClient.for_visit(visit) as c:
            if a.cmd == "menu":
                _print_menu(c.menu())
            elif a.cmd == "add":
                c.add(a.item, a.qty, a.opt); cart = c.cart()
                print(f"Added. Cart: {len(cart['lines'])} line(s), ¥{cart['total']:,}")
            elif a.cmd == "cart":
                cart = c.cart()
                for l in cart["lines"]:
                    print(f"  {l['name']} ×{l['qty']}" + (f" ({l['options']})" if l.get("options") else "") + f"  ¥{l['subtotal']:,}")
                print(f"Total ¥{(cart['total'] or 0):,}")
            elif a.cmd == "submit":
                r = c.submit(confirm=a.confirm_real_order)
                print("SENT:", r["summary"])
            elif a.cmd == "bill":
                b = c.bill()
                for o in b["orders"]:
                    print(f"  #{o.get('orderId')} [{o.get('status')}] {o.get('items')}  ¥{(o.get('total') or 0):,}")
                print(f"Bill ¥{(b['total'] or 0):,}")
            elif a.cmd == "paid":
                c.paid(a.total, a.note); print(f"Recorded ¥{a.total:,} paid at {c.m.get('placeName')}")
    except NeedsConfirmation as e:
        print(f"NEEDS YOUR OK: {e.summary}\nRerun with --confirm-real-order once you've checked it.")
        sys.exit(3)
    except RecipeError as e:
        print(f"Recipe error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
