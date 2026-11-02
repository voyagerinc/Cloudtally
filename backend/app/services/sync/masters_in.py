"""Tally masters -> ERP (used by the initial import and by incremental polling)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.audit import TALLY
from app.connectors.tally import TallyClient
from app.connectors.tally import codec
from app.connectors.tally import xmlutil as X
from app.models import (
    Company, CostCentre, Godown, Ledger, LedgerGroup, StockGroup, StockItem, TallyConnector, TallyMapping, Unit,
    VoucherType,
)
from app.models.base import utcnow
from app.models.masters import BASE_VOUCHER_TYPES


class MasterImporter:
    def __init__(self, db: Session, company: Company, conn: TallyConnector, *, refresh: bool = False):
        """refresh=True re-applies records Tally reports as unchanged (e.g. after new fields were added)."""
        self.db, self.company, self.conn, self.refresh = db, company, conn, refresh
        self.counts: dict[str, dict[str, int]] = {}
        self.errors: list[dict] = []
        self.max_alter_id = 0

    # -------------------------------------------------------------- helpers

    def _bump(self, kind: str, what: str) -> None:
        self.counts.setdefault(kind, {"created": 0, "updated": 0, "unchanged": 0, "failed": 0})[what] += 1

    def _mappings(self, kind: str) -> dict[str, TallyMapping]:
        rows = self.db.scalars(select(TallyMapping).where(
            TallyMapping.company_id == self.company.id, TallyMapping.entity_type == kind))
        return {m.tally_guid: m for m in rows if m.tally_guid}

    def _map(self, kind: str, mappings: dict, erp_id, rec: dict) -> None:
        m = mappings.get(rec["guid"])
        if m is None:
            m = TallyMapping(company_id=self.company.id, entity_type=kind, erp_id=erp_id)
            self.db.add(m)
            mappings[rec["guid"]] = m
        m.erp_id = erp_id
        m.tally_company_key = self.conn.tally_company_guid or self.conn.tally_company_name
        m.tally_guid, m.tally_master_id, m.tally_alter_id = rec["guid"], rec["master_id"], rec["alter_id"]
        m.voucher_number = rec["name"]   # locator: current Tally name
        m.last_synced_at, m.sync_direction = utcnow(), "inbound"
        self.max_alter_id = max(self.max_alter_id, rec["alter_id"] or 0)

    def _generic(self, kind: str, model, records: list[dict], apply, name_attr: str = "name") -> None:
        """Upsert by GUID mapping first, then by name; never duplicate."""
        mappings = self._mappings(kind)
        by_name = {getattr(o, name_attr): o for o in self.db.scalars(select(model).where(model.company_id == self.company.id))}
        by_id = {o.id: o for o in by_name.values()}
        for rec in records:
            if not rec.get("name") or not rec.get("guid"):
                continue
            m = mappings.get(rec["guid"])
            if m and m.tally_alter_id is not None and rec["alter_id"] <= m.tally_alter_id and not self.refresh:
                self._bump(kind, "unchanged")
                continue
            obj = by_id.get(m.erp_id) if m else None
            obj = obj or by_name.get(rec["name"])
            created = obj is None
            try:
                with self.db.begin_nested():
                    if created:
                        obj = model(company_id=self.company.id)
                        self.db.add(obj)
                    old_name = getattr(obj, name_attr, None)
                    apply(obj, rec)
                    self.db.flush()
                    self._map(kind, mappings, obj.id, rec)
                    self.db.flush()
            except Exception as exc:
                self._bump(kind, "failed")
                self.errors.append({"kind": kind, "name": rec["name"], "error": str(exc)[:500]})
                continue
            if old_name and old_name != rec["name"]:
                by_name.pop(old_name, None)
            by_name[rec["name"]], by_id[obj.id] = obj, obj
            self._bump(kind, "created" if created else "updated")
            audit.record(self.db, TALLY, f"{kind}.{'create' if created else 'update'}", kind, obj.id,
                         company_id=self.company.id, after={"name": rec["name"], "tally_guid": rec["guid"]})

    # -------------------------------------------------------------- per master type

    def groups(self, records: list[dict]) -> None:
        def apply(g: LedgerGroup, r: dict):
            g.name = r["name"]
            g.is_revenue = X.logical(r.get("isrevenue"))
            g.nature = codec.group_nature(g.is_revenue, X.logical(r.get("isdeemedpositive")))
            g.affects_gross_profit = X.logical(r.get("affectsgrossprofit"))
            g.is_reserved = bool(r.get("reserved_name"))
        # Parents first so the hierarchy can be linked in the second pass.
        self._generic("group", LedgerGroup, records, apply)
        by_name = {g.name: g for g in self.db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == self.company.id))}
        for r in records:
            g = by_name.get(r.get("name"))
            if g is not None:
                parent = by_name.get(r["parent"]) if r.get("parent") else None
                if parent is not g:
                    g.parent = parent
        self.db.flush()

    def _primary_ledger_group(self, groups: dict) -> LedgerGroup:
        """Tally keeps 'Profit & Loss A/c' directly under Primary; give it a reserved home on the liabilities side."""
        name = "Profit & Loss A/c"
        if name not in groups:
            groups[name] = LedgerGroup(company_id=self.company.id, name=name, nature="liability", is_reserved=True)
            self.db.add(groups[name])
        return groups[name]

    def ledgers(self, records: list[dict]) -> None:
        groups = {g.name: g for g in self.db.scalars(select(LedgerGroup).where(LedgerGroup.company_id == self.company.id))}

        def apply(led: Ledger, r: dict):
            group = groups.get(r.get("parent") or "")
            if group is None and not r.get("parent"):
                group = self._primary_ledger_group(groups)
            if group is None:
                raise ValueError(f"Group '{r.get('parent')}' does not exist")
            led.name, led.group = r["name"], group
            led.opening_balance = -X.amount(r.get("openingbalance"))
            led.is_billwise = X.logical(r.get("isbillwiseon"))
            days = X.amount(r.get("billcreditperiod"))
            led.credit_days = int(days) if days else None
            led.pan = r.get("incometaxnumber") or None
            led.tax_type = r.get("taxtype") or None
            led.gst_duty_head = r.get("gstdutyhead") or None
            for field, value in (r.get("details") or {}).items():   # address, GST, contacts, bank, MSME
                setattr(led, field, value)
            led.source_system = led.source_system or "tally"
            led.version = (led.version or 0) + 1
        self._generic("ledger", Ledger, records, apply)

    def voucher_types(self, records: list[dict]) -> None:
        names = {r["name"]: r for r in records if r.get("name")}

        def base_of(r: dict) -> str:
            seen = set()
            while r.get("parent") and r["parent"] not in seen and r["name"] not in BASE_VOUCHER_TYPES:
                seen.add(r["parent"])
                r = names.get(r["parent"], {"name": r["parent"]})
            return r["name"]

        def apply(vt: VoucherType, r: dict):
            vt.name = r["name"]
            vt.base_type = base_of(r)
            vt.numbering = "manual" if "manual" == (r.get("numberingmethod") or "").strip().lower() else "auto"
            vt.is_active = (r.get("isactive") or "Yes").lower() != "no"
        self._generic("voucher_type", VoucherType, records, apply)

    def units(self, records: list[dict]) -> None:
        def apply(u: Unit, r: dict):
            u.symbol, u.formal_name = r["name"], r.get("originalname") or None
            u.decimals = int(X.amount(r.get("decimalplaces")))
            if not X.logical(r.get("issimpleunit") or "Yes"):
                u.base_unit, u.additional_unit = r.get("baseunits"), r.get("additionalunits")
                u.conversion = X.amount(r.get("conversion")) or None
        self._generic("unit", Unit, records, apply, name_attr="symbol")

    def godowns(self, records: list[dict]) -> None:
        self._generic("godown", Godown, records, lambda g, r: setattr(g, "name", r["name"]))

    GST_FIELDS = ("hsn_code", "hsn_description", "gst_rate", "gst_rates", "taxability")

    def stock_groups(self, records: list[dict]) -> None:
        def apply(g: StockGroup, r: dict):
            g.name = r["name"]
            for f in self.GST_FIELDS:
                setattr(g, f, (r.get("details") or {}).get(f))
        self._generic("stock_group", StockGroup, records, apply)
        # Parents may arrive after their children, so link them once every group exists.
        groups = {g.name: g for g in self.db.scalars(select(StockGroup).where(StockGroup.company_id == self.company.id))}
        for r in records:
            g, parent = groups.get(r.get("name")), groups.get(r.get("parent") or "")
            if g is not None:
                g.parent_id = parent.id if parent and parent is not g else None
        self.db.flush()

    def _group_gst(self, group: StockGroup | None, groups_by_id: dict) -> tuple[dict, str]:
        """GST/HSN an item inherits: the nearest group up the chain that sets them, else the company."""
        seen = set()
        while group is not None and group.id not in seen:
            if group.hsn_code or group.gst_rate is not None:
                return {f: getattr(group, f) for f in self.GST_FIELDS}, f"stock group: {group.name}"
            seen.add(group.id)
            group = groups_by_id.get(group.parent_id)
        return {}, "company"

    def stock_items(self, records: list[dict]) -> None:
        groups = {g.name: g for g in self.db.scalars(select(StockGroup).where(StockGroup.company_id == self.company.id))}
        by_id = {g.id: g for g in groups.values()}

        def apply(i: StockItem, r: dict):
            i.name = r["name"]
            group = groups.get(r.get("parent") or "")
            i.group_id = group.id if group else None
            i.unit = r.get("baseunits") or None
            i.alternate_unit = None if r.get("additionalunits") in (None, "", "Not Applicable") else r["additionalunits"]
            qty, _ = X.quantity(r.get("openingbalance"))
            i.opening_qty = qty
            i.opening_rate = X.rate(r.get("openingrate"))
            i.opening_value = abs(X.amount(r.get("openingvalue")))
            d = dict(r.get("details") or {})
            inherited = d.pop("gst_inherited", False)
            if d.get("valuation_method") is None:
                d.pop("valuation_method", None)   # column has a default and is not nullable
            for field, value in d.items():
                setattr(i, field, value)
            i.gst_source = "item"
            if inherited:
                values, i.gst_source = self._group_gst(group, by_id)
                for f, value in values.items():
                    if value is not None:
                        setattr(i, f, value)
        self._generic("stock_item", StockItem, records, apply)

    def cost_centres(self, records: list[dict]) -> None:
        def apply(c: CostCentre, r: dict):
            c.name = r["name"]
            c.category = r.get("category") or "Primary Cost Category"
        self._generic("cost_centre", CostCentre, records, apply)

    # -------------------------------------------------------------- orchestration

    SPECS = [
        ("Group", codec.GROUP_FETCH, "groups"),
        ("Ledger", codec.LEDGER_FETCH, "ledgers"),
        ("Voucher Type", codec.VOUCHER_TYPE_FETCH, "voucher_types"),
        ("Unit", codec.UNIT_FETCH, "units"),
        ("Godown", codec.GODOWN_FETCH, "godowns"),
        ("Stock Group", codec.STOCK_GROUP_FETCH, "stock_groups"),
        ("Stock Item", codec.STOCK_ITEM_FETCH, "stock_items"),
        ("Cost Centre", codec.COST_CENTRE_FETCH, "cost_centres"),
    ]

    def pull(self, client: TallyClient, since_alter_id: int | None = None, on_type=None) -> None:
        """on_type(index, count, label) is called before each master type is read (for progress bars)."""
        for n, (ctype, fetch, method) in enumerate(self.SPECS):
            if on_type:
                on_type(n, len(self.SPECS), ctype.lower() + "s")
            records = client.masters(ctype, fetch, since_alter_id=since_alter_id)
            getattr(self, method)(records)
            for r in records:
                self.max_alter_id = max(self.max_alter_id, r.get("alter_id") or 0)
