"""SQLite persistence for the local supply-chain operations workspace."""

from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "supply_chain.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    standard_lead_time_days REAL NOT NULL CHECK (standard_lead_time_days >= 0),
    quality_rate_pct REAL NOT NULL CHECK (quality_rate_pct BETWEEN 0 AND 100),
    price_competitiveness_score REAL NOT NULL CHECK (price_competitiveness_score BETWEEN 1 AND 5),
    contract_status TEXT NOT NULL CHECK (contract_status IN ('Active', 'Expiring', 'Expired', 'Pending')),
    renewal_date TEXT,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('Low', 'Medium', 'High')),
    account_owner TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    on_hand REAL NOT NULL CHECK (on_hand >= 0),
    avg_weekly_demand REAL NOT NULL CHECK (avg_weekly_demand >= 0),
    lead_time_days REAL NOT NULL CHECK (lead_time_days >= 0),
    unit_cost REAL NOT NULL CHECK (unit_cost >= 0),
    minimum_stock REAL NOT NULL CHECK (minimum_stock >= 0),
    maximum_stock REAL NOT NULL CHECK (maximum_stock >= minimum_stock),
    carrying_cost_pct REAL NOT NULL CHECK (carrying_cost_pct BETWEEN 0 AND 100),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_requests (
    request_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES inventory(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    requester TEXT NOT NULL,
    department TEXT NOT NULL,
    justification TEXT NOT NULL,
    needed_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Pending', 'Approved', 'Rejected', 'Converted')),
    reviewed_by TEXT,
    review_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_id TEXT PRIMARY KEY,
    request_id TEXT UNIQUE REFERENCES purchase_requests(request_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id TEXT NOT NULL REFERENCES inventory(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_cost REAL NOT NULL CHECK (unit_cost >= 0),
    expected_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('Pending', 'Approved', 'Rejected', 'Completed')),
    actual_quantity INTEGER CHECK (actual_quantity IS NULL OR actual_quantity >= 0),
    actual_unit_cost REAL CHECK (actual_unit_cost IS NULL OR actual_unit_cost >= 0),
    department TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    purchase_order_id TEXT UNIQUE REFERENCES purchase_orders(po_id),
    supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
    product_id TEXT REFERENCES inventory(product_id),
    destination TEXT NOT NULL,
    expected_date TEXT NOT NULL,
    actual_date TEXT,
    status TEXT NOT NULL CHECK (status IN ('Scheduled', 'Preparing', 'In transit', 'Delayed', 'Delivered', 'Cancelled')),
    units INTEGER NOT NULL CHECK (units >= 0),
    transport_cost REAL NOT NULL CHECK (transport_cost >= 0),
    carrier TEXT NOT NULL DEFAULT '',
    delay_reason TEXT NOT NULL DEFAULT '',
    corrective_action TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_allocations (
    allocation_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES inventory(product_id),
    destination TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    status TEXT NOT NULL CHECK (status IN ('Pending', 'Allocated', 'Released', 'Consumed')),
    requested_by TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES inventory(product_id),
    movement_type TEXT NOT NULL CHECK (movement_type IN ('Receipt', 'Issue', 'Adjustment')),
    quantity_change REAL NOT NULL,
    unit_cost_at_movement REAL NOT NULL CHECK (unit_cost_at_movement >= 0),
    reference TEXT NOT NULL DEFAULT '',
    movement_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    stock_after REAL
);

CREATE TABLE IF NOT EXISTS corrective_actions (
    action_id TEXT PRIMARY KEY,
    issue_key TEXT,
    issue TEXT NOT NULL,
    operational_impact TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('High', 'Medium', 'Low')),
    recommendation TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (resolution_status IN ('Open', 'In progress', 'On hold', 'Resolved')),
    responsible TEXT NOT NULL,
    department TEXT NOT NULL,
    due_date TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
    period TEXT PRIMARY KEY,
    amount REAL NOT NULL CHECK (amount >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_shipments_status_expected
    ON shipments(status, expected_date);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status
    ON purchase_orders(status);
CREATE INDEX IF NOT EXISTS idx_movements_date_product
    ON stock_movements(movement_date, product_id);
CREATE INDEX IF NOT EXISTS idx_activities_occurred
    ON activities(occurred_at);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


@contextmanager
def transaction(db_path: str | Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _csv_rows(data_dir: Path, filename: str) -> list[dict[str, str]]:
    with (data_dir / filename).open("r", newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def _value(row: dict[str, str], key: str) -> str | None:
    value = row.get(key, "")
    return value.strip() if value and value.strip() else None


def _insert_csv_rows(
    connection: sqlite3.Connection,
    data_dir: Path,
    filename: str,
    table: str,
    columns: tuple[str, ...],
    transform=None,
) -> None:
    rows = _csv_rows(data_dir, filename)
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    insert_sql = f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})"
    for row in rows:
        values = transform(row) if transform else tuple(_value(row, column) for column in columns)
        connection.execute(insert_sql, values)


def _seed_suppliers(connection: sqlite3.Connection, data_dir: Path, timestamp: str) -> None:
    _insert_csv_rows(
        connection,
        data_dir,
        "suppliers.csv",
        "suppliers",
        (
            "supplier_id",
            "name",
            "category",
            "standard_lead_time_days",
            "quality_rate_pct",
            "price_competitiveness_score",
            "contract_status",
            "renewal_date",
            "risk_level",
            "account_owner",
            "notes",
            "created_at",
            "updated_at",
        ),
        lambda row: (
            _value(row, "supplier_id"),
            _value(row, "supplier"),
            _value(row, "category"),
            float(row["standard_lead_time_days"]),
            float(row["quality_rate_pct"]),
            float(row["price_competitiveness_score"]),
            _value(row, "contract_status"),
            _value(row, "contract_renewal_date"),
            _value(row, "risk_level"),
            _value(row, "account_owner"),
            _value(row, "notes") or "",
            timestamp,
            timestamp,
        ),
    )


def _seed_inventory(connection: sqlite3.Connection, data_dir: Path, timestamp: str) -> None:
    suppliers = {
        row["name"]: row["supplier_id"]
        for row in connection.execute("SELECT supplier_id, name FROM suppliers")
    }

    def transform(row: dict[str, str]) -> tuple:
        supplier_id = suppliers.get(row["supplier"])
        if not supplier_id:
            raise ValueError(f"Unknown sample inventory supplier: {row['supplier']}")
        return (
            row["product_id"],
            row["product_name"],
            row["category"],
            supplier_id,
            float(row["on_hand"]),
            float(row["avg_weekly_demand"]),
            float(row["lead_time_days"]),
            float(row["unit_cost"]),
            float(row["minimum_stock"]),
            float(row["maximum_stock"]),
            float(row["carrying_cost_pct"]),
            timestamp,
        )

    _insert_csv_rows(
        connection,
        data_dir,
        "inventory.csv",
        "inventory",
        (
            "product_id",
            "product_name",
            "category",
            "supplier_id",
            "on_hand",
            "avg_weekly_demand",
            "lead_time_days",
            "unit_cost",
            "minimum_stock",
            "maximum_stock",
            "carrying_cost_pct",
            "updated_at",
        ),
        transform,
    )


def _seed_orders_and_shipments(
    connection: sqlite3.Connection, data_dir: Path, timestamp: str
) -> None:
    supplier_ids = {
        row["name"]: row["supplier_id"]
        for row in connection.execute("SELECT supplier_id, name FROM suppliers")
    }

    _insert_csv_rows(
        connection,
        data_dir,
        "purchase_requests.csv",
        "purchase_requests",
        (
            "request_id",
            "product_id",
            "quantity",
            "requester",
            "department",
            "justification",
            "needed_by",
            "status",
            "reviewed_by",
            "review_notes",
            "created_at",
            "updated_at",
        ),
        lambda row: (
            _value(row, "request_id"),
            _value(row, "product_id"),
            int(row["quantity"]),
            _value(row, "requester"),
            _value(row, "department"),
            _value(row, "justification"),
            _value(row, "needed_by"),
            _value(row, "status"),
            _value(row, "reviewed_by"),
            _value(row, "review_notes") or "",
            _value(row, "created_at") or timestamp,
            timestamp,
        ),
    )

    _insert_csv_rows(
        connection,
        data_dir,
        "purchase_orders.csv",
        "purchase_orders",
        (
            "po_id",
            "request_id",
            "supplier_id",
            "product_id",
            "quantity",
            "unit_cost",
            "expected_date",
            "status",
            "actual_quantity",
            "actual_unit_cost",
            "department",
            "notes",
            "created_at",
            "updated_at",
        ),
        lambda row: (
            _value(row, "po_id"),
            _value(row, "request_id"),
            supplier_ids[row["supplier"]],
            _value(row, "product_id"),
            int(row["quantity"]),
            float(row["unit_cost"]),
            _value(row, "expected_date"),
            _value(row, "status"),
            int(row["actual_quantity"]) if _value(row, "actual_quantity") else None,
            float(row["actual_unit_cost"]) if _value(row, "actual_unit_cost") else None,
            _value(row, "department"),
            _value(row, "notes") or "",
            _value(row, "created_at") or timestamp,
            timestamp,
        ),
    )

    _insert_csv_rows(
        connection,
        data_dir,
        "shipments.csv",
        "shipments",
        (
            "shipment_id",
            "purchase_order_id",
            "supplier_id",
            "product_id",
            "destination",
            "expected_date",
            "actual_date",
            "status",
            "units",
            "transport_cost",
            "carrier",
            "delay_reason",
            "corrective_action",
            "created_at",
            "updated_at",
        ),
        lambda row: (
            _value(row, "shipment_id"),
            _value(row, "purchase_order_id"),
            supplier_ids[row["supplier"]],
            _value(row, "product_id"),
            _value(row, "destination"),
            _value(row, "expected_date"),
            _value(row, "actual_date"),
            _value(row, "status"),
            int(row["units"]),
            float(row["transport_cost"]),
            _value(row, "carrier") or "",
            _value(row, "delay_reason") or "",
            _value(row, "corrective_action") or "",
            _value(row, "created_at") or timestamp,
            timestamp,
        ),
    )


def _seed_stock_movements(
    connection: sqlite3.Connection, data_dir: Path
) -> None:
    _insert_csv_rows(
        connection,
        data_dir,
        "stock_movements.csv",
        "stock_movements",
        (
            "product_id",
            "movement_type",
            "quantity_change",
            "unit_cost_at_movement",
            "reference",
            "movement_date",
            "notes",
        ),
        lambda row: (
            _value(row, "product_id"),
            _value(row, "movement_type"),
            float(row["quantity_change"]),
            float(row["unit_cost_at_movement"]),
            _value(row, "reference") or "",
            _value(row, "movement_date"),
            _value(row, "notes") or "",
        ),
    )


def _seed_corrective_actions(
    connection: sqlite3.Connection, data_dir: Path, timestamp: str
) -> None:
    _insert_csv_rows(
        connection,
        data_dir,
        "corrective_actions.csv",
        "corrective_actions",
        (
            "action_id",
            "issue_key",
            "issue",
            "operational_impact",
            "priority",
            "recommendation",
            "resolution_status",
            "responsible",
            "department",
            "due_date",
            "notes",
            "created_at",
            "updated_at",
        ),
        lambda row: (
            _value(row, "action_id"),
            _value(row, "issue_key"),
            _value(row, "issue"),
            _value(row, "operational_impact"),
            _value(row, "priority"),
            _value(row, "recommendation"),
            _value(row, "resolution_status"),
            _value(row, "responsible"),
            _value(row, "department"),
            _value(row, "due_date"),
            _value(row, "notes") or "",
            _value(row, "created_at") or timestamp,
            timestamp,
        ),
    )


def _migrate_corrective_action_history(connection: sqlite3.Connection) -> None:
    has_unique_issue_key = False
    for index in connection.execute("PRAGMA index_list('corrective_actions')"):
        if not index["unique"]:
            continue
        index_name = str(index["name"]).replace('"', '""')
        columns = connection.execute(
            f'PRAGMA index_info("{index_name}")'
        ).fetchall()
        if [column["name"] for column in columns] == ["issue_key"]:
            has_unique_issue_key = True
            break

    if has_unique_issue_key:
        connection.execute(
            "ALTER TABLE corrective_actions RENAME TO corrective_actions_legacy"
        )
        connection.execute(
            """
            CREATE TABLE corrective_actions (
                action_id TEXT PRIMARY KEY,
                issue_key TEXT,
                issue TEXT NOT NULL,
                operational_impact TEXT NOT NULL,
                priority TEXT NOT NULL CHECK (priority IN ('High', 'Medium', 'Low')),
                recommendation TEXT NOT NULL,
                resolution_status TEXT NOT NULL
                    CHECK (resolution_status IN ('Open', 'In progress', 'On hold', 'Resolved')),
                responsible TEXT NOT NULL,
                department TEXT NOT NULL,
                due_date TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO corrective_actions (
                action_id, issue_key, issue, operational_impact, priority,
                recommendation, resolution_status, responsible, department,
                due_date, notes, created_at, updated_at
            )
            SELECT action_id, issue_key, issue, operational_impact, priority,
                   recommendation, resolution_status, responsible, department,
                   due_date, notes, created_at, updated_at
            FROM corrective_actions_legacy
            """
        )
        connection.execute("DROP TABLE corrective_actions_legacy")

    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_corrective_actions_active_issue
        ON corrective_actions(issue_key)
        WHERE issue_key IS NOT NULL AND resolution_status <> 'Resolved'
        """
    )


def initialize_database(
    db_path: str | Path = DB_PATH,
    data_dir: str | Path = DATA_DIR,
) -> None:
    """Create the local schema and load the illustrative CSVs once."""
    data_dir = Path(data_dir)
    timestamp = now_utc()
    connection = connect(db_path)
    try:
        connection.executescript(SCHEMA)
        connection.execute("BEGIN")
        _migrate_corrective_action_history(connection)
        seeded = connection.execute(
            "SELECT 1 FROM app_meta WHERE key = 'sample_data_seeded'"
        ).fetchone()
        if seeded is None:
            _seed_suppliers(connection, data_dir, timestamp)
            _seed_inventory(connection, data_dir, timestamp)
            _seed_orders_and_shipments(connection, data_dir, timestamp)
            _seed_stock_movements(connection, data_dir)
            _seed_corrective_actions(connection, data_dir, timestamp)
            connection.execute(
                "INSERT INTO app_meta (key, value) VALUES ('sample_data_seeded', 'yes')"
            )
        budget_rows = _csv_rows(data_dir, "budgets.csv")
        default_budget = (
            float(budget_rows[0]["budget_amount"]) if budget_rows else 50000.0
        )
        current_period = date.today().strftime("%Y-%m")
        connection.execute(
            "INSERT OR IGNORE INTO budgets (period, amount, updated_at) VALUES (?, ?, ?)",
            (current_period, default_budget, timestamp),
        )
        activity_exists = connection.execute(
            "SELECT 1 FROM activities LIMIT 1"
        ).fetchone()
        if activity_exists is None:
            connection.execute(
                """
                INSERT INTO activities (occurred_at, entity_type, entity_id, action, details)
                VALUES (?, 'workspace', 'local-demo', 'Initialized', ?)
                """,
                (timestamp, "Loaded illustrative local supply-chain records."),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fetch_all(
    sql: str, params: tuple = (), db_path: str | Path = DB_PATH
) -> list[sqlite3.Row]:
    connection = connect(db_path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def fetch_one(
    sql: str, params: tuple = (), db_path: str | Path = DB_PATH
) -> sqlite3.Row | None:
    connection = connect(db_path)
    try:
        return connection.execute(sql, params).fetchone()
    finally:
        connection.close()


def record_activity(
    connection: sqlite3.Connection,
    entity_type: str,
    entity_id: str,
    action: str,
    details: str,
) -> None:
    connection.execute(
        """
        INSERT INTO activities (occurred_at, entity_type, entity_id, action, details)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now_utc(), entity_type, entity_id, action, details),
    )
