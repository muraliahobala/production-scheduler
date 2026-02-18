# main.py
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Any, Dict

from scheduler import solve_schedule

app = FastAPI()

class ScheduleRequest(BaseModel):
    data: Dict[str, Any]

@app.post("/optimize-schedule")
def optimize_schedule(req: ScheduleRequest):
    # req.data will contain your jobs, machines, etc.
    result = solve_schedule(req.data)
    return result
