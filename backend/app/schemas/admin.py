import uuid
from datetime import date
from decimal import Decimal

import re

from pydantic import BaseModel, ConfigDict, field_validator


class CompanyIn(BaseModel):
    name: str
    legal_name: str | None = None
    address: str | None = None
    gstin: str | None = None
    pan: str | None = None
    state: str | None = None
    base_currency: str = "INR"
    books_from: date
    timezone: str = "Asia/Kolkata"


class CompanyOut(CompanyIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class FinancialYearOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    start_date: date
    end_date: date
    is_locked: bool
    locked_upto: date | None


class FinancialYearIn(BaseModel):
    start_date: date


class PeriodLockIn(BaseModel):
    is_locked: bool | None = None
    locked_upto: date | None = None


class UserIn(BaseModel):
    username: str
    display_name: str | None = None
    is_superadmin: bool = False


class AccessIn(BaseModel):
    user_id: uuid.UUID
    role: str


class GroupIn(BaseModel):
    name: str
    parent: str | None = None
    nature: str | None = None


FORMATS = {
    "pan": (r"[A-Z]{5}[0-9]{4}[A-Z]", "PAN must be 10 characters like ABCDE1234F"),
    "gstin": (r"[0-9]{2}[A-Z0-9]{10}[0-9A-Z]Z[0-9A-Z]", "GSTIN must be 15 characters like 06ABCDE1234F1Z5"),
    "bank_ifsc": (r"[A-Z]{4}0[A-Z0-9]{6}", "IFSC must be 11 characters like HDFC0001234"),
    "ifsc": (r"[A-Z]{4}0[A-Z0-9]{6}", "IFSC must be 11 characters like HDFC0001234"),
    "msme_udyam_no": (r"UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}", "Udyam number must look like UDYAM-HR-03-0023487"),
    "email": (r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", "Email address is not valid"),
}
UPPER = ("pan", "gstin", "bank_ifsc", "ifsc", "msme_udyam_no")
GST_TYPES = ("Regular", "Composition", "Unregistered/Consumer", "Unknown", "Regular - SEZ", "OIDAR",
             "Consumer", "Unregistered")
MSME_TYPES = ("Micro", "Small", "Medium")
MSME_ACTIVITIES = ("Manufacturing", "Services", "Traders")


def check_format(field: str, value):
    """Blank means "not given"; anything else must match the format Tally expects."""
    if isinstance(value, str):
        value = value.strip() or None
    if value is None:
        return None
    if field in UPPER:
        value = value.upper().replace(" ", "")
    if field in FORMATS and not re.fullmatch(FORMATS[field][0], value):
        raise ValueError(FORMATS[field][1])
    return value


class PartyBank(BaseModel):
    """A bank account of the party, as in Tally's Bank Details (PAYMENTDETAILS)."""
    label: str | None = None
    bank_name: str | None = None
    account_no: str | None = None
    ifsc: str | None = None
    favouring: str | None = None
    transfer_type: str | None = None
    default: bool = False

    @field_validator("ifsc")
    @classmethod
    def _ifsc(cls, v):
        return check_format("ifsc", v)

    @field_validator("account_no")
    @classmethod
    def _account(cls, v):
        v = (v or "").replace(" ", "") or None
        if v and not re.fullmatch(r"[0-9A-Za-z]{5,34}", v):
            raise ValueError("Account number must be 5 to 34 letters or digits")
        return v


class LedgerIn(BaseModel):
    name: str
    group: str
    opening_balance: Decimal | None = None
    is_billwise: bool | None = None
    credit_days: int | None = None
    gstin: str | None = None
    pan: str | None = None
    state: str | None = None
    gst_registration_type: str | None = None
    tax_type: str | None = None
    gst_duty_head: str | None = None
    bank_account_no: str | None = None
    bank_ifsc: str | None = None
    mailing_name: str | None = None
    address: str | None = None
    is_active: bool | None = None
    pincode: str | None = None
    country: str | None = None
    place_of_supply: str | None = None
    contact_person: str | None = None
    phone: str | None = None
    mobile: str | None = None
    fax: str | None = None
    email: str | None = None
    email_cc: str | None = None
    website: str | None = None
    contacts: list[dict] | None = None
    bank_name: str | None = None
    bank_branch: str | None = None
    bank_account_holder: str | None = None
    party_banks: list[dict] | None = None
    msme_type: str | None = None
    msme_udyam_no: str | None = None
    msme_activity: str | None = None
    msme_from: date | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is not None and not v.strip():
            raise ValueError("Name is required")
        return v.strip() if v else v

    @field_validator("pan", "gstin", "bank_ifsc", "msme_udyam_no", "email", "email_cc")
    @classmethod
    def _formats(cls, v, info):
        if info.field_name == "email_cc":   # several addresses allowed, comma separated
            return ", ".join(check_format("email", part) for part in (v or "").split(",") if part.strip()) or None
        return check_format(info.field_name, v)

    @field_validator("gst_registration_type")
    @classmethod
    def _gst_type(cls, v):
        if v and v not in GST_TYPES:
            raise ValueError(f"Registration type must be one of: {', '.join(GST_TYPES[:6])}")
        return v or None

    @field_validator("msme_type")
    @classmethod
    def _msme_type(cls, v):
        if v and v not in MSME_TYPES:
            raise ValueError("MSME type must be Micro, Small or Medium")
        return v or None

    @field_validator("msme_activity")
    @classmethod
    def _msme_activity(cls, v):
        if v and v not in MSME_ACTIVITIES:
            raise ValueError("MSME activity must be Manufacturing, Services or Traders")
        return v or None

    @field_validator("pincode")
    @classmethod
    def _pincode(cls, v):
        return (v or "").strip() or None

    @field_validator("credit_days")
    @classmethod
    def _credit(cls, v):
        if v is not None and not 0 <= v <= 3650:
            raise ValueError("Credit period must be between 0 and 3650 days")
        return v

    @field_validator("party_banks")
    @classmethod
    def _banks(cls, v):
        if v is None:
            return None
        rows = [PartyBank(**row).model_dump() for row in v]
        return [r for r in rows if r["account_no"] or r["bank_name"]] or None


class LedgerPatch(LedgerIn):
    name: str | None = None
    group: str | None = None
    expected_version: int | None = None


class VoucherTypeIn(BaseModel):
    name: str
    base_type: str
    numbering: str = "auto"
    prefix: str = ""
    suffix: str = ""
    requires_approval: bool = False
    approval_threshold: Decimal | None = None


class VoucherTypePatch(BaseModel):
    numbering: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    next_number: int | None = None
    requires_approval: bool | None = None
    approval_threshold: Decimal | None = None
    is_active: bool | None = None


class StockItemIn(BaseModel):
    name: str
    code: str | None = None
    group: str | None = None
    unit: str | None = None
    hsn_code: str | None = None
    gst_rate: Decimal | None = None
    opening_qty: Decimal = Decimal("0")
    opening_rate: Decimal = Decimal("0")
    opening_value: Decimal = Decimal("0")
    reorder_level: Decimal | None = None


class NamedIn(BaseModel):
    name: str
    parent: str | None = None
