"""Fixed assets & depreciation inside Tally Cloud.

The Fixed Assets page (static/fixed-assets.html, adapted from the stand-alone Fixedassets app) talks to
these calls. They answer from Tally Cloud's own books (already synced from Tally) instead of asking Tally,
sign-in is Tally Cloud's, each company's asset register is kept in the database, and the depreciation
journal becomes a Journal voucher here, which goes to Tally with the sync like any other voucher.
Shapes follow the stand-alone app's server.py so its page works unchanged."""
import uuid
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.core.security import Perm, allowed_company_ids, has_perm
from app.models import Company, Ledger, LedgerGroup, Voucher, VoucherEntry, VoucherType
from app.models.mis import FixedAssetState
from app.models.base import utcnow
from app.services.accounting import vouchers as engine

router = APIRouter(prefix="/fixed-assets", tags=["fixed assets"])
POSTED = "posted"


def _companies(db, user) -> list[Company]:
    ids = allowed_company_ids(db, user)
    stmt = select(Company).order_by(Company.name)
    if ids is not None:
        stmt = stmt.where(Company.id.in_(ids))
    return [c for c in db.scalars(stmt) if has_perm(db, user, c.id, Perm.REPORTS_READ) and _licensed(db, c.id)]


def _licensed(db, company_id) -> bool:
    """Fixed assets is a module of the customer's licence (ticked on the licence server)."""
    from app.licensing.enforce import entitlement_for
    return entitlement_for(db, company_id).allows_module("fixed_assets")


def _company(db, user, name: str, perm: str = Perm.REPORTS_READ) -> Company:
    """The page names companies; the user's own company of that name."""
    c = next((c for c in _companies(db, user) if c.name == name), None) or \
        next((c for c in _companies(db, user) if c.name.lower().strip() == (name or "").lower().strip()), None)
    if c is None:
        if any(c.name.lower().strip() == (name or "").lower().strip() and not _licensed(db, c.id)
               for c in db.scalars(select(Company))):
            raise HTTPException(403, "Your licence does not include Fixed assets & depreciation. Contact Voyager to add it.")
        raise HTTPException(404, f"Company '{name}' not found")
    authorize(db, user, c.id, perm)
    return c


def _tally_sign(dr_positive: Decimal) -> float:      # the page keeps Tally's sign: negative = debit
    return float(-dr_positive)


# ------------------------------------------------------------------ session and companies

@router.get("/auth")
def auth(user: CurrentUser):
    return {"setup": False, "user": user.username}


@router.post("/logout")
def logout():
    return {"ok": True}


@router.get("/status")
def status(db: DB, user: CurrentUser):
    """'Tally' for the page is Tally Cloud: its companies are always available."""
    return {"connected": True, "settings": {},
            "companies": [{"name": c.name, "startingFrom": str(c.books_from), "booksFrom": str(c.books_from),
                           "id": str(c.id)} for c in _companies(db, user)]}


@router.get("/users")
def users(user: CurrentUser):
    return {"me": user.username, "users": [{"name": user.username}]}


@router.post("/users")
@router.post("/password")
@router.post("/settings")
def managed_in_tally_cloud():
    raise HTTPException(400, "Users, passwords and the Tally connection are managed in Tally Cloud")


# ------------------------------------------------------------------ saved asset registers

def _saved_row(st: dict) -> dict:
    return {"company": st.get("company"), "savedAt": st.get("savedAt"), "savedBy": st.get("savedBy"),
            "source": st.get("source"), "from": st.get("from"), "to": st.get("to"), "fetchedAt": st.get("fetchedAt"),
            "assetCount": len([a for a in st.get("assets", []) if not a.get("excluded")])}


@router.get("/saved")
def saved(db: DB, user: CurrentUser):
    names = {c.id: c.name for c in _companies(db, user)}
    rows = db.scalars(select(FixedAssetState).where(FixedAssetState.company_id.in_(list(names) or [None])))
    out = [_saved_row({**r.state, "company": names[r.company_id]}) for r in rows]
    return {"saved": sorted(out, key=lambda s: s.get("savedAt") or "", reverse=True)}


@router.get("/state")
def get_state(company: str, db: DB, user: CurrentUser):
    c = _company(db, user, company)
    row = db.get(FixedAssetState, c.id)
    return {"state": {**row.state, "company": c.name} if row else None}


class StateIn(BaseModel):
    company: str
    state: dict


@router.post("/state")
def save_state(body: StateIn, db: DB, user: CurrentUser):
    c = _company(db, user, body.company, Perm.VOUCHER_CREATE)
    st = dict(body.state)
    st["savedAt"] = datetime.now().isoformat(timespec="seconds")
    st["savedBy"] = user.username
    row = db.get(FixedAssetState, c.id)
    if row is None:
        row = FixedAssetState(company_id=c.id)
        db.add(row)
    row.state, row.saved_by, row.saved_at = st, user.username, utcnow()
    db.commit()
    return {"ok": True, "savedAt": st["savedAt"]}


# ------------------------------------------------------------------ fixed asset ledgers and their vouchers

def _groups_under(groups: dict, roots: set[str]) -> set:
    """Ids of the primary groups named in roots and every group under them."""
    found = {gid for gid, g in groups.items() if g.parent_id is None and g.name in roots}
    changed = True
    while changed:
        changed = False
        for gid, g in groups.items():
            if gid not in found and g.parent_id in found:
                found.add(gid)
                changed = True
    return found


def fixed_asset_data(db, c: Company, f: date, t: date, books: date) -> dict:
    groups = {g.id: g for g in db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == c.id))}
    fa = _groups_under(groups, {"Fixed Assets"})
    exp = _groups_under(groups, {"Indirect Expenses", "Direct Expenses"})
    ledgers = list(db.scalars(select(Ledger).where(Ledger.company_id == c.id).order_by(Ledger.name)))
    fa_ledgers = [led for led in ledgers if led.group_id in fa]
    names = {led.id: led.name for led in ledgers}

    # Every posted voucher line of the fixed-asset ledgers, with the other ledgers of the same voucher.
    rows = db.execute(
        select(VoucherEntry.ledger_id, VoucherEntry.amount, Voucher.id, Voucher.voucher_date, Voucher.voucher_number,
               Voucher.narration, VoucherType.name)
        .join(Voucher, Voucher.id == VoucherEntry.voucher_id).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
        .where(Voucher.company_id == c.id, Voucher.status == POSTED, VoucherType.affects_accounts.is_(True),
               VoucherEntry.ledger_id.in_([led.id for led in fa_ledgers] or [None]))).all()
    vids = {r[2] for r in rows}
    others: dict = {}
    for vid, lid in db.execute(select(VoucherEntry.voucher_id, VoucherEntry.ledger_id)
                               .where(VoucherEntry.voucher_id.in_(list(vids) or [None]))):
        others.setdefault(vid, set()).add(names.get(lid))
    per_ledger: dict = {}
    for lid, amt, vid, d, number, narration, vtype in rows:
        v = per_ledger.setdefault(lid, {}).setdefault(vid, {
            "date": str(d), "vchType": vtype, "vchNo": number or "", "narration": narration or "", "dr": Decimal(0),
            "others": sorted(n for n in others.get(vid, ()) if n and n != names[lid])})
        v["dr"] += amt
    f_s, t_s, b_s = str(f), str(t), str(books)
    assets = []
    for led in fa_ledgers:
        allv = sorted(({**v, "amount": _tally_sign(v["dr"])} for v in per_ledger.get(led.id, {}).values()
                       if v["dr"]), key=lambda v: v["date"])
        for v in allv:
            v.pop("dr")
        prior = sum((v["amount"] for v in allv if b_s <= v["date"] < f_s), 0.0)
        assets.append({"name": led.name, "group": groups[led.group_id].name,
                       "openingTally": -(_tally_sign(led.opening_balance) + prior),
                       "reconcileDiff": None,         # one set of books: nothing to reconcile
                       "outsidePeriod": [v for v in allv if v["date"] > t_s or v["date"] < b_s],
                       "vouchers": [v for v in allv if f_s <= v["date"] <= t_s],
                       "vouchersFailed": False})
    return {"company": c.name, "from": f_s, "to": t_s, "fetchedAt": datetime.now().isoformat(timespec="seconds"),
            "faGroups": sorted(groups[g].name for g in fa), "assets": assets,
            "ledgers": [{"name": led.name, "parent": groups[led.group_id].name, "isExpense": led.group_id in exp,
                         "isFixedAsset": led.group_id in fa} for led in ledgers],
            "errors": []}


@router.post("/fetch")
def fetch(body: dict, db: DB, user: CurrentUser):
    c = _company(db, user, body.get("company", ""))
    try:
        f, t = date.fromisoformat(body["from"]), date.fromisoformat(body["to"])
    except (KeyError, ValueError):
        raise HTTPException(422, "Give the period: from and to")
    books = date.fromisoformat(body["booksFrom"]) if body.get("booksFrom") else c.books_from or f
    return fixed_asset_data(db, c, f, t, min(books, f))


# ------------------------------------------------------------------ the depreciation journal

@router.post("/post-journal")
def post_journal(body: dict, db: DB, user: CurrentUser):
    """A Journal voucher in Tally Cloud (posted); with sync to Tally on, it goes to Tally like any other."""
    c = _company(db, user, body.get("company", ""), Perm.VOUCHER_POST)
    lines = body.get("lines") or []
    entries = []
    for ln in lines:
        amt = Decimal(str(ln.get("dr") or 0)) - Decimal(str(ln.get("cr") or 0))
        if amt:
            entries.append({"ledger": ln["ledger"], "amount": f"{amt.quantize(Decimal('0.01'))}"})
    vt = db.scalar(select(VoucherType).where(VoucherType.company_id == c.id, VoucherType.name == "Journal")) or \
        db.scalar(select(VoucherType).where(VoucherType.company_id == c.id, VoucherType.base_type == "Journal"))
    if vt is None:
        return {"CREATED": "0", "ALTERED": "0", "ERRORS": "1", "EXCEPTIONS": "0",
                "lineErrors": ["This company has no Journal voucher type"]}
    try:
        v = engine.create_voucher(db, c, {"voucher_type": vt.name, "date": body.get("date"),
                                          "narration": body.get("narration") or "Depreciation", "entries": entries},
                                  actor_of(user), action="post")
        audit.record(db, actor_of(user), "fixed_assets.post_journal", "voucher", v.id, company_id=c.id,
                     after={"date": body.get("date"), "lines": len(entries)})
        db.commit()
    except engine.VoucherError as exc:
        db.rollback()
        return {"CREATED": "0", "ALTERED": "0", "ERRORS": "1", "EXCEPTIONS": "0", "lineErrors": [exc.message]}
    return {"CREATED": "1", "ALTERED": "0", "ERRORS": "0", "EXCEPTIONS": "0", "lineErrors": [],
            "voucher_number": v.voucher_number, "status": v.status, "sync_status": v.sync_status}
