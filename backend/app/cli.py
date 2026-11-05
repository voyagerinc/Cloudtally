"""Operator CLI: python -m app.cli <command> --help"""
import argparse
import getpass
import json
import sys
from datetime import date

from sqlalchemy import select

from app.audit import Actor
from app.connectors.tally import TallyClient, TallyError
from app.core.db import SessionLocal
from app.core.security import ROLES, check_password_rules, hash_key, hash_password, new_api_key
from app.models import Company, TallyConnector, User, UserCompanyAccess
from app.services.accounting import masters
from app.services.sync import common, initial_import, reconcile

CLI = Actor("cli", "system")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def init_db(_args) -> None:
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(cfg, "head")
    print("Database schema is up to date.")


def _ask_password(username: str) -> str:
    """Read a new password twice from the terminal (nothing is shown while typing)."""
    while True:
        pw = getpass.getpass(f"Password for {username}: ")
        if reason := check_password_rules(pw, username):
            print(reason)
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("The two passwords differ; try again.")
            continue
        return pw


def create_user(args) -> None:
    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.username == args.username)):
            sys.exit(f"User {args.username} already exists")
        password = None if args.api_key_only else _ask_password(args.username)
        key = new_api_key() if args.api_key or args.api_key_only else None
        user = User(username=args.username, display_name=args.name, is_superadmin=args.superadmin,
                    password_hash=hash_password(password) if password else None,
                    api_key_hash=hash_key(key) if key else None, created_by="cli")
        db.add(user)
        db.flush()
        if args.company:
            company = db.scalar(select(Company).where(Company.name == args.company))
            if company is None:
                sys.exit(f"Company {args.company} not found")
            db.add(UserCompanyAccess(user_id=user.id, company_id=company.id, role=args.role))
        db.commit()
        print(f"Created {args.username}." + (" Sign in with this user ID and the password you typed." if password else ""))
        if key:
            print(f"API key for integrations (shown once):\n{key}")


def license_keygen(args) -> None:
    """Licence portal only: make the Ed25519 key pair that signs entitlements and installers."""
    import os
    from pathlib import Path
    from app.licensing.core import generate_keypair
    path = Path(args.out)
    if path.exists() and not args.force:
        sys.exit(f"{path} already exists. Replacing it makes every installed server's licence invalid; "
                 "use --force only if the key was lost or leaked.")
    pem, public = generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    print(f"Private key written to {path} (keep it secret; it never goes into git or an installer).")
    print("Add to backend/.env on the licence portal:")
    print(f"  ERP_LICENSE_SIGNING_KEY_FILE={path}")
    print("Public key, for ERP_LICENSE_PUBLIC_KEY on every installation (the installer sets it):")
    print(f"  {public}")


def release_token(args) -> None:
    """Licence portal: a token with which GitHub Actions uploads releases (it can do nothing else).

    Writes its hash to backend/.env (run as root on the server) and prints the token once, for the
    GitHub secret TALLYCLOUD_RELEASE_TOKEN. Running it again replaces the old token."""
    import re
    import secrets
    from pathlib import Path
    token = "rel_" + secrets.token_urlsafe(32)
    env = Path(args.env)
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"ERP_RELEASE_TOKEN_HASH={hash_key(token)}"
    text = re.sub(r"(?m)^ERP_RELEASE_TOKEN_HASH=.*$", line, text) if "ERP_RELEASE_TOKEN_HASH=" in text \
        else text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env.write_text(text, encoding="utf-8")
    print(f"Release token written to {env} (as a hash). Restart the app: sudo systemctl restart tallycloud")
    print("Put this in GitHub: repository -> Settings -> Secrets and variables -> Actions -> New repository secret")
    print("  Name:  TALLYCLOUD_RELEASE_TOKEN")
    print(f"  Value: {token}")


def set_password(args) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            sys.exit("No such user")
        user.password_hash = hash_password(_ask_password(args.username))
        user.must_change_password = False
        user.failed_logins, user.locked_until, user.is_active = 0, None, True
        db.commit()
        print(f"Password of {args.username} changed; the user is unlocked and active.")


def rotate_key(args) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            sys.exit("No such user")
        key = new_api_key()
        user.api_key_hash = hash_key(key)
        db.commit()
        print(f"New API key for {args.username} (shown once):\n{key}")


def discover(args) -> None:
    with TallyClient(args.host, args.port, args.company or "") as client:
        try:
            _print(initial_import.discover(client) if args.company else
                   {"server": client.ping(), "companies": client.list_companies()})
        except TallyError as exc:
            sys.exit(f"{exc.code}: {exc.message}")


def link(args) -> None:
    """Create an ERP company bound to a Tally company, in shadow mode, and import masters."""
    with TallyClient(args.host, args.port, args.tally_company) as client:
        found = next((c for c in client.list_companies() if c["name"] == args.tally_company), None)
        if found is None:
            sys.exit(f"'{args.tally_company}' is not open in Tally")
        with SessionLocal() as db:
            company = masters.create_company(db, {"name": args.name or args.tally_company,
                                                  "books_from": date.fromisoformat(found["books_from"] or found["starting_from"])},
                                             CLI, with_defaults=False)
            conn = TallyConnector(company_id=company.id, host=args.host, port=args.port,
                                  tally_company_name=args.tally_company, tally_company_guid=found["guid"],
                                  enabled=True, inbound_enabled=True, outbound_enabled=False)
            db.add(conn)
            db.commit()
            batch = initial_import.import_masters(db, company, conn, client, CLI)
            _print({"company_id": company.id, "masters": batch.status, "counts": batch.counts,
                    "errors": batch.errors[:20], "mode": "shadow (outbound disabled)"})


def _company(db, name: str) -> tuple[Company, TallyConnector]:
    company = db.scalar(select(Company).where(Company.name == name))
    if company is None:
        sys.exit(f"Company {name} not found")
    conn = db.get(TallyConnector, company.id)
    if conn is None:
        sys.exit(f"Company {name} has no Tally connector")
    return company, conn


def import_vouchers(args) -> None:
    with SessionLocal() as db:
        company, conn = _company(db, args.company)
        with common.client_for(conn) as client:
            batch = initial_import.import_vouchers(db, company, conn, client, CLI, date.fromisoformat(args.from_date),
                                                   date.fromisoformat(args.to_date), args.batch_size)
        _print({"status": batch.status, "counts": batch.counts, "errors": batch.errors[:20]})


def run_reconcile(args) -> None:
    with SessionLocal() as db:
        company, conn = _company(db, args.company)
        with common.client_for(conn) as client:
            run = reconcile.run(db, company, conn, client, date.fromisoformat(args.from_date),
                                date.fromisoformat(args.to_date), CLI, compare_ledgers=not args.no_ledgers)
        db.commit()
        _print({"status": run.status, "differences": run.differences, "result": run.result})


def sync_once(_args) -> None:
    from app.workers.sync_worker import run_once
    _print(run_once())


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="apply database migrations").set_defaults(fn=init_db)

    u = sub.add_parser("create-user", help="create a user who signs in with a password (asked for here)")
    u.add_argument("username")
    u.add_argument("--name")
    u.add_argument("--superadmin", action="store_true")
    u.add_argument("--api-key", action="store_true", help="also issue an API key for integrations")
    u.add_argument("--api-key-only", action="store_true", help="integration user: API key, no password")
    u.add_argument("--company", help="grant access to this company")
    u.add_argument("--role", default="accountant", choices=sorted(ROLES))
    u.set_defaults(fn=create_user)

    kg = sub.add_parser("license-keygen", help="licence portal: make the key pair that signs licences")
    kg.add_argument("--out", default="/etc/tallycloud/license_signing.pem")
    kg.add_argument("--force", action="store_true")
    kg.set_defaults(fn=license_keygen)

    rt = sub.add_parser("release-token", help="licence portal: token for GitHub Actions to upload releases")
    rt.add_argument("--env", default=".env", help="the .env file to write the token's hash to")
    rt.set_defaults(fn=release_token)

    sp = sub.add_parser("set-password", help="set a user's password (also unlocks and switches the user on)")
    sp.add_argument("username")
    sp.set_defaults(fn=set_password)

    r = sub.add_parser("rotate-key", help="issue a new API key")
    r.add_argument("username")
    r.set_defaults(fn=rotate_key)

    d = sub.add_parser("discover", help="list companies in Tally (read-only)")
    d.add_argument("--host", default="localhost")
    d.add_argument("--port", type=int, default=9000)
    d.add_argument("--company", help="also report object counts for this company")
    d.set_defaults(fn=discover)

    lk = sub.add_parser("link", help="create ERP company from a Tally company (shadow mode) and import masters")
    lk.add_argument("tally_company")
    lk.add_argument("--name", help="ERP company name (default: same as Tally)")
    lk.add_argument("--host", default="localhost")
    lk.add_argument("--port", type=int, default=9000)
    lk.set_defaults(fn=link)

    iv = sub.add_parser("import-vouchers", help="import Tally vouchers for a date range (idempotent)")
    iv.add_argument("company")
    iv.add_argument("from_date")
    iv.add_argument("to_date")
    iv.add_argument("--batch-size", type=int, default=None, help="vouchers per Tally request")
    iv.set_defaults(fn=import_vouchers)

    rc = sub.add_parser("reconcile", help="compare ERP with Tally for a date range")
    rc.add_argument("company")
    rc.add_argument("from_date")
    rc.add_argument("to_date")
    rc.add_argument("--no-ledgers", action="store_true")
    rc.set_defaults(fn=run_reconcile)

    sub.add_parser("sync-once", help="run one sync cycle for all enabled companies").set_defaults(fn=sync_once)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
