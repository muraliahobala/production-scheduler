# main.py
import logging
import time
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, Request
from pydantic import BaseModel

from config import get_logging_level
from scheduler import solve_schedule

# ---------- Logging setup ----------
logging.basicConfig(
    level=get_logging_level(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("api")
logger.info("🚀 Production Scheduler API starting up")

# ---------- Your existing Pydantic models (unchanged) ----------
class Horizon(BaseModel):
    start: datetime
    end: datetime

class Settings(BaseModel):
    objective: str

class Product(BaseModel):
    product_id: str
    code: str
    name: str
    default_batch_size: int
    standard_pack_size: int

class WorkCenter(BaseModel):
    work_center_id: str
    code: str
    name: str
    type: str
    daily_capacity_hours: float

class Machine(BaseModel):
    machine_id: str
    code: str
    name: str
    work_center_id: str
    daily_capacity_hours: float
    is_active: bool

class Routing(BaseModel):
    routing_id: str
    product_id: str
    code: str
    is_active: bool

class RoutingOperation(BaseModel):
    routing_operation_id: str
    routing_id: str
    sequence: int
    work_center_id: str
    operation_name: str
    std_time_per_unit_hours: float
    min_batch_size: int
    max_batch_size: int

class ProductionOrder(BaseModel):
    order_id: str
    order_number: str
    product_id: str
    required_quantity: int
    due_date: datetime
    priority: str
    status: str

class ExistingScheduleOperation(BaseModel):
    temp_id: str
    order_id: str
    routing_operation_id: str
    work_center_id: str
    machine_id: str
    qty: int
    start: datetime
    end: datetime
    status: str

class ScheduleInput(BaseModel):
    run_id: str
    horizon: Horizon
    settings: Settings
    products: List[Product]
    work_centers: List[WorkCenter]
    machines: List[Machine]
    work_center_calendars: List[dict] = []
    machine_calendars: List[dict] = []
    routings: List[Routing]
    routing_operations: List[RoutingOperation]
    production_orders: List[ProductionOrder]
    existing_schedule_operations: List[ExistingScheduleOperation] = []

# ---------- FastAPI app with logging middleware ----------
app = FastAPI(title="Production Scheduler API")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    logger.info(f"📥 REQUEST: {request.method} {request.url.path} (run_id: {request.query_params.get('run_id', 'unknown')})")
    
    response = await call_next(request)
    process_time = time.time() - start_time
    
    logger.info(
        f"📤 RESPONSE: {request.method} {request.url.path} "
        f"status={response.status_code} time={process_time:.2f}s"
    )
    return response

@app.post("/optimize-schedule")
async def optimize_schedule(payload: ScheduleInput, request: Request):
    """
    Accepts the JSON exactly as you showed from Knack and calls the solver.
    """
    logger.info(
        f"Scheduling run '{payload.run_id}': "
        f"{len(payload.production_orders)} orders, "
        f"{len(payload.machines)} machines, "
        f"horizon {payload.horizon.start.date()} → {payload.horizon.end.date()}"
    )
    
    # Convert to a plain dict to pass into solve_schedule
    data = payload.dict()
    result = solve_schedule(data)
    
    logger.info(
        f"Scheduling '{payload.run_id}' COMPLETE: "
        f"status={result['status']} "
        f"ops={len(result.get('schedule_operations', []))} "
        f"orders_on_time={result['summary']['orders_on_time']}/{result['summary']['total_orders']}"
    )
    
    return result

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat() + "Z"}

logger.info("API ready at http://127.0.0.1:8000/docs")
