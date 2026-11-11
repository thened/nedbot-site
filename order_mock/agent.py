"""
Shop side of the Table Agent Protocol (draft table-agent/0.1, see tableorder/PROTOCOL.md),
plugged into the mock order server. Shares the server's table sessions, so agent orders
and phone orders land on the same bill and the same kitchen.

Endpoints:
  GET  /.well-known/table-agent       signed shop descriptor (identity, key, policy, terms)
  POST /agent/hello                   tap.hello  -> tap.challenge (pairing code shown on the table's phone)
  POST /agent/prove                   tap.prove  -> tap.grant
  GET  /agent/menu                    signed menu                  (menu.read)
  POST /agent/quote                   signed, priced quote          (order.quote; no side effects)
  POST /agent/orders                  {quoteId} -> signed receipt   (order.place)
  GET  /agent/orders/<id>             order status                  (bill.read)
  GET  /agent/bill                    signed bill                   (bill.read)
Phone (the person at the table):
  GET  /api/agents                    pending pairing requests (with their code) + connected agents
  POST /api/agents/<id>/revoke        deny a pending request or revoke a grant
"""
import json, os, re, secrets, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tableorder"))
import tap  # noqa: E402
from tap import TapError  # noqa: E402,F401  (re-exported for server.py)

NOT_MINE = object()

KEY_PATH = Path(os.environ.get("ORDER_MOCK_SHOP_KEY", Path(__file__).with_name(".shop_key.pem")))
SHOP_KEY = tap.Key.load_or_create(KEY_PATH)
SHOP = {"id": "nedbot-cafe", "name": "NedBot Café"}
SYSTEM = {"id": "nedbot-mock", "name": "NedBot mock table order", "vendor": "nedbot"}

SCOPES = ["menu.read", "order.quote", "order.place", "bill.read"]
POLICY = {
    "agents": os.environ.get("ORDER_MOCK_AGENTS", "allowed"),  # "allowed" | "off"
    "presence": ["pairing_code"],
    "scopes": SCOPES,
    "maxSpendPerGrant": int(os.environ.get("ORDER_MOCK_AGENT_MAX_SPEND", "10000")),
    "maxOrdersPerHour": int(os.environ.get("ORDER_MOCK_AGENT_MAX_ORDERS", "6")),
    "humanConfirmation": "customer-side",
    "grantExpires": "checkout",
    "disclosure": "required",
}
TERMS = {"currency": "JPY", "pricesIncludeTax": True, "taxRate": 0.10, "serviceCharge": 0,
         "cancellation": "until-preparing", "payment": "at-register"}
ENDPOINTS = {"hello": "/agent/hello", "prove": "/agent/prove", "menu": "/agent/menu", "quote": "/agent/quote",
             "orders": "/agent/orders", "bill": "/agent/bill"}

CODE_TTL, QUOTE_TTL, MAX_ATTEMPTS = 300, 300, 5

handshakes = {}   # handshakeId -> {...}
grants = {}       # token -> {...}
quotes = {}       # quoteId -> {...}
seen_sigs = {}    # signature -> expiry (replay protection)


def now():
    return time.time()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def signed(payload):
    return tap.sign(SHOP_KEY, payload)


def on_checkout(session):
    for g in grants.values():
        if g["session"] is session and not g.get("ended"):
            g["ended"] = "checkout"


def _grant_view(g):
    return {"grantId": g["grantId"], "agent": g["agentLabel"], "fingerprint": tap.fingerprint_words(g["agentKey"].kid),
            "principal": g["principal"], "scopes": g["scopes"], "spent": g["spent"],
            "spendLimit": g["limits"]["spendLimit"], "status": g.get("ended") or ("revoked" if g["revoked"] else "active")}


# ── auth for calls after the grant ────────────────────────────────────────────

def authenticate(h, method, path, scope):
    auth = h.headers.get("Authorization") or ""
    if not auth.startswith("TAP "):
        raise TapError("no_grant", "Missing 'Authorization: TAP <token>'; do the handshake first", 401)
    token = auth[4:].strip()
    g = grants.get(token)
    if not g:
        raise TapError("unknown_grant", "Unknown token", 401)
    if g["revoked"]:
        raise TapError("grant_revoked", "The person at the table revoked this agent", 401)
    if g.get("ended") or not g["session"]["open"]:
        raise TapError("grant_expired", "The table has checked out; this grant has ended", 401)
    try:
        ts = int(h.headers.get("X-TAP-Ts") or 0)
    except ValueError:
        ts = 0
    if abs(now() - ts) > tap.MAX_SKEW:
        raise TapError("stale_request", "X-TAP-Ts missing or too far from the shop's clock", 401)
    sig, nonce = h.headers.get("X-TAP-Sig") or "", h.headers.get("X-TAP-Nonce") or ""
    if len(nonce) < 8:
        raise TapError("no_nonce", "X-TAP-Nonce (random, at least 8 chars) is required", 401)
    base = tap.request_sig_base(method, path, ts, nonce, h.raw_body, token)
    if not g["agentKey"].verify_bytes(base, sig):
        raise TapError("bad_request_signature", "X-TAP-Sig doesn't verify with the agent's key", 401)
    for s, exp in list(seen_sigs.items()):
        if exp < now():
            del seen_sigs[s]
    if sig in seen_sigs:
        raise TapError("replay", "This exact request was already used", 401)
    seen_sigs[sig] = now() + tap.MAX_SKEW * 2
    if scope not in g["scopes"]:
        raise TapError("scope", f"This grant doesn't include '{scope}'", 403)
    return g


# ── routes ────────────────────────────────────────────────────────────────────

def route(h, method, path, srv):
    if method == "GET" and path == "/.well-known/table-agent":
        if POLICY["agents"] != "allowed":
            raise TapError("agents_off", "This shop isn't accepting agents right now", 503)
        return signed({"type": "tap.shop", "shop": SHOP, "system": SYSTEM, "key": SHOP_KEY.describe(),
                       "endpoints": ENDPOINTS,
                       "policy": POLICY, "terms": TERMS})

    if method == "POST" and path == "/agent/hello":
        return hello(h, srv)
    if method == "POST" and path == "/agent/prove":
        return prove(h, srv)

    if method == "GET" and path == "/agent/menu":
        authenticate(h, method, path, "menu.read")
        return signed({"type": "tap.menu", "shop": SHOP["id"], "currency": "JPY", "pricesIncludeTax": True,
                       "categories": srv.MENU["categories"], "items": srv.MENU["items"]})

    if method == "POST" and path == "/agent/quote":
        g = authenticate(h, method, path, "order.quote")
        return quote(h, g, srv)

    if method == "POST" and path == "/agent/orders":
        g = authenticate(h, method, path, "order.place")
        return place(h, g, srv)

    m = re.fullmatch(r"/agent/orders/([\w-]+)", path)
    if m and method == "GET":
        g = authenticate(h, method, path, "bill.read")
        o = next((o for o in g["session"]["orders"] if o["orderId"] == m.group(1)), None)
        if not o:
            raise TapError("unknown_order", "No such order at this table", 404)
        return {"type": "tap.order", **srv.order_view(o)}

    if method == "GET" and path == "/agent/bill":
        g = authenticate(h, method, path, "bill.read")
        b = srv.bill_view(g["session"])
        return signed({"type": "tap.bill", "grantId": g["grantId"], "seat": b["seat"], "open": b["open"],
                       "orders": [{k: v for k, v in o.items() if k != "placedAt"} | {"placedAt": iso(o["placedAt"])}
                                  for o in b["orders"]],
                       "total": b["total"], "taxIncluded": b["taxIncluded"]})

    # the phone at the table
    if method == "GET" and path == "/api/agents":
        s = srv.get_session(h, allow_closed=True)
        pending = [{"handshakeId": hid, "agent": x["agentLabel"], "fingerprint": tap.fingerprint_words(x["agentKey"].kid),
                    "principal": x["principal"], "scopes": x["scopes"], "spendLimit": x["spendLimit"],
                    "code": x["code"], "expiresAt": iso(x["expiresAt"])}
                   for hid, x in handshakes.items()
                   if x["session"] is s and not x["done"] and x["expiresAt"] > now()]
        return {"agentsAllowed": POLICY["agents"] == "allowed", "pending": pending,
                "grants": [_grant_view(g) for g in grants.values() if g["session"] is s]}

    m = re.fullmatch(r"/api/agents/([\w-]+)/revoke", path)
    if m and method == "POST":
        s = srv.get_session(h, allow_closed=True)
        x = handshakes.get(m.group(1))
        if x and x["session"] is s:
            x["done"] = "denied"
            return {"ok": True, "denied": m.group(1)}
        g = next((g for g in grants.values() if g["grantId"] == m.group(1) and g["session"] is s), None)
        if not g:
            raise srv.ApiError(404, "unknown_agent", "No such agent at this table")
        g["revoked"] = True
        print(f"  >> AGENT    {s['seat']['name']}  revoked {g['agentLabel']} ({g['grantId']})")
        return {"ok": True, "revoked": g["grantId"]}

    return NOT_MINE


def hello(h, srv):
    if POLICY["agents"] != "allowed":
        raise TapError("agents_off", "This shop isn't accepting agents right now", 503)
    env = h.json_body()
    try:
        claimed = env["payload"]["agent"]["key"]
        agent_key = tap.Key.from_public(claimed["public"])
    except (KeyError, TypeError, ValueError):
        raise TapError("bad_hello", "tap.hello must include agent.key.public")
    hello_p = tap.verify(env, agent_key, "tap.hello")  # proves the agent holds the key it claims
    seat = srv.SEATS.get(hello_p.get("seatToken") or "")
    if not seat:
        raise TapError("unknown_seat", "That seat token isn't valid", 404)
    sid = srv.seat_session.get(seat["seatId"])
    s = srv.sessions.get(sid) if sid else None
    if not s or not s["open"]:
        raise TapError("table_not_open", f"Nobody has opened {seat['name']} yet. A person at the table must "
                                         "scan the QR on their phone first; agents can't open tables.", 409)
    req = hello_p.get("request") or {}
    scopes = [x for x in (req.get("scopes") or []) if x in SCOPES]
    spend = min(int(req.get("spendLimit") or POLICY["maxSpendPerGrant"]), POLICY["maxSpendPerGrant"])
    agent = hello_p.get("agent") or {}
    for old in handshakes.values():  # a new hello from the same bot replaces its pending one
        if old["session"] is s and old["agentKey"].kid == agent_key.kid and not old["done"]:
            old["done"] = "superseded"
    hid = "h-" + secrets.token_urlsafe(9)
    code = f"{secrets.randbelow(10**6):06d}"
    hello_hash = tap.digest(hello_p)
    challenge = {"type": "tap.challenge", "handshakeId": hid, "seat": {"id": seat["seatId"], "name": seat["name"]},
                 "helloHash": hello_hash, "nonce": secrets.token_urlsafe(12),
                 "challenge": {"type": "pairing_code", "digits": 6, "expiresAt": iso(now() + CODE_TTL),
                               "attemptsLeft": MAX_ATTEMPTS,
                               "where": f"On the phone that opened {seat['name']}: Bots tab → '{agent.get('name')} "
                                        "wants to connect'. Read the 6-digit code from there."}}
    env_out = signed(challenge)
    handshakes[hid] = {"session": s, "seat": seat, "agentKey": agent_key, "hello": hello_p, "helloHash": hello_hash,
                       "challengeHash": tap.digest(env_out["payload"]), "code": code,
                       "expiresAt": now() + CODE_TTL, "attempts": MAX_ATTEMPTS, "done": None,
                       "agentLabel": f"{agent.get('name', '?')}/{agent.get('version', '?')}",
                       "principal": (hello_p.get("principal") or {}).get("id", "anonymous"),
                       "scopes": scopes, "spendLimit": spend}
    print(f"  >> AGENT    {seat['name']}  {handshakes[hid]['agentLabel']} asks to connect "
          f"(scopes {', '.join(scopes)}; limit ¥{spend:,}) - code shown on the table's phone")
    return env_out


def prove(h, srv):
    env = h.json_body()
    hid = (env.get("payload") or {}).get("handshakeId")
    x = handshakes.get(hid or "")
    if not x:
        raise TapError("unknown_handshake", "No such handshake; start again with /agent/hello", 404)
    p = tap.verify(env, x["agentKey"], "tap.prove")
    if x["done"]:
        raise TapError("handshake_closed", f"This handshake is {x['done']}", 409)
    if x["expiresAt"] < now():
        x["done"] = "expired"
        raise TapError("code_expired", "The pairing code expired; start again", 409)
    if p.get("challengeHash") != x["challengeHash"]:
        raise TapError("challenge_mismatch", "prove doesn't refer to this challenge", 400)
    if not secrets.compare_digest(str(p.get("code", "")), x["code"]):
        x["attempts"] -= 1
        if x["attempts"] <= 0:
            x["done"] = "locked"
            raise TapError("locked", "Too many wrong codes; this handshake is closed", 403)
        raise TapError("wrong_code", f"Wrong code; {x['attempts']} attempt(s) left", 403)
    if not x["session"]["open"]:
        raise TapError("table_closed", "The table checked out meanwhile", 409)
    x["done"] = "granted"
    prove_hash = tap.digest(p)
    token, gid = secrets.token_urlsafe(24), "g-" + secrets.token_urlsafe(9)
    transcript = tap.digest([x["helloHash"], x["challengeHash"], prove_hash])
    grants[token] = {"grantId": gid, "session": x["session"], "agentKey": x["agentKey"], "agentLabel": x["agentLabel"],
                     "principal": x["principal"], "scopes": x["scopes"],
                     "limits": {"spendLimit": x["spendLimit"], "ordersPerHour": POLICY["maxOrdersPerHour"]},
                     "spent": 0, "orderTimes": [], "revoked": False, "transcriptHash": transcript}
    print(f"  >> AGENT    {x['seat']['name']}  {x['agentLabel']} connected ({gid})")
    return signed({"type": "tap.grant", "grantId": gid, "token": token,
                   "seat": {"id": x["seat"]["seatId"], "name": x["seat"]["name"]},
                   "agentKid": x["agentKey"].kid, "principal": x["principal"], "scopes": x["scopes"],
                   "limits": grants[token]["limits"], "expires": "checkout", "transcriptHash": transcript})


def quote(h, g, srv):
    body = h.json_body()
    lines_in = body.get("lines")
    if not isinstance(lines_in, list) or not lines_in:
        raise TapError("bad_quote", "Body must be {lines: [{itemId, qty, options}]}")
    lines = []
    for l in lines_in:
        item = srv.ITEMS.get((l or {}).get("itemId", ""))
        if not item:
            raise TapError("unknown_item", f"No menu item '{(l or {}).get('itemId')}'", 404)
        if item.get("soldOut"):
            raise TapError("sold_out", f"'{item['name']}' is sold out", 409)
        qty = l.get("qty", 1)
        if not isinstance(qty, int) or not 1 <= qty <= srv.MAX_QTY:
            raise TapError("bad_qty", f"qty must be 1-{srv.MAX_QTY}")
        try:
            line = srv.price_line(item, l.get("options") or {}, qty)
        except srv.ApiError as e:
            raise TapError(e.code, e.message, e.status)
        line.pop("lineId", None)
        lines.append(line)
    total = sum(l["subtotal"] for l in lines)
    qid = "q-" + secrets.token_urlsafe(9)
    quotes[qid] = {"grantId": g["grantId"], "lines": lines, "total": total, "expiresAt": now() + QUOTE_TTL,
                   "used": False}
    remaining = g["limits"]["spendLimit"] - g["spent"]
    return signed({"type": "tap.quote", "quoteId": qid, "grantId": g["grantId"], "lines": lines, "total": total,
                   "terms": TERMS, "expiresAt": iso(quotes[qid]["expiresAt"]),
                   "withinLimit": total <= remaining, "remainingLimit": remaining})


def place(h, g, srv):
    qid = h.json_body().get("quoteId")
    q = quotes.get(qid or "")
    if not q or q["grantId"] != g["grantId"]:
        raise TapError("unknown_quote", "Order a quote you got from /agent/quote with this grant", 404)
    if q["used"]:
        raise TapError("quote_used", "This quote was already ordered", 409)
    if q["expiresAt"] < now():
        raise TapError("quote_expired", "Quote expired; get a new one (prices may have changed)", 409)
    if g["spent"] + q["total"] > g["limits"]["spendLimit"]:
        raise TapError("spend_limit", f"Over this grant's limit (¥{g['limits']['spendLimit']:,}; "
                                      f"¥{g['spent']:,} already ordered)", 403)
    g["orderTimes"] = [t for t in g["orderTimes"] if t > now() - 3600]
    if len(g["orderTimes"]) >= g["limits"]["ordersPerHour"]:
        raise TapError("rate_limit", "Too many agent orders this hour", 429)
    for l in q["lines"]:  # re-check stock at commit time
        if srv.ITEMS[l["itemId"]].get("soldOut"):
            raise TapError("sold_out", f"'{l['name']}' sold out since the quote", 409)
    lines = [{**l, "lineId": secrets.token_hex(4)} for l in q["lines"]]
    order = srv.place_order(g["session"], lines, via={"agent": g["agentLabel"], "principal": g["principal"],
                                                      "grantId": g["grantId"]})
    q["used"] = True
    g["spent"] += q["total"]
    g["orderTimes"].append(now())
    return signed({"type": "tap.receipt", "receiptId": "r-" + secrets.token_urlsafe(9), "orderId": order["orderId"],
                   "quoteId": qid, "shop": SHOP, "seat": {"id": g["session"]["seat"]["seatId"],
                                                          "name": g["session"]["seat"]["name"]},
                   "lines": q["lines"], "total": q["total"], "placedAt": iso(order["placedAt"]),
                   "status": "received", "grantId": g["grantId"], "agent": g["agentLabel"],
                   "principal": g["principal"], "transcriptHash": g["transcriptHash"],
                   "terms": TERMS})
