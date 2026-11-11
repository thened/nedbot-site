# Next: move this into uanapi

This folder (plus `order_mock/`) is a working prototype. The plan is to rebuild it on
uanapi's own building blocks rather than copy it over.

1. **AirREGI browser recipe:** `uanapi/recipes/browser/airregi-order.yaml`, a draft in
   the style of `mcdonalds-jp-bigmac`.
   - Seen so far: the terms screen (同意してはじめる), then the headcount screen (N 名 and
     人数を確定して、注文へ進む).
   - Everything after that: `handoff` steps, `confirm_spend` before 注文する, and a fix on
     the first real run.
   - Store id: the launch screen POSTs `api.beta.spo.order.airregi.jp/api/beta/store {"rs": ...}`.
2. **Restaurants as parties:** a party card per place (`ordering_system`), the menu as an
   entry with price history, and each order as a `purchase` entry (`parties.record_purchase`).
3. **Protocol section "presence: a door at a table":** map `PROTOCOL.md` here onto uanapi
   primitives and drop the separate Ed25519 `TAP` scheme.
   - knock = the seat token plus the uanapi signed request
   - presence = a pairing code on the phone that opened the table
   - door = scoped to that table, closes at checkout
   - order = a priced job (quote, spend ask, order)
   - receipt = a `purchase` notice
   - Port `test_protocol.py` (36 checks) to the new design.
4. **A uanapi skill** (playbooks for an AI using the uanapi tools), with "eating out" as
   the first playbook.

Read uanapi's `CLAUDE.md` before starting. It's long and full of conventions.
