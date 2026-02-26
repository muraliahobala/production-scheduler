# scheduler.py - FULL ENHANCED VERSION

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from ortools.sat.python import cp_model
import logging
from config import get_solver_defaults, get_logging_level

# ---------- Helper dataclasses (internal) ----------

@dataclass
class OperationDef:
    routing_operation_id: str
    routing_id: str          
    sequence: int
    work_center_id: str
    operation_name: str
    std_time_per_unit_hours: float
    min_batch_size: int
    max_batch_size: int

@dataclass
class MachineDef:
    machine_id: str
    work_center_id: str
    daily_capacity_hours: float
    is_active: bool

# ---------- Utility functions ----------

def parse_iso(dt_val) -> datetime:
    """
    Accepts either an ISO string with optional 'Z' at the end
    or an already-parsed datetime, and returns a datetime.
    """
    if isinstance(dt_val, datetime):
        return dt_val
    s = str(dt_val)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def hours_between(start: datetime, end: datetime) -> float:
    delta = end - start
    return delta.total_seconds() / 3600.0

def date_range_days(start: datetime, end: datetime) -> int:
    """Number of full days from start to end (ceil)."""
    return int((end - start).days)

# ---------- Main entry ----------

def solve_schedule(request_data: Dict[str, Any]) -> Dict[str, Any]:
    logger = logging.getLogger("scheduler")
    run_id: str = request_data["run_id"]
    logger.info(f"🔄 CP-SAT solve_schedule START: run_id={run_id}")

    # ----- Extract input -----
    horizon_raw = request_data["horizon"]
    horizon_start: datetime = parse_iso(horizon_raw["start"])
    horizon_end: datetime = parse_iso(horizon_raw["end"])

    work_centers = request_data["work_centers"]
    machines_raw = request_data["machines"]
    routings = request_data["routings"]
    routing_operations_raw = request_data["routing_operations"]
    production_orders = request_data["production_orders"]

    logger.info(
        f"📊 INPUT: {len(production_orders)} orders, "
        f"{len(machines_raw)} machines, {len(routing_operations_raw)} routing ops"
    )

    # ----- Time discretization: hours -----
    horizon_hours = int((horizon_end - horizon_start).total_seconds() // 3600)
    if horizon_hours <= 0:
        return {
            "run_id": run_id,
            "status": "infeasible",
            "summary": {
                "total_orders": len(production_orders),
                "orders_on_time": 0,
                "orders_late": len(production_orders),
                "max_lateness_hours": None,
                "total_processing_hours": 0,
            },
            "order_results": [],
            "schedule_operations": [],
            "resource_utilization": [],
            "infeasibilities": [
                {"type": "HORIZON_INVALID", "message": "Horizon start >= end"}
            ],
        }

    # ----- Index machines by work_center -----
    machines_by_wc: Dict[str, List[Dict[str, Any]]] = {}
    for m in machines_raw:
        if m["is_active"]:
            machines_by_wc.setdefault(m["work_center_id"], []).append(m)

    # ----- Routing ops per routing_id -----
    routing_ops_by_routing: Dict[str, List[Dict[str, Any]]] = {}
    for ro in routing_operations_raw:
        routing_ops_by_routing.setdefault(ro["routing_id"], []).append(ro)

    for rid in routing_ops_by_routing:
        routing_ops_by_routing[rid].sort(key=lambda x: x["sequence"])

    # ----- Routing per product -----
    routing_by_product: Dict[str, str] = {}
    for r in routings:
        if r["is_active"]:
            routing_by_product[r["product_id"]] = r["routing_id"]

    # ----- Build CP-SAT model -----
    model = cp_model.CpModel()

    op_vars: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order_completion: Dict[str, cp_model.IntVar] = {}
    order_lateness: Dict[str, cp_model.IntVar] = {}

    # Build operations
    for order in production_orders:
        order_id = order["order_id"]
        product_id = order["product_id"]
        qty = int(order["required_quantity"])
        due_dt_raw = order["due_date"]
        due_dt: datetime = parse_iso(due_dt_raw) if due_dt_raw else horizon_end
        due_hours = int((due_dt - horizon_start).total_seconds() // 3600)
        if due_hours < 0:
            due_hours = 0

        routing_id = routing_by_product.get(product_id)
        if routing_id is None:
            logger.warning(f"No routing for product {product_id}, order {order_id}")
            continue

        ops_def = routing_ops_by_routing.get(routing_id, [])
        if not ops_def:
            logger.warning(f"No routing_operations for routing {routing_id}")
            continue

        prev_end = None
        last_end_vars: List[cp_model.IntVar] = []

        for ro in ops_def:
            ro_id = ro["routing_operation_id"]
            wc_id = ro["work_center_id"]
            std_time_per_unit_hours = float(ro["std_time_per_unit_hours"])

            machines_here = machines_by_wc.get(wc_id, [])
            if not machines_here:
                logger.warning(f"No machines for work_center {wc_id} (order {order_id})")
                continue

            machine = machines_here[0]
            machine_id = machine["machine_id"]

            proc_hours = int(round(std_time_per_unit_hours * qty))
            if proc_hours <= 0:
                proc_hours = 1

            start = model.NewIntVar(0, horizon_hours, f"start_{order_id}_{ro_id}")
            end = model.NewIntVar(0, horizon_hours, f"end_{order_id}_{ro_id}")
            interval = model.NewIntervalVar(start, proc_hours, end, f"int_{order_id}_{ro_id}")

            op_vars[(order_id, ro_id)] = {
                "start": start,
                "end": end,
                "interval": interval,
                "order_id": order_id,
                "routing_operation_id": ro_id,
                "work_center_id": wc_id,
                "machine_id": machine_id,
                "qty": qty,
                "proc_hours": proc_hours,
            }

            if prev_end is not None:
                model.Add(start >= prev_end)
            prev_end = end
            last_end_vars.append(end)

        if last_end_vars:
            comp = model.NewIntVar(0, horizon_hours, f"completion_{order_id}")
            model.AddMaxEquality(comp, last_end_vars)
            order_completion[order_id] = comp

            lat = model.NewIntVar(0, horizon_hours * 2, f"lateness_{order_id}")
            model.Add(lat >= comp - due_hours)
            model.Add(lat >= 0)
            order_lateness[order_id] = lat

    # Machine no-overlap constraints
    intervals_by_machine: Dict[str, List[cp_model.IntervalVar]] = {}
    for key, ov in op_vars.items():
        mid = ov["machine_id"]
        intervals_by_machine.setdefault(mid, []).append(ov["interval"])

    for mid, intervals in intervals_by_machine.items():
        model.AddNoOverlap(intervals)

    # Objective: minimize total lateness
    if order_lateness:
        model.Minimize(sum(order_lateness.values()))
    else:
        logger.warning("No schedulable orders found; returning infeasible")
        return {
            "run_id": run_id,
            "status": "infeasible",
            "summary": {
                "total_orders": len(production_orders),
                "orders_on_time": 0,
                "orders_late": len(production_orders),
                "max_lateness_hours": None,
                "total_processing_hours": 0,
            },
            "order_results": [],
            "schedule_operations": [],
            "resource_utilization": [],
            "infeasibilities": [],
        }

    # ----- Solver -----
    solver = cp_model.CpSolver()
    solver_params = get_solver_defaults()
    solver.max_time_in_seconds = solver_params["max_time_seconds"]
    solver.num_search_workers = solver_params["num_workers"]
    solver.relative_gap_limit = solver_params["relative_gap"]
    solver.log_search_progress = solver_params["log_search"]

    logger.info(
        f"⚙️  Solver params: time={solver.max_time_in_seconds}s, "
        f"workers={solver.num_search_workers}, "
        f"rel_gap={solver.relative_gap_limit}"
    )

    status = solver.Solve(model)

    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status_str = status_map.get(status, "UNKNOWN")

    logger.info(
        f"🧮 CP-SAT finished: status={status_str}, "
        f"obj={solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 'NA'}, "
        f"time={solver.WallTime():.2f}s"
    )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "run_id": run_id,
            "status": "infeasible",
            "summary": {
                "total_orders": len(production_orders),
                "orders_on_time": 0,
                "orders_late": len(production_orders),
                "max_lateness_hours": None,
                "total_processing_hours": 0,
            },
            "order_results": [],
            "schedule_operations": [],
            "resource_utilization": [],
            "infeasibilities": [],
        }

    # ----- 🔥 ENHANCED OUTPUT WITH RESOURCE UTILIZATION -----
    schedule_operations: List[Dict[str, Any]] = []
    total_processing_hours = 0.0
    orders_on_time = 0
    orders_late = 0
    max_lateness_hours = 0.0
    order_results: List[Dict[str, Any]] = []

    # Map order_id -> parsed due_date & qty
    due_by_order: Dict[str, datetime] = {
        o["order_id"]: parse_iso(o["due_date"]) if o.get("due_date") else None 
        for o in production_orders
    }
    qty_by_order: Dict[str, int] = {
        o["order_id"]: int(o["required_quantity"]) for o in production_orders
    }

    # 🔥 ENHANCED: Track hours by order/product per machine
    resource_utilization: Dict[str, Dict[str, Any]] = {}
    machine_usage: Dict[str, Dict[Tuple[str, str], float]] = {}
    horizon_days = (horizon_end - horizon_start).days + 1

    # Build schedule_operations AND track resource usage
    temp_counter = 1
    for (order_id, ro_id), ov in op_vars.items():
        start_h = solver.Value(ov["start"])
        end_h = solver.Value(ov["end"])
        machine_id = ov["machine_id"]
        wc_id = ov["work_center_id"]
        qty = ov["qty"]
        proc_h = ov["proc_hours"]
        total_processing_hours += proc_h

        # 🔥 Track hours by (order, product) combination
        ro_data = next((ro for ro in routing_operations_raw if ro["routing_operation_id"] == ro_id), {})
        routing_data = next((r for r in routings if r["routing_id"] == ro_data.get("routing_id", "")), {})
        product_id = routing_data.get("product_id", "Unknown")
        
        usage_key = (order_id, product_id)
        
        if machine_id not in machine_usage:
            machine_usage[machine_id] = {}
        if usage_key not in machine_usage[machine_id]:
            machine_usage[machine_id][usage_key] = 0.0
        machine_usage[machine_id][usage_key] += proc_h

        start_dt = horizon_start + timedelta(hours=start_h)
        end_dt = horizon_start + timedelta(hours=end_h)

        temp_id = f"{run_id}-op-{temp_counter:04d}"
        temp_counter += 1

        schedule_operations.append({
            "temp_id": temp_id,
            "order_id": order_id,
            "routing_operation_id": ro_id,
            "work_center_id": wc_id,
            "machine_id": machine_id,  # ✅ INCLUDED
            "qty": qty,
            "start": start_dt.isoformat().replace("+00:00", "Z"),
            "end": end_dt.isoformat().replace("+00:00", "Z"),
            "duration_hours": float(end_h - start_h),
            "status": "Proposed",
        })

        # Initialize resource tracking
        if machine_id not in resource_utilization:
            resource_utilization[machine_id] = {"consumed_hours": 0.0}
        resource_utilization[machine_id]["consumed_hours"] += proc_h

    # 🔥 FINAL RESOURCE UTILIZATION WITH HOURS PER ORDER/PRODUCT
    for machine_id, usage in resource_utilization.items():
        machine_def = next((m for m in machines_raw if m["machine_id"] == machine_id), {})
        usage["total_capacity_hours"] = machine_def.get("daily_capacity_hours", 0) * horizon_days
        usage["remaining_hours"] = usage["total_capacity_hours"] - usage["consumed_hours"]
        usage["utilization_pct"] = (usage["consumed_hours"] / usage["total_capacity_hours"] * 100) if usage["total_capacity_hours"] > 0 else 0
        usage["work_center_id"] = machine_def.get("work_center_id", "")
        
        # 🔥 Hours consumed per order/product combination
        order_product_hours = []
        for (order_id, product_id), hours in machine_usage.get(machine_id, {}).items():
            order_product_hours.append({
                "order_id": order_id,
                "product_id": product_id,
                "hours_consumed": round(hours, 2)
            })
        usage["order_product_usage"] = order_product_hours

    # Build enhanced order_results (with order start/end times)
    for order_id in qty_by_order:
        qty = qty_by_order[order_id]
        due_dt = due_by_order[order_id]
        
        if order_id in order_completion:
            comp_h = solver.Value(order_completion[order_id])
            comp_dt = horizon_start + timedelta(hours=comp_h)
            lat_h = float(solver.Value(order_lateness[order_id]))
            is_on_time = lat_h <= 0.0
            
            if is_on_time:
                orders_on_time += 1
            else:
                orders_late += 1
            if lat_h > max_lateness_hours:
                max_lateness_hours = lat_h

            # Find first/last operation for order start/end
            order_ops = [op for op in schedule_operations if op["order_id"] == order_id]
            order_start = min((op["start"] for op in order_ops), default=None) if order_ops else None
            order_end = max((op["end"] for op in order_ops), default=None) if order_ops else None

            order_results.append({
                "order_id": order_id,
                "required_quantity": qty,
                "scheduled_quantity": qty,
                "due_date": due_dt.isoformat().replace("+00:00", "Z") if due_dt else None,
                "order_start": order_start,      # ✅ Order start time
                "order_end": order_end,          # ✅ Order end time
                "completion_time": comp_dt.isoformat().replace("+00:00", "Z"),
                "lateness_hours": lat_h,
                "is_on_time": is_on_time,
                "operations_count": len(order_ops)
            })
        else:
            order_results.append({
                "order_id": order_id,
                "required_quantity": qty,
                "scheduled_quantity": 0,
                "due_date": due_dt.isoformat().replace("+00:00", "Z") if due_dt else None,
                "order_start": None,
                "order_end": None,
                "completion_time": None,
                "lateness_hours": None,
                "is_on_time": False,
                "operations_count": 0
            })

    summary = {
        "total_orders": len(production_orders),
        "orders_on_time": orders_on_time,
        "orders_late": orders_late,
        "max_lateness_hours": max_lateness_hours if (orders_on_time + orders_late) > 0 else None,
        "total_processing_hours": total_processing_hours,
        "total_operations": len(schedule_operations),
        "avg_utilization_pct": sum(u["utilization_pct"] for u in resource_utilization.values()) / max(1, len(resource_utilization))
    }

    result: Dict[str, Any] = {
        "run_id": run_id,
        "status": "feasible" if schedule_operations else "infeasible",
        "summary": summary,
        "order_results": order_results,              # ✅ 1️⃣ Orders w/ start/end times
        "schedule_operations": schedule_operations,  # ✅ 2️⃣ Operations w/ machine_id
        "resource_utilization": list(resource_utilization.values()),  # ✅ 3️⃣ Hours per order/product
        "infeasibilities": [],
    }

    logger.info(
        f"🏁 CP-SAT COMPLETE: {result['status']}, "
        f"{len(schedule_operations)} ops, "
        f"{orders_on_time}/{len(production_orders)} on-time, "
        f"avg util {summary['avg_utilization_pct']:.1f}%"
    )

    return result
