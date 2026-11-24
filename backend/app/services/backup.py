"""Backup and restore of the whole database, to move Tally Cloud to another server without reading
everything from Tally again.

A backup is a .zip: manifest.json (version, schema revision, row counts) and one JSON-lines file per table.
It needs no PostgreSQL tools (pg_dump and pg_restore must match the server's version; this does not), and
a backup from an older version restores into a newer one: columns the backup lacks keep their defaults.

Restore replaces everything in one transaction: if anything fails, the server keeps its data as it was.
Not restored: signed-in sessions (everyone signs in again, with the passwords of the old server) and, when
this server is already activated, its own licence identity."""
import base64
import json
import threading
import uuid
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Integer, LargeBinary, func, inspect, select, text
from sqlalchemy.engine import Connection

from app import __version__
from app.core.db import Base, engine

FORMAT = "tallycloud-backup"
FORMAT_VERSION = 1
BATCH = 1000
SKIP = {"user_sessions"}            # sign-ins belong to this server
LICENCE = "license_state"


class BackupError(Exception):
    pass


def _tables():
    import app.models  # noqa: F401  (every table is on Base.metadata)
    return [t for t in Base.metadata.sorted_tables if t.name not in SKIP]


def schema_revision(conn: Connection) -> str | None:
    if not inspect(conn).has_table("alembic_version"):
        return None
    return conn.scalar(text("SELECT version_num FROM alembic_version"))


def _revisions() -> list[str]:
    """Migration revisions, oldest first."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    return [r.revision for r in reversed(list(ScriptDirectory.from_config(cfg).walk_revisions()))]


def _dump_value(v):
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (Decimal, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    return v


def _loader(col):
    """Turns a value from the backup back into what the column takes."""
    if isinstance(col.type, LargeBinary):
        return lambda v: base64.b64decode(v)
    try:
        kind = col.type.python_type
    except NotImplementedError:
        return None
    if kind is datetime:
        return datetime.fromisoformat
    if kind is date:
        return date.fromisoformat
    if kind is time:
        return time.fromisoformat
    if kind is Decimal:
        return Decimal
    if kind is uuid.UUID:
        return uuid.UUID
    return None


# ------------------------------------------------------------------ backup
def write_backup(path: Path, progress=None) -> dict:
    """Writes every table to the .zip at path, from one consistent snapshot. Returns the manifest."""
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            revision = schema_revision(conn)
            tables = _tables()
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for i, table in enumerate(tables):
                    if progress:
                        progress(i, len(tables), table.name)
                    n = 0
                    with zf.open(f"tables/{table.name}.jsonl", "w", force_zip64=True) as out:
                        rows = conn.execution_options(stream_results=True, yield_per=BATCH).execute(select(table))
                        for row in rows.mappings():
                            out.write(json.dumps({k: _dump_value(v) for k, v in row.items()},
                                                 ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                            n += 1
                    counts[table.name] = n
                manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
                            "schema_revision": revision, "created_at": datetime.now().astimezone().isoformat(),
                            "tables": counts}
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


# ------------------------------------------------------------------ backup of chosen companies
def _company_selects(company_ids: list):
    """For each table: the rows of these companies (the same links that Delete company follows), the users
    with access to them, and their audit trail."""
    from sqlalchemy import or_
    from app.services.accounting.delete_company import _filters
    per = [_filters(c) for c in company_ids]
    out = {}
    for t in _tables():
        conds = [f[t.name] for f in per if t.name in f]
        if conds:
            out[t.name] = select(t).where(or_(*conds))
    users, access = Base.metadata.tables["users"], Base.metadata.tables["user_company_access"]
    out["users"] = select(users).where(users.c.id.in_(
        select(access.c.user_id).where(access.c.company_id.in_(company_ids))))
    audit_t = Base.metadata.tables["audit_events"]
    out["audit_events"] = select(audit_t).where(audit_t.c.company_id.in_(company_ids))
    return out


def write_company_backup(path: Path, company_ids: list, progress=None) -> dict:
    """Only these companies: their books, masters, Tally link and sync state, users with access, audit trail.
    Imported on another server next to its own companies (Import companies)."""
    company_ids = [uuid.UUID(str(c)) for c in company_ids]
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        with conn.begin():
            revision = schema_revision(conn)
            companies_t = Base.metadata.tables["companies"]
            names = [{"id": str(r.id), "name": r.name} for r in conn.execute(
                select(companies_t.c.id, companies_t.c.name).where(companies_t.c.id.in_(company_ids)))]
            if len(names) != len(company_ids):
                raise BackupError("A chosen company was not found.")
            selects = _company_selects(company_ids)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for i, (name, stmt) in enumerate(selects.items()):
                    if progress:
                        progress(i, len(selects), name)
                    n = 0
                    with zf.open(f"tables/{name}.jsonl", "w", force_zip64=True) as out:
                        rows = conn.execution_options(stream_results=True, yield_per=BATCH).execute(stmt)
                        for row in rows.mappings():
                            out.write(json.dumps({k: _dump_value(v) for k, v in row.items()},
                                                 ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                            n += 1
                    counts[name] = n
                manifest = {"format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
                            "schema_revision": revision, "created_at": datetime.now().astimezone().isoformat(),
                            "scope": "companies", "companies": names, "tables": counts}
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def existing_companies(manifest: dict) -> list[str]:
    """Companies of a company backup that this server already has (same company, or the same name)."""
    if manifest.get("scope") != "companies":
        return []
    companies_t = Base.metadata.tables["companies"]
    ids = [uuid.UUID(c["id"]) for c in manifest["companies"]]
    names = [c["name"] for c in manifest["companies"]]
    with engine.connect() as conn:
        return [r.name for r in conn.execute(select(companies_t.c.name).where(
            companies_t.c.id.in_(ids) | companies_t.c.name.in_(names)))]


def import_companies(path: Path, progress=None, replace: bool = False) -> dict:
    """Adds the companies of a company backup to this server, next to its own; in one transaction.
    A company already here (same company or name) is replaced only with replace=True. Users are matched by
    user name: one already here keeps their password and only gets access; a new one comes as in the backup,
    never as super admin."""
    from sqlalchemy.orm import Session
    from app.services.accounting.delete_company import delete_company
    manifest = read_manifest(path)
    if manifest.get("scope") != "companies":
        raise BackupError("This is a backup of a whole server. Use Restore backup to replace everything.")
    T = Base.metadata.tables
    tables = [t for t in _tables() if t.name not in ("users",)]
    with zipfile.ZipFile(path) as zf, engine.begin() as conn:
        check_compatible(manifest, conn)
        ids = [uuid.UUID(c["id"]) for c in manifest["companies"]]
        names = [c["name"] for c in manifest["companies"]]
        clash = list(conn.execute(select(T["companies"].c.id, T["companies"].c.name).where(
            T["companies"].c.id.in_(ids) | T["companies"].c.name.in_(names))))
        if clash and not replace:
            raise BackupError("Already on this server: " + ", ".join(r.name for r in clash)
                              + ". Choose to replace them, or delete them first.")
        for r in clash:
            delete_company(conn, r.id)

        # users: by user name
        users_t = T["users"]
        here = dict(conn.execute(select(users_t.c.username, users_t.c.id)).all())
        user_map: dict = {}
        loaders = {c.name: _loader(c) for c in users_t.columns}
        for raw in _rows(zf, "users"):
            if raw["username"] in here:
                user_map[raw["id"]] = here[raw["username"]]
                continue
            row = {k: (loaders[k](v) if loaders.get(k) and v is not None else v) for k, v in raw.items() if k in loaders}
            row.update(is_superadmin=False, api_key_hash=None, customer_id=None)
            conn.execute(users_t.insert(), row)
            user_map[raw["id"]] = row["id"]

        customers = {str(c) for (c,) in conn.execute(select(T["customers"].c.id))} if "customers" in T else set()
        for i, table in enumerate(tables + [T["audit_events"]]):
            if progress:
                progress(i, len(tables) + 1, table.name)
            loaders = {c.name: _loader(c) for c in table.columns}
            rows = _rows(zf, table.name)
            if any(fk.column.table is table for fk in table.foreign_keys):
                rows = iter(_parents_first(list(rows), table))
            batch = []
            for raw in rows:
                if table.name == "companies" and raw.get("customer_id") not in customers:
                    raw["customer_id"] = None
                if table.name == "user_company_access":
                    if raw["user_id"] not in user_map:
                        continue
                    raw["user_id"] = str(user_map[raw["user_id"]])
                if table.name == "audit_events":
                    raw.pop("id", None)           # its own numbering here
                row = {k: (loaders[k](v) if loaders.get(k) and v is not None else v) for k, v in raw.items() if k in loaders}
                batch.append(row)
                if len(batch) >= BATCH:
                    _insert(conn, table, batch)
                    batch = []
            if batch:
                _insert(conn, table, batch)

        # the licence's company limit counts imported companies too (checked before anything is kept)
        from app.core.config import settings
        if settings.license_mode == "client":
            from app.licensing import enforce
            with Session(bind=conn) as s:
                ent = enforce.installation_entitlement(s)
                count = conn.scalar(select(func.count()).select_from(T["companies"]))
                if ent.max_companies is not None and count > ent.max_companies:
                    raise BackupError(f"Your licence allows {ent.max_companies} Tally companies; with these there "
                                      f"would be {count}. Contact Voyager for more, or delete a company first.")
    return {**manifest, "replaced": [r.name for r in clash]}


# ------------------------------------------------------------------ restore
def read_manifest(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        raise BackupError("This is not a Tally Cloud backup file (.zip made by Backup on the System admin page).")
    if manifest.get("format") != FORMAT:
        raise BackupError("This is not a Tally Cloud backup file.")
    if int(manifest.get("format_version") or 0) > FORMAT_VERSION:
        raise BackupError("This backup was made by a newer version of Tally Cloud. Update this server first.")
    return manifest


def check_compatible(manifest: dict, conn: Connection) -> None:
    ours, theirs = schema_revision(conn), manifest.get("schema_revision")
    if not ours or not theirs or ours == theirs:
        return
    order = _revisions()
    if theirs not in order:
        raise BackupError(f"This backup was made by a newer version of Tally Cloud ({manifest.get('app_version')}). "
                          f"Update this server to that version or later first, then restore.")
    if ours in order and order.index(theirs) > order.index(ours):
        raise BackupError(f"This backup was made by a newer version of Tally Cloud ({manifest.get('app_version')}). "
                          f"Update this server first, then restore.")


def _rows(zf: zipfile.ZipFile, name: str):
    try:
        f = zf.open(f"tables/{name}.jsonl")
    except KeyError:
        return
    with f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _parents_first(rows: list[dict], table) -> list[dict]:
    """A table that points at itself (a group under a group): parents go in before their children."""
    pk = [c.name for c in table.primary_key.columns]
    selfs = [(fk.parent.name, fk.column.name) for fk in table.foreign_keys if fk.column.table is table]
    if not selfs or len(pk) != 1:
        return rows
    done, out, left = set(), [], rows
    while left:
        ready = [r for r in left if all(r.get(c) is None or r.get(c) in done or r.get(c) == r.get(pk[0])
                                        for c, _ in selfs)]
        if not ready:                       # a loop or a missing parent: let the database say what is wrong
            out += left
            break
        out += ready
        done |= {r[pk[0]] for r in ready}
        ids = {id(r) for r in ready}
        left = [r for r in left if id(r) not in ids]
    return out


def restore_backup(path: Path, progress=None) -> dict:
    """Replaces all data with the backup's, in one transaction. Returns the manifest."""
    manifest = read_manifest(path)
    if manifest.get("scope") == "companies":
        raise BackupError("This backup holds only some companies. Use Import companies to add them to this server.")
    tables = _tables()
    with zipfile.ZipFile(path) as zf, engine.begin() as conn:
        check_compatible(manifest, conn)
        keep_licence = bool(conn.execute(text(
            f"SELECT 1 FROM {LICENCE} WHERE installation_secret IS NOT NULL")).first()) \
            if inspect(conn).has_table(LICENCE) else False
        wipe = [t.name for t in tables if not (t.name == LICENCE and keep_licence)] + list(SKIP)
        # TRUNCATE, not DELETE: the append-only guards on history tables are row triggers and stay in place
        conn.execute(text(f"TRUNCATE {', '.join(wipe)} RESTART IDENTITY CASCADE"))
        for i, table in enumerate(tables):
            if table.name == LICENCE and keep_licence:
                continue
            if progress:
                progress(i, len(tables), table.name)
            loaders = {c.name: _loader(c) for c in table.columns}

            def convert(raw: dict) -> dict:
                row = {}
                for k, v in raw.items():
                    if k in loaders:          # a column dropped since the backup is left out
                        f = loaders[k]
                        row[k] = f(v) if (f and v is not None) else v
                return row

            rows = _rows(zf, table.name)
            if any(fk.column.table is table for fk in table.foreign_keys):
                rows = iter(_parents_first(list(rows), table))
            batch = []
            for raw in rows:
                batch.append(convert(raw))
                if len(batch) >= BATCH:
                    _insert(conn, table, batch)
                    batch = []
            if batch:
                _insert(conn, table, batch)
            _reset_sequence(conn, table)
    return manifest


def _insert(conn: Connection, table, rows: list[dict]) -> None:
    # rows from one table share their columns, except backups that cross a version: group them by columns
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    for batch in groups.values():
        conn.execute(table.insert(), batch)


def _reset_sequence(conn: Connection, table) -> None:
    """Integer ids handed out by a sequence carry on after the highest restored one."""
    for col in table.primary_key.columns:
        if isinstance(col.type, Integer):
            seq = conn.scalar(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": col.name})
            if seq:
                conn.execute(text(f"SELECT setval('{seq}', COALESCE((SELECT MAX({col.name}) FROM {table.name}), 0) + 1, false)"))


# ------------------------------------------------------------------ restore in the background (web page)
JOB: dict = {"state": "never"}
_LOCK = threading.Lock()


def busy() -> bool:
    return JOB.get("state") == "running"


def start_restore(path: Path, who: str, on_done=None, companies: bool = False, replace: bool = False) -> None:
    with _LOCK:
        if busy():
            raise BackupError("A restore is already running.")
        JOB.clear()
        JOB.update(state="running", by=who, started_at=datetime.now().astimezone().isoformat(),
                   step="Starting", done=0, total=0)

    def tick(i, n, name):
        JOB.update(done=i, total=n, step=name)

    def run():
        try:
            if companies:
                m = import_companies(path, tick, replace=replace)
                from app.licensing import enforce
                enforce.forget_cached()
                names = ", ".join(c["name"] for c in m["companies"])
                JOB.update(state="ok", done=JOB.get("total", 0), step="Done", companies=True,
                           message=f"Imported {names}" + (f" (replaced the copy that was here)" if m["replaced"] else "")
                                   + ". Reload the page to see them.", tables=m.get("tables"))
                return
            m = restore_backup(path, tick)
            from app.licensing import enforce
            enforce.forget_cached()             # the licence may have come with the backup
            JOB.update(state="ok", done=JOB.get("total", 0), step="Done",
                       message=f"Restored the backup of {m.get('created_at', '')[:16].replace('T', ' ')} "
                               f"(version {m.get('app_version')}). Sign in again with the users and passwords "
                               f"of the old server.", tables=m.get("tables"))
        except Exception as exc:                # nothing changed: the transaction was rolled back
            JOB.update(state="failed", message=f"Restore failed, nothing was changed: {exc}"[:2000])
        finally:
            JOB["finished_at"] = datetime.now().astimezone().isoformat()
            try:
                path.unlink()
            except OSError:
                pass
            if on_done:
                on_done(JOB)

    threading.Thread(target=run, name="restore", daemon=True).start()
