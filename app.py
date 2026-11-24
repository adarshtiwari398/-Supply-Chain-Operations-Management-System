from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from database import DB_PATH, initialize_database
from management import (
    ACTION_STATUSES,
    PO_STATUSES,
    PRIORITIES,
    SHIPMENT_STATUSES,
    create_allocation,
    create_corrective_action,
    create_inventory_item,
    create_order_from_requisition,
    create_purchase_order,
    create_requisition,
    create_shipment,
    create_supplier,
    dashboard_kpis,
    demand_supply_plan,
    delete_corrective_action,
    delete_inventory_item,
    delete_purchase_order,
    delete_requisition,
    delete_shipment,
    delete_supplier,
    get_budget,
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
    list_stock_movements,
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
from operations import build_replenishment_plan

APP_DIR = Path(__file__).resolve().parent
TODAY = date.today()

st.set_page_config(
    page_title="Supply Chain Operations & Management",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

initialize_database()

st.markdown(
    """
    <style>
    .stApp { background: #f5f7f8; }
    [data-testid="stSidebar"] { background: #102a36; }
    [data-testid="stSidebar"] * { color: #eef5f5; }
    h1, h2, h3 { color: #173844; }
    [data-testid="stMetric"] {
        background: #ffffff; padding: 14px; border: 1px solid #e4eaec;
        border-radius: 10px;
    }
    div[data-testid="stMetricValue"] { color: #173844; }
    </style>
    """,
    unsafe_allow_html=True,
)


def _show_table(
    frame: pd.DataFrame,
    columns: list[str] | None = None,
    rename: dict[str, str] | None = None,
    height: int = 360,
) -> None:
    if frame.empty:
        st.info("No records to display.")
        return
    view = frame.copy()
    if columns:
        view = view[[column for column in columns if column in view.columns]]
    if rename:
        view = view.rename(columns=rename)
    st.dataframe(view, hide_index=True, width="stretch", height=height)


def _save_action(action, message: str) -> bool:
    try:
        action()
    except ValueError as error:
        st.error(str(error))
        return False
    st.success(message)
    return True


def _supplier_labels(suppliers: pd.DataFrame) -> dict[str, str]:
    return {
        row["supplier_id"]: row["name"]
        for row in suppliers.to_dict(orient="records")
    }


def _product_labels(inventory: pd.DataFrame) -> dict[str, str]:
    return {
        row["product_id"]: f"{row['product_id']} · {row['product_name']}"
        for row in inventory.to_dict(orient="records")
    }


def _display_money(value: float | int | None) -> str:
    return "Not available" if value is None else f"{value:,.2f}"


def _metric_row(metrics: list[tuple[str, str, str | None]]) -> None:
    row_size = 2 if len(metrics) == 4 else 3
    for start in range(0, len(metrics), row_size):
        row = metrics[start : start + row_size]
        columns = st.columns(len(row))
        for column, (label, value, help_text) in zip(columns, row):
            column.metric(label, value, help=help_text)


st.sidebar.markdown("## 📦 Supply Chain Operations")
st.sidebar.caption("Planning · procurement · coordination · control")
workspace = st.sidebar.radio(
    "Management workspace",
    (
        "Executive dashboard",
        "Procurement",
        "Supplier management",
        "Inventory & resources",
        "Logistics & distribution",
        "Demand & supply planning",
        "Risk & corrective actions",
    ),
    key="workspace",
)
st.sidebar.divider()
st.sidebar.caption("Local records persist in the project database.")
st.sidebar.caption("Illustrative sample data · no live integrations")
st.sidebar.caption("Project owner: Adarsh Tiwari")

st.title("Supply Chain Operations & Management System")
st.caption(
    "Coordinate purchasing, supplier commitments, inventory, delivery execution, "
    "and management follow-up from one operational workspace."
)


if workspace == "Executive dashboard":
    period = TODAY.strftime("%Y-%m")
    metrics = dashboard_kpis(period)
    procurement = procurement_summary(period)
    inventory = inventory_control()
    suppliers = list_suppliers()
    shipments = list_shipments()
    issues = list_issues(period)
    activities = list_activities(limit=10)

    st.subheader(f"Executive operating position · {period}")
    _metric_row(
        [
            ("Actual spend", f"{metrics['actual_expenditure']:,.0f}", "Received orders recorded at actual cost."),
            (
                "Budget used",
                f"{metrics['budget_utilization_pct']:.1f}%"
                if metrics["budget_utilization_pct"] is not None
                else "Unavailable",
                "Actual expenditure plus outstanding commitments divided by an allocated monthly budget.",
            ),
            ("Open POs", f"{metrics['open_purchase_orders']:,}", "Pending approval or approved, not yet received."),
        ]
    )
    _metric_row(
        [
            ("Inventory value", f"{metrics['inventory_value']:,.0f}", "Current on-hand inventory at unit cost; inbound supply is excluded."),
            ("Supplier OTD", f"{metrics['supplier_on_time_pct']:.0f}%" if metrics["supplier_on_time_pct"] is not None else "No history", "On-time delivery from completed shipment records."),
            ("Late inbound", f"{metrics['delayed_shipments']:,}", "Open shipments past expected date or explicitly marked delayed."),
        ]
    )
    _metric_row(
        [
            ("90-day turns", f"{metrics['inventory_turnover']:.2f}×", "Cost of goods issued divided by average inventory value at cost for the last 90 days."),
            ("Stockout risk", f"{metrics['stockout_risk']:,}", "Products at or below estimated supplier lead-time demand."),
        ]
    )
    _metric_row(
        [
            ("Approvals", f"{metrics['pending_approvals']:,}", "Pending requisitions plus purchase orders awaiting review."),
            ("Priority issues", f"{metrics['high_priority_issues']:,}", "Unresolved high-priority items in the operational risk register."),
        ]
    )

    spend_column, supplier_column = st.columns((1.1, 1))
    with spend_column:
        st.subheader("Procurement control")
        budget_display = (
            _display_money(procurement["budget"])
            if procurement["budget_exists"]
            else "Not allocated"
        )
        available_display = (
            _display_money(procurement["available_budget"])
            if procurement["available_budget"] is not None
            else "Not available"
        )
        st.write(
            f"Monthly budget: **{budget_display}** · "
            f"Actual expenditure: **{_display_money(procurement['actual_expenditure'])}**"
        )
        st.write(
            f"Outstanding commitments: **{_display_money(procurement['approved_commitments'])}** · "
            f"Available budget: **{available_display}**"
        )
        st.write(
            f"Planned total: **{_display_money(procurement['planned_expenditure'])}**"
        )
        if not procurement["budget_exists"]:
            st.warning(
                f"No budget is allocated for {period}. Purchase-order approvals are blocked until one is recorded."
            )
        elif procurement["budget"] > 0:
            st.progress(
                min(float(procurement["budget_utilization_pct"] or 0) / 100, 1.0)
            )
        if procurement["budget_exists"] and procurement["planned_expenditure"] > procurement["budget"]:
            st.error("Planned procurement is above the allocated budget.")
        elif procurement["budget_exists"] and procurement["budget"] > 0:
            st.caption("Budget utilization includes actual spend and approved open-order commitments.")

        st.subheader("Inventory position")
        if inventory.empty:
            st.info("No inventory records.")
        else:
            status_order = ["Critical", "Reorder", "Healthy", "Overstock", "No demand"]
            status_counts = (
                inventory["status"].value_counts().reindex(status_order, fill_value=0)
                .rename_axis("Status").reset_index(name="Products")
            )
            status_chart = px.bar(
                status_counts,
                x="Status",
                y="Products",
                color="Status",
                color_discrete_map={
                    "Critical": "#bd4848",
                    "Reorder": "#d39231",
                    "Healthy": "#31816d",
                    "Overstock": "#597b9b",
                    "No demand": "#89949a",
                },
            )
            status_chart.update_layout(
                showlegend=False,
                margin=dict(l=5, r=5, t=10, b=5),
                plot_bgcolor="white",
                paper_bgcolor="white",
            )
            st.plotly_chart(status_chart, width="stretch")

    with supplier_column:
        st.subheader("Supplier performance")
        if suppliers.empty:
            st.info("No suppliers are registered.")
        else:
            _show_table(
                suppliers.head(5),
                [
                    "name", "on_time_rate_pct", "quality_rate_pct",
                    "evaluation_score", "risk_level", "selection_recommendation",
                ],
                {
                    "name": "Supplier",
                    "on_time_rate_pct": "On time (%)",
                    "quality_rate_pct": "Quality (%)",
                    "evaluation_score": "Score / 100",
                    "risk_level": "Risk",
                    "selection_recommendation": "Management recommendation",
                },
                height=255,
            )
        st.subheader("Logistics exceptions")
        delayed_open = shipments.loc[
            shipments["is_delayed"] & shipments["actual_date"].isna()
        ] if not shipments.empty else shipments
        _show_table(
            delayed_open,
            ["shipment_id", "supplier", "destination", "expected_date", "days_late", "operational_status"],
            {
                "shipment_id": "Shipment",
                "supplier": "Supplier",
                "destination": "Destination",
                "expected_date": "Expected",
                "days_late": "Days late",
                "operational_status": "Status",
            },
            height=230,
        )

    st.subheader("Priority management decisions")
    if issues.empty:
        st.success("No current management issues are registered.")
    else:
        priority_issues = issues.loc[
            issues["resolution_status"] != "Resolved"
        ].head(8)
        _show_table(
            priority_issues,
            [
                "priority", "issue", "operational_impact", "recommendation",
                "resolution_status", "responsible", "due_date",
            ],
            {
                "priority": "Priority",
                "issue": "Issue",
                "operational_impact": "Operational impact",
                "recommendation": "Recommended management action",
                "resolution_status": "Resolution",
                "responsible": "Responsible",
                "due_date": "Due date",
            },
            height=310,
        )

    pending_requests = list_requisitions()
    pending_requests = pending_requests.loc[
        pending_requests["status"] == "Pending"
    ].head(5) if not pending_requests.empty else pending_requests
    pending_orders = list_purchase_orders()
    pending_orders = pending_orders.loc[
        pending_orders["status"] == "Pending"
    ].head(5) if not pending_orders.empty else pending_orders
    decision_left, decision_right = st.columns(2)
    with decision_left:
        st.subheader("Requisitions awaiting approval")
        _show_table(
            pending_requests,
            ["request_id", "product_name", "quantity", "department", "needed_by", "estimated_value"],
            {
                "request_id": "Request",
                "product_name": "Product",
                "quantity": "Units",
                "department": "Department",
                "needed_by": "Need by",
                "estimated_value": "Estimated value",
            },
            height=210,
        )
    with decision_right:
        st.subheader("Purchase orders awaiting approval")
        _show_table(
            pending_orders,
            ["po_id", "supplier", "product_name", "quantity", "planned_value", "expected_date"],
            {
                "po_id": "PO",
                "supplier": "Supplier",
                "product_name": "Product",
                "quantity": "Units",
                "planned_value": "Planned value",
                "expected_date": "Expected",
            },
            height=210,
        )

    st.subheader("Recent operational activity")
    _show_table(
        activities,
        ["occurred_at", "entity_type", "entity_id", "action", "details"],
        {
            "occurred_at": "When (UTC)",
            "entity_type": "Record type",
            "entity_id": "Reference",
            "action": "Action",
            "details": "Details",
        },
        height=250,
    )


elif workspace == "Procurement":
    st.subheader("Procurement management")
    st.caption(
        "Control requisition decisions, purchase order approvals, supplier commitments, "
        "and monthly purchasing limits."
    )
    period = TODAY.strftime("%Y-%m")
    summary = procurement_summary(period)
    _metric_row(
        [
            ("Actual expenditure", _display_money(summary["actual_expenditure"]), "Cost of purchase orders received during this month."),
            ("Approved commitments", _display_money(summary["approved_commitments"]), "All approved orders not yet delivered, regardless of creation month."),
            ("Planned expenditure", _display_money(summary["planned_expenditure"]), "Current-month actual expenditure plus all outstanding approved commitments."),
        ]
    )
    _metric_row(
        [
            (
                "Allocated budget",
                _display_money(summary["budget"])
                if summary["budget_exists"]
                else "Not allocated",
                "Locally allocated procurement budget for this month.",
            ),
            (
                "Available budget",
                _display_money(summary["available_budget"])
                if summary["available_budget"] is not None
                else "Not available",
                "Allocated budget less current-month actuals and outstanding commitments.",
            ),
            (
                "Budget utilization",
                f"{summary['budget_utilization_pct']:.1f}%"
                if summary["budget_utilization_pct"] is not None
                else "Unavailable",
                "Planned expenditure divided by an allocated, non-zero monthly budget.",
            ),
        ]
    )
    if not summary["budget_exists"]:
        st.warning(
            f"No budget is allocated for {period}. Purchase-order approvals are blocked until one is recorded."
        )

    with st.expander("Manage monthly procurement budget", expanded=False):
        with st.form("budget_form"):
            budget_period = st.text_input("Budget period (YYYY-MM)", value=period)
            budget_amount = st.number_input(
                "Allocated amount", min_value=0.0, value=float(summary["budget"]), step=1000.0
            )
            save_budget = st.form_submit_button("Save budget allocation")
        if save_budget and _save_action(
            lambda: set_budget(budget_period, budget_amount),
            "Budget allocation saved.",
        ):
            st.rerun()

    req_tab, order_tab, create_tab = st.tabs(
        ["Requisitions & approvals", "Purchase orders", "Create request or order"]
    )

    with req_tab:
        requests = list_requisitions()
        _show_table(
            requests,
            [
                "request_id", "product_id", "product_name", "quantity", "requester",
                "department", "justification", "needed_by", "estimated_value",
                "status", "reviewed_by", "review_notes",
            ],
            {
                "request_id": "Request",
                "product_id": "SKU",
                "product_name": "Product",
                "quantity": "Units",
                "requester": "Requested by",
                "department": "Department",
                "justification": "Business justification",
                "needed_by": "Need by",
                "estimated_value": "Estimated value",
                "status": "Status",
                "reviewed_by": "Reviewed by",
                "review_notes": "Decision notes",
            },
        )

        pending = requests.loc[requests["status"] == "Pending"] if not requests.empty else requests
        if not pending.empty:
            st.markdown("#### Review a requisition")
            with st.form("review_requisition_form"):
                request_id = st.selectbox("Pending requisition", pending["request_id"].tolist())
                decision = st.selectbox(
                    "Decision", ("Approved", "Rejected"), key="requisition_decision"
                )
                reviewer = st.text_input("Decision owner / role", value="Procurement Manager")
                review_notes = st.text_input("Decision notes")
                review_request = st.form_submit_button("Record requisition decision")
            if review_request:
                _save_action(
                    lambda: review_requisition(
                        request_id, decision, reviewer, review_notes
                    ),
                    f"Requisition {request_id} marked {decision.lower()}.",
                )
        else:
            st.info("There are no requisitions awaiting approval.")

        editable = requests.loc[requests["status"] == "Pending"] if not requests.empty else requests
        if not editable.empty:
            st.markdown("#### Edit or delete a pending requisition")
            products = list_inventory()
            labels = _product_labels(products)
            rows = {row["request_id"]: row for row in editable.to_dict(orient="records")}
            selected = st.selectbox(
                "Requisition to edit", list(rows), format_func=lambda key: f"{key} · {rows[key]['product_name']}",
                key="edit_request_choice",
            )
            selected_row = rows[selected]
            with st.form("edit_requisition_form"):
                product = st.selectbox(
                    "Product", list(labels),
                    index=list(labels).index(selected_row["product_id"]),
                    format_func=lambda key: labels[key],
                )
                quantity = st.number_input(
                    "Units", min_value=1, value=int(selected_row["quantity"]), step=1
                )
                requester = st.text_input("Requester", value=selected_row["requester"])
                department = st.text_input("Department", value=selected_row["department"])
                justification = st.text_input(
                    "Business justification", value=selected_row["justification"]
                )
                needed_by = st.date_input(
                    "Need-by date", value=date.fromisoformat(selected_row["needed_by"])
                )
                save_request = st.form_submit_button("Save requisition changes")
            if save_request:
                _save_action(
                    lambda: update_requisition(
                        selected, product, quantity, requester, department,
                        justification, needed_by,
                    ),
                    "Requisition changes saved.",
                )

        deletable = requests.loc[
            requests["status"].isin(["Pending", "Rejected"])
        ] if not requests.empty else requests
        if not deletable.empty:
            with st.form("delete_requisition_form"):
                delete_request_id = st.selectbox(
                    "Requisition to delete",
                    deletable["request_id"].tolist(),
                    key="delete_request_choice",
                )
                confirm_delete = st.checkbox("Confirm deletion of this requisition")
                delete_request = st.form_submit_button("Delete requisition")
            if delete_request:
                if confirm_delete:
                    _save_action(
                        lambda: delete_requisition(delete_request_id),
                        "Requisition deleted.",
                    )
                else:
                    st.error("Confirm the deletion before continuing.")

    with order_tab:
        orders = list_purchase_orders()
        _show_table(
            orders,
            [
                "po_id", "request_id", "supplier", "product_name", "quantity",
                "unit_cost", "planned_value", "expected_date", "status",
                "actual_quantity", "actual_value", "department", "notes",
            ],
            {
                "po_id": "PO",
                "request_id": "Requisition",
                "supplier": "Supplier",
                "product_name": "Product",
                "quantity": "Ordered units",
                "unit_cost": "Planned unit cost",
                "planned_value": "Planned value",
                "expected_date": "Expected delivery",
                "status": "Approval / order status",
                "actual_quantity": "Received units",
                "actual_value": "Actual expenditure",
                "department": "Department",
                "notes": "Notes",
            },
        )

        pending_orders = orders.loc[orders["status"] == "Pending"] if not orders.empty else orders
        if not pending_orders.empty:
            st.markdown("#### Approve or reject a purchase order")
            with st.form("review_po_form"):
                po_id = st.selectbox("Order awaiting review", pending_orders["po_id"].tolist())
                decision = st.selectbox(
                    "Decision", ("Approved", "Rejected"), key="purchase_order_decision"
                )
                reviewer = st.text_input("Decision owner / role", value="Procurement Manager")
                override_reason = st.text_input(
                    "Budget override reason (required only if over budget; a missing budget blocks approval)"
                )
                review_po = st.form_submit_button("Record order decision")
            if review_po:
                _save_action(
                    lambda: review_purchase_order(
                        po_id, decision, reviewer, override_reason
                    ),
                    f"Purchase order {po_id} marked {decision.lower()}.",
                )
        else:
            st.info("There are no purchase orders awaiting approval.")

        editable_orders = orders.loc[
            orders["status"].isin(["Pending", "Rejected"])
        ] if not orders.empty else orders
        if not editable_orders.empty:
            st.markdown("#### Edit or delete a draft order")
            suppliers = list_suppliers()
            products = list_inventory()
            supplier_names = _supplier_labels(suppliers)
            product_names = _product_labels(products)
            rows = {
                row["po_id"]: row for row in editable_orders.to_dict(orient="records")
            }
            selected_po = st.selectbox(
                "Draft order", list(rows),
                format_func=lambda key: f"{key} · {rows[key]['product_name']}",
                key="edit_po_choice",
            )
            selected_row = rows[selected_po]
            with st.form("edit_po_form"):
                supplier_id = st.selectbox(
                    "Supplier", list(supplier_names),
                    index=list(supplier_names).index(selected_row["supplier_id"]),
                    format_func=lambda key: supplier_names[key],
                )
                product_id = st.selectbox(
                    "Product", list(product_names),
                    index=list(product_names).index(selected_row["product_id"]),
                    format_func=lambda key: product_names[key],
                )
                quantity = st.number_input(
                    "Ordered units", min_value=1, value=int(selected_row["quantity"])
                )
                unit_cost = st.number_input(
                    "Planned unit cost", min_value=0.0,
                    value=float(selected_row["unit_cost"]), format="%.2f",
                )
                expected_date = st.date_input(
                    "Expected delivery", value=date.fromisoformat(selected_row["expected_date"])
                )
                department = st.text_input("Department", value=selected_row["department"])
                notes = st.text_input("Order notes", value=selected_row["notes"])
                save_order = st.form_submit_button("Save order changes")
            if save_order:
                _save_action(
                    lambda: update_purchase_order(
                        selected_po, supplier_id, product_id, quantity,
                        unit_cost, expected_date, department, notes,
                    ),
                    "Purchase order changes saved.",
                )

            with st.form("delete_po_form"):
                delete_po_id = st.selectbox(
                    "Draft or rejected order to delete", list(rows), key="delete_po_choice"
                )
                confirm_delete_po = st.checkbox("Confirm deletion of this purchase order")
                delete_po = st.form_submit_button("Delete purchase order")
            if delete_po:
                if confirm_delete_po:
                    _save_action(
                        lambda: delete_purchase_order(delete_po_id),
                        "Purchase order deleted.",
                    )
                else:
                    st.error("Confirm the deletion before continuing.")

    with create_tab:
        inventory = list_inventory()
        suppliers = list_suppliers()
        product_names = _product_labels(inventory)
        supplier_names = _supplier_labels(suppliers)
        request_rows = list_requisitions()
        approved_requests = (
            request_rows.loc[request_rows["status"] == "Approved"]
            if not request_rows.empty else request_rows
        )
        create_request_column, create_order_column = st.columns(2)

        with create_request_column:
            st.markdown("#### Raise a purchase requisition")
            with st.form("new_requisition_form"):
                product_id = st.selectbox(
                    "Requested product", list(product_names),
                    format_func=lambda key: product_names[key],
                )
                quantity = st.number_input("Requested units", min_value=1, value=1)
                requester = st.text_input("Requesting team", value="Operations Team")
                department = st.text_input("Department", value="Operations")
                justification = st.text_area("Business reason")
                needed_by = st.date_input(
                    "Required by", value=TODAY + timedelta(days=14)
                )
                submit_request = st.form_submit_button("Submit for approval")
            if submit_request:
                _save_action(
                    lambda: create_requisition(
                        product_id, quantity, requester, department,
                        justification, needed_by,
                    ),
                    "Requisition submitted to the approval queue.",
                )

        with create_order_column:
            st.markdown("#### Create an order from an approved request")
            if approved_requests.empty:
                st.info("Approve a requisition before converting it to a purchase order.")
            else:
                req_rows = {
                    row["request_id"]: row
                    for row in approved_requests.to_dict(orient="records")
                }
                with st.form("convert_request_form"):
                    request_id = st.selectbox(
                        "Approved requisition", list(req_rows),
                        format_func=lambda key: (
                            f"{key} · {req_rows[key]['product_name']} · "
                            f"{req_rows[key]['quantity']} units"
                        ),
                    )
                    selected_request = req_rows[request_id]
                    supplier_id = st.selectbox(
                        "Supplier", list(supplier_names),
                        format_func=lambda key: supplier_names[key],
                    )
                    product_row = inventory.loc[
                        inventory["product_id"] == selected_request["product_id"]
                    ].iloc[0]
                    unit_cost = st.number_input(
                        "Agreed unit cost", min_value=0.0,
                        value=float(product_row["unit_cost"]), format="%.2f",
                    )
                    expected_date = st.date_input(
                        "Expected delivery", value=date.fromisoformat(selected_request["needed_by"])
                    )
                    notes = st.text_input("Order notes")
                    convert_request = st.form_submit_button("Create pending purchase order")
                if convert_request:
                    _save_action(
                        lambda: create_order_from_requisition(
                            request_id, supplier_id, unit_cost, expected_date, notes
                        ),
                        "Purchase order created and added to the approval queue.",
                    )

        with st.expander("Create a direct purchase order (without a requisition)"):
            with st.form("direct_po_form"):
                direct_supplier = st.selectbox(
                    "Supplier", list(supplier_names),
                    format_func=lambda key: supplier_names[key],
                    key="direct_po_supplier",
                )
                direct_product = st.selectbox(
                    "Product", list(product_names),
                    format_func=lambda key: product_names[key],
                    key="direct_po_product",
                )
                direct_qty = st.number_input("Order units", min_value=1, value=1)
                direct_cost = st.number_input(
                    "Unit cost", min_value=0.0, value=0.0, format="%.2f"
                )
                direct_date = st.date_input(
                    "Expected delivery", value=TODAY + timedelta(days=14)
                )
                direct_department = st.text_input("Department", value="Operations")
                direct_notes = st.text_input("Business notes")
                submit_direct_po = st.form_submit_button("Create pending purchase order")
            if submit_direct_po:
                _save_action(
                    lambda: create_purchase_order(
                        direct_supplier, direct_product, direct_qty, direct_cost,
                        direct_date, direct_department, direct_notes,
                    ),
                    "Purchase order created and added to the approval queue.",
                )


elif workspace == "Supplier management":
    st.subheader("Supplier relationship management")
    st.caption(
        "Compare supplier service, quality, price position, contract coverage, and risk "
        "before making an award decision."
    )
    suppliers = list_suppliers()
    if not suppliers.empty:
        delivery_events = int(suppliers["delivered_count"].sum())
        on_time_events = int(suppliers["on_time_count"].fillna(0).sum())
        overall_on_time = on_time_events / delivery_events * 100 if delivery_events else None
        _metric_row(
            [
                ("Registered suppliers", f"{len(suppliers):,}", None),
                ("Recorded deliveries", f"{delivery_events:,}", None),
                ("On-time delivery", f"{overall_on_time:.1f}%" if overall_on_time is not None else "No history", "Only delivered shipments with expected and actual dates are used."),
                ("High-risk suppliers", f"{int((suppliers['risk_level'] == 'High').sum()):,}", None),
            ]
        )

    st.markdown("#### Supplier evaluation and selection scorecard")
    st.caption(
        "Weighted score: delivery reliability 40%, quality 30%, pricing competitiveness 20%, "
        "and current contract standing 10%. New suppliers have limited delivery evidence."
    )
    _show_table(
        suppliers,
        [
            "name", "category", "delivered_count", "on_time_rate_pct",
            "quality_rate_pct", "price_competitiveness_score", "evaluation_score",
            "contract_status", "effective_contract_status", "renewal_date",
            "risk_level", "account_owner", "selection_recommendation",
        ],
        {
            "name": "Supplier",
            "category": "Category",
            "delivered_count": "Delivered records",
            "on_time_rate_pct": "On-time (%)",
            "quality_rate_pct": "Quality acceptance (%)",
            "price_competitiveness_score": "Price score / 5",
            "evaluation_score": "Weighted score / 100",
            "contract_status": "Recorded contract status",
            "effective_contract_status": "Current contract standing",
            "renewal_date": "Renewal date",
            "risk_level": "Management risk",
            "account_owner": "Account owner",
            "selection_recommendation": "Selection recommendation",
        },
    )

    create_supplier_tab, edit_supplier_tab = st.tabs(["Register supplier", "Edit or remove supplier"])
    with create_supplier_tab:
        with st.form("create_supplier_form"):
            name = st.text_input("Supplier name")
            category = st.text_input("Goods or service category")
            lead_time = st.number_input("Standard lead time (days)", min_value=0.0, value=14.0)
            quality = st.number_input(
                "Quality acceptance (%)", min_value=0.0, max_value=100.0, value=98.0
            )
            price_score = st.number_input(
                "Pricing competitiveness (1 = weak, 5 = strong)",
                min_value=1.0, max_value=5.0, value=3.0, step=0.5,
            )
            contract = st.selectbox("Contract status", ("Active", "Expiring", "Expired", "Pending"))
            renewal = st.date_input("Contract renewal date", value=TODAY + timedelta(days=365))
            risk = st.selectbox("Management risk classification", ("Low", "Medium", "High"))
            owner = st.text_input("Account owner / responsible team", value="Procurement Team")
            notes = st.text_area("Supplier notes")
            save_supplier = st.form_submit_button("Register supplier")
        if save_supplier:
            _save_action(
                lambda: create_supplier(
                    name, category, lead_time, quality, price_score,
                    contract, renewal, risk, owner, notes,
                ),
                "Supplier registered.",
            )

    with edit_supplier_tab:
        if suppliers.empty:
            st.info("Register a supplier before editing.")
        else:
            supplier_rows = {
                row["supplier_id"]: row for row in suppliers.to_dict(orient="records")
            }
            selected_supplier = st.selectbox(
                "Supplier record", list(supplier_rows),
                format_func=lambda key: supplier_rows[key]["name"],
            )
            row = supplier_rows[selected_supplier]
            renewal_value = (
                date.fromisoformat(str(row["renewal_date"]))
                if pd.notna(row["renewal_date"]) else TODAY + timedelta(days=365)
            )
            with st.form("edit_supplier_form"):
                name = st.text_input("Supplier name", value=row["name"])
                category = st.text_input("Category", value=row["category"])
                lead_time = st.number_input(
                    "Standard lead time (days)", min_value=0.0,
                    value=float(row["standard_lead_time_days"]),
                )
                quality = st.number_input(
                    "Quality acceptance (%)", min_value=0.0, max_value=100.0,
                    value=float(row["quality_rate_pct"]),
                )
                price_score = st.number_input(
                    "Price competitiveness score", min_value=1.0, max_value=5.0,
                    value=float(row["price_competitiveness_score"]), step=0.5,
                )
                contract = st.selectbox(
                    "Contract status", ("Active", "Expiring", "Expired", "Pending"),
                    index=("Active", "Expiring", "Expired", "Pending").index(row["contract_status"]),
                )
                renewal = st.date_input("Renewal date", value=renewal_value)
                risk = st.selectbox(
                    "Risk classification", ("Low", "Medium", "High"),
                    index=("Low", "Medium", "High").index(row["risk_level"]),
                )
                owner = st.text_input("Account owner", value=row["account_owner"])
                notes = st.text_area("Notes", value=row["notes"])
                save_supplier_changes = st.form_submit_button("Save supplier changes")
            if save_supplier_changes:
                _save_action(
                    lambda: update_supplier(
                        selected_supplier, name, category, lead_time, quality,
                        price_score, contract, renewal, risk, owner, notes,
                    ),
                    "Supplier record updated.",
                )
            with st.form("delete_supplier_form"):
                confirm = st.checkbox("Confirm supplier deletion")
                delete = st.form_submit_button("Delete supplier")
            if delete:
                if confirm:
                    _save_action(
                        lambda: delete_supplier(selected_supplier),
                        "Supplier deleted.",
                    )
                else:
                    st.error("Confirm deletion before continuing.")
    st.caption("Supplier delivery percentages are recalculated from shipment history; they are not manually entered estimates.")


elif workspace == "Inventory & resources":
    st.subheader("Inventory and resource control")
    st.caption(
        "Monitor available stock, reserve resources against operational needs, "
        "control replenishment, and review stock movements."
    )
    safety_weeks = st.slider(
        "Safety-stock policy (weeks of average demand)",
        min_value=0, max_value=8, value=2, key="inventory_safety_weeks",
    )
    inventory = inventory_control(safety_weeks=safety_weeks)
    if not inventory.empty:
        _metric_row(
            [
                ("Inventory value", _display_money(inventory["inventory_value"].sum()), "On-hand units × current unit cost."),
                ("Critical stock", f"{int((inventory['status'] == 'Critical').sum()):,}", "Available units at or below lead-time demand."),
                ("Reorder items", f"{int((inventory['status'] == 'Reorder').sum()):,}", "At or below policy reorder point."),
                ("Overstock items", f"{int((inventory['status'] == 'Overstock').sum()):,}", "Physical stock above the recorded maximum target."),
                ("Annual carrying cost", _display_money(inventory["annual_carrying_cost"].sum()), "Inventory value × item carrying-cost policy."),
            ]
        )

    stock_tab, replenish_tab, allocation_tab, movement_tab, import_tab = st.tabs(
        ["Inventory records", "Replenishment plan", "Resource allocation", "Stock movements", "Import inventory"]
    )
    with stock_tab:
        _show_table(
            inventory,
            [
                "product_id", "product_name", "category", "supplier", "on_hand",
                "allocated_qty", "available_qty", "incoming_qty", "minimum_stock",
                "safety_stock", "reorder_point", "maximum_stock", "days_of_cover",
                "inventory_value", "annual_carrying_cost", "status",
            ],
            {
                "product_id": "SKU",
                "product_name": "Resource",
                "category": "Category",
                "supplier": "Primary supplier",
                "on_hand": "On hand",
                "allocated_qty": "Allocated",
                "available_qty": "Available",
                "incoming_qty": "Approved incoming",
                "minimum_stock": "Minimum",
                "safety_stock": "Safety stock",
                "reorder_point": "Reorder point",
                "maximum_stock": "Maximum",
                "days_of_cover": "Days of cover",
                "inventory_value": "Value at cost",
                "annual_carrying_cost": "Annual carrying cost",
                "status": "Control status",
            },
        )

        suppliers = list_suppliers()
        supplier_names = _supplier_labels(suppliers)
        product_names = _product_labels(inventory)
        with st.expander("Create inventory record"):
            with st.form("create_inventory_form"):
                product_id = st.text_input("New SKU / resource code")
                product_name = st.text_input("Resource name")
                category = st.text_input("Category")
                supplier_id = st.selectbox(
                    "Primary supplier", list(supplier_names),
                    format_func=lambda key: supplier_names[key],
                )
                on_hand = st.number_input("On-hand units", min_value=0.0, value=0.0)
                demand = st.number_input("Average weekly demand", min_value=0.0, value=0.0)
                lead = st.number_input("Lead time (days)", min_value=0.0, value=14.0)
                unit_cost = st.number_input("Unit cost", min_value=0.0, value=0.0)
                minimum = st.number_input("Minimum stock", min_value=0.0, value=0.0)
                maximum = st.number_input("Maximum stock", min_value=0.0, value=0.0)
                carrying = st.number_input(
                    "Annual carrying cost (%)", min_value=0.0, max_value=100.0, value=22.0
                )
                create_item = st.form_submit_button("Create inventory record")
            if create_item:
                _save_action(
                    lambda: create_inventory_item(
                        product_id, product_name, category, supplier_id,
                        on_hand, demand, lead, unit_cost, minimum, maximum, carrying,
                    ),
                    "Inventory record created.",
                )

        if not inventory.empty:
            rows = {
                row["product_id"]: row for row in inventory.to_dict(orient="records")
            }
            selected_product = st.selectbox(
                "Inventory record to edit", list(rows),
                format_func=lambda key: product_names[key],
                key="edit_inventory_choice",
            )
            row = rows[selected_product]
            with st.form("edit_inventory_form"):
                product_name = st.text_input("Resource name", value=row["product_name"])
                category = st.text_input("Category", value=row["category"])
                supplier_id = st.selectbox(
                    "Primary supplier", list(supplier_names),
                    index=list(supplier_names).index(row["supplier_id"]),
                    format_func=lambda key: supplier_names[key],
                )
                on_hand = st.number_input(
                    "On-hand units", min_value=0.0, value=float(row["on_hand"])
                )
                demand = st.number_input(
                    "Average weekly demand", min_value=0.0,
                    value=float(row["avg_weekly_demand"]),
                )
                lead = st.number_input(
                    "Lead time (days)", min_value=0.0, value=float(row["lead_time_days"])
                )
                unit_cost = st.number_input(
                    "Unit cost", min_value=0.0, value=float(row["unit_cost"]), format="%.2f"
                )
                minimum = st.number_input(
                    "Minimum stock", min_value=0.0, value=float(row["minimum_stock"])
                )
                maximum = st.number_input(
                    "Maximum stock", min_value=0.0, value=float(row["maximum_stock"])
                )
                carrying = st.number_input(
                    "Annual carrying cost (%)", min_value=0.0, max_value=100.0,
                    value=float(row["carrying_cost_pct"]),
                )
                update_item = st.form_submit_button("Save inventory changes")
            if update_item:
                _save_action(
                    lambda: update_inventory_item(
                        selected_product, product_name, category, supplier_id,
                        on_hand, demand, lead, unit_cost, minimum, maximum, carrying,
                    ),
                    "Inventory record updated. Any stock change is recorded in movement history.",
                )
            with st.form("delete_inventory_form"):
                confirm = st.checkbox("Confirm inventory record deletion")
                delete_item = st.form_submit_button("Delete inventory record")
            if delete_item:
                if confirm:
                    _save_action(
                        lambda: delete_inventory_item(selected_product),
                        "Inventory record deleted.",
                    )
                else:
                    st.error("Confirm deletion before continuing.")

    with replenish_tab:
        st.write(
            "This retains the original lead-time, safety-stock, reorder-point, and "
            "target-coverage planning workflow. Recommendations are planning estimates, "
            "not purchase orders."
        )
        target_weeks = st.slider(
            "Target stock cover (weeks)",
            min_value=max(1, safety_weeks),
            max_value=12,
            value=max(4, safety_weeks),
            key="replenishment_target_weeks",
        )
        base_inventory = list_inventory()
        legacy_plan = build_replenishment_plan(
            base_inventory,
            safety_weeks=safety_weeks,
            target_cover_weeks=target_weeks,
        )
        _show_table(
            legacy_plan.sort_values(
                ["status", "recommended_order_value"],
                ascending=[True, False],
            ),
            [
                "product_id", "product_name", "supplier", "on_hand",
                "avg_weekly_demand", "lead_time_days", "safety_stock",
                "reorder_point", "target_stock", "recommended_order_qty",
                "recommended_order_value", "status",
            ],
            {
                "product_id": "SKU",
                "product_name": "Resource",
                "supplier": "Supplier",
                "on_hand": "On hand",
                "avg_weekly_demand": "Weekly demand",
                "lead_time_days": "Lead time (days)",
                "safety_stock": "Safety stock",
                "reorder_point": "Reorder point",
                "target_stock": "Target stock",
                "recommended_order_qty": "Suggested units",
                "recommended_order_value": "Estimated order value",
                "status": "Status",
            },
        )
        st.download_button(
            "Download replenishment plan",
            data=legacy_plan.to_csv(index=False).encode("utf-8"),
            file_name="supply_chain_replenishment_plan.csv",
            mime="text/csv",
        )

    with allocation_tab:
        allocations = list_allocations()
        _show_table(
            allocations,
            [
                "allocation_id", "product_id", "product_name", "destination",
                "quantity", "requested_by", "status", "created_at", "notes",
            ],
            {
                "allocation_id": "Allocation",
                "product_id": "SKU",
                "product_name": "Resource",
                "destination": "Destination",
                "quantity": "Units",
                "requested_by": "Requested by",
                "status": "Status",
                "created_at": "Requested at",
                "notes": "Notes",
            },
        )
        with st.form("create_allocation_form"):
            allocation_product = st.selectbox(
                "Resource to allocate", list(product_names),
                format_func=lambda key: product_names[key],
            )
            destination = st.text_input("Site / work package / cost centre")
            allocation_qty = st.number_input("Units to reserve", min_value=1, value=1)
            requested_by = st.text_input("Requested by", value="Operations Team")
            notes = st.text_input("Allocation notes")
            request_allocation = st.form_submit_button("Submit allocation request")
        if request_allocation:
            _save_action(
                lambda: create_allocation(
                    allocation_product, destination, allocation_qty, requested_by, notes
                ),
                "Allocation request submitted for review.",
            )
        active_allocations = allocations.loc[
            allocations["status"].isin(["Pending", "Allocated"])
        ] if not allocations.empty else allocations
        if not active_allocations.empty:
            allocation_rows = {
                row["allocation_id"]: row
                for row in active_allocations.to_dict(orient="records")
            }
            selected_allocation = st.selectbox(
                "Allocation to action", list(allocation_rows),
                format_func=lambda key: (
                    f"{key} · {allocation_rows[key]['product_name']} · "
                    f"{allocation_rows[key]['status']}"
                ),
            )
            selected_status = allocation_rows[selected_allocation]["status"]
            next_statuses = (
                ("Allocated", "Released")
                if selected_status == "Pending"
                else ("Released", "Consumed")
            )
            with st.form("update_allocation_form"):
                next_status = st.selectbox("New allocation status", next_statuses)
                reviewer = st.text_input("Decision owner", value="Inventory Controller")
                update_allocation = st.form_submit_button("Update allocation")
            if update_allocation:
                _save_action(
                    lambda: update_allocation_status(
                        selected_allocation, next_status, reviewer
                    ),
                    f"Allocation marked {next_status.lower()}.",
                )
        st.caption("Allocated units reduce available stock. Consuming an allocation reduces on-hand stock and creates an issue movement.")

    with movement_tab:
        movements = list_stock_movements()
        _show_table(
            movements,
            [
                "movement_date", "product_id", "product_name", "movement_type",
                "quantity_change", "unit_cost_at_movement", "reference", "stock_after", "notes",
            ],
            {
                "movement_date": "Date",
                "product_id": "SKU",
                "product_name": "Resource",
                "movement_type": "Movement",
                "quantity_change": "Change in units",
                "unit_cost_at_movement": "Unit cost",
                "reference": "Reference",
                "stock_after": "Stock after",
                "notes": "Notes",
            },
        )
        turnover = inventory_turnover()
        st.caption(
            f"Cost-based 90-day turnover = cost of goods issued "
            f"({turnover['cost_of_goods_issued']:,.2f}) ÷ average inventory value "
            f"at cost ({turnover['average_inventory_value']:,.2f})."
        )

    with import_tab:
        st.write(
            "Importing creates or updates local inventory records. Existing supplier names "
            "must already be registered. The import is validated and saved as one transaction."
        )
        with st.form("inventory_import_form"):
            inventory_file = st.file_uploader(
                "Inventory CSV", type="csv",
                help="Required: product_id, product_name, category, supplier, on_hand, avg_weekly_demand, lead_time_days, unit_cost.",
            )
            import_button = st.form_submit_button("Validate and save inventory file")
        if import_button:
            if inventory_file is None:
                st.error("Choose an inventory CSV file before importing.")
            else:
                try:
                    import_frame = pd.read_csv(inventory_file)
                    result = import_inventory_csv(import_frame)
                except (pd.errors.ParserError, UnicodeDecodeError, ValueError) as error:
                    st.error(f"Inventory import was not saved: {error}")
                else:
                    summary = (
                        f"Added: {result['added']} · Updated: {result['updated']} · "
                        f"Skipped: {result['skipped']} · Failed: {result['failed']}"
                    )
                    if result["failed"] or result["errors"]:
                        st.warning(f"Import finished with exceptions. {summary}")
                    else:
                        st.success(f"Import complete. {summary}")
                    if result["errors"]:
                        st.write("Import details:")
                        for error in result["errors"]:
                            st.write(f"- {error}")


elif workspace == "Logistics & distribution":
    st.subheader("Logistics and distribution control")
    st.caption(
        "Schedule approved orders, track handoffs and delivery dates, monitor transport cost, "
        "and escalate open delays."
    )
    shipments = list_shipments()
    if not shipments.empty:
        open_delays = shipments.loc[
            shipments["is_delayed"] & shipments["actual_date"].isna()
        ]
        in_transit = shipments["status"].isin(["Preparing", "In transit", "Delayed"]).sum()
        delivered = int((shipments["status"] == "Delivered").sum())
        on_time = int(
            (
                (shipments["status"] == "Delivered")
                & (shipments["days_late"] == 0)
            ).sum()
        )
        _metric_row(
            [
                ("Open consignments", f"{int(in_transit):,}", None),
                ("Overdue inbound", f"{len(open_delays):,}", "Open shipment past expected date or flagged delayed."),
                ("Delivered orders", f"{delivered:,}", None),
                ("On-time deliveries", f"{on_time / delivered * 100:.1f}%" if delivered else "No history", "Based on actual versus expected delivery dates."),
                ("Transport cost", _display_money(shipments["transport_cost"].sum()), "Illustrative recorded shipment costs."),
            ]
        )

    st.subheader("Shipment register")
    _show_table(
        shipments.sort_values(
            ["is_delayed", "expected_date"], ascending=[False, True]
        ) if not shipments.empty else shipments,
        [
            "shipment_id", "purchase_order_id", "supplier", "product_name", "destination",
            "expected_date", "actual_date", "days_late", "operational_status",
            "units", "transport_cost", "carrier", "delay_reason", "corrective_action",
        ],
        {
            "shipment_id": "Shipment",
            "purchase_order_id": "Purchase order",
            "supplier": "Supplier",
            "product_name": "Product",
            "destination": "Destination",
            "expected_date": "Expected delivery",
            "actual_date": "Actual delivery",
            "days_late": "Days late",
            "operational_status": "Status",
            "units": "Units",
            "transport_cost": "Transport cost",
            "carrier": "Carrier",
            "delay_reason": "Delay reason",
            "corrective_action": "Corrective action",
        },
        height=450,
    )

    schedule_tab, tracking_tab, edit_shipment_tab = st.tabs(
        ["Schedule inbound", "Update shipment status", "Edit or delete shipment"]
    )
    with schedule_tab:
        orders = list_purchase_orders()
        existing_order_ids = (
            set(shipments["purchase_order_id"].dropna().tolist())
            if not shipments.empty else set()
        )
        eligible_orders = orders.loc[
            (orders["status"] == "Approved")
            & (~orders["po_id"].isin(existing_order_ids))
        ] if not orders.empty else orders
        if eligible_orders.empty:
            st.info("Approve an order before scheduling a new inbound shipment.")
        else:
            eligible = {
                row["po_id"]: row for row in eligible_orders.to_dict(orient="records")
            }
            with st.form("schedule_shipment_form"):
                po_id = st.selectbox(
                    "Approved purchase order", list(eligible),
                    format_func=lambda key: (
                        f"{key} · {eligible[key]['supplier']} · "
                        f"{eligible[key]['quantity']} units"
                    ),
                )
                destination = st.text_input("Receiving destination")
                expected = st.date_input(
                    "Expected delivery date", value=TODAY + timedelta(days=7)
                )
                transport_cost = st.number_input(
                    "Estimated transportation cost", min_value=0.0, value=0.0
                )
                carrier = st.text_input("Carrier / transport provider")
                create_shipment_button = st.form_submit_button("Schedule shipment")
            if create_shipment_button:
                _save_action(
                    lambda: create_shipment(
                        po_id, destination, expected, transport_cost, carrier
                    ),
                    "Inbound shipment scheduled.",
                )

    with tracking_tab:
        active = shipments.loc[
            ~shipments["status"].isin(["Delivered", "Cancelled"])
        ] if not shipments.empty else shipments
        if active.empty:
            st.info("There are no open shipments to update.")
        else:
            shipment_rows = {
                row["shipment_id"]: row for row in active.to_dict(orient="records")
            }
            selected_shipment = st.selectbox(
                "Shipment", list(shipment_rows),
                format_func=lambda key: (
                    f"{key} · {shipment_rows[key]['supplier']} · "
                    f"{shipment_rows[key]['operational_status']}"
                ),
            )
            current_status = shipment_rows[selected_shipment]["status"]
            allowed = {
                "Scheduled": ("Preparing", "In transit", "Cancelled"),
                "Preparing": ("In transit", "Delayed", "Cancelled"),
                "In transit": ("Delayed", "Delivered", "Cancelled"),
                "Delayed": ("In transit", "Delivered", "Cancelled"),
            }[current_status]
            with st.form("shipment_status_form"):
                next_status = st.selectbox("New status", allowed)
                actual_date = st.date_input("Actual delivery date", value=TODAY)
                update_shipment_button = st.form_submit_button("Save shipment status")
            if update_shipment_button:
                _save_action(
                    lambda: update_shipment_status(
                        selected_shipment,
                        next_status,
                        actual_date if next_status == "Delivered" else None,
                    ),
                    f"Shipment status updated to {next_status.lower()}.",
                )
            st.caption("Marking a linked shipment delivered posts a stock receipt and completes its approved purchase order exactly once.")

    with edit_shipment_tab:
        editable_shipments = shipments.loc[
            ~shipments["status"].isin(["Delivered", "Cancelled"])
        ] if not shipments.empty else shipments
        if editable_shipments.empty:
            st.info("There are no open shipment records to edit.")
        else:
            shipment_rows = {
                row["shipment_id"]: row
                for row in editable_shipments.to_dict(orient="records")
            }
            selected_shipment = st.selectbox(
                "Open shipment to edit", list(shipment_rows),
                key="edit_shipment_choice",
                format_func=lambda key: (
                    f"{key} · {shipment_rows[key]['supplier']} · "
                    f"{shipment_rows[key]['destination']}"
                ),
            )
            row = shipment_rows[selected_shipment]
            with st.form("edit_shipment_form"):
                destination = st.text_input("Destination", value=row["destination"])
                expected = st.date_input(
                    "Expected delivery date", value=date.fromisoformat(row["expected_date"])
                )
                transport_cost = st.number_input(
                    "Transportation cost", min_value=0.0, value=float(row["transport_cost"])
                )
                carrier = st.text_input("Carrier", value=row["carrier"])
                delay_reason = st.text_input("Delay reason", value=row["delay_reason"])
                corrective = st.text_input(
                    "Corrective action", value=row["corrective_action"]
                )
                save_shipment = st.form_submit_button("Save shipment details")
            if save_shipment:
                _save_action(
                    lambda: update_shipment_details(
                        selected_shipment, destination, expected, transport_cost,
                        carrier, delay_reason, corrective,
                    ),
                    "Shipment details updated.",
                )

        deletable_shipments = shipments.loc[
            shipments["status"].isin(["Scheduled", "Cancelled"])
        ] if not shipments.empty else shipments
        if not deletable_shipments.empty:
            with st.form("delete_shipment_form"):
                delete_id = st.selectbox(
                    "Scheduled or cancelled shipment to delete",
                    deletable_shipments["shipment_id"].tolist(),
                )
                confirm = st.checkbox("Confirm shipment deletion")
                delete_shipment_button = st.form_submit_button("Delete shipment record")
            if delete_shipment_button:
                if confirm:
                    _save_action(
                        lambda: delete_shipment(delete_id),
                        "Shipment record deleted.",
                    )
                else:
                    st.error("Confirm deletion before continuing.")


elif workspace == "Demand & supply planning":
    st.subheader("Demand and supply plan")
    st.caption(
        "This is a planning worksheet based on average weekly demand, current available "
        "inventory, approved incoming orders, and the selected planning horizon. "
        "It is not an advanced forecast."
    )
    horizon = st.slider(
        "Planning horizon (weeks)", min_value=1, max_value=26, value=4
    )
    safety = st.slider(
        "Safety-stock policy (weeks)", min_value=0, max_value=8, value=2,
        key="planning_safety_weeks",
    )
    plan = demand_supply_plan(horizon, safety)
    if not plan.empty:
        total_shortage = float(plan["supply_shortage"].sum())
        total_incoming = float(plan["incoming_qty"].sum())
        total_requirement = float(plan["future_requirement"].sum())
        _metric_row(
            [
                ("Planning horizon", f"{horizon} weeks", None),
                ("Estimated demand", _display_money(plan["demand_estimate"].sum()) + " units", "Average weekly demand × planning weeks."),
                ("Available inventory", _display_money(plan["available_qty"].sum()) + " units", "On-hand stock less allocated resources."),
                ("Approved incoming", _display_money(total_incoming) + " units", "Approved orders not yet received; not added to on-hand stock."),
                ("Uncovered demand", _display_money(total_shortage) + " units", "Planning horizon demand less available stock and approved incoming."),
            ]
        )
        st.metric(
            "Future replenishment requirement",
            f"{total_requirement:,.0f} units",
            help="Quantity required to raise projected stock to the recorded maximum target when at or below the reorder point.",
        )
    _show_table(
        plan,
        [
            "product_id", "product_name", "category", "on_hand", "allocated_qty",
            "available_qty", "incoming_qty", "demand_estimate",
            "projected_available", "supply_shortage", "recommended_order_qty",
            "planned_procurement_qty", "future_requirement", "status",
        ],
        {
            "product_id": "SKU",
            "product_name": "Resource",
            "category": "Category",
            "on_hand": "On hand",
            "allocated_qty": "Allocated",
            "available_qty": "Available",
            "incoming_qty": "Approved incoming",
            "demand_estimate": "Demand estimate",
            "projected_available": "Available + incoming",
            "supply_shortage": "Uncovered demand",
            "recommended_order_qty": "Recommended new order",
            "planned_procurement_qty": "Total planned procurement",
            "future_requirement": "Future requirement",
            "status": "Inventory status",
        },
        height=470,
    )
    st.download_button(
        "Download demand and supply plan",
        data=plan.to_csv(index=False).encode("utf-8"),
        file_name="supply_chain_demand_supply_plan.csv",
        mime="text/csv",
    )


else:
    st.subheader("Management risk register and corrective actions")
    st.caption(
        "Issues are identified from current inventory, shipment dates, supplier delivery "
        "records, procurement approvals, and budget commitments. Assign an owner and due date, "
        "then update resolution status as corrective work progresses."
    )
    issues = list_issues()
    unlinked_issue_count = (
        int(issues["action_id"].isna().sum())
        if not issues.empty and "action_id" in issues
        else 0
    )
    if unlinked_issue_count:
        st.info(
            f"{unlinked_issue_count} detected issue(s) do not yet have an action record. "
            "Synchronize only when management wants to assign and track them."
        )
    if "issue_sync_notice" in st.session_state:
        st.success(st.session_state.pop("issue_sync_notice"))
    if st.button("Synchronize detected issues to action register"):
        result = synchronize_corrective_actions()
        st.session_state["issue_sync_notice"] = (
            f"Created {result['created']}, reopened {result['reopened']}, "
            f"refreshed {result['updated']}, unchanged {result['unchanged']} action(s)."
        )
        st.rerun()
    if not issues.empty:
        unresolved = issues.loc[issues["resolution_status"] != "Resolved"]
        _metric_row(
            [
                ("Current issues", f"{len(issues):,}", None),
                ("Unresolved", f"{len(unresolved):,}", None),
                ("High priority", f"{int((unresolved['priority'] == 'High').sum()):,}", None),
                ("Overdue shipments", f"{int(issues['issue_key'].str.startswith('shipment:').sum()):,}", None),
            ]
        )
    _show_table(
        issues,
        [
            "priority", "issue", "operational_impact", "recommendation",
            "resolution_status", "responsible", "department", "due_date",
            "issue_key", "corrective_notes",
        ],
        {
            "priority": "Priority",
            "issue": "Identified problem",
            "operational_impact": "Operational impact",
            "recommendation": "Recommended management action",
            "resolution_status": "Resolution status",
            "responsible": "Responsible person / role",
            "department": "Department",
            "due_date": "Target date",
            "issue_key": "Issue reference",
            "corrective_notes": "Manager notes",
        },
        height=440,
    )

    actions = list_corrective_actions()
    st.markdown("#### Corrective action register")
    _show_table(
        actions,
        [
            "action_id", "issue_key", "issue", "operational_impact",
            "priority", "recommendation", "resolution_status", "responsible",
            "department", "due_date", "notes", "updated_at",
        ],
        {
            "action_id": "Action",
            "issue_key": "Linked issue",
            "issue": "Issue / action",
            "operational_impact": "Impact",
            "priority": "Priority",
            "recommendation": "Recommended action",
            "resolution_status": "Status",
            "responsible": "Owner",
            "department": "Department",
            "due_date": "Due date",
            "notes": "Progress notes",
            "updated_at": "Last updated",
        },
    )

    create_action_tab, edit_action_tab = st.tabs(
        ["Create corrective action", "Update or delete action"]
    )
    with create_action_tab:
        with st.form("create_action_form"):
            issue = st.text_input("Problem / action title")
            impact = st.text_area("Operational impact")
            priority = st.selectbox("Priority", PRIORITIES)
            recommendation = st.text_area("Recommended management action")
            responsible = st.text_input("Responsible person or role")
            department = st.text_input("Department")
            due_date = st.date_input("Target completion date", value=TODAY + timedelta(days=7))
            notes = st.text_area("Initial progress notes")
            create_action_button = st.form_submit_button("Create corrective action")
        if create_action_button:
            _save_action(
                lambda: create_corrective_action(
                    issue, impact, priority, recommendation,
                    responsible, department, due_date, notes,
                ),
                "Corrective action created.",
            )

    with edit_action_tab:
        if actions.empty:
            st.info("There are no corrective actions to update.")
        else:
            action_rows = {
                row["action_id"]: row for row in actions.to_dict(orient="records")
            }
            selected_action = st.selectbox(
                "Corrective action", list(action_rows),
                format_func=lambda key: f"{key} · {action_rows[key]['issue']}",
            )
            row = action_rows[selected_action]
            due_value = (
                date.fromisoformat(str(row["due_date"]))
                if pd.notna(row["due_date"]) else TODAY + timedelta(days=7)
            )
            with st.form("edit_action_form"):
                issue = st.text_input("Problem / action title", value=row["issue"])
                impact = st.text_area(
                    "Operational impact", value=row["operational_impact"]
                )
                priority = st.selectbox(
                    "Priority", PRIORITIES, index=PRIORITIES.index(row["priority"])
                )
                recommendation = st.text_area(
                    "Recommended action", value=row["recommendation"]
                )
                status = st.selectbox(
                    "Resolution status",
                    ACTION_STATUSES,
                    index=ACTION_STATUSES.index(row["resolution_status"]),
                )
                responsible = st.text_input("Responsible owner", value=row["responsible"])
                department = st.text_input("Department", value=row["department"])
                due_date = st.date_input("Target completion date", value=due_value)
                notes = st.text_area("Progress notes", value=row["notes"])
                save_action_button = st.form_submit_button("Save action update")
            if save_action_button:
                _save_action(
                    lambda: update_corrective_action(
                        selected_action, issue, impact, priority, recommendation,
                        status, responsible, department, due_date, notes,
                    ),
                    "Corrective action updated.",
                )
            with st.form("delete_action_form"):
                confirm = st.checkbox("Confirm corrective action deletion")
                delete_action_button = st.form_submit_button("Delete corrective action")
            if delete_action_button:
                if confirm:
                    _save_action(
                        lambda: delete_corrective_action(selected_action),
                        "Corrective action deleted.",
                    )
                else:
                    st.error("Confirm deletion before continuing.")


st.divider()
st.caption(
    "Adarsh Tiwari · Supply Chain Operations & Management · "
    "All bundled suppliers, costs, transactions, and performance events are illustrative."
)
