import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from kalshi_client import KalshiClient
from kalshi_orders import get_executable_buy_quote, place_order
from btc_data import BRTITracker
from strategy import evaluate_market
from trade_logger import log_trade
from order_attempt_logger import log_order_attempt


# ============================================================
# CONFIG
# ============================================================

SERIES_TICKER = os.getenv("SERIES_TICKER", "KXBTC15M")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "2"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

ENTRY_SLIPPAGE_DOLLARS = float(os.getenv("ENTRY_SLIPPAGE_DOLLARS", "0.03"))

# Strategy is still decided once at T-6. These settings control execution only.
ENTRY_EXECUTION_WINDOW_SECONDS = float(
    os.getenv("ENTRY_EXECUTION_WINDOW_SECONDS", "10")
)
ENTRY_RETRY_INTERVAL_SECONDS = float(
    os.getenv("ENTRY_RETRY_INTERVAL_SECONDS", "2")
)
ENTRY_MAX_ATTEMPTS = int(
    os.getenv("ENTRY_MAX_ATTEMPTS", "5")
)

CATASTROPHE_STOP_DOLLARS = float(os.getenv("CATASTROPHE_STOP_DOLLARS", "0.50"))

EXIT_SLIPPAGE_DOLLARS = float(os.getenv("EXIT_SLIPPAGE_DOLLARS", "0.05"))
EXIT_RETRY_SLIPPAGE_STEPS = [
    float(x.strip())
    for x in os.getenv(
        "EXIT_RETRY_SLIPPAGE_STEPS",
        "0.05,0.10,0.20,0.35",
    ).split(",")
    if x.strip()
]

BALANCE_CHECK_BEFORE_ENTRY = (
    os.getenv("BALANCE_CHECK_BEFORE_ENTRY", "true").lower() == "true"
)
BALANCE_RESERVE_DOLLARS = float(os.getenv("BALANCE_RESERVE_DOLLARS", "0.50"))
BALANCE_FEE_BUFFER_PER_CONTRACT = float(
    os.getenv("BALANCE_FEE_BUFFER_PER_CONTRACT", "0.02")
)
MIN_ENTRY_COUNT = float(os.getenv("MIN_ENTRY_COUNT", "1"))
DEFAULT_EXCHANGE_INDEX = int(os.getenv("DEFAULT_EXCHANGE_INDEX", "2"))

POSITION_STATE_FILE = os.getenv(
    "POSITION_STATE_FILE",
    str(BASE_DIR / "active_position.json"),
)
POSITION_RECONCILE_SECONDS = float(
    os.getenv("POSITION_RECONCILE_SECONDS", "10")
)
POSITION_COUNT_EPSILON = 0.005


# ============================================================
# SMALL HELPERS
# ============================================================

def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def seconds_to_close(market):
    close_time = market.get("close_time")
    if not close_time:
        return 0.0
    try:
        close_dt = parse_time(close_time)
    except ValueError:
        return 0.0
    return (close_dt - datetime.now(timezone.utc)).total_seconds()


def sleep_to_cadence(started_at):
    elapsed = time.monotonic() - started_at
    time.sleep(max(0.0, POLL_SECONDS - elapsed))


def get_orderbook(client, ticker):
    return client.request("GET", f"/markets/{ticker}/orderbook")


def find_current_market(client):
    response = client.request(
        "GET",
        "/markets",
        params={
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": 100,
        },
    )
    markets = response.get("markets", [])
    now = datetime.now(timezone.utc)
    valid = []
    for market in markets:
        close_raw = market.get("close_time")
        if not close_raw:
            continue
        try:
            close_dt = parse_time(close_raw)
        except ValueError:
            continue
        if close_dt > now:
            valid.append(market)
    if not valid:
        return None
    valid.sort(key=lambda item: item.get("close_time", "9999"))
    return valid[0]


def _book(orderbook):
    if not isinstance(orderbook, dict):
        return {}
    return (
        orderbook.get("orderbook_fp")
        or orderbook.get("orderbook")
        or {}
    )


def get_executable_sell_quote(orderbook, outcome, count):
    """Executable outcome bid for selling the full position."""
    outcome = str(outcome).lower()
    book = _book(orderbook)
    key = "yes_dollars" if outcome == "yes" else "no_dollars"
    fallback = "yes" if outcome == "yes" else "no"
    raw_levels = book.get(key) or book.get(fallback) or []

    levels = []
    for level in raw_levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = to_float(level[0])
        size = to_float(level[1])
        if price is None or size is None or size <= 0:
            continue
        if not (0.0 < price < 1.0):
            continue
        levels.append((price, size))

    levels.sort(key=lambda item: item[0], reverse=True)
    best_bid = levels[0][0] if levels else None
    visible = 0.0
    full_fill_price = None
    for price, size in levels:
        visible += size
        if visible >= float(count) - 1e-9:
            full_fill_price = price
            break

    return {
        "best_bid": best_bid,
        "full_fill_price": full_fill_price,
        "visible_count": visible,
    }


def get_fallback_market_bid(market, outcome):
    key = "yes_bid_dollars" if outcome == "yes" else "no_bid_dollars"
    return to_float(market.get(key))


# ============================================================
# LOCAL POSITION STATE
# ============================================================

def save_active_position_state(position):
    if DRY_RUN or position is None:
        return
    path = Path(POSITION_STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(position, file, indent=2, sort_keys=True)
    temp_path.replace(path)


def clear_active_position_state():
    if DRY_RUN:
        return
    try:
        Path(POSITION_STATE_FILE).unlink()
    except FileNotFoundError:
        pass


def load_active_position_state():
    if DRY_RUN:
        return None
    path = Path(POSITION_STATE_FILE)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            position = json.load(file)
    except (OSError, ValueError, TypeError) as exc:
        print(f"POSITION STATE WARNING | {type(exc).__name__}: {exc}")
        return None

    required = ("ticker", "outcome", "count", "entry_price")
    if not all(key in position for key in required):
        print("POSITION STATE WARNING | incomplete state ignored")
        return None
    return position


# ============================================================
# LIVE POSITION SAFETY
# ============================================================

def get_live_market_position(client, ticker):
    response = client.request(
        "GET",
        "/portfolio/positions",
        params={"ticker": ticker, "limit": 100},
    )
    for item in response.get("market_positions", []):
        if item.get("ticker") != ticker:
            continue
        signed = to_float(item.get("position_fp"))
        if signed is None:
            signed = to_float(item.get("position"))
        signed = signed or 0.0
        if abs(signed) <= POSITION_COUNT_EPSILON:
            return None, 0.0
        if signed > 0:
            return "yes", signed
        return "no", abs(signed)
    return None, 0.0


def reconcile_position(client, active_position):
    if DRY_RUN or active_position is None:
        return active_position, "ok"

    ticker = active_position["ticker"]
    try:
        live_side, live_count = get_live_market_position(client, ticker)
    except Exception as exc:
        print(
            "POSITION RECONCILIATION ERROR | "
            f"{ticker} | {type(exc).__name__}: {exc}"
        )
        return active_position, "error"

    if live_side is None:
        # Do not erase a position here. It may have just settled. The market
        # transition handler will fetch the actual result and log settlement.
        return active_position, "possibly_settled"

    tracked_side = str(active_position["outcome"]).lower()
    tracked_count = float(active_position["count"])

    if live_side != tracked_side:
        print(
            "POSITION RECONCILIATION MISMATCH | "
            f"tracked={tracked_side} x{tracked_count:.2f} | "
            f"live={live_side} x{live_count:.2f}"
        )
        return active_position, "mismatch"

    if live_count < tracked_count - POSITION_COUNT_EPSILON:
        old_count = tracked_count
        active_position["count"] = live_count
        if old_count > 0:
            active_position["entry_fee"] = (
                float(active_position.get("entry_fee", 0.0) or 0.0)
                * live_count
                / old_count
            )
        save_active_position_state(active_position)
        print(
            "POSITION RECONCILIATION | "
            f"size {old_count:.2f} -> {live_count:.2f}"
        )
        return active_position, "ok"

    if live_count > tracked_count + POSITION_COUNT_EPSILON:
        print(
            "POSITION RECONCILIATION MISMATCH | "
            f"tracked={tracked_count:.2f} live={live_count:.2f}"
        )
        return active_position, "mismatch"

    return active_position, "ok"


# ============================================================
# BALANCE
# ============================================================

def get_balance_snapshot(client):
    response = client.request("GET", "/portfolio/balance")
    total = to_float(response.get("balance_dollars"))
    if total is None:
        cents = to_float(response.get("balance"))
        total = cents / 100.0 if cents is not None else None

    exchange_balances = {}
    for item in response.get("balance_breakdown", []):
        try:
            index = int(item.get("exchange_index"))
        except (TypeError, ValueError):
            continue
        value = to_float(item.get("balance"))
        if value is not None:
            exchange_balances[index] = value
    return total, exchange_balances


def get_market_exchange_index(market):
    try:
        return int(market.get("exchange_index"))
    except (TypeError, ValueError):
        return DEFAULT_EXCHANGE_INDEX


def affordable_count(client, market, requested_count, price):
    if not BALANCE_CHECK_BEFORE_ENTRY or DRY_RUN:
        return float(requested_count)

    try:
        total, exchange_balances = get_balance_snapshot(client)
    except Exception as exc:
        print(f"ENTRY BLOCKED | balance check failed | {type(exc).__name__}: {exc}")
        return 0.0

    exchange_index = get_market_exchange_index(market)
    balance = exchange_balances.get(exchange_index)
    if balance is None:
        print(
            "ENTRY BLOCKED | no exchange balance | "
            f"index={exchange_index} total={total}"
        )
        return 0.0

    spendable = max(0.0, balance - BALANCE_RESERVE_DOLLARS)
    unit_cost = float(price) + BALANCE_FEE_BUFFER_PER_CONTRACT
    if unit_cost <= 0:
        return 0.0
    max_count = int(spendable // unit_cost)
    return float(min(float(requested_count), max_count))


# ============================================================
# LOGGING HELPER
# ============================================================

def log_attempt(signal, ticker, status, remaining, brti, target, **extra):
    row = {
        "ticker": ticker,
        "strategy": signal.get("strategy_type", "HIGH_CONFIDENCE_6M"),
        "outcome": signal.get("outcome", ""),
        "count": signal.get("count", ""),
        "signal_price": signal.get("price", ""),
        "requested_limit_price": extra.get("requested_limit_price", ""),
        "snapped_limit_price": extra.get("snapped_limit_price", ""),
        "model_probability": signal.get("model_probability", ""),
        "market_probability": signal.get("market_probability", ""),
        "edge": "",
        "seconds_remaining": remaining,
        "brti_price": brti,
        "target_price": target,
        "distance_dollars": (
            brti - target
            if brti is not None and target is not None
            else ""
        ),
        "status": status,
        "fill_count": extra.get("fill_count", 0),
        "actual_fill_price": extra.get("actual_fill_price", ""),
        "error_type": extra.get("error_type", ""),
        "error_message": extra.get("error_message", ""),
    }
    log_order_attempt(row)


# ============================================================
# ENTRY
# ============================================================

def execute_entry(
    client,
    ticker,
    signal,
    market,
    orderbook,
    remaining,
    current_brti,
    target,
):
    """
    Try one IOC entry for a frozen T-6 signal.

    The strategy decision and signal price are NOT recalculated here.
    We only use the current orderbook to determine whether the frozen
    signal can still be executed inside its original slippage cap.

    Returns:
        (position, status)

    Retriable statuses are intentionally limited to:
        BLOCKED_LIQUIDITY
        ZERO_FILL

    API_REJECTED / NO_RESPONSE are not automatically retried because
    execution state can be uncertain after a transport/API failure.
    """
    outcome = signal["outcome"]
    requested_count = float(signal["count"])
    signal_price = float(signal["price"])

    # This is the maximum price we accepted at the original T-6 decision.
    # Keep it frozen across retries so execution retries do not change alpha.
    max_price = min(
        0.99,
        signal_price + ENTRY_SLIPPAGE_DOLLARS,
    )

    quote = get_executable_buy_quote(
        orderbook,
        outcome,
        requested_count,
        max_outcome_price=max_price,
    )
    executable_price = to_float(
        quote.get("full_fill_price")
    )

    if executable_price is None:
        print(
            "ENTRY SKIP | insufficient visible depth inside frozen slippage cap | "
            f"{outcome.upper()} signal=${signal_price:.4f} cap=${max_price:.4f}"
        )
        log_attempt(
            signal,
            ticker,
            "BLOCKED_LIQUIDITY",
            remaining,
            current_brti,
            target,
            requested_limit_price=max_price,
        )
        return None, "BLOCKED_LIQUIDITY"

    # Use the whole approved slippage cap as the IOC limit.
    # A limit is only the maximum payable price; Kalshi can fill us cheaper.
    # This avoids missing a fill simply because the book moved by one tick
    # between the quote snapshot and order arrival.
    order_price = max_price

    # Be conservative for the balance check: assume we may pay the limit.
    count = affordable_count(
        client,
        market,
        requested_count,
        order_price,
    )

    if count < MIN_ENTRY_COUNT:
        print(
            "ENTRY BLOCKED | insufficient exchange balance | "
            f"requested={requested_count:g} affordable={count:g}"
        )
        log_attempt(
            signal,
            ticker,
            "BLOCKED_INSUFFICIENT_BALANCE",
            remaining,
            current_brti,
            target,
            requested_limit_price=order_price,
        )
        return None, "BLOCKED_INSUFFICIENT_BALANCE"

    # If balance reduced the count, verify that this smaller size is still
    # executable inside the same frozen slippage cap.
    if count < requested_count:
        quote = get_executable_buy_quote(
            orderbook,
            outcome,
            count,
            max_outcome_price=max_price,
        )
        executable_price = to_float(
            quote.get("full_fill_price")
        )
        if executable_price is None:
            log_attempt(
                signal,
                ticker,
                "BLOCKED_LIQUIDITY",
                remaining,
                current_brti,
                target,
                requested_limit_price=max_price,
            )
            return None, "BLOCKED_LIQUIDITY"

    print(
        "ENTRY | "
        f"{outcome.upper()} x{count:g} | "
        f"confidence={signal.get('model_probability', 0):.1%} | "
        f"signal=${signal_price:.4f} | "
        f"book=${executable_price:.4f} | "
        f"IOC cap=${order_price:.4f}"
    )

    try:
        response = place_order(
            client=client,
            ticker=ticker,
            outcome=outcome,
            action="buy",
            count=count,
            price=order_price,
            time_in_force="immediate_or_cancel",
            reduce_only=False,
            price_ranges=market.get("price_ranges"),
        )
    except Exception as exc:
        log_attempt(
            signal,
            ticker,
            "API_REJECTED",
            remaining,
            current_brti,
            target,
            requested_limit_price=order_price,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        print(
            "ENTRY ERROR | not auto-retrying uncertain API state | "
            f"{type(exc).__name__}: {exc}"
        )
        return None, "API_REJECTED"

    if response is None:
        log_attempt(
            signal,
            ticker,
            "NO_RESPONSE",
            remaining,
            current_brti,
            target,
            requested_limit_price=order_price,
        )
        print(
            "ENTRY ERROR | no response; not auto-retrying uncertain execution state"
        )
        return None, "NO_RESPONSE"

    fill_count = to_float(
        response.get("fill_count")
    ) or 0.0
    fill_price = to_float(
        response.get("average_outcome_price")
    )
    snapped = to_float(
        response.get("snapped_outcome_price")
    )

    if fill_count <= 0:
        print(
            "ENTRY | zero fill; frozen signal remains eligible for retry"
        )
        log_attempt(
            signal,
            ticker,
            "ZERO_FILL",
            remaining,
            current_brti,
            target,
            requested_limit_price=order_price,
            snapped_limit_price=snapped,
        )
        return None, "ZERO_FILL"

    if fill_price is None:
        fill_price = (
            snapped
            if snapped is not None
            else order_price
        )

    avg_fee = to_float(
        response.get("average_fee_paid")
    ) or 0.0
    entry_fee = avg_fee * fill_count

    status = (
        "FILLED"
        if fill_count >= count - 1e-9
        else "PARTIAL_FILL"
    )

    log_attempt(
        signal,
        ticker,
        status,
        remaining,
        current_brti,
        target,
        requested_limit_price=order_price,
        snapped_limit_price=snapped,
        fill_count=fill_count,
        actual_fill_price=fill_price,
    )

    position = {
        "ticker": ticker,
        "outcome": outcome,
        "count": fill_count,
        "entry_price": fill_price,
        "entry_fee": entry_fee,
        "strategy_type": "HIGH_CONFIDENCE_6M",
        "entry_time": time.time(),
        "entry_confidence": signal.get(
            "model_probability"
        ),
        "market_probability": signal.get(
            "market_probability"
        ),
        "order_id": response.get("order_id"),
    }

    print(
        "ENTRY FILLED | "
        f"{outcome.upper()} x{fill_count:g} @ ${fill_price:.4f} | "
        f"fee=${entry_fee:.4f}"
    )
    return position, status


# ============================================================
# CATASTROPHE EXIT
# ============================================================

def execute_exit(client, active_position, exit_bid, reason, price_ranges=None):
    ticker = active_position["ticker"]
    outcome = active_position["outcome"]
    original_count = float(active_position["count"])
    entry_price = float(active_position["entry_price"])
    entry_fee = float(active_position.get("entry_fee", 0.0) or 0.0)

    remaining = original_count
    total_count = 0.0
    total_notional = 0.0
    total_exit_fee = 0.0
    steps = EXIT_RETRY_SLIPPAGE_STEPS or [EXIT_SLIPPAGE_DOLLARS]

    print(
        "CATASTROPHE EXIT | "
        f"{outcome.upper()} entry=${entry_price:.4f} bid=${exit_bid:.4f} | {reason}"
    )

    for slippage in steps:
        if remaining <= POSITION_COUNT_EPSILON:
            break
        limit_price = max(0.001, float(exit_bid) - float(slippage))
        try:
            response = place_order(
                client=client,
                ticker=ticker,
                outcome=outcome,
                action="sell",
                count=remaining,
                price=limit_price,
                time_in_force="immediate_or_cancel",
                reduce_only=True,
                price_ranges=price_ranges,
            )
        except Exception as exc:
            print(f"EXIT ERROR | {type(exc).__name__}: {exc}")
            continue

        if response is None:
            continue
        fill_count = to_float(response.get("fill_count")) or 0.0
        if fill_count <= 0:
            continue
        fill_price = to_float(response.get("average_outcome_price"))
        if fill_price is None:
            fill_price = to_float(response.get("snapped_outcome_price")) or limit_price
        avg_fee = to_float(response.get("average_fee_paid")) or 0.0

        total_count += fill_count
        total_notional += fill_price * fill_count
        total_exit_fee += avg_fee * fill_count
        remaining = max(0.0, remaining - fill_count)

    if remaining > POSITION_COUNT_EPSILON:
        active_position["count"] = remaining
        if original_count > 0:
            active_position["entry_fee"] = entry_fee * remaining / original_count
        save_active_position_state(active_position)
        print(f"EXIT INCOMPLETE | still holding x{remaining:.2f}")
        return {"closed": False, "remaining_count": remaining}

    exit_price = total_notional / total_count if total_count > 0 else float(exit_bid)
    gross = (exit_price - entry_price) * total_count
    fees = entry_fee + total_exit_fee
    net = gross - fees

    log_trade(
        {
            "event": "CLOSED",
            "ticker": ticker,
            "strategy": "HIGH_CONFIDENCE_6M",
            "outcome": outcome,
            "count": total_count,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "entry_fee": round(entry_fee, 6),
            "exit_fee": round(total_exit_fee, 6),
            "gross_pnl": round(gross, 6),
            "net_pnl": round(net, 6),
            "reason": reason,
        }
    )
    clear_active_position_state()
    print(f"EXIT CLOSED | net=${net:+.4f}")
    return {"closed": True, "net_pnl": net}


def manage_position(client, market, orderbook, active_position):
    outcome = active_position["outcome"]
    count = float(active_position["count"])
    entry_price = float(active_position["entry_price"])

    quote = get_executable_sell_quote(orderbook, outcome, count)
    exit_bid = to_float(quote.get("full_fill_price"))
    source = "ORDERBOOK"
    if exit_bid is None:
        exit_bid = get_fallback_market_bid(market, outcome)
        source = "MARKET_FALLBACK"

    if exit_bid is None:
        print("POSITION | no executable/fallback bid; holding")
        return None

    pnl_per_contract = exit_bid - entry_price
    print(
        "POSITION | "
        f"{outcome.upper()} | entry=${entry_price:.4f} | "
        f"bid=${exit_bid:.4f} | P/L={pnl_per_contract:+.4f} | mark={source}"
    )

    if pnl_per_contract <= -CATASTROPHE_STOP_DOLLARS:
        return execute_exit(
            client,
            active_position,
            exit_bid,
            reason=(
                f"CATASTROPHE_STOP {pnl_per_contract:+.4f} "
                f"<= -{CATASTROPHE_STOP_DOLLARS:.2f}"
            ),
            price_ranges=market.get("price_ranges"),
        )

    return None


# ============================================================
# SETTLEMENT
# ============================================================

def finalize_settlement(client, active_position):
    ticker = active_position["ticker"]
    try:
        response = client.request("GET", f"/markets/{ticker}")
    except Exception as exc:
        print(f"SETTLEMENT CHECK ERROR | {ticker} | {type(exc).__name__}: {exc}")
        return None

    market = response.get("market", response)
    result = str(market.get("result") or "").strip().lower()
    if result not in ("yes", "no"):
        return None

    outcome = str(active_position["outcome"]).lower()
    count = float(active_position["count"])
    entry_price = float(active_position["entry_price"])
    entry_fee = float(active_position.get("entry_fee", 0.0) or 0.0)
    final_price = 1.0 if result == outcome else 0.0
    gross = (final_price - entry_price) * count
    net = gross - entry_fee

    log_trade(
        {
            "event": "CLOSED",
            "ticker": ticker,
            "strategy": "HIGH_CONFIDENCE_6M",
            "outcome": outcome,
            "count": count,
            "entry_price": round(entry_price, 4),
            "exit_price": final_price,
            "entry_fee": round(entry_fee, 6),
            "exit_fee": 0.0,
            "gross_pnl": round(gross, 6),
            "net_pnl": round(net, 6),
            "reason": f"SETTLEMENT {result.upper()}",
        }
    )
    clear_active_position_state()
    print(
        "SETTLEMENT | "
        f"{ticker} | held {outcome.upper()} | result={result.upper()} | "
        f"net=${net:+.4f}"
    )
    return {"closed": True, "net_pnl": net, "result": result}


# ============================================================
# MAIN
# ============================================================

def run():
    client = KalshiClient()
    brti = BRTITracker(history_seconds=120)

    active_position = load_active_position_state()
    current_ticker = None

    # Markets whose T-6 decision is completely finished:
    # no signal, filled, non-retriable failure, or retry window expired.
    decided_markets = set()

    # A pending entry contains a FROZEN T-6 signal. The strategy is not
    # recalculated during retries; only execution is retried.
    pending_entries = {}

    last_reconcile = 0.0

    print("=" * 88)
    print("KALSHI BTC 15M - HIGH CONFIDENCE SETTLEMENT BOT")
    print(f"Series: {SERIES_TICKER}")
    print("Decision: one observation at T-6:00")
    print("Entry: corrected settlement confidence >= 80%")
    print(
        "Execution retry: "
        f"{ENTRY_EXECUTION_WINDOW_SECONDS:.0f}s window | "
        f"max {ENTRY_MAX_ATTEMPTS} attempts | "
        f"{ENTRY_RETRY_INTERVAL_SECONDS:.1f}s interval"
    )
    print(f"Catastrophe stop: -${CATASTROPHE_STOP_DOLLARS:.2f}/contract")
    print("Normal exit: settlement only")
    print(f"DRY_RUN: {DRY_RUN}")
    print("=" * 88)

    if active_position:
        print(
            "POSITION STATE LOADED | "
            f"{active_position['ticker']} | "
            f"{active_position['outcome'].upper()} x{active_position['count']} | "
            f"entry=${active_position['entry_price']}"
        )

    while True:
        loop_started = time.monotonic()
        try:
            market = find_current_market(client)
            if market is None:
                print("No open BTC 15m market")
                sleep_to_cadence(loop_started)
                continue

            ticker = market["ticker"]
            remaining = seconds_to_close(market)
            target = to_float(market.get("floor_strike"))

            # If the prior market was held to expiry, record the actual settlement
            # before doing anything in the new market.
            if active_position is not None and active_position["ticker"] != ticker:
                settled = finalize_settlement(client, active_position)
                if settled is None:
                    print(
                        "WAIT | previous position not resolved yet | "
                        f"{active_position['ticker']}"
                    )
                    sleep_to_cadence(loop_started)
                    continue
                active_position = None

            if ticker != current_ticker:
                current_ticker = ticker
                print("\n" + "=" * 88)
                print(
                    f"NEW MARKET | {ticker} | target=${target:,.2f} | "
                    f"close={market.get('close_time')}"
                )
                print("=" * 88)

            # Keep BRTI history warm for the 6-minute decision.
            current_brti = brti.get_current_price(client)
            recent_change = brti.get_price_change()
            volatility = brti.get_volatility_per_sqrt_second()
            history_samples = brti.sample_count()
            orderbook = get_orderbook(client, ticker)

            if (
                active_position is not None
                and active_position["ticker"] == ticker
                and not DRY_RUN
                and time.time() - last_reconcile >= POSITION_RECONCILE_SECONDS
            ):
                active_position, sync = reconcile_position(client, active_position)
                last_reconcile = time.time()
                if sync in ("mismatch", "error"):
                    sleep_to_cadence(loop_started)
                    continue

            # Position management is only the catastrophe stop. Otherwise hold.
            if active_position is not None and active_position["ticker"] == ticker:
                exit_result = manage_position(
                    client,
                    market,
                    orderbook,
                    active_position,
                )
                if exit_result and exit_result.get("closed"):
                    active_position = None
                elif exit_result and not exit_result.get("closed"):
                    save_active_position_state(active_position)

                sleep_to_cadence(loop_started)
                continue

            result = evaluate_market(
                market=market,
                orderbook=orderbook,
                seconds_remaining=remaining,
                current_btc_price=current_brti,
                recent_volatility=volatility,
                history_samples=history_samples,
                recent_change=recent_change,
            )

            # ============================================================
            # SINGLE T-6 STRATEGY DECISION
            # ============================================================
            #
            # Strategy selection happens exactly once.
            #
            # If T-6 produces no signal, the market is immediately complete.
            # If T-6 produces a signal, freeze that signal and give execution
            # a short retry window. Retrying does NOT recalculate confidence,
            # direction, signal price, or the slippage cap.
            # ============================================================

            if result.decision_ready:
                if ticker in decided_markets:
                    result.signal = None
                    result.reason = "DECISION COMPLETE | no re-entry"

                elif ticker in pending_entries:
                    result.signal = None
                    pending = pending_entries[ticker]
                    frozen_signal = pending["signal"]
                    result.reason = (
                        "ENTRY PENDING | "
                        f"frozen {frozen_signal['outcome'].upper()} "
                        f"{frozen_signal.get('model_probability', 0):.1%}"
                    )

                elif result.signal is None:
                    decided_markets.add(ticker)

                else:
                    # Freeze a copy of the T-6 signal before any order attempt.
                    pending_entries[ticker] = {
                        "signal": dict(result.signal),
                        "started_at": time.monotonic(),
                        "attempts": 0,
                        "next_attempt_at": 0.0,
                    }
                    result.reason = (
                        f"{result.reason} | EXECUTION ARMED"
                    )

            distance = (
                current_brti - target
                if current_brti is not None and target is not None
                else 0.0
            )
            print(
                f"{datetime.now().strftime('%H:%M:%S')} | "
                f"{remaining:.0f}s | BRTI ${current_brti:,.2f} | "
                f"target ${target:,.2f} | Δ ${distance:+.2f} | "
                f"{result.reason}"
            )

            # ============================================================
            # EXECUTION OF FROZEN T-6 SIGNAL
            # ============================================================

            pending = pending_entries.get(ticker)

            if pending is not None:
                elapsed = (
                    time.monotonic()
                    - float(pending["started_at"])
                )
                attempts = int(
                    pending["attempts"]
                )

                window_expired = (
                    elapsed
                    >= ENTRY_EXECUTION_WINDOW_SECONDS
                )
                attempts_exhausted = (
                    attempts
                    >= ENTRY_MAX_ATTEMPTS
                )

                if (
                    window_expired
                    or attempts_exhausted
                ):
                    frozen_signal = pending["signal"]
                    reason = (
                        "time window expired"
                        if window_expired
                        else "max attempts reached"
                    )
                    print(
                        "ENTRY EXPIRED | "
                        f"{ticker} | {reason} | "
                        f"attempts={attempts}"
                    )
                    log_attempt(
                        frozen_signal,
                        ticker,
                        "EXECUTION_WINDOW_EXPIRED",
                        remaining,
                        current_brti,
                        target,
                        requested_limit_price=(
                            min(
                                0.99,
                                float(
                                    frozen_signal[
                                        "price"
                                    ]
                                )
                                + ENTRY_SLIPPAGE_DOLLARS,
                            )
                        ),
                        error_message=reason,
                    )
                    pending_entries.pop(
                        ticker,
                        None,
                    )
                    decided_markets.add(
                        ticker
                    )
                    sleep_to_cadence(
                        loop_started
                    )
                    continue

                if (
                    time.monotonic()
                    < float(
                        pending["next_attempt_at"]
                    )
                ):
                    sleep_to_cadence(
                        loop_started
                    )
                    continue

                frozen_signal = pending[
                    "signal"
                ]

                # Do not interfere with a position created outside this process.
                if not DRY_RUN:
                    try:
                        live_side, live_count = (
                            get_live_market_position(
                                client,
                                ticker,
                            )
                        )
                    except Exception as exc:
                        print(
                            "ENTRY CHECK ERROR | "
                            "will retry while execution window remains | "
                            f"{type(exc).__name__}: {exc}"
                        )
                        pending[
                            "next_attempt_at"
                        ] = (
                            time.monotonic()
                            + ENTRY_RETRY_INTERVAL_SECONDS
                        )
                        sleep_to_cadence(
                            loop_started
                        )
                        continue

                    if live_side is not None:
                        print(
                            "ENTRY BLOCKED | existing live position | "
                            f"{live_side.upper()} x{live_count:.2f}"
                        )
                        log_attempt(
                            frozen_signal,
                            ticker,
                            "BLOCKED_EXISTING_POSITION",
                            remaining,
                            current_brti,
                            target,
                            error_message=(
                                f"{live_side.upper()} "
                                f"x{live_count:.2f}"
                            ),
                        )
                        pending_entries.pop(
                            ticker,
                            None,
                        )
                        decided_markets.add(
                            ticker
                        )
                        sleep_to_cadence(
                            loop_started
                        )
                        continue

                pending["attempts"] = (
                    attempts + 1
                )

                print(
                    "ENTRY ATTEMPT | "
                    f"{pending['attempts']}/"
                    f"{ENTRY_MAX_ATTEMPTS} | "
                    f"elapsed={elapsed:.1f}s"
                )

                position, entry_status = (
                    execute_entry(
                        client=client,
                        ticker=ticker,
                        signal=frozen_signal,
                        market=market,
                        orderbook=orderbook,
                        remaining=remaining,
                        current_brti=current_brti,
                        target=target,
                    )
                )

                if position is not None:
                    active_position = position
                    save_active_position_state(
                        active_position
                    )
                    pending_entries.pop(
                        ticker,
                        None,
                    )
                    decided_markets.add(
                        ticker
                    )

                elif entry_status in (
                    "BLOCKED_LIQUIDITY",
                    "ZERO_FILL",
                ):
                    # Confirmed safe-to-retry states:
                    # no order was sent, or IOC explicitly filled zero.
                    pending[
                        "next_attempt_at"
                    ] = (
                        time.monotonic()
                        + ENTRY_RETRY_INTERVAL_SECONDS
                    )
                    print(
                        "ENTRY RETRY ARMED | "
                        f"status={entry_status} | "
                        f"next in "
                        f"{ENTRY_RETRY_INTERVAL_SECONDS:.1f}s"
                    )

                else:
                    # Balance failures and uncertain API/transport outcomes
                    # are deliberately not retried automatically.
                    print(
                        "ENTRY COMPLETE WITHOUT FILL | "
                        f"status={entry_status} | "
                        "no automatic retry"
                    )
                    pending_entries.pop(
                        ticker,
                        None,
                    )
                    decided_markets.add(
                        ticker
                    )

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}")

        sleep_to_cadence(loop_started)


if __name__ == "__main__":
    run()
