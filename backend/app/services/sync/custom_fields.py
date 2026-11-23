"""Custom (TDL) fields from a customised Tally, e.g. chassis no. and motor no. of each vehicle sold.

A customisation adds user-defined fields (UDFs) to vouchers, their item lines or batches. Every field
Tally sends with a voucher is kept on it (Voucher.custom_fields). Fields that Tally sends only when asked
for by name are found with discover(): it reads Tally's own Day Book for a day (which carries every field),
lists them with a sample, and checks which of them come with the normal sync. The company keeps the ones it
wants (TallyConnector.custom_fields); sync then asks for them, and refresh() reads them again for vouchers
synced before. Everything here is read-only for Tally."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.tally import TallyClient, TallyError, codec
from app.connectors.tally import xmlutil as X
from app.models import TallyConnector, TallyMapping, Voucher, VoucherType
from app.services.sync import common

LEVELS = {"voucher": "Voucher", "ledger": "Ledger line", "item": "Item line", "batch": "Batch"}
MAX_DAYS = 3                # Day Book requests while looking for a day with custom fields
TIMEOUT = 180.0


def _connector(db: Session, company_id: uuid.UUID) -> TallyConnector:
    conn = db.get(TallyConnector, company_id)
    if conn is None or not conn.enabled:
        raise ValueError("This company is not connected to Tally")
    return conn


def _fields_in(el) -> list[dict]:
    """Each custom field of one voucher element: level, tag, label and value."""
    out = []

    def add(level, owner):
        for e in owner:
            if (one := codec.udf_value(e)) is not None:
                out.append({"level": level, "tag": e.tag[:-5] if e.tag.endswith(".LIST") else e.tag,
                            "label": one[0], "sample": one[1], "type": e.get("TYPE")})

    add("voucher", el)
    for e in el.findall("ALLLEDGERENTRIES.LIST") + el.findall("LEDGERENTRIES.LIST"):
        add("ledger", e)
        for i in e.findall("INVENTORYALLOCATIONS.LIST"):
            add("item", i)
            for b in i.findall("BATCHALLOCATIONS.LIST"):
                add("batch", b)
    for tag in ("ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIESIN.LIST", "INVENTORYENTRIESOUT.LIST"):
        for i in el.findall(tag):
            add("item", i)
            for b in i.findall("BATCHALLOCATIONS.LIST"):
                add("batch", b)
    return out


def _labels(cf: dict) -> set[str]:
    have = set(cf.get("voucher") or {})
    for part in ("ledgers", "items"):
        for row in cf.get(part) or []:
            have |= set(row["fields"])
    return have


def discover(db: Session, company_id: uuid.UUID, day: date | None = None) -> dict:
    conn = _connector(db, company_id)
    if day:
        days = [day]
    else:   # the latest days with vouchers in the software
        days = list(db.scalars(select(Voucher.voucher_date).where(Voucher.company_id == company_id)
                               .group_by(Voucher.voucher_date).order_by(Voucher.voucher_date.desc()).limit(MAX_DAYS)))
        if not days:
            raise ValueError("No vouchers yet: sync from Tally first, or give a date that has vouchers in Tally")
    found: dict[tuple, dict] = {}
    looked, vouchers, sample_mid = [], 0, None
    with TallyClient(conn.host, conn.port, conn.tally_company_name, timeout=TIMEOUT) as client:
        for d in days:
            looked.append(str(d))
            els = client.day_book(d)
            vouchers += len(els)
            for el in els:
                fields = _fields_in(el)
                if fields and sample_mid is None and X.txt(el, "MASTERID"):
                    sample_mid = int(X.txt(el, "MASTERID"))
                for f in fields:
                    g = found.setdefault((f["level"], f["tag"]), {
                        **f, "level_label": LEVELS[f["level"]], "vouchers": 0, "voucher_types": set(),
                        "fetch_name": codec.fetch_name(f["tag"]), "comes_with_sync": None})
                    g["vouchers"] += 1
                    g["voucher_types"].add(X.clean_name(X.txt(el, "VOUCHERTYPENAME") or el.get("VCHTYPE")) or "")
            if found:
                break
        # Does the normal sync bring them? Ask for one voucher the way sync does, with the fields by name.
        if sample_mid is not None:
            client.extra_fetch = sorted({g["fetch_name"] for g in found.values() if g["fetch_name"]})
            got = client.vouchers(master_id=sample_mid)
            have = _labels(got[0].doc.get("custom_fields") or {}) if got else set()
            for g in found.values():
                g["comes_with_sync"] = g["label"] in have
    chosen = {(f.get("level"), f.get("tag")) for f in conn.custom_fields or []}
    fields = sorted(({**g, "voucher_types": sorted(t for t in g["voucher_types"] if t),
                      "chosen": (g["level"], g["tag"]) in chosen} for g in found.values()),
                    key=lambda g: (list(LEVELS).index(g["level"]), g["label"]))
    return {"days": looked, "vouchers_read": vouchers, "fields": fields, "saved": conn.custom_fields or [],
            "note": None if fields else "No custom fields were found in these days' vouchers. Give a date on which "
                                        "customised vouchers (e.g. sales of vehicles) were entered."}


def save(db: Session, company_id: uuid.UUID, fields: list[dict]) -> list[dict]:
    """The fields to bring with every voucher: tag, label, level, the voucher types they were seen on and whether
    the normal sync brings them (when not, each sync reads the Day Book of the days it changed)."""
    conn = _connector(db, company_id)
    clean, seen = [], set()
    for f in fields:
        tag = (f.get("tag") or "").strip()
        level = f.get("level") if f.get("level") in LEVELS else "voucher"
        if not tag or (level, tag) in seen:
            continue
        seen.add((level, tag))
        clean.append({"tag": tag, "label": (f.get("label") or tag).strip()[:100], "level": level,
                      "voucher_types": sorted({str(t) for t in f.get("voucher_types") or [] if t}),
                      "comes_with_sync": f.get("comes_with_sync")})
    conn.custom_fields = clean
    db.commit()
    return clean


def _types(conn: TallyConnector) -> set[str] | None:
    """Voucher types the chosen fields were seen on (None: every type)."""
    types = {t for f in conn.custom_fields or [] for t in f.get("voucher_types") or []}
    return types or None


def _days(db: Session, company_id: uuid.UUID, conn: TallyConnector, from_date: date, to_date: date,
          since=None) -> list[date]:
    """Days that have vouchers from Tally (of the types with custom fields) in the period."""
    stmt = (select(Voucher.voucher_date).join(TallyMapping, TallyMapping.erp_id == Voucher.id)
            .join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, TallyMapping.entity_type == "voucher",
                   Voucher.voucher_date.between(from_date, to_date))
            .group_by(Voucher.voucher_date).order_by(Voucher.voucher_date))
    if (types := _types(conn)) is not None:
        stmt = stmt.where(VoucherType.name.in_(types))
    if since is not None:
        stmt = stmt.where(Voucher.updated_at >= since)
    return list(db.scalars(stmt))


def read_days(db: Session, company_id: uuid.UUID, days: list[date], progress=None) -> dict:
    """Read the custom fields of these days' vouchers from Tally's Day Book (it carries every field, whatever
    level: voucher, item line or batch), one day per request with a pause so people can keep working in Tally."""
    import time
    from app.core.config import settings
    conn = _connector(db, company_id)
    by_mid = {mid: vid for mid, vid in db.execute(
        select(TallyMapping.tally_master_id, TallyMapping.erp_id).join(Voucher, Voucher.id == TallyMapping.erp_id)
        .where(Voucher.company_id == company_id, TallyMapping.entity_type == "voucher",
               TallyMapping.tally_master_id.is_not(None), Voucher.voucher_date.in_(days)))} if days else {}
    stats = {"days": len(days), "days_done": 0, "read": 0, "updated": 0, "skipped": [], "skipped_count": 0}
    with TallyClient(conn.host, conn.port, conn.tally_company_name, timeout=TIMEOUT) as client:
        for n, d in enumerate(days):
            if n and settings.sync_voucher_batch_pause:
                time.sleep(min(settings.sync_voucher_batch_pause, 1.0))
            try:
                els = client.day_book(d)
            except TallyError as exc:
                if not stats["read"] and not stats["skipped_count"]:   # Tally is not answering at all
                    raise
                stats["skipped_count"] += 1
                if len(stats["skipped"]) < 20:
                    stats["skipped"].append(f"{d}: {exc.message}")
                continue
            for el in els:
                vid = by_mid.get(int(X.txt(el, "MASTERID") or 0))
                v = db.get(Voucher, vid) if vid else None
                if v is None:
                    continue
                stats["read"] += 1
                cf = codec.custom_fields_of(el)
                if cf != (v.custom_fields or {}):
                    v.custom_fields = cf
                    stats["updated"] += 1
            db.commit()
            stats["days_done"] = n + 1
            if progress:
                progress(stats)
    return stats


def refresh(db: Session, company_id: uuid.UUID, from_date: date, to_date: date, progress=None) -> dict:
    """Read the custom fields again for the period's vouchers already in the software (read-only for Tally)."""
    conn = _connector(db, company_id)
    return read_days(db, company_id, _days(db, company_id, conn, from_date, to_date), progress)


def after_sync(db: Session, company_id: uuid.UUID, since) -> dict | None:
    """After a sync: when a chosen field does not come with the normal voucher fetch (e.g. a batch's chassis no.),
    read the Day Book of the days whose vouchers this sync created or changed."""
    conn = db.get(TallyConnector, company_id)
    if conn is None or not any(f.get("comes_with_sync") is False for f in conn.custom_fields or []):
        return None
    days = _days(db, company_id, conn, date(1900, 1, 1), date(9999, 12, 31), since=since)
    return read_days(db, company_id, days) if days else None


# ------------------------------------------------------------------ "read from the books' beginning" in the background

import threading                                    # noqa: E402

JOBS: dict[uuid.UUID, dict] = {}
_jobs_lock = threading.Lock()


def start_refresh(session_factory, company_id: uuid.UUID, from_date: date, to_date: date) -> dict:
    """Run refresh() in the background (it can take a while from the books' beginning); job() reports progress."""
    with _jobs_lock:
        job = JOBS.get(company_id)
        if job and job["running"]:
            return job
        job = JOBS[company_id] = {"running": True, "from": str(from_date), "to": str(to_date), "days": None,
                                  "days_done": 0, "read": 0, "updated": 0, "skipped_count": 0, "error": None}

    def run():
        try:
            with session_factory() as db:
                r = refresh(db, company_id, from_date, to_date, progress=lambda st: job.update(
                    {k: v for k, v in st.items() if k != "skipped"}))
                job.update({k: v for k, v in r.items()})
        except (TallyError, ValueError) as exc:
            job["error"] = getattr(exc, "message", None) or str(exc)
        except Exception as exc:                      # shown to the user; the server log has the details
            job["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            job["running"] = False

    threading.Thread(target=run, name=f"custom-fields-{company_id}", daemon=True).start()
    return job


def job(company_id: uuid.UUID) -> dict | None:
    return JOBS.get(company_id)


def register(db: Session, company_id: uuid.UUID, from_date: date, to_date: date,
             voucher_type: str | None = None) -> dict:
    """Custom fields report: one row per item line / batch (or per voucher) that has custom fields."""
    stmt = (select(Voucher).join(VoucherType, VoucherType.id == Voucher.voucher_type_id)
            .where(Voucher.company_id == company_id, Voucher.voucher_date.between(from_date, to_date),
                   Voucher.custom_fields.is_not(None))
            .order_by(Voucher.voucher_date, Voucher.voucher_number))
    if voucher_type:
        stmt = stmt.where(VoucherType.name == voucher_type)
    conn = db.get(TallyConnector, company_id)
    order = [f["label"] for f in (conn.custom_fields if conn else None) or []]
    rows, labels = [], []
    for v in db.scalars(stmt):
        cf = v.custom_fields or {}
        if not cf:
            continue
        own = dict(cf.get("voucher") or {})
        for led in cf.get("ledgers") or []:
            own.update(led["fields"])
        base = {"voucher_id": str(v.id), "date": str(v.voucher_date), "voucher_type": v.voucher_type.name,
                "voucher_number": v.voucher_number, "party": v.party_ledger.name if v.party_ledger else None}
        for it in cf.get("items") or [None]:
            fields = {**own, **(it["fields"] if it else {})}
            labels += [n for n in fields if n not in labels]
            rows.append({**base, "item": it and it["item"], "batch": it and it["batch"],
                         "quantity": it and it["quantity"], "fields": fields})
    labels = [n for n in order if n in labels] + [n for n in labels if n not in order]
    return {"from": str(from_date), "to": str(to_date), "fields": labels,
            "rows": [{**{k: x for k, x in r.items() if k != "fields"}, **{n: r["fields"].get(n) for n in labels}}
                     for r in rows]}
