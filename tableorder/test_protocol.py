"""
End-to-end test of the Table Agent Protocol against the mock shop.

    python tableorder/test_protocol.py

Starts order_mock/server.py on a spare port with throwaway data, plays both the person at
the table (phone API) and the bot (native.py), and checks the happy path plus the attacks
the handshake is meant to stop. Needs: pip install cryptography
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TMP = Path(tempfile.mkdtemp(prefix="tap-test-"))
os.environ["TABLEORDER_DATA"] = str(TMP / "data")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import tap  # noqa: E402
import native as N  # noqa: E402
import knowledge as K  # noqa: E402
from order_mock.bot_client import OrderClient  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))


def expect_err(name, fn, exc, contains=""):
    try:
        fn()
    except exc as e:
        check(name, contains in str(e), f"got: {e}")
        return e
    check(name, False, "no error raised")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_shop(port, key_file):
    env = {**os.environ, "ORDER_MOCK_PORT": str(port), "ORDER_MOCK_STEP_SECS": "0", "ORDER_MOCK_SHOP_KEY": str(key_file)}
    p = subprocess.Popen([sys.executable, str(REPO / "order_mock" / "server.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); return p
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("shop didn't start")


def phone_agents(base, sid):
    req = urllib.request.Request(base + "/api/agents", headers={"X-Session-Id": sid})
    return json.loads(urllib.request.urlopen(req).read())


def phone_post(base, sid, path):
    req = urllib.request.Request(base + path, data=b"", method="POST", headers={"X-Session-Id": sid})
    return json.loads(urllib.request.urlopen(req).read())


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    shop = start_shop(port, TMP / "shop.pem")
    try:
        seats = OrderClient(base).seats()
        url4 = next(s["qrUrl"] for s in seats if s["seatId"] == 4)
        url5 = next(s["qrUrl"] for s in seats if s["seatId"] == 5)

        print("Discovery & trust")
        found = N.discover(url4)
        check("shop descriptor found and self-signature verifies", found and found["payload"]["shop"]["name"] == "NedBot Café")
        check("descriptor names the ordering system", found["payload"]["system"]["id"] == "nedbot-mock")
        expect_err("first contact needs a human 'yes, I'm here'", lambda: N.NativeClient.connect(url4), N.TrustNeeded, "First visit")

        print("Presence")
        expect_err("agent can't open a table nobody is sitting at",
                   lambda: N.NativeClient.connect(url4, trust_shop=True), N.NativeError, "table_not_open")
        phone = OrderClient(base); phone.join(qr_url=url4)  # the person opens the table on their phone
        c = N.NativeClient.connect(url4, label="cafe", guests=1, trust_shop=True, spend_limit=3000)
        check("hello accepted, challenge points to the table's phone", "phone" in c.state["challenge"]["challenge"]["where"])
        pend = phone_agents(base, phone.session_id)["pending"]
        check("phone shows exactly one pending bot with a 6-digit code", len(pend) == 1 and len(pend[0]["code"]) == 6)
        check("phone shows the bot's name, limit and key fingerprint",
              pend[0]["agent"] == "NedBot/0.1" and pend[0]["spendLimit"] == 3000 and pend[0]["fingerprint"])
        code = pend[0]["code"]
        expect_err("calls before pairing are refused", c.menu, N.NativeError, "Not paired")
        expect_err("wrong code is refused and counted", lambda: c.pair("000000"), N.NativeError, "4 attempt")
        g = c.pair(code)
        check("grant is signed, bound to our key and transcript", g["agentKid"] == N.agent_key().kid)
        check("grant limits = min(requested, shop policy)", g["limits"]["spendLimit"] == 3000)
        expect_err("a used code can't be replayed for a second grant", lambda: c.pair(code), N.NativeError, "handshake_closed")

        print("Ordering")
        items = c.menu()
        check("signed menu read natively", len(items) == 10 and any(i["soldOut"] for i in items))
        expect_err("sold-out item rejected", lambda: c.add("Egg Sandwich"), N.NativeError, "sold out")
        expect_err("missing required option rejected and not left in cart", lambda: c.add("latte"), N.NativeError, "option_required")
        check("cart still empty after rejected adds", c.state["cart"] == [])
        c.add("Cafe Latte", 2, ["iced", "large"]); c.add("Retro Pudding")
        e = expect_err("submit without confirmation returns a signed quote", c.submit, N.NeedsConfirmation, "¥1,900")
        r = c.submit(confirm=True)
        check("confirmed submit orders exactly the approved quote", r["receipt"]["quoteId"] == e.quote["quoteId"]
              and r["receipt"]["total"] == 1900)
        check("receipt carries the handshake transcript hash", r["receipt"]["transcriptHash"] == c.state["transcriptHash"])
        phone.add("oj"); phone.submit()
        b = c.bill()
        check("bot and phone orders share one bill", b["total"] == 2350 and len(b["orders"]) == 2)
        check("bot order is labelled as an agent order", any(o["via"] == "NedBot/0.1" for o in b["orders"]))
        c.add("Curry Rice"); c.add("Napolitan")
        e = expect_err("quote over the grant's spend limit is flagged", c.submit, N.NeedsConfirmation, "over this bot's remaining limit")
        expect_err("shop enforces the spend limit at order time", lambda: c.submit(confirm=True), N.NativeError, "spend_limit")
        c.clear()

        print("Attacks")
        tok = c.state["token"]
        st, body = N.http("GET", base + "/agent/menu", headers={"Authorization": f"TAP {tok}"})
        check("stolen token without the agent's key is useless", st == 401, body)
        h = tap.sign_request(c.key, "GET", "/agent/menu", b"", tok)
        st1, _ = N.http("GET", base + "/agent/menu", headers=h)
        st2, body = N.http("GET", base + "/agent/menu", headers=h)
        check("exact request replay is refused", st1 == 200 and st2 == 401 and body.get("code") == "replay", body)
        h = tap.sign_request(c.key, "POST", "/agent/quote", b'{"lines":[]}', tok)
        st, body = N.http("POST", base + "/agent/quote", b'{"lines":[{"itemId":"cake","qty":9}]}', headers=h)
        check("body tampering breaks the request signature", st == 401 and body.get("code") == "bad_request_signature", body)
        evil = tap.Key.generate()
        h = tap.sign_request(evil, "GET", "/agent/menu", b"", tok)
        st, _ = N.http("GET", base + "/agent/menu", headers=h)
        check("another key can't use our grant", st == 401)
        q_env = c._call("POST", "/agent/quote", {"lines": [{"itemId": "cake", "qty": 1}]}, expect="tap.quote")
        st, body = N.http("POST", base + "/agent/orders", tap.canonical({"quoteId": q_env["quoteId"]}),
                          headers={**tap.sign_request(c.key, "POST", "/agent/orders",
                                                      tap.canonical({"quoteId": q_env["quoteId"]}), tok)})
        forged = json.loads(json.dumps(body)); forged["payload"]["total"] = 1
        expect_err("forged receipt fails shop-signature check", lambda: tap.verify(forged, c.shop_key, "tap.receipt"),
                   tap.TapError, "bad_signature")
        ok, bad = c.verify_transcript()
        check("full transcript re-verifies", ok > 5 and not bad, bad)

        print("Human control")
        gid = c.state["grant"]["grantId"]
        phone_post(base, phone.session_id, f"/api/agents/{gid}/revoke")
        expect_err("revoked on the phone: bot is cut off immediately", c.bill, N.NativeError, "grant_revoked")
        phone2 = OrderClient(base); phone2.join(qr_url=url5)
        c5 = N.NativeClient.connect(url5, trust_shop=True)
        pend = phone_agents(base, phone2.session_id)["pending"]
        phone_post(base, phone2.session_id, f"/api/agents/{pend[0]['handshakeId']}/revoke")
        expect_err("person can deny a pairing request", lambda: c5.pair(pend[0]["code"]), N.NativeError, "denied")
        c5 = N.NativeClient.connect(url5, trust_shop=True)
        c5.pair(phone_agents(base, phone2.session_id)["pending"][0]["code"])
        phone2.checkout()
        expect_err("checkout ends the grant", c5.menu, N.NativeError, "grant_expired")

        print("Shop key change")
        shop.terminate(); shop.wait()
        shop = start_shop(port, TMP / "other-shop.pem")  # same address, different key
        phone3 = OrderClient(base); phone3.join(qr_url=url4)
        expect_err("changed shop key stops everything until a human re-confirms",
                   lambda: N.NativeClient.connect(url4), N.TrustNeeded, "CHANGED")

        print("Knowledge")
        p = K.load(c.place_key)
        check("place records system, pinned key and menu", p["system"] == "nedbot-mock" and p["shopKey"] and len(p["menu"]) == 10)
        check("place records the signed order", p["orders"] and p["orders"][0]["total"] == 1900)
        note = (K.NOTES / f"{c.place_key}.md").read_text("utf-8")
        check("Markdown note written", "NedBot Café" in note and "ordering_system" in note)
    finally:
        shop.terminate()

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
