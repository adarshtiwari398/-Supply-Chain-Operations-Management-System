"""Supply-chain management workflows and business calculations."""

from __future__ import annotations

import math
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from database import DB_PATH, fetch_all, fetch_one, now_utc, record_activity, transaction
from operations import REQUIRED_INVENTORY_COLUMNS, validate_inventory

REQUEST_STATUSES = ("Pending", "Approved", "Rejected", "Converted")
PO_STATUSES = ("Pending", "Approved", "Rejected", "Completed")
SHIPMENT_STATUSES = (
    "Scheduled",
    "Preparing",
    "In transit",
    "Delayed",
    "Delivered",
    "Cancelled",
)
ACTION_STATUSES = ("Open", "In progress", "On hold", "Resolved")
PRIORITIES = ("High", "Medium", "Low")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _required_text(value: Any, label: str) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise ValueError(f"{label} is required.")
    return result


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a number.") from error
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}.")
    return result


def _quantity(value: Any, label: str = "Quantity") -> int:
    result = _number(value, label, minimum=1)
    if not result.is_integer():
        raise ValueError(f"{label} must be a whole number.")
    return int(result)


def _date_text(value: Any, label: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    result = _required_text(value, label)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as error:
        raise ValueError(f"{label} must be a valid YYYY-MM-DD date.") from error


def _optional_date_text(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _date_text(value, label)


def _enum(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(allowed)}.")
    return value


def _frame(sql: str, params: tuple = (), db_path: str | Path = DB_PATH) -> pd.DataFrame:
    return pd.DataFrame([dict(row) for row in fetch_all(sql, params, db_path)])


def _row(sql: str, params: tuple = (), db_path: str | Path = DB_PATH) -> dict | None:
    result = fetch_one(sql, params, db_path)
    return dict(result) if result is not None else None


def _require_record(
    connection: sqlite3.Connection, table: str, key_column: str, key_value: str
) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE {key_column} = ?", (key_value,)
    ).fetchone()
    if row is None:
        raise ValueError(f"{table.replace('_', ' ').title()} '{key_value}' was not found.")
    return row


def _insert_inventory_movement(
    connection: sqlite3.Connection,
    product_id: str,
    movement_type: str,
    quantity_change: float,
    unit_cost: float,
    reference: str,
    notes: str,
    stock_after: float,
    as_of: date | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO stock_movements (
            product_id, movement_type, quantity_change, unit_cost_at_movement,
            reference, movement_date, notes, stock_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            movement_type,
            quantity_change,
            unit_cost,
            reference,
            (as_of or date.today()).isoformat(),
            notes,
            stock_after,
        ),
    )


def list_suppliers(db_path: str | Path = DB_PATH, as_of: date | None = None) -> pd.DataFrame:
    suppliers = _frame(
        """
        SELECT s.*,
               COUNT(sh.shipment_id) AS delivered_count,
               SUM(CASE WHEN sh.actual_date <= sh.expected_date THEN 1 ELSE 0 END) AS on_time_count
        FROM suppliers s
        LEFT JOIN shipments sh
          ON sh.supplier_id = s.supplier_id
         AND sh.status = 'Delivered'
         AND sh.actual_date IS NOT NULL
        GROUP BY s.supplier_id
        ORDER BY s.name
        """,
        db_path=db_path,
    )
    if suppliers.empty:
        return suppliers

    today = as_of or date.today()
    suppliers["on_time_rate_pct"] = (
        suppliers["on_time_count"].fillna(0)
        .div(suppliers["delivered_count"].replace(0, pd.NA))
        .mul(100)
    )
    suppliers["effective_contract_status"] = suppliers.apply(
        lambda row: _effective_contract_status(
            row["contract_status"], row["renewal_date"], today
        ),
        axis=1,
    )
    suppliers["evaluation_score"] = suppliers.apply(
        _supplier_score, axis=1, today=today
    )
    suppliers["selection_recommendation"] = suppliers.apply(
        _supplier_recommendation, axis=1
    )
    return suppliers.sort_values(
        ["evaluation_score", "name"], ascending=[False, True]
    ).reset_index(drop=True)


def _effective_contract_status(status: str, renewal_date: Any, as_of: date) -> str:
    if pd.notna(renewal_date) and renewal_date:
        renewal = date.fromisoformat(str(renewal_date))
        if renewal < as_of:
            return "Expired"
        if status == "Active" and (renewal - as_of).days <= 45:
            return "Expiring"
    return status


def _supplier_score(row: pd.Series, today: date) -> float:
    delivery = row["on_time_rate_pct"]
    if pd.isna(delivery):
        return float("nan")
    delivery_score = float(delivery)
    contract_score = {
        "Active": 100.0,
        "Expiring": 60.0,
        "Pending": 40.0,
        "Expired": 0.0,
    }[_effective_contract_status(row["contract_status"], row["renewal_date"], today)]
    return round(
        delivery_score * 0.4
        + float(row["quality_rate_pct"]) * 0.3
        + float(row["price_competitiveness_score"]) / 5 * 100 * 0.2
        + contract_score * 0.1,
        1,
    )


def _supplier_recommendation(row: pd.Series) -> str:
    if row["effective_contract_status"] == "Expired":
        return "Renew or qualify an alternative before new award"
    if pd.isna(row["on_time_rate_pct"]):
        return "Limited delivery history; complete due diligence"
    if row["on_time_rate_pct"] < 80 or row["quality_rate_pct"] < 95:
        return "Review corrective plan and compare alternatives"
    if row["evaluation_score"] >= 80:
        return "Preferred for comparison; confirm capacity and terms"
    return "Compare total cost, service levels, and delivery capacity"


def create_supplier(
    name: str,
    category: str,
    standard_lead_time_days: float,
    quality_rate_pct: float,
    price_competitiveness_score: float,
    contract_status: str,
    renewal_date: str | date | None,
    risk_level: str,
    account_owner: str,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> str:
    name = _required_text(name, "Supplier name")
    category = _required_text(category, "Supplier category")
    lead_time = _number(standard_lead_time_days, "Standard lead time")
    quality = _number(quality_rate_pct, "Quality acceptance", minimum=0)
    if quality > 100:
        raise ValueError("Quality acceptance cannot exceed 100%.")
    price_score = _number(price_competitiveness_score, "Price score", minimum=1)
    if price_score > 5:
        raise ValueError("Price score must be between 1 and 5.")
    contract_status = _enum(
        contract_status, ("Active", "Expiring", "Expired", "Pending"), "Contract status"
    )
    risk_level = _enum(risk_level, ("Low", "Medium", "High"), "Risk classification")
    account_owner = _required_text(account_owner, "Account owner")
    renewal = _optional_date_text(renewal_date, "Contract renewal date")
    supplier_id = _new_id("SUP")
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            connection.execute(
                """
                INSERT INTO suppliers (
                    supplier_id, name, category, standard_lead_time_days,
                    quality_rate_pct, price_competitiveness_score, contract_status,
                    renewal_date, risk_level, account_owner, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    supplier_id, name, category, lead_time, quality, price_score,
                    contract_status, renewal, risk_level, account_owner, notes.strip(),
                    timestamp, timestamp,
                ),
            )
            record_activity(connection, "supplier", supplier_id, "Registered", name)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not register supplier: {error}.") from error
    return supplier_id


def update_supplier(
    supplier_id: str,
    name: str,
    category: str,
    standard_lead_time_days: float,
    quality_rate_pct: float,
    price_competitiveness_score: float,
    contract_status: str,
    renewal_date: str | date | None,
    risk_level: str,
    account_owner: str,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    name = _required_text(name, "Supplier name")
    category = _required_text(category, "Supplier category")
    lead_time = _number(standard_lead_time_days, "Standard lead time")
    quality = _number(quality_rate_pct, "Quality acceptance")
    if quality > 100:
        raise ValueError("Quality acceptance cannot exceed 100%.")
    price_score = _number(price_competitiveness_score, "Price score", minimum=1)
    if price_score > 5:
        raise ValueError("Price score must be between 1 and 5.")
    contract_status = _enum(
        contract_status, ("Active", "Expiring", "Expired", "Pending"), "Contract status"
    )
    risk_level = _enum(risk_level, ("Low", "Medium", "High"), "Risk classification")
    account_owner = _required_text(account_owner, "Account owner")
    renewal = _optional_date_text(renewal_date, "Contract renewal date")
    try:
        with transaction(db_path) as connection:
            _require_record(connection, "suppliers", "supplier_id", supplier_id)
            connection.execute(
                """
                UPDATE suppliers
                SET name = ?, category = ?, standard_lead_time_days = ?,
                    quality_rate_pct = ?, price_competitiveness_score = ?,
                    contract_status = ?, renewal_date = ?, risk_level = ?,
                    account_owner = ?, notes = ?, updated_at = ?
                WHERE supplier_id = ?
                """,
                (
                    name, category, lead_time, quality, price_score, contract_status,
                    renewal, risk_level, account_owner, notes.strip(), now_utc(), supplier_id,
                ),
            )
            record_activity(connection, "supplier", supplier_id, "Updated", name)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not update supplier: {error}.") from error


def delete_supplier(supplier_id: str, db_path: str | Path = DB_PATH) -> None:
    try:
        with transaction(db_path) as connection:
            row = _require_record(connection, "suppliers", "supplier_id", supplier_id)
            connection.execute("DELETE FROM suppliers WHERE supplier_id = ?", (supplier_id,))
            record_activity(connection, "supplier", supplier_id, "Deleted", row["name"])
    except sqlite3.IntegrityError as error:
        raise ValueError(
            "Supplier is linked to inventory or transaction history and cannot be deleted."
        ) from error


def list_inventory(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    return _frame(
        """
        SELECT i.*, s.name AS supplier,
               COALESCE(a.allocated_qty, 0) AS allocated_qty,
               i.on_hand - COALESCE(a.allocated_qty, 0) AS available_qty,
               COALESCE(p.incoming_qty, 0) AS incoming_qty,
               i.on_hand * i.unit_cost AS inventory_value
        FROM inventory i
        JOIN suppliers s ON s.supplier_id = i.supplier_id
        LEFT JOIN (
            SELECT product_id, SUM(quantity) AS allocated_qty
            FROM resource_allocations WHERE status = 'Allocated'
            GROUP BY product_id
        ) a ON a.product_id = i.product_id
        LEFT JOIN (
            SELECT product_id, SUM(quantity) AS incoming_qty
            FROM purchase_orders WHERE status = 'Approved'
            GROUP BY product_id
        ) p ON p.product_id = i.product_id
        ORDER BY i.product_name
        """,
        db_path=db_path,
    )


def inventory_control(
    db_path: str | Path = DB_PATH,
    safety_weeks: float = 2,
) -> pd.DataFrame:
    safety_weeks = _number(safety_weeks, "Safety-stock weeks")
    inventory = list_inventory(db_path)
    if inventory.empty:
        return inventory

    inventory["lead_time_demand"] = (
        inventory["avg_weekly_demand"] * inventory["lead_time_days"] / 7
    ).map(math.ceil)
    inventory["safety_stock"] = (
        inventory["avg_weekly_demand"] * safety_weeks
    ).map(math.ceil)
    policy_reorder_point = inventory["lead_time_demand"] + inventory["safety_stock"]
    inventory["reorder_point"] = inventory[
        ["minimum_stock"]
    ].join(policy_reorder_point.rename("policy_reorder_point")).max(axis=1)
    inventory["days_of_cover"] = (
        inventory["available_qty"] / inventory["avg_weekly_demand"] * 7
    ).where(inventory["avg_weekly_demand"] > 0)

    def status(row: pd.Series) -> str:
        if row["avg_weekly_demand"] == 0:
            return "No demand"
        if row["available_qty"] <= row["lead_time_demand"]:
            return "Critical"
        if row["available_qty"] <= row["reorder_point"]:
            return "Reorder"
        if row["on_hand"] > row["maximum_stock"]:
            return "Overstock"
        return "Healthy"

    inventory["status"] = inventory.apply(status, axis=1)
    projected = inventory["available_qty"] + inventory["incoming_qty"]
    inventory["target_stock"] = inventory[
        ["maximum_stock", "reorder_point"]
    ].max(axis=1)
    inventory["recommended_order_qty"] = (
        inventory["target_stock"] - projected
    ).clip(lower=0).where(
        inventory["status"].isin(["Critical", "Reorder"]), 0
    ).map(math.ceil)
    inventory["annual_carrying_cost"] = (
        inventory["inventory_value"] * inventory["carrying_cost_pct"] / 100
    )
    return inventory


def _inventory_values(
    product_id: str,
    product_name: str,
    category: str,
    supplier_id: str,
    on_hand: float,
    avg_weekly_demand: float,
    lead_time_days: float,
    unit_cost: float,
    minimum_stock: float | None,
    maximum_stock: float | None,
    carrying_cost_pct: float,
) -> tuple:
    product_name = _required_text(product_name, "Product name")
    category = _required_text(category, "Category")
    supplier_id = _required_text(supplier_id, "Supplier")
    on_hand = _number(on_hand, "On-hand stock")
    demand = _number(avg_weekly_demand, "Average weekly demand")
    lead_time = _number(lead_time_days, "Lead time")
    unit_cost = _number(unit_cost, "Unit cost")
    min_stock = (
        math.ceil(demand * lead_time / 7)
        if minimum_stock is None
        else _number(minimum_stock, "Minimum stock")
    )
    max_stock = (
        math.ceil(demand * (lead_time / 7 + 4))
        if maximum_stock is None
        else _number(maximum_stock, "Maximum stock")
    )
    carrying = _number(carrying_cost_pct, "Carrying cost percentage")
    if max_stock < min_stock:
        raise ValueError("Maximum stock must be at least minimum stock.")
    if carrying > 100:
        raise ValueError("Carrying cost percentage cannot exceed 100%.")
    return (
        product_id, product_name, category, supplier_id, on_hand, demand,
        lead_time, unit_cost, min_stock, max_stock, carrying,
    )


def create_inventory_item(
    product_id: str,
    product_name: str,
    category: str,
    supplier_id: str,
    on_hand: float,
    avg_weekly_demand: float,
    lead_time_days: float,
    unit_cost: float,
    minimum_stock: float | None = None,
    maximum_stock: float | None = None,
    carrying_cost_pct: float = 22,
    db_path: str | Path = DB_PATH,
) -> None:
    product_id = _required_text(product_id, "Product ID")
    values = _inventory_values(
        product_id, product_name, category, supplier_id, on_hand,
        avg_weekly_demand, lead_time_days, unit_cost, minimum_stock,
        maximum_stock, carrying_cost_pct,
    )
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            _require_record(connection, "suppliers", "supplier_id", supplier_id)
            connection.execute(
                """
                INSERT INTO inventory (
                    product_id, product_name, category, supplier_id, on_hand,
                    avg_weekly_demand, lead_time_days, unit_cost, minimum_stock,
                    maximum_stock, carrying_cost_pct, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values + (timestamp,),
            )
            record_activity(connection, "inventory", product_id, "Created", product_name)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not create inventory record: {error}.") from error


def update_inventory_item(
    product_id: str,
    product_name: str,
    category: str,
    supplier_id: str,
    on_hand: float,
    avg_weekly_demand: float,
    lead_time_days: float,
    unit_cost: float,
    minimum_stock: float,
    maximum_stock: float,
    carrying_cost_pct: float,
    db_path: str | Path = DB_PATH,
) -> None:
    values = _inventory_values(
        product_id, product_name, category, supplier_id, on_hand,
        avg_weekly_demand, lead_time_days, unit_cost, minimum_stock,
        maximum_stock, carrying_cost_pct,
    )
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            current = _require_record(connection, "inventory", "product_id", product_id)
            allocated = connection.execute(
                """
                SELECT COALESCE(SUM(quantity), 0) FROM resource_allocations
                WHERE product_id = ? AND status = 'Allocated'
                """,
                (product_id,),
            ).fetchone()[0]
            if values[4] < allocated:
                raise ValueError(
                    f"On-hand stock cannot be below the {allocated:g} units already allocated."
                )
            _require_record(connection, "suppliers", "supplier_id", supplier_id)
            connection.execute(
                """
                UPDATE inventory SET
                    product_name = ?, category = ?, supplier_id = ?, on_hand = ?,
                    avg_weekly_demand = ?, lead_time_days = ?, unit_cost = ?,
                    minimum_stock = ?, maximum_stock = ?, carrying_cost_pct = ?,
                    updated_at = ?
                WHERE product_id = ?
                """,
                values[1:11] + (timestamp, product_id),
            )
            delta = values[4] - float(current["on_hand"])
            if delta:
                _insert_inventory_movement(
                    connection,
                    product_id,
                    "Adjustment",
                    delta,
                    values[7],
                    "MANUAL-COUNT",
                    "On-hand quantity updated by an inventory manager.",
                    values[4],
                )
            record_activity(connection, "inventory", product_id, "Updated", product_name)
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not update inventory record: {error}.") from error


def delete_inventory_item(product_id: str, db_path: str | Path = DB_PATH) -> None:
    try:
        with transaction(db_path) as connection:
            row = _require_record(connection, "inventory", "product_id", product_id)
            connection.execute("DELETE FROM inventory WHERE product_id = ?", (product_id,))
            record_activity(
                connection, "inventory", product_id, "Deleted", row["product_name"]
            )
    except sqlite3.IntegrityError as error:
        raise ValueError(
            "This inventory record has operational history or linked records and cannot be deleted."
        ) from error


def import_inventory_csv(
    inventory_frame: pd.DataFrame,
    db_path: str | Path = DB_PATH,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "added": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    missing_columns = [
        column for column in REQUIRED_INVENTORY_COLUMNS
        if column not in inventory_frame.columns
    ]
    if missing_columns:
        result["failed"] = len(inventory_frame)
        result["errors"].append(
            "Missing required columns: " + ", ".join(missing_columns)
        )
        return result
    if inventory_frame.empty:
        result["errors"].append("The inventory file has no product rows.")
        return result

    product_ids = inventory_frame["product_id"].astype("string").str.strip()
    valid_ids = product_ids.notna() & product_ids.ne("")
    duplicate_ids = set(
        product_ids[valid_ids & product_ids.duplicated(keep=False)].tolist()
    )
    rows_to_import: list[tuple[int, dict[str, Any]]] = []
    for row_number, raw_row in enumerate(
        inventory_frame.to_dict(orient="records"), start=2
    ):
        product_id = product_ids.iloc[row_number - 2]
        if pd.notna(product_id) and product_id in duplicate_ids:
            result["failed"] += 1
            result["errors"].append(
                f"Row {row_number}: duplicate product ID '{product_id}'; "
                "all rows with this ID were skipped."
            )
            continue
        try:
            clean_row = validate_inventory(pd.DataFrame([raw_row])).iloc[0]
        except ValueError as error:
            result["failed"] += 1
            result["errors"].append(f"Row {row_number}: {error}")
            continue
        rows_to_import.append((row_number, clean_row.to_dict()))

    timestamp = now_utc()
    with transaction(db_path) as connection:
        connection.execute("BEGIN")
        supplier_ids = {
            row["name"]: row["supplier_id"]
            for row in connection.execute("SELECT supplier_id, name FROM suppliers")
        }
        for row_number, row in rows_to_import:
            connection.execute("SAVEPOINT inventory_import_row")
            try:
                supplier_id = supplier_ids.get(str(row["supplier"]))
                if supplier_id is None:
                    raise ValueError(
                        f"Supplier '{row['supplier']}' is not registered."
                    )
                product_id = str(row["product_id"])
                old = connection.execute(
                    "SELECT * FROM inventory WHERE product_id = ?", (product_id,)
                ).fetchone()
                imported_fields = (
                    str(row["product_name"]),
                    str(row["category"]),
                    supplier_id,
                    float(row["on_hand"]),
                    float(row["avg_weekly_demand"]),
                    float(row["lead_time_days"]),
                    float(row["unit_cost"]),
                )
                if old:
                    existing_fields = (
                        old["product_name"],
                        old["category"],
                        old["supplier_id"],
                        float(old["on_hand"]),
                        float(old["avg_weekly_demand"]),
                        float(old["lead_time_days"]),
                        float(old["unit_cost"]),
                    )
                    if existing_fields == imported_fields:
                        result["skipped"] += 1
                        connection.execute("RELEASE SAVEPOINT inventory_import_row")
                        continue

                    allocated = connection.execute(
                        """
                        SELECT COALESCE(SUM(quantity), 0) FROM resource_allocations
                        WHERE product_id = ? AND status = 'Allocated'
                        """,
                        (product_id,),
                    ).fetchone()[0]
                    if imported_fields[3] < allocated:
                        raise ValueError(
                            "on-hand stock is below the existing resource allocation."
                        )
                    connection.execute(
                        """
                        UPDATE inventory SET product_name=?, category=?, supplier_id=?,
                            on_hand=?, avg_weekly_demand=?, lead_time_days=?, unit_cost=?,
                            updated_at=?
                        WHERE product_id=?
                        """,
                        imported_fields + (timestamp, product_id),
                    )
                    delta = imported_fields[3] - float(old["on_hand"])
                    if delta:
                        _insert_inventory_movement(
                            connection,
                            product_id,
                            "Adjustment",
                            delta,
                            imported_fields[6],
                            "CSV-IMPORT",
                            "On-hand quantity updated by an authorized inventory CSV import.",
                            imported_fields[3],
                        )
                    result["updated"] += 1
                else:
                    minimum = math.ceil(imported_fields[4] * imported_fields[5] / 7)
                    maximum = max(
                        minimum,
                        math.ceil(imported_fields[4] * (imported_fields[5] / 7 + 4)),
                    )
                    connection.execute(
                        """
                        INSERT INTO inventory (
                            product_id, product_name, category, supplier_id, on_hand,
                            avg_weekly_demand, lead_time_days, unit_cost, minimum_stock,
                            maximum_stock, carrying_cost_pct, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            product_id,
                            *imported_fields,
                            minimum,
                            maximum,
                            22.0,
                            timestamp,
                        ),
                    )
                    result["added"] += 1
                connection.execute("RELEASE SAVEPOINT inventory_import_row")
            except (ValueError, sqlite3.IntegrityError) as error:
                connection.execute("ROLLBACK TO SAVEPOINT inventory_import_row")
                connection.execute("RELEASE SAVEPOINT inventory_import_row")
                result["failed"] += 1
                result["errors"].append(f"Row {row_number}: {error}")

        if result["added"] or result["updated"]:
            record_activity(
                connection,
                "inventory",
                "csv-import",
                "Imported",
                (
                    f"Added {result['added']}, updated {result['updated']}, "
                    f"skipped {result['skipped']}, failed {result['failed']} records."
                ),
            )
    return result


def list_requisitions(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    return _frame(
        """
        SELECT r.*, i.product_name, i.category, i.unit_cost,
               r.quantity * i.unit_cost AS estimated_value
        FROM purchase_requests r JOIN inventory i ON i.product_id = r.product_id
        ORDER BY CASE r.status WHEN 'Pending' THEN 0 WHEN 'Approved' THEN 1 ELSE 2 END,
                 r.created_at DESC
        """,
        db_path=db_path,
    )


def create_requisition(
    product_id: str,
    quantity: int,
    requester: str,
    department: str,
    justification: str,
    needed_by: str | date,
    db_path: str | Path = DB_PATH,
) -> str:
    quantity = _quantity(quantity)
    requester = _required_text(requester, "Requester")
    department = _required_text(department, "Department")
    justification = _required_text(justification, "Business justification")
    needed_by = _date_text(needed_by, "Need-by date")
    request_id = _new_id("REQ")
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            _require_record(connection, "inventory", "product_id", product_id)
            connection.execute(
                """
                INSERT INTO purchase_requests (
                    request_id, product_id, quantity, requester, department,
                    justification, needed_by, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)
                """,
                (
                    request_id, product_id, quantity, requester, department,
                    justification, needed_by, timestamp, timestamp,
                ),
            )
            record_activity(
                connection, "requisition", request_id, "Created",
                f"{quantity} units requested for {product_id}.",
            )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not create requisition: {error}.") from error
    return request_id


def update_requisition(
    request_id: str,
    product_id: str,
    quantity: int,
    requester: str,
    department: str,
    justification: str,
    needed_by: str | date,
    db_path: str | Path = DB_PATH,
) -> None:
    quantity = _quantity(quantity)
    requester = _required_text(requester, "Requester")
    department = _required_text(department, "Department")
    justification = _required_text(justification, "Business justification")
    needed_by = _date_text(needed_by, "Need-by date")
    with transaction(db_path) as connection:
        row = _require_record(connection, "purchase_requests", "request_id", request_id)
        if row["status"] != "Pending":
            raise ValueError("Only pending requisitions can be edited.")
        _require_record(connection, "inventory", "product_id", product_id)
        connection.execute(
            """
            UPDATE purchase_requests SET product_id=?, quantity=?, requester=?,
                department=?, justification=?, needed_by=?, updated_at=?
            WHERE request_id=?
            """,
            (
                product_id, quantity, requester, department, justification,
                needed_by, now_utc(), request_id,
            ),
        )
        record_activity(connection, "requisition", request_id, "Updated", justification)


def review_requisition(
    request_id: str,
    decision: str,
    reviewed_by: str,
    review_notes: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    decision = _enum(decision, ("Approved", "Rejected"), "Decision")
    reviewed_by = _required_text(reviewed_by, "Reviewer")
    with transaction(db_path) as connection:
        row = _require_record(connection, "purchase_requests", "request_id", request_id)
        if row["status"] != "Pending":
            raise ValueError("Only pending requisitions can be approved or rejected.")
        connection.execute(
            """
            UPDATE purchase_requests SET status=?, reviewed_by=?, review_notes=?, updated_at=?
            WHERE request_id=?
            """,
            (decision, reviewed_by, review_notes.strip(), now_utc(), request_id),
        )
        record_activity(
            connection, "requisition", request_id, decision,
            review_notes.strip() or f"Reviewed by {reviewed_by}.",
        )


def delete_requisition(request_id: str, db_path: str | Path = DB_PATH) -> None:
    try:
        with transaction(db_path) as connection:
            row = _require_record(connection, "purchase_requests", "request_id", request_id)
            if row["status"] not in ("Pending", "Rejected"):
                raise ValueError("Only pending or rejected requisitions can be deleted.")
            if connection.execute(
                "SELECT 1 FROM purchase_orders WHERE request_id = ?", (request_id,)
            ).fetchone():
                raise ValueError("A requisition linked to a purchase order cannot be deleted.")
            connection.execute(
                "DELETE FROM purchase_requests WHERE request_id = ?", (request_id,)
            )
            record_activity(connection, "requisition", request_id, "Deleted", row["status"])
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not delete requisition: {error}.") from error


def list_purchase_orders(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    return _frame(
        """
        SELECT po.*, s.name AS supplier, i.product_name,
               po.quantity * po.unit_cost AS planned_value,
               COALESCE(po.actual_quantity, 0) * COALESCE(po.actual_unit_cost, po.unit_cost) AS actual_value
        FROM purchase_orders po
        JOIN suppliers s ON s.supplier_id = po.supplier_id
        JOIN inventory i ON i.product_id = po.product_id
        ORDER BY CASE po.status WHEN 'Pending' THEN 0 WHEN 'Approved' THEN 1 ELSE 2 END,
                 po.created_at DESC
        """,
        db_path=db_path,
    )


def _order_period_totals(
    connection: sqlite3.Connection, period: str
) -> tuple[float, float]:
    actual = connection.execute(
        """
        SELECT COALESCE(SUM(po.actual_quantity * po.actual_unit_cost), 0)
        FROM purchase_orders po
        JOIN shipments sh ON sh.purchase_order_id = po.po_id
        WHERE po.status = 'Completed' AND substr(sh.actual_date, 1, 7) = ?
        """,
        (period,),
    ).fetchone()[0]
    approved_commitments = connection.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_cost), 0)
        FROM purchase_orders
        WHERE status = 'Approved'
        """
    ).fetchone()[0]
    return float(actual), float(approved_commitments)


def get_budget(period: str | None = None, db_path: str | Path = DB_PATH) -> float:
    period = period or date.today().strftime("%Y-%m")
    _date_text(period + "-01", "Budget period")
    row = _row("SELECT amount FROM budgets WHERE period = ?", (period,), db_path)
    return float(row["amount"]) if row else 0.0


def set_budget(
    period: str,
    amount: float,
    db_path: str | Path = DB_PATH,
) -> None:
    period = _required_text(period, "Budget period")
    if len(period) != 7:
        raise ValueError("Budget period must use YYYY-MM format.")
    _date_text(period + "-01", "Budget period")
    amount = _number(amount, "Procurement budget")
    with transaction(db_path) as connection:
        connection.execute(
            """
            INSERT INTO budgets (period, amount, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(period) DO UPDATE SET amount=excluded.amount, updated_at=excluded.updated_at
            """,
            (period, amount, now_utc()),
        )
        record_activity(
            connection, "budget", period, "Updated", f"Budget set to {amount:,.2f}."
        )


def procurement_summary(
    period: str | None = None,
    db_path: str | Path = DB_PATH,
) -> dict[str, float | int | str | bool | None]:
    period = period or date.today().strftime("%Y-%m")
    _date_text(period + "-01", "Budget period")
    budget_row = _row("SELECT amount FROM budgets WHERE period = ?", (period,), db_path)
    budget_exists = budget_row is not None
    budget = float(budget_row["amount"]) if budget_row else 0.0
    connection = sqlite3.connect(db_path)
    try:
        actual, commitments = _order_period_totals(connection, period)
        open_pos = connection.execute(
            "SELECT COUNT(*) FROM purchase_orders WHERE status IN ('Pending', 'Approved')"
        ).fetchone()[0]
        pending_requests = connection.execute(
            "SELECT COUNT(*) FROM purchase_requests WHERE status = 'Pending'"
        ).fetchone()[0]
        pending_pos = connection.execute(
            "SELECT COUNT(*) FROM purchase_orders WHERE status = 'Pending'"
        ).fetchone()[0]
    finally:
        connection.close()
    planned = actual + commitments
    return {
        "period": period,
        "budget": budget,
        "budget_exists": budget_exists,
        "actual_expenditure": actual,
        "approved_commitments": commitments,
        "planned_expenditure": planned,
        "available_budget": budget - planned if budget_exists else None,
        "budget_utilization_pct": planned / budget * 100 if budget > 0 else None,
        "open_purchase_orders": int(open_pos),
        "pending_requisitions": int(pending_requests),
        "pending_purchase_orders": int(pending_pos),
        "pending_approvals": int(pending_requests + pending_pos),
    }


def dashboard_kpis(
    period: str | None = None,
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> dict[str, float | int | bool | None]:
    today = as_of or date.today()
    procurement = procurement_summary(period or today.strftime("%Y-%m"), db_path)
    inventory = inventory_control(db_path)
    suppliers = list_suppliers(db_path, today)
    shipments = list_shipments(db_path, today)
    issues = list_issues(period or today.strftime("%Y-%m"), db_path, today)
    turnover = inventory_turnover(db_path, today)
    delivered_count = int(suppliers["delivered_count"].sum()) if not suppliers.empty else 0
    on_time_count = int(suppliers["on_time_count"].fillna(0).sum()) if not suppliers.empty else 0
    return {
        "actual_expenditure": float(procurement["actual_expenditure"]),
        "budget": float(procurement["budget"]),
        "budget_exists": bool(procurement["budget_exists"]),
        "available_budget": procurement["available_budget"],
        "budget_utilization_pct": (
            float(procurement["budget_utilization_pct"])
            if procurement["budget_utilization_pct"] is not None
            else None
        ),
        "planned_expenditure": float(procurement["planned_expenditure"]),
        "inventory_value": float(inventory["inventory_value"].sum()) if not inventory.empty else 0.0,
        "inventory_turnover": float(turnover["turnover"]),
        "supplier_on_time_pct": (
            on_time_count / delivered_count * 100 if delivered_count else None
        ),
        "open_purchase_orders": int(procurement["open_purchase_orders"]),
        "delayed_shipments": (
            int(shipments["is_delayed"].sum()) if not shipments.empty else 0
        ),
        "stockout_risk": int((inventory["status"] == "Critical").sum()) if not inventory.empty else 0,
        "pending_approvals": int(procurement["pending_approvals"]),
        "high_priority_issues": (
            int(
                (
                    (issues["priority"] == "High")
                    & (issues["resolution_status"] != "Resolved")
                ).sum()
            )
            if not issues.empty
            else 0
        ),
        "delivered_shipments": delivered_count,
    }


def create_purchase_order(
    supplier_id: str,
    product_id: str,
    quantity: int,
    unit_cost: float,
    expected_date: str | date,
    department: str,
    notes: str = "",
    request_id: str | None = None,
    db_path: str | Path = DB_PATH,
) -> str:
    quantity = _quantity(quantity)
    unit_cost = _number(unit_cost, "Unit cost")
    expected_date = _date_text(expected_date, "Expected delivery date")
    department = _required_text(department, "Department")
    po_id = _new_id("PO")
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            _require_record(connection, "suppliers", "supplier_id", supplier_id)
            _require_record(connection, "inventory", "product_id", product_id)
            if request_id:
                request = _require_record(
                    connection, "purchase_requests", "request_id", request_id
                )
                if request["status"] != "Approved":
                    raise ValueError("Only an approved requisition can be converted to an order.")
                if request["product_id"] != product_id:
                    raise ValueError("The order product must match the approved requisition.")
                if quantity > int(request["quantity"]):
                    raise ValueError("Order quantity cannot exceed the approved requisition.")
            connection.execute(
                """
                INSERT INTO purchase_orders (
                    po_id, request_id, supplier_id, product_id, quantity, unit_cost,
                    expected_date, status, department, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?, ?)
                """,
                (
                    po_id, request_id, supplier_id, product_id, quantity, unit_cost,
                    expected_date, department, notes.strip(), timestamp, timestamp,
                ),
            )
            if request_id:
                connection.execute(
                    "UPDATE purchase_requests SET status='Converted', updated_at=? WHERE request_id=?",
                    (timestamp, request_id),
                )
                record_activity(
                    connection, "requisition", request_id, "Converted",
                    f"Created purchase order {po_id}.",
                )
            record_activity(
                connection, "purchase order", po_id, "Created",
                f"{quantity} units of {product_id}; pending approval.",
            )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not create purchase order: {error}.") from error
    return po_id


def create_order_from_requisition(
    request_id: str,
    supplier_id: str,
    unit_cost: float,
    expected_date: str | date,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> str:
    request = _row(
        "SELECT * FROM purchase_requests WHERE request_id = ?", (request_id,), db_path
    )
    if request is None:
        raise ValueError(f"Requisition '{request_id}' was not found.")
    return create_purchase_order(
        supplier_id=supplier_id,
        product_id=request["product_id"],
        quantity=int(request["quantity"]),
        unit_cost=unit_cost,
        expected_date=expected_date,
        department=request["department"],
        notes=notes,
        request_id=request_id,
        db_path=db_path,
    )


def update_purchase_order(
    po_id: str,
    supplier_id: str,
    product_id: str,
    quantity: int,
    unit_cost: float,
    expected_date: str | date,
    department: str,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    quantity = _quantity(quantity)
    unit_cost = _number(unit_cost, "Unit cost")
    expected_date = _date_text(expected_date, "Expected delivery date")
    department = _required_text(department, "Department")
    with transaction(db_path) as connection:
        row = _require_record(connection, "purchase_orders", "po_id", po_id)
        if row["status"] != "Pending":
            raise ValueError("Only purchase orders pending approval can be edited.")
        _require_record(connection, "suppliers", "supplier_id", supplier_id)
        _require_record(connection, "inventory", "product_id", product_id)
        if row["request_id"]:
            request = _require_record(
                connection, "purchase_requests", "request_id", row["request_id"]
            )
            if request["product_id"] != product_id or quantity > int(request["quantity"]):
                raise ValueError(
                    "A requisition-linked order must keep the approved product and quantity limit."
                )
        connection.execute(
            """
            UPDATE purchase_orders SET supplier_id=?, product_id=?, quantity=?,
                unit_cost=?, expected_date=?, department=?, notes=?, updated_at=?
            WHERE po_id=?
            """,
            (
                supplier_id, product_id, quantity, unit_cost, expected_date,
                department, notes.strip(), now_utc(), po_id,
            ),
        )
        record_activity(connection, "purchase order", po_id, "Updated", notes.strip())


def review_purchase_order(
    po_id: str,
    decision: str,
    reviewed_by: str,
    override_reason: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    decision = _enum(decision, ("Approved", "Rejected"), "Decision")
    reviewed_by = _required_text(reviewed_by, "Reviewer")
    with transaction(db_path) as connection:
        order = _require_record(connection, "purchase_orders", "po_id", po_id)
        if order["status"] != "Pending":
            raise ValueError("Only purchase orders pending approval can be reviewed.")
        notes = str(order["notes"] or "")
        if decision == "Approved":
            period = str(order["created_at"])[:7]
            budget_row = connection.execute(
                "SELECT amount FROM budgets WHERE period = ?", (period,)
            ).fetchone()
            if budget_row is None:
                raise ValueError(
                    f"No procurement budget is allocated for {period}. "
                    "Set a monthly budget before approving this purchase order."
                )
            budget = float(budget_row["amount"])
            actual, commitments = _order_period_totals(connection, period)
            proposed = actual + commitments + int(order["quantity"]) * float(order["unit_cost"])
            if proposed > budget and not override_reason.strip():
                raise ValueError(
                    "Approval would exceed the procurement budget. Record an override reason to continue."
                )
            if override_reason.strip():
                notes = f"{notes}\nBudget approval note ({reviewed_by}): {override_reason.strip()}".strip()
        connection.execute(
            """
            UPDATE purchase_orders SET status=?, notes=?, updated_at=?
            WHERE po_id=?
            """,
            (decision, notes, now_utc(), po_id),
        )
        record_activity(
            connection, "purchase order", po_id, decision,
            override_reason.strip() or f"Reviewed by {reviewed_by}.",
        )


def delete_purchase_order(po_id: str, db_path: str | Path = DB_PATH) -> None:
    try:
        with transaction(db_path) as connection:
            row = _require_record(connection, "purchase_orders", "po_id", po_id)
            if row["status"] not in ("Pending", "Rejected"):
                raise ValueError("Only pending or rejected purchase orders can be deleted.")
            if connection.execute(
                "SELECT 1 FROM shipments WHERE purchase_order_id = ?", (po_id,)
            ).fetchone():
                raise ValueError("An order with a shipment record cannot be deleted.")
            connection.execute("DELETE FROM purchase_orders WHERE po_id = ?", (po_id,))
            record_activity(connection, "purchase order", po_id, "Deleted", row["status"])
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not delete purchase order: {error}.") from error


def create_shipment(
    po_id: str,
    destination: str,
    expected_date: str | date,
    transport_cost: float,
    carrier: str = "",
    db_path: str | Path = DB_PATH,
) -> str:
    destination = _required_text(destination, "Destination")
    expected_date = _date_text(expected_date, "Expected delivery date")
    transport_cost = _number(transport_cost, "Transportation cost")
    shipment_id = _new_id("INB")
    timestamp = now_utc()
    try:
        with transaction(db_path) as connection:
            order = _require_record(connection, "purchase_orders", "po_id", po_id)
            if order["status"] != "Approved":
                raise ValueError("A shipment can only be scheduled for an approved purchase order.")
            if connection.execute(
                "SELECT 1 FROM shipments WHERE purchase_order_id = ?", (po_id,)
            ).fetchone():
                raise ValueError("This purchase order already has a shipment record.")
            connection.execute(
                """
                INSERT INTO shipments (
                    shipment_id, purchase_order_id, supplier_id, product_id, destination,
                    expected_date, status, units, transport_cost, carrier, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'Scheduled', ?, ?, ?, ?, ?)
                """,
                (
                    shipment_id, po_id, order["supplier_id"], order["product_id"],
                    destination, expected_date, order["quantity"], transport_cost,
                    carrier.strip(), timestamp, timestamp,
                ),
            )
            record_activity(
                connection, "shipment", shipment_id, "Scheduled",
                f"{order['quantity']} units against {po_id}; expected {expected_date}.",
            )
    except sqlite3.IntegrityError as error:
        raise ValueError(f"Could not schedule shipment: {error}.") from error
    return shipment_id


def list_shipments(
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> pd.DataFrame:
    shipments = _frame(
        """
        SELECT sh.*, s.name AS supplier, i.product_name, po.quantity AS ordered_quantity,
               po.status AS order_status
        FROM shipments sh
        JOIN suppliers s ON s.supplier_id = sh.supplier_id
        LEFT JOIN inventory i ON i.product_id = sh.product_id
        LEFT JOIN purchase_orders po ON po.po_id = sh.purchase_order_id
        ORDER BY sh.expected_date
        """,
        db_path=db_path,
    )
    if shipments.empty:
        return shipments
    today = as_of or date.today()

    def delay_days(row: pd.Series) -> int:
        expected = date.fromisoformat(row["expected_date"])
        if pd.notna(row["actual_date"]) and row["actual_date"]:
            actual = date.fromisoformat(row["actual_date"])
            return max(0, (actual - expected).days)
        if row["status"] not in ("Delivered", "Cancelled"):
            return max(0, (today - expected).days)
        return 0

    shipments["days_late"] = shipments.apply(delay_days, axis=1)
    shipments["is_late_delivery"] = (
        shipments["actual_date"].notna() & (shipments["days_late"] > 0)
    )
    shipments["is_delayed"] = (
        (shipments["status"] == "Delayed")
        | (
            shipments["actual_date"].isna()
            & (shipments["status"] != "Cancelled")
            & (shipments["days_late"] > 0)
        )
    )
    shipments["operational_status"] = shipments["status"]
    shipments.loc[
        shipments["is_delayed"] & (shipments["status"] != "Delivered"),
        "operational_status",
    ] = "Delayed"
    return shipments


def update_shipment_details(
    shipment_id: str,
    destination: str,
    expected_date: str | date,
    transport_cost: float,
    carrier: str,
    delay_reason: str = "",
    corrective_action: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    destination = _required_text(destination, "Destination")
    expected_date = _date_text(expected_date, "Expected delivery date")
    transport_cost = _number(transport_cost, "Transportation cost")
    with transaction(db_path) as connection:
        row = _require_record(connection, "shipments", "shipment_id", shipment_id)
        if row["status"] in ("Delivered", "Cancelled"):
            raise ValueError("Delivered or cancelled shipment details are locked.")
        connection.execute(
            """
            UPDATE shipments SET destination=?, expected_date=?, transport_cost=?,
                carrier=?, delay_reason=?, corrective_action=?, updated_at=?
            WHERE shipment_id=?
            """,
            (
                destination, expected_date, transport_cost, carrier.strip(),
                delay_reason.strip(), corrective_action.strip(), now_utc(), shipment_id,
            ),
        )
        record_activity(
            connection, "shipment", shipment_id, "Updated",
            corrective_action.strip() or delay_reason.strip(),
        )


def update_shipment_status(
    shipment_id: str,
    new_status: str,
    actual_date: str | date | None = None,
    db_path: str | Path = DB_PATH,
) -> None:
    new_status = _enum(new_status, SHIPMENT_STATUSES, "Shipment status")
    with transaction(db_path) as connection:
        shipment = _require_record(connection, "shipments", "shipment_id", shipment_id)
        current = shipment["status"]
        allowed = {
            "Scheduled": {"Preparing", "In transit", "Cancelled"},
            "Preparing": {"In transit", "Delayed", "Cancelled"},
            "In transit": {"Delayed", "Delivered", "Cancelled"},
            "Delayed": {"In transit", "Delivered", "Cancelled"},
            "Delivered": set(),
            "Cancelled": set(),
        }
        if new_status not in allowed[current]:
            raise ValueError(f"Shipment cannot move from {current} to {new_status}.")
        actual = None
        if new_status == "Delivered":
            actual = _date_text(actual_date or date.today(), "Actual delivery date")
            if date.fromisoformat(actual) > date.today():
                raise ValueError("Actual delivery date cannot be in the future.")
            if shipment["purchase_order_id"]:
                order = _require_record(
                    connection, "purchase_orders", "po_id", shipment["purchase_order_id"]
                )
                if order["status"] != "Approved":
                    raise ValueError("Only an approved purchase order can be received.")
                if int(shipment["units"]) != int(order["quantity"]):
                    raise ValueError(
                        "This workflow records full-order receipts; shipment units must match the purchase order."
                    )
                current_stock = connection.execute(
                    "SELECT on_hand, unit_cost FROM inventory WHERE product_id = ?",
                    (shipment["product_id"],),
                ).fetchone()
                new_stock = float(current_stock["on_hand"]) + int(shipment["units"])
                connection.execute(
                    "UPDATE inventory SET on_hand=?, updated_at=? WHERE product_id=?",
                    (new_stock, now_utc(), shipment["product_id"]),
                )
                _insert_inventory_movement(
                    connection,
                    shipment["product_id"],
                    "Receipt",
                    int(shipment["units"]),
                    float(order["unit_cost"]),
                    shipment_id,
                    f"Receipt from purchase order {order['po_id']}.",
                    new_stock,
                    date.fromisoformat(actual),
                )
                connection.execute(
                    """
                    UPDATE purchase_orders SET status='Completed', actual_quantity=?,
                        actual_unit_cost=unit_cost, updated_at=? WHERE po_id=?
                    """,
                    (int(shipment["units"]), now_utc(), order["po_id"]),
                )
                record_activity(
                    connection, "purchase order", order["po_id"], "Completed",
                    f"Received {shipment['units']} units on {actual}.",
                )
        connection.execute(
            "UPDATE shipments SET status=?, actual_date=?, updated_at=? WHERE shipment_id=?",
            (new_status, actual, now_utc(), shipment_id),
        )
        record_activity(
            connection, "shipment", shipment_id, new_status,
            f"Status changed from {current} to {new_status}."
            + (f" Actual delivery: {actual}." if actual else ""),
        )


def delete_shipment(shipment_id: str, db_path: str | Path = DB_PATH) -> None:
    with transaction(db_path) as connection:
        row = _require_record(connection, "shipments", "shipment_id", shipment_id)
        if row["status"] not in ("Scheduled", "Cancelled"):
            raise ValueError("Only scheduled or cancelled shipments can be deleted.")
        connection.execute("DELETE FROM shipments WHERE shipment_id = ?", (shipment_id,))
        record_activity(connection, "shipment", shipment_id, "Deleted", row["status"])


def create_allocation(
    product_id: str,
    destination: str,
    quantity: int,
    requested_by: str,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> str:
    quantity = _quantity(quantity)
    destination = _required_text(destination, "Allocation destination")
    requested_by = _required_text(requested_by, "Requested by")
    allocation_id = _new_id("ALLOC")
    timestamp = now_utc()
    with transaction(db_path) as connection:
        _require_record(connection, "inventory", "product_id", product_id)
        connection.execute(
            """
            INSERT INTO resource_allocations (
                allocation_id, product_id, destination, quantity, status,
                requested_by, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'Pending', ?, ?, ?, ?)
            """,
            (
                allocation_id, product_id, destination, quantity,
                requested_by, notes.strip(), timestamp, timestamp,
            ),
        )
        record_activity(
            connection, "allocation", allocation_id, "Requested",
            f"{quantity} units of {product_id} for {destination}.",
        )
    return allocation_id


def update_allocation_status(
    allocation_id: str,
    new_status: str,
    reviewed_by: str,
    db_path: str | Path = DB_PATH,
) -> None:
    new_status = _enum(new_status, ("Allocated", "Released", "Consumed"), "Allocation status")
    reviewed_by = _required_text(reviewed_by, "Reviewer")
    with transaction(db_path) as connection:
        allocation = _require_record(
            connection, "resource_allocations", "allocation_id", allocation_id
        )
        current = allocation["status"]
        allowed = {
            "Pending": {"Allocated", "Released"},
            "Allocated": {"Released", "Consumed"},
            "Released": set(),
            "Consumed": set(),
        }
        if new_status not in allowed[current]:
            raise ValueError(f"Allocation cannot move from {current} to {new_status}.")
        if new_status == "Allocated":
            row = connection.execute(
                """
                SELECT i.on_hand - COALESCE(SUM(
                    CASE WHEN a.status = 'Allocated' THEN a.quantity ELSE 0 END
                ), 0) AS available
                FROM inventory i LEFT JOIN resource_allocations a
                  ON a.product_id = i.product_id AND a.allocation_id != ?
                WHERE i.product_id = ? GROUP BY i.product_id
                """,
                (allocation_id, allocation["product_id"]),
            ).fetchone()
            if row is None or float(row["available"]) < int(allocation["quantity"]):
                raise ValueError("There is not enough unallocated stock for this request.")
        if new_status == "Consumed":
            product = _require_record(
                connection, "inventory", "product_id", allocation["product_id"]
            )
            quantity = int(allocation["quantity"])
            if float(product["on_hand"]) < quantity:
                raise ValueError("On-hand stock is no longer sufficient to fulfill allocation.")
            new_stock = float(product["on_hand"]) - quantity
            connection.execute(
                "UPDATE inventory SET on_hand=?, updated_at=? WHERE product_id=?",
                (new_stock, now_utc(), allocation["product_id"]),
            )
            _insert_inventory_movement(
                connection,
                allocation["product_id"],
                "Issue",
                -quantity,
                float(product["unit_cost"]),
                allocation_id,
                f"Resource allocation consumed by {reviewed_by}.",
                new_stock,
            )
        connection.execute(
            "UPDATE resource_allocations SET status=?, updated_at=? WHERE allocation_id=?",
            (new_status, now_utc(), allocation_id),
        )
        record_activity(
            connection, "allocation", allocation_id, new_status,
            f"Updated by {reviewed_by}.",
        )


def list_allocations(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    return _frame(
        """
        SELECT a.*, i.product_name, i.category
        FROM resource_allocations a JOIN inventory i ON i.product_id = a.product_id
        ORDER BY a.created_at DESC
        """,
        db_path=db_path,
    )


def list_stock_movements(
    db_path: str | Path = DB_PATH, limit: int = 100
) -> pd.DataFrame:
    return _frame(
        """
        SELECT m.*, i.product_name, i.category
        FROM stock_movements m JOIN inventory i ON i.product_id = m.product_id
        ORDER BY m.movement_date DESC, m.movement_id DESC LIMIT ?
        """,
        (limit,),
        db_path,
    )


def inventory_turnover(
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
    period_days: int = 90,
) -> dict[str, float | int | str]:
    if period_days <= 0:
        raise ValueError("Turnover period must be positive.")
    end_date = as_of or date.today()
    start_date = end_date - timedelta(days=period_days)
    inventory = list_inventory(db_path)
    movements = _frame(
        """
        SELECT product_id, movement_type, quantity_change,
               unit_cost_at_movement
        FROM stock_movements
        WHERE movement_date > ? AND movement_date <= ?
        """,
        (start_date.isoformat(), end_date.isoformat()),
        db_path,
    )
    if inventory.empty:
        return {
            "period_days": period_days,
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
            "cost_of_goods_issued": 0.0,
            "average_inventory_value": 0.0,
            "turnover": 0.0,
        }

    if movements.empty:
        movements = pd.DataFrame(
            columns=["product_id", "movement_type", "quantity_change", "unit_cost_at_movement"]
        )
    net_changes = movements.groupby("product_id")["quantity_change"].sum().to_dict()
    inventory["period_net_change"] = inventory["product_id"].map(net_changes).fillna(0)
    start_quantities = inventory["on_hand"] - inventory["period_net_change"]
    if (start_quantities < 0).any():
        raise ValueError(
            "Inventory history implies a negative opening balance; reconcile stock movements."
        )
    start_value = (start_quantities * inventory["unit_cost"]).sum()
    end_value = inventory["inventory_value"].sum()
    average_value = float((start_value + end_value) / 2)
    issue_rows = movements.loc[
        (movements["movement_type"] == "Issue")
        & (movements["quantity_change"] < 0)
    ]
    cogs = float(
        (-issue_rows["quantity_change"] * issue_rows["unit_cost_at_movement"]).sum()
    )
    return {
        "period_days": period_days,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "cost_of_goods_issued": cogs,
        "average_inventory_value": average_value,
        "turnover": cogs / average_value if average_value else 0.0,
    }


def demand_supply_plan(
    horizon_weeks: float = 4,
    safety_weeks: float = 2,
    db_path: str | Path = DB_PATH,
) -> pd.DataFrame:
    horizon = _number(horizon_weeks, "Planning horizon")
    inventory = inventory_control(db_path, safety_weeks)
    if inventory.empty:
        return inventory
    inventory["demand_estimate"] = inventory["avg_weekly_demand"] * horizon
    inventory["projected_available"] = (
        inventory["available_qty"] + inventory["incoming_qty"]
    )
    inventory["supply_shortage"] = (
        inventory["demand_estimate"] - inventory["projected_available"]
    ).clip(lower=0)
    inventory["planned_procurement_qty"] = (
        inventory["incoming_qty"] + inventory["recommended_order_qty"]
    )
    inventory["future_requirement"] = inventory["recommended_order_qty"]
    return inventory


def list_issues(
    period: str | None = None,
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> pd.DataFrame:
    today = as_of or date.today()
    period = period or today.strftime("%Y-%m")
    inventory = inventory_control(db_path)
    suppliers = list_suppliers(db_path, today)
    shipments = list_shipments(db_path, today)
    procurement = procurement_summary(period, db_path)
    issues: list[dict[str, Any]] = []

    for row in inventory.to_dict(orient="records"):
        if row["status"] == "Critical":
            issues.append(
                {
                    "issue_key": f"inventory:{row['product_id']}",
                    "issue": f"{row['product_name']} is below lead-time stock cover",
                    "operational_impact": (
                        f"Only {row['available_qty']:g} units are available against "
                        f"{row['lead_time_demand']:g} units of estimated supplier lead-time demand."
                    ),
                    "priority": "High",
                    "recommendation": (
                        "Confirm incoming supply and expedite replenishment or reallocate stock."
                    ),
                    "responsible": "Inventory Controller",
                    "department": "Inventory",
                }
            )
        elif row["status"] == "Overstock":
            issues.append(
                {
                    "issue_key": f"overstock:{row['product_id']}",
                    "issue": f"{row['product_name']} exceeds its maximum stock target",
                    "operational_impact": (
                        f"{row['on_hand'] - row['maximum_stock']:g} units are above the "
                        "planned maximum, increasing carrying-cost exposure."
                    ),
                    "priority": "Low",
                    "recommendation": (
                        "Review purchasing cadence and redeploy excess stock before the next order."
                    ),
                    "responsible": "Inventory Controller",
                    "department": "Inventory",
                }
            )

    for row in suppliers.to_dict(orient="records"):
        delivery = row["on_time_rate_pct"]
        poor_delivery = pd.notna(delivery) and float(delivery) < 80
        high_risk = row["risk_level"] == "High"
        if poor_delivery or high_risk:
            issues.append(
                {
                    "issue_key": f"supplier:{row['supplier_id']}",
                    "issue": f"{row['name']} requires supplier performance review",
                    "operational_impact": (
                        f"On-time delivery is {delivery:.0f}% across "
                        f"{int(row['delivered_count'])} recorded deliveries."
                        if pd.notna(delivery)
                        else "Supplier risk is marked high; no delivery history is available."
                    ),
                    "priority": "High" if high_risk or (pd.notna(delivery) and delivery < 60) else "Medium",
                    "recommendation": (
                        "Request a recovery plan, verify capacity, and compare qualified alternatives."
                    ),
                    "responsible": "Procurement Manager",
                    "department": "Procurement",
                }
            )

    for row in shipments.loc[shipments["is_delayed"]].to_dict(orient="records"):
        issues.append(
            {
                "issue_key": f"shipment:{row['shipment_id']}",
                "issue": f"{row['shipment_id']} is delayed",
                "operational_impact": (
                    f"{row['units']} units for "
                    f"{row['product_name'] if pd.notna(row['product_name']) else 'an unassigned item'} "
                    f"are {row['days_late']} day(s) past the expected date."
                ),
                "priority": "High" if row["days_late"] >= 3 else "Medium",
                "recommendation": (
                    "Obtain a confirmed ETA, assess stock cover, and notify the receiving location."
                ),
                "responsible": "Logistics Coordinator",
                "department": "Logistics",
            }
        )

    if not procurement["budget_exists"]:
        issues.append(
            {
                "issue_key": f"budget-missing:{period}",
                "issue": f"No procurement budget is allocated for {period}",
                "operational_impact": (
                    "Purchase order approvals cannot be checked against an allocated monthly limit."
                ),
                "priority": "High",
                "recommendation": (
                    "Allocate and record the monthly procurement budget before approving purchase orders."
                ),
                "responsible": "Procurement Manager",
                "department": "Procurement",
            }
        )
    elif procurement["planned_expenditure"] > procurement["budget"]:
        issues.append(
            {
                "issue_key": f"budget:{period}",
                "issue": f"Procurement commitments exceed the {period} budget",
                "operational_impact": (
                    f"Planned expenditure is {procurement['planned_expenditure']:,.2f}, "
                    f"above the allocated {procurement['budget']:,.2f}."
                ),
                "priority": "High",
                "recommendation": (
                    "Review uncommitted demand, phase purchasing, or obtain a documented budget exception."
                ),
                "responsible": "Procurement Manager",
                "department": "Procurement",
            }
        )

    pending_requests = _frame(
        "SELECT request_id, department FROM purchase_requests WHERE status='Pending'",
        db_path=db_path,
    )
    for row in pending_requests.to_dict(orient="records"):
        issues.append(
            {
                "issue_key": f"approval:{row['request_id']}",
                "issue": f"Requisition {row['request_id']} is awaiting review",
                "operational_impact": "Unreviewed demand may delay planned replenishment.",
                "priority": "Medium",
                "recommendation": "Review stock availability, need date, and budget before deciding.",
                "responsible": "Procurement Manager",
                "department": row["department"],
            }
        )
    pending_orders = _frame(
        "SELECT po_id, department FROM purchase_orders WHERE status='Pending'",
        db_path=db_path,
    )
    for row in pending_orders.to_dict(orient="records"):
        issues.append(
            {
                "issue_key": f"approval:{row['po_id']}",
                "issue": f"Purchase order {row['po_id']} is awaiting approval",
                "operational_impact": "Supplier commitment and expected delivery remain unconfirmed.",
                "priority": "Medium",
                "recommendation": "Check supplier terms, expected value, and available budget, then approve or reject.",
                "responsible": "Procurement Manager",
                "department": row["department"],
            }
        )

    if not issues:
        return pd.DataFrame(
            columns=[
                "issue_key", "issue", "operational_impact", "priority",
                "recommendation", "resolution_status", "responsible",
                "department", "due_date", "action_id",
            ]
        )

    action_rows = _frame(
        """
        SELECT * FROM corrective_actions
        WHERE issue_key IS NOT NULL AND resolution_status <> 'Resolved'
        ORDER BY updated_at DESC
        """,
        db_path=db_path,
    )
    actions = (
        {row["issue_key"]: row for row in action_rows.to_dict(orient="records")}
        if not action_rows.empty
        else {}
    )
    for issue in issues:
        action = actions.get(issue["issue_key"], {})
        issue.update({
            "action_id": action.get("action_id"),
            "resolution_status": action.get("resolution_status", "Open"),
            "responsible": action.get("responsible", issue["responsible"]),
            "department": action.get("department", issue["department"]),
            "due_date": action.get("due_date"),
            "corrective_notes": action.get("notes", ""),
        })
    return pd.DataFrame(issues).sort_values(
        ["priority", "issue"], key=lambda column: column.map(
            {"High": 0, "Medium": 1, "Low": 2}
        ) if column.name == "priority" else column
    ).reset_index(drop=True)


def synchronize_corrective_actions(
    period: str | None = None,
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> dict[str, int]:
    today = as_of or date.today()
    issues = list_issues(period, db_path, today)
    result = {"created": 0, "reopened": 0, "updated": 0, "unchanged": 0}
    timestamp = now_utc()

    with transaction(db_path) as connection:
        for issue in issues.to_dict(orient="records"):
            active = connection.execute(
                """
                SELECT * FROM corrective_actions
                WHERE issue_key = ? AND resolution_status <> 'Resolved'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (issue["issue_key"],),
            ).fetchone()
            issue_fields = (
                issue["issue"],
                issue["operational_impact"],
                issue["priority"],
                issue["recommendation"],
            )
            if active is not None:
                stored_fields = (
                    active["issue"],
                    active["operational_impact"],
                    active["priority"],
                    active["recommendation"],
                )
                if stored_fields == issue_fields:
                    result["unchanged"] += 1
                    continue
                connection.execute(
                    """
                    UPDATE corrective_actions
                    SET issue = ?, operational_impact = ?, priority = ?,
                        recommendation = ?, updated_at = ?
                    WHERE action_id = ?
                    """,
                    issue_fields + (timestamp, active["action_id"]),
                )
                record_activity(
                    connection,
                    "corrective action",
                    active["action_id"],
                    "Synchronized",
                    issue["issue"],
                )
                result["updated"] += 1
                continue

            was_resolved = connection.execute(
                """
                SELECT 1 FROM corrective_actions
                WHERE issue_key = ? AND resolution_status = 'Resolved'
                LIMIT 1
                """,
                (issue["issue_key"],),
            ).fetchone() is not None
            action_id = _new_id("ACT")
            connection.execute(
                """
                INSERT INTO corrective_actions (
                    action_id, issue_key, issue, operational_impact, priority,
                    recommendation, resolution_status, responsible, department,
                    due_date, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'Open', ?, ?, ?, '', ?, ?)
                """,
                (
                    action_id,
                    issue["issue_key"],
                    *issue_fields,
                    issue["responsible"],
                    issue["department"],
                    (
                        today
                        + timedelta(days=3 if issue["priority"] == "High" else 7)
                    ).isoformat(),
                    timestamp,
                    timestamp,
                ),
            )
            action_type = "Reopened" if was_resolved else "Created"
            record_activity(
                connection,
                "corrective action",
                action_id,
                action_type,
                issue["issue"],
            )
            result["reopened" if was_resolved else "created"] += 1
    return result


def list_corrective_actions(
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> pd.DataFrame:
    actions = _frame(
        "SELECT * FROM corrective_actions ORDER BY CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, updated_at DESC",
        db_path=db_path,
    )
    if actions.empty:
        return actions

    current_issues = list_issues(db_path=db_path, as_of=as_of)
    if current_issues.empty:
        return actions
    live_issues = current_issues.set_index("issue_key")
    for index, action in actions.iterrows():
        if (
            action["issue_key"] in live_issues.index
            and action["resolution_status"] != "Resolved"
        ):
            for column in (
                "issue",
                "operational_impact",
                "priority",
                "recommendation",
            ):
                actions.at[index, column] = live_issues.at[action["issue_key"], column]
    return actions


def create_corrective_action(
    issue: str,
    operational_impact: str,
    priority: str,
    recommendation: str,
    responsible: str,
    department: str,
    due_date: str | date | None = None,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> str:
    issue = _required_text(issue, "Issue")
    impact = _required_text(operational_impact, "Operational impact")
    priority = _enum(priority, PRIORITIES, "Priority")
    recommendation = _required_text(recommendation, "Recommended action")
    responsible = _required_text(responsible, "Responsible person or role")
    department = _required_text(department, "Department")
    due = _optional_date_text(due_date, "Due date")
    action_id = _new_id("ACT")
    timestamp = now_utc()
    with transaction(db_path) as connection:
        connection.execute(
            """
            INSERT INTO corrective_actions (
                action_id, issue, operational_impact, priority, recommendation,
                resolution_status, responsible, department, due_date, notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id, issue, impact, priority, recommendation,
                responsible, department, due, notes.strip(), timestamp, timestamp,
            ),
        )
        record_activity(connection, "corrective action", action_id, "Created", issue)
    return action_id


def update_corrective_action(
    action_id: str,
    issue: str,
    operational_impact: str,
    priority: str,
    recommendation: str,
    resolution_status: str,
    responsible: str,
    department: str,
    due_date: str | date | None = None,
    notes: str = "",
    db_path: str | Path = DB_PATH,
) -> None:
    issue = _required_text(issue, "Issue")
    impact = _required_text(operational_impact, "Operational impact")
    priority = _enum(priority, PRIORITIES, "Priority")
    recommendation = _required_text(recommendation, "Recommended action")
    resolution_status = _enum(resolution_status, ACTION_STATUSES, "Resolution status")
    responsible = _required_text(responsible, "Responsible person or role")
    department = _required_text(department, "Department")
    due = _optional_date_text(due_date, "Due date")
    with transaction(db_path) as connection:
        _require_record(connection, "corrective_actions", "action_id", action_id)
        connection.execute(
            """
            UPDATE corrective_actions SET issue=?, operational_impact=?, priority=?,
                recommendation=?, resolution_status=?, responsible=?, department=?,
                due_date=?, notes=?, updated_at=? WHERE action_id=?
            """,
            (
                issue, impact, priority, recommendation, resolution_status,
                responsible, department, due, notes.strip(), now_utc(), action_id,
            ),
        )
        record_activity(
            connection, "corrective action", action_id, resolution_status,
            f"{responsible} · {notes.strip()}",
        )


def delete_corrective_action(
    action_id: str,
    db_path: str | Path = DB_PATH,
    as_of: date | None = None,
) -> None:
    action = _row(
        "SELECT * FROM corrective_actions WHERE action_id = ?", (action_id,), db_path
    )
    if action is None:
        raise ValueError(f"Corrective action '{action_id}' was not found.")
    if action["issue_key"]:
        active = list_issues(db_path=db_path, as_of=as_of)
        if not active.empty and action["issue_key"] in set(active["issue_key"]):
            raise ValueError(
                "Resolve the linked operational issue before deleting its corrective action."
            )
    with transaction(db_path) as connection:
        connection.execute("DELETE FROM corrective_actions WHERE action_id = ?", (action_id,))
        record_activity(connection, "corrective action", action_id, "Deleted", action["issue"])


def list_activities(
    db_path: str | Path = DB_PATH, limit: int = 12
) -> pd.DataFrame:
    return _frame(
        "SELECT * FROM activities ORDER BY occurred_at DESC, activity_id DESC LIMIT ?",
        (limit,),
        db_path,
    )
