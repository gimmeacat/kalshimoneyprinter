import csv
import os
from datetime import datetime
from pathlib import Path


DEFAULT_FIELDS = [
    "timestamp",
    "event",
    "ticker",
    "strategy",
    "outcome",
    "count",
    "entry_price",
    "exit_price",
    "entry_fee",
    "exit_fee",
    "gross_pnl",
    "net_pnl",
    "reason",
]


def log_trade(data):
    base_dir = Path(__file__).resolve().parent
    data_dir = base_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    log_file = Path(
        os.getenv(
            "TRADE_LOG_FILE",
            str(data_dir / "trades.csv"),
        )
    )

    log_file.parent.mkdir(parents=True, exist_ok=True)

    file_exists = log_file.exists()

    row = {
        field: data.get(field, "")
        for field in DEFAULT_FIELDS
    }

    if not row["timestamp"]:
        row["timestamp"] = (
            datetime.now().isoformat(
                timespec="seconds"
            )
        )

    with open(
        log_file,
        "a",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=DEFAULT_FIELDS,
        )

        if not file_exists:
            writer.writeheader()

        writer.writerow(row)