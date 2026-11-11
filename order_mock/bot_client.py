"""
Client for the NedBot mock order server — what the bot imports.

    from order_mock.bot_client import OrderClient
    c = OrderClient("http://127.0.0.1:8890")
    c.join(qr_url)                       # or c.join(seat_token=...)
    c.order_text("2 iced latte large, pudding")   # parse -> cart -> submit
    c.bill()

CLI (handy for testing):
    python order_mock/bot_client.py seats
    python order_mock/bot_client.py menu
    python order_mock/bot_client.py order 4 "iced latte, 2 pudding"
    python order_mock/bot_client.py status 4
    python order_mock/bot_client.py bill 4
    python order_mock/bot_client.py checkout 4

Stdlib only.
"""
import json, re, sys, urllib.request, urllib.error
from urllib.parse import urlparse, parse_qs


class OrderError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


class OrderClient:
    def __init__(self, base_url="http://127.0.0.1:8890", session_id=None, timeout=10):
        self.base = base_url.rstrip("/")
        self.session_id = session_id
        self.timeout = timeout
        self._menu = None

    # ── transport ──
    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.session_id:
            req.add_header("X-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                err = {"error": "http_error", "message": str(e)}
            raise OrderError(e.code, err.get("error"), err.get("message")) from None

    # ── session ──
    def join(self, qr_url=None, seat_token=None):
        """Open (or rejoin) the table session from a QR link or seat token."""
        if qr_url and not seat_token:
            seat_token = parse_qs(urlparse(qr_url).query).get("t", [None])[0]
        r = self._req("POST", "/api/session", {"seatToken": seat_token})
        self.session_id = r["sessionId"]
        return r

    def seats(self):        return self._req("GET", "/admin/seats")["seats"]
    def session(self):      return self._req("GET", "/api/session")

    # ── menu / cart / orders ──
    def menu(self, refresh=False):
        if self._menu is None or refresh:
            self._menu = self._req("GET", "/api/menu")
        return self._menu

    def cart(self):                     return self._req("GET", "/api/cart")
    def clear_cart(self):               return self._req("DELETE", "/api/cart")
    def remove(self, line_id):          return self._req("DELETE", f"/api/cart/items/{line_id}")
    def set_qty(self, line_id, qty):    return self._req("PATCH", f"/api/cart/items/{line_id}", {"qty": qty})
    def add(self, item_id, qty=1, options=None):
        return self._req("POST", "/api/cart/items", {"itemId": item_id, "qty": qty, "options": options or {}})
    def submit(self):                   return self._req("POST", "/api/orders")
    def orders(self):                   return self._req("GET", "/api/orders")["orders"]
    def cancel(self, order_id):         return self._req("POST", f"/api/orders/{order_id}/cancel")
    def bill(self):                     return self._req("GET", "/api/bill")
    def checkout(self):                 return self._req("POST", "/api/checkout")

    # ── chat-text ordering ──
    def parse(self, text):
        """
        "2 iced latte large, pudding x2, toast ogura" ->
        [{"itemId":"latte","qty":2,"options":{"temp":"iced","size":"large"}}, ...]
        Matches item names/ids and option choice names/ids by word; defaults
        a required option to its first choice. Raises OrderError on unknown items.
        """
        menu = self.menu()
        out = []
        for chunk in re.split(r"[,、\n;]+|\band\b", text.lower()):
            words = chunk.strip()
            if not words:
                continue
            qty = 1
            m = re.search(r"(?:^|\s)(?:x\s*(\d+)|(\d+)\s*x?)(?=\s|$)", words)
            if m:
                qty = int(m.group(1) or m.group(2))
                words = (words[:m.start()] + " " + words[m.end():]).strip()
            tokens = set(re.findall(r"[a-z0-9]+|[^\sa-z0-9]+", words))
            best, score = None, 0
            for item in menu["items"]:
                keys = set(re.findall(r"[a-z0-9]+", item["name"].lower())) | {item["id"]}
                s = len(keys & tokens)
                if s > score:
                    best, score = item, s
            if not best:
                raise OrderError(400, "unknown_item", f"Couldn't find a menu item in '{chunk.strip()}'")
            opts = {}
            for o in best["options"]:
                for c in o["choices"]:
                    names = {c["id"]} | set(re.findall(r"[a-z0-9]+", c["name"].lower()))
                    if names & tokens:
                        opts[o["id"]] = c["id"]
                if o["required"] and o["id"] not in opts:
                    opts[o["id"]] = o["choices"][0]["id"]
            out.append({"itemId": best["id"], "qty": qty, "options": opts})
        return out

    def order_text(self, text):
        """Parse chat text, add everything to the cart, submit. Cart is cleared first."""
        lines = self.parse(text)
        self.clear_cart()
        for l in lines:
            self.add(l["itemId"], l["qty"], l["options"])
        return self.submit()


def summary(order):
    items = ", ".join(
        f"{l['name']}" + (f" ({'/'.join(o['name'] for o in l['options'])})" if l["options"] else "")
        + f" x{l['qty']}" for l in order["items"])
    return f"#{order['orderId']} [{order['status']}] {items} — ¥{order['total']:,}"


def _cli(argv):
    import os
    base = os.environ.get("ORDER_MOCK_URL", "http://127.0.0.1:8890")
    c = OrderClient(base)
    if not argv:
        print(__doc__); return 1
    cmd, rest = argv[0], argv[1:]

    def join_table(n):
        tok = next(s["seatToken"] for s in c.seats() if s["seatId"] == int(n))
        c.join(seat_token=tok)

    try:
        if cmd == "seats":
            for s in c.seats():
                print(f"{s['name']:<9} {s['qrUrl']}")
        elif cmd == "menu":
            for i in c.menu()["items"]:
                opts = "; ".join(f"{o['id']}: " + "/".join(ch["id"] for ch in o["choices"]) for o in i["options"])
                print(f"{i['id']:<10} {i['name']:<15} ¥{i['price']:>5}" + (" SOLD OUT" if i.get("soldOut") else "")
                      + (f"   [{opts}]" if opts else ""))
        elif cmd == "order":
            join_table(rest[0]); print(summary(c.order_text(" ".join(rest[1:]))))
        elif cmd == "status":
            join_table(rest[0])
            for o in c.orders(): print(summary(o))
        elif cmd == "bill":
            join_table(rest[0]); b = c.bill()
            for o in b["orders"]: print(summary(o))
            print(f"Total ¥{b['total']:,} (incl. tax ¥{b['taxIncluded']:,})")
        elif cmd == "checkout":
            join_table(rest[0]); b = c.checkout()
            print(f"Checked out {b['seat']['name']}: ¥{b['total']:,}. {b['message']}")
        else:
            print(f"Unknown command {cmd}"); return 1
    except OrderError as e:
        print(f"Error: {e}"); return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
