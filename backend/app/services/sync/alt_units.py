"""Alternate-unit quantity of stock lines (e.g. "140 PCS = 144.2 KGS"), filled in for vouchers read from Tally
before the software kept it, from the copy of Tally's text kept with each received voucher."""
import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.connectors.tally import codec, xmlutil as X

log = logging.getLogger("sync.alt_units")

LATEST = text("""
    SELECT erp_id, raw_xml FROM (
        SELECT DISTINCT ON (erp_id) erp_id, raw_xml FROM sync_inbox
        WHERE entity_type = 'voucher' AND status = 'applied' AND erp_id IS NOT NULL AND raw_xml IS NOT NULL
        ORDER BY erp_id, tally_alter_id DESC, received_at DESC) t
    WHERE raw_xml ~ 'QTY>[^<]*='""")
LINES = text("""SELECT l.id, s.name FROM voucher_inventory_lines l JOIN stock_items s ON s.id = l.stock_item_id
                WHERE l.voucher_id = :v ORDER BY l.line_no""")
SET = text("UPDATE voucher_inventory_lines SET alt_quantity = :q, alt_unit = :u WHERE id = :id")


def backfill(conn: Connection) -> int:
    """Stock lines given their alternate quantity. Only vouchers whose lines still match Tally's text."""
    done = 0
    result = conn.execute(LATEST.execution_options(stream_results=True, yield_per=200))
    for erp_id, raw in result:
        try:
            root = X.parse(raw)
            el = root if root.tag == "VOUCHER" else next(root.iter("VOUCHER"), None)
            inv = codec.parse_voucher(el).doc["inventory"] if el is not None else []
        except Exception:   # an odd copy is skipped: the next change of that voucher in Tally brings it
            continue
        if not any(i.get("alt_unit") for i in inv):
            continue
        lines = conn.execute(LINES, {"v": erp_id}).all()
        if len(lines) != len(inv) or any(name != i["item"] for (_, name), i in zip(lines, inv)):
            continue
        todo = [{"id": lid, "q": i["alt_quantity"], "u": i["alt_unit"]} for (lid, _), i in zip(lines, inv) if i.get("alt_unit")]
        conn.execute(SET, todo)
        done += len(todo)
    log.info("alternate quantities filled in for %s stock lines", done)
    return done
