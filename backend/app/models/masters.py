import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.db import Base
from app.models.base import JSON, ZERO, IdMixin, Money, Qty, Rate, TimestampMixin

# Group nature drives Balance Sheet vs P&L placement.
NATURES = ("asset", "liability", "income", "expense")

# How Tally treats each base voucher type. Orders, memos, reversing journals and attendance never touch
# ledger balances; only the types below move stock. A type created under another inherits its base's effect.
ACCOUNTING_BASES = frozenset({"Contra", "Payment", "Receipt", "Journal", "Sales", "Purchase", "Credit Note",
                              "Debit Note", "Payroll"})
STOCK_BASES = frozenset({"Sales", "Purchase", "Credit Note", "Debit Note", "Journal", "Stock Journal",
                         "Delivery Note", "Receipt Note", "Material In", "Material Out", "Rejections In",
                         "Rejections Out"})

BASE_VOUCHER_TYPES = (
    "Contra", "Payment", "Receipt", "Journal", "Sales", "Purchase", "Debit Note", "Credit Note",
    "Memorandum", "Reversing Journal", "Sales Order", "Purchase Order", "Delivery Note",
    "Receipt Note", "Stock Journal", "Physical Stock",
)


class LedgerGroup(IdMixin, TimestampMixin, Base):
    __tablename__ = "ledger_groups"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_groups.id"))
    nature: Mapped[str] = mapped_column(String(10))
    is_revenue: Mapped[bool] = mapped_column(Boolean, default=False)
    affects_gross_profit: Mapped[bool] = mapped_column(Boolean, default=False)
    is_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)

    parent: Mapped["LedgerGroup | None"] = relationship(remote_side="LedgerGroup.id")


class Ledger(IdMixin, TimestampMixin, Base):
    __tablename__ = "ledgers"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_groups.id"), index=True)
    # Signed: debit positive, credit negative.
    opening_balance: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    is_billwise: Mapped[bool] = mapped_column(Boolean, default=False)
    credit_days: Mapped[int | None] = mapped_column(Integer)
    gstin: Mapped[str | None] = mapped_column(String(20))
    pan: Mapped[str | None] = mapped_column(String(20))
    state: Mapped[str | None] = mapped_column(String(100))
    gst_registration_type: Mapped[str | None] = mapped_column(String(40))
    tax_type: Mapped[str | None] = mapped_column(String(40))          # e.g. GST, TDS, TCS for duty ledgers
    gst_duty_head: Mapped[str | None] = mapped_column(String(20))     # IGST/CGST/SGST/Cess
    bank_account_no: Mapped[str | None] = mapped_column(String(40))
    bank_ifsc: Mapped[str | None] = mapped_column(String(20))
    mailing_name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(String(1000))          # lines joined with newlines
    pincode: Mapped[str | None] = mapped_column(String(100))         # free text in Tally (seen: "930, Kathmandu, Nepal")
    country: Mapped[str | None] = mapped_column(String(60))
    place_of_supply: Mapped[str | None] = mapped_column(String(100))
    # Contact
    contact_person: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(100))
    mobile: Mapped[str | None] = mapped_column(String(100))
    fax: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(300))
    email_cc: Mapped[str | None] = mapped_column(String(300))
    website: Mapped[str | None] = mapped_column(String(200))
    contacts: Mapped[list | None] = mapped_column(JSON)                 # [{label, isd, number, whatsapp}]
    # Bank: own account (bank ledgers) and the party's accounts we pay into
    bank_name: Mapped[str | None] = mapped_column(String(200))
    bank_branch: Mapped[str | None] = mapped_column(String(300))
    bank_account_holder: Mapped[str | None] = mapped_column(String(200))
    party_banks: Mapped[list | None] = mapped_column(JSON)   # [{bank_name, account_no, ifsc, favouring, transfer_type, label, default}]
    # MSME (latest registration)
    msme_type: Mapped[str | None] = mapped_column(String(20))           # Micro / Small / Medium
    msme_udyam_no: Mapped[str | None] = mapped_column(String(40))
    msme_activity: Mapped[str | None] = mapped_column(String(40))       # Manufacturing / Services / Traders
    msme_from: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_system: Mapped[str] = mapped_column(String(10), default="erp")

    group: Mapped[LedgerGroup] = relationship()


class VoucherType(IdMixin, TimestampMixin, Base):
    __tablename__ = "voucher_types"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    base_type: Mapped[str] = mapped_column(String(40))
    numbering: Mapped[str] = mapped_column(String(10), default="auto")   # auto | manual
    prefix: Mapped[str] = mapped_column(String(20), default="")
    suffix: Mapped[str] = mapped_column(String(20), default="")
    next_number: Mapped[int] = mapped_column(Integer, default=1)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_threshold: Mapped[Decimal | None] = mapped_column(Money)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    affects_accounts: Mapped[bool] = mapped_column(Boolean, default=True)   # counts in ledger balances / reports
    affects_stock: Mapped[bool] = mapped_column(Boolean, default=True)      # moves stock quantities

    @validates("base_type")
    def _effects_follow_base(self, key, base_type):
        self.affects_accounts = base_type in ACCOUNTING_BASES
        self.affects_stock = base_type in STOCK_BASES
        return base_type


class CostCentre(IdMixin, Base):
    __tablename__ = "cost_centres"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(200), default="Primary Cost Category")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cost_centres.id"))


class Unit(IdMixin, Base):
    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("company_id", "symbol"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    formal_name: Mapped[str | None] = mapped_column(String(100))
    decimals: Mapped[int] = mapped_column(Integer, default=2)
    # Compound unit: 1 base = conversion x additional
    base_unit: Mapped[str | None] = mapped_column(String(40))
    additional_unit: Mapped[str | None] = mapped_column(String(40))
    conversion: Mapped[Decimal | None] = mapped_column(Rate)


class Godown(IdMixin, Base):
    __tablename__ = "godowns"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("godowns.id"))
    address: Mapped[str | None] = mapped_column(String(1000))


class StockGroup(IdMixin, Base):
    __tablename__ = "stock_groups"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_groups.id"))
    # GST/HSN set on the group; items marked "As per Company/Stock Group" inherit them
    hsn_code: Mapped[str | None] = mapped_column(String(50))
    hsn_description: Mapped[str | None] = mapped_column(Text)
    gst_rate: Mapped[Decimal | None] = mapped_column(Rate)
    gst_rates: Mapped[dict | None] = mapped_column(JSON)
    taxability: Mapped[str | None] = mapped_column(String(30))


class StockItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "stock_items"
    __table_args__ = (UniqueConstraint("company_id", "name"),)

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str | None] = mapped_column(String(60))
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stock_groups.id"))
    unit: Mapped[str | None] = mapped_column(String(40))
    alternate_unit: Mapped[str | None] = mapped_column(String(40))
    hsn_code: Mapped[str | None] = mapped_column(String(50))
    gst_rate: Mapped[Decimal | None] = mapped_column(Rate)
    valuation_method: Mapped[str] = mapped_column(String(50), default="avg_cost")   # Tally costing method
    opening_qty: Mapped[Decimal] = mapped_column(Qty, default=ZERO)
    opening_rate: Mapped[Decimal] = mapped_column(Rate, default=ZERO)
    opening_value: Mapped[Decimal] = mapped_column(Money, default=ZERO)
    reorder_level: Mapped[Decimal | None] = mapped_column(Qty)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    alias: Mapped[str | None] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(100))
    # Alternate unit: `denominator` base units = `conversion` alternate units (e.g. 1 PCS = 0.265 KGS)
    conversion: Mapped[Decimal | None] = mapped_column(Qty)
    denominator: Mapped[Decimal | None] = mapped_column(Qty)
    # GST
    gst_applicable: Mapped[str | None] = mapped_column(String(30))
    supply_type: Mapped[str | None] = mapped_column(String(50))         # Goods / Services / Capital Goods
    hsn_description: Mapped[str | None] = mapped_column(Text)
    gst_rates: Mapped[dict | None] = mapped_column(JSON)                # {"IGST": 18, "CGST": 9, ...}
    taxability: Mapped[str | None] = mapped_column(String(30))
    itc_eligible: Mapped[bool | None] = mapped_column(Boolean)
    gst_source: Mapped[str | None] = mapped_column(String(250))          # item | stock group | company
    # Behaviour
    market_valuation: Mapped[str | None] = mapped_column(String(30))
    batch_wise: Mapped[bool] = mapped_column(Boolean, default=False)
    has_mfg_date: Mapped[bool] = mapped_column(Boolean, default=False)
    perishable: Mapped[bool] = mapped_column(Boolean, default=False)
    ignore_negative_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    # Rates and balances as Tally reports them
    standard_cost: Mapped[Decimal | None] = mapped_column(Rate)
    standard_price: Mapped[Decimal | None] = mapped_column(Rate)
    closing_qty: Mapped[Decimal | None] = mapped_column(Qty)
    closing_rate: Mapped[Decimal | None] = mapped_column(Rate)
    closing_value: Mapped[Decimal | None] = mapped_column(Money)
    opening_allocations: Mapped[list | None] = mapped_column(JSON)      # [{godown, batch, qty, rate, value}]
    entered_by: Mapped[str | None] = mapped_column(String(100))


class TaxCode(IdMixin, Base):
    """Effective-dated GST rate master (PRD §5.2 Tax masters)."""

    __tablename__ = "tax_codes"

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    hsn_sac: Mapped[str] = mapped_column(String(20), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    rate: Mapped[Decimal] = mapped_column(Rate)
    cess_rate: Mapped[Decimal] = mapped_column(Rate, default=ZERO)
    effective_from: Mapped[date] = mapped_column(Date)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


# ------------------------------------------------------------------ long texts from Tally
# Tally allows longer texts than some columns hold. A descriptive text that is too long is shortened rather
# than making the whole master fail to import (and every voucher that uses it). Names and keys are never
# shortened: they identify records, so those still fail loudly.
_NEVER_SHORTEN = {"name", "code", "alias", "unit", "alternate_unit", "group", "parent", "guid"}


def _shorten_long_texts(mapper, connection, target) -> None:
    for col in mapper.columns:
        length = getattr(col.type, "length", None)
        if not length or col.key in _NEVER_SHORTEN or not isinstance(col.type, String):
            continue
        value = getattr(target, col.key, None)
        if isinstance(value, str) and len(value) > length:
            setattr(target, col.key, value[:length])


for _cls in (Ledger, LedgerGroup, StockItem, StockGroup, Unit, Godown, VoucherType, CostCentre):
    event.listen(_cls, "before_insert", _shorten_long_texts)
    event.listen(_cls, "before_update", _shorten_long_texts)
