# main.py
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any  # ← add Dict, Any

from fastapi import FastAPI, Request, HTTPException  # ← add HTTPException
from pydantic import BaseModel

from config import get_logging_level
from scheduler import solve_schedule

from fastapi.middleware.cors import CORSMiddleware
import os
import requests  # ← for server-side HTTP calls to Knack

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://apps.knack.com",
        "http://localhost:3000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    logger.info(
        f"📥 REQUEST: {request.method} {request.url.path} "
        f"(run_id: {request.query_params.get('run_id', 'unknown')})"
    )
    response = await call_next(request)
    process_time = time.time() - start_time
    logger.info(
        f"📤 RESPONSE: {request.method} {request.url.path} "
        f"status={response.status_code} time={process_time:.2f}s"
    )
    return response

@app.post("/optimize-schedule")
async def optimize_schedule(request: Request):
    """
    Accepts raw JSON from Knack (bypasses Pydantic validation) and calls CP-SAT solver.
    """
    try:
        payload = await request.json()
        logger.info(f"📥 Raw payload keys: {list(payload.keys())}")
        logger.info(
            f"Scheduling run '{payload.get('run_id', 'unknown')}': "
            f"{len(payload.get('production_orders', []))} orders, "
            f"{len(payload.get('machines', []))} machines"
        )
        result = solve_schedule(payload)
        logger.info(
            f"Scheduling '{payload.get('run_id', 'unknown')}' COMPLETE: "
            f"status={result['status']} "
            f"ops={len(result.get('schedule_operations', []))} "
            f"orders_on_time={result['summary']['orders_on_time']}/"
            f"{result['summary']['total_orders']}"
        )
         # 🔥 NEW: AUTO-SAVE TO KNACK
        logger.info("💾 Auto-saving results to Knack...")
        save_result = save_knack_schedule(result)  # Call your save function
        logger.info(f"✅ Save complete: {save_result}")
        return result

    except Exception as e:
        logger.error(f"❌ Endpoint error: {str(e)}")
        return {
            "run_id": "error",
            "status": "error",
            "error": str(e),
            "message": "Invalid JSON or server error",
        }

# ---------- NEW: SAVE RESULTS TO KNACK ----------
@app.post("/save-knack-schedule")
async def save_knack_schedule(data: Dict[str, Any]):
    """
    Save schedule results into Knack objects (runs, operations, utilization).
    This runs server-side, so no CORS issues.
    """
    try:
        run_id = data.get("run_id")
        status = data.get("status")
        summary = data.get("summary", {})
        schedule_operations = data.get("schedule_operations", [])
        resource_utilization = data.get("resource_utilization", [])

        logger.info(f"💾 Saving schedule '{run_id}' to Knack")

        app_id = os.getenv("KNACK_APP_ID")
        api_token = os.getenv("KNACK_API_TOKEN")
        if not app_id or not api_token:
            raise RuntimeError("KNACK_APP_ID or KNACK_API_TOKEN not set")

        headers = {
            "X-Knack-Application-Id": app_id,
            "X-Knack-REST-API-Key": api_token,
            "Content-Type": "application/json",
            "User-Agent": "ProductionScheduler/1.0"
        }

        # TODO: replace these with your real object IDs
        RUN_OBJECT = "object_13"   # Schedule Runs
        OPS_OBJECT = "object_14"   # Operations
        UTIL_OBJECT = "object_15"  # Utilization

        # 1️⃣ Save schedule run
        run_payload = {
            "field_128": run_id,
            "field_131": status,
            "field_132": summary.get("total_orders", 0),
            "field_133": summary.get("orders_on_time", 0),
            "field_134": summary.get("total_operations", 0),
        }
        r = requests.post(
            f"https://api.knack.com/v1/objects/{RUN_OBJECT}/records",
            json=run_payload,
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        logger.info(f"✅ Run saved to {RUN_OBJECT}")

        # 2️⃣ Save operations (limit 50)
        for op in schedule_operations[:50]:
            op_payload = {
                "field_136": run_id,
                "field_137": op.get("order_id"),
                "field_138": op.get("routing_operation_id"),
                "field_139": op.get("work_center_id"),
                "field_140": op.get("machine_id"),
                "field_141": op.get("qty"),
                "field_142": op.get("start"),
                "field_143": op.get("end"),
                "field_144": op.get("duration_hours", 0),
            }
            r = requests.post(
                f"https://api.knack.com/v1/objects/{OPS_OBJECT}/records",
                json=op_payload,
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
        logger.info(f"✅ {len(schedule_operations[:50])} ops saved to {OPS_OBJECT}")

        # 3️⃣ Save utilization (limit 20)
        for res in resource_utilization[:20]:
            util_payload = {
                "field_146": run_id,
                "field_147": res.get("machine_id"),
                "field_148": res.get("work_center_id"),
                "field_149": res.get("total_capacity_hours", 0),
                "field_150": res.get("consumed_hours", 0),
                "field_151": res.get("utilization_pct", 0),
                "field_152": str(res.get("order_product_usage", [])),
            }
            r = requests.post(
                f"https://api.knack.com/v1/objects/{UTIL_OBJECT}/records",
                json=util_payload,
                headers=headers,
                timeout=20,
            )
            r.raise_for_status()
        logger.info(f"✅ {len(resource_utilization[:20])} resources saved to {UTIL_OBJECT}")

        return {"success": True, "message": "Saved schedule to Knack"}

    except Exception as e:
        logger.error(f"❌ Knack save error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat() + "Z"}

logger.info("API ready at http://127.0.0.1:8000/docs")

def lookup_production_order(order_id: str) -> dict:
    """Find OP_ORD_002 record by searching ALL records"""
    try:
        url = f"https://api.knack.com/v1/objects/object_3/records"
        params = {'rows_per_page': 200}  # Get more records
        
        response = requests.get(url, headers={
            'X-Knack-Application-Id': os.getenv("KNACK_APP_ID"),
            'X-Knack-REST-API-Key': os.getenv("KNACK_API_TOKEN")
        }, params=params, timeout=10)
        
        if response.ok:
            records = response.json().get('records', [])
            logger.info(f"🔍 Scanned {len(records)} total records")
            
            # 🔥 SEARCH for exact order_id match
            for record in records:
                if record.get('field_17_raw') == order_id or record.get('field_17') == order_id:
                    logger.info(f"✅ FOUND {order_id}: record_id={record['id']}")
                    
                    product_raw = record.get('field_98_raw', [])
                    product_id = product_raw[0].get('identifier') if product_raw else record.get('field_181', 'PROD001')
                    
                    customer_raw = record.get('field_180_raw', [])
                    customer_id = customer_raw[0].get('identifier') if customer_raw else 'Test Customer'
                    
                    logger.info(f"✅ Extracted: product_id='{product_id}', customer='{customer_id}'")
                    return {'product_id': product_id, 'customer': customer_id}
            
            logger.warning(f"❌ {order_id} not found in {len(records)} records")
        
        return {'product_id': 'PROD001', 'customer': 'Test Customer'}
        
    except Exception as e:
        logger.error(f"❌ Lookup crashed: {e}")
        return {'product_id': 'PROD001', 'customer': 'Test Customer'}

@app.post("/promise-order")
async def promise_order(request: Request):
    try:
        payload = await request.json()
        logger.info(f"🔮 Promise payload received: run_id={payload.get('run_id')}")
        
        # 🔥 FIX 1: Correctly extract reference_order from production_orders[0]
        reference_order = None
        if (payload.get('production_orders') and 
            len(payload['production_orders']) > 0 and 
            'reference_order' in payload['production_orders'][0]):
            reference_order = payload['production_orders'][0]['reference_order']
            logger.info(f"✅ Found reference_order: {reference_order}")
        else:
            logger.warning("❌ No reference_order in production_orders[0]")
        
        # 🔥 FIX 2: Always ensure product_id exists (default to PROD001)
        if payload.get('production_orders') and payload['production_orders']:
            trial_order = payload['production_orders'][0]
            if 'product_id' not in trial_order:
                trial_order['product_id'] = 'PROD001'  # Default
                logger.info("✅ Added default product_id=PROD001")
            
            # Lookup if reference_order exists
            if reference_order:
                order_details = lookup_production_order(reference_order)
                trial_order['product_id'] = order_details.get('product_id', trial_order['product_id'])
                trial_order['customer'] = order_details.get('customer', 'Test Customer')
                logger.info(f"✅ Updated with lookup: {order_details}")
        
        logger.info(f"🔮 Final production_orders[0]: {payload['production_orders'][0]}")
        
        result = solve_schedule(payload, promise_mode=True)
        logger.info(f"✅ Promise COMPLETE: feasible={result.get('feasible', False)}")
        return result
        
    except KeyError as e:
        logger.error(f"❌ Promise KeyError: {e}")
        return {"feasible": False, "status_message": f"Missing required field: {e}"}
    except Exception as e:
        logger.error(f"❌ Promise error: {str(e)}", exc_info=True)
        return {"feasible": False, "status_message": str(e)}
        
@app.post("/save-promise-record")
async def save_promise_record(request: Request):
    """Find & update promise record by reference_order (LIKE PRODUCTION SCHEDULER)"""
    try:
        payload = await request.json()
        result = payload.get('result')
        
        # 🔥 STEP 1: Extract reference_order from result (like scheduler does)
        reference_order = None
        if (result.get('production_orders') and 
            len(result['production_orders']) > 0):
            reference_order = result['production_orders'][0].get('reference_order')
        
        logger.info(f"💾 Promise save: reference_order='{reference_order}'")
        
        if not reference_order:
            return {"error": "No reference_order in result"}
        
        # 🔥 STEP 2: Find record by reference_order (SAME AS lookup_production_order)
        records = []
        url = f"https://api.knack.com/v1/objects/object_3/records"
        params = {'rows_per_page': 200}
        
        response = requests.get(url, headers={
            'X-Knack-Application-Id': os.getenv("KNACK_APP_ID"),
            'X-Knack-REST-API-Key': os.getenv("KNACK_API_TOKEN")
        }, params=params)
        
        if response.ok:
            records = response.json().get('records', [])
            logger.info(f"🔍 Found {len(records)} records")
            
            # Find matching record
            target_record = None
            for record in records:
                if record.get('field_17') == reference_order or record.get('field_17_raw') == reference_order:
                    target_record = record
                    break
            
            if not target_record:
                logger.error(f"❌ No record found for {reference_order}")
                return {"error": f"Promise record '{reference_order}' not found"}
            
            record_id = target_record['id']
            logger.info(f"✅ Found record {record_id} for {reference_order}")
        
        # 🔥 STEP 3: Update record (SAME FIELDS AS BEFORE)
        update_data = {
            "field_178": "Yes" if result.get("feasible") else "No",
            "field_179": result.get("status_message", "Promised"),
            "field_177": result.get("proposed_date", "").split("T")[0]
        }
        
        update_url = f"https://api.knack.com/v1/objects/object_3/records/{record_id}"
        response = requests.put(update_url, headers={
            'X-Knack-Application-Id': os.getenv("KNACK_APP_ID"),
            'X-Knack-REST-API-Key': os.getenv("KNACK_API_TOKEN"),
            'Content-Type': 'application/json'
        }, json=update_data)
        
        logger.info(f"💾 Updated {record_id}: {response.status_code}")
        
        if response.ok:
            return {"success": True, "record_id": record_id, "reference_order": reference_order}
        else:
            return {"error": response.text}
            
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
        return {"error": str(e)}
