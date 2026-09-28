import csv
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import strategy


DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_FILE = DATA_DIR / "snapshots_2s.csv"
RESULT_FILE = DATA_DIR / "results_2s.csv"
TRADE_OUTPUT = OUTPUT_DIR / "high_confidence_6m_trades.csv"

UTC = ZoneInfo("UTC")
TRADE_TIMEZONE = ZoneInfo("America/Chicago")

VALIDATION_SPLIT_DATE = os.getenv(
    "BACKTEST_VALIDATION_SPLIT_DATE",
    "2026-09-22T21:06:29-05:00",
)
FORWARD_TEST_START_DATE = os.getenv(
    "FORWARD_TEST_START_DATE",
    "2026-09-23T16:38:00-05:00",
)

TAKER_FEE_RATE = 0.07
BACKTEST_TRADE_COUNT = float(os.getenv("BACKTEST_TRADE_COUNT", "1"))
ENTRY_SLIPPAGE_DOLLARS = float(os.getenv("ENTRY_SLIPPAGE_DOLLARS", "0.03"))
CATASTROPHE_STOP_DOLLARS = float(os.getenv("CATASTROPHE_STOP_DOLLARS", "0.50"))
EXIT_ADVERSE_SLIPPAGE_DOLLARS = float(
    os.getenv("BACKTEST_EXIT_ADVERSE_SLIPPAGE_DOLLARS", "0.00")
)
STARTING_CAPITAL = float(os.getenv("BACKTEST_STARTING_CAPITAL", "40"))


def safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x):
            return default
        return x
    except (TypeError, ValueError):
        return default


def parse_time(value):
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_local_cut(value):
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TRADE_TIMEZONE)
    return dt.astimezone(UTC)


def taker_fee(count, price):
    if count <= 0 or price is None or not (0.0 < price < 1.0):
        return 0.0
    return round(TAKER_FEE_RATE * count * price * (1.0 - price), 4)


def load_snapshots(path):
    snapshots = defaultdict(list)
    with open(path, "r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            ticker = row.get("ticker")
            ts = parse_time(row.get("ts_utc"))
            if not ticker or ts is None:
                continue
            snapshots[ticker].append(
                {
                    "ts": ts,
                    "secs_to_close": safe_float(row.get("secs_to_close")),
                    "floor_strike": safe_float(row.get("floor_strike")),
                    "brti_price": safe_float(row.get("brti_price")),
                    "brti_price_change": safe_float(row.get("brti_price_change")),
                    "brti_volatility_per_sqrt_second": safe_float(
                        row.get("brti_volatility_per_sqrt_second")
                    ),
                    "brti_sample_count": safe_float(row.get("brti_sample_count"), 0),
                    "yes_bid": safe_float(row.get("yes_bid")),
                    "yes_ask": safe_float(row.get("yes_ask")),
                    "no_bid": safe_float(row.get("no_bid")),
                    "no_ask": safe_float(row.get("no_ask")),
                    "yes_bid_size": safe_float(row.get("yes_bid_size")),
                    "yes_ask_size": safe_float(row.get("yes_ask_size")),
                    "book_yes_best_ask": safe_float(row.get("book_yes_best_ask")),
                    "book_yes_exec_price": safe_float(row.get("book_yes_exec_price")),
                    "book_yes_visible_count": safe_float(row.get("book_yes_visible_count")),
                    "book_no_best_ask": safe_float(row.get("book_no_best_ask")),
                    "book_no_exec_price": safe_float(row.get("book_no_exec_price")),
                    "book_no_visible_count": safe_float(row.get("book_no_visible_count")),
                    "book_trade_count": safe_float(row.get("book_trade_count")),
                }
            )
    for ticker in snapshots:
        snapshots[ticker].sort(key=lambda item: item["ts"])
    return snapshots


def load_results(path):
    results = {}
    with open(path, "r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        for row in reader:
            ticker = row.get("ticker")
            result = str(row.get("result") or "").strip().lower()
            if ticker and result in ("yes", "no"):
                results[ticker] = result
    return results


def snapshot_orderbook(snapshot):
    """Rebuild only the best bid levels needed by strategy.py."""
    yes_bid = snapshot.get("yes_bid")
    no_bid = snapshot.get("no_bid")

    # Prefer the recorder's fresh orderbook when available.
    no_best_ask = snapshot.get("book_no_best_ask")
    yes_best_ask = snapshot.get("book_yes_best_ask")
    if no_best_ask is not None:
        yes_bid = 1.0 - no_best_ask
    if yes_best_ask is not None:
        no_bid = 1.0 - yes_best_ask

    yes_levels = [[round(yes_bid, 4), 1000000.0]] if yes_bid is not None else []
    no_levels = [[round(no_bid, 4), 1000000.0]] if no_bid is not None else []
    return {
        "orderbook_fp": {
            "yes_dollars": yes_levels,
            "no_dollars": no_levels,
        }
    }


def snapshot_market(snapshot):
    return {
        "floor_strike": snapshot.get("floor_strike"),
        "yes_bid_dollars": snapshot.get("yes_bid"),
        "yes_ask_dollars": snapshot.get("yes_ask"),
        "no_bid_dollars": snapshot.get("no_bid"),
        "no_ask_dollars": snapshot.get("no_ask"),
    }


def select_decision_snapshot(ticker_snapshots):
    # Same semantics as live: first recorded poll that enters the decision window.
    for snapshot in ticker_snapshots:
        if strategy.is_decision_time(snapshot.get("secs_to_close")):
            return snapshot
    return None


def executable_entry_price(snapshot, outcome, count, signal_price):
    cap = min(0.999, float(signal_price) + ENTRY_SLIPPAGE_DOLLARS)

    if outcome == "yes":
        fresh_exec = snapshot.get("book_yes_exec_price")
        fresh_count = snapshot.get("book_trade_count")
        fresh_best = snapshot.get("book_yes_best_ask")
        legacy_ask = snapshot.get("yes_ask")
        legacy_size = snapshot.get("yes_ask_size")
    else:
        fresh_exec = snapshot.get("book_no_exec_price")
        fresh_count = snapshot.get("book_trade_count")
        fresh_best = snapshot.get("book_no_best_ask")
        legacy_ask = snapshot.get("no_ask")
        # NO ask liquidity corresponds to YES bid liquidity in old schema.
        legacy_size = None

    # Recorder's full-fill price was usually recorded for a larger size (e.g. x6).
    # If so it is conservative for a x1 backtest and proves sufficient depth.
    if (
        fresh_exec is not None
        and fresh_count is not None
        and fresh_count >= count - 1e-9
        and fresh_exec <= cap + 1e-9
    ):
        return fresh_exec, "BOOK_FULL_FILL_CONSERVATIVE"

    if fresh_best is not None and fresh_best <= cap + 1e-9:
        return fresh_best, "BOOK_BEST_ASK"

    if legacy_ask is not None and legacy_ask <= cap + 1e-9:
        if outcome == "yes" and legacy_size is not None and legacy_size < count:
            return None, None
        return legacy_ask, "MARKET_ASK_FALLBACK"

    return None, None


def executable_exit_bid(snapshot, outcome):
    # Fresh orderbook best bid is the complement of the opposite side's best ask.
    if outcome == "yes":
        opposite_best_ask = snapshot.get("book_no_best_ask")
        if opposite_best_ask is not None:
            return 1.0 - opposite_best_ask, "BOOK"
        return snapshot.get("yes_bid"), "MARKET"

    opposite_best_ask = snapshot.get("book_yes_best_ask")
    if opposite_best_ask is not None:
        return 1.0 - opposite_best_ask, "BOOK"
    return snapshot.get("no_bid"), "MARKET"


def build_entries(snapshots, results):
    entries = []
    predictions = []

    for ticker, ticker_snapshots in snapshots.items():
        settlement = results.get(ticker)
        if settlement is None:
            continue

        decision = select_decision_snapshot(ticker_snapshots)
        if decision is None:
            continue

        result = strategy.evaluate_market(
            market=snapshot_market(decision),
            orderbook=snapshot_orderbook(decision),
            seconds_remaining=decision.get("secs_to_close"),
            current_btc_price=decision.get("brti_price"),
            recent_volatility=decision.get("brti_volatility_per_sqrt_second"),
            history_samples=decision.get("brti_sample_count"),
            recent_change=decision.get("brti_price_change"),
        )

        if result.corrected_yes_probability is not None:
            y = 1.0 if settlement == "yes" else 0.0
            predictions.append(
                {
                    "ticker": ticker,
                    "ts": decision["ts"],
                    "y": y,
                    "market_yes": result.market_yes_probability,
                    "corrected_yes": result.corrected_yes_probability,
                    "confidence": result.chosen_confidence,
                }
            )

        signal = result.signal
        if not isinstance(signal, dict):
            continue

        outcome = signal["outcome"]
        price, source = executable_entry_price(
            decision,
            outcome,
            BACKTEST_TRADE_COUNT,
            signal["price"],
        )
        if price is None:
            continue

        entries.append(
            {
                "ticker": ticker,
                "entry_ts": decision["ts"],
                "entry_secs_to_close": decision.get("secs_to_close"),
                "outcome": outcome,
                "count": BACKTEST_TRADE_COUNT,
                "entry_price": price,
                "entry_fee": taker_fee(BACKTEST_TRADE_COUNT, price),
                "confidence": signal.get("model_probability"),
                "market_probability": signal.get("market_probability"),
                "entry_source": source,
                "settlement": settlement,
            }
        )

    entries.sort(key=lambda row: row["entry_ts"])
    predictions.sort(key=lambda row: row["ts"])
    return entries, predictions


def simulate_trade(entry, ticker_snapshots, use_stop):
    outcome = entry["outcome"]
    count = entry["count"]
    entry_price = entry["entry_price"]
    entry_fee = entry["entry_fee"]
    settlement = entry["settlement"]

    relevant = [s for s in ticker_snapshots if s["ts"] >= entry["entry_ts"]]
    stop_triggered = False
    stop_false_positive = False
    exit_price = None
    exit_fee = 0.0
    exit_ts = relevant[-1]["ts"] if relevant else entry["entry_ts"]
    exit_reason = "SETTLEMENT"
    mae = 0.0

    if use_stop:
        for snapshot in relevant:
            bid, source = executable_exit_bid(snapshot, outcome)
            if bid is None:
                continue
            delta = bid - entry_price
            mae = min(mae, delta)
            if delta <= -CATASTROPHE_STOP_DOLLARS:
                stop_triggered = True
                stop_false_positive = settlement == outcome
                exit_price = max(
                    0.001,
                    bid - EXIT_ADVERSE_SLIPPAGE_DOLLARS,
                )
                exit_fee = taker_fee(count, exit_price)
                exit_ts = snapshot["ts"]
                exit_reason = f"CATASTROPHE_STOP_{source}"
                break

    if exit_price is None:
        exit_price = 1.0 if settlement == outcome else 0.0
        exit_fee = 0.0

    gross = (exit_price - entry_price) * count
    net = gross - entry_fee - exit_fee
    return {
        **entry,
        "exit_ts": exit_ts,
        "exit_price": exit_price,
        "exit_fee": exit_fee,
        "gross_pnl": gross,
        "net_pnl": net,
        "exit_reason": exit_reason,
        "direction_correct": int(settlement == outcome),
        "stop_triggered": int(stop_triggered),
        "false_stop": int(stop_false_positive),
        "mae_per_contract": mae,
    }


def simulate(entries, snapshots, use_stop):
    rows = []
    for entry in entries:
        rows.append(
            simulate_trade(
                entry,
                snapshots.get(entry["ticker"], []),
                use_stop=use_stop,
            )
        )
    rows.sort(key=lambda row: row["entry_ts"])
    return rows


def subset(rows, start=None, end=None):
    selected = []
    for row in rows:
        ts = row["entry_ts"]
        if start is not None and ts < start:
            continue
        if end is not None and ts >= end:
            continue
        selected.append(row)
    return selected


def summarize(rows):
    if not rows:
        return {
            "n": 0,
            "wr": 0.0,
            "settlement_accuracy": 0.0,
            "net": 0.0,
            "pf": 0.0,
            "mdd": 0.0,
            "stops": 0,
            "false_stops": 0,
        }

    wins = [r for r in rows if r["net_pnl"] > 0]
    losses = [r for r in rows if r["net_pnl"] < 0]
    pos = sum(r["net_pnl"] for r in wins)
    neg = abs(sum(r["net_pnl"] for r in losses))
    pf = pos / neg if neg > 0 else math.inf

    equity = STARTING_CAPITAL
    peak = equity
    mdd = 0.0
    for row in rows:
        equity += row["net_pnl"]
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)

    return {
        "n": len(rows),
        "wr": len(wins) / len(rows),
        "settlement_accuracy": sum(r["direction_correct"] for r in rows) / len(rows),
        "net": sum(r["net_pnl"] for r in rows),
        "pf": pf,
        "mdd": mdd,
        "stops": sum(r["stop_triggered"] for r in rows),
        "false_stops": sum(r["false_stop"] for r in rows),
    }


def prediction_scores(rows):
    if not rows:
        return None
    market_brier = sum((r["market_yes"] - r["y"]) ** 2 for r in rows) / len(rows)
    corr_brier = sum((r["corrected_yes"] - r["y"]) ** 2 for r in rows) / len(rows)

    def logloss(key):
        values = []
        for row in rows:
            p = min(0.999999, max(0.000001, row[key]))
            y = row["y"]
            values.append(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)))
        return sum(values) / len(values)

    return {
        "n": len(rows),
        "market_brier": market_brier,
        "corrected_brier": corr_brier,
        "market_logloss": logloss("market_yes"),
        "corrected_logloss": logloss("corrected_yes"),
    }


def save_rows(path, rows):
    if not rows:
        return
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(label, rows):
    s = summarize(rows)
    pf = "inf" if math.isinf(s["pf"]) else f"{s['pf']:.2f}"
    print(
        f"{label:<12} | N={s['n']:>3} | WR={s['wr']:.1%} | "
        f"settle={s['settlement_accuracy']:.1%} | Net=${s['net']:+.4f} | "
        f"PF={pf} | MDD=${s['mdd']:.4f} | "
        f"stops={s['stops']} false={s['false_stops']}"
    )


def main():
    print("=" * 100)
    print("BTC 15M - HIGH CONFIDENCE 6M SETTLEMENT BACKTEST")
    print("=" * 100)
    print(f"Decision: T-{strategy.DECISION_SECONDS / 60:.0f}m")
    print(f"Confidence: >= {strategy.MIN_SETTLEMENT_CONFIDENCE:.0%}")
    print(f"Correction beta: {strategy.CORRECTION_BETA:.3f}")
    print(f"Catastrophe stop: -${CATASTROPHE_STOP_DOLLARS:.2f}")
    print(f"Trade count: {BACKTEST_TRADE_COUNT:g}")

    snapshots = load_snapshots(SNAPSHOT_FILE)
    results = load_results(RESULT_FILE)
    entries, predictions = build_entries(snapshots, results)

    print(f"Snapshots: {sum(len(v) for v in snapshots.values()):,}")
    print(f"Markets: {len(snapshots):,} | settled={len(results):,}")
    print(f"Executable high-confidence entries: {len(entries)}")

    if entries:
        span = (entries[-1]["entry_ts"] - entries[0]["entry_ts"]).total_seconds()
        trades_per_day = len(entries) * 86400 / span if span > 0 else 0.0
        print(f"Approx trades/24h: {trades_per_day:.1f}")

    validation = parse_local_cut(VALIDATION_SPLIT_DATE)
    forward = parse_local_cut(FORWARD_TEST_START_DATE)

    hold = simulate(entries, snapshots, use_stop=False)
    stopped = simulate(entries, snapshots, use_stop=True)

    print("\nSETTLEMENT HOLD BENCHMARK")
    print_summary("Research", subset(hold, end=validation))
    print_summary("Validation", subset(hold, start=validation, end=forward))
    print_summary("Forward", subset(hold, start=forward))
    print_summary("All", hold)

    print("\nSELECTED STRATEGY: 50c CATASTROPHE STOP + OTHERWISE SETTLEMENT")
    print_summary("Research", subset(stopped, end=validation))
    print_summary("Validation", subset(stopped, start=validation, end=forward))
    print_summary("Forward", subset(stopped, start=forward))
    print_summary("All", stopped)

    score = prediction_scores(predictions)
    if score:
        print("\nPREDICTION BENCHMARK AT 6 MINUTES")
        print(f"N: {score['n']}")
        print(f"Kalshi Brier:     {score['market_brier']:.5f}")
        print(f"Corrected Brier:  {score['corrected_brier']:.5f}")
        print(f"Kalshi LogLoss:   {score['market_logloss']:.5f}")
        print(f"Corrected LogLoss:{score['corrected_logloss']:.5f}")

    save_rows(TRADE_OUTPUT, stopped)
    print(f"\nSaved: {TRADE_OUTPUT}")


if __name__ == "__main__":
    main()
