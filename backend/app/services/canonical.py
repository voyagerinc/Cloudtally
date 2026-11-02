"""Canonical voucher representation shared by the ERP and the Tally connector.

Both sides are normalized into this shape before hashing, so that the same accounting
content produces the same hash regardless of which system it came from. Hash equality
is what lets the sync engine recognise its own echoes and detect genuine changes.
"""
import hashlib
import json
from decimal import Decimal

TWO = Decimal("0.01")
FOUR = Decimal("0.0001")


def m(value) -> str:
    return f"{Decimal(value).quantize(TWO):f}"


def q(value) -> str:
    return f"{Decimal(value).quantize(FOUR):f}"


def _s(value) -> str:
    return (value or "").strip()


# Tally's implicit defaults: stock without a godown/batch lands in these.
_DEFAULT_GODOWN = "Main Location"


def _godown(value) -> str:
    value = _s(value)
    return "" if value == _DEFAULT_GODOWN else value


def normalize(doc: dict) -> dict:
    """Return the order-independent, formatting-independent content used for hashing and diffing."""
    entries = []
    for e in doc.get("entries", []):
        entries.append({
            "ledger": _s(e["ledger"]),
            "amount": m(e["amount"]),
            "bills": sorted(
                ({"type": _s(b.get("type")), "name": _s(b.get("name")), "amount": m(b["amount"])}
                 for b in e.get("bills") or []),
                key=lambda b: (b["type"], b["name"], b["amount"]),
            ),
            "cost": sorted(
                ({"category": _s(c.get("category")), "centre": _s(c["centre"]), "amount": m(c["amount"])}
                 for c in e.get("cost") or []),
                key=lambda c: (c["category"], c["centre"], c["amount"]),
            ),
        })
    entries.sort(key=lambda e: (e["ledger"], e["amount"]))
    inventory = sorted(
        ({"item": _s(i["item"]), "quantity": q(i["quantity"]), "amount": m(i.get("amount") or 0),
          "godown": _godown(i.get("godown")), "ledger": _s(i.get("ledger")),
          **({"direction": i.get("direction")} if Decimal(str(i["quantity"])) == 0 else {})}
         for i in doc.get("inventory") or []),
        key=lambda i: (i["item"], i["quantity"], i["amount"], i["godown"]),
    )
    return {
        "voucher_type": _s(doc.get("voucher_type")),
        "voucher_number": _s(doc.get("voucher_number")),
        "date": str(doc.get("date") or ""),
        "reference_no": _s(doc.get("reference_no")),
        "party_ledger": _s(doc.get("party_ledger")),
        "narration": " ".join(_s(doc.get("narration")).split()),
        "is_cancelled": bool(doc.get("is_cancelled")),
        "entries": entries,
        "inventory": inventory,
    }


def content_hash(doc: dict) -> str:
    payload = json.dumps(normalize(doc), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff(a: dict, b: dict) -> list[dict]:
    """Field-level differences between two canonical documents (used in the conflict screen)."""
    na, nb = normalize(a), normalize(b)
    out = []
    for key in na:
        if key in ("entries", "inventory"):
            continue
        if na[key] != nb[key]:
            out.append({"field": key, "erp": na[key], "tally": nb[key]})

    for key, label in (("entries", lambda e: e["ledger"]), ("inventory", lambda i: f"{i['item']}@{i['godown']}")):
        la, lb = {}, {}
        for x in na[key]:
            la.setdefault(label(x), []).append(x)
        for x in nb[key]:
            lb.setdefault(label(x), []).append(x)
        for name in sorted(set(la) | set(lb)):
            if la.get(name) != lb.get(name):
                out.append({"field": f"{key}[{name}]", "erp": la.get(name), "tally": lb.get(name)})
    return out
