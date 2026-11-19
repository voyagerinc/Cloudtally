"""Any table on screen as an Excel file, laid out as it is shown (the page sends the cells it shows)."""
import re
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import audit
from app.api.deps import DB, CurrentUser, actor_of, authorize
from app.api.reports import XLSX
from app.core.security import Perm
from app.reports import xlsx

router = APIRouter(tags=["export"])

STYLES = {"bold", "num", "boldnum", "head", "headnum", "pct", "boldpct"}
NUMERIC = {"num", "boldnum", "headnum"}


class Cell(BaseModel):
    v: str | None = None
    s: str | None = None


class TableIn(BaseModel):
    title: str = Field("Table", max_length=120)
    rows: list[list[Cell | str | None]] = Field(max_length=200_000)
    widths: list[int] | None = Field(None, max_length=200)


def _value(c: Cell | str | None):
    if c is None or isinstance(c, str):
        return c
    style = c.s if c.s in STYLES else None
    v = c.v
    if style in NUMERIC and v not in (None, ""):
        try:
            v = Decimal(v)
        except InvalidOperation:
            style = {"num": None, "boldnum": "bold", "headnum": "head"}[style]
    return (v, style) if style else v


@router.post("/export/table")
def export_table(company_id: uuid.UUID, body: TableIn, db: DB, user: CurrentUser):
    authorize(db, user, company_id, Perm.EXPORT)
    grid = [[_value(c) for c in row] for row in body.rows]
    audit.record(db, actor_of(user), "report.export", "table", body.title[:60], company_id=company_id,
                 after={"format": "xlsx", "rows": len(grid)})
    db.commit()
    widths = [max(6, min(60, w)) for w in body.widths] if body.widths else None
    data = xlsx.build_grid(grid, re.sub(r"[\[\]:*?/\\]", " ", body.title)[:31] or "Table", widths=widths)
    name = re.sub(r'[\\/:*?"<>|]+', " ", body.title).strip()[:100] or "Table"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
