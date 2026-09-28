import math
import os
import uuid

from dotenv import load_dotenv


load_dotenv()


# ============================================================
# CONFIG
# ============================================================

DRY_RUN = (
    os.getenv(
        "DRY_RUN",
        "true",
    ).lower()
    != "false"
)

MAX_COUNT_PER_ORDER = float(
    os.getenv(
        "MAX_COUNT_PER_ORDER",
        "1",
    )
)

MAX_ORDER_COST_DOLLARS = float(
    os.getenv(
        "MAX_ORDER_COST_DOLLARS",
        "1.00",
    )
)


# KXBTC15M currently routes to exchange_index=2.
# Keep this configurable in .env rather than hard-coding -1 auto-routing.
EXCHANGE_INDEX = int(
    os.getenv(
        "EXCHANGE_INDEX",
        "2",
    )
)


# ============================================================
# HELPERS
# ============================================================

def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_price(price):
    return max(
        0.001,
        min(
            0.999,
            float(price),
        ),
    )


def _snap_v2_price_to_valid_tick(
    price,
    price_ranges,
    side,
):
    """
    Snap a YES-book V2 price to a valid market tick using market.price_ranges.

    Important:
    - bid orders snap DOWN so the bot never pays beyond its intended max.
    - ask orders snap UP for the same reason after NO/YES complement conversion.

    This is a local arithmetic operation only. No API request is made here.
    """

    price = _clamp_price(price)

    if not price_ranges:
        return round(price, 4)

    side = side.lower()

    if side not in ("bid", "ask"):
        raise ValueError(
            f"Invalid V2 book side: {side}"
        )

    epsilon = 1e-10

    for price_range in price_ranges:
        start = _to_float(
            price_range.get(
                "start"
            )
        )

        end = _to_float(
            price_range.get(
                "end"
            )
        )

        step = _to_float(
            price_range.get(
                "step"
            )
        )

        if (
            start is None
            or end is None
            or step is None
            or step <= 0
        ):
            continue

        if (
            start - epsilon
            <= price
            <= end + epsilon
        ):
            relative = (
                price
                - start
            ) / step

            if side == "bid":
                tick_number = math.floor(
                    relative
                    + epsilon
                )
            else:
                tick_number = math.ceil(
                    relative
                    - epsilon
                )

            snapped = (
                start
                + tick_number * step
            )

            snapped = max(
                start,
                min(
                    end,
                    snapped,
                ),
            )

            return round(
                _clamp_price(
                    snapped
                ),
                4,
            )

    # If metadata is missing/malformed for the relevant range,
    # preserve the requested price rather than guessing a tick.
    return round(
        price,
        4,
    )


# ============================================================
# OUTCOME PRICE <-> V2 BOOK PRICE
# ============================================================

def outcome_to_v2(
    outcome,
    action,
    outcome_price,
):
    """
    Bot internals always use the held outcome's contract price.

    Examples:
        BUY YES @ 0.70
        BUY NO  @ 0.30

    Kalshi V2 single-book representation:

        buy YES  -> bid @ YES price
        sell YES -> ask @ YES price

        buy NO   -> ask @ (1 - NO price)
        sell NO  -> bid @ (1 - NO price)
    """

    outcome = outcome.lower()
    action = action.lower()

    price = _clamp_price(
        outcome_price
    )

    if outcome not in (
        "yes",
        "no",
    ):
        raise ValueError(
            f"Invalid outcome: {outcome}"
        )

    if action not in (
        "buy",
        "sell",
    ):
        raise ValueError(
            f"Invalid action: {action}"
        )

    if outcome == "yes":

        if action == "buy":
            return "bid", price

        return "ask", price

    complementary_price = (
        1.0
        - price
    )

    if action == "buy":
        return (
            "ask",
            complementary_price,
        )

    return (
        "bid",
        complementary_price,
    )


def v2_fill_to_outcome_price(
    outcome,
    v2_fill_price,
):
    """
    Convert YES-book average_fill_price back to the held outcome price.
    """

    fill_price = _to_float(
        v2_fill_price
    )

    if fill_price is None:
        return None

    if outcome.lower() == "yes":
        return fill_price

    if outcome.lower() == "no":
        return (
            1.0
            - fill_price
        )

    raise ValueError(
        f"Invalid outcome: {outcome}"
    )




# ============================================================
# LIVE ORDERBOOK -> EXECUTABLE BUY QUOTE
# ============================================================

def get_executable_buy_quote(
    orderbook,
    outcome,
    count,
    max_outcome_price=None,
):
    """
    Calculate the outcome price required to buy `count` contracts from
    the CURRENT visible Kalshi orderbook.

    Kalshi's orderbook exposes bids on YES and NO, not asks.

      BUY YES liquidity comes from NO bids:
          YES ask = 1 - NO bid

      BUY NO liquidity comes from YES bids:
          NO ask = 1 - YES bid

    Returns:
        {
            "best_ask": float | None,
            "full_fill_price": float | None,
            "visible_count": float,
            "required_count": float,
            "levels": [(ask_price, count), ...],
        }

    `full_fill_price` is None when the visible book does not contain
    enough quantity within max_outcome_price to fill the requested size.
    """

    outcome = str(outcome).lower()

    if outcome not in ("yes", "no"):
        raise ValueError(
            f"Invalid outcome: {outcome}"
        )

    required_count = float(count)

    if required_count <= 0:
        raise ValueError(
            "count must be greater than 0"
        )

    if max_outcome_price is not None:
        max_outcome_price = float(
            max_outcome_price
        )

    if not isinstance(
        orderbook,
        dict,
    ):
        return {
            "best_ask": None,
            "full_fill_price": None,
            "visible_count": 0.0,
            "required_count": required_count,
            "levels": [],
        }

    book = (
        orderbook.get("orderbook_fp")
        or orderbook.get("orderbook")
        or {}
    )

    if outcome == "yes":
        raw_levels = (
            book.get("no_dollars")
            or book.get("no")
            or []
        )
    else:
        raw_levels = (
            book.get("yes_dollars")
            or book.get("yes")
            or []
        )

    levels = []

    for level in raw_levels:
        if (
            not isinstance(level, (list, tuple))
            or len(level) < 2
        ):
            continue

        bid_price = _to_float(level[0])
        level_count = _to_float(level[1])

        if (
            bid_price is None
            or level_count is None
            or level_count <= 0
        ):
            continue

        ask_price = round(
            1.0 - bid_price,
            4,
        )

        if not (
            0.0 < ask_price < 1.0
        ):
            continue

        levels.append(
            (
                ask_price,
                float(level_count),
            )
        )

    # Lowest ask is best for a buyer.
    levels.sort(
        key=lambda item: item[0]
    )

    best_ask = (
        levels[0][0]
        if levels
        else None
    )

    visible_count = 0.0
    full_fill_price = None
    used_levels = []

    for ask_price, level_count in levels:
        if (
            max_outcome_price is not None
            and ask_price
            > max_outcome_price + 1e-9
        ):
            break

        visible_count += level_count
        used_levels.append(
            (
                ask_price,
                level_count,
            )
        )

        if (
            visible_count
            >= required_count - 1e-9
        ):
            full_fill_price = ask_price
            break

    return {
        "best_ask": best_ask,
        "full_fill_price": full_fill_price,
        "visible_count": visible_count,
        "required_count": required_count,
        "levels": used_levels,
    }


# ============================================================
# PLACE ORDER
# ============================================================

def place_order(
    client,
    ticker,
    outcome,
    action,
    count,
    price,
    time_in_force="good_till_canceled",
    reduce_only=False,
    price_ranges=None,
):

    outcome = outcome.lower()
    action = action.lower()

    count = float(
        count
    )

    requested_outcome_price = (
        _clamp_price(
            price
        )
    )

    # ========================================================
    # CONVERT TO V2 BOOK
    # ========================================================

    side, requested_v2_price = (
        outcome_to_v2(
            outcome=outcome,
            action=action,
            outcome_price=requested_outcome_price,
        )
    )

    # ========================================================
    # SNAP V2 PRICE TO VALID TICK
    # ========================================================

    v2_price = (
        _snap_v2_price_to_valid_tick(
            price=requested_v2_price,
            price_ranges=price_ranges,
            side=side,
        )
    )

    # Convert snapped V2 price back to the bot's outcome-price
    # representation. This is the actual limit implied by the payload.
    if outcome == "yes":
        outcome_price = v2_price
    else:
        outcome_price = (
            1.0
            - v2_price
        )

    outcome_price = _clamp_price(
        outcome_price
    )

    # ========================================================
    # SAFETY
    # ========================================================

    if count <= 0:
        raise ValueError(
            "count must be greater than 0"
        )

    if (
        count
        > MAX_COUNT_PER_ORDER
    ):
        raise ValueError(
            f"Order count {count} exceeds "
            f"MAX_COUNT_PER_ORDER="
            f"{MAX_COUNT_PER_ORDER}"
        )

    # 신규 BUY 주문에만 주문 비용 제한 적용
    if (
        action == "buy"
        and not reduce_only
    ):

        estimated_cost = (
            outcome_price
            * count
        )

        if (
            estimated_cost
            > MAX_ORDER_COST_DOLLARS
        ):

            raise ValueError(
                f"Estimated order cost "
                f"${estimated_cost:.2f} exceeds "
                f"MAX_ORDER_COST_DOLLARS="
                f"${MAX_ORDER_COST_DOLLARS:.2f}"
            )

    payload = {
        "ticker": ticker,

        "client_order_id": str(
            uuid.uuid4()
        ),

        "side": side,

        "count": (
            f"{count:.2f}"
        ),

        "price": (
            f"{v2_price:.4f}"
        ),

        "time_in_force": (
            time_in_force
        ),

        "self_trade_prevention_type": (
            "taker_at_cross"
        ),

        "reduce_only": (
            reduce_only
        ),

        "exchange_index": EXCHANGE_INDEX,
    }

    print(
        "\n[실제 API 주문 전송]"
        if not DRY_RUN
        else "\n[DRY RUN 주문]"
    )

    print(
        f"요청 outcome 가격: "
        f"${requested_outcome_price:.4f}"
    )

    if abs(
        outcome_price
        - requested_outcome_price
    ) > 1e-9:

        print(
            f"유효 tick 보정: "
            f"${requested_outcome_price:.4f} "
            f"-> ${outcome_price:.4f}"
        )

    print(
        f"주문: "
        f"{action} "
        f"{outcome} "
        f"x{count:g} "
        f"@ ${outcome_price:.4f}"
    )

    print(
        f"V2 book: "
        f"{side} "
        f"@ ${v2_price:.4f}"
    )


    print(
        f"Exchange index: "
        f"{EXCHANGE_INDEX}"
    )

    # ========================================================
    # DRY RUN
    # ========================================================

    if DRY_RUN:

        return {
            "client_order_id": (
                payload[
                    "client_order_id"
                ]
            ),

            "fill_count": (
                f"{count:.2f}"
            ),

            "average_fill_price": (
                f"{v2_price:.4f}"
            ),

            "average_outcome_price": (
                f"{outcome_price:.4f}"
            ),

            "average_fee_paid": (
                "0.0000"
            ),

            "remaining_count": (
                "0.00"
            ),

            "dry_run": True,
        }

    # ========================================================
    # LIVE ORDER
    # ========================================================

    response = client.request(
        "POST",
        "/portfolio/events/orders",
        json=payload,
    )

    # ========================================================
    # NORMALIZE RESPONSE
    # ========================================================

    if response is None:
        return None

    raw_fill_price = response.get(
        "average_fill_price"
    )

    outcome_fill_price = (
        v2_fill_to_outcome_price(
            outcome,
            raw_fill_price,
        )
    )

    response[
        "raw_average_fill_price"
    ] = raw_fill_price

    response[
        "average_outcome_price"
    ] = outcome_fill_price

    response[
        "requested_outcome"
    ] = outcome

    response[
        "requested_action"
    ] = action

    response[
        "requested_outcome_price"
    ] = requested_outcome_price

    response[
        "snapped_outcome_price"
    ] = outcome_price

    response[
        "v2_book_side"
    ] = side

    response[
        "requested_v2_book_price"
    ] = requested_v2_price

    response[
        "v2_book_price"
    ] = v2_price

    return response


# ============================================================
# GET ORDER
# ============================================================

def get_order(
    client,
    order_id,
    ticker=None,
):
    params = {"exchange_index": EXCHANGE_INDEX}
    if ticker:
        params["market_ticker"] = ticker

    return client.request(
        "GET",
        f"/portfolio/events/orders/{order_id}",
        params=params,
    )


# ============================================================
# CANCEL ORDER
# ============================================================

def cancel_order(
    client,
    order_id,
    ticker,
):

    if DRY_RUN:

        print(
            f"[DRY RUN] Cancel "
            f"{order_id}"
        )

        return {
            "order_id": order_id,
            "status": "canceled",
        }

    return client.request(
        "DELETE",
        (
            f"/portfolio/events/orders/"
            f"{order_id}"
        ),
        params={
            "market_ticker": ticker,
            "exchange_index": EXCHANGE_INDEX,
        },
    )
