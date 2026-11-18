"""A small .xlsx writer for report exports: one sheet, a bold header row, numbers stored as numbers."""
import io
import re
import zipfile
from decimal import Decimal
from xml.sax.saxutils import escape

NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
# Internal ids mean nothing in a spreadsheet.
HIDDEN = re.compile(r"(^id$|_id$|^key$)")
_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


HEAD = 4   # bold on a yellow background: every heading in an export


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, value, style: int = 0) -> str:
    st = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{st}/>' if style else ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{st}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float, Decimal)) or (isinstance(value, str) and NUMBER.match(value) and len(value) < 16
                                            and not (value.startswith("0") and len(value) > 1 and value[1] != ".")):
        return f'<c r="{ref}"{st}><v>{value}</v></c>'
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    text = escape(_BAD_XML.sub("", str(value)))
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{text}</t></is></c>'


LABELS = {"gstin": "GSTIN", "margin": "Gross profit", "margin_pct": "Margin %", "cost_per_unit": "Cost / unit"}


def header(key: str, labels: dict | None = None) -> str:
    return ({**LABELS, **(labels or {})}).get(key) or key.replace("_", " ").strip().capitalize()


def build(rows: list[dict], sheet: str = "Report", labels: dict | None = None) -> bytes:
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys and not k.startswith("_") and not HIDDEN.search(k):
                keys.append(k)
    lines = ['<row r="1">' + "".join(_cell(f"{_col(i)}1", header(k, labels), HEAD) for i, k in enumerate(keys, 1)) + "</row>"]
    for n, r in enumerate(rows, 2):
        lines.append(f'<row r="{n}">' + "".join(_cell(f"{_col(i)}{n}", r.get(k)) for i, k in enumerate(keys, 1)) + "</row>")
    widths = "".join(f'<col min="{i}" max="{i}" width="{min(45, max(10, len(header(k, labels)) + 2, *(len(str(r.get(k) or "")) + 2 for r in rows[:500])))}" customWidth="1"/>'
                     for i, k in enumerate(keys, 1))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                 '</sheetView></sheetViews>'
                 + (f"<cols>{widths}</cols>" if keys else "") + f'<sheetData>{"".join(lines)}</sheetData></worksheet>')
    return _package(sheet_xml, sheet)


def build_grid(grid: list[list], sheet: str = "Report", widths: list[int] | None = None) -> bytes:
    """A free-form sheet: grid is rows of cells, each a value or (value, style). Styles: "bold", "num" and
    "boldnum" (numbers shown as 1,23,456.00), "head" and "headnum" (bold on yellow, for headings), "pct" and
    "boldpct" (a fraction shown as 12.34%). None leaves a cell empty."""
    styles = {None: 0, "bold": 1, "num": 2, "boldnum": 3, "head": HEAD, "headnum": 5, "pct": 6, "boldpct": 7}
    lines = []
    for n, row in enumerate(grid, 1):
        cells = []
        for i, cell in enumerate(row, 1):
            value, style = cell if isinstance(cell, tuple) else (cell, None)
            cells.append(_cell(f"{_col(i)}{n}", value, styles[style]))
        lines.append(f'<row r="{n}">{"".join(cells)}</row>')
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths or [], 1))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 + (f"<cols>{cols}</cols>" if cols else "") + f'<sheetData>{"".join(lines)}</sheetData></worksheet>')
    return _package(sheet_xml, sheet)


def _package(sheet_xml: str, sheet: str) -> bytes:
    name = escape(re.sub(r"[\[\]:*?/\\]", " ", sheet)[:31] or "Report")
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="4" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="4" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyNumberFormat="1"/>'
        '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="10" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/></cellXfs>'
        '</styleSheet>',
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data)
    return buf.getvalue()
