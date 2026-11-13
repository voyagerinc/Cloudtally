"""Scheduled ERP vs Tally reconciliation: counts, totals, identities, content and ledger balances."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.audit import Actor
from app.connectors.tally import TallyClient
from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.models import Company, ReconciliationRun, TallyConnector, TallyMapping, Voucher, VoucherStatus
from app.models.base import utcnow
from app.reports import ledger_balances
from app.services.accounting.vouchers import voucher_total
from app.services.canonical import content_hash, m

ZERO = Decimal("0")


def _tally_total(doc: dict) -> Decimal:
    return sum((Decimal(e["amount"]) for e in doc["entries"] if Decimal(e["amount"]) > 0), ZERO)


def run(db: Session, company: Company, conn: TallyConnector, client: TallyClient, from_date: date, to_date: date,
        actor: Actor, *, compare_ledgers: bool = True) -> ReconciliationRun:
    rec = ReconciliationRun(company_id=company.id, from_date=from_date, to_date=to_date, started_by=actor.name)
    db.add(rec)
    db.flush()
    try:
        result = _compare(db, company, client, from_date, to_date, compare_ledgers)
        diffs = sum(len(result[k]) for k in ("missing_in_erp", "missing_in_tally", "content_mismatch", "status_mismatch",
                                             "ledger_differences"))
        diffs += sum(1 for r in result["by_voucher_type"] if r["count_diff"] or r["amount_diff"] != "0.00")
        rec.result, rec.differences = result, diffs
        rec.status = "matched" if diffs == 0 else "differences"
    except Exception as exc:
        rec.status, rec.result = "error", {"error": f"{type(exc).__name__}: {exc}"}
    rec.finished_at = utcnow()
    audit.record(db, actor, "sync.reconcile", "company", company.id, company_id=company.id,
                 after={"status": rec.status, "differences": rec.differences, "from": from_date, "to": to_date})
    return rec


def _compare(db: Session, company: Company, client: TallyClient, from_date: date, to_date: date,
             compare_ledgers: bool) -> dict:
    erp = db.scalars(select(Voucher).where(
        Voucher.company_id == company.id, Voucher.voucher_date.between(from_date, to_date),
        Voucher.status.in_([VoucherStatus.POSTED, VoucherStatus.CANCELLED]))).all()
    maps = {mp.erp_id: mp for mp in db.scalars(select(TallyMapping).where(
        TallyMapping.company_id == company.id, TallyMapping.entity_type == "voucher"))}
    erp_by_id = {v.id: v for v in erp}
    by_master = {mp.tally_master_id: mp for mp in maps.values() if mp.tally_master_id}

    # Only what changed is read in full. A voucher whose AlterID in Tally is the one we stored has not been
    # touched since; reading thousands of full vouchers is what used to make Tally time out and hang.
    index = [r for r in client.voucher_index() if r.date and from_date <= r.date <= to_date]
    unchanged: dict[int, Voucher] = {}
    to_fetch = []
    for r in index:
        mp = by_master.get(r.master_id)
        v = erp_by_id.get(mp.erp_id) if mp else None
        if v is not None and mp.tally_alter_id == r.alter_id:
            unchanged[r.master_id] = v
        else:
            to_fetch.append(r.master_id)
    fetched = [t for found in client.vouchers_in_batches(to_fetch) for t in found if not t.is_deleted] if to_fetch else []
    by_master_tally = {t.master_id: t for t in fetched}
    in_tally = set(unchanged) | set(by_master_tally)

    totals = defaultdict(lambda: {"tally_count": 0, "erp_count": 0, "tally_amount": ZERO, "erp_amount": ZERO})
    for t in fetched:
        if not t.doc["is_cancelled"]:
            row = totals[t.doc["voucher_type"]]
            row["tally_count"] += 1
            row["tally_amount"] += _tally_total(t.doc)
    for v in unchanged.values():          # identical in Tally: same type, status and amount
        if v.status == VoucherStatus.POSTED:
            row = totals[v.voucher_type.name]
            row["tally_count"] += 1
            row["tally_amount"] += voucher_total(v)
    for v in erp:
        if v.status == VoucherStatus.POSTED:
            row = totals[v.voucher_type.name]
            row["erp_count"] += 1
            row["erp_amount"] += voucher_total(v)

    missing_in_tally, content_mismatch, status_mismatch = [], [], []
    matched_masters = set()
    for v in erp:
        mp = maps.get(v.id)
        master = mp.tally_master_id if mp else None
        ident = {"erp_id": str(v.id), "voucher_type": v.voucher_type.name, "voucher_number": v.voucher_number,
                 "date": str(v.voucher_date)}
        if master is None or master not in in_tally:
            if v.status == VoucherStatus.POSTED:
                missing_in_tally.append({**ident, "reason": "not mapped" if not mp else "mapped voucher not found in Tally"})
            continue
        matched_masters.add(master)
        t = by_master_tally.get(master)
        if t is None:                     # unchanged since we stored it
            continue
        if (v.status == VoucherStatus.CANCELLED) != bool(t.doc["is_cancelled"]):
            status_mismatch.append({**ident, "erp_status": v.status, "tally_cancelled": t.doc["is_cancelled"]})
        elif v.status == VoucherStatus.POSTED and content_hash(t.doc) != v.content_hash:
            content_mismatch.append({**ident, "tally_guid": t.guid})
    missing_in_erp = [
        {"tally_guid": t.guid, "master_id": t.master_id, "voucher_type": t.doc["voucher_type"],
         "voucher_number": t.doc["voucher_number"], "date": t.doc["date"], "amount": m(_tally_total(t.doc))}
        for t in fetched if t.master_id not in matched_masters and not t.doc["is_cancelled"]
    ]

    ledger_differences = []
    if compare_ledgers:
        # Tally reports closing balances for its current period; compare as of the end of the range.
        erp_bal = {r["ledger"]: Decimal(r["closing"]) for r in ledger_balances(db, company.id, to_date)}
        xml = codec.export_collection("Ledger", ["NAME", "CLOSINGBALANCE"], client.company,
                                      from_date=company.books_from, to_date=to_date)
        root = X.parse(client.post(xml))
        for el in root.iter("LEDGER"):
            name = X.clean_name(el.get("NAME"))
            if not name:
                continue
            tally_bal = -X.amount(X.txt(el, "CLOSINGBALANCE"))
            if tally_bal != erp_bal.get(name, ZERO):
                ledger_differences.append({"ledger": name, "tally": m(tally_bal), "erp": m(erp_bal.get(name, ZERO)),
                                           "difference": m(tally_bal - erp_bal.get(name, ZERO))})

    return {
        "by_voucher_type": [
            {"voucher_type": k, "tally_count": r["tally_count"], "erp_count": r["erp_count"],
             "count_diff": r["tally_count"] - r["erp_count"], "tally_amount": m(r["tally_amount"]),
             "erp_amount": m(r["erp_amount"]), "amount_diff": m(r["tally_amount"] - r["erp_amount"])}
            for k, r in sorted(totals.items())
        ],
        "missing_in_erp": missing_in_erp,
        "missing_in_tally": missing_in_tally,
        "content_mismatch": content_mismatch,
        "status_mismatch": status_mismatch,
        "ledger_differences": ledger_differences,
        "read_from_tally": {"vouchers_in_range": len(index), "read_in_full": len(to_fetch), "unchanged": len(unchanged)},
    }
