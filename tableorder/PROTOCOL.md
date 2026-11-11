# Table Agent Protocol: draft `table-agent/0.1`

How a customer's agent (your bot) and a shop's agent (the ordering system) talk to each
other directly, with no screen-reading, and how they come to trust each other.
Reference implementations: the shop side is in `order_mock/agent.py`, the customer side
is in `tableorder/native.py`, and the shared signing code is in `tableorder/tap.py`.

> Status: a working draft, exercised end to end against the mock. Nothing here is an
> AirREGI or Recruit API. It's a proposal for how such systems could talk to agents.

## 1. Principles

1. **The QR is the door, not the key.** Table QR links are static and public, so knowing
   one proves nothing. Trust starts with **proof of presence**: something only a person at
   the table can see right now.
2. **Both sides say they're agents.** The customer agent identifies itself as a bot. The
   shop marks bot orders on the kitchen ticket, and a shop agent marks its suggestions
   (upsells) as suggestions.
3. **Least privilege.** The customer agent asks for specific scopes and a spending limit.
   The shop grants the overlap with its own policy, for one table, until checkout.
4. **A human commits money.** The customer agent gets the shop's *signed quote*, shows it
   to its human, and orders only that quote. Shop policy can allow less than the customer
   agent wants, never more. A customer agent's own confirm rule always applies.
5. **Shared, verifiable records.** Every step of the handshake and every order is signed.
   Both sides keep the same transcript, so the customer's ledger and the shop's register
   agree by construction.
6. **Share as little as possible.** The shop learns a pairwise pseudonym (different for
   every shop) and the order, nothing else, unless the human chooses to share more.
7. **Graceful fallback.** No `/.well-known/table-agent` means no native support, so the
   client falls back to screen recipes (`recipe.py`) behind the same interface.

## 2. Encoding

Signed messages are envelopes:

```json
{"payload": {"type": "tap.grant", "protocol": "table-agent/0.1", "iat": 1762834000, "...": "..."},
 "sig": {"alg": "Ed25519", "kid": "ed25519:Xq3...", "value": "<base64url signature>"}}
```

- The signature covers the **canonical JSON** of `payload`: keys sorted, no whitespace,
  UTF-8.
- `kid` = `"ed25519:"` + the first 16 characters of the base64url SHA-256 of the raw
  public key.
- Hashes are written as `"sha256:<base64url>"` over canonical JSON (or raw bytes for bodies).
- Errors are plain, unsigned JSON: `{"type": "tap.error", "code": "...", "message": "..."}`.

## 3. Discovery

`GET /.well-known/table-agent` on **the same origin as the QR link** returns a signed `tap.shop`:

```json
{"type": "tap.shop",
 "shop": {"id": "nedbot-cafe", "name": "NedBot Café"},
 "system": {"id": "nedbot-mock", "name": "NedBot mock table order", "vendor": "nedbot"},
 "key": {"alg": "Ed25519", "kid": "ed25519:...", "public": "..."},
 "endpoints": {"hello": "/agent/hello", "prove": "/agent/prove", "menu": "/agent/menu",
               "quote": "/agent/quote", "orders": "/agent/orders", "bill": "/agent/bill"},
 "policy": {"agents": "allowed", "presence": ["pairing_code"],
            "scopes": ["menu.read", "order.quote", "order.place", "bill.read"],
            "maxSpendPerGrant": 10000, "maxOrdersPerHour": 6,
            "humanConfirmation": "customer-side", "grantExpires": "checkout",
            "disclosure": "required"},
 "terms": {"currency": "JPY", "pricesIncludeTax": true, "taxRate": 0.10,
           "serviceCharge": 0, "cancellation": "until-preparing", "payment": "at-register"}}
```

**How the customer agent decides to trust the shop:**
- **Origin check:** the descriptor must come from the QR's origin. A redirect or another
  origin is rejected, which defends against a swapped sticker sending you somewhere else.
- **First visit (trust on first use):** the agent shows its human the shop name and a
  short key fingerprint (e.g. `3F-A1-07-C2`) and asks "is this where you are?". On "yes",
  it pins the key for that place.
- **Later visits:** the key must match the pin. A changed key stops everything until the
  human accepts it again.
- *(Future)* the shop key could be anchored in DNS, or signed by the ordering-system
  vendor's key, so first contact doesn't rely on trust on first use.

## 4. The handshake

```
 customer agent                                   shop agent                    phone at table
      |  GET /.well-known/table-agent  ------------->  |                              |
      |  <------------------------------- tap.shop (signed)                           |
      |  human: "is this NedBot Café? 3F-A1-07-C2"  -> yes, pin key                   |
      |  POST /agent/hello  tap.hello (signed by agent) ->                            |
      |  <--------- tap.challenge (signed): "pairing code on the table's phone"       |
      |                                                 |  -- shows code 482913 ----> |
      |  human reads code 482913 off the phone                                        |
      |  POST /agent/prove  tap.prove {code} (signed) -> |                            |
      |  <------------------ tap.grant (signed): token, scopes, limits                |
      |                                                 |  -- "NedBot connected [Revoke]" -> |
```

**`tap.hello`** (customer agent to shop, signed with the agent's key):

```json
{"type": "tap.hello", "seatToken": "<t from the QR>",
 "agent": {"name": "NedBot", "version": "0.1", "key": {"alg": "Ed25519", "kid": "...", "public": "..."},
           "descriptor": "https://nedbot.site/.well-known/table-agent-client.json"},
 "principal": {"id": "p-9Qe4...", "kind": "pairwise-pseudonym"},
 "intent": {"partySize": 1},
 "request": {"scopes": ["menu.read", "order.quote", "order.place", "bill.read"], "spendLimit": 5000},
 "confirmation": "human-per-order",
 "nonce": "<random>"}
```

**`tap.challenge`** (shop, signed) binds to the hello:
`{"handshakeId", "seat": {"id": 4, "name": "Table 4"}, "helloHash", "challenge": {"type": "pairing_code", "where": "...", "digits": 6, "expiresAt", "attemptsLeft": 5}, "nonce"}`.
The table must already be **opened by a person** (on a phone or by staff). An agent can't
open a table itself, so presence always starts with a human.

**`tap.prove`** (agent, signed): `{"handshakeId", "challengeHash", "code"}`. Codes are
6 digits, single use, expire after 5 minutes, and a handshake allows 5 attempts.

**`tap.grant`** (shop, signed):

```json
{"type": "tap.grant", "grantId": "g-...", "token": "<opaque>", "seat": {"id": 4, "name": "Table 4"},
 "agentKid": "ed25519:...", "principal": "p-9Qe4...",
 "scopes": ["menu.read", "order.quote", "order.place", "bill.read"],
 "limits": {"spendLimit": 5000, "ordersPerHour": 6}, "expires": "checkout",
 "transcriptHash": "sha256:..."}
```

`transcriptHash` = hash of `[helloHash, challengeHash, proveHash]`. Both sides store it,
and every receipt repeats it.

The phone that opened the table shows connected agents, and **the human can revoke any
grant at any time**. Checkout ends every grant at the table.

## 5. Authenticated calls

After the grant, every request carries:

```
Authorization: TAP <token>
X-TAP-Ts:      <unix seconds>
X-TAP-Nonce:   <random, at least 8 chars>
X-TAP-Sig:     sign(agentKey, canonical({"m": METHOD, "p": PATH, "ts": TS, "n": NONCE, "b": sha256(body), "t": token}))
```

The nonce matters because Ed25519 signatures are deterministic: without it, two identical
requests in the same second would look like a replay. The shop rejects requests with a
timestamp more than 120 seconds off its clock, a signature it has already seen (replay), a missing scope, a revoked or expired grant, or a
broken limit. A stolen token is useless without the agent's private key.

## 6. Native operations

| call | scope | returns |
|---|---|---|
| `GET /agent/menu` | `menu.read` | signed `tap.menu`: items with ids, categories, prices, options, `soldOut` |
| `POST /agent/quote` `{lines: [{itemId, qty, options: {optId: choiceId}}]}` | `order.quote` | signed `tap.quote`: priced lines, total, terms, `expiresAt` (5 min). Has no side effects |
| `POST /agent/orders` `{quoteId}` | `order.place` | signed `tap.receipt`: receipt id, order id, lines, total, grant, transcript hash |
| `GET /agent/orders/<orderId>` | `bill.read` | status (`received`, `preparing`, `served`, `cancelled`) |
| `GET /agent/bill` | `bill.read` | signed `tap.bill`: every order at the table, total, open or closed |

**Two-phase ordering:** quote, then the customer-side human confirms, then the order is
placed. The human approves an exact, signed price list; the shop must honour the quote
until it expires; and the receipt points back to the quote it fulfils. The shop checks
`spendLimit` (the sum of the grant's orders) and `ordersPerHour` when an order is placed.

Payment stays human-only in 0.1 (`payment: "at-register"`). No scope moves money.

## 7. What the shop sees

Kitchen tickets for agent orders read
`Table 4 · #4-0007 · via NedBot/0.1 (agent, pairwise id p-9Qe4…)`. The shop can turn
agents off, tighten limits, or revoke a grant without affecting the people at the table.

## 8. Customer agent descriptor (optional)

A bot can publish `/.well-known/table-agent-client.json` on its own domain. It lists its
name, the protocols it speaks, its public key, and its promises (`"humanConfirmation":
"always"`, `"principal": "pairwise-pseudonym"`). If `tap.hello` points to it and the key
matches, the shop knows *which* bot it's dealing with, anchored to a domain.
`python tableorder/native.py identity` prints this file for your key.

## 9. Open questions

- Anchoring shop keys to the vendor (AirREGI-signed shop keys) so first contact isn't trust on first use.
- Presence without a phone: NFC tags at the table, a code on a table display, a code read out by staff.
- Splitting the bill and multiple agents at one table (one grant per agent, already supported).
- Shop-agent suggestions: a `tap.suggestion` type the customer agent may show, but never auto-accept.
- Allergens and ingredients as structured, signed data.
