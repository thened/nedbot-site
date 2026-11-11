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

## Native: when the shop has its own agent

If the QR's site serves `/.well-known/table-agent`, the bot skips screen-reading and
talks to the shop's agent directly over the **Table Agent Protocol** (draft
`table-agent/0.1`, spec in [`PROTOCOL.md`](PROTOCOL.md)). The handshake is where trust is
established:

1. **Is this really the shop?** On a first visit you're shown the shop name and a key
   fingerprint to confirm (`--trust-shop`). After that the key is pinned, and a changed key
   stops everything.
2. **Is the bot really at the table?** The table must already be opened by a person, and the
   bot only gets in with a 6-digit code shown on that person's phone (Bots tab), which also
   shows who's asking, what it wants, and its spending limit.
3. **What may it do?** It gets a signed permission for that one visit: listed actions only,
   a spending cap, and an end at checkout. The phone can revoke it at any time.
4. **Every order:** a signed quote, then your OK, then a signed receipt for exactly that
   quote. Receipts go straight into your ledger and place notes.

```
python tableorder/native.py connect "<qr url>" --label cafe --guests 1 [--trust-shop]
python tableorder/native.py pair 819023                 # code from the table's phone
python tableorder/native.py menu
python tableorder/native.py add "Cafe Latte" --qty 2 --opt iced --opt large
python tableorder/native.py submit                      # shows the signed quote
python tableorder/native.py submit --confirm-real-order # orders exactly that quote
python tableorder/native.py bill | transcript | paid 1900
python tableorder/native.py identity --descriptor-url https://nedbot.site/.well-known/table-agent-client.json
```

`native.connect_any(qr_url, ...)` picks the native protocol when the shop offers it and
the screen recipe otherwise. Both return clients with the same `menu`, `add`, `cart`,
`submit` and `bill` calls, so the bot doesn't care which it gets.

Your bot's signing key and pseudonym secret are created on first use in
`visits/identity/` (gitignored). Back that folder up, because the key is how shops
recognise your bot.

`python tableorder/test_protocol.py` runs 36 end-to-end checks against the mock: the
handshake, ordering, limits, revocation, a shop key change, and attacks (stolen token,
replay, tampered body, foreign key, forged receipt).

## Recipes: the screens as an API

Until these places have APIs, what you can see and tap *is* the API. A **recipe**
(`recipes/<system>.yaml`) writes down how to do each operation on one ordering system.
The operations are `open`, `menu`, `add_item`, `cart`, `submit` and `bill`. Steps target
the page's **accessibility tree** (roles and accessible names, the same structure a screen
reader uses), so it's automation as accessibility, not pixel-scraping.

```
python tableorder/recipe.py open "<qr url>" --label "cafe name" --agree --guests 1
python tableorder/recipe.py menu                      # reads the menu and updates your knowledge store
python tableorder/recipe.py add "Cafe Latte" --qty 2 --opt Iced --opt Large
python tableorder/recipe.py cart
python tableorder/recipe.py submit                    # prints the cart and asks for your OK
python tableorder/recipe.py submit --confirm-real-order
python tableorder/recipe.py bill
python tableorder/recipe.py paid 1900 --note "split with A"
python tableorder/recipe.py places                    # every place, its system, menu size, last visit
python tableorder/recipe.py recipes                   # which operations are verified per system
```

For the bot, the same operations are available from Python:

```python
from recipe import RecipeClient, NeedsConfirmation
with RecipeClient.open_table(qr_url, label="cafe", guests=1, agree=True) as c:
    c.menu(); c.add("Cafe Latte", 2, ["Iced"])
    try: c.submit()
    except NeedsConfirmation as e: ask_human(e.summary)   # then c.submit(confirm=True)
```

**Safety is built into the runner:** a step marked `!`, or any click whose name looks
like ordering or paying, never runs without confirmation, even if a recipe is wrong.
When a step can't find what it expects, it stops, saves a screenshot, and tells you the
recipe needs updating.

| recipe | status |
|---|---|
| `nedbot-mock` | fully verified against `order_mock` |
| `airregi-order` | draft: terms and headcount screens seen; the rest is finished from the first real capture |

## Knowledge store: places you've been

Each `open`/`menu`/`submit`/`paid` updates `visits/knowledge/`:

- `places/<slug>.json`: the place, its ordering system, visits, the menu with **price history**
  (items that change price, sell out, or disappear), and orders and payments
- `notes/<slug>.md`: a Markdown note with YAML front matter (`type: place`,
  `ordering_system`, `price_range`, `total_spent`, tags) for Obsidian, Logseq or similar

Set `TABLEORDER_NOTES=/path/to/vault/Places` to write the notes straight into your vault.

## Setup (PC)

```
pip install playwright pyyaml cryptography
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
