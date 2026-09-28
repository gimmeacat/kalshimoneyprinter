import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from kalshi_orders import get_executable_buy_quote
from btc_data import BRTITracker


SERIES = "KXBTC15M"

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2"))
TRADE_COUNT = float(os.getenv("TRADE_COUNT", "6"))
ENTRY_SLIPPAGE_DOLLARS = float(os.getenv("ENTRY_SLIPPAGE_DOLLARS", "0.03"))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SNAP_FILE = DATA_DIR / "snapshots_2s.csv"
RESULT_FILE = DATA_DIR / "results_2s.csv"


SNAP_FIELDS = [
    "ts_utc",
    "ticker",
    "close_time",
    "secs_to_close",
    "floor_strike",

    # BRTI
    "brti_price",
    "brti_price_change",
    "brti_volatility_per_sqrt_second",
    "brti_sample_count",
    "distance_to_target",

    # Kalshi market
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_bid_size",
    "yes_ask_size",

    # Fresh executable orderbook liquidity for TRADE_COUNT
    "book_yes_best_ask",
    "book_yes_exec_price",
    "book_yes_visible_count",
    "book_yes_depth_3c",
    "book_no_best_ask",
    "book_no_exec_price",
    "book_no_visible_count",
    "book_no_depth_3c",
    "book_trade_count",

    "last_price",
    "volume",
]


RESULT_FIELDS = [
    "ticker",
    "close_time",
    "floor_strike",
    "result",
    "expiration_value",
]



def ensure_csv_schema(path, fields):
    """Upgrade an existing CSV header without discarding old snapshots."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        current_fields = reader.fieldnames or []
        if current_fields == list(fields):
            return
        rows = list(reader)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})

    temp_path.replace(path)
    print(f"Upgraded snapshot CSV schema: {path}")


def orderbook_snapshot(client, ticker):
    """Return fresh executable quotes/depth for the configured trade size."""
    orderbook = client.request(
        "GET",
        f"/markets/{ticker}/orderbook",
    )

    yes_quote = get_executable_buy_quote(
        orderbook,
        "yes",
        TRADE_COUNT,
    )
    no_quote = get_executable_buy_quote(
        orderbook,
        "no",
        TRADE_COUNT,
    )

    yes_best = yes_quote.get("best_ask")
    no_best = no_quote.get("best_ask")

    yes_depth_3c = 0.0
    no_depth_3c = 0.0

    if yes_best is not None:
        yes_depth_3c = float(
            get_executable_buy_quote(
                orderbook,
                "yes",
                1e12,
                max_outcome_price=min(0.99, yes_best + ENTRY_SLIPPAGE_DOLLARS),
            ).get("visible_count", 0.0)
        )

    if no_best is not None:
        no_depth_3c = float(
            get_executable_buy_quote(
                orderbook,
                "no",
                1e12,
                max_outcome_price=min(0.99, no_best + ENTRY_SLIPPAGE_DOLLARS),
            ).get("visible_count", 0.0)
        )

    return {
        "book_yes_best_ask": yes_best,
        "book_yes_exec_price": yes_quote.get("full_fill_price"),
        "book_yes_visible_count": yes_quote.get("visible_count", 0.0),
        "book_yes_depth_3c": yes_depth_3c,
        "book_no_best_ask": no_best,
        "book_no_exec_price": no_quote.get("full_fill_price"),
        "book_no_visible_count": no_quote.get("visible_count", 0.0),
        "book_no_depth_3c": no_depth_3c,
        "book_trade_count": TRADE_COUNT,
    }

def append_row(path, fields, row):
    is_new = not os.path.exists(path)

    with open(
        path,
        "a",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields,
        )

        if is_new:
            writer.writeheader()

        writer.writerow(row)


def parse_time(s):
    return datetime.fromisoformat(
        s.replace("Z", "+00:00")
    )


def safe_float(value):
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main():

    client = KalshiClient()

    # BRTI 최근 120초 history 유지
    brti = BRTITracker(
        history_seconds=120
    )

    # 정산 결과 대기
    # ticker -> close_time
    pending = {}

    ensure_csv_schema(
        SNAP_FILE,
        SNAP_FIELDS,
    )

    print(
        "BTC 15m + BRTI 2초 기록 시작 "
        "(중지: Ctrl+C)"
    )

    while True:

        loop_start = time.time()

        try:

            now = datetime.now(
                timezone.utc
            )

            # ==================================================
            # 1. BRTI 업데이트
            # ==================================================

            brti_price = (
                brti.get_current_price(
                    client
                )
            )

            brti_change = (
                brti.get_price_change()
            )

            brti_volatility = (
                brti
                .get_volatility_per_sqrt_second()
            )

            brti_samples = (
                brti.sample_count()
            )

            # ==================================================
            # 2. 현재 Kalshi BTC 15m 시장
            # ==================================================

            data = client.request(
                "GET",
                "/markets",
                params={
                    "series_ticker": SERIES,
                    "status": "open",
                },
            )

            markets = data.get(
                "markets",
                []
            )

            for m in markets:

                close = parse_time(
                    m["close_time"]
                )

                secs_to_close = int(
                    (
                        close - now
                    ).total_seconds()
                )

                floor_strike = safe_float(
                    m.get("floor_strike")
                )

                if floor_strike is not None:
                    distance_to_target = (
                        brti_price
                        - floor_strike
                    )
                else:
                    distance_to_target = None

                book_data = {
                    "book_yes_best_ask": None,
                    "book_yes_exec_price": None,
                    "book_yes_visible_count": None,
                    "book_yes_depth_3c": None,
                    "book_no_best_ask": None,
                    "book_no_exec_price": None,
                    "book_no_visible_count": None,
                    "book_no_depth_3c": None,
                    "book_trade_count": TRADE_COUNT,
                }

                try:
                    book_data.update(
                        orderbook_snapshot(
                            client,
                            m["ticker"],
                        )
                    )
                except Exception as exc:
                    print(
                        "ORDERBOOK SNAPSHOT ERROR | "
                        f"{m['ticker']} | "
                        f"{type(exc).__name__}: {exc}"
                    )

                append_row(
                    SNAP_FILE,
                    SNAP_FIELDS,
                    {
                        "ts_utc":
                            now.isoformat(),

                        "ticker":
                            m["ticker"],

                        "close_time":
                            m["close_time"],

                        "secs_to_close":
                            secs_to_close,

                        "floor_strike":
                            floor_strike,

                        # ------------------------------
                        # BRTI
                        # ------------------------------

                        "brti_price":
                            brti_price,

                        "brti_price_change":
                            brti_change,

                        "brti_volatility_per_sqrt_second":
                            brti_volatility,

                        "brti_sample_count":
                            brti_samples,

                        "distance_to_target":
                            distance_to_target,

                        # ------------------------------
                        # Kalshi
                        # ------------------------------

                        "yes_bid":
                            m.get(
                                "yes_bid_dollars"
                            ),

                        "yes_ask":
                            m.get(
                                "yes_ask_dollars"
                            ),

                        "no_bid":
                            m.get(
                                "no_bid_dollars"
                            ),

                        "no_ask":
                            m.get(
                                "no_ask_dollars"
                            ),

                        "yes_bid_size":
                            m.get(
                                "yes_bid_size_fp"
                            ),

                        "yes_ask_size":
                            m.get(
                                "yes_ask_size_fp"
                            ),

                        "book_yes_best_ask":
                            book_data["book_yes_best_ask"],

                        "book_yes_exec_price":
                            book_data["book_yes_exec_price"],

                        "book_yes_visible_count":
                            book_data["book_yes_visible_count"],

                        "book_yes_depth_3c":
                            book_data["book_yes_depth_3c"],

                        "book_no_best_ask":
                            book_data["book_no_best_ask"],

                        "book_no_exec_price":
                            book_data["book_no_exec_price"],

                        "book_no_visible_count":
                            book_data["book_no_visible_count"],

                        "book_no_depth_3c":
                            book_data["book_no_depth_3c"],

                        "book_trade_count":
                            book_data["book_trade_count"],

                        "last_price":
                            m.get(
                                "last_price_dollars"
                            ),

                        "volume":
                            m.get(
                                "volume_fp"
                            ),
                    },
                )

                pending[
                    m["ticker"]
                ] = m["close_time"]

            # ==================================================
            # 3. Settlement 확인
            # ==================================================

            for ticker, close_time in list(
                pending.items()
            ):

                seconds_since_close = (
                    now
                    - parse_time(
                        close_time
                    )
                ).total_seconds()

                if seconds_since_close <= 60:
                    continue

                resp = client.request(
                    "GET",
                    f"/markets/{ticker}",
                )

                m = resp.get(
                    "market",
                    resp,
                )

                if not m.get("result"):
                    continue

                append_row(
                    RESULT_FILE,
                    RESULT_FIELDS,
                    {
                        "ticker":
                            ticker,

                        "close_time":
                            close_time,

                        "floor_strike":
                            m.get(
                                "floor_strike"
                            ),

                        "result":
                            m.get(
                                "result"
                            ),

                        "expiration_value":
                            m.get(
                                "expiration_value"
                            ),
                    },
                )

                del pending[ticker]

            # ==================================================
            # 4. 상태 표시
            # ==================================================

            vol_text = (
                f"{brti_volatility:.8f}"
                if brti_volatility
                is not None
                else "warming up"
            )

            change_text = (
                f"{brti_change:+.5%}"
                if brti_change
                is not None
                else "warming up"
            )

            print(
                f"{now.strftime('%H:%M:%S')} | "
                f"BRTI ${brti_price:,.2f} | "
                f"Δ {change_text} | "
                f"vol {vol_text} | "
                f"samples {brti_samples} | "
                f"markets {len(markets)}"
            )

        except KeyboardInterrupt:
            print("\n기록 종료")
            break

        except Exception as e:
            print(
                "오류:",
                repr(e),
            )

        # ======================================================
        # 정확히 2초 cadence에 가깝게 유지
        # ======================================================

        elapsed = (
            time.time()
            - loop_start
        )

        sleep_time = max(
            0,
            POLL_SECONDS - elapsed,
        )

        time.sleep(
            sleep_time
        )


if __name__ == "__main__":
    main()