# scheduler.py
from ortools.sat.python import cp_model

def solve_schedule(request_data: dict) -> dict:
    # 1) Create model
    model = cp_model.CpModel()

    # TODO: parse request_data and build variables/constraints here

    # For now, just make a trivial example so we know things run
    x = model.NewIntVar(0, 10, "x")
    model.Maximize(x)

    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "infeasible"}

    # Example response structure
    return {
        "status": "ok",
        "objective": solver.ObjectiveValue(),
        "x": solver.Value(x),
    }
