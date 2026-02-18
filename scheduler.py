# scheduler.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
import logging
from config import get_solver_defaults, get_logging_level

# ---------- Helper dataclasses (internal) ----------

@dataclass
class OperationDef:
    routing_operation_id: str
    routing_id: str          
    sequence: int
    work_center_id: str
    operation_name: str    # ← ADD THIS LINE
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

from datetime import datetime

def parse_iso(dt_val) -> datetime:
    """
    Accepts either an ISO string with optional 'Z' at the end
    or an already-parsed datetime, and returns a datetime.
    """
    if isinstance(dt_val, datetime):
        return dt_val
    # force to string in case it's something else
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
    """
    For now: a simple, deterministic scheduler that:
      - Respects routing sequences.
      - Assigns operations to machines in their work center.
      - Schedules in whole-day blocks.
      - Minimizes lateness only via greedy ordering by due_date.

    Shape is ready to be replaced later by a CP-SAT model.
    """
    logger = logging.getLogger("scheduler")
    logger.info(f"🔄 solve_schedule START: run_id={request_data['run_id']}")
    
    # Log input summary
    logger.info(
        f"📊 INPUT: {len(request_data['production_orders'])} orders, "
        f"{len(request_data['machines'])} machines, "
        f"{len(request_data['routing_operations'])} routing ops"
    )
    
    run_id: str = request_data["run_id"]
    horizon_raw = request_data["horizon"]
    settings = request_data.get("settings", {})
    objective = settings.get("objective", "minimize_lateness")

    products = request_data["products"]
    work_centers = request_data["work_centers"]
    machines_raw = request_data["machines"]
    routings = request_data["routings"]
    routing_operations_raw = request_data["routing_operations"]
    production_orders = request_data["production_orders"]
    existing_schedule_operations = request_data.get("existing_schedule_operations", [])

    logger.info(f"📊 {len(production_orders)} orders, {len(machines_raw)} machines")

    logger.info("⚙️  Applying solver configuration")
    solver_params = get_solver_defaults()
    logger.info(f"Solver defaults: time={solver_params['max_time_seconds']}s, workers={solver_params['num_workers']}")

    # ---------- Time discretization: days ----------

    horizon_start = horizon_raw["start"]
    horizon_end = horizon_raw["end"]
    num_days = date_range_days(horizon_start, horizon_end)
    if num_days <= 0:
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
            "infeasibilities": [
                {"type": "HORIZON_INVALID", "message": "Horizon start >= end"}
            ],
        }

    # ---------- Routing operations by routing_id ----------

    routing_ops_by_routing: Dict[str, List[OperationDef]] = {}
    for ro in routing_operations_raw:
        rd = OperationDef(
            routing_operation_id=ro["routing_operation_id"],
            routing_id=ro["routing_id"],
            sequence=ro["sequence"],
            work_center_id=ro["work_center_id"],
            operation_name=ro["operation_name"],
            std_time_per_unit_hours=float(ro["std_time_per_unit_hours"]),
            min_batch_size=int(ro["min_batch_size"]),
            max_batch_size=int(ro["max_batch_size"]),
        )
        routing_ops_by_routing.setdefault(rd.routing_id, []).append(rd)

    # Sort operations by sequence within each routing
    for rid in routing_ops_by_routing:
        routing_ops_by_routing[rid].sort(key=lambda x: x.sequence)

    # ---------- Machines by work_center ----------

    machines_by_wc: Dict[str, List[MachineDef]] = {}
    for m in machines_raw:
        md = MachineDef(
            machine_id=m["machine_id"],
            work_center_id=m["work_center_id"],
            daily_capacity_hours=float(m["daily_capacity_hours"]),
            is_active=bool(m["is_active"]),
        )
        if md.is_active:
            machines_by_wc.setdefault(md.work_center_id, []).append(md)

    # ---------- Routing lookup by product_id ----------

    routing_by_product: Dict[str, str] = {}
    for r in routings:
        if r["is_active"]:
            routing_by_product[r["product_id"]] = r["routing_id"]

    # ---------- Simple greedy scheduling ----------

    # Sort orders by due_date (earliest first)
    orders_sorted = sorted(
        production_orders, key=lambda o: parse_iso(o["due_date"])
    )

    scheduled_ops: List[Dict[str, Any]] = []
    order_results: List[Dict[str, Any]] = []

    day_slot_usage: Dict[Tuple[str, str, int], float] = {}
    # key: (work_center_id, machine_id, day_index) -> used_hours

    temp_counter = 1
    total_processing_hours = 0.0
    max_lateness_hours = 0.0
    orders_on_time = 0
    orders_late = 0

    for order in orders_sorted:
        order_id = order["order_id"]
        product_id = order["product_id"]
        qty = int(order["required_quantity"])
        due_date = order["due_date"]

        routing_id = routing_by_product.get(product_id)
        if routing_id is None:
            # No routing for this product
            order_results.append(
                {
                    "order_id": order_id,
                    "required_quantity": qty,
                    "scheduled_quantity": 0,
                    "due_date": order["due_date"],
                    "completion_time": None,
                    "lateness_hours": None,
                    "is_on_time": False,
                }
            )
            continue

        ops_def = routing_ops_by_routing[routing_id]

        # For simplicity, we schedule full quantity in one batch per op
        current_start_time = horizon_start  # earliest possible
        last_end_time = current_start_time

        for op_def in ops_def:
            wc_id = op_def.work_center_id
            machines_here = machines_by_wc.get(wc_id, [])
            if not machines_here:
                # No machine for this work center
                order_results.append(
                    {
                        "order_id": order_id,
                        "required_quantity": qty,
                        "scheduled_quantity": 0,
                        "due_date": order["due_date"],
                        "completion_time": None,
                        "lateness_hours": None,
                        "is_on_time": False,
                    }
                )
                break

            # processing hours for this operation
            proc_hours = op_def.std_time_per_unit_hours * qty

            # naive: schedule on first machine with space, starting after last_end_time
            scheduled = False
            for day_index in range(num_days):
                day_start = horizon_start + timedelta(days=day_index)
                day_end = day_start + timedelta(days=1)

                # respect precedence: can't start before last_end_time
                if day_end <= last_end_time:
                    continue

                for mach in machines_here:
                    key = (wc_id, mach.machine_id, day_index)
                    used = day_slot_usage.get(key, 0.0)
                    capacity = mach.daily_capacity_hours
                    available = capacity - used

                    if available >= proc_hours:
                        # schedule full operation on this day/machine
                        start_time = max(day_start, last_end_time)
                        end_time = start_time + timedelta(hours=proc_hours)

                        day_slot_usage[key] = used + proc_hours
                        scheduled = True
                        last_end_time = end_time
                        total_processing_hours += proc_hours

                        temp_id = f"{run_id}-op-{temp_counter:04d}"
                        temp_counter += 1

                        scheduled_ops.append(
                            {
                                "temp_id": temp_id,
                                "order_id": order_id,
                                "routing_operation_id": op_def.routing_operation_id,
                                "work_center_id": wc_id,
                                "machine_id": mach.machine_id,
                                "qty": qty,
                                "start": start_time.isoformat().replace("+00:00", "Z"),
                                "end": end_time.isoformat().replace("+00:00", "Z"),
                                "status": "Proposed",
                            }
                        )
                        break

                if scheduled:
                    break

            if not scheduled:
                # Could not schedule this operation within horizon
                order_results.append(
                    {
                        "order_id": order_id,
                        "required_quantity": qty,
                        "scheduled_quantity": 0,
                        "due_date": order["due_date"],
                        "completion_time": None,
                        "lateness_hours": None,
                        "is_on_time": False,
                    }
                )
                break

        else:
            # All operations scheduled for this order
            completion_time = last_end_time
            lateness = max(0.0, hours_between(due_date, completion_time))
            max_lateness_hours = max(max_lateness_hours, lateness)
            is_on_time = lateness == 0.0
            if is_on_time:
                orders_on_time += 1
            else:
                orders_late += 1

            order_results.append(
                {
                    "order_id": order_id,
                    "required_quantity": qty,
                    "scheduled_quantity": qty,
                    "due_date": order["due_date"],
                    "completion_time": completion_time.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "lateness_hours": lateness,
                    "is_on_time": is_on_time,
                }
            )

    # ---------- Build output JSON ----------

    status = "feasible" if orders_on_time + orders_late > 0 else "infeasible"

    summary = {
        "total_orders": len(production_orders),
        "orders_on_time": orders_on_time,
        "orders_late": orders_late,
        "max_lateness_hours": max_lateness_hours if orders_on_time + orders_late > 0 else None,
        "total_processing_hours": total_processing_hours,
    }

    result: Dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "order_results": order_results,
        "schedule_operations": scheduled_ops,
        "infeasibilities": [],
    }

#    logger.info(
#        f"✅ solve_schedule COMPLETE: status={status}, "
#        f"objective={solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else 'N/A'}, "
#        f"time={solver.WallTime():.2f}s"
#    )
    
    logger.info(f"🏁 Scheduling COMPLETE: status={result['status']}, ops={len(result.get('schedule_operations', []))}, hours={result['summary']['total_processing_hours']}")

    return result