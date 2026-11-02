"""Controlled migration from Tally (PRD §9): discovery, masters, then vouchers in date order."""
from datetime import date

from sqlalchemy.orm import Session

from app import audit
from app.audit import Actor
from app.connectors.tally import TallyClient
from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.models import Company, ImportBatch, TallyConnector
from app.models.base import utcnow
from app.services.sync import inbound
from app.services.sync.masters_in import MasterImporter


def discover(client: TallyClient) -> dict:
    """Phase-0 discovery: loaded companies and object counts reported by Tally."""
    companies = client.list_companies()
    xml = codec.export_collection("Company", ["NAME"], client.company)
    root = X.parse(client.post(xml))
    info = root.find(".//CMPINFO")
    counts = {child.tag.lower(): int((child.text or "0").strip() or 0) for child in info} if info is not None else {}
    return {"server": client.ping(), "companies": companies,
            "object_counts": {k: v for k, v in counts.items() if v}}


def import_masters(db: Session, company: Company, conn: TallyConnector, client: TallyClient, actor: Actor) -> ImportBatch:
    batch = ImportBatch(company_id=company.id, kind="masters", started_by=actor.name)
    db.add(batch)
    db.flush()
    importer = MasterImporter(db, company, conn, refresh=True)
    try:
        importer.pull(client)
        conn.master_watermark = max(conn.master_watermark, importer.max_alter_id)
        batch.status = "completed" if not importer.errors else "completed_with_errors"
    except Exception as exc:
        batch.status = "failed"
        importer.errors.append({"error": f"{type(exc).__name__}: {exc}"})
    batch.counts, batch.errors, batch.finished_at = importer.counts, importer.errors[:500], utcnow()
    audit.record(db, actor, "import.masters", "company", company.id, company_id=company.id,
                 after={"status": batch.status, "counts": batch.counts})
    db.commit()
    return batch


def import_vouchers(db: Session, company: Company, conn: TallyConnector, client: TallyClient, actor: Actor,
                    from_date: date, to_date: date, batch_size: int | None = None) -> ImportBatch:
    """Idempotent: re-running a range never duplicates (inbox + mapping).

    One header-only index of the company's vouchers, then full vouchers in small MasterID batches with a
    pause between them, so Tally stays usable during a long import.
    """
    batch = ImportBatch(company_id=company.id, kind="vouchers", started_by=actor.name,
                        counts={"received": 0, "applied": 0, "ignored": 0, "failed": 0, "conflict": 0, "batches": 0})
    db.add(batch)
    db.commit()
    counts, errors, max_alter = dict(batch.counts), [], conn.voucher_watermark
    try:
        index = [r for r in client.voucher_index() if r.date and from_date <= r.date <= to_date]
        counts["in_range"] = len(index)
        for found in client.vouchers_in_batches([r.master_id for r in index], batch_size=batch_size):
            for tv in sorted(found, key=lambda t: (t.doc["date"] or "", t.alter_id)):
                max_alter = max(max_alter, tv.alter_id)
                item = inbound.receive(db, company.id, tv)
                if item is None:
                    counts["ignored"] += 1
                    continue
                counts["received"] += 1
                outcome = inbound.apply_item(db, company, conn, item)
                counts[outcome] = counts.get(outcome, 0) + 1
                if outcome == "failed" and len(errors) < 500:
                    errors.append({"voucher": f"{tv.doc['voucher_type']} {tv.doc['voucher_number']} {tv.doc['date']}",
                                   "code": item.error_code, "error": item.error})
            counts["batches"] += 1
            batch.counts = dict(counts)
            db.commit()
        batch.status = "completed" if counts["failed"] == 0 else "completed_with_errors"
    except Exception as exc:
        db.rollback()
        batch.status = "failed"
        errors.append({"error": f"{type(exc).__name__}: {exc}", "after_batches": counts["batches"]})
    conn.voucher_watermark = max(conn.voucher_watermark, max_alter)
    batch.counts, batch.errors, batch.finished_at = counts, errors, utcnow()
    audit.record(db, actor, "import.vouchers", "company", company.id, company_id=company.id,
                 after={"status": batch.status, "counts": counts, "from": from_date, "to": to_date})
    db.commit()
    return batch
