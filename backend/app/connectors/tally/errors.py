"""Sync error taxonomy (PRD §6.6)."""
import re


class ErrorCode:
    CONNECTIVITY = "connectivity"
    AUTHENTICATION = "authentication"
    COMPANY_MISMATCH = "company_mismatch"
    MISSING_MASTER = "missing_master"
    INVALID_TAX = "invalid_tax_setup"
    PERIOD_LOCKED = "period_locked"
    DUPLICATE = "duplicate_voucher"
    UNSUPPORTED_FIELD = "unsupported_field"
    VALIDATION = "validation_failure"
    TALLY_REJECTION = "tally_rejection"
    VERIFY_MISMATCH = "verify_mismatch"   # Tally accepted the write but read-back differs
    CONFLICT = "conflict"

    TRANSIENT = {CONNECTIVITY}


class TallyError(Exception):
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False,
                 request_xml: str | None = None, response_xml: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # True when the request may have reached Tally (e.g. read timeout after send).
        self.outcome_unknown = outcome_unknown
        self.request_xml = request_xml
        self.response_xml = response_xml

    @property
    def transient(self) -> bool:
        return self.code in ErrorCode.TRANSIENT


_RULES = [
    (re.compile(r"(ledger|stock item|group|godown|unit|voucher type|cost centre)\s*'?.*'?\s*does not exist|"
                r"could not find|not found", re.I), ErrorCode.MISSING_MASTER),
    (re.compile(r"company.*(not (open|loaded)|does not exist)|no company", re.I), ErrorCode.COMPANY_MISMATCH),
    (re.compile(r"(gst|hsn|sac|tax|duty)", re.I), ErrorCode.INVALID_TAX),
    (re.compile(r"(out of (period|range)|period.*lock|locked|outside.*books|before.*books)", re.I), ErrorCode.PERIOD_LOCKED),
    (re.compile(r"(duplicate|already exists)", re.I), ErrorCode.DUPLICATE),
    (re.compile(r"(unknown|invalid) (tag|field|request)", re.I), ErrorCode.UNSUPPORTED_FIELD),
    (re.compile(r"(password|security|access denied|unauthori[sz]ed)", re.I), ErrorCode.AUTHENTICATION),
    (re.compile(r"(total.*not.*match|dr.*cr|not balanced|amount)", re.I), ErrorCode.VALIDATION),
]


def classify_tally_message(message: str) -> str:
    for pattern, code in _RULES:
        if pattern.search(message or ""):
            return code
    return ErrorCode.TALLY_REJECTION
