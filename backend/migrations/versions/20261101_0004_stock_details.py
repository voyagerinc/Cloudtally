"""stock groups and items: GST/HSN, units, valuation, balances and godown openings from Tally

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

QTY, RATE, MONEY = sa.Numeric(18, 4), sa.Numeric(18, 6), sa.Numeric(18, 2)
GROUP_COLUMNS = [
    ("hsn_code", sa.String(20)), ("hsn_description", sa.String(300)), ("gst_rate", RATE),
    ("gst_rates", postgresql.JSONB()), ("taxability", sa.String(30)),
]
ITEM_COLUMNS = [
    ("alias", sa.String(200)), ("category", sa.String(100)), ("conversion", QTY), ("denominator", QTY),
    ("gst_applicable", sa.String(30)), ("supply_type", sa.String(20)), ("hsn_description", sa.String(300)),
    ("gst_rates", postgresql.JSONB()), ("taxability", sa.String(30)), ("itc_eligible", sa.Boolean()),
    ("gst_source", sa.String(20)), ("market_valuation", sa.String(30)),
    ("batch_wise", sa.Boolean()), ("has_mfg_date", sa.Boolean()), ("perishable", sa.Boolean()),
    ("ignore_negative_stock", sa.Boolean()), ("standard_cost", RATE), ("standard_price", RATE),
    ("closing_qty", QTY), ("closing_rate", RATE), ("closing_value", MONEY),
    ("opening_allocations", postgresql.JSONB()), ("entered_by", sa.String(100)),
]


def upgrade() -> None:
    for name, type_ in GROUP_COLUMNS:
        op.add_column("stock_groups", sa.Column(name, type_, nullable=True))
    for name, type_ in ITEM_COLUMNS:
        op.add_column("stock_items", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(ITEM_COLUMNS):
        op.drop_column("stock_items", name)
    for name, _ in reversed(GROUP_COLUMNS):
        op.drop_column("stock_groups", name)
