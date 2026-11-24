import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from database import DATA_DIR, SCHEMA, fetch_one, initialize_database
from management import (
    create_allocation,
    create_corrective_action,
    create_inventory_item,
    create_order_from_requisition,
    create_purchase_order,
    create_requisition,
    create_shipment,
    create_supplier,
    dashboard_kpis,
    delete_corrective_action,
    delete_inventory_item,
    delete_purchase_order,
    delete_requisition,
    delete_shipment,
    delete_supplier,
    import_inventory_csv,
    inventory_control,
    inventory_turnover,
    list_activities,
    list_allocations,
    list_corrective_actions,
    list_inventory,
    list_issues,
    list_purchase_orders,
    list_requisitions,
    list_shipments,
    list_suppliers,
    procurement_summary,
    review_purchase_order,
    review_requisition,
    set_budget,
    synchronize_corrective_actions,
    update_allocation_status,
    update_corrective_action,
    update_inventory_item,
    update_purchase_order,
    update_requisition,
    update_shipment_details,
    update_shipment_status,
    update_supplier,
)


class ManagementWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary_directory.name) / "operations.db"
        initialize_database(self.db_path, DATA_DIR)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_seed_records_derive_supplier_delivery_from_actual_delivery_events(self):
        suppliers = list_suppliers(self.db_path, date(2026, 11, 21))
        vertex = suppliers.loc[suppliers["supplier_id"] == "SUP-006"].iloc[0]
        self.assertEqual(vertex["delivered_count"], 3)
        self.assertAlmostEqual(vertex["on_time_rate_pct"], 100 / 3)

        shipments = list_shipments(self.db_path, date(2026, 11, 21))
        overdue = shipments.loc[shipments["shipment_id"] == "INB-2404"].iloc[0]
        self.assertTrue(overdue["is_delayed"])
        self.assertEqual(overdue["days_late"], 3)
        self.assertFalse(
            shipments.loc[shipments["shipment_id"] == "HIST-004", "is_delayed"].iloc[0]
        )
        self.assertTrue(
            shipments.loc[shipments["shipment_id"] == "HIST-004", "is_late_delivery"].iloc[0]
        )

    def test_procurement_kpis_reconcile_budget_commitments_and_actual_cost(self):
        summary = procurement_summary("2026-11", self.db_path)

        self.assertEqual(summary["budget"], 50000)
        self.assertAlmostEqual(summary["actual_expenditure"], 1178)
        self.assertAlmostEqual(summary["approved_commitments"], 8912.5)
        self.assertAlmostEqual(summary["planned_expenditure"], 10090.5)
        self.assertAlmostEqual(summary["budget_utilization_pct"], 10090.5 / 50000 * 100)
        self.assertEqual(summary["pending_approvals"], 2)

    def test_incoming_order_is_separate_from_received_inventory(self):
        product = list_inventory(self.db_path).set_index("product_id").loc["SKU-1001"]

        self.assertEqual(product["on_hand"], 90)
        self.assertEqual(product["incoming_qty"], 240)
        self.assertEqual(product["available_qty"], 90)

    def test_requisition_approval_order_receipt_and_stock_movement_are_atomic(self):
        request_id = create_requisition(
            "SKU-1008", 10, "Site Operations", "Operations",
            "Planned maintenance material", date.today() + timedelta(days=14),
            self.db_path,
        )
        review_requisition(request_id, "Approved", "Procurement Manager", db_path=self.db_path)
        po_id = create_order_from_requisition(
            request_id, "SUP-004", 20, date.today() + timedelta(days=10),
            "Test receipt", self.db_path,
        )
        self.assertEqual(
            list_requisitions(self.db_path).set_index("request_id").loc[request_id, "status"],
            "Converted",
        )

        review_purchase_order(po_id, "Approved", "Procurement Manager", db_path=self.db_path)
        shipment_id = create_shipment(
            po_id, "West Hub", date.today() + timedelta(days=7), 45, "Test carrier",
            self.db_path,
        )
        before = list_inventory(self.db_path).set_index("product_id").loc["SKU-1008", "on_hand"]
        update_shipment_status(shipment_id, "In transit", db_path=self.db_path)
        unchanged = list_inventory(self.db_path).set_index("product_id").loc["SKU-1008", "on_hand"]
        self.assertEqual(before, unchanged)

        update_shipment_status(shipment_id, "Delivered", date.today(), self.db_path)
        after = list_inventory(self.db_path).set_index("product_id").loc["SKU-1008", "on_hand"]
        self.assertEqual(after, before + 10)
        self.assertEqual(
            list_purchase_orders(self.db_path).set_index("po_id").loc[po_id, "status"],
            "Completed",
        )
        with self.assertRaisesRegex(ValueError, "cannot move"):
            update_shipment_status(shipment_id, "Delivered", date.today(), self.db_path)
        self.assertEqual(
            fetch_one(
                "SELECT COUNT(*) AS count FROM stock_movements WHERE reference=?",
                (shipment_id,),
                self.db_path,
            )["count"],
            1,
        )

    def test_budget_override_is_required_to_approve_an_overrun(self):
        set_budget("2026-11", 10000, self.db_path)
        po_id = create_purchase_order(
            "SUP-001", "SKU-1001", 500, 100, date.today() + timedelta(days=20),
            "Operations", db_path=self.db_path,
        )

        with self.assertRaisesRegex(ValueError, "override reason"):
            review_purchase_order(po_id, "Approved", "Procurement Manager", db_path=self.db_path)
        review_purchase_order(
            po_id, "Approved", "Procurement Manager",
            "Critical scope requirement approved by budget holder.", self.db_path,
        )
        approved = list_purchase_orders(self.db_path).set_index("po_id").loc[po_id]
        self.assertEqual(approved["status"], "Approved")
        self.assertIn(
            "Critical scope requirement approved by budget holder.",
            approved["notes"],
        )

    def test_prior_month_approved_order_remains_committed_until_current_month_receipt(self):
        today = date.today()
        period = today.strftime("%Y-%m")
        prior_period = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        set_budget(prior_period, 10000, self.db_path)
        set_budget(period, 20000, self.db_path)
        po_id = create_purchase_order(
            "SUP-001", "SKU-1001", 5, 10, today + timedelta(days=5),
            "Operations", db_path=self.db_path,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE purchase_orders SET created_at=? WHERE po_id=?",
                (f"{prior_period}-15T09:00:00+00:00", po_id),
            )
        review_purchase_order(po_id, "Approved", "Procurement Manager", db_path=self.db_path)

        before_receipt = procurement_summary(period, self.db_path)
        self.assertAlmostEqual(
            before_receipt["approved_commitments"], 8912.5 + 50
        )
        self.assertAlmostEqual(
            before_receipt["available_budget"],
            before_receipt["budget"] - before_receipt["planned_expenditure"],
        )

        shipment_id = create_shipment(
            po_id, "Central Warehouse", today + timedelta(days=5), 25,
            "Test Carrier", self.db_path,
        )
        update_shipment_status(shipment_id, "In transit", db_path=self.db_path)
        update_shipment_status(shipment_id, "Delivered", today, self.db_path)
        after_receipt = procurement_summary(period, self.db_path)

        self.assertAlmostEqual(after_receipt["approved_commitments"], 8912.5)
        self.assertAlmostEqual(after_receipt["actual_expenditure"], 1178 + 50)
        self.assertAlmostEqual(
            after_receipt["planned_expenditure"],
            before_receipt["planned_expenditure"],
        )

    def test_purchase_order_approval_requires_a_monthly_budget_record(self):
        period = date.today().strftime("%Y-%m")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DELETE FROM budgets WHERE period=?", (period,))
        po_id = create_purchase_order(
            "SUP-001", "SKU-1001", 10, 10, date.today() + timedelta(days=10),
            "Operations", db_path=self.db_path,
        )

        with self.assertRaisesRegex(ValueError, "No procurement budget is allocated"):
            review_purchase_order(
                po_id,
                "Approved",
                "Procurement Manager",
                "Budget holder exception",
                self.db_path,
            )
        summary = procurement_summary(period, self.db_path)
        self.assertFalse(summary["budget_exists"])
        self.assertIsNone(summary["available_budget"])
        self.assertIsNone(summary["budget_utilization_pct"])
        self.assertEqual(
            list_purchase_orders(self.db_path).set_index("po_id").loc[po_id, "status"],
            "Pending",
        )

    def test_allocation_reserves_and_consumes_only_available_stock(self):
        allocation_id = create_allocation(
            "SKU-1006", "Work Package A", 50, "Operations Team", db_path=self.db_path
        )
        update_allocation_status(
            allocation_id, "Allocated", "Inventory Controller", self.db_path
        )
        product = list_inventory(self.db_path).set_index("product_id").loc["SKU-1006"]
        self.assertEqual(product["allocated_qty"], 50)
        self.assertEqual(product["available_qty"], 95)

        update_allocation_status(
            allocation_id, "Consumed", "Inventory Controller", self.db_path
        )
        product = list_inventory(self.db_path).set_index("product_id").loc["SKU-1006"]
        self.assertEqual(product["on_hand"], 95)
        self.assertEqual(product["allocated_qty"], 0)
        self.assertEqual(
            list_allocations(self.db_path).set_index("allocation_id").loc[allocation_id, "status"],
            "Consumed",
        )

        second = create_allocation(
            "SKU-1006", "Work Package B", 96, "Operations Team", db_path=self.db_path
        )
        with self.assertRaisesRegex(ValueError, "not enough unallocated stock"):
            update_allocation_status(
                second, "Allocated", "Inventory Controller", self.db_path
            )

    def test_inventory_turnover_uses_issue_cost_and_average_inventory_at_cost(self):
        turnover = inventory_turnover(
            self.db_path, date(2026, 11, 21), period_days=90
        )

        self.assertGreater(turnover["cost_of_goods_issued"], 0)
        self.assertGreater(turnover["average_inventory_value"], 0)
        self.assertAlmostEqual(
            turnover["turnover"],
            turnover["cost_of_goods_issued"] / turnover["average_inventory_value"],
        )
        self.assertAlmostEqual(turnover["cost_of_goods_issued"], 48401.6)

    def test_corrective_action_assignment_and_resolution_persist(self):
        action_id = create_corrective_action(
            "Coordinate maintenance stock", "Work could be delayed", "Medium",
            "Confirm substitute stock and communicate with the site.",
            "Inventory Controller", "Inventory", date.today() + timedelta(days=5),
            db_path=self.db_path,
        )
        update_corrective_action(
            action_id, "Coordinate maintenance stock", "Work could be delayed",
            "High", "Confirm substitute stock and communicate with the site.",
            "In progress", "Operations Manager", "Inventory",
            date.today() + timedelta(days=3), "Site team contacted.", self.db_path,
        )
        row = list_corrective_actions(self.db_path).set_index("action_id").loc[action_id]
        self.assertEqual(row["resolution_status"], "In progress")
        self.assertEqual(row["responsible"], "Operations Manager")
        delete_corrective_action(action_id, self.db_path, date(2026, 11, 21))
        self.assertNotIn(action_id, set(list_corrective_actions(self.db_path)["action_id"]))

    def test_active_risk_action_cannot_be_deleted_until_issue_is_resolved(self):
        before = fetch_one(
            "SELECT COUNT(*) AS count FROM corrective_actions", db_path=self.db_path
        )["count"]
        issues = list_issues("2026-11", self.db_path, date(2026, 11, 21))
        action_id = issues.loc[
            issues["issue_key"] == "shipment:INB-2404", "action_id"
        ].iloc[0]
        with self.assertRaisesRegex(ValueError, "Resolve the linked"):
            delete_corrective_action(action_id, self.db_path, date(2026, 11, 21))
        after = fetch_one(
            "SELECT COUNT(*) AS count FROM corrective_actions", db_path=self.db_path
        )["count"]
        self.assertEqual(after, before)

    def test_issue_and_action_list_reads_do_not_write_corrective_actions(self):
        before = fetch_one(
            "SELECT COUNT(*) AS count FROM corrective_actions", db_path=self.db_path
        )["count"]

        for _ in range(3):
            list_issues("2026-11", self.db_path, date(2026, 11, 21))
            list_corrective_actions(self.db_path, date(2026, 11, 21))
            dashboard_kpis("2026-11", self.db_path, date(2026, 11, 21))

        after = fetch_one(
            "SELECT COUNT(*) AS count FROM corrective_actions", db_path=self.db_path
        )["count"]
        self.assertEqual(after, before)

    def test_deleting_inactive_linked_action_is_not_recreated_by_issue_reads(self):
        action = fetch_one(
            "SELECT action_id FROM corrective_actions WHERE issue_key=?",
            ("inventory:SKU-1005",),
            self.db_path,
        )
        self.assertIsNotNone(action)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE inventory SET on_hand=200 WHERE product_id='SKU-1005'"
            )

        delete_corrective_action(
            action["action_id"], self.db_path, date(2026, 11, 21)
        )
        for _ in range(3):
            list_issues("2026-11", self.db_path, date(2026, 11, 21))
        deleted = fetch_one(
            "SELECT 1 FROM corrective_actions WHERE action_id=?",
            (action["action_id"],),
            self.db_path,
        )
        self.assertIsNone(deleted)

    def test_recurrent_issue_creates_new_action_without_losing_resolved_history(self):
        as_of = date(2026, 11, 21)
        action = fetch_one(
            "SELECT * FROM corrective_actions WHERE issue_key=?",
            ("inventory:SKU-1005",),
            self.db_path,
        )
        self.assertIsNotNone(action)
        update_corrective_action(
            action["action_id"],
            action["issue"],
            action["operational_impact"],
            action["priority"],
            action["recommendation"],
            "Resolved",
            action["responsible"],
            action["department"],
            action["due_date"],
            "Resolved after replenishment.",
            self.db_path,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE inventory SET on_hand=200 WHERE product_id='SKU-1005'"
            )
        self.assertNotIn(
            "inventory:SKU-1005",
            set(list_issues("2026-11", self.db_path, as_of)["issue_key"]),
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE inventory SET on_hand=12 WHERE product_id='SKU-1005'"
            )
        recurring = list_issues("2026-11", self.db_path, as_of)
        issue = recurring.loc[recurring["issue_key"] == "inventory:SKU-1005"].iloc[0]
        self.assertEqual(issue["resolution_status"], "Open")
        self.assertTrue(pd.isna(issue["action_id"]))

        synchronize_corrective_actions("2026-11", self.db_path, as_of)
        actions = list_corrective_actions(self.db_path, as_of)
        history = actions.loc[actions["issue_key"] == "inventory:SKU-1005"]
        self.assertEqual(len(history), 2)
        self.assertIn("Resolved", set(history["resolution_status"]))
        reopened = history.loc[history["resolution_status"] == "Open"].iloc[0]
        self.assertNotEqual(reopened["action_id"], action["action_id"])
        self.assertEqual(
            history.loc[
                history["action_id"] == action["action_id"], "notes"
            ].iloc[0],
            "Resolved after replenishment.",
        )

        count_after_sync = len(actions)
        synchronize_corrective_actions("2026-11", self.db_path, as_of)
        self.assertEqual(len(list_corrective_actions(self.db_path, as_of)), count_after_sync)

    def test_issue_descriptions_use_current_stock_in_both_issue_and_action_views(self):
        as_of = date(2026, 11, 21)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE inventory SET on_hand=12 WHERE product_id='SKU-1005'"
            )
        synchronize_corrective_actions("2026-11", self.db_path, as_of)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE inventory SET on_hand=5 WHERE product_id='SKU-1005'"
            )
        issue = list_issues("2026-11", self.db_path, as_of).loc[
            lambda frame: frame["issue_key"] == "inventory:SKU-1005"
        ].iloc[0]
        self.assertIn("Only 5 units are available", issue["operational_impact"])
        action = list_corrective_actions(self.db_path, as_of).loc[
            lambda frame: frame["issue_key"] == "inventory:SKU-1005"
        ].iloc[0]
        self.assertIn("Only 5 units are available", action["operational_impact"])
        self.assertEqual(action["notes"], "Coordinate receiving capacity")

        synchronize_corrective_actions("2026-11", self.db_path, as_of)
        stored = fetch_one(
            """
            SELECT operational_impact, notes FROM corrective_actions
            WHERE issue_key='inventory:SKU-1005' AND resolution_status <> 'Resolved'
            """,
            db_path=self.db_path,
        )
        self.assertIn("Only 5 units are available", stored["operational_impact"])
        self.assertEqual(stored["notes"], "Coordinate receiving capacity")

    def test_supplier_inventory_requisition_order_and_shipment_crud_guards(self):
        supplier_id = create_supplier(
            "Temporary Supplier", "Office goods", 12, 99, 4,
            "Active", date.today() + timedelta(days=200), "Low",
            "Procurement Team", db_path=self.db_path,
        )
        update_supplier(
            supplier_id, "Temporary Supplier Updated", "Office goods", 11,
            99, 4, "Active", date.today() + timedelta(days=200),
            "Low", "Procurement Team", "Updated record", self.db_path,
        )
        self.assertIn(
            supplier_id,
            set(list_suppliers(self.db_path)["supplier_id"]),
        )

        create_inventory_item(
            "SKU-TEMP", "Temporary item", "Office", supplier_id, 10, 2,
            7, 5, 2, 20, 15, self.db_path,
        )
        update_inventory_item(
            "SKU-TEMP", "Temporary item updated", "Office", supplier_id, 8,
            2, 7, 5, 2, 20, 15, self.db_path,
        )
        item = list_inventory(self.db_path).set_index("product_id").loc["SKU-TEMP"]
        self.assertEqual(item["on_hand"], 8)
        self.assertEqual(item["available_qty"], 8)
        with self.assertRaisesRegex(ValueError, "cannot be deleted"):
            delete_inventory_item("SKU-TEMP", self.db_path)

        request_id = create_requisition(
            "SKU-TEMP", 2, "Facilities Team", "Facilities", "Small repair",
            date.today() + timedelta(days=4), self.db_path,
        )
        update_requisition(
            request_id, "SKU-TEMP", 3, "Facilities Team", "Facilities",
            "Revised repair scope", date.today() + timedelta(days=5), self.db_path,
        )
        delete_requisition(request_id, self.db_path)

        po_id = create_purchase_order(
            supplier_id, "SKU-TEMP", 4, 5, date.today() + timedelta(days=8),
            "Facilities", db_path=self.db_path,
        )
        update_purchase_order(
            po_id, supplier_id, "SKU-TEMP", 5, 5, date.today() + timedelta(days=9),
            "Facilities", "Updated pending order", self.db_path,
        )
        delete_purchase_order(po_id, self.db_path)

        shipment_po = create_purchase_order(
            supplier_id, "SKU-TEMP", 5, 5, date.today() + timedelta(days=10),
            "Facilities", db_path=self.db_path,
        )
        review_purchase_order(
            shipment_po, "Approved", "Procurement Manager", db_path=self.db_path
        )
        shipment_id = create_shipment(
            shipment_po, "Central Warehouse", date.today() + timedelta(days=10),
            30, "Test Carrier", self.db_path,
        )
        update_shipment_details(
            shipment_id, "East Hub", date.today() + timedelta(days=11),
            35, "Updated Carrier", "Revised route",
            "Confirmed new receiving slot", self.db_path,
        )
        delete_shipment(shipment_id, self.db_path)

        with self.assertRaisesRegex(ValueError, "linked to inventory"):
            delete_supplier(supplier_id, self.db_path)
        self.assertGreater(len(list_activities(self.db_path)), 0)

    def test_unreferenced_supplier_and_inventory_records_can_be_deleted(self):
        supplier_id = create_supplier(
            "Unused Supplier", "Office goods", 10, 98, 3,
            "Active", date.today() + timedelta(days=180), "Low",
            "Procurement Team", db_path=self.db_path,
        )
        create_inventory_item(
            "SKU-UNUSED", "Unused item", "Office", supplier_id, 0, 0,
            10, 2, 0, 20, 10, self.db_path,
        )
        delete_inventory_item("SKU-UNUSED", self.db_path)
        delete_supplier(supplier_id, self.db_path)
        self.assertNotIn(
            "SKU-UNUSED", set(list_inventory(self.db_path)["product_id"])
        )
        self.assertNotIn(
            supplier_id, set(list_suppliers(self.db_path)["supplier_id"])
        )

    def test_inventory_csv_import_preserves_manual_settings_and_reports_results(self):
        current = list_inventory(self.db_path).set_index("product_id")
        existing = current.loc["SKU-1002"]
        update_inventory_item(
            "SKU-1002",
            existing["product_name"],
            existing["category"],
            existing["supplier_id"],
            existing["on_hand"],
            existing["avg_weekly_demand"],
            existing["lead_time_days"],
            existing["unit_cost"],
            77,
            200,
            31,
            self.db_path,
        )
        current = list_inventory(self.db_path).set_index("product_id")
        unchanged = current.loc["SKU-1003"]
        frame = pd.DataFrame(
            [
                {
                    "product_id": "SKU-1002",
                    "product_name": "Travel Mug - revised",
                    "category": "Drinkware",
                    "supplier": "Northstar Components",
                    "on_hand": 125,
                    "avg_weekly_demand": 30,
                    "lead_time_days": 11,
                    "unit_cost": 7.0,
                },
                {
                    "product_id": "SKU-NEW-IMPORT",
                    "product_name": "Imported spare part",
                    "category": "Maintenance",
                    "supplier": "Northstar Components",
                    "on_hand": 10,
                    "avg_weekly_demand": 2,
                    "lead_time_days": 7,
                    "unit_cost": 3.5,
                },
                {
                    "product_id": "SKU-1003",
                    "product_name": unchanged["product_name"],
                    "category": unchanged["category"],
                    "supplier": unchanged["supplier"],
                    "on_hand": unchanged["on_hand"],
                    "avg_weekly_demand": unchanged["avg_weekly_demand"],
                    "lead_time_days": unchanged["lead_time_days"],
                    "unit_cost": unchanged["unit_cost"],
                },
            ]
        )

        result = import_inventory_csv(frame, self.db_path)
        self.assertEqual(
            {key: result[key] for key in ("added", "updated", "skipped", "failed")},
            {"added": 1, "updated": 1, "skipped": 1, "failed": 0},
        )
        inventory = list_inventory(self.db_path).set_index("product_id")
        updated = inventory.loc["SKU-1002"]
        self.assertEqual(updated["product_name"], "Travel Mug - revised")
        self.assertEqual(updated["minimum_stock"], 77)
        self.assertEqual(updated["maximum_stock"], 200)
        self.assertEqual(updated["carrying_cost_pct"], 31)
        imported = inventory.loc["SKU-NEW-IMPORT"]
        self.assertEqual(imported["minimum_stock"], 2)
        self.assertEqual(imported["maximum_stock"], 10)
        self.assertEqual(imported["carrying_cost_pct"], 22)

    def test_inventory_csv_import_rejects_duplicate_ids_and_reports_partial_results(self):
        duplicate = {
            "product_id": "SKU-DUPLICATE",
            "product_name": "Duplicate row",
            "category": "Maintenance",
            "supplier": "Northstar Components",
            "on_hand": 5,
            "avg_weekly_demand": 1,
            "lead_time_days": 7,
            "unit_cost": 2,
        }
        valid = {
            "product_id": "SKU-NEW-VALID",
            "product_name": "Valid imported item",
            "category": "Maintenance",
            "supplier": "Northstar Components",
            "on_hand": 5,
            "avg_weekly_demand": 1,
            "lead_time_days": 7,
            "unit_cost": 2,
        }

        result = import_inventory_csv(
            pd.DataFrame([duplicate, duplicate, valid]), self.db_path
        )
        self.assertEqual(
            {key: result[key] for key in ("added", "updated", "skipped", "failed")},
            {"added": 1, "updated": 0, "skipped": 0, "failed": 2},
        )
        self.assertEqual(len(result["errors"]), 2)
        ids = set(list_inventory(self.db_path)["product_id"])
        self.assertIn("SKU-NEW-VALID", ids)
        self.assertNotIn("SKU-DUPLICATE", ids)

    def test_sample_inventory_has_a_balanced_status_mix(self):
        statuses = inventory_control(self.db_path)["status"].value_counts().to_dict()
        self.assertEqual(
            statuses,
            {"Healthy": 10, "Reorder": 5, "Critical": 4, "Overstock": 1},
        )

    def test_old_corrective_action_unique_constraint_migrates_and_keeps_history(self):
        legacy_path = Path(self.temporary_directory.name) / "legacy.db"
        legacy_schema = SCHEMA.replace("issue_key TEXT,", "issue_key TEXT UNIQUE,")
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.executescript(legacy_schema)
            connection.execute(
                """
                INSERT INTO corrective_actions (
                    action_id, issue_key, issue, operational_impact, priority,
                    recommendation, resolution_status, responsible, department,
                    due_date, notes, created_at, updated_at
                ) VALUES (
                    'ACT-HISTORY', 'inventory:SKU-HISTORY', 'Resolved shortage',
                    'Historic impact', 'High', 'Historic action', 'Resolved',
                    'Inventory Manager', 'Inventory', NULL, 'Closed with evidence',
                    '2026-10-01', '2026-10-02'
                )
                """
            )

        initialize_database(legacy_path, DATA_DIR)
        preserved = fetch_one(
            "SELECT * FROM corrective_actions WHERE action_id='ACT-HISTORY'",
            db_path=legacy_path,
        )
        self.assertEqual(preserved["resolution_status"], "Resolved")
        self.assertEqual(preserved["notes"], "Closed with evidence")
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO corrective_actions (
                    action_id, issue_key, issue, operational_impact, priority,
                    recommendation, resolution_status, responsible, department,
                    due_date, notes, created_at, updated_at
                ) VALUES (
                    'ACT-REOPENED', 'inventory:SKU-HISTORY', 'Recurring shortage',
                    'Current impact', 'High', 'Current action', 'Open',
                    'Inventory Manager', 'Inventory', NULL, '',
                    '2026-11-01', '2026-11-01'
                )
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO corrective_actions (
                        action_id, issue_key, issue, operational_impact, priority,
                        recommendation, resolution_status, responsible, department,
                        due_date, notes, created_at, updated_at
                    ) VALUES (
                        'ACT-DUPLICATE', 'inventory:SKU-HISTORY', 'Duplicate active',
                        'Duplicate impact', 'High', 'Duplicate action', 'Open',
                        'Inventory Manager', 'Inventory', NULL, '',
                        '2026-11-02', '2026-11-02'
                    )
                    """
                )

    def test_rejected_approval_and_initialization_persistence(self):
        request_id = create_requisition(
            "SKU-1008", 3, "Facilities Team", "Facilities", "Small repair",
            date.today() + timedelta(days=10), self.db_path,
        )
        review_requisition(
            request_id, "Rejected", "Procurement Manager", "Not required this month.",
            self.db_path,
        )
        with self.assertRaisesRegex(ValueError, "Only pending requisitions"):
            review_requisition(
                request_id, "Approved", "Procurement Manager", db_path=self.db_path
            )
        delete_requisition(request_id, self.db_path)
        delete_requisition("REQ-2026-103", self.db_path)
        initialize_database(self.db_path, DATA_DIR)
        remaining_ids = set(list_requisitions(self.db_path)["request_id"])
        self.assertNotIn("REQ-2026-103", remaining_ids)
        self.assertNotIn(request_id, remaining_ids)

    def test_sample_dashboard_identifies_overstock_and_management_actions(self):
        statuses = inventory_control(self.db_path).set_index("product_id")["status"]
        self.assertEqual(statuses["SKU-1009"], "Overstock")
        issues = list_issues("2026-11", self.db_path, date(2026, 11, 21))
        self.assertIn("overstock:SKU-1009", set(issues["issue_key"]))
        self.assertIn("shipment:INB-2404", set(issues["issue_key"]))
        kpis = dashboard_kpis("2026-11", self.db_path, date(2026, 11, 21))
        self.assertEqual(kpis["delayed_shipments"], 1)
        self.assertGreater(kpis["high_priority_issues"], 0)


if __name__ == "__main__":
    unittest.main()
