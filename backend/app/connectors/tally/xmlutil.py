"""Low-level helpers for Tally's XML-over-HTTP format."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape, quoteattr

from lxml import etree

# Tally emits control characters such as &#4; (prefix of "Primary") that are invalid XML 1.0.
_BAD_CHAR_REFS = re.compile(r"&#(?:x0*[0-8bBcCeEfF]|x0*1[0-9a-fA-F]|0*[0-8]|0*1[1-2]|0*1[4-9]|0*2[0-9]|0*3[01]);")
_BAD_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PARSER = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)

PRIMARY = "Primary"


def decode_response(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse(text: str) -> etree._Element:
    text = _BAD_CHARS.sub("", _BAD_CHAR_REFS.sub("", text))
    root = etree.fromstring(text.encode("utf-8"), _PARSER)
    if root is None:
        raise ValueError("Empty or unparseable XML from Tally")
    return root


def txt(el: etree._Element | None, tag: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    value = child.text.strip()
    return value if value else default


def clean_name(value: str | None) -> str | None:
    """Tally prefixes reserved names with \\x04 (e.g. '\\x04 Primary')."""
    if value is None:
        return None
    value = _BAD_CHARS.sub("", value).strip()
    return value or None


def logical(value: str | None) -> bool:
    return (value or "").strip().lower() in ("yes", "true", "1")


def yes_no(flag: bool) -> str:
    return "Yes" if flag else "No"


_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def amount(value: str | None) -> Decimal:
    """Parse Tally amounts like ' -1,234.50', '1234.50 Dr', '₹ 12.00', '$12.00 @ ₹ 83.1/$ = ₹ 997.20'."""
    if not value:
        return Decimal("0")
    value = value.strip()
    if "=" in value:          # foreign currency: take the base-currency figure after '='
        value = value.split("=")[-1]
    m = _NUM.search(value)
    if not m:
        return Decimal("0")
    try:
        result = Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return Decimal("0")
    if value.endswith(("Dr", "Dr.")) and result > 0:
        result = -result      # Tally convention: debit negative
    return result


def quantity(value: str | None) -> tuple[Decimal, str | None]:
    """' 12.50 Nos' -> (Decimal('12.50'), 'Nos'). Compound units keep only the first figure."""
    if not value:
        return Decimal("0"), None
    value = value.strip()
    m = _NUM.search(value)
    if not m:
        return Decimal("0"), None
    rest = value[m.end():].strip().split(" ")[0] or None
    return Decimal(m.group(0).replace(",", "")), rest


def rate(value: str | None) -> Decimal:
    """'50.00/Nos' -> 50.00"""
    if not value:
        return Decimal("0")
    return abs(amount(value.split("/")[0]))


def tally_date(d: date | None) -> str:
    return d.strftime("%Y%m%d") if d else ""


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    from datetime import datetime
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def money_str(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):f}"


def esc(value: object) -> str:
    return escape("" if value is None else str(value))


def attr(value: object) -> str:
    return quoteattr("" if value is None else str(value))


def tag(name: str, value: object) -> str:
    if value is None or value == "":
        return ""
    return f"<{name}>{esc(value)}</{name}>"
