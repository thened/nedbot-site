"""
Personal knowledge store for places you've eaten: which ordering system they use,
their menu with price history, and what you ordered/paid on each visit.

Data:  <TABLEORDER_DATA>/knowledge/places/<slug>.json   (source of truth)
Notes: <TABLEORDER_DATA>/knowledge/notes/<slug>.md      (Markdown + YAML front matter,
       ready to drop into Obsidian / Logseq / any notes vault)

Set TABLEORDER_NOTES to write the Markdown straight into your vault folder instead.
"""
import datetime, json, os, re
from pathlib import Path

import tableorder as T

KROOT = T.ROOT / "knowledge"
PLACES = KROOT / "places"
NOTES = Path(os.environ.get("TABLEORDER_NOTES") or KROOT / "notes")


def today():
    return datetime.date.today().isoformat()


def slugify(s):
    s = re.sub(r"[^\w]+", "-", (s or "").strip().lower(), flags=re.UNICODE).strip("-")
    return s or "unknown-place"


def place_key(system, place_id=None, name=None):
    return slugify(f"{system}-{place_id}" if place_id else f"{system}-{name}")


def load(key):
    p = PLACES / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text("utf-8"))
    return {"key": key, "name": None, "placeId": None, "system": None, "systemName": None, "host": None,
            "firstVisit": None, "lastVisit": None, "visits": [], "menu": {}, "orders": [], "payments": []}


def save(place):
    PLACES.mkdir(parents=True, exist_ok=True)
    (PLACES / f"{place['key']}.json").write_text(json.dumps(place, ensure_ascii=False, indent=2), "utf-8")
    export_md(place)
    return place


def record_visit(key, *, name, place_id, system, system_name, host, visit, guests=None):
    p = load(key)
    p.update({k: v for k, v in {"name": name, "placeId": place_id, "system": system,
                                "systemName": system_name, "host": host}.items() if v})
    if not any(v["visit"] == visit for v in p["visits"]):
        p["visits"].append({"visit": visit, "date": today(), "guests": guests})
    p["firstVisit"] = p["firstVisit"] or today()
    p["lastVisit"] = today()
    return save(p)


def record_menu(key, items):
    """items: [{name, price, category?, options?, soldOut?}]; keeps a price history per item."""
    p = load(key)
    seen_now = set()
    for it in items:
        name = it["name"]
        seen_now.add(name)
        e = p["menu"].setdefault(name, {"category": None, "prices": [], "options": [], "soldOutSeen": [],
                                        "firstSeen": today(), "lastSeen": None})
        e["category"] = it.get("category") or e["category"]
        if it.get("options"):
            e["options"] = it["options"]
        if not e["prices"] or e["prices"][-1]["price"] != it["price"]:
            e["prices"].append({"date": today(), "price": it["price"]})
        if it.get("soldOut") and today() not in e["soldOutSeen"]:
            e["soldOutSeen"].append(today())
        e["lastSeen"] = today()
        e.pop("missingSince", None)
    for name, e in p["menu"].items():  # items that vanished from the menu
        if name not in seen_now and "missingSince" not in e:
            e["missingSince"] = today()
    p["menuCapturedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    return save(p)


def record_order(key, visit, lines, total, order_id=None):
    p = load(key)
    p["orders"].append({"visit": visit, "date": today(), "time": datetime.datetime.now().strftime("%H:%M"),
                        "orderId": order_id, "lines": lines, "total": total})
    return save(p)


def record_payment(key, visit, total, note=""):
    p = load(key)
    p["payments"].append({"visit": visit, "date": today(), "total": total, "note": note})
    return save(p)


def all_places():
    return [json.loads(f.read_text("utf-8")) for f in sorted(PLACES.glob("*.json"))] if PLACES.exists() else []


# ── Markdown export ───────────────────────────────────────────────────────────

def yen(n):
    return f"¥{n:,}" if isinstance(n, (int, float)) else "?"


def export_md(p):
    NOTES.mkdir(parents=True, exist_ok=True)
    current = {n: e for n, e in p["menu"].items() if "missingSince" not in e}
    prices = [e["prices"][-1]["price"] for e in current.values() if e["prices"]]
    spent = sum(x["total"] for x in p["payments"]) or sum(o["total"] for o in p["orders"])
    title = p["name"] or p["key"]
    fm = {
        "type": "place", "name": title, "ordering_system": p["systemName"] or p["system"],
        "system_id": p["system"], "place_id": p["placeId"], "first_visit": p["firstVisit"],
        "last_visit": p["lastVisit"], "visits": len(p["visits"]), "menu_items": len(current),
        "price_range": f"{yen(min(prices))}–{yen(max(prices))}" if prices else None,
        "total_spent": spent or None, "tags": ["place", "food", f"ordering/{p['system']}"],
    }
    out = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items() if v is not None] + ["---", ""]
    out += [f"# {title}", "",
            f"- **Ordering system:** {p['systemName'] or p['system']}" + (f" (`{p['host']}`)" if p["host"] else ""),
            f"- **Visits:** {len(p['visits'])} (first {p['firstVisit']}, last {p['lastVisit']})"]
    if spent:
        out.append(f"- **Spent here:** {yen(spent)}")
    out += ["", f"## Menu (captured {(p.get('menuCapturedAt') or '')[:10] or 'never'})", ""]
    if current:
        out += ["| Item | Category | Price | Options | Notes |", "|---|---|---:|---|---|"]
        for name, e in sorted(current.items(), key=lambda kv: (kv[1]["category"] or "", kv[0])):
            notes = []
            if len(e["prices"]) > 1:
                notes.append("was " + ", ".join(f"{yen(x['price'])} ({x['date']})" for x in e["prices"][:-1]))
            if e["soldOutSeen"]:
                notes.append(f"sold out on {', '.join(e['soldOutSeen'][-3:])}")
            opts = ", ".join(o["name"] + (f" +{yen(o['price'])}" if o.get("price") else "") for o in e["options"])
            out.append(f"| {name} | {e['category'] or ''} | {yen(e['prices'][-1]['price'])} | {opts} | {'; '.join(notes)} |")
    gone = {n: e for n, e in p["menu"].items() if "missingSince" in e}
    if gone:
        out += ["", "Gone from the menu: " + ", ".join(f"{n} (since {e['missingSince']})" for n, e in gone.items())]
    if p["orders"] or p["payments"]:
        out += ["", "## What I ordered", ""]
        for v in p["visits"]:
            os_ = [o for o in p["orders"] if o["visit"] == v["visit"]]
            pay = [x for x in p["payments"] if x["visit"] == v["visit"]]
            if not os_ and not pay:
                continue
            head = f"- **{v['date']}**" + (f" · {v['guests']} guest(s)" if v.get("guests") else "")
            if pay:
                head += f" · paid {yen(sum(x['total'] for x in pay))}"
            out.append(head)
            for o in os_:
                items = ", ".join(f"{l['name']} ×{l.get('qty', 1)}" + (f" ({l['options']})" if l.get("options") else "")
                                  for l in o["lines"])
                out.append(f"  - {o['time']} {items} — {yen(o['total'])}")
    out.append("")
    (NOTES / f"{p['key']}.md").write_text("\n".join(out), "utf-8")
