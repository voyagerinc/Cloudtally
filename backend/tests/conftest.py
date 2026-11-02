import os
import sys
from pathlib import Path

os.environ.setdefault("ERP_DATABASE_URL", "postgresql+psycopg://erp@localhost:55432/tally_erp_test")
os.environ.setdefault("ERP_SYNC_BACKOFF_BASE", "0")
os.environ.setdefault("ERP_TALLY_TIMEOUT", "5")
os.environ.setdefault("ERP_SYNC_VOUCHER_SCAN_SECONDS", "0")
os.environ.setdefault("ERP_SYNC_VOUCHER_BATCH_PAUSE", "0")
os.environ.setdefault("ERP_AUTO_SYNC", "false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.models  # noqa: E402,F401
from app.audit import Actor  # noqa: E402
from app.core.db import Base, SessionLocal, engine, install_db_extras  # noqa: E402
from app.models import Company, TallyConnector  # noqa: E402
from app.services.accounting import masters  # noqa: E402
from app.services.sync import common  # noqa: E402
from app.services.sync.initial_import import import_masters  # noqa: E402
from tools.mock_tally import MockTally, demo  # noqa: E402

TALLY_COMPANY = "Demo Trading Co"
USER = Actor("asha", "user")
CHECKER = Actor("ravi", "user")


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        install_db_extras(conn)
    yield


@pytest.fixture(autouse=True)
def clean_tables(schema):
    with engine.begin() as conn:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture()
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def mock():
    m = MockTally()
    demo(m, TALLY_COMPANY)
    m.port = m.start()
    yield m
    m.stop()


@pytest.fixture()
def tc(mock):
    return mock.companies[TALLY_COMPANY]


@pytest.fixture()
def company(db, mock):
    """ERP company linked to the mock Tally company, masters imported, sync enabled both ways."""
    c = masters.create_company(db, {"name": "Demo Trading Co", "books_from": date(2026, 4, 1)}, USER)
    conn = TallyConnector(company_id=c.id, host="127.0.0.1", port=mock.port, tally_company_name=TALLY_COMPANY,
                          enabled=True, outbound_enabled=True, inbound_enabled=True)
    db.add(conn)
    db.commit()
    client = common.client_for(conn)
    batch = import_masters(db, c, conn, client, USER)
    client.close()
    assert batch.status == "completed", batch.errors
    return db.get(Company, c.id)
