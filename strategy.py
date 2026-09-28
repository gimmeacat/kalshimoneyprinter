import math
import os
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Optional, Tuple

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


# ============================================================
# FIXED HIGH-CONFIDENCE STRATEGY PARAMETERS
# ============================================================

DECISION_SECONDS: float = float(os.getenv("DECISION_SECONDS", "360"))
DECISION_WINDOW_SECONDS: float = float(
    os.getenv("DECISION_WINDOW_SECONDS", "10")
)
MIN_SETTLEMENT_CONFIDENCE: float = float(
    os.getenv("MIN_SETTLEMENT_CONFIDENCE", "0.80")
)
CORRECTION_BETA: float = float(os.getenv("CORRECTION_BETA", "1.662"))
MIN_HISTORY_SAMPLES: int = int(os.getenv("MIN_HISTORY_SAMPLES", "8"))
TRADE_COUNT: float = float(os.getenv("TRADE_COUNT", "1"))


@dataclass
class StrategyResult:
    signal: Optional[dict]
    reason: str
    decision_ready: bool = False
    strategy_type: str = "HIGH_CONFIDENCE_6M"
    market_yes_probability: Optional[float] = None
    diffusion_yes_probability: Optional[float] = None
    corrected_yes_probability: Optional[float] = None
    chosen_confidence: Optional[float] = None
    target_price: Optional[float] = None
    brti_price: Optional[float] = None
    distance_dollars: Optional[float] = None


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip_probability(value: float) -> float:
    return min(0.999, max(0.001, float(value)))


def _logit(probability: float) -> float:
    p = _clip_probability(probability)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def is_decision_time(seconds_remaining) -> bool:
    """Allow exactly one bot decision in the first poll window after T-6:00."""
    seconds = _to_float(seconds_remaining)
    if seconds is None:
        return False

    return (
        DECISION_SECONDS - DECISION_WINDOW_SECONDS
        <= seconds
        <= DECISION_SECONDS
    )


def _book(orderbook: Optional[dict]) -> dict:
    if not isinstance(orderbook, dict):
        return {}
    return (
        orderbook.get("orderbook_fp")
        or orderbook.get("orderbook")
        or {}
    )


def _best_bid(orderbook: Optional[dict], outcome: str) -> Optional[float]:
    book = _book(orderbook)
    key = "yes_dollars" if outcome == "yes" else "no_dollars"
    fallback_key = "yes" if outcome == "yes" else "no"
    levels = book.get(key) or book.get(fallback_key) or []

    best: Optional[float] = None
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue

        price = _to_float(level[0])
        count = _to_float(level[1])

        if price is None or count is None or count <= 0:
            continue
        if not (0.0 < price < 1.0):
            continue

        if best is None or price > best:
            best = price

    return best


def _best_ask(orderbook: Optional[dict], outcome: str) -> Optional[float]:
    # Kalshi exposes YES/NO bids. Buying one side takes the opposite-side bid.
    opposite = "no" if outcome == "yes" else "yes"
    opposite_bid = _best_bid(orderbook, opposite)
    if opposite_bid is None:
        return None
    return round(1.0 - opposite_bid, 4)


def _market_quotes(
    market: dict,
    orderbook: Optional[dict],
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    yes_bid = _best_bid(orderbook, "yes")
    no_bid = _best_bid(orderbook, "no")
    yes_ask = _best_ask(orderbook, "yes")
    no_ask = _best_ask(orderbook, "no")

    # Market-summary fallback only when the live orderbook is unavailable.
    if yes_bid is None:
        yes_bid = _to_float(market.get("yes_bid_dollars"))
    if yes_ask is None:
        yes_ask = _to_float(market.get("yes_ask_dollars"))
    if no_bid is None:
        no_bid = _to_float(market.get("no_bid_dollars"))
    if no_ask is None:
        no_ask = _to_float(market.get("no_ask_dollars"))

    if no_ask is None and yes_bid is not None:
        no_ask = 1.0 - yes_bid
    if yes_ask is None and no_bid is not None:
        yes_ask = 1.0 - no_bid

    return yes_bid, yes_ask, no_bid, no_ask


def market_probability_yes(
    market: dict,
    orderbook: Optional[dict],
) -> Optional[float]:
    """Kalshi YES bid/ask midpoint used as the prior probability."""
    yes_bid, yes_ask, _, _ = _market_quotes(market, orderbook)

    if yes_bid is None or yes_ask is None:
        return None
    if not (0.0 < yes_bid < 1.0 and 0.0 < yes_ask < 1.0):
        return None
    if yes_bid > yes_ask:
        return None

    return _clip_probability((yes_bid + yes_ask) / 2.0)


def model_probability_yes(
    current_btc_price,
    target_price,
    seconds_remaining,
    volatility_per_sqrt_second,
) -> Optional[float]:
    """Zero-drift diffusion estimate used only as one correction feature."""
    current = _to_float(current_btc_price)
    target = _to_float(target_price)
    seconds = _to_float(seconds_remaining)
    volatility = _to_float(volatility_per_sqrt_second)

    if (
        current is None
        or target is None
        or seconds is None
        or volatility is None
        or current <= 0
        or target <= 0
        or seconds <= 0
        or volatility <= 0
    ):
        return None

    remaining_sigma = volatility * math.sqrt(seconds)
    if remaining_sigma <= 0:
        return None

    log_distance = math.log(current / target)
    z = log_distance / remaining_sigma
    return _clip_probability(NormalDist().cdf(z))


def corrected_probability_yes(
    market_yes: float,
    diffusion_yes: float,
) -> float:
    """
    Kalshi is the prior. The diffusion model supplies one frozen correction.

    logit(p_corrected)
        = logit(p_market) + beta * (p_diffusion - p_market)
    """
    adjusted_logit = (
        _logit(market_yes)
        + CORRECTION_BETA * (diffusion_yes - market_yes)
    )
    return _clip_probability(_sigmoid(adjusted_logit))


def _result(
    *,
    signal: Optional[dict],
    reason: str,
    decision_ready: bool,
    market_yes_probability: Optional[float],
    diffusion_yes_probability: Optional[float],
    corrected_yes_probability: Optional[float],
    chosen_confidence: Optional[float],
    target_price: Optional[float],
    brti_price: Optional[float],
    distance_dollars: Optional[float],
) -> StrategyResult:
    """Typed constructor helper; avoids ambiguous **dict unpacking for Pylance."""
    return StrategyResult(
        signal=signal,
        reason=reason,
        decision_ready=decision_ready,
        market_yes_probability=market_yes_probability,
        diffusion_yes_probability=diffusion_yes_probability,
        corrected_yes_probability=corrected_yes_probability,
        chosen_confidence=chosen_confidence,
        target_price=target_price,
        brti_price=brti_price,
        distance_dollars=distance_dollars,
    )


def evaluate_market(
    market: dict,
    orderbook: Optional[dict],
    seconds_remaining,
    current_btc_price,
    recent_volatility,
    history_samples,
    recent_change=None,
) -> StrategyResult:
    # recent_change is intentionally unused. It remains in the signature so the
    # live bot and recorder interfaces stay simple and backwards-compatible.
    del recent_change

    target = _to_float(market.get("floor_strike"))
    brti = _to_float(current_btc_price)
    distance = (
        brti - target
        if brti is not None and target is not None
        else None
    )

    if not is_decision_time(seconds_remaining):
        return _result(
            signal=None,
            reason=f"WAIT | decision at T-{DECISION_SECONDS / 60:.0f}m",
            decision_ready=False,
            market_yes_probability=None,
            diffusion_yes_probability=None,
            corrected_yes_probability=None,
            chosen_confidence=None,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    # Reaching this branch consumes the one decision for this market in bot.py.
    if target is None or brti is None:
        return _result(
            signal=None,
            reason="NO TRADE | missing Target/BRTI at decision time",
            decision_ready=True,
            market_yes_probability=None,
            diffusion_yes_probability=None,
            corrected_yes_probability=None,
            chosen_confidence=None,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    sample_count = 0
    try:
        sample_count = int(history_samples)
    except (TypeError, ValueError):
        sample_count = 0

    if sample_count < MIN_HISTORY_SAMPLES:
        return _result(
            signal=None,
            reason="NO TRADE | insufficient BRTI history at decision time",
            decision_ready=True,
            market_yes_probability=None,
            diffusion_yes_probability=None,
            corrected_yes_probability=None,
            chosen_confidence=None,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    market_yes = market_probability_yes(market, orderbook)
    if market_yes is None:
        return _result(
            signal=None,
            reason="NO TRADE | market prior unavailable",
            decision_ready=True,
            market_yes_probability=None,
            diffusion_yes_probability=None,
            corrected_yes_probability=None,
            chosen_confidence=None,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    diffusion_yes = model_probability_yes(
        current_btc_price=brti,
        target_price=target,
        seconds_remaining=seconds_remaining,
        volatility_per_sqrt_second=recent_volatility,
    )
    if diffusion_yes is None:
        return _result(
            signal=None,
            reason="NO TRADE | diffusion input unavailable",
            decision_ready=True,
            market_yes_probability=market_yes,
            diffusion_yes_probability=None,
            corrected_yes_probability=None,
            chosen_confidence=None,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    corrected_yes: float = corrected_probability_yes(
        market_yes,
        diffusion_yes,
    )
    corrected_no: float = 1.0 - corrected_yes

    if corrected_yes >= corrected_no:
        outcome = "yes"
        confidence: float = corrected_yes
        market_probability: float = market_yes
    else:
        outcome = "no"
        confidence = corrected_no
        market_probability = 1.0 - market_yes

    _, yes_ask, _, no_ask = _market_quotes(market, orderbook)
    signal_price = yes_ask if outcome == "yes" else no_ask

    if confidence < MIN_SETTLEMENT_CONFIDENCE:
        return _result(
            signal=None,
            reason=(
                f"NO TRADE | confidence {confidence:.1%} "
                f"< {MIN_SETTLEMENT_CONFIDENCE:.0%}"
            ),
            decision_ready=True,
            market_yes_probability=market_yes,
            diffusion_yes_probability=diffusion_yes,
            corrected_yes_probability=corrected_yes,
            chosen_confidence=confidence,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    if signal_price is None or not (0.0 < signal_price < 1.0):
        return _result(
            signal=None,
            reason="NO TRADE | executable ask unavailable",
            decision_ready=True,
            market_yes_probability=market_yes,
            diffusion_yes_probability=diffusion_yes,
            corrected_yes_probability=corrected_yes,
            chosen_confidence=confidence,
            target_price=target,
            brti_price=brti,
            distance_dollars=distance,
        )

    signal = {
        "action": "buy",
        "outcome": outcome,
        "count": TRADE_COUNT,
        "price": float(signal_price),
        "strategy_type": "HIGH_CONFIDENCE_6M",
        # Existing loggers keep this field name. It now means the corrected
        # settlement probability for the chosen outcome, not old edge-model P.
        "model_probability": float(confidence),
        "market_probability": float(market_probability),
        "edge": None,
        "corrected_yes_probability": float(corrected_yes),
        "diffusion_yes_probability": float(diffusion_yes),
        "confidence": float(confidence),
    }

    return _result(
        signal=signal,
        reason=(
            f"BUY {outcome.upper()} | corrected={confidence:.1%} | "
            f"market={market_probability:.1%} | ask=${signal_price:.4f}"
        ),
        decision_ready=True,
        market_yes_probability=market_yes,
        diffusion_yes_probability=diffusion_yes,
        corrected_yes_probability=corrected_yes,
        chosen_confidence=confidence,
        target_price=target,
        brti_price=brti,
        distance_dollars=distance,
    )
