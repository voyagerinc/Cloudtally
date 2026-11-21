"""Re-read the e-invoice and e-way bill details of a period's invoices from Tally (read only).

Vouchers synced before these details were read (or whose e-way bill was made later in Tally without any
other change) get them here. Only the statutory details are updated: no new voucher version, nothing is
written to Tally.

Kept light for Tally: only the voucher id and its IRN / acknowledgement / e-way bill fields are asked for (no
ledger or item lines), in small MasterID ranges with a long time limit each. A range Tally does not answer in
time is skipped and reported; the rest are still read."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.tally import TallyClient, TallyError, codec
from app.connectors.tally import xmlutil as X
from app.models import TallyConnector, TallyMapping, Voucher, VoucherType

BASES = ("Sales", "Credit Note", "Debit Note", "Purchase")
LIMIT = 5000
BATCH = 40                  # vouchers per request
TIMEOUT = 180.0             # seconds Tally may take for one small request
FETCH = ["MASTERID", "IRN", "IRNACKNO", "IRNACKDATE", "EWAYBILLNO", "EWAYBILLDETAILS"]


def refresh(db: Session, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    conn = db.get(TallyConnector, company_id)
    if conn is None or not conn.enabled:
        raise ValueError("This company is not connected to Tally")
    pairs = db.execute(select(TallyMapping.tally_master_id, Voucher)
                       .join(Voucher, Voucher.id == TallyMapping.erp_id)
                       .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
                       .where(Voucher.company_id == company_id, TallyMapping.entity_type == "voucher",
                              TallyMapping.tally_master_id.is_not(None), VoucherType.base_type.in_(BASES),
                              Voucher.voucher_date.between(from_date, to_date))).all()
    if len(pairs) > LIMIT:
        raise ValueError(f"{len(pairs)} invoices in this period: choose a shorter period (at most {LIMIT} at a time)")
    by_mid = {mid: v for mid, v in pairs}
    mids = sorted(by_mid)
    updated, read, skipped = 0, 0, []
    with TallyClient(conn.host, conn.port, conn.tally_company_name, timeout=TIMEOUT) as client:
        for i in range(0, len(mids), BATCH):
            part = mids[i:i + BATCH]
            try:
                els = list(client._collection("Voucher", "VOUCHER", FETCH,
                                              f"$MasterID >= {part[0]} AND $MasterID <= {part[-1]}"))
            except TallyError as exc:
                if not read and not skipped:      # the very first request failed: Tally is not answering at all
                    raise
                skipped.append(f"{part[0]}-{part[-1]}: {exc.message}")
                continue
            for el in els:
                mid = int(X.txt(el, "MASTERID") or 0)
                v = by_mid.get(mid)
                if v is None:
                    continue
                read += 1
                st = codec.statutory(el)
                if st and st != (v.statutory or {}):
                    v.statutory = {**(v.statutory or {}), **st}
                    updated += 1
            db.commit()
    return {"invoices": len(by_mid), "read": read, "updated": updated, "skipped": skipped[:20],
            "skipped_count": len(skipped)}
