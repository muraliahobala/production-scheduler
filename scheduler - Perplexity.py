from typing import Dict, Any, List
from ortools.sat.python import cp_model

def solve_schedule(request_data: Dict[str, Any]) -> Dict[str, Any]:
    # 1) Extract input data
    machines = request_data["machines"]
    jobs = request_data["jobs"]
    horizon = request_data["horizon"]
    parameters = request_data.get("parameters", {})

    # 2) Create model
    model = cp_model.CpModel()

    # 3) Create variables
    # op_vars[(job_id, op_id)] = {
    #   "start": IntVar,
    #   "end": IntVar,
    #   "interval": IntervalVar,
    #   "machine_id": str
    # }
    op_vars: Dict[tuple, Dict[str, Any]] = {}

    # For convenience, build a list of machine IDs
    machine_ids = [m["id"] for m in machines]

    for job in jobs:
        job_id = job["id"]
        for op in job["operations"]:
            op_id = op["id"]
            processing_time = op["processing_time"]
            earliest_start = op.get("earliest_start", 0)

            # For now, assume exactly one machine per operation
            op_machine_ids = op.get("machine_ids", [])
            if len(op_machine_ids) != 1:
                # You can later extend this to handle multiple machines
                raise ValueError(
                    f"Operation {op_id} must have exactly one machine for this simple model"
                )
            machine_id = op_machine_ids[0]

            # Create start and end variables within [0, horizon]
            start_var = model.NewIntVar(earliest_start, horizon, f"start_{job_id}_{op_id}")
            end_var = model.NewIntVar(0, horizon, f"end_{job_id}_{op_id}")

            # Create interval variable [start, start + processing_time)
            interval_var = model.NewIntervalVar(
                start_var,
                processing_time,
                end_var,
                f"interval_{job_id}_{op_id}"
            )

            op_vars[(job_id, op_id)] = {
                "start": start_var,
                "end": end_var,
                "interval": interval_var,
                "machine_id": machine_id,
                "processing_time": processing_time,
            }


    # 4) Add constraints

    # 4a) Precedence constraints within each job
    for job in jobs:
        job_id = job["id"]
        ops = job["operations"]
        for i in range(len(ops) - 1):
            current_op = ops[i]
            next_op = ops[i + 1]

            cur_key = (job_id, current_op["id"])
            next_key = (job_id, next_op["id"])

            cur_end = op_vars[cur_key]["end"]
            next_start = op_vars[next_key]["start"]

            # next operation cannot start before current one finishes
            model.Add(next_start >= cur_end)

    # 4b) No-overlap constraints on each machine
    for machine_id in machine_ids:
        machine_intervals: List[cp_model.IntervalVar] = []

        for job in jobs:
            job_id = job["id"]
            for op in job["operations"]:
                op_id = op["id"]
                key = (job_id, op_id)
                if op_vars[key]["machine_id"] == machine_id:
                    machine_intervals.append(op_vars[key]["interval"])

        if machine_intervals:
            model.AddNoOverlap(machine_intervals)


    # 5) Add objective
    # Simple objective: minimize makespan (finish all operations as early as possible)
    all_end_vars = [v["end"] for v in op_vars.values()]
    if not all_end_vars:
        # No operations: nothing to schedule
        return {
            "status": "ok",
            "objective_value": 0,
            "jobs": [],
            "unassigned_jobs": [],
            "metadata": {
                "solve_time_seconds": 0.0,
                "solver_status": "NO_OPS"
            }
        }

    makespan = model.NewIntVar(0, horizon, "makespan")
    for end_var in all_end_vars:
        model.Add(makespan >= end_var)

    model.Minimize(makespan)


    # 6) Configure solver, solve, and build response
    solver = cp_model.CpSolver()

    # Optional: set parameters from request
    time_limit = parameters.get("time_limit_seconds")
    if time_limit is not None:
        solver.parameters.max_time_in_seconds = float(time_limit)

    num_workers = parameters.get("num_workers")
    if num_workers is not None:
        solver.parameters.num_search_workers = int(num_workers)

    status = solver.Solve(model)

    # Map CP-SAT status to a string
    status_map = {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }
    status_str = status_map.get(status, "UNKNOWN")

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "infeasible",
            "objective_value": None,
            "jobs": [],
            "unassigned_jobs": [job["id"] for job in jobs],
            "metadata": {
                "solve_time_seconds": solver.WallTime(),
                "solver_status": status_str,
            }
        }

    # Build result structure
    result_jobs = []
    for job in jobs:
        job_id = job["id"]
        due_date = job.get("due_date")
        ops_result = []

        completion_time = 0
        for op in job["operations"]:
            op_id = op["id"]
            key = (job_id, op_id)
            start_val = solver.Value(op_vars[key]["start"])
            end_val = solver.Value(op_vars[key]["end"])
            machine_id = op_vars[key]["machine_id"]

            ops_result.append({
                "id": op_id,
                "machine_id": machine_id,
                "start": int(start_val),
                "end": int(end_val),
            })

            if end_val > completion_time:
                completion_time = end_val

        # Compute tardiness if due_date is provided
        if due_date is not None:
            tardiness = max(0, completion_time - due_date)
        else:
            tardiness = 0

        result_jobs.append({
            "id": job_id,
            "completion_time": int(completion_time),
            "tardiness": int(tardiness),
            "operations": ops_result,
        })

    response: Dict[str, Any] = {
        "status": "ok",
        "objective_value": float(solver.ObjectiveValue()),
        "jobs": result_jobs,
        "unassigned_jobs": [],
        "metadata": {
            "solve_time_seconds": solver.WallTime(),
            "solver_status": status_str,
        }
    }
    return response

#    return {}