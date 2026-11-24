"""Deleting a company: its books, masters, Tally link, sync history and user access, in one transaction.

Found from the foreign keys, so a table added later is included by itself. The audit trail stays: it records
the deletion and keeps the company's history (it has no link that would stop the delete)."""
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.db import Base


def _filters(company_id: uuid.UUID) -> dict:
    """For each table holding a company's rows: the condition that picks this company's rows."""
    import app.models  # noqa: F401
    companies = Base.metadata.tables["companies"]
    found = {"companies": companies.c.id == company_id}
    changed = True
    while changed:
        changed = False
        for t in Base.metadata.sorted_tables:
            if t.name in found:
                continue
            direct = [fk for fk in t.foreign_keys if fk.column.table is companies]
            if direct:
                found[t.name] = direct[0].parent == company_id
                changed = True
                continue
            # no company column: through a required link to a row that is the company's (an entry of a voucher)
            for fk in t.foreign_keys:
                parent = fk.column.table
                if parent is not t and parent.name in found and not fk.parent.nullable:
                    found[t.name] = fk.parent.in_(select(fk.column).where(found[parent.name]))
                    changed = True
                    break
    return found


def count_rows(db: Session, company_id: uuid.UUID) -> dict:
    out = {}
    for t in Base.metadata.sorted_tables:
        f = _filters(company_id).get(t.name)
        if f is not None and t.name != "companies":
            n = db.scalar(select(func.count()).select_from(t).where(f))
            if n:
                out[t.name] = n
    return out


def delete_company(db: Session, company_id: uuid.UUID) -> dict:
    """Deletes everything of the company (the caller commits). Returns the rows deleted per table."""
    filters = _filters(company_id)
    done = {}
    for t in reversed(Base.metadata.sorted_tables):        # children before parents
        f = filters.get(t.name)
        if f is None:
            continue
        n = db.execute(delete(t).where(f).execution_options(synchronize_session=False)).rowcount
        if n:
            done[t.name] = n
    return done
