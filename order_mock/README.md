# Mock table-order server (for bot testing)

A local stand-in for "scan the QR, order from your phone" restaurant systems, so
NedBot can practise the full ordering flow without sending anything to a real shop.
It is a generic mock, not a copy of any vendor's real API. Everything is in memory and
resets when you restart it.

Requires only Python 3 (stdlib, no pip installs).

```
python order_mock/server.py
```

It prints one QR-style link per table, e.g.

```
Table 4   http://127.0.0.1:8890/auth/seat?t=<seat-token>&qrCodeType=2
```

Open `http://127.0.0.1:8890/` in a browser to pick a table and order by hand on the
phone-style page. Kitchen tickets print in the server console.

| env var                | default     | meaning                                         |
|------------------------|-------------|-------------------------------------------------|
| `ORDER_MOCK_PORT`      | `8890`      | port                                            |
| `ORDER_MOCK_HOST`      | `127.0.0.1` | bind address (`0.0.0.0` to scan from your phone on LAN) |
| `ORDER_MOCK_TABLES`    | `8`         | number of tables                                |
| `ORDER_MOCK_STEP_SECS` | `20`        | seconds per status step; `0` keeps orders at `received` |

Seat tokens are fixed (uuid5), so test scripts can hard-code them.

## From the bot (Python)

```python
from order_mock.bot_client import OrderClient, OrderError, summary

c = OrderClient("http://127.0.0.1:8890")
c.join(qr_url)                                   # or c.join(seat_token="...")
order = c.order_text("2 iced latte large, pudding x2, toast ogura")
print(summary(order))   # #4-0001 [received] Cafe Latte (Iced/Large) x2, ... — ¥2,..
c.orders(); c.bill(); c.checkout()
```

`order_text` turns chat text into cart lines. Use commas, newlines or "and" between
items, and `2 latte`, `latte x2` or `x2 latte` for quantities. Option words such as
`iced` or `large` are matched by name, and a required option falls back to its first
choice. Errors raise `OrderError`, which has `.code` and `.message`.

CLI for quick checks:

```
python order_mock/bot_client.py seats | menu
python order_mock/bot_client.py order 4 "iced latte, 2 pudding"
python order_mock/bot_client.py status|bill|checkout 4
```

## HTTP API

Send the session id in the `X-Session-Id` header (or as `?session=`).

| method | path                          | body / notes |
|--------|-------------------------------|--------------|
| GET    | `/auth/seat?t=<token>`        | browser entry: opens the table session, then redirects to the UI |
| POST   | `/api/session`                | `{"seatToken"}` or `{"qrUrl"}` → `{"sessionId","seat","created"}`; rejoins the table's open session if there is one |
| GET    | `/api/session`                | seat, open flag, cart |
| GET    | `/api/menu`                   | categories, items, options, `soldOut` |
| GET    | `/api/cart`                   | `{"items","total"}` |
| POST   | `/api/cart/items`             | `{"itemId","qty":1-10,"options":{"temp":"iced"}}` |
| PATCH  | `/api/cart/items/<lineId>`    | `{"qty"}` (0 removes it) |
| DELETE | `/api/cart/items/<lineId>`, `/api/cart` | remove one line / clear the cart |
| POST   | `/api/orders`                 | submits the cart and returns the order |
| GET    | `/api/orders`                 | orders with live status `received → preparing → served` |
| POST   | `/api/orders/<id>/cancel`     | only allowed while `received` |
| GET    | `/api/bill`                   | totals (prices include 10% tax) |
| POST   | `/api/checkout`               | closes the table; scan again to start a new session |
| GET    | `/admin/seats`, `/admin/orders` | seat tokens and QR links; kitchen view of every order |
| POST   | `/admin/reset`                | wipes all state |

Errors look like `{"error": "<code>", "message": "..."}`. Codes include
`unknown_seat`, `no_session`, `session_closed`, `unknown_item`, `sold_out`
(Egg Sandwich is always sold out so you can test this), `option_required`,
`bad_choice`, `bad_qty`, `empty_cart` and `too_late`.
