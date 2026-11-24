import unittest

import pandas as pd

from operations import build_replenishment_plan, validate_inventory


class ReplenishmentPlanTests(unittest.TestCase):
    def setUp(self):
        self.inventory = pd.DataFrame(
            [
                {
                    "product_id": "SKU-1",
                    "product_name": "Sample item",
                    "category": "Home",
                    "supplier": "Sample supplier",
                    "on_hand": 10,
                    "avg_weekly_demand": 14,
                    "lead_time_days": 7,
                    "unit_cost": 2.5,
                }
            ]
        )

    def test_reorder_point_includes_lead_demand_and_safety_stock(self):
        plan = build_replenishment_plan(
            self.inventory, safety_weeks=2, target_cover_weeks=4
        )

        self.assertEqual(plan.loc[0, "safety_stock"], 28)
        self.assertEqual(plan.loc[0, "reorder_point"], 42)
        self.assertEqual(plan.loc[0, "status"], "Critical")
        self.assertEqual(plan.loc[0, "recommended_order_qty"], 60)

    def test_healthy_product_does_not_create_purchase_order(self):
        self.inventory.loc[0, "on_hand"] = 200

        plan = build_replenishment_plan(self.inventory)

        self.assertEqual(plan.loc[0, "status"], "Healthy")
        self.assertEqual(plan.loc[0, "recommended_order_qty"], 0)

    def test_zero_demand_product_is_not_flagged_for_reordering(self):
        self.inventory.loc[0, "avg_weekly_demand"] = 0
        self.inventory.loc[0, "on_hand"] = 0

        plan = build_replenishment_plan(self.inventory)

        self.assertEqual(plan.loc[0, "status"], "No demand")
        self.assertEqual(plan.loc[0, "recommended_order_qty"], 0)

    def test_duplicate_product_ids_are_rejected(self):
        duplicate = pd.concat([self.inventory, self.inventory], ignore_index=True)

        with self.assertRaisesRegex(ValueError, "must be unique"):
            validate_inventory(duplicate)

    def test_negative_inventory_values_are_rejected(self):
        self.inventory.loc[0, "on_hand"] = -1

        with self.assertRaisesRegex(ValueError, "cannot contain negative"):
            validate_inventory(self.inventory)

    def test_infinite_inventory_values_are_rejected(self):
        invalid = self.inventory.assign(on_hand=[float("inf")])

        with self.assertRaisesRegex(ValueError, "finite numbers"):
            validate_inventory(invalid)

    def test_target_cover_cannot_be_less_than_safety_stock(self):
        with self.assertRaisesRegex(ValueError, "at least the safety-stock"):
            build_replenishment_plan(
                self.inventory, safety_weeks=3, target_cover_weeks=2
            )


if __name__ == "__main__":
    unittest.main()
