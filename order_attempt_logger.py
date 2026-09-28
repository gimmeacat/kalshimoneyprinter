import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# ============================================================
# PATH / ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

load_dotenv(BASE_DIR / ".env")


ORDER_ATTEMPT_LOG_FILE = os.getenv(
    "ORDER_ATTEMPT_LOG_FILE",
    str(DATA_DIR / "order_attempts.csv"),
)


# ============================================================
# CSV COLUMNS
# ============================================================

FIELDNAMES = [
    "timestamp_utc",
    "ticker",
    "strategy",
    "outcome",
    "count",
    "signal_price",
    "requested_limit_price",
    "snapped_limit_price",
    "model_probability",
    "market_probability",
    "edge",
    "seconds_remaining",
    "brti_price",
    "target_price",
    "distance_dollars",
    "status",
    "fill_count",
    "actual_fill_price",
    "error_type",
    "error_message",
]


# ============================================================
# HELPERS
# ============================================================

def _clean(value: Any) -> Any:
    if value is None:
        return ""

    if isinstance(value, float):
        return round(
            value,
            8,
        )

    return value


# ============================================================
# ORDER ATTEMPT LOGGER
# ============================================================

def log_order_attempt(data: dict[str, Any]) -> None:
    """
    Append one order-attempt record to CSV.

    Typical status values:
        FILLED
        PARTIAL_FILL
        ZERO_FILL
        NO_RESPONSE
        API_REJECTED
        BLOCKED_LIQUIDITY
        BLOCKED_INSUFFICIENT_BALANCE
        BLOCKED_EXISTING_POSITION
    """

    if not data:
        print(
            "[ORDER LOG WARNING] "
            "Empty attempt ignored."
        )
        return

    if not data.get("ticker"):
        print(
            "[ORDER LOG WARNING] "
            "Attempt without ticker ignored."
        )
        return

    if not data.get("status"):
        print(
            "[ORDER LOG WARNING] "
            "Attempt without status ignored."
        )
        return

    path = Path(
        ORDER_ATTEMPT_LOG_FILE
    )

    if not path.is_absolute():
        path = BASE_DIR / path

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_exists = path.exists()

    row: dict[str, Any] = {
        "timestamp_utc": (
            datetime.now(
                timezone.utc
            ).isoformat()
        ),
    }

    for field in FIELDNAMES:
        if field == "timestamp_utc":
            continue

        row[field] = _clean(
            data.get(field)
        )

    try:
        with path.open(
            "a",
            newline="",
            encoding="utf-8",
        ) as file:

            writer = csv.DictWriter(
                file,
                fieldnames=FIELDNAMES,
                extrasaction="ignore",
            )

            if not file_exists:
                writer.writeheader()

            writer.writerow(
                row
            )

    except Exception as exc:
        print(
            "[ORDER LOG ERROR] "
            f"{type(exc).__name__}: {exc}"
        )