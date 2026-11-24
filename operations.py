"""Business rules for inventory planning in the supply-chain control tower."""

from __future__ import annotations

import math

import pandas as pd

REQUIRED_INVENTORY_COLUMNS = (
    "product_id",
    "product_name",
    "category",
    "supplier",
    "on_hand",
    "avg_weekly_demand",
    "lead_time_days",
    "unit_cost",
)

NUMERIC_COLUMNS = (
    "on_hand",
    "avg_weekly_demand",
    "lead_time_days",
    "unit_cost",
)


def validate_inventory(inventory: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize an inventory table before calculations."""
    missing_columns = [
        column for column in REQUIRED_INVENTORY_COLUMNS
        if column not in inventory.columns
    ]
    if missing_columns:
        raise ValueError(
            "Missing required inventory columns: " + ", ".join(missing_columns)
        )
    if inventory.empty:
        raise ValueError("The inventory file has no product rows.")

    clean = inventory.loc[:, REQUIRED_INVENTORY_COLUMNS].copy()
    for column in ("product_id", "product_name", "category", "supplier"):
        clean[column] = clean[column].astype("string").str.strip()
        if clean[column].isna().any() or clean[column].eq("").any():
            raise ValueError(f"Every row needs a value in '{column}'.")

    if clean["product_id"].duplicated().any():
        raise ValueError("Product IDs must be unique.")

    for column in NUMERIC_COLUMNS:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
        if clean[column].isna().any():
            raise ValueError(f"'{column}' must contain valid numbers in every row.")
        if not clean[column].map(math.isfinite).all():
            raise ValueError(f"'{column}' must contain finite numbers.")
        if (clean[column] < 0).any():
            raise ValueError(f"'{column}' cannot contain negative values.")

    return clean


def build_replenishment_plan(
    inventory: pd.DataFrame,
    safety_weeks: float = 2,
    target_cover_weeks: float = 4,
) -> pd.DataFrame:
    """Calculate reorder points and suggested purchase quantities."""
    if safety_weeks < 0 or target_cover_weeks < 0:
        raise ValueError("Planning weeks cannot be negative.")
    if target_cover_weeks < safety_weeks:
        raise ValueError("Target cover must be at least the safety-stock period.")

    plan = validate_inventory(inventory)
    lead_demand = (
        plan["avg_weekly_demand"] * plan["lead_time_days"] / 7
    ).map(math.ceil)
    plan["safety_stock"] = (
        plan["avg_weekly_demand"] * safety_weeks
    ).map(math.ceil)
    plan["reorder_point"] = lead_demand + plan["safety_stock"]
    plan["target_stock"] = (
        plan["avg_weekly_demand"]
        * (plan["lead_time_days"] / 7 + target_cover_weeks)
    ).map(math.ceil)
    plan["days_of_cover"] = (
        plan["on_hand"] / plan["avg_weekly_demand"] * 7
    ).where(plan["avg_weekly_demand"] > 0)

    plan["status"] = "Healthy"
    plan.loc[plan["on_hand"] <= plan["reorder_point"], "status"] = "Reorder"
    plan.loc[plan["on_hand"] <= lead_demand, "status"] = "Critical"
    plan.loc[plan["avg_weekly_demand"] == 0, "status"] = "No demand"

    plan["recommended_order_qty"] = (
        plan["target_stock"] - plan["on_hand"]
    ).clip(lower=0).where(plan["status"].isin(["Critical", "Reorder"]), 0).astype(int)
    plan["recommended_order_value"] = (
        plan["recommended_order_qty"] * plan["unit_cost"]
    )
    return plan
