"""
NedBot mock table-order server — a local stand-in for QR "order from your phone"
systems, so the bot can be tested without sending anything to a real restaurant.

Stdlib only.  Run:  python order_mock/server.py   (port 8890, override with ORDER_MOCK_PORT)

Flow (same shape as a real table QR):
  1. Each table has a QR link:  /auth/seat?t=<seat-token>&qrCodeType=2
       browser -> redirected to the phone UI
       bot     -> POST /api/session {"seatToken": "..."}  -> {"sessionId": ...}
  2. GET  /api/menu
  3. POST /api/cart/items  {"itemId", "qty", "options"}     (header X-Session-Id)
  4. POST /api/orders                                        submits the cart to the "kitchen"
  5. GET  /api/orders     statuses move received -> preparing -> served over time
  6. GET  /api/bill,  POST /api/checkout                     closes the table session

All state is in memory and resets on restart.  See order_mock/README.md.
"""
import json, os, re, time, uuid, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT      = int(os.environ.get("ORDER_MOCK_PORT", "8890"))
HOST      = os.environ.get("ORDER_MOCK_HOST", "127.0.0.1")
TABLES    = int(os.environ.get("ORDER_MOCK_TABLES", "8"))
# Seconds an order spends in each status before advancing (0 = stay "received").
STEP_SECS = float(os.environ.get("ORDER_MOCK_STEP_SECS", "20"))
MAX_QTY   = 10
UI_FILE   = Path(__file__).with_name("ui.html")

# Stable, fake seat tokens (uuid5) so test scripts can hard-code them.
NS = uuid.UUID("6e656462-6f74-2d6d-6f63-6b2d63616665")
SEATS = {str(uuid.uuid5(NS, f"table-{n}")): {"seatId": n, "name": f"Table {n}"}
         for n in range(1, TABLES + 1)}

MENU = {
    "categories": [
        {"id": "drinks",  "name": "Drinks"},
        {"id": "food",    "name": "Food"},
        {"id": "dessert", "name": "Dessert"},
    ],
    "items": [
        {"id": "blend",     "category": "drinks",  "name": "Blend Coffee",   "price": 480,
         "options": [{"id": "temp", "name": "Temperature", "required": True,
                      "choices": [{"id": "hot", "name": "Hot", "price": 0},
                                  {"id": "iced", "name": "Iced", "price": 50}]}]},
        {"id": "latte",     "category": "drinks",  "name": "Cafe Latte",     "price": 550,
         "options": [{"id": "temp", "name": "Temperature", "required": True,
                      "choices": [{"id": "hot", "name": "Hot", "price": 0},
                                  {"id": "iced", "name": "Iced", "price": 50}]},
                     {"id": "size", "name": "Size", "required": False,
                      "choices": [{"id": "reg", "name": "Regular", "price": 0},
                                  {"id": "large", "name": "Large", "price": 100}]}]},
        {"id": "matcha",    "category": "drinks",  "name": "Matcha Latte",   "price": 600, "options": []},
        {"id": "oj",        "category": "drinks",  "name": "Orange Juice",   "price": 450, "options": []},
        {"id": "toast",     "category": "food",    "name": "Thick Toast",    "price": 400,
         "options": [{"id": "spread", "name": "Spread", "required": True,
                      "choices": [{"id": "butter", "name": "Butter", "price": 0},
                                  {"id": "ogura", "name": "Red bean (ogura)", "price": 80}]}]},
        {"id": "napolitan", "category": "food",    "name": "Napolitan",      "price": 950, "options": []},
        {"id": "curry",     "category": "food",    "name": "Curry Rice",     "price": 1000, "options": []},
        {"id": "sandwich",  "category": "food",    "name": "Egg Sandwich",   "price": 700, "options": [],
         "soldOut": True},
        {"id": "pudding",   "category": "dessert", "name": "Retro Pudding",  "price": 500, "options": []},
        {"id": "cake",      "category": "dessert", "name": "Cheesecake",     "price": 550, "options": []},
    ],
}
ITEMS = {i["id"]: i for i in MENU["items"]}
STATUSES = ["received", "preparing", "served"]

lock = threading.Lock()
sessions = {}   # sessionId -> {seat, cart: [], orders: [], open, createdAt}
order_seq = [0]
seat_session = {}  # seatId -> active sessionId (one open session per table, like a real shop)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message); self.status, self.code, self.message = status, code, message


def now():
    return time.time()


def order_status(order):
    if STEP_SECS <= 0:
        return order["status"] if order["status"] == "cancelled" else "received"
    if order["status"] == "cancelled":
        return "cancelled"
    step = int((now() - order["placedAt"]) // STEP_SECS)
    return STATUSES[min(step, len(STATUSES) - 1)]


def price_line(item, options, qty):
    unit = item["price"]
    chosen = []
    opts = options or {}
    if not isinstance(opts, dict):
        raise ApiError(400, "bad_options", "options must be an object like {\"temp\": \"iced\"}")
    known = {o["id"]: o for o in item["options"]}
    for key in opts:
        if key not in known:
            raise ApiError(400, "bad_option", f"'{item['id']}' has no option '{key}'")
    for o in item["options"]:
        pick = opts.get(o["id"])
        if pick is None:
            if o["required"]:
                raise ApiError(400, "option_required",
                               f"'{item['name']}' needs '{o['id']}': one of "
                               + ", ".join(c["id"] for c in o["choices"]))
            continue
        choice = next((c for c in o["choices"] if c["id"] == pick), None)
        if not choice:
            raise ApiError(400, "bad_choice", f"'{pick}' is not a valid {o['id']} for '{item['name']}'")
        unit += choice["price"]
        chosen.append({"option": o["id"], "choice": choice["id"], "name": choice["name"]})
    return {"lineId": uuid.uuid4().hex[:8], "itemId": item["id"], "name": item["name"],
            "qty": qty, "unitPrice": unit, "options": chosen, "subtotal": unit * qty}


def cart_view(s):
    return {"items": s["cart"], "total": sum(l["subtotal"] for l in s["cart"])}


def order_view(o):
    return {**{k: v for k, v in o.items() if k != "status"}, "status": order_status(o)}


def bill_view(s):
    orders = [order_view(o) for o in s["orders"]]
    total = sum(o["total"] for o in orders if o["status"] != "cancelled")
    tax = total - round(total / 1.1)  # prices are tax-included (10%)
    return {"seat": s["seat"], "orders": orders, "total": total, "taxIncluded": tax, "open": s["open"]}


# ── Handlers ──────────────────────────────────────────────────────────────────

def open_session(seat_token):
    seat = SEATS.get(seat_token or "")
    if not seat:
        raise ApiError(404, "unknown_seat", "That seat token isn't valid (check the QR link)")
    sid = seat_session.get(seat["seatId"])
    if sid and sessions[sid]["open"]:
        return sid, sessions[sid], False  # join the table's existing session
    sid = uuid.uuid4().hex
    sessions[sid] = {"seat": seat, "cart": [], "orders": [], "open": True, "createdAt": now()}
    seat_session[seat["seatId"]] = sid
    return sid, sessions[sid], True


def get_session(handler, allow_closed=False):
    sid = handler.headers.get("X-Session-Id") or handler.query.get("session", [None])[0]
    s = sessions.get(sid or "")
    if not s:
        raise ApiError(401, "no_session", "Missing or unknown X-Session-Id; open one via POST /api/session")
    if not s["open"] and not allow_closed:
        raise ApiError(409, "session_closed", "This table has checked out; scan the QR again")
    return s


def route(h, method, path):
    if method == "GET" and path in ("/", "/order"):
        return h.send_file(UI_FILE, "text/html; charset=utf-8")

    if method == "GET" and path == "/auth/seat":
        sid, _, _ = open_session(h.query.get("t", [None])[0])
        return h.redirect(f"/order?s={sid}")

    if method == "GET" and path == "/health":
        return {"status": "ok", "mock": True}

    if method == "GET" and path == "/admin/seats":
        base = f"http://{h.headers.get('Host', f'{HOST}:{PORT}')}"
        return {"seats": [{**seat, "seatToken": tok,
                           "qrUrl": f"{base}/auth/seat?t={tok}&qrCodeType=2",
                           "activeSession": seat_session.get(seat["seatId"])}
                          for tok, seat in SEATS.items()]}

    if method == "GET" and path == "/admin/orders":  # "kitchen" view
        out = []
        for sid, s in sessions.items():
            for o in s["orders"]:
                out.append({**order_view(o), "seat": s["seat"]["name"], "sessionId": sid})
        return {"orders": sorted(out, key=lambda o: o["placedAt"], reverse=True)}

    if method == "POST" and path == "/admin/reset":
        sessions.clear(); seat_session.clear()
        return {"ok": True}

    if method == "POST" and path == "/api/session":
        body = h.json_body()
        token = body.get("seatToken")
        if not token and body.get("qrUrl"):
            token = parse_qs(urlparse(body["qrUrl"]).query).get("t", [None])[0]
        sid, s, created = open_session(token)
        return {"sessionId": sid, "seat": s["seat"], "created": created}

    if method == "GET" and path == "/api/session":
        s = get_session(h, allow_closed=True)
        return {"seat": s["seat"], "open": s["open"], "cart": cart_view(s), "orderCount": len(s["orders"])}

    if method == "GET" and path == "/api/menu":
        return MENU

    if method == "GET" and path == "/api/cart":
        return cart_view(get_session(h))

    if method == "POST" and path == "/api/cart/items":
        s = get_session(h)
        body = h.json_body()
        item = ITEMS.get(body.get("itemId", ""))
        if not item:
            raise ApiError(404, "unknown_item", f"No menu item '{body.get('itemId')}'")
        if item.get("soldOut"):
            raise ApiError(409, "sold_out", f"'{item['name']}' is sold out")
        qty = body.get("qty", 1)
        if not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise ApiError(400, "bad_qty", f"qty must be a whole number 1-{MAX_QTY}")
        line = price_line(item, body.get("options"), qty)
        s["cart"].append(line)
        return {"added": line, "cart": cart_view(s)}

    m = re.fullmatch(r"/api/cart/items/([0-9a-f]+)", path)
    if m and method in ("PATCH", "DELETE"):
        s = get_session(h)
        line = next((l for l in s["cart"] if l["lineId"] == m.group(1)), None)
        if not line:
            raise ApiError(404, "unknown_line", "No such cart line")
        if method == "DELETE":
            s["cart"].remove(line)
        else:
            qty = h.json_body().get("qty")
            if not isinstance(qty, int) or not 0 <= qty <= MAX_QTY:
                raise ApiError(400, "bad_qty", f"qty must be a whole number 0-{MAX_QTY}")
            if qty == 0:
                s["cart"].remove(line)
            else:
                line["qty"], line["subtotal"] = qty, qty * line["unitPrice"]
        return cart_view(s)

    if method == "DELETE" and path == "/api/cart":
        s = get_session(h); s["cart"].clear()
        return cart_view(s)

    if method == "POST" and path == "/api/orders":
        s = get_session(h)
        if not s["cart"]:
            raise ApiError(409, "empty_cart", "Cart is empty")
        order_seq[0] += 1
        order = {"orderId": f"{s['seat']['seatId']}-{order_seq[0]:04d}",
                 "items": s["cart"], "total": sum(l["subtotal"] for l in s["cart"]),
                 "placedAt": now(), "status": "received"}
        s["orders"].append(order); s["cart"] = []
        print(f"  >> KITCHEN  {s['seat']['name']}  #{order['orderId']}  "
              + ", ".join(f"{l['name']} x{l['qty']}" for l in order["items"]))
        return order_view(order)

    if method == "GET" and path == "/api/orders":
        return {"orders": [order_view(o) for o in get_session(h, allow_closed=True)["orders"]]}

    m = re.fullmatch(r"/api/orders/([\w-]+)/cancel", path)
    if m and method == "POST":
        s = get_session(h)
        o = next((o for o in s["orders"] if o["orderId"] == m.group(1)), None)
        if not o:
            raise ApiError(404, "unknown_order", "No such order")
        if order_status(o) != "received":
            raise ApiError(409, "too_late", f"Order is already {order_status(o)}; can't cancel")
        o["status"] = "cancelled"
        return order_view(o)

    if method == "GET" and path == "/api/bill":
        return bill_view(get_session(h, allow_closed=True))

    if method == "POST" and path == "/api/checkout":
        s = get_session(h)
        s["open"] = False
        seat_session.pop(s["seat"]["seatId"], None)
        return {**bill_view(s), "message": "Please pay at the register. Thank you!"}

    raise ApiError(404, "not_found", f"No route for {method} {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "NedBotOrderMock/1.0"

    def _dispatch(self, method):
        u = urlparse(self.path)
        self.query = parse_qs(u.query)
        try:
            with lock:
                result = route(self, method, u.path.rstrip("/") or "/")
            if result is not None:
                self.send_json(200, result)
        except ApiError as e:
            self.send_json(e.status, {"error": e.code, "message": e.message})
        except Exception as e:  # keep the server alive for the bot
            self.send_json(500, {"error": "server_error", "message": str(e)})

    def do_GET(self):     self._dispatch("GET")
    def do_POST(self):    self._dispatch("POST")
    def do_PATCH(self):   self._dispatch("PATCH")
    def do_DELETE(self):  self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Id")

    def json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ApiError(400, "bad_json", "Body must be JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json", "Body must be a JSON object")
        return body

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status); self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def send_file(self, path, ctype):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def redirect(self, location):
        self.send_response(302); self.send_header("Location", location)
        self.send_header("Content-Length", "0"); self.end_headers()

    def log_message(self, fmt, *args):
        print("  " + fmt % args)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n NedBot mock order server  ->  http://{HOST}:{PORT}   (MOCK - no real restaurant)")
    print(" Seat QR links:")
    for tok, seat in SEATS.items():
        print(f"   {seat['name']:<9} http://{HOST}:{PORT}/auth/seat?t={tok}&qrCodeType=2")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
