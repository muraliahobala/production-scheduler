# config.py
import os
from typing import Dict, Any

def get_solver_defaults() -> Dict[str, Any]:
    """Default solver parameters from environment variables."""
    return {
        "max_time_seconds": float(os.getenv("CP_SAT_MAX_TIME_SEC", "30.0")),
        "num_workers": int(os.getenv("CP_SAT_NUM_WORKERS", "0")),  # 0 = auto
        "relative_gap": float(os.getenv("CP_SAT_REL_GAP", "0.05")),  # 5%
        "log_search": os.getenv("CP_SAT_LOG_SEARCH", "false").lower() in ("true", "1", "yes"),
    }

def get_logging_level() -> str:
    """Get log level from environment: DEBUG, INFO, WARNING, ERROR."""
    return os.getenv("LOG_LEVEL", "INFO").upper()
