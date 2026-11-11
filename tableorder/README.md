# tableorder: order from QR table-order sites, with a log on your side

For restaurants that use AirREGI Order (「ご注文、スマホから」 cards). When you're seated,
it opens the table's QR link as a phone would, records every request and response,
pulls out the menu, and keeps a ledger of your orders and bills.

**Rules it follows:**
- Use it only at a table you're actually sitting at.
- It never sends an order or checks out by itself. Any click on text like 注文する, 注文を確定,
  会計 or "place order" is **blocked** unless you add `--confirm-real-order`. A screenshot and
  the page text are saved just before the click.
- It accepts the site's terms screen only when you pass `--agree`.
- Captures go in `tableorder/visits/`, which is gitignored because it holds your table
  token and cookies. Never commit it.

## Setup (PC)

```
pip install playwright
python -m playwright install chromium
```

Optional environment variables: `TABLEORDER_DATA` sets the data folder,
`TABLEORDER_CHROMIUM` points to a browser binary, and `TABLEORDER_HOSTS` adds extra
hosts to record (for example `127.0.0.1` when testing against `order_mock`).

## At the table

```
# 1. Open the table: agree to the terms and set the headcount
python tableorder/tableorder.py open "https://spo.order.airregi.jp/auth/seat?t=...&qrCodeType=2" \
       --label cafe-name --agree --guests 1

# 2. Browse. Each step runs in order, then saves a screenshot and the page text
python tableorder/tableorder.py step <visit> "click:ドリンク" scroll scroll "click:フード" scroll

# 3. Build menu.json from everything the site sent
python tableorder/tableorder.py menu <visit>

# 4. Add to cart (normal clicks), then submit only after you've checked the cart
python tableorder/tableorder.py step <visit> "click:ブレンドコーヒー" "click:カートに追加"
python tableorder/tableorder.py step <visit> "click:注文する" --confirm-real-order

# 5. Record what you paid; the ledger also lists every order/bill API call it captured
python tableorder/tableorder.py ledger --add <visit> 1100 "blend + toast"
python tableorder/tableorder.py ledger
```

The button names above are examples. Real labels vary by shop, so check the screenshot
or `last_page.txt` after each step. Step types are `click:<text>`,
`click-nth:<n>:<text>`, `fill:<label>=<value>`, `scroll`, `back`, `wait:<ms>`,
`shot[:name]`, `text` and `goto:<url>`.

## What each visit folder holds

| file            | contents |
|-----------------|----------|
| `visit.json`    | QR URL, label, headcount, last page, saved sessionStorage |
| `network.jsonl` | every airregi.jp request: method, URL, status, request body, JSON response |
| `menu.json`     | extracted items: name, price, sold out, options |
| `actions.jsonl` | each confirmed submit, with the page text at that moment |
| `NN-*.png`      | screenshots of each step |
| `state.json`    | cookies (keep private) |

Running the same flow against `order_mock` (`TABLEORDER_HOSTS=127.0.0.1`) is a safe way
to check that everything works before you're at a real table.
