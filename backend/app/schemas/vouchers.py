import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BillType = Literal["New Ref", "Agst Ref", "Advance", "On Account"]


class BillIn(BaseModel):
    type: BillType = "New Ref"
    name: str | None = None
    amount: Decimal
    due_date: date | None = None


class CostIn(BaseModel):
    category: str = "Primary Cost Category"
    centre: str
    amount: Decimal


class BankIn(BaseModel):
    transaction_type: str | None = None
    instrument_no: str | None = None
    instrument_date: date | None = None
    bank_name: str | None = None
    amount: Decimal


class EntryIn(BaseModel):
    """Accounting line. Give either `amount` (debit positive, credit negative) or `debit`/`credit`."""

    ledger: str
    amount: Decimal | None = None
    debit: Decimal | None = Field(default=None, exclude=True)
    credit: Decimal | None = Field(default=None, exclude=True)
    narration: str | None = None
    bills: list[BillIn] = []
    cost: list[CostIn] = []
    bank: list[BankIn] = []
    gst: dict = {}

    @model_validator(mode="after")
    def _signed(self):
        if self.amount is None:
            if bool(self.debit) == bool(self.credit):
                raise ValueError("give amount, or exactly one of debit/credit")
            self.amount = self.debit if self.debit else -self.credit
        return self


class InventoryIn(BaseModel):
    item: str
    quantity: Decimal = Field(description="Inward positive, outward negative")
    unit: str | None = None
    rate: Decimal = Decimal("0")
    discount_pct: Decimal = Decimal("0")
    amount: Decimal | None = None
    godown: str | None = None
    batch: str | None = None
    ledger: str | None = Field(default=None, description="Accounting allocation, e.g. the Sales ledger")


class VoucherIn(BaseModel):
    voucher_type: str
    voucher_number: str | None = None
    date: date
    effective_date: date | None = None
    reference_no: str | None = None
    reference_date: date | None = None
    party_ledger: str | None = None
    narration: str | None = None
    currency: str = "INR"
    exchange_rate: Decimal = Decimal("1")
    place_of_supply: str | None = None
    is_reverse_charge: bool = False
    statutory: dict = {}
    entries: list[EntryIn]
    inventory: list[InventoryIn] = []


class VoucherCreate(VoucherIn):
    action: Literal["draft", "submit", "post"] = "draft"


class VoucherUpdate(VoucherIn):
    expected_version: int
    reason: str | None = None


class TransitionIn(BaseModel):
    reason: str | None = None
    expected_version: int | None = None


class VoucherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    status: str
    sync_status: str
    source_system: str
    version: int
    content_hash: str | None
    created_by: str | None
    updated_by: str | None
    approved_by: str | None
    created_at: datetime
    updated_at: datetime
    posted_at: datetime | None
    doc: dict
    tally: dict | None = None


class VoucherSummary(BaseModel):
    id: uuid.UUID
    voucher_type: str
    voucher_number: str
    date: date
    party_ledger: str | None
    narration: str | None
    amount: Decimal
    status: str
    sync_status: str
    source_system: str
    version: int
