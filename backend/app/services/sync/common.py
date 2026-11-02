import random
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.tally import TallyClient
from app.core.config import settings
from app.models import SyncConflict, TallyConnector, TallyMapping
from app.models.base import utcnow
from app.services.canonical import diff


def client_for(conn: TallyConnector) -> TallyClient:
    return TallyClient(conn.host, conn.port, conn.tally_company_name)


def backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff with jitter (PRD §6.6)."""
    ceiling = min(settings.sync_backoff_cap, settings.sync_backoff_base * (2 ** max(0, attempts - 1)))
    return timedelta(seconds=random.uniform(ceiling / 2, ceiling))


def mapping_for_erp(db: Session, company_id: uuid.UUID, entity_type: str, erp_id: uuid.UUID) -> TallyMapping | None:
    return db.scalar(select(TallyMapping).where(
        TallyMapping.company_id == company_id, TallyMapping.entity_type == entity_type, TallyMapping.erp_id == erp_id))


def mapping_for_guid(db: Session, company_id: uuid.UUID, entity_type: str, guid: str) -> TallyMapping | None:
    return db.scalar(select(TallyMapping).where(
        TallyMapping.company_id == company_id, TallyMapping.entity_type == entity_type, TallyMapping.tally_guid == guid))


def save_mapping(db: Session, conn: TallyConnector, entity_type: str, erp_id: uuid.UUID, *, guid: str | None,
                 master_id: int | None, alter_id: int | None, content_hash: str | None, erp_version: int | None,
                 direction: str, voucher_type: str | None = None, voucher_number: str | None = None,
                 voucher_date=None, mapping: TallyMapping | None = None) -> TallyMapping:
    mapping = mapping or mapping_for_erp(db, conn.company_id, entity_type, erp_id)
    if mapping is None:
        mapping = TallyMapping(company_id=conn.company_id, entity_type=entity_type, erp_id=erp_id)
        db.add(mapping)
    mapping.tally_company_key = conn.tally_company_guid or conn.tally_company_name
    mapping.tally_guid = guid or mapping.tally_guid
    mapping.tally_master_id = master_id or mapping.tally_master_id
    mapping.tally_alter_id = alter_id if alter_id is not None else mapping.tally_alter_id
    mapping.last_synced_hash = content_hash
    mapping.erp_version_synced = erp_version
    mapping.last_synced_at = utcnow()
    mapping.sync_direction = direction
    mapping.voucher_type = voucher_type or mapping.voucher_type
    mapping.voucher_number = voucher_number or mapping.voucher_number
    mapping.voucher_date = voucher_date or mapping.voucher_date
    db.flush()
    return mapping


def open_conflict(db: Session, company_id: uuid.UUID, erp_id: uuid.UUID, *, detected_by: str, erp_doc: dict,
                  tally_doc: dict, tally_guid: str | None, tally_alter_id: int | None,
                  base_doc: dict | None) -> SyncConflict:
    existing = db.scalar(select(SyncConflict).where(
        SyncConflict.erp_id == erp_id, SyncConflict.status == "open"))
    if existing:
        existing.erp_snapshot, existing.tally_snapshot = erp_doc, tally_doc
        existing.tally_alter_id = tally_alter_id
        existing.diff = diff(erp_doc, tally_doc)
        return existing
    conflict = SyncConflict(company_id=company_id, erp_id=erp_id, detected_by=detected_by, erp_snapshot=erp_doc,
                            tally_snapshot=tally_doc, base_snapshot=base_doc, tally_guid=tally_guid,
                            tally_alter_id=tally_alter_id, diff=diff(erp_doc, tally_doc))
    db.add(conflict)
    return conflict
