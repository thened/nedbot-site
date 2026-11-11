"""
Customer side of the Table Agent Protocol (draft table-agent/0.1, see PROTOCOL.md).

When a shop has its own agent (GET <qr origin>/.well-known/table-agent), the bot talks to it
directly with signed messages, with no browser and no screen-reading. When it doesn't,
`connect_any` falls back to the screen recipes in recipe.py behind the same calls.

Python (for the bot):
    from native import connect_any, NeedsConfirmation, TrustNeeded
    c = connect_any(qr_url, label="cafe", guests=1)      # may raise TrustNeeded on a first visit
    c.pair("482913")                                     # code read off the table's phone
    c.menu(); c.add("Cafe Latte", 2, ["Iced", "Large"])
    try: c.submit()
    except NeedsConfirmation as e: ask_human(e.summary)  # a signed quote
    c.submit(confirm=True)                               # orders exactly that quote
    c.bill()

CLI:
    python tableorder/native.py connect "<qr url>" --label cafe --guests 1 [--trust-shop] [--spend 5000]
    python tableorder/native.py pair 482913
    python tableorder/native.py menu | cart | bill | status | transcript       [--visit V]
    python tableorder/native.py add "Cafe Latte" --qty 2 --opt Iced --opt Large
    python tableorder/native.py submit [--confirm-real-order]
    python tableorder/native.py paid 1900 [--note ...]
    python tableorder/native.py identity [--descriptor-url URL]   # this bot's public descriptor

Needs: pip install cryptography   (plus playwright + pyyaml only for the recipe fallback)
"""
import argparse, calendar, datetime, hashlib, hmac, json, os, secrets, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
import tap  # noqa: E402
import tableorder as T  # noqa: E402
import knowledge as K  # noqa: E402

AGENT = {"name": os.environ.get("TABLEORDER_AGENT_NAME", "NedBot"), "version": "0.1"}
DESCRIPTOR_URL = os.environ.get("TABLEORDER_AGENT_DESCRIPTOR")  # e.g. https://nedbot.site/.well-known/table-agent-client.json
DEFAULT_SCOPES = ["menu.read", "order.quote", "order.place", "bill.read"]
DEFAULT_SPEND = 5000


class NeedsConfirmation(Exception):
    def __init__(self, summary, quote=None):
        super().__init__(summary)
        self.summary, self.quote = summary, quote


class TrustNeeded(Exception):
    """First contact with a shop (or its key changed): a human must confirm it's the place they're in."""
    def __init__(self, shop, fingerprint, reason):
        super().__init__(f"{reason}: {shop['name']} (key {fingerprint})")
        self.shop, self.fingerprint, self.reason = shop, fingerprint, reason


class NativeError(Exception):
    pass


# ── identity ──────────────────────────────────────────────────────────────────

def identity_dir():
    return T.ROOT / "identity"


def agent_key():
    return tap.Key.load_or_create(identity_dir() / "agent_ed25519.pem")


def principal_for(shop_id):
    """Pairwise pseudonym: stable per shop, unlinkable across shops."""
    p = identity_dir() / "pseudonym.secret"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
    return "p-" + tap.b64u(hmac.new(p.read_bytes(), shop_id.encode(), hashlib.sha256).digest())[:16]


def client_descriptor(url=None):
    k = agent_key()
    return {"type": "tap.client", "protocol": tap.PROTOCOL, "agent": AGENT, "key": k.describe(),
            "fingerprint": tap.fingerprint_words(k.kid), "descriptor": url or DESCRIPTOR_URL,
            "promises": {"humanConfirmation": "always", "principal": "pairwise-pseudonym",
                         "payment": "never-by-agent", "disclosure": "identifies-as-agent"},
            "protocols": [tap.PROTOCOL]}


# ── http ──────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method, url, body=None, headers=None):
    data = None if body is None else (body if isinstance(body, bytes) else tap.canonical(body))
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"code": "http_error", "message": str(e)}


def raise_for(status, body):
    if status >= 400:
        raise NativeError(f"{body.get('code') or body.get('error')}: {body.get('message')}")
    return body


def discover(qr_url):
    """The shop descriptor from the QR's own origin, or None if the shop has no agent."""
    u = urlparse(qr_url)
    origin = f"{u.scheme}://{u.netloc}"
    try:
        status, env = http("GET", origin + "/.well-known/table-agent")
    except (urllib.error.URLError, OSError):
        return None
    if status != 200 or not isinstance(env, dict) or "payload" not in env:
        return None  # includes redirects: the descriptor must live on the QR's origin
    try:
        shop_key = tap.Key.from_public(env["payload"]["key"]["public"])
        payload = tap.verify(env, shop_key, "tap.shop")
    except (tap.TapError, KeyError, ValueError):
        return None
    return {"origin": origin, "payload": payload, "envelope": env, "key": shop_key}


# ── client ────────────────────────────────────────────────────────────────────

class NativeClient:
    mode = "native"

    def __init__(self, visit_dir):
        self.d = Path(visit_dir)
        self.state = json.loads((self.d / "native.json").read_text("utf-8"))
        self.key = agent_key()
        self.shop_key = tap.Key.from_public(self.state["shop"]["key"]["public"])

    # context-manager parity with RecipeClient
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._save()

    def _save(self):
        (self.d / "native.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")

    def _log(self, direction, envelope_or_body):
        T.append_jsonl(self.d / "transcript.jsonl", {"t": datetime.datetime.now().isoformat(timespec="seconds"),
                                                     "dir": direction, "msg": envelope_or_body})

    @property
    def place_key(self):
        return self.state["placeKey"]

    # handshake
    @classmethod
    def connect(cls, qr_url, label=None, guests=None, trust_shop=False, spend_limit=DEFAULT_SPEND,
                scopes=DEFAULT_SCOPES, found=None):
        found = found or discover(qr_url)
        if not found:
            raise NativeError("This shop has no table agent; use the recipe fallback (connect_any)")
        shop = found["payload"]["shop"]
        system = found["payload"].get("system") or {"id": "table-agent", "name": "Table agent"}
        pkey = K.place_key(system["id"], shop["id"], shop["name"])
        place = K.load(pkey)
        fp = tap.fingerprint_words(found["key"].kid)
        pinned = place.get("shopKey")
        if pinned and pinned != found["key"].kid and not trust_shop:
            raise TrustNeeded(shop, fp, "This shop's key CHANGED since your last visit; confirm with staff before trusting it")
        if not pinned and not trust_shop:
            raise TrustNeeded(shop, fp, "First visit: confirm this is the place you're sitting in")

        d = T.new_visit(qr_url, label or shop["name"])
        T.meta(d, {"mode": "native", "guests": guests, "placeKey": pkey, "placeName": shop["name"],
                   "placeId": shop["id"], "system": system["id"]})
        K.record_visit(pkey, name=shop["name"], place_id=shop["id"], system=system["id"],
                       system_name=system.get("name"), host=urlparse(qr_url).netloc, visit=d.name, guests=guests)
        place = K.load(pkey)
        place.update({"shopKey": found["key"].kid, "shopKeyFingerprint": fp, "protocol": tap.PROTOCOL,
                      "terms": found["payload"].get("terms"), "agentPolicy": found["payload"].get("policy")})
        K.save(place)

        seat_token = T.meta(d).get("seatToken")
        key = agent_key()
        hello = tap.sign(key, {"type": "tap.hello", "seatToken": seat_token,
                               "agent": {**AGENT, "key": key.describe(),
                                         **({"descriptor": DESCRIPTOR_URL} if DESCRIPTOR_URL else {})},
                               "principal": {"id": principal_for(shop["id"]), "kind": "pairwise-pseudonym"},
                               "intent": {"partySize": guests} if guests else {},
                               "request": {"scopes": list(scopes), "spendLimit": int(spend_limit)},
                               "confirmation": "human-per-order", "nonce": secrets.token_urlsafe(12)})
        state = {"origin": found["origin"], "shop": found["payload"], "placeKey": pkey, "cart": [],
                 "helloHash": tap.digest(hello["payload"])}
        (d / "native.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        c = cls(d)
        c._log("recv", found["envelope"])
        c._log("sent", hello)
        status, env = http("POST", found["origin"] + found["payload"]["endpoints"]["hello"], hello)
        c._log("recv", env)
        raise_for(status, env)
        ch = tap.verify(env, c.shop_key, "tap.challenge")
        if ch["helloHash"] != state["helloHash"]:
            raise NativeError("Challenge doesn't refer to our hello")
        c.state.update({"handshakeId": ch["handshakeId"], "challengeHash": tap.digest(ch), "challenge": ch})
        c._save()
        return c

    def pair(self, code):
        prove = tap.sign(self.key, {"type": "tap.prove", "handshakeId": self.state["handshakeId"],
                                    "challengeHash": self.state["challengeHash"], "code": str(code).replace(" ", "")})
        self._log("sent", prove)
        status, env = http("POST", self.state["origin"] + self.state["shop"]["endpoints"]["prove"], prove)
        self._log("recv", env)
        raise_for(status, env)
        g = tap.verify(env, self.shop_key, "tap.grant")
        expect = tap.digest([self.state["helloHash"], self.state["challengeHash"], tap.digest(prove["payload"])])
        if g["agentKid"] != self.key.kid or g["transcriptHash"] != expect:
            raise NativeError("Grant doesn't match our handshake (key or transcript mismatch)")
        self.state.update({"grant": g, "token": g["token"], "transcriptHash": expect})
        self._save()
        return g

    # signed calls
    def _call(self, method, path, body=None, expect=None):
        if not self.state.get("token"):
            raise NativeError("Not paired yet: enter the code from the table's phone (pair <code>)")
        raw = b"" if body is None else tap.canonical(body)
        headers = tap.sign_request(self.key, method, path, raw, self.state["token"])
        status, resp = http(method, self.state["origin"] + path, raw if body is not None else None, headers)
        if expect:
            self._log("recv", resp)
        raise_for(status, resp)
        return tap.verify(resp, self.shop_key, expect) if expect else resp

    # API (same shape as RecipeClient)
    def menu(self):
        m = self._call("GET", self.state["shop"]["endpoints"]["menu"], expect="tap.menu")
        cats = {c["id"]: c["name"] for c in m.get("categories", [])}
        self.state["menuRaw"] = m["items"]
        self._save()
        items = [{"name": i["name"], "price": i["price"], "category": cats.get(i.get("category"), i.get("category")),
                  "soldOut": bool(i.get("soldOut")),
                  "options": [{"name": c["name"], "price": c.get("price", 0)}
                              for o in i.get("options", []) for c in o.get("choices", [])]}
                 for i in m["items"]]
        K.record_menu(self.place_key, items)
        return items

    def _resolve(self, item, options):
        raw = self.state.get("menuRaw") or (self.menu() and self.state["menuRaw"])
        by = {i["name"].lower(): i for i in raw} | {i["id"]: i for i in raw}
        it = by.get(item.lower())
        if not it:
            close = [i["name"] for i in raw if item.lower() in i["name"].lower()]
            raise NativeError(f"'{item}' isn't on the menu" + (f"; did you mean {', '.join(close)}?" if close else ""))
        chosen = {}
        for want in options:
            hit = [(o["id"], c["id"]) for o in it.get("options", []) for c in o["choices"]
                   if want.lower() in (c["id"].lower(), c["name"].lower())]
            if not hit:
                raise NativeError(f"'{it['name']}' has no option '{want}'")
            chosen[hit[0][0]] = hit[0][1]
        return it, chosen

    def add(self, item, qty=1, options=()):
        it, chosen = self._resolve(item, options)
        if it.get("soldOut"):
            raise NativeError(f"'{it['name']}' is sold out")
        self.state["cart"].append({"itemId": it["id"], "qty": int(qty), "options": chosen})
        try:  # let the shop price it now, so a bad line never sits in the cart
            self._quote()
        except NativeError:
            self.state["cart"].pop()
            raise
        self.state.pop("lastQuote", None)
        self._save()
        return self.state["cart"]

    def clear(self):
        self.state["cart"] = []
        self.state.pop("lastQuote", None)
        self._save()

    def _quote(self):
        if not self.state["cart"]:
            raise NativeError("Cart is empty")
        q = self._call("POST", self.state["shop"]["endpoints"]["quote"], {"lines": self.state["cart"]},
                       expect="tap.quote")
        q["_cartHash"] = tap.digest(self.state["cart"])
        return q

    def cart(self):
        if not self.state["cart"]:
            return {"lines": [], "total": 0}
        q = self._quote()
        return {"lines": [_line(l) for l in q["lines"]], "total": q["total"]}

    def submit(self, confirm=False):
        q = self.state.get("lastQuote")
        fresh = (q and q["_cartHash"] == tap.digest(self.state["cart"])
                 and _epoch(q["expiresAt"]) > time.time() + 10)
        if not confirm or not fresh:
            q = self._quote()
            self.state["lastQuote"] = q
            self._save()
            summary = "; ".join(f"{l['name']} ×{l['qty']}" + (f" ({_opts(l)})" if l["options"] else "")
                                for l in q["lines"]) + f" — total ¥{q['total']:,} (quote {q['quoteId']}, " \
                                                       f"valid until {_local_hm(q['expiresAt'])})"
            if not q.get("withinLimit", True):
                summary += f"  ⚠ over this bot's remaining limit (¥{q['remainingLimit']:,})"
            raise NeedsConfirmation(("Prices changed or quote expired. " if confirm else "") + "Send this order? "
                                    + summary, q)
        r = self._call("POST", self.state["shop"]["endpoints"]["orders"], {"quoteId": q["quoteId"]},
                       expect="tap.receipt")
        if r["quoteId"] != q["quoteId"] or r["total"] != q["total"] or r["transcriptHash"] != self.state["transcriptHash"]:
            raise NativeError("Receipt doesn't match the quote you approved; check the bill with staff")
        T.append_jsonl(self.d / "receipts.jsonl", r)
        K.record_order(self.place_key, self.d.name, [_line(l) for l in r["lines"]], r["total"], order_id=r["orderId"])
        self.clear()
        return {"receipt": r, "summary": f"#{r['orderId']} ¥{r['total']:,} (receipt {r['receiptId']})"}

    def status(self, order_id):
        return self._call("GET", f"{self.state['shop']['endpoints']['orders']}/{order_id}")

    def bill(self):
        b = self._call("GET", self.state["shop"]["endpoints"]["bill"], expect="tap.bill")
        return {"orders": [{"orderId": o["orderId"], "status": o["status"], "total": o["total"],
                            "items": ", ".join(f"{l['name']} ×{l['qty']}" for l in o["items"]),
                            "via": (o.get("via") or {}).get("agent")} for o in b["orders"]],
                "total": b["total"], "open": b["open"]}

    def paid(self, total, note=""):
        K.record_payment(self.place_key, self.d.name, int(total), note)

    def verify_transcript(self):
        ok, bad = 0, []
        for e in T.read_jsonl(self.d / "transcript.jsonl"):
            msg = e["msg"]
            if not isinstance(msg, dict) or "sig" not in msg:
                continue
            signer = self.key if e["dir"] == "sent" else self.shop_key
            try:
                tap.verify(msg, signer)
                ok += 1
            except tap.TapError as err:
                bad.append(f"{e['t']} {msg.get('payload', {}).get('type')}: {err}")
        for r in T.read_jsonl(self.d / "receipts.jsonl"):
            ok += 1  # receipts were verified on arrival; re-check the transcript binding
            if r["transcriptHash"] != self.state.get("transcriptHash"):
                bad.append(f"receipt {r['receiptId']}: transcript mismatch")
        return ok, bad


def _epoch(iso_z):
    return calendar.timegm(time.strptime(iso_z, "%Y-%m-%dT%H:%M:%SZ"))


def _local_hm(iso_z):
    return datetime.datetime.fromtimestamp(_epoch(iso_z)).strftime("%H:%M")


def _opts(l):
    return " · ".join(o["name"] for o in l.get("options", []))


def _line(l):
    return {"name": l["name"], "qty": l["qty"], "options": _opts(l), "subtotal": l["subtotal"]}


def connect_any(qr_url, label=None, guests=None, agree=False, trust_shop=False, **kw):
    """Native protocol if the shop speaks it, otherwise the screen recipe for its system."""
    found = discover(qr_url)
    if found:
        return NativeClient.connect(qr_url, label, guests, trust_shop=trust_shop, found=found, **kw)
    from recipe import RecipeClient
    return RecipeClient.open_table(qr_url, label=label, guests=guests, agree=agree)


def load_visit(visit="last"):
    d = T.visit_dir(visit)
    if (d / "native.json").exists():
        return NativeClient(d)
    from recipe import RecipeClient
    return RecipeClient.for_visit(visit)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("connect"); p.add_argument("url"); p.add_argument("--label"); p.add_argument("--guests", type=int)
    p.add_argument("--trust-shop", action="store_true"); p.add_argument("--spend", type=int, default=DEFAULT_SPEND)
    p.add_argument("--agree", action="store_true", help="(recipe fallback) accept the site's terms screen")
    p = sub.add_parser("pair"); p.add_argument("code"); p.add_argument("--visit", default="last")
    for n in ("menu", "cart", "bill", "transcript", "clear"):
        sub.add_parser(n).add_argument("--visit", default="last")
    p = sub.add_parser("status"); p.add_argument("order"); p.add_argument("--visit", default="last")
    p = sub.add_parser("add"); p.add_argument("item"); p.add_argument("--qty", type=int, default=1)
    p.add_argument("--opt", action="append", default=[]); p.add_argument("--visit", default="last")
    p = sub.add_parser("submit"); p.add_argument("--confirm-real-order", action="store_true")
    p.add_argument("--visit", default="last")
    p = sub.add_parser("paid"); p.add_argument("total", type=int); p.add_argument("--note", default="")
    p.add_argument("--visit", default="last")
    p = sub.add_parser("identity"); p.add_argument("--descriptor-url")
    a = ap.parse_args()
    T.ROOT.mkdir(parents=True, exist_ok=True)

    try:
        if a.cmd == "identity":
            print(json.dumps(client_descriptor(a.descriptor_url), ensure_ascii=False, indent=2)); return
        if a.cmd == "connect":
            c = connect_any(a.url, a.label, a.guests, agree=a.agree, trust_shop=a.trust_shop, spend_limit=a.spend)
            if isinstance(c, NativeClient):
                ch = c.state["challenge"]
                print(f"Connected to {c.state['shop']['shop']['name']} ({c.state['shop'].get('system', {}).get('name')}) "
                      f"natively, protocol {tap.PROTOCOL}.\nvisit: {c.d.name}\n\nPresence check for {ch['seat']['name']}: "
                      f"{ch['challenge']['where']}\nThen: native.py pair <code>")
            else:
                print(f"No table agent here; using the screen recipe '{c.recipe_id}'. visit: {c.d.name}")
                c.__exit__(None, None, None)
            return
        with load_visit(a.visit) as c:
            if a.cmd == "pair":
                g = c.pair(a.code)
                print(f"Paired with {g['seat']['name']}. Scopes: {', '.join(g['scopes'])}. "
                      f"Limit ¥{g['limits']['spendLimit']:,}. Ends at {g['expires']}.")
            elif a.cmd == "menu":
                cat = None
                for it in c.menu():
                    if it.get("category") != cat:
                        cat = it.get("category"); print(f"\n[{cat}]")
                    opts = ", ".join(o["name"] + (f" +{o['price']}" if o.get("price") else "") for o in it.get("options") or [])
                    print(f"  ¥{it['price']:>6,}  {it['name']}" + ("  (sold out)" if it["soldOut"] else "")
                          + (f"   [{opts}]" if opts else ""))
            elif a.cmd == "add":
                c.add(a.item, a.qty, a.opt); cart = c.cart()
                print(f"Added. Cart: {len(cart['lines'])} line(s), ¥{cart['total']:,}")
            elif a.cmd == "cart":
                cart = c.cart()
                for l in cart["lines"]:
                    print(f"  {l['name']} ×{l['qty']}" + (f" ({l['options']})" if l.get("options") else "") + f"  ¥{l['subtotal']:,}")
                print(f"Total ¥{(cart['total'] or 0):,}")
            elif a.cmd == "clear":
                c.clear(); print("Cart cleared.")
            elif a.cmd == "submit":
                r = c.submit(confirm=a.confirm_real_order)
                print("SENT:", r["summary"])
            elif a.cmd == "status":
                o = c.status(a.order); print(f"#{o['orderId']} {o['status']}")
            elif a.cmd == "bill":
                b = c.bill()
                for o in b["orders"]:
                    print(f"  #{o['orderId']} [{o['status']}] {o['items']}  ¥{o['total']:,}"
                          + (f"  (via {o['via']})" if o.get("via") else ""))
                print(f"Bill ¥{(b['total'] or 0):,}" + ("" if b.get("open", True) else "  (checked out)"))
            elif a.cmd == "paid":
                c.paid(a.total, a.note); print(f"Recorded ¥{a.total:,} paid.")
            elif a.cmd == "transcript":
                ok, bad = c.verify_transcript()
                print(f"{ok} signed message(s) verified." + ("" if not bad else "\nPROBLEMS:\n  " + "\n  ".join(bad)))
                if bad:
                    sys.exit(4)
    except TrustNeeded as e:
        print(f"CHECK FIRST: {e.reason}.\n  Shop: {e.shop['name']}\n  Key fingerprint: {e.fingerprint}\n"
              "If that's where you are (ask staff if unsure), rerun with --trust-shop.")
        sys.exit(5)
    except NeedsConfirmation as e:
        print(f"NEEDS YOUR OK: {e.summary}\nRerun with --confirm-real-order to order exactly this quote.")
        sys.exit(3)
    except NativeError as e:
        print(f"Error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
